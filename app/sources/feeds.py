"""RSS/Atom feeds and YouTube channel feeds."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import feedparser

from app import images
from app.db import utcnow
from app.sources.base import RawItem, SourceError, get, html_to_text


def _entry_time(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        t = entry.get(attr)
        if t:
            return datetime(*t[:6])
    return None


def _matches(cfg: dict, *texts: str) -> bool:
    blob = " ".join(t for t in texts if t)
    inc, exc = cfg.get("include"), cfg.get("exclude")
    if inc and not re.search(inc, blob, re.I):
        return False
    if exc and re.search(exc, blob, re.I):
        return False
    return True


def fetch_rss(source, session) -> list[RawItem]:
    cfg = source.config or {}
    url = cfg.get("url")
    if not url:
        raise SourceError("rss source needs a url")
    feed = feedparser.parse(get(url).content)
    if feed.bozo and not feed.entries:
        raise SourceError(f"could not parse feed: {feed.bozo_exception}")
    items = []
    for e in feed.entries[: int(cfg.get("limit", 40))]:
        tags = " ".join(t.get("term", "") for t in e.get("tags", []))
        summary = html_to_text(e.get("summary", "") or "", 2500)
        if not _matches(cfg, e.get("title", ""), summary, tags):
            continue
        link = e.get("link", "")
        extra = {"tags": tags[:300]} if tags else {}
        image = images.from_feed_entry(e)
        if image:
            extra["image"] = image
        items.append(
            RawItem(
                key=f"rss:{e.get('id') or link}",
                title=e.get("title", "(untitled)"),
                url=link,
                body=summary,
                author=e.get("author", ""),
                published_at=_entry_time(e),
                extra=extra,
            )
        )
    return items


# --- YouTube ------------------------------------------------------------------
# Channel RSS feeds need no API key or quota. The channel list comes from the
# YouTube page in admin (Takeout CSV or pasted handles/ids).


def _fetch_channel(channel_id: str, max_age: timedelta) -> list[RawItem]:
    try:
        resp = get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
    except SourceError:
        return []
    feed = feedparser.parse(resp.content)
    cutoff = utcnow() - max_age
    channel_title = feed.feed.get("title", "")
    out = []
    for e in feed.entries[:5]:
        published = _entry_time(e)
        if published and published < cutoff:
            continue
        vid = e.get("yt_videoid") or e.get("id", "")
        if "/shorts/" in e.get("link", "") or "#shorts" in (e.get("title", "") or "").lower():
            continue  # Shorts aren't worth a recap line
        desc = ""
        if e.get("media_description"):
            desc = e["media_description"]
        elif e.get("summary"):
            desc = e["summary"]
        out.append(
            RawItem(
                key=f"yt:{vid}",
                title=e.get("title", ""),
                url=e.get("link", f"https://www.youtube.com/watch?v={vid}"),
                body=html_to_text(desc, 1500),
                author=channel_title,
                published_at=published,
                extra={"channel": channel_title, "channel_id": channel_id, "video_id": vid,
                       "image": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"},
            )
        )
    return out


def fetch_youtube(source, session) -> list[RawItem]:
    from sqlalchemy import select

    from app.models import YoutubeChannel

    cfg = source.config or {}
    ids = list(cfg.get("channel_ids") or [])
    if cfg.get("use_subscriptions", True):
        ids += [c.channel_id for c in session.scalars(select(YoutubeChannel).where(YoutubeChannel.active))]
    ids = list(dict.fromkeys(i.strip() for i in ids if i.strip()))
    if not ids:
        raise SourceError("no YouTube channels yet: import your subscriptions on the YouTube page")
    max_age = timedelta(days=int(cfg.get("max_age_days", 3)))
    items: list[RawItem] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for batch in pool.map(lambda cid: _fetch_channel(cid, max_age), ids):
            items.extend(batch)
    return items
