"""Score new items, pick what the agents discuss, and catch urgent drops."""

from __future__ import annotations

import logging
import random
from collections import defaultdict
from datetime import datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, llm, settings_store
from app.db import utcnow
from app.models import Item, Topic

log = logging.getLogger(__name__)


def item_brief(item: Item, body_chars: int = 600) -> str:
    extra = item.extra or {}
    bits = [f"[id {item.id}] {item.title}"]
    meta = []
    if extra.get("source_name"):
        meta.append(f"source: {extra['source_name']}")
    for k in ("score", "num_comments", "price", "channel", "flair"):
        if extra.get(k) not in (None, ""):
            meta.append(f"{k}: {extra[k]}")
    if item.published_at:
        meta.append(f"published: {item.published_at:%Y-%m-%d %H:%M} UTC")
    if meta:
        bits.append("(" + ", ".join(meta) + ")")
    if item.url:
        bits.append(item.url)
    if item.body:
        bits.append(item.body[:body_chars].replace("\n", " "))
    return "\n".join(bits)


class _Score(BaseModel):
    id: int
    score: float = Field(description="0-10: how much the owner would care and how good it is to discuss")
    note: str = Field(description="One short line: why it matters or why not")


class _Scores(BaseModel):
    items: list[_Score]


def triage(session: Session) -> int:
    """Score every unscored item. Returns how many were scored."""
    stale = utcnow() - timedelta(days=4)
    for item in session.scalars(select(Item).where(Item.status == "new", Item.fetched_at < stale)):
        item.status = "skipped"
    items = list(
        session.scalars(
            select(Item).join(Topic).where(Item.status == "new", Topic.active).order_by(Item.id)
        )
    )
    if not items:
        return 0
    profile = settings_store.get(session, "profile")
    by_topic: dict[int, list[Item]] = defaultdict(list)
    for it in items:
        by_topic[it.topic_id].append(it)

    scored = 0
    for topic_id, group in by_topic.items():
        topic = session.get(Topic, topic_id)
        for start in range(0, len(group), 30):
            chunk = group[start : start + 30]
            system = (
                "You triage incoming items for one person's private message board. Score each item "
                "0-10 for how much this specific person would care, and how much there is to talk about. "
                "Be harsh: duplicates, spam, generic evergreen pages, ads, and stale news score 0-3. "
                "Concrete news, deals with real value, upcoming races/events, drops, and lively community "
                "threads score high.\n\nAbout the owner:\n" + profile
            )
            user = (
                f"Today is {datetime.now(config.TZ):%A %B %d, %Y}. Score down anything whose dates have "
                "already passed (deadlines, registration windows, events, expired offers).\n"
                f"Topic: {topic.name}\nWhat the owner wants from this topic: {topic.description}\n\n"
                "Items:\n\n" + "\n\n".join(item_brief(i) for i in chunk)
            )
            try:
                res = llm.generate(
                    purpose="triage",
                    role="fast",
                    system=system,
                    user=user,
                    max_tokens=3000,
                    effort="low",
                    schema=_Scores,
                    fake=lambda c=chunk: _Scores(
                        items=[_Score(id=i.id, score=random.uniform(3, 9), note="fake triage") for i in c]
                    ),
                )
            except llm.BudgetExceeded:
                log.warning("budget reached during triage")
                session.commit()
                return scored
            by_id = {s.id: s for s in res.parsed.items}
            for it in chunk:
                s = by_id.get(it.id)
                if s is None:
                    continue
                it.score = max(0.0, min(10.0, s.score))
                it.triage_note = s.note[:500]
                it.status = "scored"
                scored += 1
            session.commit()
    return scored


def pick_for_discussion(session: Session, n: int) -> list[Item]:
    """Best items across topics, spread out so one topic can't take every slot."""
    since = utcnow() - timedelta(days=3)
    candidates = list(
        session.scalars(
            select(Item)
            .join(Topic)
            .where(Item.status == "scored", Item.score >= 7, Item.fetched_at >= since, Topic.active)
        )
    )
    def rank(i: Item) -> float:
        return (i.score or 0) + (i.topic.priority - 3) * 0.6

    by_topic: dict[int, list[Item]] = defaultdict(list)
    for it in sorted(candidates, key=rank, reverse=True):
        by_topic[it.topic_id].append(it)
    picked: list[Item] = []
    # Round-robin through topics, best topic first.
    queues = sorted(by_topic.values(), key=lambda q: rank(q[0]), reverse=True)
    while len(picked) < n and any(queues):
        for q in queues:
            if q and len(picked) < n:
                picked.append(q.pop(0))
    for it in picked:
        it.status = "picked"
    session.commit()
    return picked


# --- urgent drops ------------------------------------------------------------------


class AlertDecision(BaseModel):
    id: int
    is_alert: bool
    headline: str = Field(default="", description="Short punchy headline if it's an alert")
    what: str = Field(default="", description="What dropped / was announced, factually")
    where: str = Field(default="", description="Location (venue, city) if known")
    when: str = Field(default="", description="Dates / how long it's available, if known")
    travel: str = Field(default="", description="How far from Back Bay Boston and whether it's a realistic same-week trip")


class _AlertDecisions(BaseModel):
    decisions: list[AlertDecision]


def pending_urgent(session: Session) -> list[Item]:
    """Recent items in alert topics that haven't had the alert check yet, however they arrived
    (scheduled fetch, "Fetch now", a forwarded email)."""
    since = utcnow() - timedelta(days=3)
    rows = session.scalars(
        select(Item).join(Topic)
        .where(Topic.urgent, Topic.active, Item.fetched_at >= since,
               Item.status.in_(["new", "scored", "picked", "discussed"]))
    )
    return [i for i in rows if not (i.extra or {}).get("urgent_checked")]


def check_urgent(session: Session, items: list[Item]) -> list[tuple[Item, AlertDecision]]:
    """For items in urgent topics: is this a drop worth an immediate email?"""
    urgent_items = [
        i for i in items
        if i.topic and i.topic.urgent and i.status in ("new", "scored", "picked", "discussed")
        and not (i.extra or {}).get("urgent_checked")
    ]
    if not urgent_items:
        return []
    profile = settings_store.get(session, "profile")
    out: list[tuple[Item, AlertDecision]] = []
    by_topic: dict[int, list[Item]] = defaultdict(list)
    for it in urgent_items:
        by_topic[it.topic_id].append(it)
    for topic_id, group in by_topic.items():
        topic = session.get(Topic, topic_id)
        system = (
            "You decide whether incoming items are urgent enough to email the owner right now. "
            "Only say yes for genuinely new, time-sensitive things that match the alert criteria. "
            "Routine posts, resales of common items, and old news are not alerts. "
            "For travel, assume the owner leaves from Back Bay, Boston.\n\nAbout the owner:\n" + profile
        )
        user = (
            f"Today is {datetime.now(config.TZ):%A %B %d, %Y}.\nTopic: {topic.name}\n"
            f"Alert criteria: {topic.urgent_criteria or topic.description}\n\nItems:\n\n"
            + "\n\n".join(item_brief(i, 1500) for i in group)
        )
        res = llm.generate(
            purpose="urgent_check",
            role="alerts",
            system=system,
            user=user,
            max_tokens=3000,
            effort="medium",
            schema=_AlertDecisions,
            critical=True,
            fake=lambda g=group: _AlertDecisions(
                decisions=[AlertDecision(id=i.id, is_alert=False) for i in g]
            ),
        )
        by_id = {d.id: d for d in res.parsed.decisions}
        for it in group:
            it.extra = {**(it.extra or {}), "urgent_checked": True}
            d = by_id.get(it.id)
            if d and d.is_alert:
                it.status = "alerted"
                it.score = 10.0
                it.extra = {**(it.extra or {}), "alert": d.model_dump()}
                out.append((it, d))
    session.commit()
    return out
