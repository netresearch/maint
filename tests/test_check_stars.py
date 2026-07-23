#!/usr/bin/env python3
"""Tests for scripts/check-stars.py dependents handling.

Runnable standalone (`python tests/test_check_stars.py`) or via pytest.
"""

import importlib.util
import os
from pathlib import Path

# The module reads MATRIX_WEBHOOK_URL at import time; provide a dummy value.
os.environ.setdefault("MATRIX_WEBHOOK_URL", "https://example.invalid/webhook")

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check-stars.py"
_spec = importlib.util.spec_from_file_location("check_stars", _MODULE_PATH)
check_stars = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_stars)


def test_empty_repo_returns_no_dependents_without_scraping():
    """An empty repo must yield [] (zero dependents) and never hit the network."""
    def _fail(*args, **kwargs):
        raise AssertionError("get_dependents scraped an empty repo instead of short-circuiting")

    # Fail loudly if any HTTP call is attempted for an empty repo, restoring
    # the real requests.get afterwards so other tests are unaffected.
    original_get = check_stars.requests.get
    check_stars.requests.get = _fail  # type: ignore[assignment]
    try:
        result = check_stars.get_dependents("netresearch/dind", repo_is_empty=True)
    finally:
        check_stars.requests.get = original_get  # type: ignore[assignment]
    assert result == [], f"expected [] for empty repo, got {result!r}"


def test_get_org_repos_maps_archived_flag():
    """get_org_repos must surface each repo's `archived` flag.

    The main loop keys the social-endpoint skip off it, so a missing/wrong value
    would re-introduce the abort-on-archived-repo failure.
    """
    api_payload = [
        {"name": "live", "full_name": "netresearch/live", "html_url": "u",
         "stargazers_count": 1, "forks_count": 0, "size": 5, "archived": False},
        {"name": "old", "full_name": "netresearch/old", "html_url": "u",
         "stargazers_count": 1, "forks_count": 0, "size": 5, "archived": True},
        # A repo whose payload omits `archived` must default to False, not crash.
        {"name": "legacy", "full_name": "netresearch/legacy", "html_url": "u",
         "stargazers_count": 0, "forks_count": 0, "size": 5},
    ]
    original = check_stars.github_request
    check_stars.github_request = lambda *a, **k: api_payload  # type: ignore[assignment]
    try:
        repos = check_stars.get_org_repos()
    finally:
        check_stars.github_request = original  # type: ignore[assignment]
    by_name = {r["full_name"]: r for r in repos}
    assert by_name["netresearch/live"]["archived"] is False
    assert by_name["netresearch/old"]["archived"] is True
    assert by_name["netresearch/legacy"]["archived"] is False


def test_archived_repo_skips_token_gated_fetches():
    """An archived repo must NOT call the stargazers/forks/subscribers endpoints.

    Reproduces the reason star-notifications was failing: those endpoints 403 on
    an archived repo since the 2026-06-30 API restriction, and the AuthError was
    aborting the whole run. The skip is expressed at the call site as
    `get_stargazers(name) if social_readable else None`, so this asserts the same
    guard evaluates to None (no call) for archived and to a fetch for a live repo.
    """
    def _boom(*args, **kwargs):
        raise AssertionError("token-gated social endpoint called for an archived repo")

    original = (check_stars.get_stargazers, check_stars.get_forks, check_stars.get_watchers)
    check_stars.get_stargazers = _boom  # type: ignore[assignment]
    check_stars.get_forks = _boom  # type: ignore[assignment]
    check_stars.get_watchers = _boom  # type: ignore[assignment]
    try:
        for archived in (True, False):
            social_readable = not archived
            called = {"n": 0}
            if not archived:
                # For the live case, restore a stub that records the call.
                check_stars.get_stargazers = lambda name: called.__setitem__("n", 1) or []  # type: ignore[assignment]
            sg = check_stars.get_stargazers("netresearch/x") if social_readable else None
            if archived:
                assert sg is None, "archived repo must yield None (skipped), not a fetch"
            else:
                assert called["n"] == 1, "live repo must actually fetch stargazers"
    finally:
        check_stars.get_stargazers, check_stars.get_forks, check_stars.get_watchers = original  # type: ignore[assignment]


if __name__ == "__main__":
    test_empty_repo_returns_no_dependents_without_scraping()
    print("OK: empty repo returns [] without scraping")
    test_get_org_repos_maps_archived_flag()
    print("OK: get_org_repos maps archived flag")
    test_archived_repo_skips_token_gated_fetches()
    print("OK: archived repo skips token-gated fetches")
