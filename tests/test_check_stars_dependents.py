"""Scan-level request and notification parity with offline transport/sender stubs."""

import json

import pytest
import requests

from test_check_stars import check_stars


@pytest.fixture
def scan(monkeypatch, tmp_path):
    def run(names, known=(), *, first_run=False, tracking=True, metadata_error=None,
            valid_page=True):
        saved_repo = {"stars": [], "forks": [], "watchers": []}
        if tracking:
            saved_repo["dependents"] = list(known)
        state_file = tmp_path / "state.json"
        initial = {"repos": {"netresearch/example": saved_repo},
                   "last_run": None if first_run else "2026-10-27T00:00:00"}
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
        monkeypatch.setattr(check_stars, "notify_matrix",
                            lambda text, html_body: messages.append((text, html_body)))
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
        check_stars.main()
        return messages, lookups, json.loads(state_file.read_text())

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


def test_digest_cap_and_reconciled_state_are_unchanged(scan):
    names = [f"consumer/new-{number}" for number in range(check_stars.MAX_DIGEST_LINES + 3)]
    messages, lookups, state = scan(names)
    assert len(messages) == 1, "all events of a run must go out as one digest"
    text, _ = messages[0]
    lines = text.splitlines()
    assert f"📦 {len(names)} dependents" in lines[0], "the summary counts every event, not only the shown ones"
    assert len(lines) == 1 + check_stars.MAX_DIGEST_LINES + 1
    assert lines[-1] == f"+3 more, see the [run log]({check_stars.FEED_URL})"
    assert set(state["repos"]["netresearch/example"]["dependents"]) == set(names)
    assert len(lookups) == len(names)
