#!/usr/bin/env python3
"""Build gate for the rendered dashboard.

Checks the published artefact, not the source: the point is what a crawler or a
text browser receives. Exit code 1 fails the build.

    OUTPUT_DIR=build python scripts/verify_site.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_accessibility import check_accessibility  # noqa: E402

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "build"))
MAX_SNAPSHOT_AGE_DAYS = 3

errors: list[str] = []
warnings: list[str] = []

PLACEHOLDERS = ("Loading…", "Loading...", "TBD", "Lorem ipsum")

REQUIRED_META = (
    (r'<link rel="canonical" href="[^"]+"', "canonical"),
    (r'<meta name="description" content="[^"]+"', "meta description"),
    (r'hreflang="x-default"', "x-default hreflang"),
    (r'<meta property="og:image" content="[^"]+"', "og:image"),
    (r'<meta name="twitter:card"', "twitter:card"),
    (r'<script type="application/ld\+json">', "JSON-LD"),
)


def strip_markup(html: str) -> str:
    html = re.sub(r"<script\b[^>]*>.*?</script\b[^>]*>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style\b[^>]*>.*?</style\b[^>]*>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def main() -> int:
    if not OUTPUT_DIR.exists():
        print(f"verify_site: {OUTPUT_DIR} not found — run render_site.py first", file=sys.stderr)
        return 1

    latest = OUTPUT_DIR / "data" / "latest.json"
    if not latest.exists():
        errors.append("data/latest.json is missing — the page would have nothing to render")
    else:
        snapshot = json.loads(latest.read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(snapshot["generated_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - generated).days
        if age > MAX_SNAPSHOT_AGE_DAYS:
            warnings.append(
                f"latest.json is {age} days old — the daily collection may have stopped"
            )
        if not snapshot.get("repos"):
            errors.append("latest.json contains no repositories")

    pages = sorted(OUTPUT_DIR.rglob("*.html"))
    if not pages:
        errors.append("no HTML pages were rendered")

    for page in pages:
        name = page.relative_to(OUTPUT_DIR).as_posix()
        html = page.read_text(encoding="utf-8")
        text = strip_markup(html)

        for placeholder in PLACEHOLDERS:
            if placeholder in text:
                errors.append(f"{name}: placeholder text in the initial HTML: {placeholder!r}")

        for pattern, label in REQUIRED_META:
            if not re.search(pattern, html):
                errors.append(f"{name}: no {label}")

        for block in re.findall(
            r'<script type="application/ld\+json">([\s\S]*?)</script>', html
        ):
            try:
                parsed = json.loads(block)
            except json.JSONDecodeError as exc:
                errors.append(f"{name}: invalid JSON-LD: {exc}")
                continue
            nodes = parsed.get("@graph", [parsed])
            if not nodes:
                errors.append(f"{name}: empty JSON-LD graph")
            for node in nodes:
                if "@type" not in node:
                    errors.append(f"{name}: JSON-LD node without @type")

        contact_links = re.findall(r'href="([^"]*netresearch\.de/kontakt/[^"]*)"', html)
        if not contact_links:
            errors.append(f"{name}: no business CTA to the contact form")
        for href in contact_links:
            for param in ("utm_source", "utm_medium", "utm_campaign", "utm_content"):
                if f"{param}=" not in href:
                    errors.append(f"{name}: contact link without {param}")

        logos = re.findall(r"<img[^>]+netresearch\.svg[^>]*>", html)
        if len(logos) != 1:
            errors.append(f"{name}: the logo appears {len(logos)} times, expected exactly once")

        # The dashboard's whole point is figures in the HTML.
        if name.endswith("index.html") and "/repo/" not in f"/{name}":
            if not re.search(r'data-figure="[^"]+"[^>]*>\s*[\d.,]+', html):
                errors.append(f"{name}: no rendered figure found")

        # Accessibility and semantics decidable from the markup alone.
        for problem in check_accessibility(html):
            errors.append(f"{name}: {problem}")

        # Estimated reach must never appear without its caveat on the same page.
        if "downstream-reach" in html or "reach-heading" in html:
            if 'class="warning"' not in html:
                errors.append(f"{name}: the reach estimate is shown without its caveat")

    for required in ("sitemap.xml", "robots.txt", "CITATION.cff",
                     "data/repositories.csv", "data/data-dictionary.json",
                     "assets/vendor/chart.umd.min.js"):
        if not (OUTPUT_DIR / required).exists():
            errors.append(f"missing {required}")

    # No third-party runtime requests: everything the page loads is served here.
    for page in pages:
        html = page.read_text(encoding="utf-8")
        for match in re.findall(r'<(?:script|link)[^>]+(?:src|href)="(https?://[^"]+)"', html):
            if not match.startswith("https://netresearch.github.io/"):
                errors.append(
                    f"{page.relative_to(OUTPUT_DIR).as_posix()}: loads a third-party asset: {match}"
                )

    for message in warnings:
        print(f"warn  {message}")
    for message in errors:
        print(f"ERROR {message}", file=sys.stderr)

    print(f"\nverify_site: {len(pages)} pages checked, {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
