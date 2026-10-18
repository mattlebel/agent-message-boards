from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app import config


@dataclass
class RawItem:
    key: str  # stable unique id within the source kind, e.g. "reddit:abc123" or a URL
    title: str
    url: str = ""
    body: str = ""
    author: str = ""
    published_at: Optional[datetime] = None
    extra: dict = field(default_factory=dict)


class SourceError(Exception):
    pass


def http() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": config.USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        timeout=httpx.Timeout(25.0, connect=10.0),
        follow_redirects=True,
    )


def get(url: str, **kwargs) -> httpx.Response:
    with http() as c:
        resp = c.get(url, **kwargs)
    if resp.status_code >= 400:
        raise SourceError(f"HTTP {resp.status_code} from {url}")
    return resp


def html_to_text(html: str, limit: int = 4000) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]


def dedupe_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
