from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, utcnow


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class Topic(Base):
    __tablename__ = "topics"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # Plain-English brief: what matters, what to ignore. Agents and triage read this.
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Urgent topics get checked often and send an alert email as soon as something drops.
    urgent: Mapped[bool] = mapped_column(Boolean, default=False)
    urgent_criteria: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(Integer, default=3)  # 1 (low) .. 5 (high)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    sources: Mapped[list["Source"]] = relationship(
        back_populates="topic", cascade="all, delete-orphan", order_by="Source.id"
    )


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40))  # rss, reddit, page_watch, sitemap, youtube, ebay, websearch
    name: Mapped[str] = mapped_column(String(200), default="")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # None = fetched in the twice-daily cycle only. A number = also checked that often.
    interval_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    last_fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_ok_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    topic: Mapped[Topic] = relationship(back_populates="sources")


class Item(Base):
    """One piece of outside content: a Reddit post, a blog entry, a page change, an email."""

    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    url: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(200), default="")
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    # new -> picked/skipped -> discussed. alerted = sent as an urgent alert.
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    triage_note: Mapped[str] = mapped_column(Text, default="")

    topic: Mapped[Topic] = relationship()


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[int] = mapped_column(primary_key=True)
    handle: Mapped[str] = mapped_column(String(60), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    age: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    location: Mapped[str] = mapped_column(String(200), default="")
    emoji: Mapped[str] = mapped_column(String(16), default="🙂")
    color: Mapped[str] = mapped_column(String(16), default="#888888")
    bio: Mapped[str] = mapped_column(Text, default="")
    personality: Mapped[str] = mapped_column(Text, default="")
    voice: Mapped[str] = mapped_column(Text, default="")
    thought_process: Mapped[str] = mapped_column(Text, default="")
    persona: Mapped[dict] = mapped_column(JSON, default=dict)
    interests: Mapped[list] = mapped_column(JSON, default=list)  # topic slugs
    steering_notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Thread(Base):
    __tablename__ = "threads"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("topics.id", ondelete="SET NULL"), nullable=True, index=True
    )
    item_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("items.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text)
    by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    is_alert: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    topic: Mapped[Optional[Topic]] = relationship()
    item: Mapped[Optional[Item]] = relationship()
    posts: Mapped[list["Post"]] = relationship(
        back_populates="thread", cascade="all, delete-orphan", order_by="Post.id"
    )


class Post(Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )  # None = posted by the owner (you)
    reply_to_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    thread: Mapped[Thread] = relationship(back_populates="posts")
    agent: Mapped[Optional[Agent]] = relationship()
    feedback: Mapped[Optional["Feedback"]] = relationship(
        back_populates="post", uselist=False, cascade="all, delete-orphan"
    )


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), unique=True)
    agent_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    value: Mapped[int] = mapped_column(Integer)  # +1 or -1
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    post: Mapped[Post] = relationship(back_populates="feedback")


class Newsletter(Base):
    __tablename__ = "newsletters"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # daily, alert, weekly, test
    edition_key: Mapped[Optional[str]] = mapped_column(String(80), unique=True, nullable=True)
    subject: Mapped[str] = mapped_column(Text, default="")
    markdown: Mapped[str] = mapped_column(Text, default="")
    html: Mapped[str] = mapped_column(Text, default="")
    thread_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resend_id: Mapped[str] = mapped_column(String(100), default="")
    error: Mapped[str] = mapped_column(Text, default="")


class TopicSuggestion(Base):
    __tablename__ = "topic_suggestions"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_key: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    sources: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, added, dismissed
    token: Mapped[str] = mapped_column(String(64), unique=True)
    topic_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LlmCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    purpose: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(60))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    web_searches: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")


class JobRun(Base):
    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(60), index=True)
    # Unique per scheduled slot (e.g. "newsletter:2026-10-18:am") so a slot runs once.
    key: Mapped[Optional[str]] = mapped_column(String(120), unique=True, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    detail: Mapped[str] = mapped_column(Text, default="")


class InboundEmail(Base):
    __tablename__ = "inbound_emails"
    id: Mapped[int] = mapped_column(primary_key=True)
    resend_id: Mapped[str] = mapped_column(String(100), unique=True)
    from_addr: Mapped[str] = mapped_column(Text, default="")
    to_addrs: Mapped[list] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    topic_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="new")  # new, processed, ignored, error
    items_created: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")


class YoutubeChannel(Base):
    __tablename__ = "youtube_channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    added_via: Mapped[str] = mapped_column(String(20), default="manual")  # oauth, csv, manual
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LoginToken(Base):
    """One-time emailed sign-in links. The link itself is signed; this row makes it single-use."""
    __tablename__ = "login_tokens"
    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ReadingItem(Base):
    """A link the owner saved on the Reading page: summarized for the newsletter, optionally discussed."""

    __tablename__ = "reading_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    why_it_matters: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued, ready, error
    error: Mapped[str] = mapped_column(Text, default="")
    discuss: Mapped[bool] = mapped_column(Boolean, default=False)
    instructions: Mapped[str] = mapped_column(Text, default="")
    thread_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    newsletter_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # edition it went out in
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
