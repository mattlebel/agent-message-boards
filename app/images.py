"""Pictures for emails: each item's image from its source, or the page's preview image."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


def _ok(url: str | None) -> str:
    url = (url or "").strip()
    return url if url.startswith("https://") and len(url) < 2000 else ""


def from_html(html: str, base_url: str = "") -> str:
    """The page's preview image (og:image / twitter:image), or the first sizable <img>."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for attrs in ({"property": "og:image"}, {"name": "og:image"}, {"name": "twitter:image"},
                  {"property": "twitter:image"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return _ok(urljoin(base_url, tag["content"]))
    for img in soup.find_all("img", src=True):
        w = str(img.get("width") or "")
        if w.isdigit() and int(w) < 120:
            continue  # icons and tracking pixels
        src = urljoin(base_url, img["src"])
        if not re.search(r"(pixel|spacer|1x1|logo|icon|avatar)", src, re.I):
            return _ok(src)
    return ""


def from_feed_entry(entry) -> str:
    """Image attached to an RSS/Atom entry (media tags, enclosures, or the first <img> in its HTML)."""
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key) or []:
            if _ok(m.get("url")) and ("image" in (m.get("type") or "image") or key == "media_thumbnail"):
                return _ok(m["url"])
    for enc in entry.get("enclosures") or []:
        if "image" in (enc.get("type") or "") and _ok(enc.get("href")):
            return _ok(enc["href"])
    html = entry.get("summary", "") or ""
    for c in entry.get("content") or []:
        html += c.get("value", "")
    m = re.search(r'<img[^>]+src="([^"]+)"', html)
    return _ok(m.group(1)) if m else ""


def for_item(item) -> str:
    extra = item.extra or {}
    if extra.get("image"):
        return _ok(extra["image"])
    if extra.get("video_id"):
        return f"https://i.ytimg.com/vi/{extra['video_id']}/hqdefault.jpg"
    return ""


def fetch_preview(url: str) -> str:
    from app.sources.base import get

    if not url or not url.startswith("http"):
        return ""
    try:
        resp = get(url)
    except Exception:
        return ""
    if "html" not in resp.headers.get("content-type", ""):
        return ""
    return from_html(resp.text[:400000], str(resp.url))


def fill_missing(session, items, limit: int = 8) -> None:
    """Fetch preview images for items that don't have one yet (cached on the item)."""
    todo = [i for i in items if not for_item(i) and i.url and not (i.extra or {}).get("image_checked")][:limit]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=4) as pool:
        found = list(pool.map(lambda i: fetch_preview(i.url), todo))
    for item, img in zip(todo, found):
        item.extra = {**(item.extra or {}), "image_checked": True, **({"image": img} if img else {})}
    session.flush()
