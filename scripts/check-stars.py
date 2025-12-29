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


def get_stargazers(repo_full_name: str) -> list[dict]:
    """Get stargazers for a repo with timestamps."""
    return github_request(
        f"{GITHUB_API}/repos/{repo_full_name}/stargazers?per_page=100",
        accept="application/vnd.github.star+json",
    )


def get_forks(repo_full_name: str) -> list[dict]:
    """Get forks for a repo."""
    return github_request(f"{GITHUB_API}/repos/{repo_full_name}/forks?per_page=100")


def get_watchers(repo_full_name: str) -> list[dict]:
    """Get watchers (subscribers) for a repo."""
    return github_request(f"{GITHUB_API}/repos/{repo_full_name}/subscribers?per_page=100")


def get_dependents(repo_full_name: str, max_retries: int = 3) -> list[dict]:
    """Get dependents (repositories that depend on this repo) by scraping the network/dependents page."""
    url = f"https://github.com/{repo_full_name}/network/dependents"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NetresearchBot/1.0)",
    }
    
    dependents = []
    last_error = None
    
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
                except Exception as e:
                    print(f"Warning: Could not get info for dependent {dep_full_name}: {e}")
                    # Still add basic info
                    dependents.append({
                        "full_name": dep_full_name,
                        "url": f"https://github.com/{dep_full_name}",
                        "stars": 0,
                        "forks": 0,
                    })
            
            break  # Success, exit retry loop
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                print(f"Retry {attempt + 1}/{max_retries}: {e}, waiting {wait_time}s")
                time.sleep(wait_time)
            else:
                print(f"Failed to get dependents for {repo_full_name}: {e}")
                return []
    else:
        if last_error:
            print(f"Failed to get dependents for {repo_full_name}: {last_error}")
            return []
    
    return dependents


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
        # Handle old state format (list of stargazers) -> convert to new format
        if isinstance(repo_state, list):
            repo_state = {"stars": repo_state, "forks": [], "watchers": [], "dependents": []}
        # Ensure dependents key exists for repos created before this feature
        if "dependents" not in repo_state:
            repo_state["dependents"] = []

        # Stars
        stargazers = get_stargazers(repo_name)
        known_stars = set(repo_state.get("stars", []))
        current_stars = {s["user"]["login"] for s in stargazers}
        new_stars = current_stars - known_stars

        for stargazer in stargazers:
            user = stargazer["user"]
            if user["login"] in new_stars:
                if not is_first_run:
                    msg = f"⭐ [{user['login']}]({user['html_url']}) starred [{repo['name']}]({repo['url']}) ({repo['stargazers_count']} ⭐) ([?](https://github.com/netresearch/maint))"
                    pending_notifications.append(msg)
                    print(f"Star: {user['login']} -> {repo_name}")
                total_new["stars"] += 1

        # Forks
        forks = get_forks(repo_name)
        known_forks = set(repo_state.get("forks", []))
        current_forks = {f["owner"]["login"] for f in forks}
        new_forks = current_forks - known_forks

        for fork in forks:
            owner = fork["owner"]
            if owner["login"] in new_forks:
                if not is_first_run:
                    msg = f"🍴 [{owner['login']}]({owner['html_url']}) forked [{repo['name']}]({repo['url']}) ({repo['forks_count']} 🍴) ([?](https://github.com/netresearch/maint))"
                    pending_notifications.append(msg)
                    print(f"Fork: {owner['login']} -> {repo_name}")
                total_new["forks"] += 1

        # Watchers
        watchers = get_watchers(repo_name)
        known_watchers = set(repo_state.get("watchers", []))
        current_watchers = {w["login"] for w in watchers}
        new_watchers = current_watchers - known_watchers

        for watcher in watchers:
            if watcher["login"] in new_watchers:
                if not is_first_run:
                    msg = f"👀 [{watcher['login']}]({watcher['html_url']}) watching [{repo['name']}]({repo['url']}) ({repo['watchers_count']} 👀) ([?](https://github.com/netresearch/maint))"
                    pending_notifications.append(msg)
                    print(f"Watch: {watcher['login']} -> {repo_name}")
                total_new["watchers"] += 1

        # Dependents (repositories using this repo)
        dependents = get_dependents(repo_name)
        known_dependents = set(repo_state.get("dependents", []))
        current_dependents = {d["full_name"] for d in dependents}
        new_dependents = current_dependents - known_dependents

        for dependent in dependents:
            if dependent["full_name"] in new_dependents:
                if not is_first_run:
                    msg = f"📦 [{dependent['full_name']}]({dependent['url']}) is now using [{repo['name']}]({repo['url']}) ({dependent['stars']} ⭐, {dependent['forks']} 🍴) ([?](https://github.com/netresearch/maint))"
                    pending_notifications.append(msg)
                    print(f"Dependent: {dependent['full_name']} -> {repo_name}")
                total_new["dependents"] += 1

        # Update state
        if "repos" not in state:
            state["repos"] = {}
        state["repos"][repo_name] = {
            "stars": list(current_stars),
            "forks": list(current_forks),
            "watchers": list(current_watchers),
            "dependents": list(current_dependents),
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
