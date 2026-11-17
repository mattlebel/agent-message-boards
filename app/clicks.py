"""Learn from what you click.

Every link in a daily newsletter goes through a signed redirect (BASE_URL/r/<key>) that records the
click and sends you on. Opening a thread on the board counts too (once per thread per day). Clicks
are tied to a topic (through the thread, or the item with that URL), and topic_boost() turns the last
30 days of them into a small ranking bonus for picking threads and ordering the newsletter.

The redirect key is signed, so /r/ can't be used to send anyone to an arbitrary site.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.db import utcnow
from app.models import Click, Item, Thread

WINDOW = timedelta(days=30)
MAX_BOOST = 1.5  # in score points (scores run 0-10)


def _signer() -> URLSafeSerializer:
    from app.share import _secret

    return URLSafeSerializer(_secret(), salt="click")


def make_key(url: str, newsletter_id: int | None) -> str:
    return _signer().dumps({"u": url, "n": newsletter_id})


def read_key(key: str) -> tuple[str, int | None] | None:
    try:
        data = _signer().loads(key)
    except BadSignature:
        return None
    url = data.get("u") if isinstance(data, dict) else None
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    return url, data.get("n")


_HREF = re.compile(r'href="(https?://[^"]+)"')


def wrap_links(html: str, newsletter_id: int | None) -> str:
    """Send every link in the email through the click redirect (images are left alone)."""
    if not html:
        return html
    import html as html_lib

    def repl(m):
        url = html_lib.unescape(m.group(1))
        if url.startswith((f"{config.BASE_URL}/r/", f"{config.BASE_URL}/s/")):
            return m.group(0)  # already wrapped, or a one-click "add topic" link
        return f'href="{config.BASE_URL}/r/{make_key(url, newsletter_id)}"'

    return _HREF.sub(repl, html)


_THREAD = re.compile(re.escape(config.BASE_URL) + r"/t/(\d+)")


def _topic_for(session: Session, url: str) -> tuple[int | None, int | None]:
    m = _THREAD.match(url)
    if m:
        thread = session.get(Thread, int(m.group(1)))
        return (thread.id, thread.topic_id) if thread else (None, None)
    item = session.scalar(select(Item).where(Item.url == url).order_by(Item.id.desc()).limit(1))
    if item is None:
        return None, None
    thread = session.scalar(select(Thread).where(Thread.item_id == item.id).limit(1))
    return (thread.id if thread else None), item.topic_id


def record_email_click(session: Session, url: str, newsletter_id: int | None) -> None:
    thread_id, topic_id = _topic_for(session, url)
    session.add(Click(kind="email", url=url[:2000], newsletter_id=newsletter_id, thread_id=thread_id,
                      topic_id=topic_id))
    session.commit()


def record_open(session: Session, thread: Thread) -> None:
    since = utcnow() - timedelta(days=1)
    if session.scalar(select(Click.id).where(Click.kind == "open", Click.thread_id == thread.id,
                                             Click.created_at >= since).limit(1)):
        return
    session.add(Click(kind="open", thread_id=thread.id, topic_id=thread.topic_id))
    session.commit()


def topic_counts(session: Session) -> Counter:
    rows = session.scalars(select(Click).where(Click.created_at >= utcnow() - WINDOW, Click.topic_id.is_not(None)))
    counts: Counter = Counter()
    for c in rows:
        counts[c.topic_id] += 1.0 if c.kind == "email" else 0.5
    return counts


def topic_boost(session: Session) -> dict[int, float]:
    """{topic_id: bonus}: the most-clicked topic gets MAX_BOOST, the rest in proportion."""
    counts = topic_counts(session)
    if not counts:
        return {}
    top = max(counts.values())
    return {tid: MAX_BOOST * n / top for tid, n in counts.items()}


def summary(session: Session, limit: int = 5) -> str:
    """'Wallet (12 clicks), Inciardi (9)...' for prompts."""
    from app.models import Topic

    counts = topic_counts(session)
    parts = []
    for tid, n in counts.most_common(limit):
        topic = session.get(Topic, tid)
        if topic:
            parts.append(f"{topic.name} ({n:g})")
    return ", ".join(parts)
