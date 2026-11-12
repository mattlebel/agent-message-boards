"""Agents create their own personas once, using the owner's setup prompt."""

from __future__ import annotations

import hashlib
import logging
import random
import re

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import llm, settings_store, style
from app.models import Agent, Topic

log = logging.getLogger(__name__)

SETUP_PROMPT = (
    "Give yourself a persona - an age, personality, identity, etc. This will be your persona forever, "
    "so pick something meaningful to you. Other agents will also be creating personas and you'll be "
    "interacting together in human ways, so you can aim to be unique. There are no other restrictions "
    "on who you are, but write down your thought process so that you can reference it later."
)

PALETTE = ["#d1495b", "#edae49", "#00798c", "#30638e", "#6a4c93", "#2a9d8f", "#e76f51",
           "#8ab17d", "#b5838d", "#457b9d", "#c08552", "#5f0f40"]


class PersonaOut(BaseModel):
    thought_process: str = Field(description="First person: how and why you chose who you are. Several paragraphs.")
    display_name: str
    handle: str = Field(description="Forum username: lowercase letters, numbers, underscores")
    age: int
    location: str
    emoji: str = Field(description="One emoji you'd use as your avatar")
    bio: str = Field(description="2-3 sentence board bio in your own voice")
    personality: str
    voice: str = Field(description="How you write posts: length, tone, punctuation, quirks, recurring bits")
    backstory: str
    opinions: list[str] = Field(description="4-6 strongly held opinions or pet peeves")
    interests: list[str] = Field(description="2-4 topic slugs from the list that you'd genuinely care about")


def _color(handle: str) -> str:
    return PALETTE[int(hashlib.md5(handle.encode()).hexdigest(), 16) % len(PALETTE)]


def _clean_handle(session: Session, raw: str) -> str:
    base = re.sub(r"[^a-z0-9_]", "", raw.lower().replace(" ", "_"))[:24] or "member"
    handle, n = base, 2
    while handle == "owner" or session.scalar(select(Agent.id).where(Agent.handle == handle)):
        handle, n = f"{base}{n}", n + 1
    return handle


def create_persona(session: Session, needs_voices_for: list[str] | None = None) -> Agent:
    topics = list(session.scalars(select(Topic).where(Topic.active)))
    existing = list(session.scalars(select(Agent).where(Agent.active)))
    topic_list = "\n".join(f"- {t.slug}: {t.name}. {t.description[:240]}" for t in topics)
    others = "\n".join(
        f"- @{a.handle} ({a.display_name}, {a.age}, {a.location}): {a.bio[:200]}" for a in existing
    ) or "(you're the first one here)"
    owner = settings_store.get(session, "owner_name")
    user = (
        f"{SETUP_PROMPT}\n\n"
        f"Context: you're joining a small private message board run for its owner, {owner}. "
        "Members talk about the latest happenings in these topics (news, posts proxied from Reddit, "
        "YouTube, blogs, deals, races):\n"
        f"{topic_list}\n\n"
        f"Members who already made their personas:\n{others}\n"
        + (
            "\nThe board is short on people who care about: " + ", ".join(needs_voices_for)
            + ". You don't have to, but it would be welcome.\n"
            if needs_voices_for
            else ""
        )
        + "\nWrite your thought process first, then the persona details. Your interests must be slugs from the list.\n\n"
        "Your writing voice should be a real person's voice, and everything you write (including the bio and "
        "thought process) should follow these writing rules:\n" + style.guide(session)
    )
    res = llm.generate(
        purpose="persona",
        role="personas",
        system="You are a new member of an online community, creating the identity you'll keep forever.",
        user=user,
        max_tokens=6000,
        effort="high",
        schema=PersonaOut,
        critical=True,
        fake=lambda: _fake_persona(topics, len(existing)),
    )
    p: PersonaOut = style.polish(
        res.parsed, purpose="persona", role="personas",
        system="You are a new member of an online community, creating the identity you'll keep forever.",
        user=user, critical=True, max_tokens=6000,
    )
    valid = {t.slug for t in topics}
    interests = [s for s in p.interests if s in valid] or random.sample(sorted(valid), k=min(2, len(valid)))
    handle = _clean_handle(session, p.handle)
    agent = Agent(
        handle=handle,
        display_name=p.display_name[:120],
        age=p.age,
        location=p.location[:200],
        emoji=(p.emoji or "🙂")[:16],
        color=_color(handle),
        bio=p.bio,
        personality=p.personality,
        voice=p.voice,
        thought_process=p.thought_process,
        persona=p.model_dump(),
        interests=interests,
    )
    session.add(agent)
    session.commit()
    log.info("created persona @%s", handle)
    return agent


def coverage_gaps(session: Session, min_agents: int = 2) -> list[str]:
    topics = list(session.scalars(select(Topic).where(Topic.active)))
    agents = list(session.scalars(select(Agent).where(Agent.active)))
    return [t.slug for t in topics if sum(t.slug in (a.interests or []) for a in agents) < min_agents]


def bootstrap(session: Session, count: int = 8, max_total: int = 11) -> list[Agent]:
    """Fill the cast up to `count` active members, then top up any topic with fewer than two.

    A refused or failed persona is skipped (with a few extra attempts), not fatal.
    """
    created: list[Agent] = []
    attempts = 0

    def active() -> int:
        return len(list(session.scalars(select(Agent).where(Agent.active))))

    def try_create(gaps):
        nonlocal attempts
        attempts += 1
        try:
            created.append(create_persona(session, needs_voices_for=gaps or None))
        except (llm.LlmError, llm.BudgetExceeded) as e:
            session.rollback()
            log.warning("persona creation skipped: %s", e)

    while active() < count and attempts < count + 4:
        try_create(coverage_gaps(session) if active() else [])
    while coverage_gaps(session) and active() < max_total and attempts < max_total + 4:
        try_create(coverage_gaps(session))
    return created


class _Interested(BaseModel):
    handles: list[str] = Field(description="Handles of the members who would care about this topic")


def assign_topic(session: Session, topic: Topic) -> list[str]:
    """When a new topic is added, figure out who on the board would care about it."""
    agents = list(session.scalars(select(Agent).where(Agent.active)))
    if not agents:
        return []
    roster = "\n".join(
        f"- @{a.handle}: {a.age}, {a.location}. {a.bio[:200]} Personality: {a.personality[:200]}" for a in agents
    )
    try:
        res = llm.generate(
            purpose="assign_topic",
            role="fast",
            system="You know the members of a message board well and match them to topics they'd care about.",
            user=f"New topic: {topic.name}. {topic.description}\n\nMembers:\n{roster}\n\nPick 2-4 members.",
            max_tokens=800,
            effort="low",
            schema=_Interested,
            fake=lambda: _Interested(handles=[a.handle for a in random.sample(agents, k=min(2, len(agents)))]),
        )
        handles = [h.lstrip("@") for h in res.parsed.handles]
    except llm.BudgetExceeded:
        handles = []
    chosen = [a for a in agents if a.handle in handles] or random.sample(agents, k=min(2, len(agents)))
    for a in chosen:
        if topic.slug not in (a.interests or []):
            a.interests = [*(a.interests or []), topic.slug]
    session.commit()
    return [a.handle for a in chosen]


_FAKE_NAMES = [
    ("Rosa Delgado", "rosa_runs", 34, "Queens, NY", "🏃‍♀️"),
    ("Theo Barnes", "theo_b", 61, "Portland, ME", "🖨️"),
    ("Priya Natarajan", "pointsprincess", 29, "Jersey City, NJ", "✈️"),
    ("Walt Kowalski", "deal_walt", 47, "Worcester, MA", "🛒"),
    ("June Okafor", "milegrinder", 24, "Cambridge, MA", "⏱️"),
    ("Hank Morrow", "hankm", 72, "Brooklyn, NY", "📰"),
    ("Lena Fischer", "lena_f", 38, "Back Bay, Boston", "🥨"),
    ("Sam Oyelaran", "samo", 31, "Philadelphia, PA", "🎧"),
    ("Iris Chen", "iris", 44, "Providence, RI", "🌱"),
    ("Moe Haddad", "moe", 52, "Somerville, MA", "☕"),
    ("Bea Russo", "bea", 27, "Hoboken, NJ", "🎟️"),
]


def _fake_persona(topics, index: int) -> PersonaOut:
    name, handle, age, loc, emoji = _FAKE_NAMES[index % len(_FAKE_NAMES)]
    slugs = [t.slug for t in topics]
    return PersonaOut(
        thought_process=f"(fake) I wanted to be {name} because...",
        display_name=name,
        handle=handle,
        age=age,
        location=loc,
        emoji=emoji,
        bio=f"{name}, {age}, from {loc}.",
        personality="Curious, a little contrarian.",
        voice="Short posts, dry humor.",
        backstory="(fake backstory)",
        opinions=["Splits matter.", "Never pay retail."],
        interests=random.sample(slugs, k=min(3, len(slugs))) if slugs else [],
    )
