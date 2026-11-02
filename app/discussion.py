"""Agents start threads about picked items and reply to each other (and to you)."""

from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timedelta
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config, llm, settings_store
from app.db import utcnow
from app.models import Agent, Feedback, Item, Post, Thread, Topic
from app.sources.feeds import reddit_top_comments
from app.triage import AlertDecision, item_brief

log = logging.getLogger(__name__)


# --- prompts --------------------------------------------------------------------


def steering_for(session: Session, agent: Agent) -> str:
    """The owner's thumbs up/down (and comments) become standing guidance."""
    rows = list(
        session.execute(
            select(Feedback, Post)
            .join(Post, Feedback.post_id == Post.id)
            .where(Post.agent_id == agent.id)
            .order_by(Feedback.updated_at.desc())
            .limit(15)
        )
    )
    general = list(
        session.execute(
            select(Feedback, Post)
            .join(Post, Feedback.post_id == Post.id)
            .where(Post.agent_id != agent.id, Feedback.comment != "")
            .order_by(Feedback.updated_at.desc())
            .limit(8)
        )
    )
    parts = []
    if agent.steering_notes:
        parts.append("What the owner has told you over time:\n" + agent.steering_notes)
    if rows:
        lines = []
        for fb, post in rows:
            mark = "LIKED" if fb.value > 0 else "DISLIKED"
            note = f' (owner said: "{fb.comment}")' if fb.comment else ""
            lines.append(f'- {mark}: "{post.body[:160]}"{note}')
        parts.append("Recent owner reactions to YOUR posts:\n" + "\n".join(lines))
    if general:
        lines = []
        for fb, post in general:
            mark = "liked" if fb.value > 0 else "disliked"
            lines.append(f'- The owner {mark} a post by someone else because: "{fb.comment}"')
        parts.append("General taste of the owner:\n" + "\n".join(lines))
    return "\n\n".join(parts)


def agent_system(session: Session, agent: Agent) -> str:
    owner = settings_store.get(session, "owner_name")
    profile = settings_store.get(session, "profile")
    p = agent.persona or {}
    opinions = "\n".join(f"- {o}" for o in p.get("opinions", []))
    steering = steering_for(session, agent)
    return f"""You are {agent.display_name} (@{agent.handle}), a regular on a small private message board.
This is your persona, which you chose yourself and keep forever.

Age: {agent.age}. Location: {agent.location}.
Bio: {agent.bio}
Personality: {agent.personality}
How you write: {agent.voice}
Backstory: {p.get('backstory', '')}
Opinions and pet peeves:
{opinions}

Your notes from when you created yourself:
{agent.thought_process[:3000]}

About the board: it exists for its owner, {owner} (shown as @owner). {owner} reads every thread and sometimes posts.
About {owner}:
{profile}

How to post:
- Stay in character. Write like a real person on a forum, not an assistant. No "As an AI", no sign-offs.
- Usually 1-3 short paragraphs. Specific beats generic. Have a take. Disagree when you disagree.
- Only state facts that are in the material you were given. Your persona's experiences and opinions are fine; mark guesses as guesses.
- Mention other members as @handle when you respond to them. Speak to @owner directly when it's useful to {owner}
  (e.g. a race {owner} should sign up for, a deal worth grabbing, a trip worth taking).
- Plain text, light markdown at most. Emoji only if it fits how you write.

{steering}""".strip()


def render_thread(thread: Thread, limit_chars: int = 9000) -> str:
    lines = []
    for p in thread.posts:
        who = f"@{p.agent.handle}" if p.agent else "@owner"
        reply = f" (replying to #{p.reply_to_id})" if p.reply_to_id else ""
        lines.append(f"#{p.id} {who}{reply}:\n{p.body}")
    text = "\n\n".join(lines)
    if len(text) > limit_chars:
        text = "...(earlier posts trimmed)...\n\n" + text[-limit_chars:]
    return text


def recent_posts_by(session: Session, agent: Agent, n: int = 5) -> str:
    posts = list(
        session.scalars(select(Post).where(Post.agent_id == agent.id).order_by(Post.id.desc()).limit(n))
    )
    if not posts:
        return ""
    return "Your last few posts elsewhere on the board (don't repeat yourself):\n" + "\n".join(
        f"- {p.body[:140]}" for p in posts
    )


# --- structured outputs ------------------------------------------------------------


class _Opening(BaseModel):
    title: str = Field(description="Thread title, forum style, under 90 characters")
    body: str = Field(description="Opening post")


class _Reply(BaseModel):
    reply_to_post_id: Optional[int] = Field(default=None, description="The #id you're responding to, if any")
    body: str


# --- choosing who talks ------------------------------------------------------------


def _agents(session: Session) -> list[Agent]:
    return list(session.scalars(select(Agent).where(Agent.active)))


def choose_starter(session: Session, topic: Topic) -> Agent:
    agents = _agents(session)
    interested = [a for a in agents if topic.slug in (a.interests or [])] or agents
    since = utcnow() - timedelta(days=2)
    counts = dict(
        session.execute(
            select(Post.agent_id, func.count(Post.id)).where(Post.created_at >= since).group_by(Post.agent_id)
        ).all()
    )
    # Fewer recent posts -> more likely to start this one.
    weights = [1.0 / (1 + counts.get(a.id, 0)) for a in interested]
    return random.choices(interested, weights=weights, k=1)[0]


def choose_replier(session: Session, thread: Thread, exclude_last: bool = True) -> Agent:
    agents = _agents(session)
    last = thread.posts[-1].agent_id if thread.posts else None
    pool = [a for a in agents if not (exclude_last and a.id == last)] or agents
    slug = thread.topic.slug if thread.topic else ""
    in_thread = {p.agent_id for p in thread.posts}
    weights = []
    for a in pool:
        w = 1.0
        if slug in (a.interests or []):
            w += 2.5
        if a.id in in_thread:
            w += 0.8  # people come back to threads they're in
        weights.append(w)
    return random.choices(pool, weights=weights, k=1)[0]


# --- writing posts ------------------------------------------------------------------


def _post(session: Session, thread: Thread, agent: Optional[Agent], body: str, reply_to: Optional[int] = None) -> Post:
    post = Post(agent=agent, body=body.strip(), reply_to_id=reply_to)
    thread.posts.append(post)
    thread.last_activity_at = utcnow()
    session.flush()
    return post


def _material(item: Item) -> str:
    text = item_brief(item, 2500)
    extra = item.extra or {}
    if extra.get("source_kind") == "reddit" and extra.get("reddit_id"):
        comments = reddit_top_comments(extra["reddit_id"])
        if comments:
            text += "\n\nTop comments on Reddit:\n" + "\n".join(f"- {c}" for c in comments)
    if extra.get("alert"):
        a = extra["alert"]
        text += f"\n\nAlert details: {a.get('what')} Where: {a.get('where')}. When: {a.get('when')}. Travel: {a.get('travel')}"
    return text


def start_thread(session: Session, item: Item, critical: bool = False, alert: bool = False) -> Thread:
    topic = item.topic
    agent = choose_starter(session, topic)
    today = datetime.now(config.TZ).strftime("%A %B %d, %Y")
    user = (
        f"Today is {today}. Start a new thread in the '{topic.name}' section about this:\n\n{_material(item)}\n\n"
        + ("This is URGENT: a new drop/announcement the owner may want to travel for. Make that clear.\n" if alert else "")
        + "Share it with the board: what it is, the link, and your honest take. "
        + recent_posts_by(session, agent)
    )
    res = llm.generate(
        purpose="thread_start",
        role="posts",
        system=agent_system(session, agent),
        user=user,
        max_tokens=2500,
        effort="low",
        schema=_Opening,
        critical=critical,
        fake=lambda: _Opening(title=item.title[:80], body=f"(fake) Check this out: {item.url}\n\nThoughts?"),
    )
    title = res.parsed.title.strip()[:200]
    if alert and not title.startswith("🚨"):
        title = "🚨 " + title
    thread = Thread(topic_id=topic.id, item_id=item.id, title=title, is_alert=alert)
    session.add(thread)
    session.flush()
    _post(session, thread, agent, res.parsed.body)
    item.status = "alerted" if alert else "discussed"
    session.commit()
    return thread


def add_reply(session: Session, thread: Thread, agent: Optional[Agent] = None, critical: bool = False,
              nudge: str = "") -> Post:
    agent = agent or choose_replier(session, thread)
    material = _material(thread.item) if thread.item else "(The owner started this thread.)"
    user = (
        f"Thread: {thread.title}\nSection: {thread.topic.name if thread.topic else 'General'}\n\n"
        f"What the thread is about:\n{material}\n\nThe thread so far:\n\n{render_thread(thread)}\n\n"
        "Write your reply. Respond to a specific post if it makes sense (give its #id). Add something new: "
        "an opinion, a disagreement, a relevant experience, a practical tip for @owner, or a joke. "
        "Don't summarize the thread.\n"
        + (f"{nudge}\n" if nudge else "")
        + recent_posts_by(session, agent)
    )
    valid_ids = {p.id for p in thread.posts}
    res = llm.generate(
        purpose="reply",
        role="posts",
        system=agent_system(session, agent),
        user=user,
        max_tokens=2000,
        effort="low",
        schema=_Reply,
        critical=critical,
        fake=lambda: _Reply(reply_to_post_id=thread.posts[-1].id if thread.posts else None,
                            body=f"(fake reply from @{agent.handle})"),
    )
    reply_to = res.parsed.reply_to_post_id if res.parsed.reply_to_post_id in valid_ids else None
    post = _post(session, thread, agent, res.parsed.body, reply_to)
    session.commit()
    return post


def discuss(session: Session, items: list[Item]) -> list[Thread]:
    lo = int(settings_store.get(session, "min_replies"))
    hi = int(settings_store.get(session, "max_replies"))
    threads = []
    for item in items:
        try:
            thread = start_thread(session, item)
            for _ in range(random.randint(lo, max(lo, hi))):
                add_reply(session, thread)
            threads.append(thread)
        except llm.BudgetExceeded:
            log.warning("budget reached; stopping discussion")
            item.status = "scored"  # try again next cycle
            session.commit()
            break
        except Exception:
            log.exception("thread for item %s failed", item.id)
            session.rollback()
    return threads


def revisit(session: Session, n_threads: int = 3) -> int:
    """Keep recent conversations going: threads with owner posts or feedback get priority."""
    since = utcnow() - timedelta(hours=36)
    threads = list(
        session.scalars(select(Thread).where(Thread.last_activity_at >= since).order_by(Thread.last_activity_at.desc()))
    )
    if not threads:
        return 0

    def interest(t: Thread) -> float:
        owner_posts = sum(1 for p in t.posts if p.agent_id is None)
        reactions = sum(1 for p in t.posts if p.feedback is not None)
        return owner_posts * 3 + reactions + random.random() * 2

    count = 0
    for t in sorted(threads, key=interest, reverse=True)[:n_threads]:
        try:
            add_reply(session, t)
            count += 1
        except llm.BudgetExceeded:
            break
        except Exception:
            log.exception("revisit failed for thread %s", t.id)
            session.rollback()
    return count


def respond_to_owner(session: Session, thread_id: int) -> int:
    """Called right after you post: 1-3 members answer. @mentions get first dibs."""
    thread = session.get(Thread, thread_id)
    if thread is None or not thread.posts:
        return 0
    owner_post = thread.posts[-1]
    agents = _agents(session)
    mentioned = [a for a in agents if re.search(rf"@{re.escape(a.handle)}\b", owner_post.body, re.I)]
    responders: list[Agent] = mentioned[:3]
    target = 1 if mentioned else random.randint(1, 3)
    attempts = 0
    while len(responders) < max(target, len(mentioned)) and attempts < 10:
        attempts += 1
        a = choose_replier(session, thread, exclude_last=False)
        if a not in responders:
            responders.append(a)
    count = 0
    for a in responders:
        try:
            add_reply(session, thread, agent=a, critical=True,
                      nudge="@owner just posted. Respond to what they said.")
            count += 1
        except Exception:
            log.exception("reply to owner failed")
            session.rollback()
    return count


def alert_thread(session: Session, item: Item, decision: AlertDecision) -> Thread:
    thread = start_thread(session, item, critical=True, alert=True)
    try:
        add_reply(session, thread, critical=True,
                  nudge="This is a time-sensitive drop. Weigh in on whether @owner should go and how.")
    except Exception:
        log.exception("alert reply failed")
    return thread


def thread_heat(thread: Thread) -> float:
    """Rough ranking for the newsletter: replies, owner involvement, thumbs."""
    score = len(thread.posts)
    for p in thread.posts:
        if p.agent_id is None:
            score += 3
        if p.feedback is not None:
            score += 2 if p.feedback.value > 0 else -1
    if thread.item and thread.item.score:
        score += thread.item.score
    if thread.topic:
        score += (thread.topic.priority - 3) * 1.5
    if thread.is_alert:
        score += 10
    return score


def count_posts_since(session: Session, since: datetime) -> int:
    return session.scalar(select(func.count(Post.id)).where(Post.created_at >= since)) or 0
