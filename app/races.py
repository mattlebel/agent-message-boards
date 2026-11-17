"""Races: target races with their registration windows, lottery draws and qualifying cutoffs.

Each race is researched with web search (official race sites first) when added, then every two weeks,
or every three days while one of its dates is within a month. Dates you add by hand are never replaced.
Alert emails go out a week before and the day before a registration window closes, and on the day a
window opens or a draw happens. The newsletter gets the dates that are coming up.
"""

from __future__ import annotations

import html as html_lib
import logging
from datetime import datetime, timedelta

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, emailer, llm, settings_store
from app.db import utcnow
from app.models import Newsletter, Race, RaceDate

log = logging.getLogger(__name__)

KINDS = {"opens": "Registration opens", "closes": "Registration closes", "draw": "Lottery draw",
         "cutoff": "Qualifying cutoff", "race": "Race day", "other": "Date"}
ALERTS = {"opens": ("today",), "draw": ("today",), "closes": ("week", "day"), "cutoff": ("week",)}

# Starting list: the owner's remaining majors, NYC 2027 (9+1 qualified) and mile races.
DEFAULT_RACES = [
    {"name": "Bank of America Chicago Marathon 2027", "kind": "major", "location": "Chicago",
     "notes": "World Major still to run. Lottery or charity entry; time qualifier if eligible."},
    {"name": "TCS London Marathon 2027", "kind": "major", "location": "London",
     "notes": "World Major still to run. Ballot opens right after the 2026 race; Good for Age and charity routes."},
    {"name": "Boston Marathon 2027", "kind": "major", "location": "Boston",
     "notes": "World Major still to run, at home. Qualifying window, registration week in September, cutoff time."},
    {"name": "TCS New York City Marathon 2027", "kind": "major", "location": "New York",
     "status": "target", "notes": "9+1 qualified: needs to register in the 9+1 window."},
    {"name": "Road mile races near Boston and NYC (2026-2027)", "kind": "mile", "location": "Northeast",
     "notes": "Competitive at the mile. Road miles and open track miles within a train ride: "
              "New Balance 5th Avenue Mile, B.A.A. events, local road miles."},
]


def today() -> datetime:
    return datetime.now(config.TZ).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)


def countdown(dt: datetime | None) -> str:
    if dt is None:
        return ""
    days = (dt.replace(hour=0, minute=0, second=0, microsecond=0) - today()).days
    if days < 0:
        return "past"
    return {0: "today", 1: "tomorrow"}.get(days, f"in {days} days")


def active_races(session: Session) -> list[Race]:
    rows = [r for r in session.scalars(select(Race)) if r.status in ("target", "registered")]
    return sorted(rows, key=lambda r: r.race_date or datetime.max)


def dates_for(session: Session, race: Race) -> list[RaceDate]:
    return list(session.scalars(select(RaceDate).where(RaceDate.race_id == race.id).order_by(RaceDate.date)))


def upcoming_dates(session: Session, within_days: int) -> list[tuple[Race, RaceDate]]:
    races = {r.id: r for r in active_races(session)}
    end = today() + timedelta(days=within_days)
    rows = session.scalars(select(RaceDate).where(RaceDate.date >= today(), RaceDate.date <= end)
                           .order_by(RaceDate.date))
    return [(races[d.race_id], d) for d in rows if d.race_id in races]


# --- research ---------------------------------------------------------------------------


class _Date(llm.Lenient):
    kind: str = Field(default="other", description="opens, closes, draw, cutoff, race, or other")
    label: str = Field(default="", description="Plain label, e.g. 'Lottery application window opens'")
    date: str = Field(default="", description="YYYY-MM-DD")
    confirmed: bool = Field(default=False, description="True if the organizer has announced it; false if expected")
    url: str = ""


class _Found(llm.Lenient):
    race_date: str = Field(default="", description="YYYY-MM-DD of the race, if known")
    location: str = ""
    url: str = Field(default="", description="The official race page")
    how_to_enter: str = Field(default="", description="2-4 plain sentences: entry routes, qualifying standard, cutoff")
    dates: list[_Date] = Field(default_factory=list)


def _day(value: str) -> datetime | None:
    try:
        return datetime.strptime((value or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None


def research_due(session: Session, race: Race, now: datetime | None = None) -> bool:
    if race.status not in ("target", "registered"):
        return False
    now = now or utcnow()
    if race.last_researched_at is None:
        return True
    soon = any(d.date <= today() + timedelta(days=30) for d in dates_for(session, race) if d.date >= today())
    gap = timedelta(days=3) if soon else timedelta(days=14)
    return race.last_researched_at <= now - gap + timedelta(hours=1)


def research(session: Session, race: Race) -> int:
    """Look up the race's dates. Replaces earlier researched dates (keeping which alerts were sent)."""
    from app.wallet import profile_text

    known = "\n".join(f"- {d.label}: {d.date:%Y-%m-%d}" for d in dates_for(session, race))
    res = llm.generate(
        purpose="race_research", role="search",
        system=("You track race registration for one runner. Use web search and prefer the race's official site "
                "and the organizer's announcements over blogs. Only give dates you found; mark a date confirmed "
                "only if the organizer announced it. Never guess a date. Respond with JSON only.\n\n"
                "About the runner:\n" + profile_text(session)),
        user=(f"Race: {race.name}" + (f" ({race.location})" if race.location else "")
              + (f"\nWhat the runner is after: {race.notes}" if race.notes else "")
              + (f"\nDates on file:\n{known}" if known else "")
              + f"\nToday is {today():%Y-%m-%d}. Find the race date and every upcoming entry date: registration "
                "or lottery/ballot window opening and closing, draw or notification dates, qualifying windows and "
                "cutoff announcements. For a group of races (e.g. mile races), list each race as a 'race' date with "
                "its name in the label, plus its registration dates. "
                'JSON: {"race_date": "YYYY-MM-DD", "location": "...", "url": "...", "how_to_enter": "...", '
                '"dates": [{"kind": "opens|closes|draw|cutoff|race|other", "label": "...", "date": "YYYY-MM-DD", '
                '"confirmed": true, "url": "..."}]}'),
        max_tokens=4000, effort="low", schema=_Found, web_search=True, max_searches=5, critical=True,
        fake=lambda: _Found(),
    )
    found: _Found = res.parsed
    if _day(found.race_date) and race.kind != "mile":
        race.race_date = _day(found.race_date)
    if found.location and not race.location:
        race.location = found.location[:200]
    if found.url.startswith("http") and not race.url:
        race.url = found.url
    if found.how_to_enter:
        race.research_notes = found.how_to_enter[:2000]
    old = {(d.kind, d.date.date()): d for d in dates_for(session, race) if d.source == "research"}
    added = 0
    for f in found.dates[:20]:
        when = _day(f.date)
        if when is None:
            continue
        kind = f.kind if f.kind in KINDS else "other"
        prev = old.pop((kind, when.date()), None)
        if prev is not None:
            prev.label, prev.confirmed = (f.label or prev.label)[:300], f.confirmed
            prev.url = f.url if f.url.startswith("http") else prev.url
            continue
        session.add(RaceDate(race_id=race.id, kind=kind, label=(f.label or KINDS[kind])[:300], date=when,
                             confirmed=f.confirmed, url=f.url if f.url.startswith("http") else "",
                             source="research"))
        added += 1
    for stale in old.values():
        if stale.date >= today():  # moved or no longer listed; past ones stay as history
            session.delete(stale)
    race.last_researched_at = utcnow()
    session.commit()
    return added


def research_due_races(session: Session) -> int:
    n = 0
    for race in active_races(session):
        if research_due(session, race):
            try:
                n += research(session, race)
            except (llm.LlmError, llm.BudgetExceeded):
                log.exception("race research failed for %s", race.id)
                session.rollback()
    return n


# --- alerts -----------------------------------------------------------------------------


def due_alerts(session: Session) -> list[tuple[Race, RaceDate, str]]:
    """(race, date, which) for alerts that should go out now and haven't."""
    out = []
    for race, d in upcoming_dates(session, 8):
        days = (d.date - today()).days
        sent = set(filter(None, d.alerted.split(",")))
        for which in ALERTS.get(d.kind, ()):
            due = {"today": days == 0, "day": days == 1, "week": 2 <= days <= 7}[which]
            if due and which not in sent:
                out.append((race, d, which))
    return out


def _alert_subject(race: Race, d: RaceDate, which: str) -> str:
    when = {"today": "today", "day": "tomorrow", "week": f"{d.date:%a %b %-d}"}[which]
    return f"🏃 {race.name}: {d.label or KINDS.get(d.kind, 'date')} {when}"[:150]


def send_alerts(session: Session) -> int:
    sent = 0
    name = settings_store.get(session, "newsletter_name")
    for race, d, which in due_alerts(session):
        subject = _alert_subject(race, d, which)
        rows = [("Date", f"{d.date:%A %B %-d, %Y}" + ("" if d.confirmed else " (expected, not yet announced)")),
                ("Race", race.name + (f", {race.race_date:%B %-d, %Y}" if race.race_date else "")),
                ("How entry works", race.research_notes), ("Your notes", race.notes)]
        link = d.url or race.url
        body = "".join(f"<p><b>{html_lib.escape(k)}:</b> {html_lib.escape(v)}</p>" for k, v in rows if v)
        if link:
            body += f'<p><a href="{html_lib.escape(link)}">Official page</a></p>'
        body += f'<p><a href="{config.BASE_URL}/races">All your races</a></p>'
        html = emailer.wrap(subject, body, f"{name} · Races", emailer.footer())
        nl = Newsletter(kind="alert", subject=subject, html=html,
                        markdown="\n".join(f"{k}: {v}" for k, v in rows if v) + (f"\n{link}" if link else ""))
        session.add(nl)
        session.flush()
        from app.newsletter import send_newsletter

        send_newsletter(session, nl)
        d.alerted = ",".join(sorted(set(filter(None, d.alerted.split(","))) | {which}))
        session.commit()
        sent += 1
    return sent


# --- newsletter ---------------------------------------------------------------------------


def for_newsletter(session: Session, morning: bool) -> list[tuple[Race, RaceDate]]:
    return upcoming_dates(session, 7 if morning else 45)


def newsletter_material(rows: list[tuple[Race, RaceDate]]) -> str:
    return "\n".join(
        f"- {d.date:%a %b %-d} ({countdown(d.date)}): {race.name}: {d.label or KINDS.get(d.kind)}"
        + ("" if d.confirmed else " [expected, not announced]") + (f" ({d.url or race.url})" if d.url or race.url else "")
        for race, d in rows
    )


def seed_defaults(session: Session) -> int:
    if session.scalar(select(Race.id).limit(1)):
        return 0
    for r in DEFAULT_RACES:
        session.add(Race(name=r["name"], kind=r["kind"], location=r.get("location", ""),
                         status=r.get("status", "target"), notes=r.get("notes", "")))
    session.commit()
    return len(DEFAULT_RACES)
