"""Two inbound addresses that act for you, so they only accept mail from trusted senders.

- read@<inbound domain>: email or forward a link and it goes on the Reading list (summarized for the next
  newsletter). Put "discuss" in the subject to also open a board thread; anything you write above the
  link becomes your note to the agents.
- ask@<inbound domain>: the daily newsletter's Reply-To. Reply with a question and it becomes your post in
  an "Ask the board" thread; the agents answer, and the next edition answers you up top.

Trusted senders: addresses on ALLOWED_EMAIL_DOMAIN, plus any listed under Settings → Trusted senders.
Anything else is ignored (and shown as such in the Inbox).
"""

from __future__ import annotations

import logging
import re
from email.utils import parseaddr

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, llm, reading, settings_store
from app.db import utcnow
from app.models import InboundEmail, Item, Newsletter, Post, Thread, Topic
from app.sources.base import dedupe_key

log = logging.getLogger(__name__)

READ_ADDRESSES = {"read", "reading"}
ASK_ADDRESSES = {"ask"}
ASK_SLUG = "ask"


def _locals(addresses: list[str]) -> set[str]:
    out = set()
    for a in addresses:
        local = parseaddr(str(a))[1].split("@")[0].lower()
        out.add(local.split("+", 1)[0])
    return out


def command_for(addresses: list[str]) -> str:
    """'read', 'ask' or '' for the addresses an email was sent to."""
    locs = _locals(addresses)
    if locs & READ_ADDRESSES:
        return "read"
    if locs & ASK_ADDRESSES:
        return "ask"
    return ""


def ask_address() -> str:
    return f"ask@{config.INBOUND_DOMAIN}"


def trusted(session: Session, from_addr: str) -> bool:
    email = parseaddr(from_addr or "")[1].lower()
    if not email or "@" not in email:
        return False
    extra = {e.strip().lower() for e in (settings_store.get(session, "trusted_senders") or []) if e.strip()}
    domain = config.ALLOWED_EMAIL_DOMAIN
    return email in extra or (bool(domain) and email.endswith("@" + domain))


# --- quoting ------------------------------------------------------------------------------

_QUOTE_START = re.compile(
    r"^(On [^\n]{5,200}(?:\n[^\n]{0,120})?wrote:\s*$|-{2,}\s*Original Message\s*-{2,}|-{5,}\s*Forwarded message|Begin forwarded message:"
    r"|From: .+$)", re.I | re.M)


def own_words(text: str) -> str:
    """What the sender wrote, without the quoted email below it or '>' lines."""
    m = _QUOTE_START.search(text or "")
    head = text[:m.start()] if m else (text or "")
    lines = [ln for ln in head.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


# --- read@ --------------------------------------------------------------------------------


class _Pick(llm.Lenient):
    urls: list[str] = Field(default_factory=list, description="The 1-3 links the sender wants to read")


def _pick_links(subject: str, text: str, links: list[str]) -> list[str]:
    if len(links) <= 3:
        return links
    res = llm.generate(
        purpose="read_email_links", role="fast",
        system="Someone forwarded an email to their reading list. Pick the article link(s) they meant to save: "
               "the main story or the link they pasted, never unsubscribe, tracking, social or footer links.",
        user=f"Subject: {subject}\n\n{text[:6000]}\n\nLinks:\n" + "\n".join(links[:80]),
        max_tokens=600, effort="low", schema=_Pick, critical=True, fake=lambda: _Pick(urls=links[:1]),
    )
    allowed = set(links)
    return [u for u in res.parsed.urls if u in allowed][:3] or links[:1]


def handle_read(session: Session, row: InboundEmail, text: str) -> list[int]:
    """Add the email's link(s) to the Reading list. Returns the new ReadingItem ids (processed by the caller)."""
    mine = own_words(text)
    links = reading.parse_links(mine) or reading.parse_links(text)
    if not links:
        row.status, row.error = "ignored", "no link found"
        return []
    chosen = _pick_links(row.subject, text, links)
    discuss = "discuss" in (row.subject or "").lower() or bool(re.match(r"\s*discuss\b", mine, re.I))
    note = re.sub(r"https?://\S+", "", mine)
    note = re.sub(r"^\s*discuss\b[:\s-]*", "", note, flags=re.I).strip()
    rows = reading.add_links(session, "\n".join(chosen), discuss, note[:2000])
    row.items_created = len(rows)
    row.status = "processed"
    session.commit()
    return [r.id for r in rows]


# --- ask@ ---------------------------------------------------------------------------------


def ensure_ask_topic(session: Session) -> Topic:
    topic = session.scalar(select(Topic).where(Topic.slug == ASK_SLUG))
    if topic is None:
        topic = Topic(slug=ASK_SLUG, name="Ask the board", priority=5,
                      description="Questions the owner sent by replying to the newsletter. Answer the question "
                                  "directly, using the edition they replied to for context.")
        session.add(topic)
        session.flush()
    return topic


def _replied_to(session: Session, subject: str) -> Newsletter | None:
    base = re.sub(r"^\s*((re|fwd?|aw)\s*:\s*)+", "", subject or "", flags=re.I).strip()
    if not base:
        return None
    return session.scalar(select(Newsletter).where(Newsletter.subject == base, Newsletter.kind == "daily")
                          .order_by(Newsletter.id.desc()).limit(1))


def handle_ask(session: Session, row: InboundEmail, text: str) -> int | None:
    """Open an 'Ask the board' thread with the owner's question. Returns the thread id (the caller gets replies)."""
    question = own_words(text)
    if not question:
        row.status, row.error = "ignored", "empty question"
        return None
    topic = ensure_ask_topic(session)
    edition = _replied_to(session, row.subject)
    first_line = next((ln.strip() for ln in question.splitlines() if ln.strip()), "Question")
    title = first_line if len(first_line) <= 90 else first_line[:87].rsplit(" ", 1)[0] + "…"
    item = Item(
        topic_id=topic.id, dedupe_key=dedupe_key(f"ask:{row.resend_id}"),
        title=f"Reply to “{edition.subject}”" if edition else "Question by email",
        body=(f"The owner replied to this newsletter edition:\n\n{edition.markdown[:5000]}" if edition
              else "The owner emailed this question."),
        status="discussed", extra={"source_name": "Reply to the newsletter", "source_kind": "ask",
                                   **({"newsletter_id": edition.id} if edition else {})},
    )
    session.add(item)
    session.flush()
    thread = Thread(topic_id=topic.id, item_id=item.id, title=title[:200], by_user=True)
    session.add(thread)
    session.flush()
    thread.posts.append(Post(agent_id=None, body=question[:6000]))
    thread.last_activity_at = utcnow()
    row.topic_id = topic.id
    row.items_created = 1
    row.status = "processed"
    session.commit()
    return thread.id


def handle(session: Session, row: InboundEmail, text: str, command: str) -> None:
    """Entry point from inbound.process_email for read@ and ask@."""
    if not trusted(session, row.from_addr):
        row.status, row.error = "ignored", f"{command}@ only accepts mail from trusted senders"
        log.info("ignored %s@ email from %s", command, row.from_addr)
        return
    if command == "read":
        ids = handle_read(session, row, text)
        for rid in ids:
            reading.process(session, rid)
    elif command == "ask":
        thread_id = handle_ask(session, row, text)
        if thread_id:
            from app import discussion

            discussion.respond_to_owner(session, thread_id)
