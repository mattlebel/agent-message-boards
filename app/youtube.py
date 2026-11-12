"""Adding YouTube channels by @handle, URL or channel ID (one or many at once)."""

from __future__ import annotations

import html as html_lib
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, settings_store
from app.models import YoutubeChannel

log = logging.getLogger(__name__)

_CHANNEL_ID = re.compile(r"\b(UC[\w-]{22})\b")


def parse_channels(text: str) -> list[str]:
    """Pull @handles, channel URLs and UC… ids out of pasted text (any separators)."""
    found = []
    for token in re.split(r"[\s,;]+", text or ""):
        token = token.strip().strip("<>()\"'")
        if not token:
            continue
        m = _CHANNEL_ID.search(token)
        if m:
            found.append(m.group(1))
        elif "youtube.com/" in token:
            m = re.search(r"youtube\.com/(@[\w.-]+|c/[\w.-]+|user/[\w.-]+)", token)
            if m:
                found.append(m.group(1) if m.group(1).startswith("@") else token)
        elif re.fullmatch(r"@?[\w.-]{3,100}", token):
            found.append(token if token.startswith("@") else "@" + token)
    return list(dict.fromkeys(found))


def resolve(value: str) -> tuple[str, str]:
    """(channel_id, title) for a handle/URL/id, or ("", "") if YouTube doesn't know it."""
    m = _CHANNEL_ID.search(value)
    if m:
        return m.group(1), ""
    url = value if value.startswith("http") else f"https://www.youtube.com/{value}"
    try:
        resp = httpx.get(url, timeout=20, follow_redirects=True,
                         headers={"User-Agent": config.USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
                         cookies={"CONSENT": "YES+1"})
    except httpx.HTTPError as e:
        log.info("youtube lookup failed for %s: %s", value, e)
        return "", ""
    if resp.status_code != 200:
        return "", ""
    page = resp.text
    # The canonical link names the page's own channel; channelId elsewhere can be a featured channel.
    m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page)
         or re.search(r'"externalId":"(UC[\w-]{22})"', page)
         or re.search(r'<meta itemprop="identifier" content="(UC[\w-]{22})"', page))
    if not m:
        return "", ""
    t = re.search(r'<meta property="og:title" content="([^"]*)"', page) or re.search(r"<title>(.*?)</title>", page)
    title = html_lib.unescape(t.group(1)).replace(" - YouTube", "").strip() if t else ""
    return m.group(1), title


def add_many(session: Session, values: list[str], via: str = "manual") -> tuple[int, list[str]]:
    """Look up and save channels. Returns (newly added, values that couldn't be found)."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(resolve, values))
    added, missing = 0, []
    for value, (cid, title) in zip(values, results):
        if not cid:
            missing.append(value)
            continue
        ch = session.scalar(select(YoutubeChannel).where(YoutubeChannel.channel_id == cid))
        if ch is None:
            session.add(YoutubeChannel(channel_id=cid, title=(title or value)[:300], added_via=via))
            added += 1
        elif title and (not ch.title or ch.title.startswith("@")):
            ch.title = title[:300]
    settings_store.set(session, "youtube_unresolved", missing)
    session.commit()
    return added, missing


def import_in_background(session: Session, values: list[str]) -> None:
    added, missing = add_many(session, values)
    log.info("youtube import: %d added, %d not found", added, len(missing))
