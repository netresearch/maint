#!/usr/bin/env python3
"""Check for new GitHub stars, forks, and watchers on netresearch org repos and notify Matrix."""

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


def github_request(url: str, accept: str = "application/vnd.github+json") -> list | dict:
    """Make authenticated GitHub API request."""
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": accept,
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

    total_new = {"stars": 0, "forks": 0, "watchers": 0}

    for repo in repos:
        repo_name = repo["full_name"]
        repo_state = state.get("repos", {}).get(repo_name, {})
        # Handle old state format (list of stargazers) -> convert to new format
        if isinstance(repo_state, list):
            repo_state = {"stars": repo_state, "forks": [], "watchers": []}

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
                    notify_matrix(msg)
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
                    notify_matrix(msg)
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
                    notify_matrix(msg)
                    print(f"Watch: {watcher['login']} -> {repo_name}")
                total_new["watchers"] += 1

        # Update state
        if "repos" not in state:
            state["repos"] = {}
        state["repos"][repo_name] = {
            "stars": list(current_stars),
            "forks": list(current_forks),
            "watchers": list(current_watchers),
        }

    save_state(state)

    if is_first_run:
        totals = sum(
            len(r.get("stars", [])) + len(r.get("forks", [])) + len(r.get("watchers", []))
            for r in state["repos"].values()
        )
        print(f"Initial run - indexed {totals} existing entries")
    else:
        print(f"Found: {total_new['stars']} star(s), {total_new['forks']} fork(s), {total_new['watchers']} watcher(s)")


if __name__ == "__main__":
    main()
