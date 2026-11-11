"""Fetch sources and store new items (deduplicated)."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import Item, Source, Topic
from app.sources import FETCHERS
from app.sources.base import dedupe_key, to_naive_utc

log = logging.getLogger(__name__)


def fetch_one(session: Session, source: Source) -> list[Item]:
    fetcher = FETCHERS.get(source.kind)
    source.last_fetched_at = utcnow()
    if fetcher is None:
        source.last_error = f"unknown source kind {source.kind!r}"
        return []
    try:
        raws = fetcher(source, session)
    except Exception as e:  # one broken source must not stop the others
        log.warning("source %s (%s) failed: %s", source.id, source.kind, e)
        source.last_error = f"{type(e).__name__}: {e}"[:1000]
        session.commit()
        return []

    max_age = timedelta(days=float((source.config or {}).get("max_age_days", 7)))
    cutoff = utcnow() - max_age
    recent_cutoff = utcnow() - timedelta(days=14)
    new: list[Item] = []
    seen_keys: set[str] = set()
    for r in raws:
        published = to_naive_utc(r.published_at)
        if published and published < cutoff:
            continue
        key = dedupe_key(r.key)
        if key in seen_keys or session.scalar(select(Item.id).where(Item.dedupe_key == key)):
            continue
        if r.url and session.scalar(
            select(Item.id).where(Item.url == r.url, Item.fetched_at >= recent_cutoff).limit(1)
        ):
            continue  # same link already came in through another source
        seen_keys.add(key)
        item = Item(
            topic_id=source.topic_id,
            source_id=source.id,
            dedupe_key=key,
            url=r.url,
            title=(r.title or "")[:1000],
            body=(r.body or "")[:6000],
            author=(r.author or "")[:200],
            published_at=published,
            extra={**(r.extra or {}), "source_name": source.name, "source_kind": source.kind},
        )
        session.add(item)
        new.append(item)
    source.last_ok_at = utcnow()
    source.last_error = ""
    source.last_count = len(new)
    session.commit()
    return new


def due_sources(session: Session, hot_only: bool) -> list[Source]:
    q = (
        select(Source)
        .join(Topic)
        .where(Source.active, Topic.active)
        .order_by(Source.id)
    )
    if hot_only:
        q = q.where(Source.interval_minutes.is_not(None))
    now = utcnow()
    out = []
    for s in session.scalars(q):
        # Sources with an interval are never fetched more often than that, in either mode.
        # A source whose last fetch failed is retried after at most 30 minutes.
        wait = s.interval_minutes
        if wait and s.last_error:
            wait = min(wait, 30)
        if wait and s.last_fetched_at and s.last_fetched_at > now - timedelta(minutes=wait):
            continue
        out.append(s)
    return out


def run_ingest(session: Session, hot_only: bool = False) -> list[Item]:
    new: list[Item] = []
    for source in due_sources(session, hot_only):
        new += fetch_one(session, source)
    log.info("ingest (hot_only=%s): %d new items", hot_only, len(new))
    return new
