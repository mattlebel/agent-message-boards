from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import llm, newsletter, trips
from app.db import init_db, session_scope, utcnow
from app.main import app
from app.models import InboundEmail, Newsletter, Trip, TripItem


def _cleanup(s):
    for model in (TripItem, Trip, InboundEmail):
        for obj in s.scalars(select(model)):
            s.delete(obj)


def test_trip_address_detection():
    assert trips.is_trip_address(["travel-planning@in.lebel.io"])
    assert trips.is_trip_address(["Matt <trips+japan@in.lebel.io>"])
    assert not trips.is_trip_address(["travel@in.lebel.io", "feeds@in.lebel.io"])


def test_research_timing():
    today = trips.today()
    far = Trip(name="far", start_date=today + timedelta(days=90))
    soon = Trip(name="soon", start_date=today + timedelta(days=20), last_researched_at=utcnow() - timedelta(days=1))
    week = Trip(name="week", start_date=today + timedelta(days=5), last_researched_at=utcnow() - timedelta(days=1))
    past = Trip(name="past", start_date=today - timedelta(days=10), end_date=today - timedelta(days=3))
    assert not trips.research_due(far)
    assert not trips.research_due(soon)  # every 3 days until the final week
    assert trips.research_due(week)  # daily in the final week
    assert not trips.research_due(past)
    assert trips.research_due(Trip(name="new", start_date=today + timedelta(days=30)))


def test_trips_end_to_end(monkeypatch):
    init_db()
    client = TestClient(app)
    start = (trips.today() + timedelta(days=5)).strftime("%Y-%m-%d")
    end = (trips.today() + timedelta(days=12)).strftime("%Y-%m-%d")
    r = client.post("/trips", data={"name": "Japan in March", "destination": "Tokyo, Kyoto", "start_date": start,
                                    "end_date": end, "flights": "JL7 BOS-NRT"}, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        trip = s.scalar(select(Trip).where(Trip.name == "Japan in March"))
        assert trip.last_researched_at is not None  # researched right away

        # A forwarded confirmation lands in the matching trip...
        def sorted_into_trip(**kw):
            return llm.LlmResult(text="", parsed=trips._Sorted(trip_id=trip.id, items=[
                trips._Booking(kind="hotel", title="Park Hyatt Kyoto, 3 nights", starts_at=start,
                               details="Conf ABC123")]))
        monkeypatch.setattr(llm, "generate", sorted_into_trip)
        r = client.post("/webhooks/resend", json={"type": "email.received", "data": {
            "email_id": "trip-1", "from": "Matt", "to": ["travel-planning@in.lebel.io"],
            "subject": "Fwd: Your stay at Park Hyatt Kyoto", "text": "Confirmation ABC123"}})
        assert r.status_code == 200

        # ...and one that fits no trip goes to Unsorted.
        monkeypatch.setattr(llm, "generate", lambda **kw: llm.LlmResult(text="", parsed=trips._Sorted(
            trip_id=0, items=[trips._Booking(kind="flight", title="AA100 JFK-LHR")])))
        client.post("/webhooks/resend", json={"type": "email.received", "data": {
            "email_id": "trip-2", "from": "Matt", "to": ["trips@in.lebel.io"], "subject": "Fwd: London flight",
            "text": "AA100"}})
        monkeypatch.undo()

    with session_scope() as s:
        trip = s.scalar(select(Trip).where(Trip.name == "Japan in March"))
        items = trips.items_for(s, trip)
        assert [b.title for b in items["bookings"]] == ["Park Hyatt Kyoto, 3 nights"]
        loose = trips.unsorted(s)
        assert [u.title for u in loose] == ["AA100 JFK-LHR"]
        loose_id, trip_id = loose[0].id, trip.id
        s.add(TripItem(trip_id=trip.id, kind="event", title="Kyoto Marathon expo", starts_at=trip.start_date,
                       source="research"))

    assert client.get("/trips").status_code == 200
    page = client.get(f"/trips/{trip_id}")
    assert page.status_code == 200 and "Park Hyatt Kyoto" in page.text and "Kyoto Marathon expo" in page.text
    client.post(f"/trip-items/{loose_id}/assign", data={"trip_id": trip_id}, follow_redirects=False)

    with session_scope() as s:
        assert s.get(TripItem, loose_id).trip_id == trip_id
        prompts = []
        real = llm.generate
        monkeypatch.setattr(llm, "generate", lambda **kw: prompts.append(kw["user"]) or real(**kw))
        nl = newsletter.build_daily(s, "am")
        assert nl is not None and "UPCOMING TRIPS" in prompts[0]
        assert "Kyoto Marathon expo" in prompts[0] and "Park Hyatt Kyoto" in prompts[0]  # bookings in the final week
        assert trips.for_newsletter(s)[1] == []  # each item goes out once
        s.delete(nl)
        _cleanup(s)
