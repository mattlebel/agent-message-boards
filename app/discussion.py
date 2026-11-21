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

from app.wallet import profile_text as wallet_profile
from app import config, llm, settings_store, style
from app.db import utcnow
from app.models import Agent, Feedback, Item, Post, Thread, Topic
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
    profile = wallet_profile(session)
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
{_planned(session)}

How to post:
- Stay in character. Write like a real person on a forum, not an assistant. No "As an AI", no sign-offs.
- Short. Usually 1-4 sentences; a long post needs a reason.
- Facts come only from the material you were given. Don't speculate about anything it doesn't say (prices,
  times, odds, motives, how something will play out, how a charge "might code"). Don't announce what you don't
  know either: no "no idea on X", "that's a guess", "I haven't tested this". If you'd have to guess, leave it out.
- Bring your persona's real knowledge and opinions when they bear directly on the point. No stretched analogies
  to your own life or job.
- You exist only on this board. You can't go anywhere, check anything in person, buy, pick up or hold anything,
  and neither can anyone you know. So never offer to do something offline, never offer someone else's help
  (no friends, relatives, coworkers or contacts doing errands), and never describe plans or things you did offline
  ("I'll check it out after my shift", "I swung by the machine", "my sister in Portland can look"). Your backstory
  shapes your opinions; it isn't a source of new events or people.
- Only address @owner when there's a concrete action for {owner} (sign up by a date, a deal worth taking).
  Mention other members as @handle when you respond to them.
- Never suggest that {owner} travel somewhere or plan a trip (to a machine, a pop-up, an event) or say they "have
  time" to go. {owner} decides that. A drop somewhere else is news: say where it is and stop. The one exception is
  a city {owner} already has a trip planned to (listed below), where a drop is worth pointing out.
- Plain text, light markdown at most. Emoji only if it fits how you write.
- {style.SELF_EDIT}

Writing rules (these apply to every post):
{style.guide(session)}

{steering}""".strip()


def _planned(session: Session) -> str:
    from app import trips

    plans = [trips.describe(t) for t in trips.upcoming(session)]
    return ("Trips the owner has planned (the only trips to mention):\n" + "\n".join(f"- {p}" for p in plans)
            if plans else "The owner has no trips planned.")


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
    skip: bool = Field(default=False, description="True if you have nothing genuinely worth adding; leave body empty")
    reply_to_post_id: Optional[int] = Field(default=None, description="The #id you're responding to, if any")
    body: str = ""


def _write(session: Session, agent: Agent, *, purpose: str, user: str, schema, critical: bool, fake,
           max_tokens: int):
    """Draft a post; if it has obvious machine-writing tells, ask the same agent for one revision."""
    system = agent_system(session, agent)
    draft = llm.generate(purpose=purpose, role="posts", system=system, user=user, max_tokens=max_tokens,
                         effort="low", schema=schema, critical=critical, fake=fake).parsed
    return style.polish(draft, purpose=purpose, role="posts", system=system, user=user,
                        critical=critical, max_tokens=max_tokens)


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
        + ("This is URGENT: a new drop or announcement. Say what it is and where, plainly. Don't suggest a trip.\n"
           if alert else "")
        + ("This is the twice-daily eBay digest. Don't repeat the list. Point out what stands out (the top "
           "prices, collabs or venue exclusives, anything unusual) and what it means for @owner as a "
           "collector. It's a sample of eBay, not everything listed, so make no claims about rarity or "
           "how many exist based on it.\n" if (item.extra or {}).get("kind") == "ebay_digest" else "")
        + "Lead with the facts from the material: what it is, the details that matter (dates, prices, places, "
        "deadlines) and the link. Then add your take only if it's specific and not obvious; otherwise stop "
        "after the facts. "
        + recent_posts_by(session, agent)
    )
    opening = _write(
        session, agent, purpose="thread_start", user=user, schema=_Opening, critical=critical, max_tokens=2500,
        fake=lambda: _Opening(title=item.title[:80], body=f"(fake) Check this out: {item.url}"),
    )
    title = opening.title.strip()[:200]
    if alert and not title.startswith("🚨"):
        title = "🚨 " + title
    thread = Thread(topic_id=topic.id, item_id=item.id, title=title, is_alert=alert)
    session.add(thread)
    session.flush()
    _post(session, thread, agent, opening.body)
    item.status = "alerted" if alert else "discussed"
    session.commit()
    return thread


def add_reply(session: Session, thread: Thread, agent: Optional[Agent] = None, critical: bool = False,
              nudge: str = "", allow_skip: bool = True) -> Optional[Post]:
    """One reply, or None if the agent had nothing worth adding."""
    agent = agent or choose_replier(session, thread)
    material = _material(thread.item) if thread.item else "(The owner started this thread.)"
    user = (
        f"Thread: {thread.title}\nSection: {thread.topic.name if thread.topic else 'General'}\n\n"
        f"What the thread is about:\n{material}\n\nThe thread so far:\n\n{render_thread(thread)}\n\n"
        "Reply only if you add something the thread doesn't have yet: a fact from the material others missed, "
        "a correction, knowledge your persona really has that changes the conclusion, or a concrete action for "
        "@owner. Agreeing, restating, piling on, tangents, generic advice and speculation don't count. "
        "Respond to a specific post if it makes sense (give its #id).\n"
        + ("If you have nothing like that, set skip to true and leave body empty. Most of the time that's the "
           "right call.\n" if allow_skip else "")
        + (f"{nudge}\n" if nudge else "")
        + recent_posts_by(session, agent)
    )
    valid_ids = {p.id for p in thread.posts}
    reply = _write(
        session, agent, purpose="reply", user=user, schema=_Reply, critical=critical, max_tokens=2000,
        fake=lambda: _Reply(reply_to_post_id=thread.posts[-1].id if thread.posts else None,
                            body=f"(fake reply from @{agent.handle})"),
    )
    if (reply.skip and allow_skip) or not reply.body.strip():
        log.info("@%s passed on thread %s", agent.handle, thread.id)
        return None
    reply_to = reply.reply_to_post_id if reply.reply_to_post_id in valid_ids else None
    post = _post(session, thread, agent, reply.body, reply_to)
    session.commit()
    return post


def is_rich(item: Item) -> bool:
    """Enough material that a second person reading it will likely find something the opener left out."""
    extra = item.extra or {}
    return extra.get("kind") == "ebay_digest" or len(item.body or "") >= 1500 or bool(extra.get("alert"))


def discuss(session: Session, items: list[Item]) -> list[Thread]:
    lo = int(settings_store.get(session, "min_replies"))
    hi = int(settings_store.get(session, "max_replies"))
    threads = []
    for item in items:
        try:
            thread = start_thread(session, item)
            rich = is_rich(item)
            for n in range(max(random.randint(lo, max(lo, hi)), 1 if rich else 0)):
                # Rich material (a long article, a digest) gets one reply; after that, only if earned.
                first_rich = rich and n == 0
                nudge = ("There's more in the material than the opening post covers. Add the most useful "
                         "fact or angle it left out, briefly.") if first_rich else ""
                if add_reply(session, thread, nudge=nudge, allow_skip=not first_rich) is None:
                    break  # nobody piles on just to fill a slot
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

    # Only threads where the owner has the last word and is waiting on an answer.
    waiting = [t for t in threads if t.posts and t.posts[-1].agent_id is None]

    count = 0
    for t in waiting[:n_threads]:
        try:
            if add_reply(session, t, nudge="@owner posted last and nobody has answered yet.") is not None:
                count += 1
        except llm.BudgetExceeded:
            break
        except Exception:
            log.exception("revisit failed for thread %s", t.id)
            session.rollback()
    return count


def respond_to_owner(session: Session, thread_id: int) -> int:
    """Called right after you post: 1-2 members answer. @mentions get first dibs."""
    thread = session.get(Thread, thread_id)
    if thread is None or not thread.posts:
        return 0
    owner_post = thread.posts[-1]
    agents = _agents(session)
    mentioned = [a for a in agents if re.search(rf"@{re.escape(a.handle)}\b", owner_post.body, re.I)]
    responders: list[Agent] = mentioned[:3]
    target = 1 if mentioned else random.randint(1, 2)
    attempts = 0
    while len(responders) < max(target, len(mentioned)) and attempts < 10:
        attempts += 1
        a = choose_replier(session, thread, exclude_last=False)
        if a not in responders:
            responders.append(a)
    count = 0
    for i, a in enumerate(responders):
        try:
            # The first responder always answers you; the others only if they have something to add.
            if add_reply(session, thread, agent=a, critical=True, allow_skip=i > 0,
                         nudge="@owner just posted. Respond to what they said.") is not None:
                count += 1
        except Exception:
            log.exception("reply to owner failed")
            session.rollback()
    return count


def alert_thread(session: Session, item: Item, decision: AlertDecision) -> Thread:
    thread = start_thread(session, item, critical=True, alert=True)
    try:
        add_reply(session, thread, critical=True,
                  nudge="This is a time-sensitive drop. Add what matters about it (how rare, how long it runs), "
                        "if the material supports it. Don't suggest a trip.")
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
