#!/usr/bin/env python3
"""Check for new GitHub stars on netresearch org repos and notify Matrix."""

import json
import os
from datetime import datetime
from pathlib import Path

import requests

GITHUB_API = "https://api.github.com"
ORG_NAME = os.environ.get("ORG_NAME", "netresearch")
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
MATRIX_WEBHOOK_URL = os.environ["MATRIX_WEBHOOK_URL"]
STATE_FILE = Path("state/stars-state.json")


def github_request(url: str) -> list | dict:
    """Make authenticated GitHub API request."""
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    results = []
    while url:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            results.extend(data)
            url = response.links.get("next", {}).get("url")
        else:
            return data
    return results


def get_org_repos() -> list[dict]:
    """Get all public repos in the organization."""
    repos = github_request(f"{GITHUB_API}/orgs/{ORG_NAME}/repos?type=public&per_page=100")
    return [{"name": r["name"], "full_name": r["full_name"], "url": r["html_url"]} for r in repos]


def get_stargazers(repo_full_name: str) -> list[dict]:
    """Get stargazers for a repo with timestamps."""
    headers_accept = "application/vnd.github.star+json"  # For starred_at timestamp
    url = f"{GITHUB_API}/repos/{repo_full_name}/stargazers?per_page=100"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": headers_accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    results = []
    while url:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        results.extend(response.json())
        url = response.links.get("next", {}).get("url")
    return results


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


def notify_matrix(repo: dict, stargazer: dict, star_count: int) -> None:
    """Send notification to Matrix via webhook."""
    user = stargazer["user"]
    avatar_url = user.get("avatar_url", "")

    message = f"⭐ [{user['login']}]({user['html_url']}) starred [{repo['name']}]({repo['url']}) ({star_count} ⭐)"

    payload = {
        "text": message,
        "avatar_url": avatar_url,
        "displayName": user["login"],
    }
    response = requests.post(MATRIX_WEBHOOK_URL, json=payload)
    response.raise_for_status()
    print(f"Notified: {user['login']} starred {repo['full_name']}")


def main():
    state = load_state()
    repos = get_org_repos()
    new_stars_found = 0

    for repo in repos:
        repo_name = repo["full_name"]
        stargazers = get_stargazers(repo_name)

        known_stargazers = set(state.get("repos", {}).get(repo_name, []))
        current_stargazers = {s["user"]["login"] for s in stargazers}

        new_stargazers = current_stargazers - known_stargazers

        star_count = len(stargazers)
        for stargazer in stargazers:
            if stargazer["user"]["login"] in new_stargazers:
                # Only notify if this isn't the first run (avoid spam on initial setup)
                if state.get("last_run"):
                    notify_matrix(repo, stargazer, star_count)
                new_stars_found += 1

        # Update state with current stargazers
        if "repos" not in state:
            state["repos"] = {}
        state["repos"][repo_name] = list(current_stargazers)

    save_state(state)

    if state.get("last_run"):
        print(f"Found {new_stars_found} new star(s)")
    else:
        print(f"Initial run - indexed {sum(len(v) for v in state['repos'].values())} existing stars")


if __name__ == "__main__":
    main()
