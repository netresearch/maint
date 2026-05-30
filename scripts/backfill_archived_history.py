#!/usr/bin/env python3
"""One-time backfill: fold archived repos into the historical daily totals.

Until now ``collect_impact.py`` excluded archived repos, so the day a repo was
archived it dropped out of ``data/history.json``'s daily ``totals`` (and repos
that were already archived before the dashboard started were never counted at
all). On the "Growth over time" chart that shows up as sudden downward steps,
even though no stars/forks/contributors were actually lost.

``collect_impact.py`` now keeps archived repos in the totals going forward.
This script repairs the *existing* history so the line is continuous instead of
dipping. For every daily entry it adds the contribution of each in-scope
archived repo that was **not already counted that day**, so repos that were
still active earlier in the window are never double-counted.

Per-day membership is read from ``data/snapshots/<date>.json``. The frozen
metric values come from the last daily snapshot the repo appeared in -- so for
a repo archived mid-window the backfilled value equals what was counted the day
before it was archived, and the archive boundary cancels with no residual step.
Repos archived before the dashboard existed never appear in any snapshot; their
values are collected once from the GitHub API (set ``--no-api`` to skip them,
which leaves them out of history -- the collector will then introduce a small
one-time step on its next run instead).

Run once against a gh-pages checkout, then commit the updated history.json
(OUTPUT_DIR is the same env var collect_impact.py uses for its build dir):

    OUTPUT_DIR=/path/to/gh-pages-checkout GITHUB_TOKEN=... \
        python scripts/backfill_archived_history.py

Pass ``--dry-run`` to print the corrected totals without writing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

from collect_impact import (
    OUTPUT_DIR,
    aggregate_totals,
    collect_repo,
    list_org_containers,
    list_public_members,
    list_target_repos,
    load_config,
)

# Keys in aggregate_totals() that are summed across repos. "repos" is a member
# count handled separately (per-day membership), so it is added on its own.
_SKIP_TOTALS_KEYS = {"repos"}

# Per-date set of repo names, populated by last_seen_records() and reused by
# load_snapshot_names() so each snapshot file is read and parsed only once.
_SNAPSHOT_NAMES: dict[str, set[str]] = {}


def repo_contribution(record: dict) -> dict:
    """Return the totals a single repo contributes to aggregate_totals()."""
    totals = aggregate_totals([record])
    return {k: v for k, v in totals.items() if k not in _SKIP_TOTALS_KEYS}


def load_snapshot_names(base: Path, date: str) -> set[str] | None:
    """Repo names present in the snapshot for ``date`` (None if it is missing).

    Served from the cache populated by last_seen_records(); only dates with no
    snapshot file fall through to a disk check.
    """
    if date in _SNAPSHOT_NAMES:
        return _SNAPSHOT_NAMES[date]
    path = base / "data" / "snapshots" / f"{date}.json"
    if not path.exists():
        return None
    names = {r["name"] for r in json.loads(path.read_text()).get("repos", [])}
    _SNAPSHOT_NAMES[date] = names
    return names


def last_seen_records(base: Path) -> dict[str, dict]:
    """Each repo's record from the most recent daily snapshot it appeared in.

    Also fills _SNAPSHOT_NAMES so load_snapshot_names() avoids re-reading the
    same files during the per-day pass.
    """
    seen: dict[str, dict] = {}
    for path in sorted((base / "data" / "snapshots").glob("*.json")):  # latest wins
        repos = json.loads(path.read_text()).get("repos", [])
        _SNAPSHOT_NAMES[path.stem] = {r["name"] for r in repos}
        for repo in repos:
            seen[repo["name"]] = repo
    return seen


def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    use_api = "--no-api" not in args
    base = OUTPUT_DIR

    history_path = base / "data" / "history.json"
    if not history_path.exists():
        print(f"No history at {history_path}", file=sys.stderr)
        return 1
    history = json.loads(history_path.read_text())
    daily = history.get("daily", [])
    if not daily:
        print("History has no daily entries; nothing to backfill.", file=sys.stderr)
        return 0

    cfg = load_config()
    print("Listing in-scope repos (archived included)...", file=sys.stderr)
    archived = [r for r in list_target_repos(cfg) if r.get("archived")]
    print(f"Found {len(archived)} archived in-scope repos.", file=sys.stderr)
    if not archived:
        print("Nothing archived in scope; history unchanged.", file=sys.stderr)
        return 0

    # Frozen contribution per archived repo: prefer its last snapshot value so
    # mid-window archive boundaries cancel exactly; only repos that never appear
    # in any snapshot need a fresh API collection.
    seen = last_seen_records(base)
    contributions: dict[str, dict] = {
        r["name"]: repo_contribution(seen[r["name"]])
        for r in archived
        if r["name"] in seen
    }
    need_fresh = [r for r in archived if r["name"] not in seen]
    if need_fresh:
        names = ", ".join(sorted(r["name"] for r in need_fresh))
        if use_api:
            print(f"Collecting {len(need_fresh)} never-seen archived repos from API: {names}", file=sys.stderr)
            members = list_public_members()
            containers = list_org_containers()
            for repo in need_fresh:
                try:
                    record = collect_repo(repo, members, containers)
                except requests.RequestException as e:
                    # One unreachable repo (deleted, transient error) must not
                    # abort the whole backfill. Log the type only -- never the
                    # exception object, which can carry tokens in the URL.
                    print(f"  ERROR collecting {repo['name']}: {type(e).__name__}, skipping", file=sys.stderr)
                    continue
                record["archived"] = True
                contributions[repo["name"]] = repo_contribution(record)
        else:
            print(f"--no-api: leaving {len(need_fresh)} never-seen archived repos OUT of history: {names}", file=sys.stderr)

    changed = 0
    for entry in daily:
        totals = entry.setdefault("totals", {})
        present = load_snapshot_names(base, entry["date"])
        for name, contribution in contributions.items():
            if present is not None and name in present:
                continue  # already counted that day -> no double-count
            for key, value in contribution.items():
                totals[key] = totals.get(key, 0) + value
            totals["repos"] = totals.get("repos", 0) + 1
            changed += 1

    print(
        f"Backfilled {len(contributions)} archived repos across {len(daily)} days "
        f"({changed} repo-day additions).",
        file=sys.stderr,
    )
    print(json.dumps(daily[-1]["totals"], indent=2))

    if dry_run:
        print("--dry-run: history.json not written.", file=sys.stderr)
        return 0

    history["daily"] = daily
    history_path.write_text(json.dumps(history, indent=2))
    print(f"Wrote {history_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
