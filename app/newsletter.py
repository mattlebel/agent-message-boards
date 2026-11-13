"""The twice-daily recap (a Money Stuff parody) and urgent alert emails."""

from __future__ import annotations

import html as html_lib
import logging
import re
from datetime import datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, emailer, images, llm, reading, settings_store, share, style
from app.db import utcnow
from app.discussion import thread_heat
from app.models import Item, Newsletter, Source, Thread, Topic
from app.triage import AlertDecision

log = logging.getLogger(__name__)

STYLE_GUIDE = """You write a twice-daily email newsletter that recaps a private message board.
It's a loving parody of Matt Levine's "Money Stuff" from Bloomberg: the same shape and spirit, with your own
original jokes and sentences. Never copy his actual text, never claim to be him, and don't mention him.

Voice:
- Conversational, wry, deadpan, smart. You find the hidden mechanics in everything: incentives, arbitrage,
  why a rule exists, who is the "counterparty". A $1 mini-print vending machine is a market-structure story.
  A credit card offer is a structured product. A marathon lottery is an auction with weird clearing rules.
- You explain things patiently, as if to a smart friend, with "the way this works is..." moments.
- Long, winding sentences followed by short punchlines, an occasional rhetorical question, self-aware asides.
  Use these devices with intent and variety; when one starts repeating, it has become a tic.
- Footnotes are part of the voice: use markdown footnotes ([^1]) for asides and jokes. Aim for 2-5.
- Recurring bits are good. Running jokes can build over editions (e.g. "everything is points arbitrage").
- Lead with what matters most to the reader: something to act on (a drop, a deadline, a race opening, an
  offer about to expire) beats an interesting argument. Say the action, the date and the link plainly.
- Board members are sources, not the story. Quote one by @handle only when they said something genuinely
  sharp or useful. Don't narrate a thread's back-and-forth, and don't recap who agreed with whom.
- Don't write about what isn't known ("the price hasn't been posted", "nobody knows when"). Leave it out.

Structure (markdown):
1. A one-line greeting-free opener is fine, or jump straight in.
2. 1-2 main sections with "## " headings on the most important item(s): the facts first, then your take on
   the mechanics (400-700 words total).
3. "## Things happened" - a bulleted list of 6-12 short items, each one sentence with a link, often with a
   dry comment after it.
4. If the reader saved links, a "## From your reading list" section (see the material).
5. If there are dated actions, end with "## Dates" - a short list of what to do and when.

Links: link to original sources with their URLs, and to board threads with the board URLs given. Never invent URLs.
Images: when an item in the material has an "Image:" URL, you may show it once, with markdown
![a short plain description](url) on its own line right after the paragraph about that item. Use up to 4,
favoring drops, alerts, prints and places. Only use Image URLs from the material.
Facts: only use facts present in the material. Opinions and jokes are yours.
Length: 700-1,200 words, shorter on a slow day. Never pad.
Subject line: specific and wry, about the lead item. No counted lists ("five X, two Y, zero Z").

The parody's signature moves (footnotes, the patient "here's how this works" explanation, running jokes,
the odd economics metaphor) are welcome when they're doing real work. The writing rules below still apply:
a newsletter that reads like a language model imitating a columnist is the failure mode to avoid."""


class _Edition(BaseModel):
    subject: str = Field(description="Email subject line: witty, specific, under 80 characters")
    preheader: str = Field(description="Inbox preview text, under 110 characters")
    markdown: str = Field(description="The full newsletter body in markdown, with footnotes")


def keep_known_images(md: str, allowed: set[str], limit: int = 4) -> str:
    """Remove markdown images whose URL we didn't provide (no invented images), and cap the count."""
    kept = 0

    def repl(m):
        nonlocal kept
        if m.group(2) in allowed and kept < limit:
            kept += 1
            return m.group(0)
        return ""

    return re.sub(r"!\[([^\]]*)\]\((\S+?)\)", repl, md or "")


def thread_url(thread_id: int) -> str:
    return f"{config.BASE_URL}/t/{thread_id}"


def _thread_material(t: Thread, max_chars: int = 3500) -> str:
    head = f"### Thread: {t.title}\nBoard URL: {thread_url(t.id)}\nSection: {t.topic.name if t.topic else 'General'}"
    if t.item:
        head += f"\nOriginal source: {t.item.url or '(none)'}\nAbout: {t.item.title}. {t.item.body[:700]}"
        if images.for_item(t.item):
            head += f"\nImage: {images.for_item(t.item)}"
    posts = []
    for p in t.posts:
        who = f"@{p.agent.handle}" if p.agent else "@owner (the reader)"
        reaction = ""
        if p.feedback is not None:
            reaction = " [the reader liked this]" if p.feedback.value > 0 else " [the reader disliked this]"
        posts.append(f"{who}{reaction}: {p.body}")
    body = "\n\n".join(posts)
    return head + "\n\n" + body[:max_chars]


def collect(session: Session, since: datetime) -> tuple[list[Thread], list[Item]]:
    threads = list(session.scalars(select(Thread).where(Thread.last_activity_at >= since)))
    # Reading-list threads get their own section, so they aren't counted twice.
    threads = [t for t in threads if not (t.topic and t.topic.slug == reading.TOPIC_SLUG)]
    threads.sort(key=thread_heat, reverse=True)
    # Items that never got a thread but scored well (e.g. YouTube videos) for "Things happened".
    discussed = {t.item_id for t in threads if t.item_id}
    leftovers = list(
        session.scalars(
            select(Item)
            .where(Item.fetched_at >= since, Item.score >= 6, Item.status.in_(["scored", "picked"]))
            .order_by(Item.score.desc())
            .limit(15)
        )
    )
    leftovers = [i for i in leftovers if i.id not in discussed]
    return threads[:12], leftovers


def failing_sources_note(session: Session) -> str:
    """One footer line naming sources whose last fetch failed, so breakage shows up in the inbox."""
    rows = list(session.scalars(
        select(Source).join(Topic)
        .where(Source.active, Topic.active, Source.last_error != "")
        .order_by(Topic.name, Source.name)
    ))
    if not rows:
        return ""

    def reason(err: str) -> str:
        err = re.sub(r"^\w+(Error|Exception):\s*", "", err)  # drop the "SourceError: " prefix
        return err[:70].rstrip() + ("…" if len(err) > 70 else "")

    listed = "; ".join(
        f"{html_lib.escape(src.name or src.kind)} ({html_lib.escape(reason(src.last_error))})" for src in rows[:6]
    )
    more = f" and {len(rows) - 6} more" if len(rows) > 6 else ""
    return (f'<b>{len(rows)} source{"s" if len(rows) != 1 else ""} failing:</b> {listed}{more}. '
            f'<a href="{config.BASE_URL}/topics" style="color:#777;">Check Topics</a>')


def build_daily(session: Session, edition: str, critical: bool = True) -> Newsletter | None:
    last = session.scalar(
        select(Newsletter).where(Newsletter.kind == "daily", Newsletter.sent_at.is_not(None))
        .order_by(Newsletter.created_at.desc())
    )
    since = last.created_at if last else utcnow() - timedelta(hours=14)
    threads, leftovers = collect(session, since)
    saved = reading.for_newsletter(session)
    if not threads and not leftovers and not saved:
        log.info("nothing new for the newsletter")
        return None
    name = settings_store.get(session, "newsletter_name")
    owner = settings_store.get(session, "owner_name")
    profile = settings_store.get(session, "profile")
    now_et = datetime.now(config.TZ)
    recent = list(
        session.scalars(
            select(Newsletter).where(Newsletter.kind == "daily").order_by(Newsletter.created_at.desc()).limit(2)
        )
    )
    previous = "\n\n".join(f"Subject: {n.subject}\n{n.markdown[:1500]}" for n in recent)
    images.fill_missing(session, [t.item for t in threads[:6] if t.item])
    session.commit()
    allowed_images = {images.for_item(t.item) for t in threads if t.item} - {""}
    material = "\n\n---\n\n".join(_thread_material(t) for t in threads)
    extras = "\n".join(
        f"- [{i.topic.name}] {i.title} ({i.url}) {i.triage_note}" for i in leftovers
    )
    user = (
        f"Newsletter name: {name}. Edition: {'morning' if edition == 'am' else 'evening'}, "
        f"{now_et:%A %B %d, %Y}.\nThe reader is {owner}. About them:\n{profile}\n\n"
        f"BOARD THREADS since the last edition (hottest first):\n\n{material}\n\n"
        + (f"OTHER ITEMS that came in (good for 'Things happened'):\n{extras}\n\n" if extras else "")
        + (
            "THE READER'S READING LIST (links they saved to read). Give these their own section, "
            "'## From your reading list', after the main section: one short paragraph per link with "
            "what it says, why it matters to them if it does, the link, and the board's takes when "
            "there's a thread (link it).\n\n" + reading.newsletter_material(session, saved, thread_url,
                                                                            allowed_images) + "\n\n"
            if saved else ""
        )
        + (f"YOUR LAST EDITIONS (for running jokes; don't repeat the same items):\n{previous}\n" if previous else "")
    )
    system = f"{STYLE_GUIDE}\n\nWriting rules:\n{style.guide(session)}"
    res = llm.generate(
        purpose="newsletter",
        role="newsletter",
        system=system,
        user=user,
        max_tokens=12000,
        effort="medium",
        schema=_Edition,
        critical=critical,
        fake=lambda: _Edition(
            subject=f"{name}: fake edition",
            preheader="Fake preheader",
            markdown="## Fake essay\n\nSomething happened.[^1]\n\n## Things happened\n\n"
            + "\n".join(f"- [{t.title}]({thread_url(t.id)})" for t in threads)
            + "\n\n[^1]: A footnote.",
        ),
    )
    ed: _Edition = res.parsed
    ed = style.polish(ed, purpose="newsletter", role="newsletter", system=system, user=user,
                      critical=critical, long_form=True, max_tokens=12000)
    ed.markdown = keep_known_images(ed.markdown, allowed_images)
    kicker = f"{name} · {now_et:%a %b %-d} · {'Morning' if edition == 'am' else 'Evening'}"
    html = emailer.wrap(ed.subject, emailer.markdown_to_html(ed.markdown), kicker,
                        emailer.footer(failing_sources_note(session)), ed.preheader)
    key = f"daily:{now_et:%Y-%m-%d}:{edition}"
    existing = session.scalar(select(Newsletter).where(Newsletter.edition_key == key))
    if existing is not None:
        key = f"{key}:{utcnow():%H%M%S}"  # manual re-run on the same day
    nl = Newsletter(kind="daily", edition_key=key, subject=ed.subject, markdown=ed.markdown, html=html,
                    thread_ids=[t.id for t in threads])
    session.add(nl)
    session.flush()
    for r in saved:
        r.newsletter_id = nl.id
    session.commit()
    return nl


def send_newsletter(session: Session, nl: Newsletter, to: list[str] | None = None,
                    scheduled_at: datetime | None = None) -> None:
    # Board links get a share key that works for 24 hours after delivery, without signing in.
    # The stored copy keeps plain links (the admin view is behind sign-in anyway).
    html = share.tokenize_links(nl.html, scheduled_at)
    text = share.tokenize_links(nl.markdown, scheduled_at)
    try:
        nl.resend_id = emailer.send(
            to or emailer.recipients(session), nl.subject, html, text,
            scheduled_at=scheduled_at.isoformat() if scheduled_at else "",
        )
        nl.sent_at = utcnow()
        nl.error = ""
    except emailer.EmailError as e:
        nl.error = str(e)[:2000]
        log.error("send failed: %s", e)
    session.commit()


def send_alert(session: Session, item: Item, decision: AlertDecision, thread: Thread | None) -> Newsletter:
    topic = session.get(Topic, item.topic_id)
    subject = f"🚨 {topic.name}: {decision.headline or item.title}"[:150]
    rows = [
        ("What", decision.what or item.title),
        ("Where", decision.where),
        ("When", decision.when),
        ("Getting there", decision.travel),
    ]
    body = ""
    picture = images.for_item(item)
    if not picture and item.url:
        picture = images.fetch_preview(item.url)
        if picture:
            item.extra = {**(item.extra or {}), "image": picture}
    if picture:
        body += f'<p><img src="{html_lib.escape(picture)}" alt="{html_lib.escape(item.title[:120])}"></p>'
    body += "".join(
        f"<p><b>{html_lib.escape(k)}:</b> {html_lib.escape(v)}</p>" for k, v in rows if v
    )
    if item.url:
        body += f'<p><a href="{html_lib.escape(item.url)}">Original source</a></p>'
    if thread:
        body += f'<p><a href="{thread_url(thread.id)}">What the board is saying</a></p>'
    name = settings_store.get(session, "newsletter_name")
    html = emailer.wrap(subject, body, f"{name} · Alert", emailer.footer())
    text = "\n".join(f"{k}: {v}" for k, v in rows if v) + f"\n{item.url}"
    nl = Newsletter(kind="alert", subject=subject, markdown=text, html=html,
                    thread_ids=[thread.id] if thread else [])
    session.add(nl)
    session.flush()
    send_newsletter(session, nl)
    return nl
