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


def test_empty_repo_returns_no_dependents_without_scraping(monkeypatch=None):
    """An empty repo must yield [] (zero dependents) and never hit the network."""
    def _fail(*args, **kwargs):
        raise AssertionError("get_dependents scraped an empty repo instead of short-circuiting")

    # Fail loudly if any HTTP call is attempted for an empty repo.
    check_stars.requests.get = _fail  # type: ignore[assignment]
    try:
        result = check_stars.get_dependents("netresearch/dind", repo_is_empty=True)
    finally:
        # Restore is not required across the tiny standalone run, but keep it tidy.
        pass
    assert result == [], f"expected [] for empty repo, got {result!r}"


if __name__ == "__main__":
    test_empty_repo_returns_no_dependents_without_scraping()
    print("OK: empty repo returns [] without scraping")
