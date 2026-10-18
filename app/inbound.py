"""Newsletters you forward (or subscribe) to the inbox address become board items.

Route by address: <topic-slug>@INBOUND_DOMAIN (e.g. amex@in.lebel.io) goes straight to that
topic. Anything else (e.g. feeds@in.lebel.io) is sorted by Claude.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, llm, settings_store
from app.models import InboundEmail, Item, Topic
from app.sources.base import dedupe_key, html_to_text

log = logging.getLogger(__name__)


def fetch_received_email(email_id: str) -> dict:
    import resend

    resend.api_key = config.RESEND_API_KEY
    return dict(resend.Emails.Receiving.get(email_id))


def _topic_from_address(session: Session, addresses: list[str]) -> Topic | None:
    topics = list(session.scalars(select(Topic).where(Topic.active)))
    for addr in addresses:
        local = addr.split("@")[0].lower()
        local = local.split("+", 1)[1] if "+" in local else local
        for t in topics:
            if local == t.slug or local.replace("_", "-") == t.slug:
                return t
    return None


class _Route(BaseModel):
    topic_slug: str = Field(description="One of the listed slugs, or 'none' if it fits nothing")


class _Extracted(BaseModel):
    title: str
    url: str = ""
    summary: str = Field(default="", description="2-3 factual sentences, keep numbers/prices/dates")


class _ExtractedList(BaseModel):
    items: list[_Extracted] = Field(default_factory=list)


def process_email(session: Session, row: InboundEmail, email: dict) -> None:
    text = email.get("text") or html_to_text(email.get("html") or "", 40000)
    text = text[:40000]
    row.subject = (email.get("subject") or row.subject or "")[:1000]
    row.from_addr = str(email.get("from") or row.from_addr)
    row.text = text[:20000]

    addresses = list(email.get("to") or []) + list(email.get("received_for") or []) + list(row.to_addrs or [])
    topic = _topic_from_address(session, addresses)
    topics = list(session.scalars(select(Topic).where(Topic.active)))
    if topic is None and topics:
        listing = "\n".join(f"- {t.slug}: {t.name}. {t.description[:200]}" for t in topics)
        res = llm.generate(
            purpose="inbound_route",
            role="fast",
            system="You file forwarded emails into the right topic of a personal message board.",
            user=f"Topics:\n{listing}\n\nEmail subject: {row.subject}\nFrom: {row.from_addr}\n\n{text[:6000]}",
            max_tokens=500,
            effort="low",
            schema=_Route,
            fake=lambda: _Route(topic_slug=topics[0].slug),
        )
        topic = next((t for t in topics if t.slug == res.parsed.topic_slug), None)
    if topic is None:
        row.status = "ignored"
        row.error = "no matching topic"
        return
    row.topic_id = topic.id

    profile = settings_store.get(session, "profile")
    res = llm.generate(
        purpose="inbound_extract",
        role="fast",
        system=(
            "You split a newsletter or notification email into separate news items for a message board. "
            "Keep only items relevant to the topic and the owner. Skip ads, footers, and unsubscribe junk. "
            "Use the real article/deal link when present.\n\nAbout the owner:\n" + profile
        ),
        user=f"Topic: {topic.name}: {topic.description}\n\nSubject: {row.subject}\nFrom: {row.from_addr}\n\n{text}",
        max_tokens=4000,
        effort="low",
        schema=_ExtractedList,
        fake=lambda: _ExtractedList(items=[_Extracted(title=row.subject or "Forwarded email", summary=text[:300])]),
    )
    created = 0
    for ex in res.parsed.items[:15]:
        key = dedupe_key(f"email:{row.resend_id}:{ex.url or ex.title}")
        if session.scalar(select(Item.id).where(Item.dedupe_key == key)):
            continue
        session.add(
            Item(
                topic_id=topic.id,
                dedupe_key=key,
                url=ex.url if re.match(r"https?://", ex.url or "") else "",
                title=ex.title[:1000],
                body=ex.summary[:4000],
                author=row.from_addr[:200],
                extra={"source_name": f"Email: {row.subject[:80]}", "source_kind": "email"},
            )
        )
        created += 1
    row.items_created = created
    row.status = "processed"


def record_webhook_event(session: Session, event: dict) -> InboundEmail | None:
    """Store the email right away; processing happens in the background."""
    if event.get("type") != "email.received":
        return None
    data = event.get("data") or {}
    email_id = data.get("email_id") or data.get("id")
    if not email_id:
        return None
    row = session.scalar(select(InboundEmail).where(InboundEmail.resend_id == email_id))
    if row is not None:
        return row  # webhook retry
    row = InboundEmail(
        resend_id=email_id,
        from_addr=str(data.get("from", "")),
        to_addrs=list(data.get("to") or []),
        subject=str(data.get("subject", "")),
        text=str(data.get("text", ""))[:20000],
    )
    session.add(row)
    session.flush()
    return row


def process_inbound(session: Session, row: InboundEmail) -> None:
    try:
        if config.LLM_FAKE or not config.RESEND_API_KEY:
            email = {"subject": row.subject, "text": row.text, "to": row.to_addrs}
        else:
            email = fetch_received_email(row.resend_id)
        process_email(session, row, email)
    except Exception as e:
        log.exception("inbound email failed")
        row.status = "error"
        row.error = repr(e)[:2000]
    session.commit()


def process_pending(session: Session) -> int:
    rows = list(session.scalars(select(InboundEmail).where(InboundEmail.status == "new")))
    for row in rows:
        process_inbound(session, row)
    return len(rows)
