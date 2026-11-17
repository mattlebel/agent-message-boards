"""Planned trips: forwarded confirmations sorted into the right trip, plus researched ideas and events.

Forward anything trip-related to travel-planning@<inbound domain> (or trips@). Claude matches it to a
trip by destination and dates and pulls out the bookings. Unmatched emails wait in "Unsorted".
Upcoming trips get researched (web search) when added, then every few days until departure, and the
newsletter carries an "Upcoming trips" section.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, llm, style
from app.db import utcnow
from app.models import Trip, TripItem

log = logging.getLogger(__name__)

ADDRESSES = {"travel-planning", "trips", "trip"}
BOOKING_KINDS = ("flight", "hotel", "reservation")
KIND_LABELS = {"flight": "Flight", "hotel": "Stay", "reservation": "Booking", "event": "Event",
               "idea": "Idea", "note": "Note"}


def is_trip_address(addresses: list[str]) -> bool:
    for addr in addresses:
        local = str(addr).split("<")[-1].split("@")[0].lower().strip()
        local = local.split("+", 1)[0]
        if local in ADDRESSES:
            return True
    return False


def today() -> datetime:
    return datetime.now(config.TZ).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)


def upcoming(session: Session, within_days: int | None = None) -> list[Trip]:
    """Trips that haven't ended, soonest first (optionally only those starting within N days)."""
    now = today()
    rows = [t for t in session.scalars(select(Trip)) if (t.end_date or t.start_date or now) >= now]
    if within_days is not None:
        rows = [t for t in rows if t.start_date and t.start_date <= now + timedelta(days=within_days)]
    return sorted(rows, key=lambda t: t.start_date or datetime.max)


def describe(trip: Trip) -> str:
    when = ""
    if trip.start_date:
        when = f"{trip.start_date:%b %-d}" + (f" to {trip.end_date:%b %-d, %Y}" if trip.end_date else f", {trip.start_date:%Y}")
    return f"{trip.name} ({trip.destination}{', ' + when if when else ''})"


def when_label(dt: datetime | None) -> str:
    """'Tue Mar 3, 10:55', or just 'Tue Mar 3' when no time is known."""
    if not dt:
        return ""
    return f"{dt:%a %b %-d}" + (f", {dt:%H:%M}" if (dt.hour, dt.minute) != (0, 0) else "")


def countdown(trip: Trip) -> str:
    if not trip.start_date:
        return ""
    days = (trip.start_date - today()).days
    if days > 1:
        return f"in {days} days"
    if days == 1:
        return "tomorrow"
    if days == 0:
        return "today"
    return "under way" if trip.end_date and trip.end_date >= today() else "done"


# --- forwarded emails --------------------------------------------------------------------------


class _Booking(llm.Lenient):
    kind: str = Field(default="note", description="flight, hotel, reservation (restaurant, tickets, tour), event, idea or note")
    title: str = Field(default="", description="Short, e.g. 'JL7 BOS-NRT' or 'Park Hyatt Kyoto, 3 nights'")
    starts_at: str = Field(default="", description="ISO date or date-time it happens, local to the place, if known")
    details: str = Field(default="", description="The useful specifics in one or two lines: times, confirmation code, address")
    url: str = ""


class _Sorted(llm.Lenient):
    trip_id: int = Field(default=0, description="The matching trip's id, or 0 if it fits none")
    items: list[_Booking] = Field(default_factory=list)


def process_email(session: Session, row, text: str) -> int:
    """File a forwarded email into a trip. Returns how many items were added."""
    trips = upcoming(session)
    listing = "\n".join(f"- id {t.id}: {describe(t)}" for t in trips) or "(no trips yet)"
    res = llm.generate(
        purpose="trip_email", role="fast",
        system=("You file travel emails (confirmations, itineraries, tickets, ideas) into the right planned trip "
                "by destination and dates, and pull out what matters. Skip marketing filler."),
        user=f"Planned trips:\n{listing}\n\nEmail subject: {row.subject}\nFrom: {row.from_addr}\n\n{text[:20000]}",
        max_tokens=3000, effort="low", schema=_Sorted, critical=True,
        fake=lambda: _Sorted(trip_id=trips[0].id if trips else 0,
                             items=[_Booking(kind="note", title=row.subject or "Forwarded email")]),
    )
    sorted_ = res.parsed
    trip_id = sorted_.trip_id if any(t.id == sorted_.trip_id for t in trips) else None
    added = 0
    for b in sorted_.items[:12]:
        session.add(TripItem(
            trip_id=trip_id, kind=b.kind if b.kind in KIND_LABELS else "note", title=b.title[:500] or row.subject,
            details=b.details[:2000], url=b.url if b.url.startswith("http") else "",
            starts_at=_parse_when(b.starts_at), source="email", inbound_id=row.id,
        ))
        added += 1
    session.flush()
    return added


def _parse_when(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "")[:19]).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.fromisoformat(value.strip()[:10])
        except ValueError:
            return None


# --- research: ideas and events -----------------------------------------------------------------


class _Found(llm.Lenient):
    kind: str = Field(default="idea", description="event (dated, happening during the trip) or idea")
    title: str = ""
    when: str = Field(default="", description="ISO date for events, empty for ideas")
    url: str = ""
    why: str = Field(default="", description="One plain line on why it fits this person")


class _FoundList(llm.Lenient):
    items: list[_Found] = Field(default_factory=list)


def research_due(trip: Trip, now: datetime | None = None) -> bool:
    now = now or utcnow()
    if not trip.start_date or (trip.end_date or trip.start_date) < today():
        return False
    days_out = (trip.start_date - today()).days
    if days_out > 60:
        return False
    gap = timedelta(days=1) if days_out <= 7 else timedelta(days=3)
    return trip.last_researched_at is None or trip.last_researched_at <= now - gap + timedelta(hours=1)


def research(session: Session, trip: Trip) -> int:
    from app.wallet import profile_text

    known = [i.title for i in session.scalars(select(TripItem).where(TripItem.trip_id == trip.id))]
    end = trip.end_date or trip.start_date
    res = llm.generate(
        purpose="trip_research", role="search",
        system=("You plan trips for one person. Use web search. Events must actually happen during their dates; "
                "ideas must fit who they are. Facts only, with real links. " + style.SELF_EDIT
                + " Respond with JSON only.\n\nAbout them:\n" + profile_text(session)),
        user=(f"Trip: {describe(trip)}. Dates: {trip.start_date:%Y-%m-%d} to {end:%Y-%m-%d}.\n"
              + (f"Their notes: {trip.notes}\n" if trip.notes else "")
              + (f"Already on the list (don't repeat): {'; '.join(known[:40])}\n" if known else "")
              + "Find up to 8 new things: events during those dates (concerts, exhibitions, festivals, games, "
                "races or group runs) and ideas that fit them (food, running routes, neighborhoods, shops). "
                'JSON: {"items": [{"kind": "event|idea", "title": "...", "when": "YYYY-MM-DD", "url": "...", "why": "..."}]}'),
        max_tokens=4000, effort="low", schema=_FoundList, web_search=True, max_searches=5, critical=True,
        fake=lambda: _FoundList(items=[]),
    )
    seen = {t.lower() for t in known}
    added = 0
    for f in res.parsed.items[:8]:
        if not f.title or f.title.lower() in seen:
            continue
        session.add(TripItem(trip_id=trip.id, kind="event" if f.kind == "event" else "idea", title=f.title[:500],
                             details=f.why[:1000], url=f.url if f.url.startswith("http") else "",
                             starts_at=_parse_when(f.when), source="research"))
        seen.add(f.title.lower())
        added += 1
    trip.last_researched_at = utcnow()
    session.commit()
    return added


def research_due_trips(session: Session) -> int:
    n = 0
    for trip in upcoming(session):
        if research_due(trip):
            try:
                n += research(session, trip)
            except (llm.LlmError, llm.BudgetExceeded):
                log.exception("trip research failed for %s", trip.id)
                session.rollback()
    return n


# --- pages and newsletter -------------------------------------------------------------------------


def items_for(session: Session, trip: Trip) -> dict[str, list[TripItem]]:
    rows = list(session.scalars(select(TripItem).where(TripItem.trip_id == trip.id, TripItem.dismissed.is_(False))))
    bookings = sorted([r for r in rows if r.kind in BOOKING_KINDS], key=lambda r: r.starts_at or datetime.max)
    events = sorted([r for r in rows if r.kind == "event"], key=lambda r: r.starts_at or datetime.max)
    ideas = [r for r in rows if r.kind == "idea"]
    notes = [r for r in rows if r.kind == "note"]
    return {"bookings": bookings, "events": events, "ideas": ideas, "notes": notes}


def unsorted(session: Session) -> list[TripItem]:
    return list(session.scalars(select(TripItem).where(TripItem.trip_id.is_(None), TripItem.dismissed.is_(False))
                                .order_by(TripItem.id.desc())))


def for_newsletter(session: Session) -> tuple[list[Trip], list[TripItem]]:
    """Trips starting within 45 days (or under way), and their items not yet in a newsletter."""
    trips = upcoming(session, within_days=45)
    ids = [t.id for t in trips]
    fresh = list(session.scalars(select(TripItem).where(
        TripItem.trip_id.in_(ids or [0]), TripItem.newsletter_id.is_(None), TripItem.dismissed.is_(False))))
    return trips, fresh


def newsletter_material(session: Session, trips: list[Trip], fresh: list[TripItem]) -> str:
    parts = []
    for trip in trips:
        block = f"### {describe(trip)} — {countdown(trip)}"
        days_out = (trip.start_date - today()).days if trip.start_date else 99
        if days_out <= 7:
            bookings = items_for(session, trip)["bookings"]
            if trip.flights:
                block += f"\nFlights (from the reader): {trip.flights}"
            if bookings:
                block += "\nBookings:\n" + "\n".join(
                    f"- {KIND_LABELS[b.kind]}: {b.title}{f' ({when_label(b.starts_at)})' if b.starts_at else ''} {b.details}"
                    for b in bookings)
        new = [i for i in fresh if i.trip_id == trip.id and i.kind in ("event", "idea")]
        if new:
            block += "\nNew ideas and events:\n" + "\n".join(
                f"- {KIND_LABELS[i.kind]}: {i.title}{f' ({when_label(i.starts_at)})' if i.starts_at else ''} {i.url} | {i.details}"
                for i in new)
        parts.append(block)
    return "\n\n".join(parts)
