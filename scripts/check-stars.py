#!/usr/bin/env python3
"""Check for new GitHub stars, forks, watchers, and dependents on netresearch org repos and notify Matrix."""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

GITHUB_API = "https://api.github.com"
ORG_NAME = os.environ.get("ORG_NAME", "netresearch")
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
MATRIX_WEBHOOK_URL = os.environ["MATRIX_WEBHOOK_URL"]
STATE_FILE = Path("state/stars-state.json")
MAX_NOTIFICATIONS = 20
FEED_URL = "https://github.com/netresearch/maint/actions/workflows/star-notifications.yml"

# In-memory cache for user details (login -> user info dict)
_user_cache: dict[str, dict] = {}


def github_request(url: str, accept: str = "application/vnd.github+json", max_retries: int = 3) -> list | dict:
    """Make authenticated GitHub API request with retry logic for transient errors."""
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    results = []
    while url:
        last_error = None
        for attempt in range(max_retries):
            try:
                response = requests.get(url, headers=headers, timeout=30)
                # Retry on transient server errors
                if response.status_code in (429, 502, 503, 504):
                    retry_after = int(response.headers.get("Retry-After", 2 ** attempt))
                    print(f"Retry {attempt + 1}/{max_retries}: {response.status_code} for {url}, waiting {retry_after}s")
                    time.sleep(retry_after)
                    continue
                response.raise_for_status()
                data = response.json()
                if isinstance(data, list):
                    results.extend(data)
                    url = response.links.get("next", {}).get("url")
                else:
                    return data
                break  # Success, exit retry loop
            except requests.exceptions.RequestException as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"Retry {attempt + 1}/{max_retries}: {e}, waiting {wait_time}s")
                    time.sleep(wait_time)
                else:
                    raise
        else:
            # All retries exhausted for transient HTTP errors
            if last_error:
                raise last_error
            response.raise_for_status()
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
    except requests.exceptions.RequestException as e:
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
    except requests.exceptions.RequestException as e:
        print(f"Failed to get watchers for {repo_full_name}: {e}")
        return None


def get_dependents(repo_full_name: str, max_retries: int = 3) -> list[dict] | None:
    """Get dependents (repositories that depend on this repo) by scraping the network/dependents page.

    Returns:
        list[dict]: List of dependent repos if successful
        None: If fetch failed (to distinguish from "no dependents exist")
    """
    url = f"https://github.com/{repo_full_name}/network/dependents"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NetresearchBot/1.0)",
    }

    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code in (429, 502, 503, 504):
                retry_after = int(response.headers.get("Retry-After", 2 ** attempt))
                print(f"Retry {attempt + 1}/{max_retries}: {response.status_code} for {url}, waiting {retry_after}s")
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

            dependents = []
            # Find all dependent repository entries
            for item in soup.select('.Box-row'):
                # Look for the repository link
                repo_link = item.select_one('a[data-hovercard-type="repository"]')
                if not repo_link:
                    continue

                dep_full_name = repo_link.get('href', '').lstrip('/')
                if not dep_full_name or '/' not in dep_full_name:
                    continue

                # Get repository info via API
                try:
                    repo_info = github_request(f"{GITHUB_API}/repos/{dep_full_name}")
                    dependents.append({
                        "full_name": dep_full_name,
                        "url": f"https://github.com/{dep_full_name}",
                        "stars": repo_info.get("stargazers_count", 0),
                        "forks": repo_info.get("forks_count", 0),
                    })
                except (requests.exceptions.RequestException, KeyError, ValueError) as e:
                    print(f"Warning: Could not get info for dependent {dep_full_name}: {e}")
                    # Still add basic info
                    dependents.append({
                        "full_name": dep_full_name,
                        "url": f"https://github.com/{dep_full_name}",
                        "stars": 0,
                        "forks": 0,
                    })

            return dependents  # Success, return results (may be empty if truly no dependents)
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
    payload = {"text": message}
    response = requests.post(MATRIX_WEBHOOK_URL, json=payload)
    response.raise_for_status()


def main():
    state = load_state()
    repos = get_org_repos()
    is_first_run = not state.get("last_run")

    total_new = {"stars": 0, "forks": 0, "watchers": 0, "dependents": 0}
    notifications_sent = 0
    pending_notifications = []

    for repo in repos:
        repo_name = repo["full_name"]
        repo_state = state.get("repos", {}).get(repo_name, {})
        # Track if this repo had dependents tracking before any state migrations
        had_dependents_tracking = isinstance(repo_state, dict) and "dependents" in repo_state
        # Handle old state format (list of stargazers) -> convert to new format
        if isinstance(repo_state, list):
            repo_state = {"stars": repo_state, "forks": [], "watchers": []}
        # Stars
        known_stars = set(repo_state.get("stars", []))
        stargazers = get_stargazers(repo_name)
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
        forks = get_forks(repo_name)
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
        watchers = get_watchers(repo_name)
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
        dependents = get_dependents(repo_name)
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

    # Send notifications (limited)
    for msg in pending_notifications[:MAX_NOTIFICATIONS]:
        notify_matrix(msg)
        notifications_sent += 1

    # If there are more, send a summary
    remaining = len(pending_notifications) - MAX_NOTIFICATIONS
    if remaining > 0:
        summary = f"📊 +{remaining} more events. [See full log]({FEED_URL}) ([?](https://github.com/netresearch/maint))"
        notify_matrix(summary)
        print(f"Truncated: {remaining} additional notifications not sent")

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


if __name__ == "__main__":
    main()
