"""Admin sign-in, limited to one email domain (lebel.io by default).

The main way in is a one-time link emailed through Resend. Google sign-in still works
if GOOGLE_CLIENT_ID is set, and is also what the one-click YouTube import uses.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from authlib.integrations.starlette_client import OAuth
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import delete, func, select

from app import config
from app.db import utcnow
from app.models import LoginToken

oauth = OAuth()
_GOOGLE_META = "https://accounts.google.com/.well-known/openid-configuration"

if config.GOOGLE_CLIENT_ID:
    oauth.register(
        name="google",
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        server_metadata_url=_GOOGLE_META,
        client_kwargs={"scope": "openid email profile"},
    )
    # Separate client for the one-time YouTube subscriptions import. It can be any Google
    # account (e.g. a personal Gmail with YouTube Premium), not just the admin login.
    oauth.register(
        name="google_yt",
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        server_metadata_url=_GOOGLE_META,
        client_kwargs={"scope": "openid email https://www.googleapis.com/auth/youtube.readonly"},
    )


class LoginRequired(Exception):
    pass


def email_allowed(email: str, hd: str | None = None) -> bool:
    email = (email or "").lower()
    domain = config.ALLOWED_EMAIL_DOMAIN
    return bool(domain) and email.endswith("@" + domain)


def current_user(request: Request) -> dict | None:
    if config.DEV_MODE:
        return {"email": f"dev@{config.ALLOWED_EMAIL_DOMAIN}", "name": "Dev"}
    user = request.session.get("user")
    if user and email_allowed(user.get("email", "")):
        return user
    return None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise LoginRequired()
    return user


def require_user_api(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return user


# --- emailed sign-in links ---------------------------------------------------------

LINK_MAX_AGE = 15 * 60  # seconds
LINKS_PER_WINDOW = 5    # per address, per LINK_MAX_AGE
_signer = URLSafeTimedSerializer(config.SECRET_KEY, salt="email-login")


def safe_next(url: str | None) -> str:
    url = url or "/"
    return url if url.startswith("/") and not url.startswith("//") else "/"


def can_send_link(session, email: str) -> bool:
    since = utcnow() - timedelta(seconds=LINK_MAX_AGE)
    recent = session.scalar(
        select(func.count()).select_from(LoginToken)
        .where(LoginToken.email == email, LoginToken.created_at >= since)
    ) or 0
    return recent < LINKS_PER_WINDOW


def make_login_token(session, email: str, next_url: str = "/") -> str:
    email = email.strip().lower()
    session.execute(delete(LoginToken).where(LoginToken.created_at < utcnow() - timedelta(days=1)))
    nonce = secrets.token_urlsafe(24)
    session.add(LoginToken(nonce=nonce, email=email))
    session.flush()
    return _signer.dumps({"e": email, "n": nonce, "next": safe_next(next_url)})


def _check(session, token: str) -> tuple[LoginToken, dict] | None:
    try:
        data = _signer.loads(token or "", max_age=LINK_MAX_AGE)
    except BadSignature:  # also covers expired links
        return None
    row = session.get(LoginToken, str(data.get("n", "")))
    if row is None or row.used_at is not None or row.email != data.get("e") or not email_allowed(row.email):
        return None
    return row, data


def peek_login_token(session, token: str) -> str | None:
    """The address a link signs in, without using it up (mail scanners open links too)."""
    found = _check(session, token)
    return found[0].email if found else None


def redeem_login_token(session, token: str) -> tuple[str, str] | None:
    """Uses the link up. Returns (email, next path) or None if it's invalid, expired or used."""
    found = _check(session, token)
    if not found:
        return None
    row, data = found
    row.used_at = utcnow()
    session.flush()
    return row.email, safe_next(data.get("next"))
