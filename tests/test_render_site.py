"""Published citation paths must remain useful after the next daily run."""
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest
from bs4 import BeautifulSoup


def test_shared_renderers_produce_identical_bytes_in_ascii_locale():
    program = """
import json, sys
sys.path.insert(0, sys.argv[1])
from site_shell import render_header, render_page_navigation
output = []
for lang in ('de', 'en'):
    output.append(render_header({'lang': lang, 'assetBase': '/assets/site-shell',
        'languageUrls': {'de': '/de/', 'en': '/'}, 'active': 'impact',
        'contactHref': 'https://www.netresearch.de/kontakt/'}))
    output.append(render_page_navigation({'lang': lang, 'title': 'Open Source Impact',
        'links': [{'href': '#main', 'label': 'Start'}]}))
json.dump(output, sys.stdout, ensure_ascii=False)
"""
    shell = Path(__file__).resolve().parents[1] / "shared/site-shell"
    command = [sys.executable, "-c", program, str(shell)]
    normal_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    ascii_env = {**normal_env, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    normal = subprocess.run(command, env=normal_env, capture_output=True)
    ascii_locale = subprocess.run(command, env=ascii_env, capture_output=True)
    assert normal.returncode == 0, normal.stderr.decode("utf-8", errors="replace")
    assert ascii_locale.returncode == 0, ascii_locale.stderr.decode("utf-8", errors="replace")
    assert normal.stdout == ascii_locale.stdout
    assert len(json.loads(normal.stdout)) == 4


@pytest.fixture
def rendered(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "scripts" / "render_site.py"
    spec = importlib.util.spec_from_file_location("render_site", source)
    renderer = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(source.parent))
    spec.loader.exec_module(renderer)
    monkeypatch.setattr(renderer, "OUTPUT_DIR", tmp_path)
    snapshot = {"generated_at": "2026-10-24T03:16:06Z", "repos": [], "totals": {"stars": 1}}
    data = tmp_path / "data"
    data.mkdir()
    (data / "latest.json").write_text(json.dumps(snapshot))
    renderer.main()
    return tmp_path, renderer


def test_every_page_has_the_shared_header_and_local_navigation(rendered):
    root, _ = rendered
    for page in root.rglob("index.html"):
        soup = BeautifulSoup(page.read_text(), "html.parser")
        header = soup.select_one("header[data-nr-shell]")
        assert header is not None, page
        assert len(header.select("img")) == 1
        assert "assets/site-shell/" in header.select_one("img")["src"]
        assert header.select_one("details[data-nr-mobile-menu]")
        assert {radio["value"] for radio in header.select('input[name="nr-shell-theme"]')} == {"system", "light", "dark"}
        assert soup.select_one("[data-nr-page-navigation]")
        lang = soup.html["lang"]
        assert header.select_one(f'a[hreflang="{lang}"]')
        assert header.select_one(f'a[hreflang="{"de" if lang == "en" else "en"}"]')
    for name in ("site-shell.css", "site-shell.js", "netresearch-symbol-only.svg"):
        assert (root / "assets/site-shell" / name).read_bytes() == (Path(__file__).resolve().parents[1] / "shared/site-shell" / name).read_bytes()


@pytest.mark.parametrize("prefix", ["", "de/"])
def test_historical_header_migrates_without_touching_archived_content(rendered, prefix):
    root, renderer = rendered
    target = root / prefix / "snapshot/2026-10-24/index.html"
    original = target.read_text()
    old_header = '<header class="site-header"><a href="https://www.netresearch.de/"><img src="../../assets/netresearch.svg" alt="Netresearch"></a><nav>Old header</nav></header>'
    legacy, replaced = re.subn(r'<!-- site-shell:start -->[\s\S]*?<!-- site-shell:end -->', old_header, original, count=1)
    if not replaced:
        legacy = re.sub(r'<header\b[\s\S]*?</header>', old_header, original, count=1)
    target.write_text(legacy)
    before = BeautifulSoup(legacy, "html.parser")
    renderer.main()
    migrated = target.read_text()
    after = BeautifulSoup(migrated, "html.parser")
    assert after.select_one("header[data-nr-shell]")
    assert "Old header" not in migrated
    assert str(after.main) == str(before.main)
    assert re.search(r'<main\b[\s\S]*?</main>', migrated).group() == re.search(r'<main\b[\s\S]*?</main>', legacy).group()
    assert [str(node) for node in after.select('script[type="application/ld+json"]')] == [str(node) for node in before.select('script[type="application/ld+json"]')]
    assert after.select_one('link[rel="canonical"]') == before.select_one('link[rel="canonical"]')
    renderer.main()
    assert target.read_text() == migrated


def test_header_migration_also_covers_html_without_surviving_json(rendered):
    root, renderer = rendered
    original = (root / "snapshot/2026-10-24/index.html").read_text()
    legacy = re.sub(r'<!-- site-shell:start -->[\s\S]*?<!-- site-shell:end -->', '<header class="site-header">Legacy chrome</header>', original)
    target = root / "snapshot/2020-01-01/index.html"
    target.parent.mkdir()
    target.write_text(legacy)
    renderer.main()
    migrated = target.read_text()
    assert 'data-nr-shell' in migrated
    assert 'Legacy chrome' not in migrated
    assert re.search(r'<main\b[\s\S]*?</main>', migrated).group() == re.search(r'<main\b[\s\S]*?</main>', legacy).group()
    # A surviving single-language archive cannot create a translation from lost JSON.
    soup = BeautifulSoup(migrated, "html.parser")
    assert soup.select_one('header a[hreflang="de"]')["href"] == f"{renderer.SITE_URL}de/"


@pytest.mark.parametrize("locale,prefix", [("en", ""), ("de", "de/")])
def test_copy_button_has_translated_action(rendered, locale, prefix):
    root, renderer = rendered
    soup = BeautifulSoup((root / prefix / "index.html").read_text(), "html.parser")
    expected = renderer.load_translations()[locale]["cite"]["copy"]
    assert soup.select_one("#copy-citation").get_text(strip=True) == expected
    assert "built-in method" not in str(soup)


@pytest.mark.parametrize("prefix", ["", "de/"])
def test_citation_and_dataset_resolve_to_existing_immutable_snapshot(rendered, prefix):
    root, renderer = rendered
    soup = BeautifulSoup((root / prefix / "index.html").read_text(), "html.parser")
    key = renderer.archive_key(json.loads((root / "data/latest.json").read_text()))
    archive = f"https://netresearch.github.io/maint/{prefix}snapshot/{key}/"
    assert archive in soup.select_one("#citation-text").get_text()
    assert (root / prefix / f"snapshot/{key}/index.html").is_file()
    archived = BeautifulSoup((root / prefix / f"snapshot/{key}/index.html").read_text(), "html.parser")
    assert archived.select_one("#copy-citation").get_text(strip=True) == renderer.load_translations()["de" if prefix else "en"]["cite"]["copy"]
    graph = json.loads(soup.select_one('script[type="application/ld+json"]').string)
    dataset = next(node for node in graph["@graph"] if node["@type"] == "Dataset")
    assert dataset["url"] == archive
    assert dataset["distribution"][0]["contentUrl"].endswith(f"/data/snapshots/{key}.json")
    assert (root / f"data/snapshots/{key}.json").is_file()


def test_two_runs_on_same_day_preserve_cited_html_and_json(rendered):
    root, renderer = rendered
    first = json.loads((root / "data/latest.json").read_text())
    first_key = renderer.archive_key(first)
    first_html = (root / f"snapshot/{first_key}/index.html").read_bytes()
    second = {**first, "generated_at": "2026-10-24T15:00:00Z", "totals": {"stars": 2}}
    (root / "data/latest.json").write_text(json.dumps(second))
    renderer.main()
    second_key = renderer.archive_key(second)
    assert second_key != first_key
    assert json.loads((root / f"data/snapshots/{first_key}.json").read_text()) == first
    assert json.loads((root / "data/snapshots/2026-10-24.json").read_text()) == first
    assert (root / f"snapshot/{first_key}/index.html").read_bytes() == first_html
    assert json.loads((root / f"data/snapshots/{second_key}.json").read_text()) == second
    assert f"/snapshot/{second_key}/" in (root / "index.html").read_text()
    assert (root / f"snapshot/{second_key}/index.html").is_file()


def test_legacy_metadata_migration_preserves_figures_and_collection_date(rendered):
    root, renderer = rendered
    legacy_url = "https://netresearch.github.io/maint/snapshot/2026-10-24/"
    soup = BeautifulSoup((root / "snapshot/2026-10-24/index.html").read_text(), "html.parser")
    script = soup.select_one('script[type="application/ld+json"]')
    payload = json.loads(script.string)
    dataset = next(node for node in payload["@graph"] if node["@type"] == "Dataset")
    dataset["url"] = legacy_url
    dataset["distribution"] = [{"@type": "DataDownload", "contentUrl": "https://netresearch.github.io/maint/data/latest.json"}]
    original_date = dataset["dateModified"]
    script.string = json.dumps(payload)
    soup.select_one("#copy-citation").string = "<built-in method copy of dict object>"
    figures = [str(node) for node in soup.select("[data-figure]")]
    assert figures
    migrated = BeautifulSoup(renderer.migrate_legacy_snapshot_html(str(soup), renderer.load_translations()["en"]), "html.parser")
    actual = next(node for node in json.loads(migrated.select_one('script[type="application/ld+json"]').string)["@graph"] if node["@type"] == "Dataset")
    assert actual["dateModified"] == original_date
    assert actual["distribution"] == [{"@type": "DataDownload", "name": renderer.load_translations()["en"]["hero"]["title"], "encodingFormat": "text/html", "contentUrl": legacy_url}]
    assert [str(node) for node in migrated.select("[data-figure]")] == figures
    assert migrated.select_one("#copy-citation").get_text() == renderer.load_translations()["en"]["cite"]["copy"]
    assert not any(link.get("href", "").endswith("data/snapshots/2026-10-24.json") for link in migrated.select("a"))
    download = migrated.select_one("a[download]")
    assert download["href"] == legacy_url
    assert download.get_text() == "HTML archive"


@pytest.mark.parametrize("locale", ["en", "de"])
def test_legacy_actions_are_selective_and_idempotent(rendered, locale):
    _, renderer = rendered
    translations = renderer.load_translations()[locale]
    metadata = {"@type": "Dataset", "url": "https://example.org/archive/?a=1&b=2", "dateModified": "2020-01-01", "distribution": {"contentUrl": "https://example.org/data/repositories.csv"}}
    markup = '<link href="https://example.org/archive/?a=1&amp;b=2" rel="canonical">\n'
    markup += '<script data-extra="value"\n type="application/ld+json">' + json.dumps(metadata) + '</script>\n'
    unchanged = '<script type="application/json">{"keep": "<script>"}</script><p data-figure="stars">42 &amp; 17</p>'
    markup += unchanged
    markup += '<a class="btn" href="../../data/snapshots/2020-01-01.json">JSON</a>'
    markup += '<a href="../../data/snapshots/2020-01-01-abcdef123456.json">Hashed JSON</a>'
    markup += '<button data-copied="OK" id="copy-citation">broken</button>'
    migrated = renderer.migrate_legacy_snapshot_html(markup, translations)
    assert unchanged in migrated
    assert '<a href="../../data/snapshots/2020-01-01-abcdef123456.json">Hashed JSON</a>' in migrated
    soup = BeautifulSoup(migrated, "html.parser")
    assert soup.select_one('a[download]')["href"] == "https://example.org/archive/?a=1&b=2"
    assert soup.select_one('a[download]')["class"] == ["btn"]
    assert soup.select_one('#copy-citation').get_text() == translations["cite"]["copy"]
    assert renderer.migrate_legacy_snapshot_html(migrated, translations) == migrated


def test_collector_preserves_daily_archive_before_rendering(rendered):
    root, renderer = rendered
    data = root / "data"
    first = json.loads((data / "latest.json").read_text())
    daily = data / "snapshots/2026-10-24.json"
    first_bytes = daily.read_bytes()
    # The collector writes before the renderer: preservation must hold at that
    # boundary, without a later scan of historical JSON restoring the first run.
    second = {**first, "generated_at": "2026-10-24T15:00:00Z", "totals": {"stars": 2}}
    second_key = renderer.write_snapshot(data, second)
    assert daily.read_bytes() == first_bytes
    assert json.loads((data / f"snapshots/{second_key}.json").read_text()) == second


@pytest.mark.parametrize("prefix", ["", "de/"])
def test_modern_daily_alias_keeps_json_download_on_rerender(rendered, prefix):
    root, renderer = rendered
    alias = root / prefix / "snapshot/2026-10-24/index.html"
    original = alias.read_bytes()
    renderer.main()
    assert alias.read_bytes() == original
    soup = BeautifulSoup(alias.read_text(), "html.parser")
    assert soup.select_one('a[href$="data/snapshots/2026-10-24.json"]')


@pytest.mark.parametrize("locale,prefix", [("en", ""), ("de", "de/")])
def test_copy_action_is_repaired_without_rewriting_modern_download(rendered, locale, prefix):
    root, renderer = rendered
    soup = BeautifulSoup((root / prefix / "snapshot/2026-10-24/index.html").read_text(), "html.parser")
    metadata = str(soup.select_one('script[type="application/ld+json"]'))
    soup.select_one("#copy-citation").string = "<built-in method copy of dict object>"
    translations = renderer.load_translations()[locale]
    migrated = BeautifulSoup(renderer.migrate_legacy_snapshot_html(str(soup), translations), "html.parser")
    assert str(migrated.select_one('script[type="application/ld+json"]')) == metadata
    assert migrated.select_one("#copy-citation").get_text() == translations["cite"]["copy"]
    assert migrated.select_one('a[href$="data/snapshots/2026-10-24.json"]')


def test_discovery_counts_exact_runs_once_and_keeps_aliases_resolvable(rendered, monkeypatch):
    root, renderer = rendered
    monkeypatch.setattr(renderer, "SNAPSHOT_LINKS", 3)
    data = root / "data"
    first = json.loads((data / "latest.json").read_text())
    second = {**first, "generated_at": "2026-10-24T15:00:00Z", "totals": {"stars": 2}}
    earlier = {**first, "generated_at": "2026-10-23T15:00:00Z", "totals": {"stars": 3}}
    keys = {renderer.archive_key(item) for item in (first, second, earlier)}
    renderer.write_snapshot(data, earlier)
    (data / "latest.json").write_text(json.dumps(second))
    renderer.main()
    for prefix in ("", "de/"):
        soup = BeautifulSoup((root / prefix / "index.html").read_text(), "html.parser")
        links = [link["href"] for link in soup.select(".snapshot-list a")]
        assert set(links) == {f"snapshot/{key}/" for key in keys}
        assert len(links) == len(keys)
        assert links[0] == f"snapshot/{renderer.archive_key(second)}/"
        for key in (*keys, "2026-10-24", "2026-10-23"):
            assert (root / prefix / f"snapshot/{key}/index.html").is_file()
    sitemap = ElementTree.fromstring((root / "sitemap.xml").read_text())
    locations = [node.text for node in sitemap.findall(".//{*}loc") if "/snapshot/" in node.text]
    assert set(locations) == {f"{renderer.SITE_URL}{prefix}snapshot/{key}/" for prefix in ("", "de/") for key in keys}
    assert len(locations) == len(keys) * 2


def test_unique_legacy_html_run_survives_json_alias_deduplication(rendered):
    root, renderer = rendered
    data = root / "data"
    earlier = json.loads((data / "latest.json").read_text())
    earlier_key = renderer.archive_key(earlier)
    figures = {}
    for prefix in ("", "de/"):
        page = root / prefix / "snapshot/2026-10-24/index.html"
        soup = BeautifulSoup(page.read_text(), "html.parser")
        script = soup.select_one('script[type="application/ld+json"]')
        payload = json.loads(script.string)
        dataset = next(node for node in payload["@graph"] if node["@type"] == "Dataset")
        dataset["url"] = f"{renderer.SITE_URL}{prefix}snapshot/2026-10-24/"
        dataset["distribution"] = [{"@type": "DataDownload", "contentUrl": f"{renderer.SITE_URL}data/latest.json"}]
        script.string = json.dumps(payload)
        figures[prefix] = [str(node) for node in soup.select("[data-figure]")]
        page.write_text(str(soup))
        (root / prefix / f"snapshot/{earlier_key}/index.html").unlink()
    (data / f"snapshots/{earlier_key}.json").unlink()
    # Simulate the historical collector overwriting the daily JSON while the
    # daily HTML still contains the only surviving record of the earlier run.
    later = {**earlier, "generated_at": "2026-10-24T15:00:00Z", "totals": {"stars": 99}}
    (data / "latest.json").write_text(json.dumps(later))
    (data / "snapshots/2026-10-24.json").write_text(json.dumps(later))
    renderer.main()
    later_key = renderer.archive_key(later)
    migrated_pages = {prefix: (root / prefix / "snapshot/2026-10-24/index.html").read_bytes() for prefix in ("", "de/")}
    renderer.main()
    for prefix in ("", "de/"):
        assert (root / prefix / "snapshot/2026-10-24/index.html").read_bytes() == migrated_pages[prefix]
        home = BeautifulSoup((root / prefix / "index.html").read_text(), "html.parser")
        assert {link["href"] for link in home.select(".snapshot-list a")} == {f"snapshot/{later_key}/", "snapshot/2026-10-24/"}
        alias = BeautifulSoup((root / prefix / "snapshot/2026-10-24/index.html").read_text(), "html.parser")
        assert [str(node) for node in alias.select("[data-figure]")] == figures[prefix]
        graph = json.loads(alias.select_one('script[type="application/ld+json"]').string)["@graph"]
        dataset = next(node for node in graph if node["@type"] == "Dataset")
        assert dataset["dateModified"] == earlier["generated_at"]
        assert dataset["distribution"][0]["encodingFormat"] == "text/html"
    sitemap = (root / "sitemap.xml").read_text()
    for prefix in ("", "de/"):
        assert f"{renderer.SITE_URL}{prefix}snapshot/2026-10-24/" in sitemap
        assert f"{renderer.SITE_URL}{prefix}snapshot/{later_key}/" in sitemap
