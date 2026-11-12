"""The Reading page: links you save get summarized for the newsletter and, if you ask, discussed.

Flow: save a link -> fetch it (directly, or through Claude's web fetch when a site blocks the
server) -> summarize -> optionally open a board thread with your note as the first post, which the
agents answer like any of your posts -> the next newsletter has a "From your reading list" section.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import llm, settings_store, style
from app.db import utcnow
from app.models import Item, Post, ReadingItem, Thread, Topic
from app.sources.base import SourceError, dedupe_key, get, html_to_text

log = logging.getLogger(__name__)

TOPIC_SLUG = "reading"
MAX_TEXT = 30000


class _Summary(llm.Lenient):
    title: str = Field(default="", description="The article's real title")
    summary: str = Field(default="", description="3-5 plain sentences: what it says, with the key facts and numbers")
    why_it_matters: str = Field(
        default="", description="One line on why it matters to this reader in particular; empty if it doesn't"
    )


def _allowed_url(url: str) -> bool:
    """Public http(s) links only (no localhost or private network addresses)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
    except ValueError:
        return True  # a hostname


def parse_links(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s<>\"']+", text or "")
    return list(dict.fromkeys(u.rstrip(").,;") for u in urls if _allowed_url(u.rstrip(").,;"))))


def ensure_topic(session: Session) -> Topic:
    topic = session.scalar(select(Topic).where(Topic.slug == TOPIC_SLUG))
    if topic is None:
        topic = Topic(
            slug=TOPIC_SLUG, name="Reading list", priority=4,
            description="Links the owner saved to read. Discuss what the piece actually says and why it "
                        "matters to the owner; follow the owner's note when there is one.",
        )
        session.add(topic)
        session.flush()
    return topic


def add_links(session: Session, text: str, discuss: bool, instructions: str) -> list[ReadingItem]:
    rows = []
    for url in parse_links(text):
        row = ReadingItem(url=url, discuss=discuss, instructions=(instructions or "").strip()[:2000] if discuss else "")
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def _fetch(url: str) -> tuple[str, str]:
    """(title, text) fetched directly. Raises SourceError when the site blocks us or it isn't HTML."""
    resp = get(url)
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        raise SourceError(f"not a web page ({ctype or 'unknown type'})")
    soup = BeautifulSoup(resp.text, "lxml")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    text = html_to_text(resp.text, MAX_TEXT)
    if len(text) < 600:
        raise SourceError("page has too little text (paywall or script-only page)")
    return title, text


def _summarize(session: Session, row: ReadingItem) -> tuple[_Summary, str]:
    """Summary plus the article text we got (empty if Claude fetched it)."""
    profile = settings_store.get(session, "profile")
    system = (
        "You summarize articles for one busy reader. Report what the piece actually says, plainly, with "
        "its key facts and numbers. No hype, no filler, nothing the article doesn't support.\n\n"
        f"About the reader:\n{profile}\n\nWriting rules:\n{style.guide(session)}"
    )
    fake = lambda: _Summary(title=row.url, summary="(fake summary)", why_it_matters="")  # noqa: E731
    try:
        title, text = _fetch(row.url)
        user = f"URL: {row.url}\nTitle: {title}\n\nArticle text:\n{text}"
        res = llm.generate(purpose="reading_summary", role="reading", system=system, user=user,
                           max_tokens=2000, effort="low", schema=_Summary, critical=True, fake=fake)
        summary = res.parsed
        summary.title = title or summary.title  # the page's own title beats a paraphrase
    except SourceError as e:
        log.info("direct fetch failed for %s (%s); asking Claude to fetch it", row.url, e)
        text = ""
        user = (
            f"Use web_fetch to read this URL, then summarize it: {row.url}\n\n"
            'Respond with JSON only: {"title": "...", "summary": "...", "why_it_matters": "..."}. '
            "If the page can't be read (paywall, error), return an empty summary."
        )
        res = llm.generate(purpose="reading_summary", role="reading", system=system, user=user,
                           max_tokens=3000, effort="low", schema=_Summary, web_fetch=True,
                           critical=True, fake=fake)
        summary = res.parsed
    summary = style.polish(summary, purpose="reading_summary", role="reading", system=system,
                           user=f"A summary of {row.url}. Keep every fact; only fix the wording.",
                           critical=True, max_tokens=2000)
    return summary, text


def _start_discussion(session: Session, row: ReadingItem, text: str) -> Thread:
    from app import discussion

    topic = ensure_topic(session)
    body = row.summary + (f"\n\nWhy it matters: {row.why_it_matters}" if row.why_it_matters else "")
    if text:
        body += "\n\nFrom the article:\n" + text[:4000]
    item = Item(
        topic_id=topic.id, dedupe_key=dedupe_key(f"reading:{row.id}:{row.url}"), url=row.url,
        title=row.title[:1000] or row.url, body=body[:6000], status="discussed",
        extra={"source_name": "Your reading list", "source_kind": "reading"},
    )
    session.add(item)
    session.flush()
    thread = Thread(topic_id=topic.id, item_id=item.id, title=(row.title or row.url)[:200], by_user=True)
    session.add(thread)
    session.flush()
    note = row.instructions or "Worth a read. What do you make of it?"
    thread.posts.append(Post(agent_id=None, body=f"{row.url}\n\n{note}"))
    thread.last_activity_at = utcnow()
    session.commit()
    discussion.respond_to_owner(session, thread.id)
    return thread


def process(session: Session, row_id: int) -> None:
    row = session.get(ReadingItem, row_id)
    if row is None or row.status == "ready":
        return
    try:
        summary, text = _summarize(session, row)
        if not summary.summary.strip():
            raise SourceError("couldn't read the page (paywall or blocked)")
        row.title = (summary.title or row.url)[:1000]
        row.summary = summary.summary
        row.why_it_matters = summary.why_it_matters
        row.status, row.error = "ready", ""
        session.commit()
        if row.discuss and not row.thread_id:
            row.thread_id = _start_discussion(session, row, text).id
            session.commit()
    except Exception as e:  # shown on the Reading page; the link can be retried
        log.exception("reading item %s failed", row_id)
        session.rollback()
        row = session.get(ReadingItem, row_id)
        row.status, row.error = "error", f"{type(e).__name__}: {e}"[:500]
        session.commit()


def for_newsletter(session: Session) -> list[ReadingItem]:
    return list(session.scalars(
        select(ReadingItem).where(ReadingItem.status == "ready", ReadingItem.newsletter_id.is_(None))
        .order_by(ReadingItem.id)
    ))


def newsletter_material(session: Session, rows: list[ReadingItem], thread_url) -> str:
    parts = []
    for r in rows:
        block = f"### {r.title or r.url}\nLink: {r.url}\nSummary: {r.summary}"
        if r.why_it_matters:
            block += f"\nWhy it matters to the reader: {r.why_it_matters}"
        if r.instructions:
            block += f"\nThe reader's note to the board: {r.instructions}"
        thread = session.get(Thread, r.thread_id) if r.thread_id else None
        if thread is not None:
            replies = [p for p in thread.posts if p.agent is not None]
            block += f"\nBoard thread: {thread_url(thread.id)}"
            if replies:
                block += "\nBoard takes:\n" + "\n".join(f"- @{p.agent.handle}: {p.body}" for p in replies)
        parts.append(block)
    return "\n\n".join(parts)
