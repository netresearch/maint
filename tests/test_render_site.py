"""Published citation paths must remain useful after the next daily run."""
import importlib.util
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup


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
    figures = [node.get_text() for node in soup.select("[data-figure]")]
    migrated = BeautifulSoup(renderer.migrate_legacy_snapshot_html(str(soup), renderer.load_translations()["en"]), "html.parser")
    actual = next(node for node in json.loads(migrated.select_one('script[type="application/ld+json"]').string)["@graph"] if node["@type"] == "Dataset")
    assert actual["dateModified"] == original_date
    assert actual["distribution"] == [{"@type": "DataDownload", "name": renderer.load_translations()["en"]["hero"]["title"], "encodingFormat": "text/html", "contentUrl": legacy_url}]
    assert [node.get_text() for node in migrated.select("[data-figure]")] == figures
    assert migrated.select_one("#copy-citation").get_text() == renderer.load_translations()["en"]["cite"]["copy"]
    assert not any(link.get("href", "").endswith("data/snapshots/2026-10-24.json") for link in migrated.select("a"))
    download = migrated.select_one("a[download]")
    assert download["href"] == legacy_url
    assert download.get_text() == "HTML archive"
