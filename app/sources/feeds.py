"""RSS/Atom feeds, Reddit, YouTube channel feeds."""

from __future__ import annotations

import base64
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import feedparser

from app import config, images
from app.db import utcnow
from app.sources.base import RawItem, SourceError, get, html_to_text, http


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


# --- Reddit -----------------------------------------------------------------
# Reddit is shutting down open access: new API requests close Oct 31 2026, RSS
# support ends Nov 13 2026. With credentials we use the OAuth API; without them
# we try RSS. The "websearch" source kind restricted to reddit.com is the backup.

_reddit_token: dict = {"value": None, "expires": 0.0}


def _reddit_access_token() -> str:
    if _reddit_token["value"] and time.time() < _reddit_token["expires"] - 60:
        return _reddit_token["value"]
    auth = base64.b64encode(
        f"{config.REDDIT_CLIENT_ID}:{config.REDDIT_CLIENT_SECRET}".encode()
    ).decode()
    with http() as c:
        resp = c.post(
            "https://www.reddit.com/api/v1/access_token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {auth}", "User-Agent": config.REDDIT_USER_AGENT},
        )
    if resp.status_code != 200:
        raise SourceError(f"reddit auth failed: HTTP {resp.status_code}")
    data = resp.json()
    _reddit_token.update(value=data["access_token"], expires=time.time() + data.get("expires_in", 3600))
    return _reddit_token["value"]


def _reddit_api(path: str, params: dict | None = None) -> dict | list:
    token = _reddit_access_token()
    with http() as c:
        resp = c.get(
            f"https://oauth.reddit.com{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}", "User-Agent": config.REDDIT_USER_AGENT},
        )
    if resp.status_code >= 400:
        raise SourceError(f"reddit API HTTP {resp.status_code} for {path}")
    return resp.json()


def reddit_enabled() -> bool:
    return bool(config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET)


def fetch_reddit(source, session) -> list[RawItem]:
    cfg = source.config or {}
    sub = (cfg.get("subreddit") or "").strip().removeprefix("r/")
    if not sub:
        raise SourceError("reddit source needs a subreddit")
    sort = cfg.get("sort", "new")
    limit = int(cfg.get("limit", 30))
    min_score = int(cfg.get("min_score", 0))
    items: list[RawItem] = []
    if reddit_enabled():
        data = _reddit_api(f"/r/{sub}/{sort}", {"limit": limit, "raw_json": 1})
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            if d.get("stickied") or d.get("score", 0) < min_score:
                continue
            if not _matches(cfg, d.get("title", ""), d.get("selftext", ""), d.get("link_flair_text") or ""):
                continue
            items.append(
                RawItem(
                    key=f"reddit:{d['id']}",
                    title=d.get("title", ""),
                    url=f"https://www.reddit.com{d.get('permalink', '')}",
                    body=(d.get("selftext") or "")[:3000],
                    author=f"u/{d.get('author', '')}",
                    published_at=datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc).replace(tzinfo=None),
                    extra={
                        "subreddit": sub,
                        "score": d.get("score"),
                        "num_comments": d.get("num_comments"),
                        "flair": d.get("link_flair_text"),
                        "link": d.get("url") if not d.get("is_self") else None,
                        "reddit_id": d["id"],
                    },
                )
            )
        return items
    # No credentials: RSS (works until Reddit turns it off on Nov 13 2026).
    resp = get(f"https://www.reddit.com/r/{sub}/{sort}/.rss", headers={"User-Agent": config.REDDIT_USER_AGENT})
    feed = feedparser.parse(resp.content)
    for e in feed.entries[:limit]:
        body = html_to_text(e.get("summary", ""), 3000)
        if not _matches(cfg, e.get("title", ""), body):
            continue
        items.append(
            RawItem(
                key=f"reddit:{e.get('id')}",
                title=e.get("title", ""),
                url=e.get("link", ""),
                body=body,
                author=e.get("author", ""),
                published_at=_entry_time(e),
                extra={"subreddit": sub},
            )
        )
    return items


def reddit_top_comments(reddit_id: str, n: int = 6) -> list[str]:
    """Best-effort: top comments for a post, so agents can relay what people said."""
    if not reddit_enabled() or not reddit_id:
        return []
    try:
        data = _reddit_api(f"/comments/{reddit_id}", {"limit": n, "sort": "top", "depth": 1, "raw_json": 1})
        out = []
        for child in data[1]["data"]["children"][:n]:
            d = child.get("data", {})
            if d.get("body"):
                out.append(f"u/{d.get('author')} ({d.get('score', 0)} pts): {d['body'][:500]}")
        return out
    except Exception:
        return []


# --- YouTube ------------------------------------------------------------------
# Channel RSS feeds need no API key or quota. The channel list comes from the
# YouTube page in admin (Google sign-in import, Takeout CSV, or pasted ids).


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
