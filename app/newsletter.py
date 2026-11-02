"""The twice-daily recap (a Money Stuff parody) and urgent alert emails."""

from __future__ import annotations

import html as html_lib
import logging
from datetime import datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, emailer, llm, settings_store, style
from app.db import utcnow
from app.discussion import thread_heat
from app.models import Item, Newsletter, Thread, Topic
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
- Footnotes are essential: use markdown footnotes ([^1]) for asides, caveats, and jokes. Aim for 4-8.
- Recurring bits are good. Running jokes can build over editions (e.g. "everything is points arbitrage").
- Treat board members as your sources: quote them by @handle like reported quotes ("As @rosa_runs put it, ...").
  The members are AI personas on the board; you can describe them as "board members" or "regulars".
- Be useful to the reader: if there's an action (sign up by Friday, offer expires, a drop worth a trip), say so plainly.

Structure (markdown):
1. A one-line greeting-free opener is fine, or jump straight in.
2. 1-2 main sections with "## " headings: take the most interesting board thread(s) and riff on them at length
   (the main essay ~600-900 words total).
3. "## Things happened" - a bulleted list of 6-12 short items, each one sentence with a link, often with a
   dry comment after it.
4. Optionally a short closing section (e.g. "## Housekeeping" for upcoming deadlines or the reader's races).

Links: link to original sources with their URLs, and to board threads with the board URLs given. Never invent URLs.
Facts: only use facts present in the material. Opinions and jokes are yours.
Length: 1,000-1,500 words.

The parody's signature moves (footnotes, the patient "here's how this works" explanation, running jokes,
the odd economics metaphor) are welcome when they're doing real work. The writing rules below still apply:
a newsletter that reads like a language model imitating a columnist is the failure mode to avoid."""


class _Edition(BaseModel):
    subject: str = Field(description="Email subject line: witty, specific, under 80 characters")
    preheader: str = Field(description="Inbox preview text, under 110 characters")
    markdown: str = Field(description="The full newsletter body in markdown, with footnotes")


def thread_url(thread_id: int) -> str:
    return f"{config.BASE_URL}/t/{thread_id}"


def _thread_material(t: Thread, max_chars: int = 3500) -> str:
    head = f"### Thread: {t.title}\nBoard URL: {thread_url(t.id)}\nSection: {t.topic.name if t.topic else 'General'}"
    if t.item:
        head += f"\nOriginal source: {t.item.url or '(none)'}\nAbout: {t.item.title}. {t.item.body[:700]}"
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


def build_daily(session: Session, edition: str, critical: bool = True) -> Newsletter | None:
    last = session.scalar(
        select(Newsletter).where(Newsletter.kind == "daily", Newsletter.sent_at.is_not(None))
        .order_by(Newsletter.created_at.desc())
    )
    since = last.created_at if last else utcnow() - timedelta(hours=14)
    threads, leftovers = collect(session, since)
    if not threads and not leftovers:
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
    material = "\n\n---\n\n".join(_thread_material(t) for t in threads)
    extras = "\n".join(
        f"- [{i.topic.name}] {i.title} ({i.url}) {i.triage_note}" for i in leftovers
    )
    user = (
        f"Newsletter name: {name}. Edition: {'morning' if edition == 'am' else 'evening'}, "
        f"{now_et:%A %B %d, %Y}.\nThe reader is {owner}. About them:\n{profile}\n\n"
        f"BOARD THREADS since the last edition (hottest first):\n\n{material}\n\n"
        + (f"OTHER ITEMS that came in (good for 'Things happened'):\n{extras}\n\n" if extras else "")
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
    tropes = style.find_tropes(f"{ed.subject}\n{ed.markdown}", long_form=True)
    if len(tropes) >= 2:
        try:
            ed = llm.generate(
                purpose="newsletter_revise", role="newsletter", system=system,
                user=f"{user}\n\nYour draft:\n{ed.model_dump_json()}\n\n{style.revision_note(tropes)}",
                max_tokens=12000, effort="low", schema=_Edition, critical=critical, fake=lambda: ed,
            ).parsed
        except llm.LlmError:
            pass  # keep the first draft
    kicker = f"{name} · {now_et:%a %b %-d} · {'Morning' if edition == 'am' else 'Evening'}"
    html = emailer.wrap(ed.subject, emailer.markdown_to_html(ed.markdown), kicker, emailer.footer(), ed.preheader)
    key = f"daily:{now_et:%Y-%m-%d}:{edition}"
    existing = session.scalar(select(Newsletter).where(Newsletter.edition_key == key))
    if existing is not None:
        key = f"{key}:{utcnow():%H%M%S}"  # manual re-run on the same day
    nl = Newsletter(kind="daily", edition_key=key, subject=ed.subject, markdown=ed.markdown, html=html,
                    thread_ids=[t.id for t in threads])
    session.add(nl)
    session.commit()
    return nl


def send_newsletter(session: Session, nl: Newsletter, to: list[str] | None = None,
                    scheduled_at: datetime | None = None) -> None:
    try:
        nl.resend_id = emailer.send(
            to or emailer.recipients(session), nl.subject, nl.html, nl.markdown,
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
    body = "".join(
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
