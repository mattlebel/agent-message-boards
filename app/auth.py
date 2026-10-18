"""Google sign-in, limited to one email domain (lebel.io by default)."""

from __future__ import annotations

from authlib.integrations.starlette_client import OAuth
from fastapi import HTTPException, Request

from app import config

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
