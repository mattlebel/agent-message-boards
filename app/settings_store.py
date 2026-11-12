"""App settings editable from the admin Settings page (stored in the DB)."""

import copy
from typing import Any

from sqlalchemy.orm import Session

from app.models import Setting

DEFAULT_PROFILE = """\
I live in Back Bay, Boston, and work as Chief of Staff at a permanent carbon capture company.
I love running. I'm 9+1 qualified for the 2026 and 2027 TCS New York City Marathons and I'm running both.
I've finished 3 of the 6 World Marathon Majors; Chicago, London and Boston are left.
I love mile races and I'm more competitive at the mile than at longer distances.
For run clubs I care about very local Back Bay stuff (e.g. the Dick's Sporting Goods run club,
Tracksmith group runs on Newbury Street), not every club in Greater Boston.
I collect Inciardi mini prints. I want to know about new machines, limited-time machines and pop-ups
the moment they're announced. I'll travel the same week if it's within a 2-3 hour flight or a
4-5 hour train ride or drive from Boston.
I like travel (points, miles, premium cabins, hotel deals) and card offers (AmEx).
"""

DEFAULTS: dict[str, Any] = {
    "profile": DEFAULT_PROFILE,
    "owner_name": "Matt",
    "newsletter_name": "Board Stuff",
    "models": {
        "posts": "claude-sonnet-5-5",  # agent posts and replies
        "newsletter": "claude-opus-5-5",  # the daily essay
        "personas": "claude-opus-5-5",  # one-time persona creation
        "suggestions": "claude-opus-5-5",  # weekly topic ideas (uses web search)
        "fast": "claude-haiku-5-5",  # triage, classification, email parsing
        "search": "claude-sonnet-5-5",  # web-search sources
        "alerts": "claude-sonnet-5-5",  # deciding whether something is an urgent drop
    },
    "daily_budget_usd": 3.0,
    "threads_per_cycle": 5,
    "min_replies": 0,
    "max_replies": 2,
    # Times are US Eastern.
    "schedule": {
        "cycle_am": "05:00",
        "newsletter_am": "06:00",
        "cycle_pm": "18:00",
        "newsletter_pm": "19:00",
        "weekly_weekday": 6,  # Monday=0 .. Sunday=6
        "weekly_time": "15:00",
    },
    "recipients": [],  # empty = EMAIL_TO env var
    "style_guide": "",  # empty = built-in anti-"LLM speak" rules in app/style.py
    "paused": False,
}


def get(session: Session, key: str) -> Any:
    row = session.get(Setting, key)
    if row is None:
        return copy.deepcopy(DEFAULTS.get(key))
    value = row.value
    default = DEFAULTS.get(key)
    if isinstance(default, dict) and isinstance(value, dict):
        merged = copy.deepcopy(default)
        merged.update(value)
        return merged
    return value


def set(session: Session, key: str, value: Any) -> None:  # noqa: A001
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    session.flush()


def model_for(session: Session, role: str) -> str:
    return get(session, "models").get(role) or DEFAULTS["models"][role]
