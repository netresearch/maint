"""fetch_dependents() adds up the counts of every package GitHub lists."""

import os
import sys
from pathlib import Path

# The module reads GITHUB_TOKEN at import time; provide a dummy value.
os.environ.setdefault("GITHUB_TOKEN", "synthetic-test-token")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import collect_impact

BASE = "https://github.com/netresearch/example/network/dependents"


def counts(package, repositories, packages, *, query=True):
    suffix = f"&amp;package_id={package}" if query else ""
    menu = "".join(f'<a href="/netresearch/example/network/dependents?package_id={p}" '
                   f'class="select-menu-item">{p}</a>' for p in ("P1", "P2"))
    return (f"{menu}"
            f'<a class="btn-link" href="?dependent_type=REPOSITORY{suffix}"><svg></svg> {repositories} Repositories</a>'
            f'<a class="btn-link" href="?dependent_type=PACKAGE{suffix}"><svg></svg> {packages} Packages</a>')


def test_counts_of_all_packages_are_added_up(monkeypatch):
    pages = {
        BASE: counts("P1", 0, 0, query=False),
        f"{BASE}?package_id=P1": counts("P1", 0, 0),
        f"{BASE}?package_id=P2": counts("P2", "1,063", 82),
    }
    monkeypatch.setattr(collect_impact, "scrape_html", pages.get)
    result = collect_impact.fetch_dependents("netresearch/example")
    assert (result["repositories"], result["packages"]) == (1063, 82)


def test_a_failed_package_page_reports_no_count(monkeypatch):
    pages = {BASE: counts("P1", 5, 0, query=False), f"{BASE}?package_id=P1": counts("P1", 5, 0)}
    monkeypatch.setattr(collect_impact, "scrape_html", pages.get)
    assert collect_impact.fetch_dependents("netresearch/example") is None
