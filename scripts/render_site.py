#!/usr/bin/env python3
"""Render the impact dashboard as static HTML.

The dashboard used to ship an empty shell and build every number in the browser.
Search engines, text browsers and answer engines therefore saw "Loading…" where
the figures should be, and nothing on the page was citable.

This renderer turns the collector's JSON into finished HTML:

  index.html                    English dashboard
  de/index.html                 German dashboard
  repo/<name>/index.html        per-repository detail, one stable URL each
  de/repo/<name>/index.html
  snapshot/<date>/index.html    immutable archive of one collection run
  de/snapshot/<date>/index.html
  data/repositories.csv         the repository table, for reuse
  data/data-dictionary.json     what every field means
  sitemap.xml, robots.txt, CITATION.cff

JavaScript is left with sorting, filtering and the charts. It never introduces a
figure the HTML does not already contain.

Input:  OUTPUT_DIR/data/latest.json, history.json, snapshots/*.json
Output: OUTPUT_DIR/
"""

from __future__ import annotations

import csv
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import yaml
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "build"))
SITE_URL = os.environ.get("SITE_URL", "https://netresearch.github.io/maint/")

LANGS = ("en", "de")

UTC_SUFFIX = "+00:00"
SCHEMA_TYPE = "@type"
INDEX_HTML = "index.html"
TWO_UP = "../../"

# Short pill labels, shared by both languages: these are technology names.
SHORT_LABELS = {
    "typo3-extension": "TYPO3",
    "skill": "Skill",
    "go-project": "Go",
    "commerce": "Commerce",
    "ansible": "Ansible",
    "tool": "Tool",
}

LIFETIME_KPIS = [
    "stars", "forks", "contributors", "external_contributors", "dependents_repos",
    "issues", "prs_merged", "releases", "commits", "packagist_downloads", "ghcr_downloads",
]
RECENT_KPIS = [
    "commits_30d", "issues_opened_30d", "prs_opened_30d", "prs_merged_30d",
    "releases_30d", "packagist_downloads_30d", "ghcr_downloads_30d",
]
CATEGORY_STATS = [
    "stars", "forks", "contributors", "external_contributors",
    "releases", "packagist_downloads", "ghcr_downloads", "dependents_repos",
]

# Weights behind the estimated downstream reach. Kept here and rendered on the
# page so the number is never presented without the arithmetic that made it.
REACH_WEIGHTS = [
    {"key": "external_contributors", "weight": 3},
    {"key": "issues", "weight": 1},
    {"key": "prs_merged", "weight": 1},
    {"key": "forks", "weight": 2},
    {"key": "dependents_repos", "weight": 2},
]

# How many archived snapshots to link from the dashboard. Older ones stay
# reachable at their URLs; only the list is trimmed.
SNAPSHOT_LINKS = 30

CONTACT_BASE = "https://www.netresearch.de/kontakt/"


def contact_url(position: str) -> str:
    return CONTACT_BASE + "?" + urlencode({
        "utm_source": "github-pages",
        "utm_medium": "referral",
        "utm_campaign": "impact-dashboard",
        "utm_content": position,
    })


def number(value, locale: str) -> str:
    """Locale-aware thousands grouping, rendered server-side."""
    if value is None:
        return "—"
    try:
        grouped = f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)
    return grouped.replace(",", ".") if locale.startswith("de") else grouped


def dash(value, locale: str) -> str:
    """A missing measurement renders as an em dash, never as zero."""
    return "—" if value is None else number(value, locale)


def format_date(iso: str | None, locale: str) -> str:
    if not iso:
        return "—"
    stamp = datetime.fromisoformat(iso.replace("Z", UTC_SUFFIX))
    if locale.startswith("de"):
        return stamp.strftime("%d.%m.%Y")
    return stamp.strftime("%d %B %Y")


def format_datetime(iso: str, locale: str) -> str:
    stamp = datetime.fromisoformat(iso.replace("Z", UTC_SUFFIX))
    if locale.startswith("de"):
        return stamp.strftime("%d.%m.%Y, %H:%M UTC")
    return stamp.strftime("%d %B %Y, %H:%M UTC")


def load_translations() -> dict[str, dict]:
    out = {}
    for lang in LANGS:
        with (DASHBOARD / "i18n" / f"{lang}.yaml").open(encoding="utf-8") as handle:
            out[lang] = yaml.safe_load(handle)
    return out


def reach_for(repo: dict) -> int:
    """Recompute from the snapshot so older data renders the same way."""
    existing = repo.get("estimated_downstream_reach")
    if isinstance(existing, int):
        return existing
    if isinstance(repo.get("blast_radius"), int):
        return repo["blast_radius"]
    life = repo.get("lifetime", {})
    return (
        life.get("external_contributors", 0) * 3
        + life.get("issues_open", 0)
        + life.get("issues_closed", 0)
        + life.get("prs_merged", 0)
        + life.get("forks", 0) * 2
        + life.get("dependents_repos", 0) * 2
    )


def pill_class(category: str) -> str:
    return {
        "typo3-extension": "typo3",
        "skill": "skill",
        "go-project": "go",
        "commerce": "commerce",
        "ansible": "ansible",
        "tool": "tool",
    }.get(category, "tool")


def active_repos(snapshot: dict) -> list[dict]:
    return [r for r in snapshot.get("repos", []) if not r.get("archived")]


def build_repo_rows(snapshot: dict, lang: str) -> list[dict]:
    rows = []
    for repo in active_repos(snapshot):
        life = repo.get("lifetime", {})
        traffic = repo.get("traffic_14d") or {}
        rows.append({
            "name": repo["name"],
            "category": repo.get("category", "tool"),
            "pill": pill_class(repo.get("category", "tool")),
            "pill_label": SHORT_LABELS.get(repo.get("category"), repo.get("category", "?")),
            # Both languages link relative to their own root, so the path is
            # the same either way.
            "detail_href": f"repo/{repo['name']}/",
            "stars": life.get("stars", 0),
            "forks": life.get("forks", 0),
            "contributors": life.get("contributors", 0),
            "external_contributors": life.get("external_contributors", 0),
            "issues_total": life.get("issues_open", 0) + life.get("issues_closed", 0),
            "prs_merged": life.get("prs_merged", 0),
            "releases": life.get("releases", 0),
            "commits_30d": (repo.get("recent_30d") or {}).get("commits", 0),
            "packagist_downloads": life.get("packagist_downloads", 0),
            "ghcr_downloads": life.get("ghcr_downloads", 0),
            "dependents_repos": life.get("dependents_repos", 0),
            "clones_14d": traffic.get("clones_total") if repo.get("traffic_14d") else None,
            "views_14d": traffic.get("views_total") if repo.get("traffic_14d") else None,
            "reach": reach_for(repo),
        })
    rows.sort(key=lambda r: r["stars"], reverse=True)
    return rows


def build_category_groups(snapshot: dict) -> list[dict]:
    meta = snapshot.get("categories", {})
    groups = []
    for key, info in meta.items():
        repos = [r for r in active_repos(snapshot) if r.get("category") == key]
        if not repos:
            continue
        groups.append({
            "key": key,
            "label": info.get("label", key) if isinstance(info, dict) else key,
            "pill": pill_class(key),
            "count": len(repos),
            "stats": [
                {"key": stat, "value": sum((r.get("lifetime") or {}).get(stat, 0) for r in repos)}
                for stat in CATEGORY_STATS
            ],
        })
    groups.sort(key=lambda g: g["count"], reverse=True)
    return groups


def build_history_rows(history: dict) -> list[dict]:
    rows = []
    for entry in (history.get("daily") or [])[-90:]:
        totals = entry.get("totals") or {}
        rows.append({
            "date": entry.get("date"),
            "stars": totals.get("stars", 0),
            "forks": totals.get("forks", 0),
            "contributors": totals.get("contributors", 0),
            "commits_30d": totals.get("commits_30d", 0),
            "prs_merged_30d": totals.get("prs_merged_30d", 0),
            "releases_30d": totals.get("releases_30d", 0),
        })
    return rows


def citation_text(t: dict, url: str, generated_at: str) -> str:
    """Cite the data date, not the build date.

    An "accessed today" stamp baked into static HTML would state the build date
    while looking like the reader's. The snapshot date is the fact that matters
    and it does not change, which is what makes a snapshot URL citable.
    """
    stamp = datetime.fromisoformat(generated_at.replace("Z", UTC_SUFFIX))
    return (
        f"{t['cite']['publisher']} ({stamp.year}). {t['hero']['title']} "
        f"[dataset]. {t['snapshot']['as_of']} {stamp.strftime('%Y-%m-%d')}. {url}"
    )


def json_ld_for(t: dict, snapshot: dict, canonical: str) -> str:
    organisation = {
        SCHEMA_TYPE: "Organization",
        "@id": f"{SITE_URL}#organization",
        "name": "Netresearch DTT GmbH",
        "url": "https://www.netresearch.de/",
    }
    dataset = {
        SCHEMA_TYPE: "Dataset",
        "@id": f"{SITE_URL}#dataset",
        "name": t["hero"]["title"],
        "description": t["meta"]["description"],
        "url": canonical,
        "inLanguage": t["html_lang"],
        "dateModified": snapshot["generated_at"],
        "creator": {"@id": f"{SITE_URL}#organization"},
        "publisher": {"@id": f"{SITE_URL}#organization"},
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "measurementTechnique": " ".join(t["provenance"]["sources"]),
        "isAccessibleForFree": True,
        "distribution": [
            {
                SCHEMA_TYPE: "DataDownload",
                "name": t["downloads"]["latest_json"],
                "encodingFormat": "application/json",
                "contentUrl": f"{SITE_URL}data/latest.json",
            },
            {
                SCHEMA_TYPE: "DataDownload",
                "name": t["downloads"]["repos_csv"],
                "encodingFormat": "text/csv",
                "contentUrl": f"{SITE_URL}data/repositories.csv",
            },
            {
                SCHEMA_TYPE: "DataDownload",
                "name": t["downloads"]["history_json"],
                "encodingFormat": "application/json",
                "contentUrl": f"{SITE_URL}data/history.json",
            },
        ],
    }
    breadcrumb = {
        SCHEMA_TYPE: "BreadcrumbList",
        "itemListElement": [
            {SCHEMA_TYPE: "ListItem", "position": 1, "name": t["hero"]["title"], "item": canonical},
        ],
    }
    graph = [organisation, dataset, breadcrumb]
    payload = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)
    # A literal "<" would let a value close the script element early.
    return payload.replace("<", "\\u003c")


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "name", "category", "stars", "forks", "contributors", "external_contributors",
        "issues_total", "prs_merged", "releases", "commits_30d", "packagist_downloads",
        "ghcr_downloads", "dependents_repos", "clones_14d", "views_14d", "reach",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


DICTIONARY = {
    "license": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "Netresearch DTT GmbH",
    "fields": {
        "stars": {"kind": "point-in-time", "meaning": "GitHub stargazers at collection time."},
        "forks": {"kind": "point-in-time", "meaning": "GitHub forks at collection time."},
        "contributors": {"kind": "cumulative", "meaning": "Distinct accounts with a merged commit."},
        "external_contributors": {"kind": "cumulative", "meaning": "Contributors who are not members of the organisation."},
        "issues_open": {"kind": "point-in-time", "meaning": "Open issues, pull requests excluded."},
        "issues_closed": {"kind": "cumulative", "meaning": "Closed issues, pull requests excluded."},
        "prs_merged": {"kind": "cumulative", "meaning": "Merged pull requests."},
        "releases": {"kind": "cumulative", "meaning": "Published releases."},
        "release_downloads": {"kind": "cumulative", "meaning": "Downloads of release assets."},
        "commits": {"kind": "cumulative", "meaning": "Commits on the default branch."},
        "packagist_downloads": {"kind": "cumulative", "meaning": "Total installs reported by Packagist."},
        "ghcr_downloads": {"kind": "cumulative", "meaning": "Container pulls reported by GHCR; best effort."},
        "dependents_repos": {"kind": "point-in-time", "meaning": "Public repositories GitHub's dependency graph reports as depending on this project. A lower bound."},
        "traffic_14d": {"kind": "rolling-14-day", "meaning": "Views and clones. Absent when no token with repository scope was available; absence is not zero."},
        "recent_30d": {"kind": "rolling-30-day", "meaning": "Rolling window ending at generation time. Not a calendar month."},
        "estimated_downstream_reach": {
            "kind": "derived-estimate",
            "meaning": "external_contributors*3 + issues_open + issues_closed + prs_merged + forks*2 + dependents_repos*2.",
            "caveat": "Weights are a judgement call, not a measurement. Use for ordering, not for decisions.",
        },
        "blast_radius": {
            "kind": "deprecated-alias",
            "meaning": "Former name of estimated_downstream_reach. Kept for one release cycle; use the new field.",
        },
    },
}


def main() -> None:
    data_dir = OUTPUT_DIR / "data"
    snapshot = json.loads((data_dir / "latest.json").read_text(encoding="utf-8"))
    history_path = data_dir / "history.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {"daily": []}

    translations = load_translations()
    env = Environment(# nosemgrep: python.flask.security.xss.audit.direct-use-of-jinja2.direct-use-of-jinja2
        loader=FileSystemLoader(DASHBOARD / "templates"),
        # Autoescape everything. An extension allow-list would miss ".html.j2",
        # and every deliberate raw-HTML injection is marked |safe at its use site.
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["number"] = number
    env.filters["dash"] = dash

    totals = snapshot.get("totals", {})
    repos = snapshot.get("repos", [])
    archived = [r for r in repos if r.get("archived")]
    active = active_repos(snapshot)
    history_rows = build_history_rows(history)
    category_groups_cache = {}

    snapshot_dates = sorted(
        (p.stem for p in (data_dir / "snapshots").glob("*.json")), reverse=True
    ) if (data_dir / "snapshots").exists() else []

    for lang in LANGS:
        t = translations[lang]
        locale = t["locale"]
        prefix = "" if lang == "en" else "de/"
        home_url = f"{SITE_URL}{prefix}"

        alternates = [
            {"hreflang": "en", "href": SITE_URL},
            {"hreflang": "de", "href": f"{SITE_URL}de/"},
            {"hreflang": "x-default", "href": SITE_URL},
        ]

        common = {
            "t": t,
            "locale": locale,
            "snapshot": snapshot,
            "site_url": SITE_URL,
            "current_year": datetime.now(timezone.utc).year,
            "generated_display": format_datetime(snapshot["generated_at"], locale),
            "active_repos": len(active),
            "archived_repos": len(archived),
            "contact": {
                "hero": contact_url("hero"),
                "footer": contact_url("footer"),
                "repo": contact_url("repo"),
            },
        }

        category_groups = build_category_groups(snapshot)
        category_groups_cache[lang] = category_groups
        repo_rows = build_repo_rows(snapshot, lang)

        # ── Dashboard ────────────────────────────────────────────────────────
        write(
            OUTPUT_DIR / prefix / INDEX_HTML,
            env.get_template("index.html.j2").render(
                **common,
                page_title=t["meta"]["title"],
                page_description=t["meta"]["description"],
                canonical=home_url,
                alternates=alternates,
                base="" if lang == "en" else "../",
                home="./",
                other_href=SITE_URL if lang == "de" else f"{SITE_URL}de/",
                json_ld=json_ld_for(t, snapshot, home_url),
                kpis={
                    "lifetime": [(k, totals.get(k)) for k in LIFETIME_KPIS],
                    "recent_30d": [(k, totals.get(k)) for k in RECENT_KPIS],
                },
                reach_components=REACH_WEIGHTS,
                category_groups=category_groups,
                history_rows=history_rows,
                history_from=history_rows[0]["date"] if history_rows else None,
                history_from_display=format_date(history_rows[0]["date"], locale) if history_rows else None,
                repo_rows=repo_rows,
                snapshot_links=[
                    {"date": date, "href": f"snapshot/{date}/"}
                    for date in snapshot_dates[:SNAPSHOT_LINKS]
                ],
                citation=citation_text(t, home_url, snapshot["generated_at"]),
            ),
        )

        # ── Repository detail pages ──────────────────────────────────────────
        for repo in active:
            life = repo.get("lifetime", {})
            recent = repo.get("recent_30d", {})
            repo_url = f"{home_url}repo/{repo['name']}/"
            repo_view = {**repo, "estimated_downstream_reach": reach_for(repo)}
            write(
                OUTPUT_DIR / prefix / "repo" / repo["name"] / INDEX_HTML,
                env.get_template("repo.html.j2").render(
                    **common,
                    page_title=f"{repo['name']} {t['repo']['heading_suffix']} — {t['hero']['title']}",
                    page_description=repo.get("description") or t["meta"]["description"],
                    canonical=repo_url,
                    alternates=[
                        {"hreflang": "en", "href": f"{SITE_URL}repo/{repo['name']}/"},
                        {"hreflang": "de", "href": f"{SITE_URL}de/repo/{repo['name']}/"},
                        {"hreflang": "x-default", "href": f"{SITE_URL}repo/{repo['name']}/"},
                    ],
                    base=TWO_UP if lang == "en" else TWO_UP + "../",
                    # Two levels up is the language root in both languages.
                    home=TWO_UP,
                    other_href=(
                        f"{SITE_URL}repo/{repo['name']}/" if lang == "de"
                        else f"{SITE_URL}de/repo/{repo['name']}/"
                    ),
                    json_ld=json_ld_for(t, snapshot, repo_url),
                    repo=repo_view,
                    pill=pill_class(repo.get("category", "tool")),
                    pill_label=SHORT_LABELS.get(repo.get("category"), repo.get("category", "?")),
                    product_page=repo.get("homepage") or None,
                    release_published=format_date(
                        (repo.get("latest_release") or {}).get("published_at"), locale
                    ) if repo.get("latest_release") else None,
                    lifetime_kpis=[
                        (k, life.get(k, 0)) for k in
                        ["stars", "forks", "contributors", "external_contributors",
                         "prs_merged", "releases", "commits", "packagist_downloads",
                         "ghcr_downloads", "dependents_repos"]
                    ],
                    recent_kpis=[
                        ("commits_30d", recent.get("commits", 0)),
                        ("issues_opened_30d", recent.get("issues_opened", 0)),
                        ("prs_opened_30d", recent.get("prs_opened", 0)),
                        ("prs_merged_30d", recent.get("prs_merged", 0)),
                        ("releases_30d", recent.get("releases", 0)),
                        ("packagist_downloads_30d", recent.get("packagist_downloads", 0)),
                        ("ghcr_downloads_30d", recent.get("ghcr_downloads", 0)),
                    ],
                ),
            )

        # ── Immutable snapshot pages ─────────────────────────────────────────
        for date in snapshot_dates:
            target = OUTPUT_DIR / prefix / "snapshot" / date / INDEX_HTML
            if target.exists():
                # Written once. Regenerating would break the promise that a cited
                # snapshot URL still shows the figures it showed when cited.
                continue
            archived_snapshot = json.loads(
                (data_dir / "snapshots" / f"{date}.json").read_text(encoding="utf-8")
            )
            archived_totals = archived_snapshot.get("totals", {})
            snapshot_url = f"{home_url}snapshot/{date}/"
            write(
                target,
                env.get_template("snapshot.html.j2").render(
                    **{**common, "snapshot": archived_snapshot,
                       "generated_display": format_datetime(archived_snapshot["generated_at"], locale),
                       "active_repos": len(active_repos(archived_snapshot))},
                    page_title=f"{t['hero']['title']} · {date}",
                    page_description=t["meta"]["description"],
                    canonical=snapshot_url,
                    alternates=[
                        {"hreflang": "en", "href": f"{SITE_URL}snapshot/{date}/"},
                        {"hreflang": "de", "href": f"{SITE_URL}de/snapshot/{date}/"},
                        {"hreflang": "x-default", "href": f"{SITE_URL}snapshot/{date}/"},
                    ],
                    base=TWO_UP if lang == "en" else TWO_UP + "../",
                    # Two levels up is the language root in both languages.
                    home=TWO_UP,
                    other_href=(
                        f"{SITE_URL}snapshot/{date}/" if lang == "de"
                        else f"{SITE_URL}de/snapshot/{date}/"
                    ),
                    json_ld=json_ld_for(t, archived_snapshot, snapshot_url),
                    snapshot_date=date,
                    kpis={
                        "lifetime": [(k, archived_totals.get(k)) for k in LIFETIME_KPIS],
                        "recent_30d": [(k, archived_totals.get(k)) for k in RECENT_KPIS],
                    },
                    citation=citation_text(t, snapshot_url, archived_snapshot["generated_at"]),
                ),
            )

    # ── Data products ────────────────────────────────────────────────────────
    write_csv(build_repo_rows(snapshot, "en"), data_dir / "repositories.csv")
    write(
        data_dir / "data-dictionary.json",
        json.dumps({**DICTIONARY, "generated_at": snapshot["generated_at"]}, indent=2, ensure_ascii=False) + "\n",
    )

    # ── Assets ───────────────────────────────────────────────────────────────
    assets_src = DASHBOARD / "assets"
    assets_dst = OUTPUT_DIR / "assets"
    if assets_dst.exists():
        shutil.rmtree(assets_dst)
    shutil.copytree(assets_src, assets_dst)

    citation_cff = ROOT / "CITATION.cff"
    if citation_cff.exists():
        shutil.copy(citation_cff, OUTPUT_DIR / "CITATION.cff")

    # ── Crawling ─────────────────────────────────────────────────────────────
    urls = [SITE_URL, f"{SITE_URL}de/"]
    for repo in active:
        urls.append(f"{SITE_URL}repo/{repo['name']}/")
        urls.append(f"{SITE_URL}de/repo/{repo['name']}/")
    for date in snapshot_dates[:SNAPSHOT_LINKS]:
        urls.append(f"{SITE_URL}snapshot/{date}/")
        urls.append(f"{SITE_URL}de/snapshot/{date}/")

    lastmod = snapshot["generated_at"][:10]
    entries = "\n".join(
        f"  <url><loc>{url}</loc><lastmod>{lastmod}</lastmod></url>" for url in urls
    )
    write(
        OUTPUT_DIR / "sitemap.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}\n</urlset>\n",
    )
    write(
        OUTPUT_DIR / "robots.txt",
        "User-agent: *\nAllow: /\n\n"
        "User-agent: Googlebot\nAllow: /\n\n"
        "User-agent: Bingbot\nAllow: /\n\n"
        "User-agent: OAI-SearchBot\nAllow: /\n\n"
        f"Sitemap: {SITE_URL}sitemap.xml\n",
    )

    print(
        f"Rendered {len(active)} repository pages x {len(LANGS)} languages, "
        f"{len(snapshot_dates)} snapshots, {len(history_rows)} history rows"
    )


if __name__ == "__main__":
    main()
