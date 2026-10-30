"""Shape of the daily Matrix digest built by scripts/check-stars.py."""

from test_check_stars import check_stars

REPO = "[ofelia](https://github.com/netresearch/ofelia)"


def _event(kind, line, count=1):
    return {"kind": kind, "count": count, "line": line}


def test_summary_counts_stars_forks_dependents_and_leaves_watchers_to_the_details():
    events = [
        _event("star", f"⭐ {REPO} starred by [alice](https://github.com/alice) (3 👥)"),
        _event("star", f"⭐ {REPO} gained 2 new stars (stargazer identities unavailable to this token)", 2),
        _event("fork", f"🍴 {REPO} forked by [bob](https://github.com/bob) (0 👥) → "
                       "[bob/ofelia](https://github.com/bob/ofelia)"),
        _event("watcher", f"👀 {REPO} watched by [carol](https://github.com/carol) (1 👥)"),
    ]
    text, html_body = check_stars.build_digest(events, "2026-10-30")
    lines = text.splitlines()

    assert lines[0] == (f"📊 {check_stars.ORG_NAME} 2026-10-30: ⭐ 3 stars · 🍴 1 fork · "
                        f"📦 0 dependents ([?]({check_stars.HELP_URL}))")
    assert lines[1:] == [e["line"] for e in events]
    assert html_body.startswith(
        f"<details><summary>📊 {check_stars.ORG_NAME} 2026-10-30: ⭐ 3 stars · 🍴 1 fork · "
        f'📦 0 dependents (<a href="{check_stars.HELP_URL}">?</a>)</summary>')
    assert html_body.endswith("</details>")
    assert html_body.count("<br>") == len(events) - 1
    assert "watched by" in html_body


def test_event_lines_become_links_and_profile_text_is_escaped():
    line = f"⭐ {REPO} starred by [mallory](https://github.com/mallory) (<b>Evil</b> & Co, 0 👥)"
    _, html_body = check_stars.build_digest([_event("star", line)], "2026-10-30")

    assert '<a href="https://github.com/netresearch/ofelia">ofelia</a>' in html_body
    assert '<a href="https://github.com/mallory">mallory</a>' in html_body
    assert "&lt;b&gt;Evil&lt;/b&gt; &amp; Co" in html_body
    assert "<b>" not in html_body


def test_markdown_link_with_a_query_string_keeps_its_href_valid():
    rendered = check_stars.markdown_links_to_html("[log](https://example.org/run?a=1&b=2)")
    assert rendered == '<a href="https://example.org/run?a=1&amp;b=2">log</a>'
