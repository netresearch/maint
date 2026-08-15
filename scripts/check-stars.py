#!/usr/bin/env python3
"""Check for new GitHub stars, forks, watchers, and dependents on netresearch org repos and notify Matrix."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

GITHUB_API = "https://api.github.com"
ORG_NAME = os.environ.get("ORG_NAME", "netresearch")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
MATRIX_WEBHOOK_URL = os.environ["MATRIX_WEBHOOK_URL"]
STATE_FILE = Path("state/stars-state.json")
MAX_NOTIFICATIONS = 20
FEED_URL = "https://github.com/netresearch/maint/actions/workflows/star-notifications.yml"

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
# every request it makes. THE SCHEDULE IS THE OUTER RETRY LOOP: the workflow
# fires every 15 minutes, so a run that cannot get through in 10 minutes should
# hand over to its successor rather than occupy a runner and overlap the next
# one. This also bounds the job far below the 6-hour Actions timeout no matter
# how many of the ~280 org repos are still unwalked when the limit hits — a
# per-request budget would not, since one run makes several hundred requests.
RATE_LIMIT_TOTAL_BUDGET = 600
_rate_limit_slept = 0.0

# --- Chronic starvation ------------------------------------------------------
# A deferred give-up (see RateLimitError) ends the run green, which is right for
# an hour whose budget someone else drained. It is wrong for a token that is
# permanently starved: every run would hand over to a successor that hands over
# again, forever green, processing zero repos, with only a ::warning:: nobody
# reads. So consecutive deferrals are counted across runs, and the streak turns
# the run red once it can no longer be explained by a single bad hour.
#
# The number comes from the two cadences involved: the workflow runs every 15
# minutes (4 runs/hour) and the primary budget resets hourly. Four deferrals in
# a row span only 45 minutes and can therefore all sit inside ONE drained hour —
# exactly the case that must stay green. The fifth is at least 60 minutes after
# the first, so at least one full hourly reset has come and gone without giving
# this token enough budget to finish a run: no longer a bad hour, a bad setup.
DEFERRED_GIVEUP_STREAK_KEY = "deferred_giveup_streak"
MAX_CONSECUTIVE_DEFERRALS = 5

# In-memory cache for user details (login -> user info dict)
_user_cache: dict[str, dict] = {}

# Repos whose social endpoints the token was NOT allowed to read this run, with
# the reason. Since the 2026-06-30 API restriction the stargazers/subscribers/
# forks endpoints are "limited to admins and collaborators", so a token that is
# deliberately scoped narrower gets a 403 on some repos. Those are skipped and
# summarised at the end rather than aborting the whole run — the goal is to
# process every repo the token CAN read, not to fail the schedule on the first
# one it cannot.
_inaccessible: dict[str, str] = {}


def note_inaccessible(repo_full_name: str, endpoint: str, detail: str) -> None:
    """Record (once per repo) that the token could not read a social endpoint."""
    if repo_full_name not in _inaccessible:
        _inaccessible[repo_full_name] = f"{endpoint}: {detail}"
        print(f"Skipping {repo_full_name} ({endpoint}): {detail}")


class AuthError(Exception):
    """The token may not read an endpoint.

    Raised by github_request on a non-rate-limit 401/403. It is FATAL only for
    the prerequisites (no token at all, or the org-repos listing) — without
    those there is no work to do. For an individual repo's social endpoints it
    is caught by the per-repo fetchers, which skip that repo and continue, so
    one inaccessible repo never aborts the whole run.
    """


class RateLimitError(Exception):
    """GitHub is still rate-limiting us after the retry policy gave up.

    Deliberately NOT an AuthError and NOT a RequestException: a rate limit is
    systemic rather than per-repo, so the per-repo fetchers must not catch it
    and carry on walking the remaining repos — every one of them would hit the
    same wall, and the run would end up reporting a few hundred bogus
    "inaccessible" repos instead of the one real cause. It propagates out of
    main() with a message that names the limit and its reset.

    `deferred_to_next_run` marks the DESIGNED give-up: the primary hourly
    budget is spent and the wait to its documented reset exceeds what is left
    of this run's sleep budget. The schedule is the outer retry loop (see
    RATE_LIMIT_TOTAL_BUDGET), the reset is at most an hour out, and no state
    has been saved yet, so handing over to a successor is the intended
    behaviour — run() turns it into a ::warning:: and exit 0. Every other
    give-up (a secondary limit mid-processing, retries exhausted) stays False
    and turns the run red.
    """

    def __init__(self, message: str, *, deferred_to_next_run: bool = False):
        super().__init__(message)
        self.deferred_to_next_run = deferred_to_next_run


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
                        # Deferring is only safe for a PRIMARY limit (zeroed
                        # budget header, same test describe_rate_limit uses):
                        # its reset is documented and at most an hour out, so a
                        # successor run is guaranteed fresh quota. A secondary
                        # limit that outlasted 600s of backoff is abuse
                        # detection with no promised end — that stays a failure.
                        raise RateLimitError(describe_rate_limit(
                            url, response, rate_limit_attempt,
                            f"the next wait of {wait:.0f}s exceeds the {budget_left:.0f}s left in the budget",
                        ), deferred_to_next_run=response.headers.get("X-RateLimit-Remaining") == "0")
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


def get_user_details(login: str) -> dict | None:
    """Get detailed user info (name, company, followers) with caching.

    Returns:
        dict with keys: login, name, company, followers, html_url
        None if fetch failed
    """
    if login in _user_cache:
        return _user_cache[login]

    try:
        user = github_request(f"{GITHUB_API}/users/{login}")
        details = {
            "login": login,
            "name": user.get("name"),
            "company": user.get("company"),
            "followers": user.get("followers", 0),
            "html_url": user.get("html_url", f"https://github.com/{login}"),
        }
        _user_cache[login] = details
        return details
    except (requests.exceptions.RequestException, AuthError) as e:
        # A missing user detail must never abort the run — the caller falls
        # back to a bare profile link.
        print(f"Failed to get user details for {login}: {e}")
        return None


def format_user_info(login: str, html_url: str) -> str:
    """Format user info string: 'login (Name, @Company, N 👥)' with link."""
    details = get_user_details(login)
    if not details:
        return f"[{login}]({html_url})"

    parts = []
    # Add real name if different from login
    if details["name"] and details["name"].lower() != login.lower():
        parts.append(details["name"])
    # Add company if present
    if details["company"]:
        company = details["company"].strip()
        if not company.startswith("@"):
            company = f"@{company}"
        parts.append(company)
    # Always add followers
    parts.append(f"{details['followers']} 👥")

    info = ", ".join(parts)
    return f"[{login}]({details['html_url']}) ({info})"


def get_org_repos() -> list[dict]:
    """Get all public repos in the organization (including forks)."""
    repos = github_request(f"{GITHUB_API}/orgs/{ORG_NAME}/repos?type=all&per_page=100")
    return [
        {
            "name": r["name"],
            "full_name": r["full_name"],
            "url": r["html_url"],
            "stargazers_count": r["stargazers_count"],
            "forks_count": r["forks_count"],
            "watchers_count": r.get("subscribers_count", r.get("watchers_count", 0)),
            # Empty repos (no commits) have no dependency graph page. A missing
            # size is treated as non-empty so we still scrape rather than skip.
            "is_empty": r.get("size") == 0,
            # Since the 2026-06-30 API restriction, the stargazers/subscribers/
            # forks endpoints of an ARCHIVED repo are not readable even by a
            # fine-grained PAT (403 "Resource not accessible…" for the PAT, 404
            # for other token types). Those repos are read-only, so their social
            # counts cannot change anyway — the run skips the token-gated fetches
            # for them (see main()) rather than aborting on the AuthError.
            "archived": r.get("archived", False),
        }
        for r in repos
        if not r.get("private", False)
    ]


def get_stargazers(repo_full_name: str) -> list[dict] | None:
    """Get stargazers for a repo with timestamps.

    Returns:
        list[dict]: List of stargazers if successful
        None: If fetch failed
    """
    try:
        return github_request(
            f"{GITHUB_API}/repos/{repo_full_name}/stargazers?per_page=100",
            accept="application/vnd.github.star+json",
        )
    except AuthError as e:
        # The token may not read THIS repo's stargazers (private/archived, or a
        # narrowly-scoped token post-2026-06-30). Skip it and carry on.
        note_inaccessible(repo_full_name, "stargazers", str(e))
        return None
    except requests.exceptions.RequestException as e:
        print(f"Failed to get stargazers for {repo_full_name}: {e}")
        return None


def get_forks(repo_full_name: str) -> list[dict] | None:
    """Get forks for a repo.

    Returns:
        list[dict]: List of forks if successful
        None: If fetch failed
    """
    try:
        return github_request(f"{GITHUB_API}/repos/{repo_full_name}/forks?per_page=100")
    except AuthError as e:
        note_inaccessible(repo_full_name, "forks", str(e))
        return None
    except requests.exceptions.RequestException as e:
        print(f"Failed to get forks for {repo_full_name}: {e}")
        return None


def get_watchers(repo_full_name: str) -> list[dict] | None:
    """Get watchers (subscribers) for a repo.

    Returns:
        list[dict]: List of watchers if successful
        None: If fetch failed
    """
    try:
        return github_request(f"{GITHUB_API}/repos/{repo_full_name}/subscribers?per_page=100")
    except AuthError as e:
        note_inaccessible(repo_full_name, "subscribers", str(e))
        return None
    except requests.exceptions.RequestException as e:
        print(f"Failed to get watchers for {repo_full_name}: {e}")
        return None


def _parse_dependents(soup: BeautifulSoup) -> list[dict]:
    """Extract dependent repos from a parsed #dependents page.

    Each Box-row links to a repository; enrich it with star/fork counts via the
    API, falling back to zeros when that lookup fails.
    """
    dependents = []
    for item in soup.select('.Box-row'):
        repo_link = item.select_one('a[data-hovercard-type="repository"]')
        if not repo_link:
            continue
        dep_full_name = repo_link.get('href', '').lstrip('/')
        if not dep_full_name or '/' not in dep_full_name:
            continue
        entry = {
            "full_name": dep_full_name,
            "url": f"https://github.com/{dep_full_name}",
            "stars": 0,
            "forks": 0,
        }
        try:
            repo_info = github_request(f"{GITHUB_API}/repos/{dep_full_name}")
            entry["stars"] = repo_info.get("stargazers_count", 0)
            entry["forks"] = repo_info.get("forks_count", 0)
        except (requests.exceptions.RequestException, KeyError, ValueError) as e:
            print(f"Warning: Could not get info for dependent {dep_full_name}: {e}")
        dependents.append(entry)
    return dependents


def get_dependents(repo_full_name: str, repo_is_empty: bool = False, max_retries: int = 3) -> list[dict] | None:
    """Get dependents (repositories that depend on this repo) by scraping the network/dependents page.

    Returns:
        list[dict]: List of dependent repos if successful
        None: If fetch failed (to distinguish from "no dependents exist")
    """
    # An empty repo has no dependency graph, so /network/dependents renders the
    # "This repository is empty" page with no #dependents container. Treat it as
    # zero dependents rather than a scrape failure (avoids a false "page structure
    # may have changed" warning on every run).
    if repo_is_empty:
        return []

    url = f"https://github.com/{repo_full_name}/network/dependents"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NetresearchBot/1.0)",
    }

    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code in (429, 502, 503, 504):
                # Same defensive parse as the API path: an HTTP-date here used to
                # crash the scrape with a ValueError instead of retrying. This is
                # github.com HTML, not the API, so it keeps the short ladder — it
                # is a separate budget from the token's.
                retry_after = parse_retry_after(response, default=2 ** attempt)
                print(f"Retry {attempt + 1}/{max_retries}: {response.status_code} for {url}, waiting {retry_after:.0f}s", flush=True)
                time.sleep(retry_after)
                continue
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'html.parser')

            # Verify we got a valid dependents page by checking for expected elements
            # The page should have either dependents or a "No dependents" message
            dependents_box = soup.select_one('#dependents')
            if not dependents_box:
                print(f"Warning: Could not find #dependents container for {repo_full_name} - page structure may have changed")
                return None  # Page structure changed, don't wipe state

            return _parse_dependents(soup)  # may be empty if truly no dependents
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                print(f"Retry {attempt + 1}/{max_retries}: {e}, waiting {wait_time}s")
                time.sleep(wait_time)
            else:
                print(f"Failed to get dependents for {repo_full_name}: {e}")
                return None  # Fetch failed, don't wipe state

    return None  # All retries exhausted


def is_suspicious_empty(current: set, known: set, entity_type: str, repo_name: str) -> bool:
    """Check if getting 0 results when we had data before is suspicious.

    Returns True if we should preserve old state instead of using new empty data.
    """
    if len(current) == 0 and len(known) > 0:
        print(f"Warning: {entity_type} for {repo_name} went from {len(known)} to 0 - preserving old state")
        return True
    return False


def load_state() -> dict:
    """Load previous state from file."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"repos": {}, "last_run": None}


def write_state(state: dict) -> None:
    """Write the state file verbatim, without interpreting what is in it."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def save_state(state: dict) -> None:
    """Save the state of a COMPLETED run.

    Called from the tail of main() only, i.e. once every repo has been walked,
    which is also the definition of a run that did not give up — so this is
    where the deferral streak is cleared. Do not call it mid-loop: that would
    both stamp a last_run for a run that never finished and forgive a streak
    that is still going.
    """
    state["last_run"] = datetime.utcnow().isoformat()
    state[DEFERRED_GIVEUP_STREAK_KEY] = 0
    write_state(state)


def bump_deferred_giveup_streak() -> int:
    """Count this deferred give-up against the previous ones, and return the streak.

    Deliberately re-reads the file instead of taking main()'s in-memory state:
    by the time a give-up raises, that dict already holds fresh social data for
    the repos walked before the limit hit, and writing it would advance their
    seen-stars sets without the run having notified anything for the repos it
    never reached. Round-tripping the file touches the counter and nothing else,
    so the seen-stars data stays exactly as the previous run left it. For the
    same reason this must not go through save_state(), which stamps last_run —
    on a first-ever run that would suppress the first-run indexing and turn
    every existing stargazer into a "new" notification on the next run.
    """
    state = load_state()
    streak = state.get(DEFERRED_GIVEUP_STREAK_KEY, 0) + 1
    state[DEFERRED_GIVEUP_STREAK_KEY] = streak
    write_state(state)
    return streak


def notify_matrix(message: str) -> None:
    """Send notification to Matrix via webhook."""
    payload = {"text": message}
    response = requests.post(MATRIX_WEBHOOK_URL, json=payload)
    response.raise_for_status()


def main():
    if not GITHUB_TOKEN:
        raise AuthError("no token in the environment")
    state = load_state()
    repos = get_org_repos()
    is_first_run = not state.get("last_run")

    total_new = {"stars": 0, "forks": 0, "watchers": 0, "dependents": 0}
    notifications_sent = 0
    pending_notifications = []

    for repo in repos:
        repo_name = repo["full_name"]
        # An archived repo's stargazers/forks/subscribers endpoints are not
        # readable (403/404) since the 2026-06-30 API restriction. Skipping the
        # token-gated fetches for it — rather than aborting the whole run on the
        # AuthError — is safe: an archived repo is read-only, so those counts
        # cannot change. A genuinely bad token still aborts on the first
        # NON-archived repo, so systemic auth failures are still caught loudly.
        social_readable = not repo.get("archived", False)
        repo_state = state.get("repos", {}).get(repo_name, {})
        # Track if this repo had dependents tracking before any state migrations
        had_dependents_tracking = isinstance(repo_state, dict) and "dependents" in repo_state
        # Handle old state format (list of stargazers) -> convert to new format
        if isinstance(repo_state, list):
            repo_state = {"stars": repo_state, "forks": [], "watchers": []}
        # Stars
        known_stars = set(repo_state.get("stars", []))
        stargazers = get_stargazers(repo_name) if social_readable else None
        if stargazers is not None:
            current_stars = {s["user"]["login"] for s in stargazers}
            if is_suspicious_empty(current_stars, known_stars, "stars", repo_name):
                stars_to_save = list(known_stars)
            else:
                new_stars = current_stars - known_stars
                for stargazer in stargazers:
                    user = stargazer["user"]
                    if user["login"] in new_stars:
                        if not is_first_run:
                            user_info = format_user_info(user["login"], user["html_url"])
                            msg = f"⭐ [{repo['name']}]({repo['url']}) starred by {user_info} ([?](https://github.com/netresearch/maint))"
                            pending_notifications.append(msg)
                            print(f"Star: {user['login']} -> {repo_name}")
                        total_new["stars"] += 1
                stars_to_save = list(current_stars)
        else:
            stars_to_save = list(known_stars)

        # Forks
        known_forks = set(repo_state.get("forks", []))
        forks = get_forks(repo_name) if social_readable else None
        if forks is not None:
            current_forks = {f["owner"]["login"] for f in forks}
            if is_suspicious_empty(current_forks, known_forks, "forks", repo_name):
                forks_to_save = list(known_forks)
            else:
                new_forks = current_forks - known_forks
                for fork in forks:
                    owner = fork["owner"]
                    if owner["login"] in new_forks:
                        if not is_first_run:
                            user_info = format_user_info(owner["login"], owner["html_url"])
                            fork_url = fork.get("html_url", f"https://github.com/{fork['full_name']}")
                            msg = f"🍴 [{repo['name']}]({repo['url']}) forked by {user_info} → [{fork['full_name']}]({fork_url}) ([?](https://github.com/netresearch/maint))"
                            pending_notifications.append(msg)
                            print(f"Fork: {owner['login']} -> {repo_name}")
                        total_new["forks"] += 1
                forks_to_save = list(current_forks)
        else:
            forks_to_save = list(known_forks)

        # Watchers
        known_watchers = set(repo_state.get("watchers", []))
        watchers = get_watchers(repo_name) if social_readable else None
        if watchers is not None:
            current_watchers = {w["login"] for w in watchers}
            if is_suspicious_empty(current_watchers, known_watchers, "watchers", repo_name):
                watchers_to_save = list(known_watchers)
            else:
                new_watchers = current_watchers - known_watchers
                for watcher in watchers:
                    if watcher["login"] in new_watchers:
                        if not is_first_run:
                            user_info = format_user_info(watcher["login"], watcher["html_url"])
                            msg = f"👀 [{repo['name']}]({repo['url']}) watched by {user_info} ([?](https://github.com/netresearch/maint))"
                            pending_notifications.append(msg)
                            print(f"Watch: {watcher['login']} -> {repo_name}")
                        total_new["watchers"] += 1
                watchers_to_save = list(current_watchers)
        else:
            watchers_to_save = list(known_watchers)

        # Dependents (repositories using this repo)
        known_dependents = set(repo_state.get("dependents", []))
        dependents = get_dependents(repo_name, repo_is_empty=repo.get("is_empty", False))
        if dependents is not None:
            current_dependents = {d["full_name"] for d in dependents}
            if is_suspicious_empty(current_dependents, known_dependents, "dependents", repo_name):
                dependents_to_save = list(known_dependents)
            else:
                new_dependents = current_dependents - known_dependents
                for dependent in dependents:
                    if dependent["full_name"] in new_dependents:
                        # Only notify if not first run AND dependents were already being tracked for this repo
                        if not is_first_run and had_dependents_tracking:
                            msg = f"📦 [{repo['name']}]({repo['url']}) new dependent: [{dependent['full_name']}]({dependent['url']}) ({dependent['stars']} ⭐, {dependent['forks']} 🍴) ([?](https://github.com/netresearch/maint))"
                            pending_notifications.append(msg)
                            print(f"Dependent: {dependent['full_name']} -> {repo_name}")
                        total_new["dependents"] += 1
                dependents_to_save = list(current_dependents)
        else:
            dependents_to_save = list(known_dependents)

        # Update state
        if "repos" not in state:
            state["repos"] = {}
        state["repos"][repo_name] = {
            "stars": stars_to_save,
            "forks": forks_to_save,
            "watchers": watchers_to_save,
            "dependents": dependents_to_save,
        }

    # Send notifications (limited) - errors must not prevent state saving
    notification_errors = 0
    for msg in pending_notifications[:MAX_NOTIFICATIONS]:
        try:
            notify_matrix(msg)
            notifications_sent += 1
        except requests.exceptions.RequestException as e:
            notification_errors += 1
            print(f"Failed to send Matrix notification. Error: {type(e).__name__}")
            # After first failure, skip remaining notifications (webhook likely down)
            if notification_errors == 1:
                print("Matrix webhook appears unreachable, skipping remaining notifications")
                break

    # If there are more, send a summary
    remaining = len(pending_notifications) - MAX_NOTIFICATIONS
    if remaining > 0 and notification_errors == 0:
        summary = f"📊 +{remaining} more events. [See full log]({FEED_URL}) ([?](https://github.com/netresearch/maint))"
        try:
            notify_matrix(summary)
        except requests.exceptions.RequestException as e:
            notification_errors += 1
            print(f"Failed to send Matrix summary. Error: {type(e).__name__}")
        print(f"Truncated: {remaining} additional notifications not sent")

    # Always save state, even if notifications failed - prevents re-detecting
    # the same changes on the next run
    save_state(state)

    if is_first_run:
        totals = sum(
            len(r.get("stars", [])) + len(r.get("forks", [])) + len(r.get("watchers", [])) + len(r.get("dependents", []))
            for r in state["repos"].values()
        )
        print(f"Initial run - indexed {totals} existing entries")
    else:
        print(f"Found: {total_new['stars']} star(s), {total_new['forks']} fork(s), {total_new['watchers']} watcher(s), {total_new['dependents']} dependent(s)")
        print(f"Sent: {notifications_sent} notification(s)")
        if notification_errors > 0:
            print(f"Warning: {notification_errors} notification(s) failed to send")

    # Report — but do not fail on — repos whose social endpoints the token could
    # not read. A ::warning:: keeps it visible in the Actions UI (so a token that
    # can suddenly read NOTHING is noticeable) without turning the scheduled run
    # red on every trigger, which is the whole point of degrading gracefully.
    if _inaccessible:
        total = len(repos)
        print(
            f"::warning::Skipped {len(_inaccessible)} of {total} repo(s) whose social "
            f"endpoints the token may not read (archived, private, or a narrowly-scoped "
            f"token; see the 2026-06-30 API restriction). The rest were processed normally."
        )
        for name, reason in sorted(_inaccessible.items()):
            print(f"  - {name}: {reason}")


def run() -> int:
    """Process exit code for main(), separating designed hand-overs from failures.

    The deferred give-up (primary budget spent, wait to reset exceeds the
    per-run sleep budget) is the script doing exactly what its comments
    promise, so it must not produce a red run — a red run for a non-actionable,
    self-healing condition trains people to ignore red runs. It exits 0 with a
    ::warning:: carrying the same diagnostic text. Safe because the give-up
    raises out of the repo loop BEFORE any notification is sent and before
    save_state(), so the state file on disk is still the downloaded previous
    state — bar the deferral counter below, which is written on its own — and
    re-uploading it advances no seen-stars data.

    Every other rate-limit give-up stays red: a secondary limit that outlasts
    the budget mid-processing is abuse detection with no documented reset, and
    exhausting all retries means waits that FIT the budget never cleared the
    limit. A run of red ones means the token's hourly budget is genuinely too
    small for ~280 repos at this cadence, and the cadence or the request count
    has to give.

    A green hand-over is only sound while there is a successor that can do the
    work, so deferrals are counted across runs and the MAX_CONSECUTIVE_DEFERRALS
    one turns red anyway: at that point the hand-over has been going on longer
    than a single hourly reset, and permanent quota starvation must not hide
    behind an endless row of green runs that process nothing.
    """
    try:
        main()
    except RateLimitError as e:
        if e.deferred_to_next_run:
            streak = bump_deferred_giveup_streak()
            if streak >= MAX_CONSECUTIVE_DEFERRALS:
                print(f"::error::{e} This is give-up {streak} in a row, spanning more than the hourly "
                      f"primary-limit reset, so no run in over an hour has processed a single repo. "
                      f"The token's budget is not momentarily drained, it is too small for this "
                      f"cadence — reduce the schedule or the request count.")
                return 1
            print(f"::warning::{e} Working as designed: the next scheduled run retries with a fresh "
                  f"budget (consecutive deferral {streak} of {MAX_CONSECUTIVE_DEFERRALS} before this "
                  f"turns red).")
            return 0
        print(f"::error::{e} The next scheduled run retries with a fresh budget.")
        return 1
    except AuthError as e:
        print(f"::error::{e}. Check that STAR_NOTIFICATIONS_PAT is set, unexpired, and grants "
              f"Metadata: read on the {ORG_NAME} org.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
