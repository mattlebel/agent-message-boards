"""Environment configuration. Secrets live in Render env vars, never in the repo."""

import os
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str) -> bool:
    return _env(name).lower() in {"1", "true", "yes", "on"}


def _database_url() -> str:
    url = _env("DATABASE_URL", "sqlite:///./dev.db")
    # Render hands out postgres:// URLs; SQLAlchemy wants an explicit driver.
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _database_url()
SECRET_KEY = _env("SECRET_KEY", "dev-secret-change-me")
BASE_URL = _env("BASE_URL", "http://localhost:8000").rstrip("/")

# Local development only: skips login. Never set this on Render.
DEV_MODE = _flag("DEV_MODE")

GOOGLE_CLIENT_ID = _env("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = _env("GOOGLE_CLIENT_SECRET")
ALLOWED_EMAIL_DOMAIN = _env("ALLOWED_EMAIL_DOMAIN", "lebel.io").lower()

ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
# Fake LLM responses for local tests (no API calls, no cost).
LLM_FAKE = _flag("LLM_FAKE")

RESEND_API_KEY = _env("RESEND_API_KEY")
RESEND_WEBHOOK_SECRET = _env("RESEND_WEBHOOK_SECRET")
EMAIL_FROM = _env("EMAIL_FROM", "Board Stuff <board@lebel.io>")
EMAIL_TO = _env("EMAIL_TO", "agent-talk@lebel.io")
# The domain (or resend.app address domain) you forward newsletters to.
INBOUND_DOMAIN = _env("INBOUND_DOMAIN", "in.lebel.io")

REDDIT_CLIENT_ID = _env("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = _env("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = _env("REDDIT_USER_AGENT", "web:agent-message-boards:v1 (personal use)")

EBAY_CLIENT_ID = _env("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = _env("EBAY_CLIENT_SECRET")

TZ = ZoneInfo(_env("TIMEZONE", "America/New_York"))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
