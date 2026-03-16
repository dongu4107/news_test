from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Dict
from urllib.parse import urljoin

from bs4 import BeautifulSoup

try:
    from readability import Document
except Exception as e:  # pragma: no cover
    Document = None  # type: ignore[assignment]
    _READABILITY_IMPORT_ERROR = e
else:
    _READABILITY_IMPORT_ERROR = None


@dataclass
class ExtractedArticle:
    title: Optional[str]
    content_html: Optional[str]  # cleaned article HTML fragment
    text: Optional[str]
    author: Optional[str]
    published_at: Optional[str]
    canonical_url: Optional[str]
    og_image: Optional[str]
    twitter_image: Optional[str]


def _first_meta(
    soup: BeautifulSoup, *, property_name: Optional[str] = None, name: Optional[str] = None
) -> Optional[str]:
    if property_name:
        tag = soup.find("meta", attrs={"property": property_name})
        if tag and tag.get("content"):
            return str(tag.get("content")).strip() or None
    if name:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return str(tag.get("content")).strip() or None
    return None


def _extract_meta(soup: BeautifulSoup, base_url: str) -> Dict[str, Optional[str]]:
    canonical = None
    link = soup.find("link", attrs={"rel": lambda v: isinstance(v, (str, list)) and "canonical" in (v if isinstance(v, list) else [v])})
    if link and link.get("href"):
        canonical = urljoin(base_url, str(link.get("href")).strip())
    og_image = _first_meta(soup, property_name="og:image")
    twitter_image = _first_meta(soup, name="twitter:image") or _first_meta(soup, property_name="twitter:image")

    # Common article meta candidates (best-effort)
    author = _first_meta(soup, name="author") or _first_meta(soup, property_name="article:author")
    published_at = (
        _first_meta(soup, property_name="article:published_time")
        or _first_meta(soup, name="pubdate")
        or _first_meta(soup, name="publishdate")
        or _first_meta(soup, name="date")
    )
    if og_image:
        og_image = urljoin(base_url, og_image)
    if twitter_image:
        twitter_image = urljoin(base_url, twitter_image)
    return {
        "canonical_url": canonical,
        "og_image": og_image,
        "twitter_image": twitter_image,
        "author": author,
        "published_at": published_at,
    }


def extract_article(html: str, base_url: str) -> ExtractedArticle:
    soup = BeautifulSoup(html, "lxml")
    meta = _extract_meta(soup, base_url)

    title = None
    if soup.title and soup.title.string:
        title = str(soup.title.string).strip() or None

    if Document is None:
        raise RuntimeError(f"readability-lxml not available: {_READABILITY_IMPORT_ERROR}")

    doc = Document(html)
    summary_html = doc.summary(html_partial=True)
    readable_title = (doc.short_title() or "").strip() or None
    if readable_title:
        title = readable_title

    content_soup = BeautifulSoup(summary_html, "lxml")

    # Extract text from the readable fragment
    text = content_soup.get_text(separator="\n", strip=True) or None
    # Keep this as a fragment (no <html>/<body>) so we can embed cleanly.
    if content_soup.body:
        content_html = content_soup.body.decode_contents()
    else:
        content_html = content_soup.decode()

    return ExtractedArticle(
        title=title,
        content_html=content_html,
        text=text,
        author=meta.get("author"),
        published_at=meta.get("published_at"),
        canonical_url=meta.get("canonical_url"),
        og_image=meta.get("og_image"),
        twitter_image=meta.get("twitter_image"),
    )


def parse_content_fragment(content_html: str) -> Any:
    # Wrap in a stable container so we can rewrite and then extract inner HTML safely.
    soup = BeautifulSoup(f"<div id=\"__article__\">{content_html}</div>", "lxml")
    return soup.find(id="__article__")
