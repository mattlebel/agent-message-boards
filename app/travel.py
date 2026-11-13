"""Trip reports from One Mile at a Time for the regions you list on the Travel page.

Each OMAAT post is classified once (flight, lounge or hotel review, or none) with the places it
covers, from city up to continent. A review matches a region when one of its places matches the
region's name as whole words, so "Asia" matches "East Asia" and "Tokyo" matches "Tokyo Haneda".
Adding a region also searches OMAAT's archive for older reviews of it.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import feedparser
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import images, llm, settings_store
from app.db import utcnow
from app.models import Item, Source, Topic
from app.sources.base import dedupe_key, get, html_to_text, to_naive_utc

log = logging.getLogger(__name__)

OMAAT = "onemileatatime.com"
TYPES = {"flight": "Flights", "lounge": "Lounges", "hotel": "Hotels"}


class _Review(llm.Lenient):
    id: int
    review_type: str = Field(description="flight, lounge, hotel, or none (news, deals, opinion, card posts)")
    places: list[str] = Field(default_factory=list, description=(
        "Where it is, specific to broad: city, airport code, country, region, continent "
        "(e.g. Tokyo, HND, Japan, East Asia, Asia). For a flight, include both ends."))
    subject: str = Field(default="", description="Airline and cabin, lounge, or hotel name")


class _Reviews(llm.Lenient):
    items: list[_Review] = Field(default_factory=list)


def regions(session: Session) -> list[str]:
    return [r for r in (settings_store.get(session, "travel_regions") or []) if r.strip()]


def is_omaat(item: Item) -> bool:
    return OMAAT in (item.url or "")


def _word_match(a: str, b: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(a)}(?!\w)", b, re.I))


def matched_regions(item: Item, wanted: list[str]) -> list[str]:
    review = (item.extra or {}).get("review") or {}
    if review.get("type") not in TYPES:
        return []
    places = review.get("places") or []
    return [r for r in wanted if any(_word_match(r, p) or _word_match(p, r) for p in places)]


def classify(session: Session, items: list[Item]) -> int:
    from app.triage import item_brief

    todo = [i for i in items if is_omaat(i) and not (i.extra or {}).get("review")]
    for start in range(0, len(todo), 30):
        chunk = todo[start:start + 30]
        res = llm.generate(
            purpose="travel_classify", role="fast",
            system="You sort travel blog posts. Only actual reviews of a flight, lounge or hotel count as reviews.",
            user="Posts:\n\n" + "\n\n".join(item_brief(i, 500) for i in chunk),
            max_tokens=3000, effort="low", schema=_Reviews, critical=True,
            fake=lambda c=chunk: _Reviews(items=[_Review(id=i.id, review_type="none") for i in c]),
        )
        by_id = {r.id: r for r in res.parsed.items}
        for it in chunk:
            r = by_id.get(it.id)
            kind = (r.review_type or "none").lower().strip() if r else "none"
            it.extra = {**(it.extra or {}), "review": {
                "type": kind if kind in TYPES else "none",
                "places": [p for p in (r.places if r else []) if p][:10],
                "subject": (r.subject if r else "")[:200],
            }}
        session.commit()
    return len(todo)


def classify_new(session: Session) -> int:
    since = utcnow() - timedelta(days=3)
    rows = list(session.scalars(select(Item).where(Item.fetched_at >= since)))
    return classify(session, rows)


def _omaat_source(session: Session) -> tuple[int | None, int | None]:
    for src in session.scalars(select(Source).where(Source.kind == "rss")):
        if OMAAT in str((src.config or {}).get("url", "")):
            return src.topic_id, src.id
    topic = session.scalar(select(Topic).where(Topic.slug == "travel"))
    return (topic.id if topic else None), None


def backfill(session: Session, region: str, limit: int = 30) -> int:
    """Search OMAAT's archive for reviews of a region (WordPress search feed) and file them."""
    topic_id, source_id = _omaat_source(session)
    if topic_id is None:
        return 0
    added: list[Item] = []
    for query in (f"{region} review", region):
        try:
            resp = get(f"https://{OMAAT}/?s={quote_plus(query)}&feed=rss2")
        except Exception as e:
            log.info("OMAAT search failed for %s: %s", query, e)
            continue
        for e in feedparser.parse(resp.content).entries[:limit]:
            link = e.get("link", "")
            key = dedupe_key(f"rss:{e.get('id') or link}")
            if not link or session.scalar(select(Item.id).where(Item.dedupe_key == key)):
                continue
            published = None
            for attr in ("published_parsed", "updated_parsed"):
                if e.get(attr):
                    published = to_naive_utc(datetime(*e[attr][:6]))
                    break
            image = images.from_feed_entry(e)
            item = Item(topic_id=topic_id, source_id=source_id, dedupe_key=key, url=link,
                        title=(e.get("title") or "")[:1000], body=html_to_text(e.get("summary", "") or "", 2500),
                        published_at=published, status="archive",  # past reviews: for the Travel page only
                        extra={"source_name": "One Mile at a Time", "source_kind": "rss",
                               **({"image": image} if image else {})})
            session.add(item)
            added.append(item)
        session.flush()
    session.commit()
    classify(session, added)
    return len(added)


def backfill_new_regions(session: Session) -> None:
    done = set(settings_store.get(session, "travel_backfilled") or [])
    for region in regions(session):
        if region.lower() in done:
            continue
        n = backfill(session, region)
        log.info("travel backfill %s: %d posts", region, n)
        done.add(region.lower())
        settings_store.set(session, "travel_backfilled", sorted(done))
        session.commit()


def page_data(session: Session) -> dict[str, dict[str, list[Item]]]:
    """{region: {type: [reviews, newest first]}} for the Travel page."""
    wanted = regions(session)
    out: dict[str, dict[str, list[Item]]] = {r: defaultdict(list) for r in wanted}
    if not wanted:
        return out
    rows = [i for i in session.scalars(select(Item)) if is_omaat(i) and matched_regions(i, wanted)]
    rows.sort(key=lambda i: i.published_at or i.fetched_at, reverse=True)
    for it in rows:
        kind = (it.extra or {}).get("review", {}).get("type")
        for r in matched_regions(it, wanted):
            if len(out[r][kind]) < 12:
                out[r][kind].append(it)
    return out


def for_newsletter(session: Session) -> list[Item]:
    """New (not archive) reviews from your regions that haven't been in a newsletter."""
    wanted = regions(session)
    if not wanted:
        return []
    since = utcnow() - timedelta(days=3)
    rows = list(session.scalars(select(Item).where(Item.fetched_at >= since, Item.status != "archive")))
    return [i for i in rows if is_omaat(i) and not (i.extra or {}).get("newsletter_reviews")
            and matched_regions(i, wanted)][:8]


def newsletter_material(items: list[Item], wanted: list[str]) -> str:
    lines = []
    for it in items:
        review = (it.extra or {}).get("review") or {}
        line = (f"- {TYPES.get(review.get('type'), 'Review')[:-1]} review ({', '.join(matched_regions(it, wanted))}): "
                f"{it.title} ({it.url}) | {review.get('subject', '')} | {(it.body or '')[:300]}")
        if images.for_item(it):
            line += f" | Image: {images.for_item(it)}"
        lines.append(line.replace("\n", " "))
    return "\n".join(lines)
