#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Render 1200x630 previews with the page's canonical symbol and local fonts.

Run npm ci first: rendering uses this repository's existing Puppeteer dependency,
not the system fonts selected by an SVG rasterizer. Set PUPPETEER_EXECUTABLE_PATH
or CHROME_PATH to use an installed Chrome instead of Puppeteer's browser.

    uv run scripts/render_og.py
"""
from __future__ import annotations

import base64
import html
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"
ASSETS = DASHBOARD / "assets"
SUBTITLE = {
    "en": "Measured open source activity. Transparent and reproducible.",
    "de": "Gemessene Open-Source-Aktivität. Transparent und reproduzierbar.",
}

# Keep the renderer with the generator; the repository already installs
# Puppeteer for its browser gate, so no additional runtime dependency is needed.
BROWSER_RENDER = r"""
import {readFile} from 'node:fs/promises';
import puppeteer from 'puppeteer';
const [source, target] = process.argv.slice(1);
const browser = await puppeteer.launch({
  executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || process.env.CHROME_PATH || undefined,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
try {
  const page = await browser.newPage();
  await page.setViewport({width: 1200, height: 630, deviceScaleFactor: 1});
  await page.setContent(await readFile(source, 'utf8'));
  const state = await page.evaluate(async () => {
    await Promise.all([document.fonts.load('600 64px Raleway'), document.fonts.load('400 26px "Open Sans"')]);
    await document.fonts.ready;
    const loaded = [...document.fonts].filter((face) => face.status === 'loaded').map((face) => face.family);
    const image = document.querySelector('header img');
    const overflow = [...document.querySelectorAll('[data-fit]')].some((element) => {
      const box = element.getBoundingClientRect();
      return box.right > 1200 || box.bottom > 572 || element.scrollWidth > element.clientWidth;
    });
    return {loaded, logo: image.complete && image.naturalWidth > 0, overflow};
  });
  if (!state.loaded.includes('Raleway') || !state.loaded.includes('Open Sans') || !state.logo || state.overflow) {
    throw new Error(`Card assets or layout failed validation: ${JSON.stringify(state)}`);
  }
  await page.screenshot({path: target, type: 'png'});
} finally {
  await browser.close();
}
"""


def data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def card(title: str, subtitle: str, lang: str = "en") -> str:
    logo = data_url(ASSETS / "netresearch.svg", "image/svg+xml")
    heading_font = data_url(ASSETS / "fonts/raleway-latin-600-normal.woff2", "font/woff2")
    body_font = data_url(ASSETS / "fonts/open-sans-latin-400-normal.woff2", "font/woff2")
    return f"""<!doctype html><html lang="{lang}"><meta charset="utf-8"><style>
@font-face{{font-family:Raleway;src:url('{heading_font}') format('woff2');font-weight:600}}
@font-face{{font-family:'Open Sans';src:url('{body_font}') format('woff2');font-weight:400}}
*{{box-sizing:border-box}}
body{{margin:0;width:1200px;height:630px;background:linear-gradient(120deg,#15585E,#0A5057 60%,#06363B);color:#FFFFFF;font:400 26px/1.45 'Open Sans';position:relative}}
header{{height:112px;display:flex;align-items:center;gap:24px;padding:12px 64px;background:#FFFFFF;color:#4D4F57;font-size:24px}}
header img{{width:88px;height:88px;object-fit:contain}}
main{{padding:44px 72px;position:relative}}
.eyebrow{{display:flex;align-items:center;gap:12px;font-size:20px;letter-spacing:.08em;text-transform:uppercase}}
.eyebrow::before{{content:'';width:10px;height:10px;background:#FF4D00;border-radius:50%}}
h1{{font:600 64px/1.12 Raleway;margin:20px 0 24px;max-width:1056px;overflow-wrap:anywhere}}
p{{margin:0;max-width:1056px}}
footer{{position:absolute;bottom:0;left:0;width:100%;height:58px;background:#25272D;display:flex;align-items:center;justify-content:space-between;padding:0 72px;font-size:20px}}
</style><header><img src="{logo}" alt="Netresearch DTT GmbH"><span>Open Source</span></header>
<main><div class="eyebrow" data-fit>Open Source · Netresearch</div><h1 data-fit>{html.escape(title)}</h1><p data-fit>{html.escape(subtitle)}</p></main>
<footer><span>Netresearch DTT GmbH</span><span>netresearch.github.io/maint</span></footer></html>"""


def main() -> None:
    node = os.environ.get("NODE_BINARY") or shutil.which("node")
    if not node:
        raise SystemExit("Node.js is required; run npm ci and make node available on PATH.")
    with tempfile.TemporaryDirectory(prefix="nr-impact-og-") as temporary:
        for lang in ("en", "de"):
            translations = yaml.safe_load((DASHBOARD / "i18n" / f"{lang}.yaml").read_text(encoding="utf-8"))
            source = Path(temporary) / f"{lang}.html"
            source.write_text(card(translations["hero"]["title"], SUBTITLE[lang], lang), encoding="utf-8")
            target = ASSETS / f"og-impact-{lang}.png"
            subprocess.run([node, "--input-type=module", "--eval", BROWSER_RENDER, str(source), str(target)], cwd=ROOT, check=True)
            print(f"Wrote {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
