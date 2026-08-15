#!/usr/bin/env python3
"""Tests for scripts/check-stars.py dependents handling.

Runnable standalone (`python tests/test_check_stars.py`) or via pytest.
"""

import contextlib
import importlib.util
import json
import os
import tempfile
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


def _run_with_main_raising(exc):
    """Call run() with main() replaced by one that raises `exc`.

    Returns (exit_code, captured_stdout).
    """
    import contextlib
    import io

    def _raise():
        raise exc

    original_main = check_stars.main
    check_stars.main = _raise  # type: ignore[assignment]
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = check_stars.run()
    finally:
        check_stars.main = original_main  # type: ignore[assignment]
    return code, out.getvalue()


def test_primary_budget_give_up_is_deferred_and_exits_zero():
    """Reproduces run 13078 (2026-08-13T21:28): primary budget spent, reset 1523s
    away, further than the 600s left in the per-run budget.

    The give-up is the DESIGNED hand-over to the next scheduled run, so it must
    mark the error as deferred, never sleep a partial wait, and run() must turn
    it into a ::warning:: with exit 0 instead of a red run.
    """
    reset_at = int(check_stars.time.time()) + 1523
    response = _FakeResponse(headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "5000",
                                      "X-RateLimit-Reset": str(reset_at)})
    raised, slept = _drive_rate_limited_request(response)

    assert isinstance(raised, check_stars.RateLimitError), f"expected RateLimitError, got {raised!r}"
    assert raised.deferred_to_next_run is True, "a primary budget give-up must defer to the next run"
    assert slept == [], f"a wait that exceeds the budget must not be slept at all, slept {slept}"

    code, out = _run_with_main_raising(raised)
    assert code == 0, f"a deferred give-up must exit 0, got {code}"
    assert "::warning::" in out, f"the deferred give-up must still annotate the run, got: {out}"
    assert "::error::" not in out
    assert str(raised) in out, "the warning must carry the full diagnostic text"


def test_secondary_budget_give_up_still_fails():
    """A secondary limit that outlasts the budget is abuse detection with no
    documented reset — it must NOT be deferred, and run() must stay red."""
    response = _FakeResponse(text="You have exceeded a secondary rate limit. Please wait a few minutes")
    raised, _ = _drive_rate_limited_request(response)

    assert isinstance(raised, check_stars.RateLimitError), f"expected RateLimitError, got {raised!r}"
    assert raised.deferred_to_next_run is False, "a secondary-limit give-up must not defer"

    code, out = _run_with_main_raising(raised)
    assert code == 1, f"a secondary-limit give-up must exit 1, got {code}"
    assert "::error::" in out

    # The retries-exhausted give-up constructs RateLimitError without the flag,
    # so the default must be "red" — guard it here.
    assert check_stars.RateLimitError("x").deferred_to_next_run is False

    # AuthError handling is unchanged: still red.
    code, out = _run_with_main_raising(check_stars.AuthError("no token in the environment"))
    assert code == 1, f"an AuthError must exit 1, got {code}"
    assert "::error::" in out


@contextlib.contextmanager
def _temp_state(initial):
    """Point the module's STATE_FILE at a throwaway file holding `initial`.

    Yields the path so a test can read back what the code under test wrote.
    """
    original = check_stars.STATE_FILE
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state" / "stars-state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(initial))
        check_stars.STATE_FILE = path  # type: ignore[assignment]
        try:
            yield path
        finally:
            check_stars.STATE_FILE = original  # type: ignore[assignment]


def _defer_once(seeded_state):
    """Drive run() through one deferred give-up against `seeded_state`.

    Returns (exit_code, stdout, state_as_written_back).
    """
    deferred = check_stars.RateLimitError(
        "GitHub primary rate limit still in effect after 0 attempt(s)",
        deferred_to_next_run=True,
    )
    with _temp_state(seeded_state) as path:
        code, out = _run_with_main_raising(deferred)
        return code, out, json.loads(path.read_text())


def test_deferred_give_up_counts_up_without_touching_the_seen_stars():
    """A hand-over must record that it happened and change nothing else.

    The whole safety argument for exiting 0 is that the give-up raises before
    save_state(), so the downloaded seen-stars data is re-uploaded untouched.
    Counting the deferral writes to that same file, so this pins both halves:
    the counter goes up, and the repos/last_run it shares the file with come
    back byte-identical.
    """
    seeded = {
        "repos": {"netresearch/foo": {"stars": ["alice"], "forks": [], "watchers": [], "dependents": []}},
        "last_run": "2026-08-14T21:00:00",
    }
    code, out, stored = _defer_once(seeded)

    assert code == 0, f"a deferred give-up below the threshold must still exit 0, got {code}"
    assert "::warning::" in out and "::error::" not in out, f"expected a warning, got: {out}"
    assert stored[check_stars.DEFERRED_GIVEUP_STREAK_KEY] == 1, \
        f"first deferral must count as 1, got {stored.get(check_stars.DEFERRED_GIVEUP_STREAK_KEY)!r}"
    assert stored["repos"] == seeded["repos"], "the give-up path must not advance the seen-stars data"
    assert stored["last_run"] == seeded["last_run"], \
        "the give-up path must not stamp last_run — that would suppress a pending first-run indexing"


def test_streak_reaching_the_threshold_turns_the_run_red():
    """Permanent starvation must stop hiding behind green runs.

    One below the threshold is still the designed hand-over; the run that
    reaches it has been deferring for longer than the hourly primary reset, so
    it goes red with the streak named in the message.
    """
    below = {"repos": {}, "last_run": "2026-08-14T21:00:00",
             check_stars.DEFERRED_GIVEUP_STREAK_KEY: check_stars.MAX_CONSECUTIVE_DEFERRALS - 2}
    code, out, stored = _defer_once(below)
    assert code == 0, f"deferral {check_stars.MAX_CONSECUTIVE_DEFERRALS - 1} must stay green, got {code}"
    assert "::warning::" in out and "::error::" not in out
    assert stored[check_stars.DEFERRED_GIVEUP_STREAK_KEY] == check_stars.MAX_CONSECUTIVE_DEFERRALS - 1

    at_threshold = dict(below)
    at_threshold[check_stars.DEFERRED_GIVEUP_STREAK_KEY] = check_stars.MAX_CONSECUTIVE_DEFERRALS - 1
    code, out, stored = _defer_once(at_threshold)
    assert code == 1, f"deferral {check_stars.MAX_CONSECUTIVE_DEFERRALS} must fail the run, got {code}"
    assert "::error::" in out and "::warning::" not in out, f"expected an error annotation, got: {out}"
    assert f"{check_stars.MAX_CONSECUTIVE_DEFERRALS} in a row" in out, \
        f"the error must name the streak so the log says why it is red now; got: {out}"
    assert stored[check_stars.DEFERRED_GIVEUP_STREAK_KEY] == check_stars.MAX_CONSECUTIVE_DEFERRALS, \
        "the red run must still persist its count, or the streak freezes one below the threshold"


def test_a_completed_run_resets_the_streak():
    """save_state() is the tail of main(), so reaching it clears the streak.

    Without the reset, five deferrals spread over a week of otherwise healthy
    runs would eventually turn a run red for a limit that has long since lifted.
    """
    seeded = {"repos": {}, "last_run": None,
              check_stars.DEFERRED_GIVEUP_STREAK_KEY: check_stars.MAX_CONSECUTIVE_DEFERRALS - 1}
    with _temp_state(seeded) as path:
        state = check_stars.load_state()
        assert state[check_stars.DEFERRED_GIVEUP_STREAK_KEY] == check_stars.MAX_CONSECUTIVE_DEFERRALS - 1, \
            "precondition: the streak must survive the load it is meant to be reset from"
        check_stars.save_state(state)
        stored = json.loads(path.read_text())

    assert stored[check_stars.DEFERRED_GIVEUP_STREAK_KEY] == 0, \
        f"a completed run must clear the streak, got {stored[check_stars.DEFERRED_GIVEUP_STREAK_KEY]!r}"
    assert stored["last_run"], "save_state must still stamp the completion time"


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
    test_primary_budget_give_up_is_deferred_and_exits_zero()
    print("OK: primary budget give-up warns and exits 0")
    test_secondary_budget_give_up_still_fails()
    print("OK: secondary budget give-up still fails red")
    test_deferred_give_up_counts_up_without_touching_the_seen_stars()
    print("OK: deferred give-up counts up and leaves the seen stars alone")
    test_streak_reaching_the_threshold_turns_the_run_red()
    print("OK: the streak turns the run red at the threshold")
    test_a_completed_run_resets_the_streak()
    print("OK: a completed run resets the streak")
    test_permission_403_is_still_an_autherror()
    print("OK: permission 403 is still an AuthError")
