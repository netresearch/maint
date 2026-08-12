#!/usr/bin/env python3
"""Shared GitHub REST client and Matrix webhook posting for the maint scripts.

Extracted from `check-stars.py` so that every scheduled script in this repo
speaks to GitHub through the SAME rate-limit policy. The policy is not obvious
and was paid for in broken runs (see the comments on the constants below); a
second script re-implementing a shorter, friendlier-looking version of it would
re-earn those failures rather than inherit the fix.

Imported as a plain module, not a package: the scripts here run as
`python scripts/<name>.py`, which puts `scripts/` on sys.path. Tests that load a
script by path must insert `scripts/` themselves.
"""

import os
import time
from datetime import datetime, timezone

import requests

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# --- Rate-limit backoff ------------------------------------------------------
# A rate limit needs a completely different amount of patience from a flaky
# gateway, so it gets its own attempt count and its own wait schedule. The old
# shared 1+2+4s ladder gave a secondary limit seven seconds to clear, which is
# roughly an order of magnitude short of what GitHub asks for, and it killed two
# scheduled runs (2026-08-08 21:39 and 2026-08-09 21:40 UTC) on a 403 that a
# longer wait would have ridden out.
RATE_LIMIT_MAX_RETRIES = 6
# Floor for a secondary limit that sends neither Retry-After nor an exhausted
# budget header — GitHub's documented advice is "wait at least one minute" and
# back off exponentially, so this doubles per attempt: 60s, 120s, 240s, ...
SECONDARY_RATE_LIMIT_WAIT = 60
# Total seconds this process may spend asleep waiting out rate limits, across
# every request it makes. THE SCHEDULE IS THE OUTER RETRY LOOP: every caller
# here runs on a cron, so a run that cannot get through in 10 minutes should
# hand over to its successor rather than occupy a runner — and for the
# 15-minute star-notifications cadence, rather than overlap the next run. This
# also bounds the job far below the 6-hour Actions timeout no matter how many of
# the ~280 org repos are still unwalked when the limit hits — a per-request
# budget would not, since one run makes several hundred requests.
RATE_LIMIT_TOTAL_BUDGET = 600
_rate_limit_slept = 0.0


class AuthError(Exception):
    """The token may not read an endpoint.

    Raised by github_request on a non-rate-limit 401/403. Whether it is fatal is
    the caller's decision: for a prerequisite (no token at all, or the org-repos
    listing) there is no work left to do, but for one repo among hundreds the
    caller is expected to catch it, skip that repo and carry on.
    """


class RateLimitError(Exception):
    """GitHub is still rate-limiting us after the retry policy gave up.

    Deliberately NOT an AuthError and NOT a RequestException: a rate limit is
    systemic rather than per-repo, so the per-repo fetchers must not catch it
    and carry on walking the remaining repos — every one of them would hit the
    same wall, and the run would end up reporting a few hundred bogus
    "inaccessible" repos instead of the one real cause. It propagates to main()
    and turns the run red with a message that names the limit and its reset.
    """


def is_rate_limited(response: requests.Response) -> bool:
    """Distinguish a rate-limit 403 (transient) from a permission 403 (not).

    A primary limit zeroes the budget header. A secondary limit does not, and only
    sometimes sends Retry-After, so the body is the last resort.
    https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
    """
    if response.headers.get("X-RateLimit-Remaining") == "0":
        return True
    if "Retry-After" in response.headers:
        return True
    return "secondary rate limit" in response.text.lower()


def parse_retry_after(response: requests.Response, default: float) -> float:
    """Retry-After in seconds, falling back to `default` when absent or unusable.

    GitHub documents this header as an integer number of seconds, but the RFC
    also permits an HTTP-date. An unexpected value must not crash the retry loop
    with a ValueError — that would be a worse failure than the one being
    retried — so anything unparseable falls back instead.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        print(f"Ignoring unparseable Retry-After header {raw!r}, using {default:.0f}s", flush=True)
        return default


def rate_limit_reset_text(response: requests.Response) -> str:
    """Human-readable reset time from x-ratelimit-reset, for the give-up message."""
    raw = response.headers.get("X-RateLimit-Reset")
    if raw is None:
        return "unknown (no x-ratelimit-reset header)"
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return f"unparseable ({raw!r})"


def rate_limit_wait(response: requests.Response, attempt: int) -> tuple[float, str]:
    """How long to wait before retrying a rate-limited request, and on what evidence.

    Returns (seconds, reason) so the log line can name the signal it followed.
    Order matters:

    1. Retry-After is GitHub telling us directly, so it always wins.
    2. A zeroed X-RateLimit-Remaining means the PRIMARY hourly budget is spent,
       and X-RateLimit-Reset says exactly when it refills — wait for that rather
       than guess, since no shorter wait can possibly succeed.
    3. Otherwise it is a SECONDARY limit. X-RateLimit-Reset does NOT describe
       one (it still tracks the primary window, often an hour out), so honouring
       it here would over-wait wildly; back off from the one-minute floor.

    A second of slack is added so the retry does not race the reset and earn a
    fresh 403 for arriving a few milliseconds early.
    """
    exponential = SECONDARY_RATE_LIMIT_WAIT * 2 ** attempt
    if "Retry-After" in response.headers:
        return parse_retry_after(response, default=exponential) + 1, "the Retry-After header"
    if response.headers.get("X-RateLimit-Remaining") == "0":
        raw = response.headers.get("X-RateLimit-Reset")
        if raw is not None:
            try:
                until_reset = max(0.0, float(raw) - time.time())
            except ValueError:
                until_reset = None
            if until_reset is not None:
                return until_reset + 1, f"the primary budget resetting at {rate_limit_reset_text(response)}"
    return exponential, f"secondary-limit backoff from a {SECONDARY_RATE_LIMIT_WAIT}s floor"


def spend_rate_limit_budget(seconds: float) -> None:
    """Charge a wait against the process-wide rate-limit budget."""
    global _rate_limit_slept
    _rate_limit_slept += seconds


def reset_rate_limit_budget() -> None:
    """Zero the process-wide budget. For tests, which must not inherit each other's."""
    global _rate_limit_slept
    _rate_limit_slept = 0.0


def describe_rate_limit(url: str, response: requests.Response, attempts: int, why: str) -> str:
    """Explain a rate-limit give-up in terms a run log can be read with.

    The point is that nobody should have to open this file to find out what
    "403 Client Error: Forbidden" meant — the message names the kind of limit,
    the headers it was read from, and when it lifts.
    """
    remaining = response.headers.get("X-RateLimit-Remaining", "?")
    limit = response.headers.get("X-RateLimit-Limit", "?")
    kind = "primary" if remaining == "0" else "secondary"
    return (
        f"GitHub {kind} rate limit still in effect after {attempts} attempt(s) — {why}. "
        f"Last response: HTTP {response.status_code} for {url}; "
        f"x-ratelimit-remaining={remaining}/{limit}, "
        f"Retry-After={response.headers.get('Retry-After', 'absent')}, "
        f"resets at {rate_limit_reset_text(response)}. "
        f"Waited {_rate_limit_slept:.0f}s of the {RATE_LIMIT_TOTAL_BUDGET}s per-run budget."
    )


def github_request(url: str, accept: str = "application/vnd.github+json", max_retries: int = 3) -> list | dict:
    """Make authenticated GitHub API request with retry logic for transient errors.

    A LIST response is paginated to exhaustion and returned as one flat list. A
    DICT response is returned as-is after a single request — which is what the
    envelope-shaped Actions endpoints (`{"total_count": N, "workflow_runs": [...]}`)
    return, so callers of those get exactly one page and must handle truncation
    themselves.

    Rate limits (429, or a 403 that is_rate_limited recognises) are retried on
    their own, far more patient schedule than gateway hiccups: see
    rate_limit_wait() for how long each wait is and RATE_LIMIT_TOTAL_BUDGET for
    the ceiling on all of them. Giving up raises RateLimitError, whose message
    names the limit and its reset rather than a bare "403 Forbidden".
    """
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    results = []
    while url:
        transient_attempt = 0
        rate_limit_attempt = 0
        while True:
            try:
                response = requests.get(url, headers=headers, timeout=30)

                # Rate limits first: a 403 that is_rate_limited() recognises is
                # transient and must not fall through to the AuthError branch.
                if response.status_code == 429 or (
                    response.status_code == 403 and is_rate_limited(response)
                ):
                    wait, reason = rate_limit_wait(response, rate_limit_attempt)
                    budget_left = RATE_LIMIT_TOTAL_BUDGET - _rate_limit_slept
                    if rate_limit_attempt >= RATE_LIMIT_MAX_RETRIES:
                        raise RateLimitError(describe_rate_limit(
                            url, response, rate_limit_attempt,
                            f"exhausted all {RATE_LIMIT_MAX_RETRIES} rate-limit retries",
                        ))
                    if wait > budget_left:
                        # Sleeping a partial wait would just burn an attempt on a
                        # request that is certain to be refused, so stop here and
                        # let the next scheduled run try with a fresh budget.
                        raise RateLimitError(describe_rate_limit(
                            url, response, rate_limit_attempt,
                            f"the next wait of {wait:.0f}s exceeds the {budget_left:.0f}s left in the budget",
                        ))
                    rate_limit_attempt += 1
                    spend_rate_limit_budget(wait)
                    # Flushed because stdout is block-buffered under Actions: an
                    # unflushed print during a multi-minute sleep makes the job
                    # look hung with no explanation until the process exits.
                    print(
                        f"Rate limited (HTTP {response.status_code}) on {url} — retry "
                        f"{rate_limit_attempt}/{RATE_LIMIT_MAX_RETRIES} in {wait:.0f}s per {reason} "
                        f"({_rate_limit_slept:.0f}/{RATE_LIMIT_TOTAL_BUDGET}s of the budget used)",
                        flush=True,
                    )
                    time.sleep(wait)
                    continue

                if response.status_code in (401, 403):
                    try:
                        detail = response.json().get("message", response.reason)
                    except (ValueError, AttributeError):
                        detail = response.reason
                    raise AuthError(f"{response.status_code} for {url}: {detail}")

                # Transient gateway errors are raised into the handler below,
                # which owns the attempt counting and the short backoff.
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, list):
                    return data
                results.extend(data)
                url = response.links.get("next", {}).get("url")
                break  # Success; continue with the next page, if any.
            except requests.exceptions.RequestException as e:
                # Client errors are the server's final answer, so retrying only burns time
                status = e.response.status_code if e.response is not None else None
                if status is not None and 400 <= status < 500:
                    raise
                transient_attempt += 1
                if transient_attempt >= max_retries:
                    raise
                wait_time = 2 ** (transient_attempt - 1)
                if e.response is not None:
                    wait_time = parse_retry_after(e.response, default=wait_time)
                print(f"Retry {transient_attempt}/{max_retries}: {e}, waiting {wait_time:.0f}s", flush=True)
                time.sleep(wait_time)
    return results


def post_to_matrix(webhook_url: str, message: str) -> None:
    """Send one message to Matrix via a Hookshot webhook.

    The timeout is not optional: without it a wedged webhook holds the job open
    until the Actions timeout, which turns a missed notification into a burnt
    runner-hour and a red scheduled run.
    """
    response = requests.post(webhook_url, json={"text": message}, timeout=30)
    response.raise_for_status()
