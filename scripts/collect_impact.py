#!/usr/bin/env python3
"""Collect GitHub impact metrics for netresearch TYPO3 extensions and skill repos.

Writes JSON snapshots under ./build/data/ for consumption by the static dashboard:
  data/latest.json              Current snapshot of all repos
  data/snapshots/YYYY-MM-DD.json Daily archive (point-in-time)
  data/history.json             Append-only daily rollup for time-series charts
  data/repos/<name>.json        Per-repo detail

Core metrics work with the workflow's built-in GITHUB_TOKEN.
Traffic metrics (clones/views/referrers) require a PAT with `repo` scope on the
target repos; set IMPACT_DASHBOARD_PAT. Without it, traffic is omitted.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"
ORG_NAME = os.environ.get("ORG_NAME", "netresearch")
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "build"))
CORE_TOKEN = os.environ["GITHUB_TOKEN"]
TRAFFIC_TOKEN = os.environ.get("IMPACT_DASHBOARD_PAT") or None
SCRAPE_UA = "netresearch-impact-dashboard/1.0 (+https://github.com/netresearch/maint)"
SCRAPE_HEADERS = {"User-Agent": SCRAPE_UA, "Accept": "text/html"}

NOW = datetime.now(timezone.utc)
TODAY = NOW.date().isoformat()
WINDOW_30D = (NOW - timedelta(days=30)).isoformat()


def auth_headers(token: str = CORE_TOKEN) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_get(url: str, token: str = CORE_TOKEN, allow_status: tuple[int, ...] = ()) -> requests.Response:
    """GET with built-in retry for transient errors and secondary rate limits."""
    for attempt in range(4):
        try:
            r = requests.get(url, headers=auth_headers(token), timeout=30)
        except requests.RequestException as e:
            if attempt < 3:
                print(f"  network error ({type(e).__name__}), retrying", file=sys.stderr)
                time.sleep(2 ** attempt)
                continue
            raise
        if r.status_code in allow_status:
            return r
        if r.status_code in (403, 429) and "rate limit" in r.text.lower():
            reset = int(r.headers.get("x-ratelimit-reset", "0"))
            wait = max(5, min(60, reset - int(time.time()))) if reset else 15
            print(f"  rate-limited, sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if r.status_code >= 500 and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def gh_get_all(url: str, token: str = CORE_TOKEN) -> list[dict]:
    """Paginate through a list endpoint and return all items."""
    results: list[dict] = []
    while url:
        r = gh_get(url, token=token)
        data = r.json()
        if not isinstance(data, list):
            return data  # type: ignore[return-value]
        results.extend(data)
        url = r.links.get("next", {}).get("url")  # type: ignore[assignment]
    return results


def search_count(q: str) -> int:
    """Return total_count from the issues/PRs search API."""
    url = f"{GITHUB_API}/search/issues?q={quote(q)}&per_page=1"
    r = gh_get(url)
    return int(r.json().get("total_count", 0))


def count_commits_since(full_name: str, since_iso: str | None = None) -> int:
    """Use the Link header's last-page trick to count commits cheaply."""
    q = f"per_page=1"
    if since_iso:
        q += f"&since={since_iso}"
    url = f"{GITHUB_API}/repos/{full_name}/commits?{q}"
    r = gh_get(url, allow_status=(409,))
    if r.status_code == 409:  # empty repo
        return 0
    last = r.links.get("last", {}).get("url", "")
    m = re.search(r"[?&]page=(\d+)", last)
    if m:
        return int(m.group(1))
    return len(r.json())


# ---- repo discovery -------------------------------------------------------


def classify(name: str) -> str | None:
    if name.startswith("t3x-"):
        return "typo3-extension"
    if name.endswith("-skill"):
        return "skill"
    return None


def list_target_repos() -> list[dict]:
    repos = gh_get_all(f"{GITHUB_API}/orgs/{ORG_NAME}/repos?type=public&per_page=100")
    selected = []
    for r in repos:
        if r.get("archived") or r.get("private"):
            continue
        category = classify(r["name"])
        if category is None:
            continue
        r["_category"] = category
        selected.append(r)
    selected.sort(key=lambda r: r["name"])
    return selected


def list_public_members() -> set[str]:
    members = gh_get_all(f"{GITHUB_API}/orgs/{ORG_NAME}/public_members?per_page=100")
    return {m["login"].lower() for m in members}


# ---- per-repo metric fetchers ---------------------------------------------


def fetch_issue_pr_counts(full_name: str) -> dict:
    base = f"repo:{full_name}"
    recent = f" created:>={WINDOW_30D[:10]}"
    return {
        "issues_open": search_count(f"{base} is:issue is:open"),
        "issues_closed": search_count(f"{base} is:issue is:closed"),
        "prs_open": search_count(f"{base} is:pr is:open"),
        "prs_merged": search_count(f"{base} is:pr is:merged"),
        "prs_closed_unmerged": search_count(f"{base} is:pr is:closed is:unmerged"),
        "issues_opened_30d": search_count(f"{base} is:issue{recent}"),
        "prs_opened_30d": search_count(f"{base} is:pr{recent}"),
        "prs_merged_30d": search_count(f"{base} is:pr is:merged merged:>={WINDOW_30D[:10]}"),
    }


def fetch_releases(full_name: str) -> dict:
    releases = gh_get_all(f"{GITHUB_API}/repos/{full_name}/releases?per_page=100")
    total_downloads = sum(
        a.get("download_count", 0) for rel in releases for a in rel.get("assets", [])
    )
    recent = [r for r in releases if r.get("published_at", "") >= WINDOW_30D]
    latest = releases[0] if releases else None
    return {
        "releases": len(releases),
        "releases_30d": len(recent),
        "release_downloads": total_downloads,
        "latest": {
            "name": latest.get("name") or latest.get("tag_name"),
            "tag_name": latest.get("tag_name"),
            "published_at": latest.get("published_at"),
            "url": latest.get("html_url"),
        } if latest else None,
    }


def fetch_contributors(full_name: str, org_members: set[str]) -> dict:
    contribs = gh_get_all(f"{GITHUB_API}/repos/{full_name}/contributors?per_page=100&anon=0")
    total = len(contribs)
    external = [c for c in contribs if c.get("login", "").lower() not in org_members and c.get("type") != "Bot"]
    top = sorted(contribs, key=lambda c: c.get("contributions", 0), reverse=True)[:10]
    return {
        "contributors": total,
        "external_contributors": len(external),
        "top": [
            {"login": c.get("login"), "contributions": c.get("contributions", 0), "url": c.get("html_url")}
            for c in top
            if c.get("login")
        ],
    }


def fetch_traffic(full_name: str) -> dict | None:
    if not TRAFFIC_TOKEN:
        return None
    try:
        clones = gh_get(f"{GITHUB_API}/repos/{full_name}/traffic/clones", token=TRAFFIC_TOKEN).json()
        views = gh_get(f"{GITHUB_API}/repos/{full_name}/traffic/views", token=TRAFFIC_TOKEN).json()
        referrers = gh_get(f"{GITHUB_API}/repos/{full_name}/traffic/popular/referrers", token=TRAFFIC_TOKEN).json()
        paths = gh_get(f"{GITHUB_API}/repos/{full_name}/traffic/popular/paths", token=TRAFFIC_TOKEN).json()
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (403, 404):
            print(f"  traffic unavailable for {full_name}: {e.response.status_code}", file=sys.stderr)
            return None
        raise
    return {
        "clones_total": clones.get("count", 0),
        "clones_unique": clones.get("uniques", 0),
        "views_total": views.get("count", 0),
        "views_unique": views.get("uniques", 0),
        "top_referrers": referrers[:10],
        "top_paths": paths[:10],
    }


def fetch_packagist(composer_name: str) -> dict | None:
    """Return Packagist download stats if the package is published."""
    url = f"https://packagist.org/packages/{composer_name}.json"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        pkg = r.json().get("package", {})
        dl = pkg.get("downloads", {})
        return {
            "name": composer_name,
            "total": dl.get("total", 0),
            "monthly": dl.get("monthly", 0),
            "daily": dl.get("daily", 0),
            "url": f"https://packagist.org/packages/{composer_name}",
        }
    except requests.RequestException as e:
        print(f"  packagist lookup failed for {composer_name}: {type(e).__name__}", file=sys.stderr)
        return None


def fetch_composer_name(full_name: str, default_branch: str) -> str | None:
    url = f"{GITHUB_API}/repos/{full_name}/contents/composer.json?ref={default_branch}"
    r = gh_get(url, allow_status=(404,))
    if r.status_code == 404:
        return None
    import base64
    content = r.json().get("content", "")
    try:
        data = json.loads(base64.b64decode(content))
    except (ValueError, json.JSONDecodeError):
        return None
    name = data.get("name")
    return name if isinstance(name, str) and "/" in name else None


# ---- HTML scrapers (no public API) ---------------------------------------


def scrape_html(url: str) -> str | None:
    try:
        r = requests.get(url, headers=SCRAPE_HEADERS, timeout=30, allow_redirects=True)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        print(f"  scrape failed {url}: {type(e).__name__}", file=sys.stderr)
        return None


def list_org_containers() -> dict[str, list[str]]:
    """Map repo name -> list of GHCR container package names owned by that repo.

    Requires a token with `read:packages` scope. If unavailable (403), returns
    an empty map and the collector falls back to probing package URLs per repo.
    """
    url = f"{GITHUB_API}/orgs/{ORG_NAME}/packages?package_type=container&per_page=100"
    try:
        packages = gh_get_all(url)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403, 404):
            print(
                f"  container listing unavailable ({e.response.status_code}); "
                "will probe each repo for a package named after it",
                file=sys.stderr,
            )
            return {}
        raise
    mapping: dict[str, list[str]] = {}
    for pkg in packages:
        repo = (pkg.get("repository") or {}).get("name")
        name = pkg.get("name")
        if repo and name:
            mapping.setdefault(repo, []).append(name)
    return mapping


# Matches: <h3 title="130712">131K</h3> following a "Total downloads" label.
_GHCR_TOTAL_RE = re.compile(
    r'Total downloads</span>\s*<h3[^>]*title="(\d+)"',
    re.DOTALL,
)
# Matches the 30-day sparkline block by its aria-label.
_GHCR_30D_BLOCK_RE = re.compile(
    r'aria-label="Downloads for the last 30 days".*?</svg>',
    re.DOTALL,
)
_GHCR_MERGE_COUNT_RE = re.compile(r'data-merge-count="(\d+)"')


def fetch_ghcr_stats(repo: str, pkg: str) -> dict | None:
    """Scrape the package overview page for total + last-30-day download counts."""
    url = f"{GITHUB_WEB}/{ORG_NAME}/{repo}/pkgs/container/{pkg}"
    html = scrape_html(url)
    if not html:
        return None
    total_m = _GHCR_TOTAL_RE.search(html)
    block_m = _GHCR_30D_BLOCK_RE.search(html)
    thirty_day = 0
    if block_m:
        thirty_day = sum(int(x) for x in _GHCR_MERGE_COUNT_RE.findall(block_m.group(0)))
    return {
        "package": pkg,
        "url": url,
        "total": int(total_m.group(1)) if total_m else 0,
        "thirty_day": thirty_day,
    }


def fetch_ghcr_for_repo(repo: str, pkg_names: list[str]) -> dict | None:
    """Aggregate GHCR stats across all container packages owned by a repo.

    If pkg_names is empty (no API listing available), probe `{repo}` as the
    package name (common convention, e.g. netresearch/ofelia publishes `ofelia`).
    """
    probe_names = pkg_names or [repo]
    packages = []
    for p in probe_names:
        s = fetch_ghcr_stats(repo, p)
        if s is not None:
            packages.append(s)
    if not packages:
        return None
    return {
        "packages": packages,
        "total": sum(p["total"] for p in packages),
        "thirty_day": sum(p["thirty_day"] for p in packages),
    }


# Matches: <a ... dependent_type=REPOSITORY"> ... </svg> 1,234 Repositories
_DEPENDENTS_REPO_RE = re.compile(
    r'dependent_type=REPOSITORY"[^>]*>.*?</svg>\s*([\d,]+)\s*Repositories',
    re.DOTALL,
)
_DEPENDENTS_PKG_RE = re.compile(
    r'dependent_type=PACKAGE"[^>]*>.*?</svg>\s*([\d,]+)\s*Packages',
    re.DOTALL,
)


def fetch_dependents(full_name: str) -> dict | None:
    """Scrape /network/dependents for the "Used by" counts (repositories + packages)."""
    url = f"{GITHUB_WEB}/{full_name}/network/dependents"
    html = scrape_html(url)
    if html is None:
        return None

    def _to_int(m: re.Match | None) -> int:
        return int(m.group(1).replace(",", "")) if m else 0

    return {
        "repositories": _to_int(_DEPENDENTS_REPO_RE.search(html)),
        "packages": _to_int(_DEPENDENTS_PKG_RE.search(html)),
        "url": url,
    }


# ---- aggregation ----------------------------------------------------------


def collect_repo(repo: dict, org_members: set[str], container_map: dict[str, list[str]]) -> dict:
    full = repo["full_name"]
    name = repo["name"]
    print(f"[{name}] collecting", file=sys.stderr)

    issue_pr = fetch_issue_pr_counts(full)
    releases = fetch_releases(full)
    contribs = fetch_contributors(full, org_members)
    traffic = fetch_traffic(full)
    commits_total = count_commits_since(full)
    commits_30d = count_commits_since(full, since_iso=WINDOW_30D)

    composer = None
    packagist = None
    if repo.get("language") == "PHP":
        composer = fetch_composer_name(full, repo.get("default_branch", "main"))
        if composer:
            packagist = fetch_packagist(composer)

    ghcr = fetch_ghcr_for_repo(name, container_map.get(name, []))
    dependents = fetch_dependents(full)

    blast_radius = (
        contribs["external_contributors"] * 3
        + issue_pr["issues_open"] + issue_pr["issues_closed"]
        + issue_pr["prs_merged"]
        + repo.get("forks_count", 0) * 2
        + (dependents or {}).get("repositories", 0) * 2
    )

    return {
        "name": name,
        "full_name": full,
        "url": repo["html_url"],
        "description": repo.get("description") or "",
        "language": repo.get("language"),
        "category": repo["_category"],
        "license": (repo.get("license") or {}).get("spdx_id"),
        "topics": repo.get("topics", []),
        "homepage": repo.get("homepage") or "",
        "default_branch": repo.get("default_branch"),
        "created_at": repo.get("created_at"),
        "pushed_at": repo.get("pushed_at"),
        "updated_at": repo.get("updated_at"),
        "composer_name": composer,
        "lifetime": {
            "stars": repo.get("stargazers_count", 0),
            "forks": repo.get("forks_count", 0),
            "watchers": repo.get("subscribers_count", 0),
            "network": repo.get("network_count", 0),
            "open_issues_plus_prs": repo.get("open_issues_count", 0),
            "commits": commits_total,
            **{k: v for k, v in issue_pr.items() if not k.endswith("_30d")},
            "releases": releases["releases"],
            "release_downloads": releases["release_downloads"],
            "contributors": contribs["contributors"],
            "external_contributors": contribs["external_contributors"],
            "packagist_downloads": (packagist or {}).get("total", 0) if packagist else 0,
            "ghcr_downloads": (ghcr or {}).get("total", 0),
            "dependents_repos": (dependents or {}).get("repositories", 0),
            "dependents_packages": (dependents or {}).get("packages", 0),
        },
        "recent_30d": {
            "commits": commits_30d,
            "issues_opened": issue_pr["issues_opened_30d"],
            "prs_opened": issue_pr["prs_opened_30d"],
            "prs_merged": issue_pr["prs_merged_30d"],
            "releases": releases["releases_30d"],
            "packagist_downloads": (packagist or {}).get("monthly", 0) if packagist else 0,
            "ghcr_downloads": (ghcr or {}).get("thirty_day", 0),
        },
        "traffic_14d": traffic,
        "latest_release": releases["latest"],
        "top_contributors": contribs["top"],
        "packagist": packagist,
        "ghcr": ghcr,
        "dependents": dependents,
        "blast_radius": blast_radius,
    }


def aggregate_totals(repos: list[dict]) -> dict:
    def sum_of(path: tuple[str, ...]) -> int:
        total = 0
        for r in repos:
            cur: object = r
            for key in path:
                if not isinstance(cur, dict):
                    cur = None
                    break
                cur = cur.get(key)  # type: ignore[union-attr]
            if isinstance(cur, (int, float)):
                total += int(cur)
        return total

    return {
        "repos": len(repos),
        "stars": sum_of(("lifetime", "stars")),
        "forks": sum_of(("lifetime", "forks")),
        "watchers": sum_of(("lifetime", "watchers")),
        "commits": sum_of(("lifetime", "commits")),
        "issues_open": sum_of(("lifetime", "issues_open")),
        "issues_closed": sum_of(("lifetime", "issues_closed")),
        "prs_open": sum_of(("lifetime", "prs_open")),
        "prs_merged": sum_of(("lifetime", "prs_merged")),
        "releases": sum_of(("lifetime", "releases")),
        "release_downloads": sum_of(("lifetime", "release_downloads")),
        "contributors": sum_of(("lifetime", "contributors")),
        "external_contributors": sum_of(("lifetime", "external_contributors")),
        "packagist_downloads": sum_of(("lifetime", "packagist_downloads")),
        "ghcr_downloads": sum_of(("lifetime", "ghcr_downloads")),
        "dependents_repos": sum_of(("lifetime", "dependents_repos")),
        "dependents_packages": sum_of(("lifetime", "dependents_packages")),
        "commits_30d": sum_of(("recent_30d", "commits")),
        "issues_opened_30d": sum_of(("recent_30d", "issues_opened")),
        "prs_opened_30d": sum_of(("recent_30d", "prs_opened")),
        "prs_merged_30d": sum_of(("recent_30d", "prs_merged")),
        "releases_30d": sum_of(("recent_30d", "releases")),
        "packagist_downloads_30d": sum_of(("recent_30d", "packagist_downloads")),
        "ghcr_downloads_30d": sum_of(("recent_30d", "ghcr_downloads")),
    }


def load_history(base: Path) -> dict:
    path = base / "data" / "history.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {"daily": []}


def append_history(history: dict, totals: dict) -> dict:
    daily = [d for d in history.get("daily", []) if d.get("date") != TODAY]
    daily.append({"date": TODAY, "totals": totals})
    daily.sort(key=lambda d: d["date"])
    # Keep roughly 3 years of daily data — plenty for a long-horizon chart.
    if len(daily) > 1100:
        daily = daily[-1100:]
    history["daily"] = daily
    return history


def write_outputs(base: Path, snapshot: dict, history: dict) -> None:
    (base / "data" / "snapshots").mkdir(parents=True, exist_ok=True)
    (base / "data" / "repos").mkdir(parents=True, exist_ok=True)

    (base / "data" / "latest.json").write_text(json.dumps(snapshot, indent=2))
    (base / "data" / "snapshots" / f"{TODAY}.json").write_text(json.dumps(snapshot, indent=2))
    (base / "data" / "history.json").write_text(json.dumps(history, indent=2))

    for repo in snapshot["repos"]:
        (base / "data" / "repos" / f"{repo['name']}.json").write_text(json.dumps(repo, indent=2))


def copy_dashboard_assets(base: Path) -> None:
    src = Path(__file__).resolve().parent.parent / "dashboard"
    if not src.exists():
        return
    import shutil
    for entry in src.iterdir():
        dst = base / entry.name
        if entry.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(entry, dst)
        else:
            shutil.copy2(entry, dst)


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Scanning {ORG_NAME} for matching repos...", file=sys.stderr)
    repos = list_target_repos()
    print(f"Found {len(repos)} repos (t3x-* + *-skill, non-archived).", file=sys.stderr)

    print("Loading org public members...", file=sys.stderr)
    members = list_public_members()
    print(f"Known public org members: {len(members)}", file=sys.stderr)

    print("Enumerating org container packages (GHCR)...", file=sys.stderr)
    containers = list_org_containers()
    print(f"Container packages discovered: {sum(len(v) for v in containers.values())} across {len(containers)} repos", file=sys.stderr)

    collected: list[dict] = []
    for repo in repos:
        try:
            collected.append(collect_repo(repo, members, containers))
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            print(f"  ERROR {repo['name']}: HTTP {status}", file=sys.stderr)

    snapshot = {
        "generated_at": NOW.isoformat(timespec="seconds"),
        "org": ORG_NAME,
        "traffic_available": TRAFFIC_TOKEN is not None,
        "totals": aggregate_totals(collected),
        "repos": collected,
    }

    history = load_history(OUTPUT_DIR)
    history = append_history(history, snapshot["totals"])

    write_outputs(OUTPUT_DIR, snapshot, history)
    copy_dashboard_assets(OUTPUT_DIR)

    print(f"Wrote outputs to {OUTPUT_DIR}/", file=sys.stderr)
    print(json.dumps(snapshot["totals"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
