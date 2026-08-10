#!/usr/bin/env python3
"""Tests for scripts/check_scheduled_failures.py.

The rules worth guarding are the ones that decide whether a human hears
anything: a green -> red transition speaks once, a still-red workflow speaks
once a week, a recovery speaks, and everything else stays silent. Nothing here
touches the network or the Matrix webhook.

Runnable standalone (`python tests/test_check_scheduled_failures.py`) or via pytest.
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import check_scheduled_failures as csf  # needs the sys.path line above

NOW = datetime(2026, 8, 10, 7, 0, tzinfo=timezone.utc)
REPO = {"full_name": "netresearch/netresearch.github.io", "name": "netresearch.github.io",
        "url": "https://github.com/netresearch/netresearch.github.io"}


def _run(conclusion, day, workflow_id=243601301, name="Build & Deploy"):
    """A workflow_runs entry, dated `day` of August 2026."""
    return {
        "workflow_id": workflow_id,
        "name": name,
        "conclusion": conclusion,
        "run_started_at": f"2026-08-{day:02d}T06:20:00Z",
        "created_at": f"2026-08-{day:02d}T06:20:00Z",
        "html_url": f"https://github.com/netresearch/netresearch.github.io/actions/runs/{day}",
    }


def _group(runs, exhaustive=True, name="Build & Deploy"):
    return {"name": name, "runs": runs, "exhaustive": exhaustive}


def test_streak_counts_consecutive_failures_and_dates_the_first():
    """The message promises a count and a date, so both must be read from the runs."""
    runs = [_run("failure", d) for d in (9, 8, 7)] + [_run("success", 6)]
    summary = csf.summarise_workflow(_group(runs))

    assert summary["failing"] is True
    assert summary["consecutive_failures"] == 3, f"expected 3, got {summary['consecutive_failures']}"
    assert summary["failing_since"].startswith("2026-08-07"), \
        f"the streak began with the OLDEST failure, got {summary['failing_since']}"
    assert summary["at_least"] is False, "the window reached a success, so the count is exact"


def test_cancelled_runs_neither_break_nor_extend_a_streak():
    """A cancelled run is GitHub's plumbing, not a verdict on the software.

    Counting it as red would page people about a concurrency group; counting it
    as green would silently reset a fortnight of real failures to zero.
    """
    runs = [_run("failure", 9), _run("cancelled", 8), _run("failure", 7), _run("success", 6)]
    summary = csf.summarise_workflow(_group(runs))

    assert summary["consecutive_failures"] == 2, \
        f"the cancelled run must be stepped over, not counted; got {summary['consecutive_failures']}"
    assert summary["failing_since"].startswith("2026-08-07"), "the streak still reaches back past the cancellation"


def test_a_window_with_no_verdict_yields_nothing():
    """All-cancelled is not evidence of health or breakage, so say nothing."""
    assert csf.summarise_workflow(_group([_run("cancelled", 9), _run("skipped", 8)])) is None


def test_truncated_window_reports_a_lower_bound():
    """Never state a floor as if it were the total."""
    runs = [_run("failure", d) for d in (9, 8, 7)]
    exact = csf.summarise_workflow(_group(runs, exhaustive=True))
    bounded = csf.summarise_workflow(_group(runs, exhaustive=False))

    assert exact["at_least"] is False, "a complete history gives an exact count"
    assert bounded["at_least"] is True, "no success found and more runs exist ⇒ at least N"
    assert "at least 3" in csf.format_failure(REPO, bounded, NOW, reminder=False)


def test_new_failure_speaks_once_then_stays_quiet():
    """Requirement 2: notify on the TRANSITION, not on every poll."""
    summary = csf.summarise_workflow(_group([_run("failure", 9), _run("success", 8)]))

    event, message = csf.classify(REPO, summary, previous=None, now=NOW)
    assert event == "new-failure", f"green -> red must announce, got {event}"

    carried = csf.state_entry(REPO, summary, previous=None, notified=True, now=NOW)
    next_day = NOW + timedelta(days=1)
    event, message = csf.classify(REPO, summary, previous=carried, now=next_day)
    assert event == "quiet" and message is None, \
        f"a still-red workflow must not re-announce the next day, got {event}"


def test_still_red_gets_one_reminder_a_week():
    """Requirement 3: a long-running failure cannot fade out silently."""
    summary = csf.summarise_workflow(_group([_run("failure", d) for d in (9, 8)] + [_run("success", 7)]))
    carried = csf.state_entry(REPO, summary, previous=None, notified=True, now=NOW)

    six_days = NOW + timedelta(days=6)
    assert csf.classify(REPO, summary, carried, six_days)[0] == "quiet", "day 6 is not a week yet"

    seven_days = NOW + timedelta(days=7)
    event, message = csf.classify(REPO, summary, carried, seven_days)
    assert event == "reminder", f"day 7 must nag, got {event}"
    assert "STILL failing" in message, f"a reminder must read as a reminder: {message}"


def test_quiet_cycles_do_not_reset_the_reminder_clock():
    """The bug that would recreate the 13 days of silence.

    If `last_notified` moved on every poll, the weekly reminder would never come
    due — each quiet cycle would push it another week out. It must only move
    when something was actually said.
    """
    summary = csf.summarise_workflow(_group([_run("failure", 9), _run("success", 8)]))
    entry = csf.state_entry(REPO, summary, previous=None, notified=True, now=NOW)
    first_notified = entry["last_notified"]

    for day in range(1, 7):
        entry = csf.state_entry(REPO, summary, previous=entry, notified=False, now=NOW + timedelta(days=day))
    assert entry["last_notified"] == first_notified, \
        "a quiet cycle must carry the old timestamp forward, not stamp a new one"
    assert csf.classify(REPO, summary, entry, NOW + timedelta(days=7))[0] == "reminder", \
        "after six quiet days the seventh must still come due"


def test_recovery_is_announced_once():
    """Requirement 4: a channel that only ever reports bad news gets muted."""
    failing = csf.summarise_workflow(_group([_run("failure", 8), _run("success", 7)]))
    was_failing = csf.state_entry(REPO, failing, previous=None, notified=True, now=NOW)

    recovered = csf.summarise_workflow(_group([_run("success", 9), _run("failure", 8)]))
    event, message = csf.classify(REPO, recovered, was_failing, NOW)
    assert event == "recovery", f"red -> green must announce, got {event}"
    assert "recovered after 1 failure" in message, f"the recovery should say what it recovered from: {message}"

    now_green = csf.state_entry(REPO, recovered, previous=was_failing, notified=True, now=NOW)
    assert csf.classify(REPO, recovered, now_green, NOW + timedelta(days=30))[0] == "quiet", \
        "a healthy workflow must never speak again, however long it stays healthy"


def test_message_carries_everything_needed_to_act():
    """Requirement 5: repo, workflow, count, since when, and a link to the run."""
    summary = csf.summarise_workflow(_group([_run("failure", d) for d in (9, 8, 7)] + [_run("success", 6)]))
    message = csf.format_failure(REPO, summary, NOW, reminder=False)

    for expected in ("netresearch.github.io", "Build & Deploy", "3 consecutive failures",
                     "since 2026-08-07", "(3 days)", "/actions/runs/9"):
        assert expected in message, f"the message must carry {expected!r}; got: {message}"


def test_repo_without_scheduled_runs_is_skipped_silently():
    """Requirement 1: 'no scheduled runs' is not a status worth reporting."""
    original = csf.github_request
    csf.github_request = lambda url: {"total_count": 0, "workflow_runs": []}  # type: ignore[assignment]
    try:
        assert csf.collect_workflow_runs("netresearch/quiet-repo") == {}
    finally:
        csf.github_request = original  # type: ignore[assignment]


def test_a_crowded_page_does_not_hide_a_second_workflow():
    """The maint repo's own shape: 98 of the newest 100 scheduled runs are one
    workflow. A nightly sibling can fall off that page entirely, and its failures
    would then be invisible to the very notifier meant to catch them.
    """
    busy = [_run("success", 9, workflow_id=1, name="Star Notifications") for _ in range(100)]
    calls = []

    def _fake(url):
        calls.append(url)
        if "/actions/workflows/2/runs" in url:
            return {"total_count": 2, "workflow_runs": [
                _run("failure", 9, workflow_id=2, name="Impact Dashboard"),
                _run("failure", 8, workflow_id=2, name="Impact Dashboard"),
            ]}
        if url.endswith("/actions/workflows?per_page=100"):
            return {"total_count": 2, "workflows": [
                {"id": 1, "name": "Star Notifications", "state": "active"},
                {"id": 2, "name": "Impact Dashboard", "state": "active"},
            ]}
        return {"total_count": 12996, "workflow_runs": busy}

    original = csf.github_request
    csf.github_request = _fake  # type: ignore[assignment]
    try:
        grouped = csf.collect_workflow_runs("netresearch/maint")
    finally:
        csf.github_request = original  # type: ignore[assignment]

    assert 2 in grouped, f"the crowded-out workflow must be fetched separately; got keys {sorted(grouped)}"
    assert grouped[2]["name"] == "Impact Dashboard"
    assert csf.summarise_workflow(grouped[2])["consecutive_failures"] == 2
    assert any("/actions/workflows?per_page=100" in u for u in calls), \
        "the workflow list is only worth fetching when the page was truncated"


def test_an_untruncated_page_costs_exactly_one_request():
    """The gapfill must not fire for the ~190 repos that fit on one page."""
    calls = []

    def _fake(url):
        calls.append(url)
        return {"total_count": 2, "workflow_runs": [_run("success", 9), _run("success", 8)]}

    original = csf.github_request
    csf.github_request = _fake  # type: ignore[assignment]
    try:
        csf.collect_workflow_runs("netresearch/small-repo")
    finally:
        csf.github_request = original  # type: ignore[assignment]

    assert len(calls) == 1, f"one page covered the history, so one request; made {len(calls)}: {calls}"


def test_disabled_workflows_are_not_resurrected():
    """A disabled workflow's last red run is history, not an incident."""
    def _fake(url):
        if url.endswith("/actions/workflows?per_page=100"):
            return {"total_count": 1, "workflows": [
                {"id": 7, "name": "Retired Nightly", "state": "disabled_manually"},
            ]}
        return {"total_count": 500, "workflow_runs": [_run("success", 9, workflow_id=1)]}

    original = csf.github_request
    csf.github_request = _fake  # type: ignore[assignment]
    try:
        grouped = csf.collect_workflow_runs("netresearch/some-repo")
    finally:
        csf.github_request = original  # type: ignore[assignment]

    assert 7 not in grouped, "a disabled workflow cannot run, so it cannot be failing"


def test_baseline_is_one_line_not_a_flood():
    """A lost or first-run state must neither re-announce everything as new nor
    go silent about workflows that are already red."""
    summary = csf.summarise_workflow(_group([_run("failure", d) for d in (9, 8)] + [_run("success", 7)]))
    message = csf.format_baseline([(REPO, summary), (REPO, summary)], NOW)

    assert message.count("netresearch.github.io") == 2, "every failing workflow is named"
    assert "no previous state" in message, "the message must say why it is not a transition"
    assert "🔴" not in message, "a baseline is not a green -> red transition and must not read as one"


def test_dry_run_sends_nothing():
    """The guard that let this be developed without posting to the real room."""
    def _explode(*args, **kwargs):
        raise AssertionError("--dry-run posted to Matrix")

    original = csf.post_to_matrix
    csf.post_to_matrix = _explode  # type: ignore[assignment]
    try:
        assert csf.send(["🔴 something broke"], dry_run=True) == 0
    finally:
        csf.post_to_matrix = original  # type: ignore[assignment]


def test_state_round_trips_through_the_artifact_file():
    """What is written is what the next run reads: the transition rules depend on it.

    Only the four fields the rules actually read have to survive; the rest of an
    entry is rewritten from live API data on every run.
    """
    summary = csf.summarise_workflow(_group([_run("failure", 9), _run("success", 8)]))
    entry = csf.state_entry(REPO, summary, previous=None, notified=True, now=NOW)

    original_path = csf.STATE_FILE
    with tempfile.TemporaryDirectory() as tmp:
        csf.STATE_FILE = Path(tmp) / "state" / "scheduled-failures-state.json"
        try:
            csf.save_state({"netresearch/x::1": entry})
            reloaded = csf.load_state()
        finally:
            csf.STATE_FILE = original_path

    restored = reloaded["workflows"]["netresearch/x::1"]
    for field in ("failing", "consecutive_failures", "failing_since", "last_notified"):
        assert restored[field] == entry[field], \
            f"{field} must survive the round trip: wrote {entry[field]!r}, read {restored[field]!r}"
    assert reloaded["last_run"], "last_run marks the state as a real baseline, not a first run"

    # The reminder clock is the round trip's whole point — prove it still ticks
    # from the reloaded value rather than only from the in-memory one.
    assert csf.classify(REPO, summary, restored, NOW + timedelta(days=7))[0] == "reminder"
    assert csf.classify(REPO, summary, restored, NOW + timedelta(days=6))[0] == "quiet"


def test_a_malformed_artifact_cannot_derail_the_run():
    """The state file is whatever the artifact held, so its shape is not a given.

    Every entry here is the wrong type in a way the transition rules would
    otherwise hit as an AttributeError or a TypeError, hundreds of API calls
    into a run.
    """
    hostile = {
        "last_run": ["not", "a", "timestamp"],
        "workflows": {
            "netresearch/a::1": {"failing": "yes", "consecutive_failures": "many",
                                 "failing_since": None, "last_notified": "not-a-date"},
            "netresearch/b::2": "an entry should be an object",
            "netresearch/c::3": {"consecutive_failures": -5},
        },
    }
    original_path = csf.STATE_FILE
    with tempfile.TemporaryDirectory() as tmp:
        csf.STATE_FILE = Path(tmp) / "scheduled-failures-state.json"
        csf.STATE_FILE.write_text(json.dumps(hostile))
        try:
            state = csf.load_state()
        finally:
            csf.STATE_FILE = original_path

    assert state["last_run"] is None, "a non-timestamp last_run must not pass for a real baseline"
    workflows = state["workflows"]
    assert set(workflows) == {"netresearch/a::1", "netresearch/c::3"}, \
        f"non-dict entries and non-string keys must be dropped, kept {sorted(map(str, workflows))}"
    assert workflows["netresearch/a::1"]["failing"] is True, "'yes' is truthy, and must arrive as a bool"
    assert workflows["netresearch/a::1"]["consecutive_failures"] == 0, "a non-int count must not reach a format string"
    assert workflows["netresearch/a::1"]["last_notified"] is None, "an unparseable date must not stall the reminder"
    assert workflows["netresearch/c::3"]["consecutive_failures"] == 0, "a negative count is not a count"

    # A non-string key cannot come through JSON (it stringifies them), so the
    # guard against one is only reachable in memory. Exercise it there.
    assert csf.clean_workflows({7: {"failing": True}}) == {}, "a non-string key must be dropped"

    # The rules must now run over it without raising.
    summary = csf.summarise_workflow(_group([_run("failure", 9), _run("success", 8)]))
    for previous in workflows.values():
        csf.classify(REPO, summary, previous, NOW)


def test_a_state_file_holding_a_list_is_not_an_object():
    """json.load happily returns a list; .get() on it would be an AttributeError."""
    original_path = csf.STATE_FILE
    with tempfile.TemporaryDirectory() as tmp:
        csf.STATE_FILE = Path(tmp) / "scheduled-failures-state.json"
        csf.STATE_FILE.write_text("[1, 2, 3]")
        try:
            state = csf.load_state()
        finally:
            csf.STATE_FILE = original_path

    assert state == {"workflows": {}, "last_run": None}, f"expected an empty baseline, got {state}"


def test_unreadable_state_starts_a_baseline_instead_of_crashing():
    """A corrupt artifact costs one summary line; a hard failure costs silence."""
    original_path = csf.STATE_FILE
    with tempfile.TemporaryDirectory() as tmp:
        csf.STATE_FILE = Path(tmp) / "scheduled-failures-state.json"
        csf.STATE_FILE.write_text("{not json")
        try:
            state = csf.load_state()
        finally:
            csf.STATE_FILE = original_path

    assert state == {"workflows": {}, "last_run": None}, f"expected an empty baseline, got {state}"


def test_state_file_is_json_serialisable():
    """save_state uses json.dumps; a datetime leaking into an entry would only
    surface at the very end of a 200-repo run, after all the work was done."""
    summary = csf.summarise_workflow(_group([_run("failure", 9), _run("success", 8)]))
    entry = csf.state_entry(REPO, summary, previous=None, notified=True, now=NOW)
    json.dumps({"workflows": {"k": entry}})


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"OK: {_name[5:].replace('_', ' ')}")
