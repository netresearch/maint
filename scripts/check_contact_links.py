"""Validate every marked business action, including shared-header contacts."""
import html
import re
from urllib.parse import parse_qs, urlsplit

from check_accessibility import attrs

CONTACT_CLASSES = {"nr-shell-contact", "nr-shell-menu-contact"}
REQUIRED_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_content")


def business_contact_urls(markup: str) -> list[str]:
    """Collect actions by purpose, including missing or malformed destinations."""
    links = []
    for tag in re.findall(r"<a\b[^>]*>", markup):
        attributes = attrs(tag)
        if attributes.get("data-cta") == "business" or CONTACT_CLASSES.intersection(attributes.get("class", "").split()):
            links.append(html.unescape(attributes.get("href", "")))
    return links


def check_contact_links(markup: str, lang: str) -> list[str]:
    """A valid footer cannot hide another action with a broken destination."""
    problems = []
    links = business_contact_urls(markup)
    if not links:
        problems.append("no business CTA to the contact form")
    expected_path = "/kontakt/" if lang == "de" else "/en/contact/"
    for href in links:
        try:
            target = urlsplit(href)
        except ValueError:
            problems.append(f"business CTA has an invalid URL: {href}")
            continue
        if target.scheme != "https" or target.netloc != "www.netresearch.de" or target.path != expected_path:
            problems.append(f"business CTA has an incorrect destination for {lang}: {href}")
        params = parse_qs(target.query, keep_blank_values=True)
        for param in REQUIRED_PARAMS:
            if not params.get(param, [""])[0]:
                problems.append(f"contact link without {param}: {href}")
    return problems
