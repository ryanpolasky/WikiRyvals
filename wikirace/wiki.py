"""Fetch + sanitize Wikipedia articles and extract their internal links.

This is the single source of truth for "what is a valid link on a page": the set
of links we extract here is exactly the set we render as clickable, and exactly
the set the server validates moves against. Keeping one implementation guarantees
the rendered game and the anti-cheat check can never disagree.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

REST_HTML_URL = "https://en.wikipedia.org/api/rest_v1/page/html/{title}"
USER_AGENT = "RankedWikirace/0.0 (Phase0 prototype; https://github.com/ranked-wikirace)"

# Namespaces that are not real article pages and must never be playable links.
_NON_ARTICLE_PREFIXES = {
    "file",
    "image",
    "category",
    "help",
    "wikipedia",
    "template",
    "template_talk",
    "special",
    "portal",
    "draft",
    "module",
    "mediawiki",
    "book",
    "talk",
    "user",
    "user_talk",
    "wikt",
    "s",
    "q",
    "commons",
}

# CSS class fragments for chrome we strip before extracting links / rendering.
_STRIP_CLASS_FRAGMENTS = (
    "navbox",
    "vertical-navbox",
    "metadata",
    "mw-references",
    "reflist",
    "reference",
    "noprint",
    "mw-editsection",
    "hatnote",
    "ambox",
    "sistersitebox",
    "side-box",
    "mw-empty-elt",
)


@dataclass
class Article:
    title: str           # canonical, spaces (e.g. "World War II")
    html: str            # sanitized body HTML with in-app link rewriting
    links: list[str]     # canonical titles of internal article links (deduped, ordered)


def normalize_title(raw: str) -> str:
    """Normalize an href/title fragment into a canonical, space-separated title."""
    t = raw.strip()
    # Strip a leading "./" (Parsoid) or "/wiki/" prefix.
    t = re.sub(r"^\.?/", "", t)
    if t.lower().startswith("wiki/"):
        t = t[len("wiki/"):]
    # Drop any fragment / query.
    t = t.split("#", 1)[0].split("?", 1)[0]
    t = urllib.parse.unquote(t)
    t = t.replace("_", " ").strip()
    # Wikipedia capitalizes the first letter of article titles.
    if t:
        t = t[0].upper() + t[1:]
    return t


def _is_article_title(title: str) -> bool:
    if not title:
        return False
    if ":" in title:
        prefix = title.split(":", 1)[0].strip().lower()
        if prefix in _NON_ARTICLE_PREFIXES:
            return False
    if title.lower() == "main page":
        return False
    return True


def _href_to_title(href: str) -> str | None:
    """Return canonical article title for an internal link href, else None."""
    if not href:
        return None
    if href.startswith("#"):
        return None
    # Accept Parsoid "./Title", site-relative "/wiki/Title", and absolute en.wp links.
    if href.startswith("//") or href.startswith("http"):
        parsed = urllib.parse.urlparse(href)
        if "wikipedia.org" not in parsed.netloc:
            return None
        if not parsed.path.startswith("/wiki/"):
            return None
        href = parsed.path
    if not (href.startswith("./") or href.startswith("/wiki/")):
        return None
    title = normalize_title(href)
    return title if _is_article_title(title) else None


def _strip_chrome(soup: BeautifulSoup) -> None:
    for tag in soup(["script", "style", "sup", "table"]):
        # Keep plain content tables but drop reference superscripts and chrome tables.
        if tag.name == "table":
            classes = " ".join(tag.get("class", []))
            if any(frag in classes for frag in _STRIP_CLASS_FRAGMENTS):
                tag.decompose()
            continue
        tag.decompose()
    for el in soup.find_all(class_=True):
        if el.decomposed:  # parent already removed this node
            continue
        classes = " ".join(el.get("class", []) or [])
        if any(frag in classes for frag in _STRIP_CLASS_FRAGMENTS):
            el.decompose()


def sanitize(html: str) -> tuple[str, list[str]]:
    """Return (clean_html, ordered_unique_link_titles).

    All internal article anchors are rewritten to `href="#"` with a
    `data-title` attribute and `class="wr-link"` so the frontend can intercept
    clicks and route them through server-side validation. Every other anchor is
    flattened to plain text so only valid in-game links are clickable.
    """
    soup = BeautifulSoup(html, "lxml")
    _strip_chrome(soup)

    links: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a"):
        if a.decomposed:
            continue
        title = _href_to_title(a.get("href", ""))
        if title is None:
            # Not a playable link: flatten to plain text.
            a.unwrap()
            continue
        a["href"] = "#"
        a["data-title"] = title
        a["class"] = "wr-link"
        for attr in ("rel", "title", "about", "typeof"):
            a.attrs.pop(attr, None)
        if title not in seen:
            seen.add(title)
            links.append(title)

    body = soup.find("body") or soup
    return body.decode_contents(), links


def fetch_article(title: str, session: requests.Session | None = None,
                  timeout: int = 20) -> Article:
    """Fetch + sanitize a single article from the live MediaWiki REST API."""
    sess = session or requests.Session()
    url = REST_HTML_URL.format(title=urllib.parse.quote(title.replace(" ", "_"), safe=""))
    resp = sess.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    clean_html, links = sanitize(resp.text)
    return Article(title=normalize_title(title), html=clean_html, links=links)
