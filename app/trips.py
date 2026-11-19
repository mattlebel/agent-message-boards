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


# --- tasks: "go find out X for this trip" -----------------------------------------------------------

TASK_SLUG = "trip-tasks"


class _Pick(llm.Lenient):
    name: str = ""
    kind: str = Field(default="idea", description="idea, or event if it happens at a set time")
    why: str = Field(default="", description="One or two plain sentences: why it fits this request and these people")
    where: str = Field(default="", description="Neighborhood or address, and how far from where they're staying if known")
    when: str = Field(default="", description="YYYY-MM-DD for an event, or opening hours that matter for the request")
    url: str = Field(default="", description="Official site or booking page")


class _TaskResult(llm.Lenient):
    summary: str = Field(default="", description="2-4 plain sentences answering the request directly: the top pick and why")
    plan: str = Field(default="", description=(
        "For a route, schedule or itinerary request: the plan as short lines, one stop per line, in order, "
        "with day and rough time (e.g. 'Sun 10:00 Guggenheim (walk-in, quick)'). Empty for a simple pick list."))
    picks: list[_Pick] = Field(default_factory=list)


def ensure_task_topic(session: Session):
    from app.models import Topic

    topic = session.scalar(select(Topic).where(Topic.slug == TASK_SLUG))
    if topic is None:
        topic = Topic(slug=TASK_SLUG, name="Trip tasks", priority=5,
                      description="Things the owner asked the board to find out for an upcoming trip. Answer the "
                                  "request using the research in the material; add only what it supports.")
        session.add(topic)
        session.flush()
    return topic


def _trip_context(session: Session, trip: Trip) -> str:
    end = trip.end_date or trip.start_date
    lines = [f"Trip: {describe(trip)}."]
    if trip.start_date:
        lines.append(f"Dates: {trip.start_date:%A %Y-%m-%d} to {end:%A %Y-%m-%d}.")
    items = items_for(session, trip)
    if trip.flights:
        lines.append(f"Flights: {trip.flights}")
    for b in items["bookings"]:
        lines.append(f"{KIND_LABELS[b.kind]}: {b.title}" + (f" ({when_label(b.starts_at)})" if b.starts_at else "")
                     + (f". {b.details}" if b.details else ""))
    if trip.notes:
        lines.append(f"The owner's notes for this trip: {trip.notes}")
    return "\n".join(lines)


_STOP = {"Find", "Map", "Ideally", "Near", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
         "Saturday", "Want", "Something", "Make", "Plan", "Look", "What", "Which", "Where", "When", "With", "That",
         "This", "There", "They", "Some", "Also", "Then", "Just", "Like", "Please", "Help", "Give"}
_ALIASES = {"new york": ["new york", "nyc", "manhattan", "brooklyn", "queens", "bronx"],
            "boston": ["boston", "cambridge", "back bay"], "chicago": ["chicago"], "london": ["london"]}


def _search_terms(trip: Trip, text: str) -> list[str]:
    terms: list[str] = []
    dest = (trip.destination or "").lower()
    for key, names in _ALIASES.items():
        if key in dest:
            terms += names
    terms += [p.strip() for p in dest.split(",") if len(p.strip()) > 3]
    import re

    for words in re.findall(r"(?:[A-Z][\w'&]+(?:\s+(?:are|of|the|at)?\s*[A-Z0-9][\w'&]+)*)", f"{text}\n{trip.notes}"):
        if words.split()[0] not in _STOP and len(words) > 3:
            terms.append(words.lower())
    return list(dict.fromkeys(terms))[:30]


def board_knowledge(session: Session, trip: Trip, text: str, limit: int = 25) -> str:
    """What the board already collected that bears on this trip (e.g. machine locations, events there)."""
    from app.models import Item
    from app.triage import item_brief

    terms = _search_terms(trip, text)
    if not terms:
        return ""
    since = utcnow() - timedelta(days=60)
    hits = []
    for it in session.scalars(select(Item).where(Item.fetched_at >= since)):
        hay = f"{it.title}\n{it.body}".lower()
        n = sum(1 for t in terms if t in hay)
        if n:
            hits.append((n, it.score or 0, it.fetched_at, it))
    hits.sort(key=lambda h: (h[0], h[1], h[2]), reverse=True)
    return "\n\n".join(item_brief(h[3], 500) for h in hits[:limit])


def task_plan(task) -> str:
    """The itinerary, for route/schedule tasks (kept as the first entry of picks)."""
    return next((p["plan"] for p in task.picks or [] if "plan" in p), "")


def task_picks(task) -> list[dict]:
    return [p for p in task.picks or [] if "plan" not in p]


def task_material(task) -> str:
    lines = [task.summary, ""]
    if task_plan(task):
        lines += ["Plan:", task_plan(task), ""]
    for p in task_picks(task):
        lines.append(f"- {p.get('name')}" + (f" ({p.get('where')})" if p.get("where") else "")
                     + (f", {p.get('when')}" if p.get("when") else "") + f": {p.get('why', '')}"
                     + (f" {p.get('url')}" if p.get("url") else ""))
    return "\n".join(lines).strip()


def run_task(session: Session, task_id: int) -> None:
    """Research the task with web search, save the picks to the trip, and open a board thread on it."""
    from app import discussion
    from app.models import Item, Post, Thread, TripTask
    from app.sources.base import dedupe_key
    from app.wallet import profile_text

    task = session.get(TripTask, task_id)
    trip = session.get(Trip, task.trip_id) if task else None
    if task is None or trip is None:
        return
    # A redo replaces the earlier answer: its picks on the trip, and its board thread.
    old = {p.get("name") for p in task_picks(task)}
    if old:
        for ti in session.scalars(select(TripItem).where(TripItem.trip_id == trip.id, TripItem.source == "task",
                                                         TripItem.title.in_(old))):
            session.delete(ti)
    if task.thread_id:
        old_thread = session.get(Thread, task.thread_id)
        if old_thread is not None:
            old_item = old_thread.item
            session.delete(old_thread)
            session.flush()
            if old_item is not None:
                session.delete(old_item)
        task.thread_id = None
    task.summary, task.picks = "", []
    task.status, task.error = "working", ""
    session.commit()
    system = ("You do research and planning for one person's upcoming trip, like a sharp concierge. Use web search "
              "a lot, and fetch the pages that settle the details: official sites, Resy/OpenTable listings, "
              "museum and venue pages, recent news. Check what the request depends on (open on that day, the hours, "
              "price range, lines and purchase limits, distance from where they're staying) for this year's dates. "
              "Recommend only places and events you found. Give real links. Never put people's names in search "
              "queries. Facts only; no hype. Don't describe your research or its limits; if one detail that matters "
              "couldn't be confirmed, say so in a few words inside that pick (e.g. 'confirm Sunday hours'). "
              + style.SELF_EDIT + " Respond with JSON only.\n\nAbout the owner:\n" + profile_text(session))
    known = board_knowledge(session, trip, task.text)
    user = (f"{_trip_context(session, trip)}\n\nToday is {today():%A %Y-%m-%d}.\n\n"
            + (f"WHAT THE BOARD ALREADY KNOWS (recent items it collected; use these facts and links, and check "
               f"them against current pages):\n{known}\n\n" if known else "")
            + f"THE REQUEST:\n{task.text}\n\n"
            "For a list of options, give the best 3-6. For a route or schedule, cover every stop they listed and "
            "put the order in plan. "
            'JSON: {"summary": "...", "plan": "...", "picks": [{"name": "...", "kind": "idea|event", "why": "...", '
            '"where": "...", "when": "...", "url": "..."}]}')
    try:
        res = llm.generate(
            purpose="trip_task", role="tasks", system=system, user=user, max_tokens=12000, effort="high",
            schema=_TaskResult, web_search=True, max_searches=20, web_fetch=True, max_fetches=12, critical=True,
            fake=lambda: _TaskResult(summary="(fake) Two good options near the hotel.",
                                     picks=[_Pick(name="Fake Bistro", why="Like the places you listed.",
                                                  url="https://example.com/bistro")]),
        )
        result = style.polish(res.parsed, purpose="trip_task", role="tasks", system=system, user=user,
                              critical=True, max_tokens=12000)
    except (llm.LlmError, llm.BudgetExceeded) as e:
        log.exception("trip task %s failed", task_id)
        session.rollback()
        task = session.get(TripTask, task_id)
        task.status, task.error, task.finished_at = "error", str(e)[:500], utcnow()
        session.commit()
        return

    task.summary = result.summary
    task.picks = [p.model_dump() for p in result.picks[:10] if p.name]
    if result.plan.strip():
        task.picks = [{"plan": result.plan.strip()}] + task.picks  # stored with the picks (no extra column)
    for p in result.picks[:10]:
        if not p.name:
            continue
        session.add(TripItem(trip_id=trip.id, kind="event" if p.kind == "event" else "idea", title=p.name[:500],
                             details=" · ".join(x for x in (p.where, p.why) if x)[:1000],
                             url=p.url if p.url.startswith("http") else "", starts_at=_parse_when(p.when),
                             source="task",
                             newsletter_id=0))  # 0 = covered by the task's own thread, not "Upcoming trips"
    topic = ensure_task_topic(session)
    item = Item(topic_id=topic.id, dedupe_key=dedupe_key(f"trip-task:{task.id}:{utcnow().isoformat()}"),
                title=f"{trip.name}: {task.text[:200]}",
                body=(f"{_trip_context(session, trip)}\n\nResearch for the owner's request:\n{task_material(task)}")[:6000],
                status="discussed", extra={"source_name": f"Trip task for {trip.name}", "source_kind": "trip_task"})
    session.add(item)
    session.flush()
    first = next((ln.strip() for ln in task.text.splitlines() if ln.strip()), "Trip task")
    title = f"{trip.name}: " + (first if len(first) <= 80 else first[:77].rsplit(" ", 1)[0] + "…")
    thread = Thread(topic_id=topic.id, item_id=item.id, title=title[:200], by_user=True)
    session.add(thread)
    session.flush()
    thread.posts.append(Post(agent_id=None, body=task.text[:6000]))
    thread.last_activity_at = utcnow()
    task.thread_id = thread.id
    task.status, task.finished_at = "done", utcnow()
    session.commit()
    discussion.respond_to_owner(session, thread.id)


def tasks_for(session: Session, trip: Trip) -> list:
    from app.models import TripTask

    return list(session.scalars(select(TripTask).where(TripTask.trip_id == trip.id).order_by(TripTask.id.desc())))
