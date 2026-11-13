"""Expiring share links for board threads in emails.

Each board link in an outgoing email gets a signed key (?k=...) that opens a read-only view of that
one thread without signing in, until 24 hours after the email is delivered. The key names the
thread and its expiry and is signed, so it can't be edited to open another thread or to last longer.

The signing secret lives in the database ("share_secret" setting), not in an environment variable:
emails are sent by the scheduled job and links are checked by the web service, and both must use
the same secret. Deleting that setting revokes every outstanding link (a new one is made).
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.exc import IntegrityError

from app import config

LINK_LIFETIME = timedelta(hours=24)
_cache: dict = {"secret": None}


def _secret() -> str:
    """The shared signing secret, created once and stored in the database."""
    if _cache["secret"]:
        return _cache["secret"]
    from app import settings_store
    from app.db import SessionLocal

    with SessionLocal() as s:
        value = settings_store.get(s, "share_secret")
        if not value:
            try:
                value = secrets.token_urlsafe(32)
                settings_store.set(s, "share_secret", value)
                s.commit()
            except IntegrityError:  # another process made it first; use theirs
                s.rollback()
                value = settings_store.get(s, "share_secret")
    _cache["secret"] = value
    return value


def _signer() -> URLSafeSerializer:
    return URLSafeSerializer(_secret(), salt="thread-share")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_token(thread_id: int, expires_at: datetime) -> str:
    return _signer().dumps({"t": int(thread_id), "e": int(expires_at.timestamp())})


def check_token(thread_id: int, token: str | None) -> datetime | None:
    """The link's expiry if the key is genuine, for this thread, and not expired; else None."""
    if not token:
        return None
    try:
        data = _signer().loads(token)
    except BadSignature:
        return None
    if not isinstance(data, dict) or data.get("t") != int(thread_id):
        return None
    expires = datetime.fromtimestamp(int(data.get("e", 0)), tz=timezone.utc)
    return expires if expires > _now() else None


def tokenize_links(content: str, delivered_at: datetime | None = None) -> str:
    """Add a share key to every board thread link (BASE_URL/t/<id>, with or without #fragment)."""
    if not content:
        return content
    delivered = delivered_at or _now()
    if delivered.tzinfo is None:
        delivered = delivered.replace(tzinfo=timezone.utc)
    expires = delivered + LINK_LIFETIME
    pattern = re.compile(re.escape(config.BASE_URL) + r"/t/(\d+)(?![\d?/])(#[\w-]+)?")
    return pattern.sub(
        lambda m: f"{config.BASE_URL}/t/{m.group(1)}?k={make_token(int(m.group(1)), expires)}{m.group(2) or ''}",
        content,
    )
