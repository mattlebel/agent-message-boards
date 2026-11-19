from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import discussion, trips
from app.db import init_db, session_scope
from app.main import app
from app.models import Thread, Trip, TripItem, TripTask


def test_trip_task_flow(monkeypatch):
    init_db()
    answered = []
    monkeypatch.setattr(discussion, "respond_to_owner", lambda s, tid: answered.append(tid))
    with session_scope() as s:
        trip = Trip(name="Chicago weekend", destination="Chicago", start_date=trips.today() + timedelta(days=10),
                    end_date=trips.today() + timedelta(days=12))
        s.add(trip)
        s.flush()
        s.add(TripItem(trip_id=trip.id, kind="hotel", title="The Langham Chicago", source="email"))
        trip_id = trip.id

    client = TestClient(app)
    # TestClient runs background tasks before returning, so the (fake) research is done here.
    r = client.post(f"/trips/{trip_id}/task", data={"text": "Best dinner Sunday near the hotel for two"})
    assert r.status_code == 200
    with session_scope() as s:
        task = s.scalar(select(TripTask).where(TripTask.trip_id == trip_id))
        assert task.status == "done" and task.picks and task.thread_id == answered[0]
        thread = s.get(Thread, task.thread_id)
        assert thread.topic.slug == trips.TASK_SLUG and thread.posts[0].body.startswith("Best dinner")
        assert "The Langham Chicago" in thread.item.body and "Fake Bistro" in thread.item.body
        ideas = list(s.scalars(select(TripItem).where(TripItem.trip_id == trip_id, TripItem.source == "task")))
        assert ideas and ideas[0].newsletter_id == 0
        assert ideas[0] not in trips.for_newsletter(s)[1]
    page = client.get(f"/trips/{trip_id}")
    assert "Fake Bistro" in page.text and "What the board says" in page.text

    client.post(f"/trips/{trip_id}/delete")
    with session_scope() as s:  # the smoke test expects to seed an empty DB
        assert not s.scalar(select(TripTask.id).where(TripTask.trip_id == trip_id))
        thread = s.get(Thread, answered[0])
        topic, item = thread.topic, thread.item
        s.delete(thread)
        s.flush()
        s.delete(item)
        s.delete(topic)
