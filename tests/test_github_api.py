#!/usr/bin/env python3
"""Tests for scripts/github_api.py — the shared GitHub client and its rate-limit policy.

These moved here wholesale when the client was extracted out of check-stars.py
so a second scheduled script could share it. They test the policy, not the
caller, and the policy is now one implementation rather than two.

Runnable standalone (`python tests/test_github_api.py`) or via pytest.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import github_api  # the sys.path line above is what makes this importable


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
            raise github_api.requests.exceptions.HTTPError(f"{self.status_code} Client Error", response=self)


def _drive_rate_limited_request(response):
    """Run github_request against a server that always returns `response`.

    Returns (raised_exception_or_None, list_of_sleep_durations). requests.get and
    time.sleep are swapped out so the test measures the policy rather than living
    through it, and both are restored plus the budget reset afterwards.
    """
    slept = []
    original_get, original_sleep = github_api.requests.get, github_api.time.sleep
    github_api.requests.get = lambda *a, **k: response  # type: ignore[assignment]
    github_api.time.sleep = slept.append  # type: ignore[assignment]
    github_api.reset_rate_limit_budget()
    raised = None
    try:
        github_api.github_request("https://api.github.com/orgs/netresearch/repos?type=all&per_page=100")
    except Exception as e:  # noqa: BLE001 - the test asserts on which type came out
        raised = e
    finally:
        github_api.requests.get = original_get  # type: ignore[assignment]
        github_api.time.sleep = original_sleep  # type: ignore[assignment]
        github_api.reset_rate_limit_budget()
    return raised, slept


def test_rate_limit_wait_prefers_retry_after():
    """Retry-After is GitHub telling us directly, so it outranks every guess."""
    response = _FakeResponse(headers={"Retry-After": "45", "X-RateLimit-Remaining": "0",
                                      "X-RateLimit-Reset": str(int(github_api.time.time()) + 3600)})
    wait, reason = github_api.rate_limit_wait(response, attempt=0)
    assert 45 <= wait <= 47, f"expected the 45s the server asked for (plus slack), got {wait}"
    assert "Retry-After" in reason


def test_rate_limit_wait_waits_for_the_primary_reset():
    """A spent primary budget cannot be beaten by any shorter wait, so wait for the reset."""
    response = _FakeResponse(headers={"X-RateLimit-Remaining": "0",
                                      "X-RateLimit-Reset": str(int(github_api.time.time()) + 120)})
    wait, reason = github_api.rate_limit_wait(response, attempt=0)
    assert 118 <= wait <= 123, f"expected ~120s until the reset, got {wait}"
    assert "resetting at" in reason


def test_rate_limit_wait_floors_secondary_backoff_at_a_minute():
    """A secondary limit sends no usable header; x-ratelimit-reset describes the
    PRIMARY window and would over-wait, so back off from the documented floor."""
    response = _FakeResponse(text="You have exceeded a secondary rate limit",
                             headers={"X-RateLimit-Remaining": "4213",
                                      "X-RateLimit-Reset": str(int(github_api.time.time()) + 3600)})
    waits = [github_api.rate_limit_wait(response, attempt=n)[0] for n in range(3)]
    assert waits == [60, 120, 240], f"expected a 60s floor doubling per attempt, got {waits}"


def test_secondary_rate_limit_waits_minutes_and_stays_within_budget():
    """Reproduces the 2026-08-09T21:40 failure: an unrelenting secondary 403.

    The old policy slept 1+2+4 = 7 seconds and then died with a bare HTTPError.
    The new one must wait in minutes, stop before the per-run budget is spent,
    and say what happened.
    """
    response = _FakeResponse(text="You have exceeded a secondary rate limit. Please wait a few minutes")
    raised, slept = _drive_rate_limited_request(response)

    assert isinstance(raised, github_api.RateLimitError), \
        f"expected RateLimitError, got {type(raised).__name__}: {raised}"
    assert slept[0] >= github_api.SECONDARY_RATE_LIMIT_WAIT, \
        f"first wait must clear the {github_api.SECONDARY_RATE_LIMIT_WAIT}s floor, got {slept[0]}s"
    total = sum(slept)
    assert total > 7, f"the old policy's total was 7s; the new one waited only {total}s"
    assert total <= github_api.RATE_LIMIT_TOTAL_BUDGET, \
        f"waited {total}s, over the {github_api.RATE_LIMIT_TOTAL_BUDGET}s budget that bounds the job"


def test_rate_limit_give_up_message_names_the_cause():
    """The run log must explain itself without anyone opening this script."""
    reset_at = int(github_api.time.time()) + 900
    response = _FakeResponse(headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "5000",
                                      "X-RateLimit-Reset": str(reset_at)})
    raised, _ = _drive_rate_limited_request(response)

    assert isinstance(raised, github_api.RateLimitError), f"expected RateLimitError, got {raised!r}"
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

    assert isinstance(raised, github_api.AuthError), f"expected AuthError, got {type(raised).__name__}"
    assert slept == [], f"a permission 403 must not be retried at all, slept {slept}"


def test_dict_response_is_returned_without_pagination():
    """An envelope response must come back after ONE request, not be paginated.

    check_scheduled_failures reads `{"total_count": N, "workflow_runs": [...]}`
    and detects truncation from total_count. If the client ever started
    following `next` for dict bodies it would walk the 13 000 scheduled runs a
    15-minute cadence accumulates, and the truncation handling would be dead
    code hiding a runaway job.
    """
    calls = {"n": 0}
    payload = {"total_count": 153, "workflow_runs": [{"id": 1}]}

    def _count_and_return(*args, **kwargs):
        calls["n"] += 1
        response = _FakeResponse(status_code=200, payload=payload)
        response.links = {"next": {"url": "https://api.github.com/next-page"}}
        return response

    original_get = github_api.requests.get
    github_api.requests.get = _count_and_return  # type: ignore[assignment]
    try:
        result = github_api.github_request("https://api.github.com/repos/netresearch/maint/actions/runs")
    finally:
        github_api.requests.get = original_get  # type: ignore[assignment]

    assert result == payload, f"the envelope must be returned untouched, got {result!r}"
    assert calls["n"] == 1, f"a dict body must cost exactly one request, made {calls['n']}"


def test_post_to_matrix_sets_a_timeout():
    """A wedged webhook must not hold the runner open until the Actions timeout."""
    seen = {}

    # `json` shadows the builtin deliberately: it mirrors requests.post's signature.
    def _capture(url, json=None, timeout=None):
        seen.update(url=url, json=json, timeout=timeout)
        return _FakeResponse(status_code=200, payload={})

    original_post = github_api.requests.post
    github_api.requests.post = _capture  # type: ignore[assignment]
    try:
        github_api.post_to_matrix("https://example.invalid/webhook", "hello")
    finally:
        github_api.requests.post = original_post  # type: ignore[assignment]

    assert seen["json"] == {"text": "hello"}, f"Hookshot expects a text payload, sent {seen['json']!r}"
    assert seen["timeout"], "post_to_matrix must pass a timeout"


if __name__ == "__main__":
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
    test_dict_response_is_returned_without_pagination()
    print("OK: an envelope response costs exactly one request")
    test_post_to_matrix_sets_a_timeout()
    print("OK: post_to_matrix sets a timeout")
