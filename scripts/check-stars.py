#!/usr/bin/env python3
"""Check for new GitHub stars, forks, watchers, and dependents on netresearch org repos and notify Matrix."""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# The GitHub client and its rate-limit policy live in github_api so that this
# script and check_scheduled_failures.py share one implementation rather than
# two that drift apart. Imported by bare name because the scripts run as
# `python scripts/<name>.py`, which puts scripts/ on sys.path.
from github_api import (
    GITHUB_API,
    GITHUB_TOKEN,
    AuthError,
    RateLimitError,
    github_request,
    parse_retry_after,
    post_to_matrix,
)

ORG_NAME = os.environ.get("ORG_NAME", "netresearch")
MATRIX_WEBHOOK_URL = os.environ["MATRIX_WEBHOOK_URL"]
STATE_FILE = Path("state/stars-state.json")
MAX_NOTIFICATIONS = 20
FEED_URL = "https://github.com/netresearch/maint/actions/workflows/star-notifications.yml"


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


def save_state(state: dict) -> None:
    """Save current state to file."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_run"] = datetime.utcnow().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


def notify_matrix(message: str) -> None:
    """Send notification to Matrix via webhook."""
    post_to_matrix(MATRIX_WEBHOOK_URL, message)


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


if __name__ == "__main__":
    try:
        main()
    except RateLimitError as e:
        # Still red — a rate limit that outlasts the whole budget is worth
        # noticing — but red for a stated reason. The schedule fires every 15
        # minutes, so one occurrence is normally self-healing; a run of them
        # means the token's hourly budget is genuinely too small for ~280 repos
        # at this cadence, and the cadence or the request count has to give.
        print(f"::error::{e} The next scheduled run retries with a fresh budget.")
        raise SystemExit(1)
    except AuthError as e:
        print(f"::error::{e}. Check that STAR_NOTIFICATIONS_PAT is set, unexpired, and grants "
              f"Metadata: read on the {ORG_NAME} org.")
        raise SystemExit(1)
