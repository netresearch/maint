"""get_dependents() reads every package and every page of /network/dependents."""

from itertools import pairwise

import pytest
import requests
from test_check_stars import check_stars

BASE = "https://github.com/netresearch/example/network/dependents"
OLD = "UGFja2FnZS0x"
NEW = "UGFja2FnZS0y"


def page(names, *, selected=OLD, next_after=None):
    menu = "".join(
        f'<a href="/netresearch/example/network/dependents?package_id={package}" '
        f'class="select-menu-item" role="menuitemradio" aria-checked="{str(package == selected).lower()}">'
        f'<span class="select-menu-item-text">{package}</span></a>'
        for package in (OLD, NEW))
    rows = "".join('<div class="Box-row"><a data-hovercard-type="repository" '
                   f'href="/{name}">{name}</a></div>' for name in names)
    pagination = ""
    if next_after:
        pagination = ('<div class="paginate-container"><button disabled>Previous</button>'
                      f'<a href="{BASE}?dependents_after={next_after}&amp;package_id={selected}">Next</a></div>')
    return f'<div class="select-menu-list">{menu}</div><div id="dependents">{rows}{pagination}</div>'


@pytest.fixture
def site(monkeypatch):
    pages = {}
    requested = []

    clock = [1000.0]

    def get(url, **_kwargs):
        requested.append((url, clock[0]))
        response = requests.Response()
        response.status_code = 200 if url in pages else 500
        response._content = pages.get(url, "").encode()
        response.url = url
        return response

    monkeypatch.setattr(check_stars._html_session, "get", get)
    monkeypatch.setattr(check_stars, "_last_dependents_request", float("-inf"))
    monkeypatch.setattr(check_stars.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(check_stars.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    return pages, requested, clock


def test_every_package_and_page_is_read(site):
    pages, requested, _ = site
    # GitHub opens on the old module path, which has no dependents.
    pages[BASE] = page([])
    pages[f"{BASE}?package_id={NEW}"] = page(["consumer/a", "consumer/b"], selected=NEW, next_after="X")
    pages[f"{BASE}?dependents_after=X&package_id={NEW}"] = page(["consumer/b", "consumer/c"], selected=NEW)
    names = [d["full_name"] for d in check_stars.get_dependents("netresearch/example")]
    assert names == ["consumer/a", "consumer/b", "consumer/c"]
    assert len(requested) == 3, "the selected package is not fetched a second time"


def test_one_failed_page_fails_the_whole_read(site):
    pages, _, _ = site
    pages[BASE] = page(["consumer/a"], next_after="X")
    pages[f"{BASE}?package_id={NEW}"] = page(["consumer/b"], selected=NEW)
    # The second page of the opened package is missing.
    assert check_stars.get_dependents("netresearch/example") is None


def test_a_repo_past_the_page_cap_is_not_read_in_part(site, monkeypatch):
    pages, _, _ = site
    monkeypatch.setattr(check_stars, "MAX_DEPENDENTS_PAGES", 2)
    pages[BASE] = page(["consumer/a"], next_after="X")
    pages[f"{BASE}?dependents_after=X&package_id={OLD}"] = page(["consumer/b"], next_after="Y")
    assert check_stars.get_dependents("netresearch/example") is None


def test_pages_are_requested_at_most_one_per_interval(site):
    pages, requested, _ = site
    pages[BASE] = page(["consumer/a"], next_after="X")
    pages[f"{BASE}?dependents_after=X&package_id={OLD}"] = page(["consumer/b"])
    pages[f"{BASE}?package_id={NEW}"] = page([], selected=NEW)
    check_stars.get_dependents("netresearch/example")
    times = [when for _, when in requested]
    assert len(times) == 3
    assert all(later - earlier >= 2.0
               for earlier, later in pairwise(times)), "every request waits for the previous one"
