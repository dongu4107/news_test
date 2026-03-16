from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, List, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag


_LAZY_ATTRS = [
    "src",
    "data-src",
    "data-original",
    "data-orig-src",
    "data-lazy-src",
    "data-actualsrc",
]


@dataclass
class ImageCandidate:
    original_url: str
    resolved_url: str
    tag_name: str


def _parse_srcset(value: str) -> List[Tuple[str, Optional[int]]]:
    # Returns list of (url, width) where width is in px if provided (e.g. "800w"), else None.
    out: List[Tuple[str, Optional[int]]] = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        tokens = part.split()
        u = tokens[0].strip()
        w = None
        if len(tokens) >= 2:
            m = re.match(r"^(\d+)w$", tokens[1].strip())
            if m:
                try:
                    w = int(m.group(1))
                except Exception:
                    w = None
        out.append((u, w))
    return out


def _pick_from_srcset(srcset: str, *, max_width: int = 1200) -> Optional[str]:
    candidates = _parse_srcset(srcset)
    if not candidates:
        return None
    with_w = [(u, w) for (u, w) in candidates if w is not None]
    if with_w:
        under = [(u, w) for (u, w) in with_w if int(w) <= max_width]
        if under:
            return max(under, key=lambda x: int(x[1]))[0]
        return max(with_w, key=lambda x: int(x[1]))[0]
    return candidates[-1][0]


def _best_url_from_tag(tag: Tag) -> Optional[str]:
    # srcset first (when present), then lazy attributes.
    if tag.has_attr("srcset"):
        picked = _pick_from_srcset(str(tag.get("srcset") or ""))
        if picked:
            return picked
    for attr in _LAZY_ATTRS:
        if tag.has_attr(attr):
            v = str(tag.get(attr) or "").strip()
            if v:
                return v
    return None


def iter_article_image_urls(content_root: BeautifulSoup, base_url: str) -> Iterable[ImageCandidate]:
    # Article images inside the content fragment.
    for tag in content_root.find_all(["img", "source"]):
        if not isinstance(tag, Tag):
            continue
        if tag.name == "source":
            # <source srcset="..."> (often inside <picture>)
            srcset = str(tag.get("srcset") or "").strip()
            if not srcset:
                continue
            picked = _pick_from_srcset(srcset)
            if not picked:
                continue
            original = picked
            resolved = urljoin(base_url, picked)
            yield ImageCandidate(original_url=original, resolved_url=resolved, tag_name="source")
            continue

        original = _best_url_from_tag(tag)
        if not original:
            continue
        resolved = urljoin(base_url, original)
        yield ImageCandidate(original_url=original, resolved_url=resolved, tag_name="img")


def rewrite_images_to_local(content_root: BeautifulSoup, url_map: dict[str, str]) -> None:
    # Mutates content_root: replace image URLs with local paths, drop srcset/sizes/lazy attrs.
    for tag in content_root.find_all(["img", "source"]):
        if not isinstance(tag, Tag):
            continue
        if tag.name == "source":
            srcset = str(tag.get("srcset") or "")
            picked = _pick_from_srcset(srcset) if srcset else None
            if picked and picked in url_map:
                tag["srcset"] = url_map[picked]
            else:
                # If we can't map, remove to avoid broken external refs.
                if tag.has_attr("srcset"):
                    del tag["srcset"]
            continue

        # img
        original = _best_url_from_tag(tag)
        if original and original in url_map:
            tag["src"] = url_map[original]
        else:
            # If this looks like an external image and we didn't archive it, drop the URL to avoid
            # offline network fetch attempts.
            def looks_external(u: str) -> bool:
                u = (u or "").strip().lower()
                return u.startswith("http://") or u.startswith("https://") or u.startswith("//")

            candidate_vals = []
            for attr in _LAZY_ATTRS:
                if tag.has_attr(attr):
                    candidate_vals.append(str(tag.get(attr) or ""))
            if tag.has_attr("srcset"):
                candidate_vals.append(str(tag.get("srcset") or ""))

            if any(looks_external(v) for v in candidate_vals):
                tag.attrs.pop("src", None)
                tag.attrs.pop("srcset", None)
                tag.attrs.pop("sizes", None)
                if not str(tag.get("alt") or "").strip():
                    tag["alt"] = "Image not archived"
                cls = tag.get("class") or []
                if isinstance(cls, str):
                    cls = [cls]
                if "image-not-archived" not in cls:
                    cls.append("image-not-archived")
                tag["class"] = cls
        # Remove attributes that may trigger network fetches or cause confusion offline
        for attr in list(tag.attrs.keys()):
            if attr in ("src", "alt", "title", "width", "height", "loading", "decoding", "class"):
                continue
            if attr.startswith("data-") or attr in ("srcset", "sizes"):
                tag.attrs.pop(attr, None)
