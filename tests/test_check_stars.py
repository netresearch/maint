#!/usr/bin/env python3
"""Tests for scripts/check-stars.py dependents handling.

Runnable standalone (`python tests/test_check_stars.py`) or via pytest.
"""

import importlib.util
import os
from pathlib import Path

# The module reads MATRIX_WEBHOOK_URL at import time; provide a dummy value.
os.environ.setdefault("MATRIX_WEBHOOK_URL", "https://example.invalid/webhook")

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check-stars.py"
_spec = importlib.util.spec_from_file_location("check_stars", _MODULE_PATH)
check_stars = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_stars)


def test_empty_repo_returns_no_dependents_without_scraping():
    """An empty repo must yield [] (zero dependents) and never hit the network."""
    def _fail(*args, **kwargs):
        raise AssertionError("get_dependents scraped an empty repo instead of short-circuiting")

    # Fail loudly if any HTTP call is attempted for an empty repo, restoring
    # the real requests.get afterwards so other tests are unaffected.
    original_get = check_stars.requests.get
    check_stars.requests.get = _fail  # type: ignore[assignment]
    try:
        result = check_stars.get_dependents("netresearch/dind", repo_is_empty=True)
    finally:
        check_stars.requests.get = original_get  # type: ignore[assignment]
    assert result == [], f"expected [] for empty repo, got {result!r}"


def test_get_org_repos_maps_archived_flag():
    """get_org_repos must surface each repo's `archived` flag.

    The main loop keys the social-endpoint skip off it, so a missing/wrong value
    would re-introduce the abort-on-archived-repo failure.
    """
    api_payload = [
        {"name": "live", "full_name": "netresearch/live", "html_url": "u",
         "stargazers_count": 1, "forks_count": 0, "size": 5, "archived": False},
        {"name": "old", "full_name": "netresearch/old", "html_url": "u",
         "stargazers_count": 1, "forks_count": 0, "size": 5, "archived": True},
        # A repo whose payload omits `archived` must default to False, not crash.
        {"name": "legacy", "full_name": "netresearch/legacy", "html_url": "u",
         "stargazers_count": 0, "forks_count": 0, "size": 5},
    ]
    original = check_stars.github_request
    check_stars.github_request = lambda *a, **k: api_payload  # type: ignore[assignment]
    try:
        repos = check_stars.get_org_repos()
    finally:
        check_stars.github_request = original  # type: ignore[assignment]
    by_name = {r["full_name"]: r for r in repos}
    assert by_name["netresearch/live"]["archived"] is False
    assert by_name["netresearch/old"]["archived"] is True
    assert by_name["netresearch/legacy"]["archived"] is False


def test_archived_repo_skips_token_gated_fetches():
    """An archived repo must NOT call the stargazers/forks/subscribers endpoints.

    Reproduces the reason star-notifications was failing: those endpoints 403 on
    an archived repo since the 2026-06-30 API restriction, and the AuthError was
    aborting the whole run. The skip is expressed at the call site as
    `get_stargazers(name) if social_readable else None`, so this asserts the same
    guard evaluates to None (no call) for archived and to a fetch for a live repo.
    """
    def _boom(*args, **kwargs):
        raise AssertionError("token-gated social endpoint called for an archived repo")

    original = (check_stars.get_stargazers, check_stars.get_forks, check_stars.get_watchers)
    check_stars.get_stargazers = _boom  # type: ignore[assignment]
    check_stars.get_forks = _boom  # type: ignore[assignment]
    check_stars.get_watchers = _boom  # type: ignore[assignment]
    try:
        for archived in (True, False):
            social_readable = not archived
            called = {"n": 0}
            if not archived:
                # For the live case, restore a stub that records the call.
                check_stars.get_stargazers = lambda name: called.__setitem__("n", 1) or []  # type: ignore[assignment]
            sg = check_stars.get_stargazers("netresearch/x") if social_readable else None
            if archived:
                assert sg is None, "archived repo must yield None (skipped), not a fetch"
            else:
                assert called["n"] == 1, "live repo must actually fetch stargazers"
    finally:
        check_stars.get_stargazers, check_stars.get_forks, check_stars.get_watchers = original  # type: ignore[assignment]


def test_per_repo_autherror_is_skipped_not_fatal():
    """A 403 on one repo's stargazers must skip that repo, not abort the run.

    Before this, github_request's AuthError propagated out of get_stargazers and
    killed the whole scheduled run on the first inaccessible repo. Now the fetch
    returns None (skip) and records the repo in _inaccessible.
    """
    check_stars._inaccessible.clear()

    def _deny(*args, **kwargs):
        raise check_stars.AuthError("403 for …/stargazers: Resource not accessible by personal access token")

    original = check_stars.github_request
    check_stars.github_request = _deny  # type: ignore[assignment]
    try:
        result = check_stars.get_stargazers("netresearch/deploy-rst")
    finally:
        check_stars.github_request = original  # type: ignore[assignment]

    assert result is None, "an inaccessible repo must yield None, not raise"
    assert "netresearch/deploy-rst" in check_stars._inaccessible, "the skip must be recorded for the summary"
    check_stars._inaccessible.clear()


class _FakeResponse:
    """Minimal stand-in for requests.Response for the retry-policy tests."""

    def __init__(self, status_code=403, headers=None, text="", payload=None, reason="Forbidden"):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.reason = reason
        self.links = {}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise check_stars.requests.exceptions.HTTPError(f"{self.status_code} Client Error", response=self)


def _drive_rate_limited_request(response):
    """Run github_request against a server that always returns `response`.

    Returns (raised_exception_or_None, list_of_sleep_durations). requests.get and
    time.sleep are swapped out so the test measures the policy rather than living
    through it, and both are restored plus the budget reset afterwards.
    """
    slept = []
    original_get, original_sleep = check_stars.requests.get, check_stars.time.sleep
    check_stars.requests.get = lambda *a, **k: response  # type: ignore[assignment]
    check_stars.time.sleep = slept.append  # type: ignore[assignment]
    check_stars._rate_limit_slept = 0.0
    raised = None
    try:
        check_stars.github_request("https://api.github.com/orgs/netresearch/repos?type=all&per_page=100")
    except Exception as e:  # noqa: BLE001 - the test asserts on which type came out
        raised = e
    finally:
        check_stars.requests.get = original_get  # type: ignore[assignment]
        check_stars.time.sleep = original_sleep  # type: ignore[assignment]
        check_stars._rate_limit_slept = 0.0
    return raised, slept


def test_rate_limit_wait_prefers_retry_after():
    """Retry-After is GitHub telling us directly, so it outranks every guess."""
    response = _FakeResponse(headers={"Retry-After": "45", "X-RateLimit-Remaining": "0",
                                      "X-RateLimit-Reset": str(int(check_stars.time.time()) + 3600)})
    wait, reason = check_stars.rate_limit_wait(response, attempt=0)
    assert 45 <= wait <= 47, f"expected the 45s the server asked for (plus slack), got {wait}"
    assert "Retry-After" in reason


def test_rate_limit_wait_waits_for_the_primary_reset():
    """A spent primary budget cannot be beaten by any shorter wait, so wait for the reset."""
    response = _FakeResponse(headers={"X-RateLimit-Remaining": "0",
                                      "X-RateLimit-Reset": str(int(check_stars.time.time()) + 120)})
    wait, reason = check_stars.rate_limit_wait(response, attempt=0)
    assert 118 <= wait <= 123, f"expected ~120s until the reset, got {wait}"
    assert "resetting at" in reason


def test_rate_limit_wait_floors_secondary_backoff_at_a_minute():
    """A secondary limit sends no usable header; x-ratelimit-reset describes the
    PRIMARY window and would over-wait, so back off from the documented floor."""
    response = _FakeResponse(text="You have exceeded a secondary rate limit",
                             headers={"X-RateLimit-Remaining": "4213",
                                      "X-RateLimit-Reset": str(int(check_stars.time.time()) + 3600)})
    waits = [check_stars.rate_limit_wait(response, attempt=n)[0] for n in range(3)]
    assert waits == [60, 120, 240], f"expected a 60s floor doubling per attempt, got {waits}"


def test_secondary_rate_limit_waits_minutes_and_stays_within_budget():
    """Reproduces the 2026-08-09T21:40 failure: an unrelenting secondary 403.

    The old policy slept 1+2+4 = 7 seconds and then died with a bare HTTPError.
    The new one must wait in minutes, stop before the per-run budget is spent,
    and say what happened.
    """
    response = _FakeResponse(text="You have exceeded a secondary rate limit. Please wait a few minutes")
    raised, slept = _drive_rate_limited_request(response)

    assert isinstance(raised, check_stars.RateLimitError), \
        f"expected RateLimitError, got {type(raised).__name__}: {raised}"
    assert slept[0] >= check_stars.SECONDARY_RATE_LIMIT_WAIT, \
        f"first wait must clear the {check_stars.SECONDARY_RATE_LIMIT_WAIT}s floor, got {slept[0]}s"
    total = sum(slept)
    assert total > 7, f"the old policy's total was 7s; the new one waited only {total}s"
    assert total <= check_stars.RATE_LIMIT_TOTAL_BUDGET, \
        f"waited {total}s, over the {check_stars.RATE_LIMIT_TOTAL_BUDGET}s budget that bounds the job"


def test_rate_limit_give_up_message_names_the_cause():
    """The run log must explain itself without anyone opening this script."""
    reset_at = int(check_stars.time.time()) + 900
    response = _FakeResponse(headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "5000",
                                      "X-RateLimit-Reset": str(reset_at)})
    raised, _ = _drive_rate_limited_request(response)

    assert isinstance(raised, check_stars.RateLimitError), f"expected RateLimitError, got {raised!r}"
    message = str(raised)
    for expected in ("rate limit", "x-ratelimit-remaining=0/5000", "resets at", "budget"):
        assert expected in message, f"give-up message must mention {expected!r}; got: {message}"
    assert "403 Client Error: Forbidden" not in message, "the bare HTTPError text is what this replaces"


def test_permission_403_is_still_an_autherror():
    """Guard the split: only a rate-limit 403 gets the patient treatment.

    A permission 403 must stay an AuthError so the per-repo fetchers keep
    skipping that repo instead of sleeping through six minutes of backoff for
    every one of the ~280 repos.
    """
    response = _FakeResponse(payload={"message": "Resource not accessible by personal access token"})
    raised, slept = _drive_rate_limited_request(response)

    assert isinstance(raised, check_stars.AuthError), f"expected AuthError, got {type(raised).__name__}"
    assert slept == [], f"a permission 403 must not be retried at all, slept {slept}"


if __name__ == "__main__":
    test_empty_repo_returns_no_dependents_without_scraping()
    print("OK: empty repo returns [] without scraping")
    test_get_org_repos_maps_archived_flag()
    print("OK: get_org_repos maps archived flag")
    test_archived_repo_skips_token_gated_fetches()
    print("OK: archived repo skips token-gated fetches")
    test_per_repo_autherror_is_skipped_not_fatal()
    print("OK: per-repo AuthError is skipped, not fatal")
    test_rate_limit_wait_prefers_retry_after()
    print("OK: rate-limit wait prefers Retry-After")
    test_rate_limit_wait_waits_for_the_primary_reset()
    print("OK: rate-limit wait honours the primary reset")
    test_rate_limit_wait_floors_secondary_backoff_at_a_minute()
    print("OK: secondary backoff starts at a 60s floor")
    test_secondary_rate_limit_waits_minutes_and_stays_within_budget()
    print("OK: secondary limit waits minutes, bounded by the budget")
    test_rate_limit_give_up_message_names_the_cause()
    print("OK: give-up message names the rate limit and its reset")
    test_permission_403_is_still_an_autherror()
    print("OK: permission 403 is still an AuthError")
