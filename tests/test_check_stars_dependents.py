"""Scan-level request and notification parity with offline transport/sender stubs."""

import json

import pytest
import requests

from test_check_stars import check_stars


@pytest.fixture
def scan(monkeypatch, tmp_path):
    def run(names, known=(), *, first_run=False, tracking=True, metadata_error=None,
            valid_page=True, send_error=None):
        saved_repo = {"stars": [], "forks": [], "watchers": []}
        if tracking:
            saved_repo["dependents"] = list(known)
        state_file = tmp_path / "state.json"
        initial = {"repos": {"netresearch/example": saved_repo},
                   "last_run": None if first_run else "2026-10-27T00:00:00"}
        initial[check_stars.DEFERRED_GIVEUP_STREAK_KEY] = 1
        state_file.write_text(json.dumps(initial))
        messages, lookups = [], []
        repo = {"name": "example", "full_name": "netresearch/example",
                "url": "https://github.com/netresearch/example",
                "stargazers_count": 0, "is_empty": False, "archived": False}
        monkeypatch.setattr(check_stars, "STATE_FILE", state_file)
        monkeypatch.setattr(check_stars, "GITHUB_TOKEN", "synthetic-test-token")
        monkeypatch.setattr(check_stars, "get_org_repos", lambda: [repo])
        for fetcher in ("get_stargazers", "get_forks", "get_watchers"):
            monkeypatch.setattr(check_stars, fetcher, lambda *_: [])
        def notify(text, html_body):
            messages.append((text, html_body))
            if send_error is not None:
                raise send_error

        monkeypatch.setattr(check_stars, "notify_matrix", notify)
        monkeypatch.setattr(check_stars, "_inaccessible", {})

        def metadata(url, **kwargs):
            lookups.append(url)
            if metadata_error is not None:
                raise metadata_error
            return {"stargazers_count": 7, "forks_count": 2}

        monkeypatch.setattr(check_stars, "github_request", metadata)
        rows = ''.join('<div class="Box-row"><a data-hovercard-type="repository" '
                       f'href="/{name}">{name}</a></div>' for name in names)
        document = f'<div id="dependents">{rows}</div>' if valid_page else '<html></html>'
        response = requests.Response()
        response.status_code = 200
        response._content = document.encode()
        monkeypatch.setattr(check_stars._html_session, "get", lambda *_a, **_k: response)
        try:
            check_stars.main()
        finally:
            run.state = json.loads(state_file.read_text())
        return messages, lookups, run.state

    return run


def test_unchanged_dependents_need_no_metadata_requests(scan):
    messages, lookups, state = scan(["consumer/old"], ["consumer/old"])
    assert lookups == []
    assert messages == []
    assert state["repos"]["netresearch/example"]["dependents"] == ["consumer/old"]


def test_only_new_dependent_is_enriched_with_the_same_message(scan):
    messages, lookups, state = scan(["consumer/old", "consumer/new"], ["consumer/old"])
    assert lookups == ["https://api.github.com/repos/consumer/new"]
    assert len(messages) == 1
    text, html_body = messages[0]
    assert text.splitlines()[1] == (
        "📦 [example](https://github.com/netresearch/example) new dependent: "
        "[consumer/new](https://github.com/consumer/new) (7 ⭐, 2 🍴)")
    assert "📦 1 dependent" in text.splitlines()[0]
    assert '<a href="https://github.com/consumer/new">consumer/new</a> (7 ⭐, 2 🍴)' in html_body
    assert set(state["repos"]["netresearch/example"]["dependents"]) == {
        "consumer/old", "consumer/new"}


@pytest.mark.parametrize("first_run,tracking", [(True, True), (False, False)])
def test_silent_baselines_do_not_fetch_unused_metadata(scan, first_run, tracking):
    messages, lookups, state = scan(["consumer/existing"], first_run=first_run,
                                    tracking=tracking)
    assert lookups == []
    assert messages == []
    assert state["repos"]["netresearch/example"]["dependents"] == ["consumer/existing"]


def test_metadata_http_failure_keeps_the_existing_zero_count_message(scan):
    messages, lookups, state = scan(["consumer/new"], metadata_error=requests.HTTPError("offline"))
    assert lookups == ["https://api.github.com/repos/consumer/new"]
    assert "(0 ⭐, 0 🍴)" in messages[0][0]
    assert state["repos"]["netresearch/example"]["dependents"] == ["consumer/new"]


def test_metadata_authorization_failure_is_still_fatal_for_a_new_dependent(scan):
    with pytest.raises(check_stars.AuthError, match="offline permissions"):
        scan(["consumer/new"], metadata_error=check_stars.AuthError("offline permissions"))


@pytest.mark.parametrize("valid_page", [True, False])
def test_empty_or_unrecognized_page_preserves_known_dependents(scan, valid_page):
    messages, lookups, state = scan([], ["consumer/old"], valid_page=valid_page)
    assert lookups == []
    assert messages == []
    assert state["repos"]["netresearch/example"]["dependents"] == ["consumer/old"]


def test_removal_is_saved_without_enriching_the_remaining_dependent(scan):
    messages, lookups, state = scan(["consumer/keep"], ["consumer/keep", "consumer/removed"])
    assert lookups == []
    assert messages == []
    assert state["repos"]["netresearch/example"]["dependents"] == ["consumer/keep"]


RUN_ENV = ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")


def test_digest_cap_and_reconciled_state_are_unchanged(scan, monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "netresearch/maint")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    names = [f"consumer/new-{number}" for number in range(check_stars.MAX_DIGEST_LINES + 3)]
    messages, lookups, state = scan(names)
    assert len(messages) == 1, "all events of a run must go out as one digest"
    text, _ = messages[0]
    lines = text.splitlines()
    assert lines[0].startswith("📊 netresearch since 2026-10-27 00:00 UTC: "), \
        "the header names the window since the previous completed run"
    assert f"📦 {len(names)} dependents" in lines[0], "the summary counts every event, not only the shown ones"
    assert len(lines) == 1 + check_stars.MAX_DIGEST_LINES + 1
    assert lines[-1] == "+3 more, see the [run log](https://github.com/netresearch/maint/actions/runs/123)"
    assert set(state["repos"]["netresearch/example"]["dependents"]) == set(names)
    assert len(lookups) == len(names)


def test_run_log_falls_back_to_the_workflow_runs_outside_actions(monkeypatch):
    for name in RUN_ENV:
        monkeypatch.delenv(name, raising=False)
    assert check_stars.run_log_url() == check_stars.FEED_URL


def test_undelivered_digest_keeps_the_events_for_the_next_run(scan):
    webhook_down = requests.ConnectionError("webhook down")
    with pytest.raises(check_stars.DigestNotSentError, match="1 event line"):
        scan(["consumer/old", "consumer/new"], ["consumer/old"], send_error=webhook_down)
    state = scan.state
    assert state["repos"]["netresearch/example"]["dependents"] == ["consumer/old"], \
        "the undelivered dependent must still be new to the next run"
    assert state["last_run"] == "2026-10-27T00:00:00", "the window must keep its start"
    assert state[check_stars.DEFERRED_GIVEUP_STREAK_KEY] == 0, "the scan itself completed"


def test_delivered_digest_saves_the_scan(scan):
    _, _, state = scan(["consumer/old", "consumer/new"], ["consumer/old"])
    assert state["last_run"] != "2026-10-27T00:00:00"
    assert state[check_stars.DEFERRED_GIVEUP_STREAK_KEY] == 0
