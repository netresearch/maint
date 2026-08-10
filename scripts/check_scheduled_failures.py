#!/usr/bin/env python3
"""Report failing SCHEDULED workflow runs across the netresearch org to Matrix.

Why this exists: netresearch.github.io's nightly "Build & Deploy" was red for 13
days and 14 consecutive runs before anyone noticed. GitHub's own notification
for a failing scheduled run goes to whoever last touched the workflow file,
which for a shared reusable workflow is frequently nobody who watches that repo,
so the signal reached no one. Scheduled runs are the org's early-warning system
for dependency drift — they install fresh where PR runs sit on warm caches — and
an early-warning system nobody reads is not one.

The notifier reports transitions, not states: green -> red once, one reminder a
week while it stays red, and a short note on red -> green. A repo with no
scheduled runs at all is simply skipped; it is not news.

Usage:
    python scripts/check_scheduled_failures.py [--dry-run]

--dry-run prints the messages it would post and leaves both Matrix and the state
file untouched. Use it for any manual verification: posting to the real room is
the one thing that cannot be taken back.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# Shared GitHub client: see scripts/github_api.py for the rate-limit policy this
# inherits rather than re-implements. Imported by bare name because the script
# runs as `python scripts/check_scheduled_failures.py`, putting scripts/ on sys.path.
from github_api import (
    GITHUB_API,
    GITHUB_TOKEN,
    AuthError,
    RateLimitError,
    github_request,
    post_to_matrix,
)

ORG_NAME = os.environ.get("ORG_NAME", "netresearch")
MATRIX_WEBHOOK_URL = os.environ.get("MATRIX_WEBHOOK_URL", "")
# Fixed path, not an env override: this is a write target, and building a path
# the process then writes to out of environment input is path injection (Sonar
# S2083) for the sake of test convenience the tests do not need — they rebind
# this module attribute directly. Matches check-stars.py's STATE_FILE.
STATE_FILE = Path("state/scheduled-failures-state.json")
HELP_URL = "https://github.com/netresearch/maint"
FEED_URL = "https://github.com/netresearch/maint/actions/workflows/scheduled-failure-notifications.yml"

# One nag a week while a workflow stays red. Short enough that a fortnight of
# silence is impossible, long enough that the channel does not train people to
# mute it — which is the failure mode that produced the 13 days in the first
# place, just via a different mechanism.
REMINDER_INTERVAL_DAYS = 7

# How many runs to pull per request. 100 is the API maximum and the whole point:
# the consecutive-failure count is read out of this window, so a bigger window is
# a longer streak we can state exactly rather than as a lower bound.
RUNS_PAGE_SIZE = 100

# Matches check-stars.py: past this, one summary line beats a wall of messages.
MAX_NOTIFICATIONS = 20

# A conclusion is a verdict on the run, and only some verdicts are verdicts on
# the SOFTWARE. `cancelled` usually means a human or a concurrency group stepped
# in, `skipped` means the job's `if:` said no, `neutral`/`stale`/`action_required`
# are not outcomes anyone can act on from a chat message. Treating any of them as
# red would page people about GitHub's plumbing; treating them as green would
# reset a real failure streak. They are ignored in both directions instead.
FAILING_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
PASSING_CONCLUSIONS = frozenset({"success"})

# Repos whose Actions the token may not read, recorded once each and summarised
# at the end rather than aborting the run — same reasoning as check-stars.py:
# process every repo we CAN read.
_inaccessible: dict[str, str] = {}


def note_inaccessible(repo_full_name: str, detail: str) -> None:
    """Record (once per repo) that the token could not read its Actions runs."""
    if repo_full_name not in _inaccessible:
        _inaccessible[repo_full_name] = detail
        print(f"Skipping {repo_full_name}: {detail}")


def parse_timestamp(raw: str) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp into an aware datetime, or None."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        print(f"Ignoring unparseable timestamp {raw!r}")
        return None


def list_org_repos() -> list[dict]:
    """Every non-archived repo in the org, as {full_name, name, url}.

    Archived repos are excluded because they cannot run anything: their
    workflows are disabled, so the newest scheduled run is a fossil that would
    be re-reported as a fresh failure forever.

    Follows collect_impact.py's iteration: one paginated org listing, and the
    token decides what is visible (a PAT that can see the org's private repos
    gets them included, one that cannot simply does not).
    """
    repos = github_request(f"{GITHUB_API}/orgs/{ORG_NAME}/repos?type=all&per_page=100")
    return [
        {
            "full_name": r["full_name"],
            "name": r["name"],
            "url": r["html_url"],
        }
        for r in repos
        if not r.get("archived", False)
    ]


def fetch_scheduled_runs(url: str) -> tuple[list[dict], bool]:
    """GET one page of an Actions runs endpoint.

    Returns (runs, exhaustive) where `exhaustive` says whether the page holds
    the repo's/workflow's ENTIRE scheduled history. github_request returns a
    dict envelope untouched (no auto-pagination), so this is exactly one page —
    which is deliberate: an org-wide walk cannot afford to page through the
    13 000 scheduled runs that a 15-minute cadence accumulates.
    """
    data = github_request(url)
    runs = data.get("workflow_runs", [])
    total = data.get("total_count", len(runs))
    return runs, total <= len(runs)


def collect_workflow_runs(repo_full_name: str) -> dict[int, dict]:
    """Recent completed scheduled runs for a repo, grouped by workflow.

    Returns {workflow_id: {"name": str, "runs": [...], "exhaustive": bool}},
    newest run first within each workflow. Empty when the repo has never run a
    scheduled workflow — those repos are skipped by the caller, not reported.

    One request covers the whole repo. That is enough right up until a
    high-frequency workflow crowds the page: netresearch/maint runs Star
    Notifications every 15 minutes, so of the 100 most recent scheduled runs in
    that repo, 98 are that one workflow and only 2 belong to the nightly Impact
    Dashboard — a couple more and the dashboard would vanish from the page and
    its failures would be invisible to this notifier. So when the page is
    truncated, the repo's workflow list is fetched and every workflow missing
    from the page is queried on its own.
    """
    runs, exhaustive = fetch_scheduled_runs(
        f"{GITHUB_API}/repos/{repo_full_name}/actions/runs"
        f"?event=schedule&status=completed&per_page={RUNS_PAGE_SIZE}&exclude_pull_requests=true"
    )

    grouped: dict[int, dict] = {}
    for run in runs:
        workflow_id = run.get("workflow_id")
        if workflow_id is None:
            continue
        group = grouped.setdefault(
            workflow_id,
            {"name": run.get("name") or f"workflow {workflow_id}", "runs": [], "exhaustive": exhaustive},
        )
        group["runs"].append(run)

    if not exhaustive:
        _fill_crowded_out_workflows(repo_full_name, grouped)

    return grouped


def _fill_crowded_out_workflows(repo_full_name: str, grouped: dict[int, dict]) -> None:
    """Query, per workflow, the ones that did not fit on the repo-wide page.

    Mutates `grouped` in place. A workflow that has never run on a schedule
    comes back with zero runs and is left out, so this cannot invent entries for
    push-only workflows.
    """
    workflows = github_request(
        f"{GITHUB_API}/repos/{repo_full_name}/actions/workflows?per_page={RUNS_PAGE_SIZE}"
    ).get("workflows", [])

    for workflow in workflows:
        workflow_id = workflow.get("id")
        # `disabled_manually` / `disabled_inactivity` workflows do not run, so a
        # stale red run of theirs is history, not an incident.
        if workflow_id is None or workflow_id in grouped or workflow.get("state") != "active":
            continue
        runs, exhaustive = fetch_scheduled_runs(
            f"{GITHUB_API}/repos/{repo_full_name}/actions/workflows/{workflow_id}/runs"
            f"?event=schedule&status=completed&per_page={RUNS_PAGE_SIZE}&exclude_pull_requests=true"
        )
        if not runs:
            continue
        grouped[workflow_id] = {
            "name": workflow.get("name") or runs[0].get("name") or f"workflow {workflow_id}",
            "runs": runs,
            "exhaustive": exhaustive,
        }


def summarise_workflow(group: dict) -> dict | None:
    """Reduce one workflow's runs to the status a message can be written from.

    Returns None when no run in the window carries an actionable conclusion —
    e.g. every recent run was cancelled — because that is not evidence of
    either health or breakage, and acting on it either way would be a guess.

    The streak walks newest to oldest, counting failures and stepping over
    inconclusive runs without breaking the chain: a cancelled run in the middle
    of a fortnight of failures does not mean the fortnight ended. `at_least` is
    set when the window runs out before a success is found, so the message can
    say "at least 100" instead of stating a floor as if it were the total.
    """
    runs = group["runs"]
    latest = next(
        (r for r in runs if r.get("conclusion") in FAILING_CONCLUSIONS | PASSING_CONCLUSIONS),
        None,
    )
    if latest is None:
        return None

    failing = latest.get("conclusion") in FAILING_CONCLUSIONS
    summary = {
        "workflow_name": group["name"],
        "failing": failing,
        "run_url": latest.get("html_url", ""),
        "conclusion": latest.get("conclusion", ""),
        "consecutive_failures": 0,
        "failing_since": None,
        "at_least": False,
    }
    if not failing:
        return summary

    count = 0
    oldest_failure = None
    hit_success = False
    for run in runs:
        conclusion = run.get("conclusion")
        if conclusion in PASSING_CONCLUSIONS:
            hit_success = True
            break
        if conclusion not in FAILING_CONCLUSIONS:
            continue
        count += 1
        oldest_failure = run.get("run_started_at") or run.get("created_at")

    summary["consecutive_failures"] = count
    summary["failing_since"] = oldest_failure
    summary["at_least"] = not hit_success and not group["exhaustive"]
    return summary


def load_state() -> dict:
    """Load previous statuses from the state file.

    The mechanism is a workflow ARTIFACT (downloaded at the start of the job,
    re-uploaded at the end), matching star-notifications.yml. Weighed against:

    - A committed state file would put a commit on main on every run — noise in
      the history of a repo people actually read, and a push race with any other
      job. Rejected.
    - The Actions cache is evicted after 7 days without a read and entries are
      immutable per key, so keeping it current means rotating keys on every run.
      Its eviction window is also shorter than the 7-day reminder interval this
      needs to measure. Rejected.
    - An artifact carries the accepted cost: if it expires or is deleted, the
      next run has no history. That is handled rather than merely accepted — see
      `format_baseline`, which announces a lost/first baseline as a single
      summary line instead of re-announcing every red workflow as a new failure.
      Retention is refreshed on every run, so it cannot age out while the
      workflow is alive.
    """
    raw: object = {}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # A corrupt artifact must not wedge the notifier permanently; losing
            # the baseline costs one summary line, a hard failure costs silence.
            print(f"::warning::State file is unreadable ({e}); starting from an empty baseline")
    if not isinstance(raw, dict):
        print(f"::warning::State file holds a {type(raw).__name__}, not an object; starting from an empty baseline")
        raw = {}
    return {
        "last_run": clean_timestamp(raw.get("last_run")),
        "workflows": clean_workflows(raw.get("workflows")),
    }


def clean_timestamp(value: object) -> str | None:
    """Re-emit a timestamp parsed from `value`, or None if it is not one."""
    parsed = parse_timestamp(value) if isinstance(value, str) else None
    return parsed.isoformat() if parsed else None


def clean_workflows(value: object) -> dict[str, dict]:
    """Rebuild the per-workflow state from validated primitives.

    The file is whatever the downloaded artifact contained, and the rest of this
    module reads it assuming a shape: `previous.get("failing")`, a `timedelta`
    against `last_notified`, an int failure count. Trusting that shape put an
    AttributeError one malformed entry away from killing a 190-repo run, and
    handing the parsed blob straight back to the writer is what makes the write
    read as a path sink to static analysis (Sonar S2083). Reconstructing it from
    the four fields actually read — coerced, with timestamps re-emitted from
    parsed datetimes — fixes both, and everything else in an entry is rewritten
    from live API data each run anyway.
    """
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, dict] = {}
    for key, entry in value.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        count = entry.get("consecutive_failures")
        cleaned[key] = {
            "failing": bool(entry.get("failing")),
            "consecutive_failures": count if isinstance(count, int) and count >= 0 else 0,
            "failing_since": clean_timestamp(entry.get("failing_since")),
            "last_notified": clean_timestamp(entry.get("last_notified")),
        }
    return cleaned


def save_state(workflows: dict) -> None:
    """Write the state file, creating its directory if needed.

    Takes the workflow map rather than the loaded state dict, so nothing that
    came out of the artifact is carried back into the write untouched.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_run": datetime.now(timezone.utc).isoformat(), "workflows": workflows}
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def describe_age(since: str | None, now: datetime) -> str:
    """Render 'since 2026-07-28 (13 days)' from an ISO timestamp."""
    parsed = parse_timestamp(since or "")
    if parsed is None:
        return "since an unknown date"
    days = max(0, (now - parsed).days)
    unit = "day" if days == 1 else "days"
    return f"since {parsed.date().isoformat()} ({days} {unit})"


def format_failure(repo: dict, summary: dict, now: datetime, reminder: bool) -> str:
    """One line carrying everything needed to act without opening GitHub."""
    count = summary["consecutive_failures"]
    at_least = "at least " if summary["at_least"] else ""
    plural = "failure" if count == 1 else "failures"
    icon, lede = ("🔁", "is STILL failing") if reminder else ("🔴", "is failing")
    return (
        f"{icon} [{repo['name']}]({repo['url']}) scheduled workflow \"{summary['workflow_name']}\" "
        f"{lede} — {at_least}{count} consecutive {plural} {describe_age(summary['failing_since'], now)}. "
        f"[Latest run]({summary['run_url']}) ([?]({HELP_URL}))"
    )


def format_recovery(repo: dict, summary: dict, previous: dict) -> str:
    """Recovery note. Short on purpose: a channel of pure bad news gets muted."""
    count = previous.get("consecutive_failures", 0)
    plural = "failure" if count == 1 else "failures"
    return (
        f"🟢 [{repo['name']}]({repo['url']}) scheduled workflow \"{summary['workflow_name']}\" "
        f"recovered after {count} {plural}. [Run]({summary['run_url']}) ([?]({HELP_URL}))"
    )


def format_baseline(failing: list[tuple[dict, dict]], now: datetime) -> str:
    """One summary line for a first run, or a run whose state artifact was lost.

    Announcing each of these as a fresh green -> red transition would be a lie
    (they did not just break) and a flood. Announcing nothing would hide exactly
    the situation this tool exists for. A single line does neither.
    """
    parts = [
        f"{repo['name']} / {s['workflow_name']} ({s['consecutive_failures']}x, "
        f"{describe_age(s['failing_since'], now)})"
        for repo, s in failing
    ]
    return (
        f"📋 Scheduled-failure monitoring has no previous state (first run, or the state artifact "
        f"expired). Baseline: {len(failing)} scheduled workflow(s) currently failing — "
        f"{'; '.join(parts)}. Weekly reminders continue from here. ([?]({HELP_URL}))"
    )


def classify(repo: dict, summary: dict, previous: dict | None, now: datetime) -> tuple[str, str | None]:
    """Decide what this workflow's status means relative to last run.

    Returns (event, message). `event` is one of new-failure / reminder /
    recovery / quiet, and `message` is None for quiet. Splitting the decision
    from the sending is what makes the transition rules testable without a
    webhook anywhere near them.
    """
    was_failing = bool(previous and previous.get("failing"))

    if summary["failing"] and not was_failing:
        return "new-failure", format_failure(repo, summary, now, reminder=False)

    if summary["failing"] and was_failing:
        last_notified = parse_timestamp(previous.get("last_notified") or "")
        due = last_notified is None or now - last_notified >= timedelta(days=REMINDER_INTERVAL_DAYS)
        if due:
            return "reminder", format_failure(repo, summary, now, reminder=True)
        return "quiet", None

    if not summary["failing"] and was_failing:
        return "recovery", format_recovery(repo, summary, previous)

    return "quiet", None


def state_entry(repo: dict, summary: dict, previous: dict | None, notified: bool, now: datetime) -> dict:
    """The record to carry into the next run.

    `last_notified` only moves when something was actually said, so a quiet
    cycle cannot silently reset the weekly reminder clock and stretch a
    fortnight of failures into a fortnight of silence.
    """
    carried = (previous or {}).get("last_notified")
    return {
        "repo": repo["full_name"],
        "repo_url": repo["url"],
        "workflow_name": summary["workflow_name"],
        "failing": summary["failing"],
        "consecutive_failures": summary["consecutive_failures"],
        # Normalised on the way in, so the file is byte-stable across a
        # write/read cycle: GitHub sends `...Z`, clean_timestamp re-emits
        # `...+00:00`, and without this the state churns representation on every
        # reload for no reason.
        "failing_since": clean_timestamp(summary["failing_since"]),
        "last_notified": now.isoformat() if notified else carried,
    }


def send(messages: list[str], dry_run: bool) -> int:
    """Post messages to Matrix, or print them under --dry-run.

    Returns the number sent. Mirrors check-stars.py: cap the burst, and on the
    first webhook error stop trying — a dead webhook will not revive within one
    run, and the state still gets saved either way.
    """
    if dry_run:
        for message in messages:
            print(f"[dry-run] would post: {message}")
        print(f"[dry-run] {len(messages)} message(s) withheld; nothing was sent to Matrix")
        return 0

    sent = 0
    for message in messages[:MAX_NOTIFICATIONS]:
        try:
            post_to_matrix(MATRIX_WEBHOOK_URL, message)
            sent += 1
        except requests.exceptions.RequestException as e:
            print(f"Failed to send Matrix notification. Error: {type(e).__name__}")
            print("Matrix webhook appears unreachable, skipping remaining notifications")
            return sent

    remaining = len(messages) - MAX_NOTIFICATIONS
    if remaining > 0:
        try:
            post_to_matrix(
                MATRIX_WEBHOOK_URL,
                f"📊 +{remaining} more scheduled-workflow event(s). [See full log]({FEED_URL}) ([?]({HELP_URL}))",
            )
            sent += 1
        except requests.exceptions.RequestException as e:
            print(f"Failed to send Matrix summary. Error: {type(e).__name__}")
        print(f"Truncated: {remaining} additional notifications not sent")
    return sent


def walk_repo(repo: dict) -> dict[int, dict] | None:
    """Grouped scheduled runs for one repo, or None if it could not be read."""
    try:
        return collect_workflow_runs(repo["full_name"])
    except AuthError as e:
        note_inaccessible(repo["full_name"], str(e))
        return None
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 404:
            # Actions disabled, or the token cannot see the repo any more.
            note_inaccessible(repo["full_name"], "Actions not available (HTTP 404)")
            return None
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the messages instead of posting them, and do not write the state file",
    )
    args = parser.parse_args(argv)

    if not GITHUB_TOKEN:
        raise AuthError("no token in the environment")
    if not args.dry_run and not MATRIX_WEBHOOK_URL:
        raise SystemExit("::error::MATRIX_WEBHOOK_URL is not set; use --dry-run to run without it")

    now = datetime.now(timezone.utc)
    state = load_state()
    previous_workflows = state.get("workflows", {})
    is_baseline = not state.get("last_run")

    repos = list_org_repos()
    print(f"Checking {len(repos)} non-archived repo(s) in {ORG_NAME} for failing scheduled runs")

    next_workflows: dict[str, dict] = {}
    messages: list[str] = []
    baseline_failures: list[tuple[dict, dict]] = []
    counts = {"new-failure": 0, "reminder": 0, "recovery": 0, "quiet": 0}
    repos_with_schedules = 0

    for repo in repos:
        grouped = walk_repo(repo)
        if grouped is None:
            # Unreadable: carry that repo's history forward untouched, so a
            # transient permission blip does not erase a running failure streak
            # and re-announce it as new next time.
            prefix = f"{repo['full_name']}::"
            next_workflows.update({k: v for k, v in previous_workflows.items() if k.startswith(prefix)})
            continue
        if grouped:
            repos_with_schedules += 1

        for workflow_id, group in grouped.items():
            summary = summarise_workflow(group)
            if summary is None:
                continue
            key = f"{repo['full_name']}::{workflow_id}"
            previous = previous_workflows.get(key)

            if is_baseline:
                # Nothing to compare against: record, and let report_baseline
                # say it once rather than pretending each is a fresh break.
                if summary["failing"]:
                    baseline_failures.append((repo, summary))
                next_workflows[key] = state_entry(repo, summary, previous, notified=summary["failing"], now=now)
                continue

            event, message = classify(repo, summary, previous, now)
            counts[event] += 1
            if message:
                messages.append(message)
                print(f"{event}: {key}")
            next_workflows[key] = state_entry(repo, summary, previous, notified=bool(message), now=now)

    if is_baseline and baseline_failures:
        messages.append(format_baseline(baseline_failures, now))

    sent = send(messages, args.dry_run)

    if args.dry_run:
        print("[dry-run] state file not written")
    else:
        save_state(next_workflows)

    tracked = len(next_workflows)
    if is_baseline:
        print(f"Baseline run — tracking {tracked} scheduled workflow(s), {len(baseline_failures)} already failing")
    else:
        print(
            f"Scheduled workflows tracked: {tracked} across {repos_with_schedules} repo(s). "
            f"New failures: {counts['new-failure']}, reminders: {counts['reminder']}, "
            f"recoveries: {counts['recovery']}, unchanged: {counts['quiet']}. Sent: {sent}."
        )

    if _inaccessible:
        print(
            f"::warning::Skipped {len(_inaccessible)} of {len(repos)} repo(s) whose Actions runs "
            f"could not be read (Actions disabled, or the token lacks Actions: read). "
            f"The rest were processed normally."
        )
        for name, reason in sorted(_inaccessible.items()):
            print(f"  - {name}: {reason}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AuthError as e:
        print(f"::error::{e}. Check that SCHEDULED_FAILURES_PAT is set, unexpired, and grants "
              f"Metadata: read and Actions: read on the {ORG_NAME} org.")
        raise SystemExit(1) from e
    except RateLimitError as e:
        print(f"::error::{e}")
        raise SystemExit(1) from e
