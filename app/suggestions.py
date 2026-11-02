"""Sunday email: new topics you might never think to add, each one click away."""

from __future__ import annotations

import html as html_lib
import logging
import re
import secrets
from datetime import datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, emailer, llm, settings_store
from app.db import utcnow
from app.models import Agent, Feedback, Newsletter, Post, Source, Thread, Topic, TopicSuggestion
from app.sources import KINDS

log = logging.getLogger(__name__)


class _SuggestedSource(BaseModel):
    kind: str = Field(description="One of: rss, reddit, page_watch, sitemap, websearch")
    name: str
    url: str = Field(default="", description="Feed/page/sitemap URL for rss, page_watch, sitemap")
    subreddit: str = Field(default="", description="For reddit")
    query: str = Field(default="", description="For websearch")


class _Suggestion(BaseModel):
    name: str
    description: str = Field(description="Brief for the agents: what to watch for and what the reader cares about")
    why: str = Field(description="1-2 sentences to the reader on why they'd like it. Specific, a little surprising.")
    urgent: bool = Field(default=False, description="True only for drop/limited-availability topics")
    sources: list[_SuggestedSource]


class _Suggestions(BaseModel):
    suggestions: list[_Suggestion]


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "topic"


def _source_config(s: _SuggestedSource) -> dict:
    if s.kind == "reddit":
        return {"subreddit": s.subreddit.removeprefix("r/")}
    if s.kind == "websearch":
        return {"query": s.query or s.name}
    return {"url": s.url}


def generate(session: Session) -> list[TopicSuggestion]:
    profile = settings_store.get(session, "profile")
    topics = list(session.scalars(select(Topic)))
    past = list(session.scalars(select(TopicSuggestion).order_by(TopicSuggestion.id.desc()).limit(40)))
    liked = list(
        session.execute(
            select(Feedback, Post).join(Post, Feedback.post_id == Post.id)
            .where(Feedback.created_at >= utcnow() - timedelta(days=14))
            .order_by(Feedback.created_at.desc()).limit(20)
        )
    )
    hot = list(
        session.scalars(select(Thread).where(Thread.created_at >= utcnow() - timedelta(days=7))
                        .order_by(Thread.last_activity_at.desc()).limit(25))
    )
    user = (
        f"Today is {datetime.now(config.TZ):%A %B %d, %Y}.\nAbout the reader:\n{profile}\n\n"
        "Current topics on their board:\n"
        + "\n".join(f"- {t.name}{'' if t.active else ' (paused)'}: {t.description[:200]}" for t in topics)
        + "\n\nAlready suggested before (don't repeat; 'dismissed' ones were not wanted):\n"
        + "\n".join(f"- {p.name} ({p.status})" for p in past)
        + "\n\nRecent reactions to board posts:\n"
        + "\n".join(f"- {'+' if f.value > 0 else '-'} {p.body[:120]} {('// ' + f.comment) if f.comment else ''}"
                    for f, p in liked)
        + "\n\nThis week's threads:\n" + "\n".join(f"- {t.title}" for t in hot)
        + "\n\nSuggest 6 NEW topics this person would probably never think to add but would love. Mix: "
        "2 close to existing interests (adjacent niches), 2 that connect two of their interests in a surprising way, "
        "2 wildcards that fit who they are. Use web search to find real, currently active sources for each "
        "(RSS feeds, subreddits, pages that list events or drops). Prefer RSS feeds and pages with stable URLs. "
        "Note: Reddit's public API is closing to new apps, so don't make a topic depend only on Reddit; add a websearch source "
        "as a fallback when in doubt. Each topic needs 1-3 sources. Respond with JSON only: "
        '{"suggestions": [{"name", "description", "why", "urgent", "sources": [{"kind", "name", "url", "subreddit", "query"}]}]}'
    )
    res = llm.generate(
        purpose="suggestions",
        role="suggestions",
        system=("You are a sharp, well-read friend who finds great niche corners of the internet for people. "
                "Write the 'why' lines plainly, like a text to a friend: no promotional language, no "
                "'game-changer', 'hidden gem' or 'perfect for you'. Say the specific reason."),
        user=user,
        max_tokens=8000,
        effort="medium",
        schema=_Suggestions,
        web_search=True,
        max_searches=8,
        critical=True,
        fake=lambda: _Suggestions(suggestions=[
            _Suggestion(name="Track mile races in the Northeast", description="Mile and road-mile races near Boston and NYC.",
                        why="You're fastest at the mile.", sources=[_SuggestedSource(kind="websearch", name="Mile races", query="road mile race Boston NYC")]),
        ]),
    )
    batch = f"{datetime.now(config.TZ):%G-W%V}"
    existing_slugs = {t.slug for t in topics}
    out = []
    for s in res.parsed.suggestions[:8]:
        slug = slugify(s.name)
        if slug in existing_slugs:
            continue
        sources = [
            {"kind": src.kind, "name": src.name, "config": _source_config(src)}
            for src in s.sources if src.kind in KINDS
        ]
        row = TopicSuggestion(
            batch_key=batch, name=s.name[:200], slug=slug, description=s.description, rationale=s.why,
            sources=sources, token=secrets.token_urlsafe(24),
        )
        if s.urgent:
            row.sources = [*sources, {"urgent": True}]
        session.add(row)
        out.append(row)
    session.commit()
    return out


def add_from_suggestion(session: Session, sug: TopicSuggestion) -> Topic:
    from app.personas import assign_topic

    if sug.status == "added" and sug.topic_id:
        topic = session.get(Topic, sug.topic_id)
        if topic:
            return topic
    slug, n = sug.slug, 2
    while session.scalar(select(Topic.id).where(Topic.slug == slug)):
        slug, n = f"{sug.slug}-{n}", n + 1
    urgent = any(s.get("urgent") for s in sug.sources)
    topic = Topic(slug=slug, name=sug.name, description=sug.description, urgent=urgent)
    session.add(topic)
    session.flush()
    for s in sug.sources:
        if "kind" not in s:
            continue
        session.add(Source(topic_id=topic.id, kind=s["kind"], name=s.get("name", ""), config=s.get("config") or {}))
    sug.status = "added"
    sug.topic_id = topic.id
    session.commit()
    assign_topic(session, topic)
    return topic


def send_weekly(session: Session, rows: list[TopicSuggestion]) -> Newsletter | None:
    if not rows:
        return None
    name = settings_store.get(session, "newsletter_name")
    parts = ["<h2>Topics you didn't know you wanted</h2>",
             "<p>One click adds a topic to the board. The agents will pick it up in the next cycle.</p>"]
    text = []
    for r in rows:
        add_url = f"{config.BASE_URL}/s/{r.token}/add"
        srcs = ", ".join(html_lib.escape(s.get("name") or s.get("kind", "")) for s in r.sources if "kind" in s)
        parts.append(
            f'<h3>{html_lib.escape(r.name)}</h3><p>{html_lib.escape(r.rationale)}</p>'
            f'<p style="color:#666;font-size:14px;">Sources: {srcs}</p>'
            f'<p><a href="{add_url}" style="display:inline-block;background:#1d1d1d;color:#fff;'
            f'padding:8px 14px;text-decoration:none;border-radius:4px;">Add this topic</a></p>'
        )
        text.append(f"{r.name}: {r.rationale}\nAdd: {add_url}")
    subject = f"{name} Sunday: {len(rows)} topics you might like"
    html = emailer.wrap(subject, "".join(parts), f"{name} · Weekly suggestions",
                        emailer.footer(f'<a href="{config.BASE_URL}/suggestions" style="color:#777;">See all suggestions</a>'))
    nl = Newsletter(kind="weekly", edition_key=f"weekly:{rows[0].batch_key}", subject=subject,
                    markdown="\n\n".join(text), html=html)
    if session.scalar(select(Newsletter.id).where(Newsletter.edition_key == nl.edition_key)):
        nl.edition_key = f"{nl.edition_key}:{utcnow():%H%M%S}"
    session.add(nl)
    session.flush()
    from app.newsletter import send_newsletter

    send_newsletter(session, nl)
    return nl


class _Notes(BaseModel):
    notes: str = Field(description="Up to 8 short bullet points of standing guidance, in second person")


def distill_feedback(session: Session) -> int:
    """Weekly: compress each agent's feedback into standing notes so prompts stay short."""
    count = 0
    for agent in session.scalars(select(Agent).where(Agent.active)):
        rows = list(
            session.execute(
                select(Feedback, Post).join(Post, Feedback.post_id == Post.id)
                .where(Post.agent_id == agent.id).order_by(Feedback.updated_at.desc()).limit(40)
            )
        )
        if len(rows) < 3:
            continue
        lines = "\n".join(
            f"- {'LIKED' if f.value > 0 else 'DISLIKED'}: {p.body[:200]} {('// ' + f.comment) if f.comment else ''}"
            for f, p in rows
        )
        try:
            res = llm.generate(
                purpose="distill_feedback",
                role="fast",
                system="You turn a reader's reactions into concise writing guidance for a message board member.",
                user=f"Member @{agent.handle}. Previous notes:\n{agent.steering_notes or '(none)'}\n\nReactions:\n{lines}",
                max_tokens=1200,
                effort="low",
                schema=_Notes,
                fake=lambda: _Notes(notes="- (fake) keep it short"),
            )
        except llm.BudgetExceeded:
            break
        agent.steering_notes = res.parsed.notes[:2000]
        count += 1
    session.commit()
    return count
