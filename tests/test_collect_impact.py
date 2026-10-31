"""fetch_dependents() reports the largest count among the packages GitHub lists."""

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
                   f'class="select-menu-item" aria-checked="{str(p == package).lower()}">{p}</a>'
                   for p in ("P1", "P2"))
    return (f"{menu}"
            f'<a class="btn-link" href="?dependent_type=REPOSITORY{suffix}"><svg></svg> {repositories} Repositories</a>'
            f'<a class="btn-link" href="?dependent_type=PACKAGE{suffix}"><svg></svg> {packages} Packages</a>')


def test_the_largest_package_count_is_reported(monkeypatch):
    # GitHub opened the page on P1; the dependents sit under P2.
    pages = {
        BASE: counts("P1", 3, 90, query=False),
        f"{BASE}?package_id=P2": counts("P2", "1,063", 82),
    }
    requested = []

    def scrape(url):
        requested.append(url)
        return pages.get(url)

    monkeypatch.setattr(collect_impact, "scrape_html", scrape)
    result = collect_impact.fetch_dependents("netresearch/example")
    assert (result["repositories"], result["packages"]) == (1063, 90)
    assert requested == [BASE, f"{BASE}?package_id=P2"], "P1 is not fetched a second time"


def test_a_failed_package_page_reports_no_count(monkeypatch):
    pages = {BASE: counts("P1", 5, 0, query=False)}
    monkeypatch.setattr(collect_impact, "scrape_html", pages.get)
    assert collect_impact.fetch_dependents("netresearch/example") is None


def test_scrape_waits_as_long_as_a_429_asks(monkeypatch):
    import requests

    answers = [(429, {"Retry-After": "120"}, ""), (200, {}, "page")]
    waits = []

    def get(url, **_kwargs):
        status, headers, body = answers.pop(0)
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response._content = body.encode()
        response.url = url
        return response

    monkeypatch.setattr(collect_impact.requests, "get", get)
    monkeypatch.setattr(collect_impact.time, "sleep", waits.append)
    monkeypatch.setattr(collect_impact, "SCRAPE_INTERVAL", 0)
    assert collect_impact.scrape_html(BASE) == "page"
    assert waits == [120]
