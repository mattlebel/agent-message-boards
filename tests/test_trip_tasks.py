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


def test_board_knowledge_and_plan(monkeypatch):
    from types import SimpleNamespace

    from app import llm
    from app.sources.base import dedupe_key

    init_db()
    calls = []

    def fake_generate(**kw):
        calls.append(kw)
        return SimpleNamespace(parsed=trips._TaskResult(
            summary="Two days, six stops.", plan="Sun 10:00 Guggenheim\nSun 12:00 30 Rock",
            picks=[trips._Pick(name="The Shop at NBC Studios", url="https://example.com/nbc")]))

    monkeypatch.setattr(llm, "generate", fake_generate)
    monkeypatch.setattr(discussion, "respond_to_owner", lambda s, tid: None)
    with session_scope() as s:
        topic = s.scalar(select(Thread.topic_id).limit(1))  # any topic id works for the item
        from app.models import Item, Topic

        t = s.scalar(select(Topic).limit(1))
        if t is None:
            t = Topic(slug="kn-test", name="kn", description="")
            s.add(t)
            s.flush()
        item = Item(topic_id=t.id, dedupe_key=dedupe_key("kn:nbc"), title="New machine at The Shop at NBC Studios",
                    body="miniprint.io lists a machine at 30 Rock, New York. Stocked now.", url="https://miniprint.io/x")
        s.add(item)
        trip = Trip(name="NYC", destination="New York City", start_date=trips.today() + timedelta(days=3))
        s.add(trip)
        s.flush()
        task = TripTask(trip_id=trip.id, text="Map a route to these:\n- Guggenheim museum\n- New Shop at 30 Rock prints")
        s.add(task)
        s.flush()
        assert "NBC Studios" in trips.board_knowledge(s, trip, task.text)
        trips.run_task(s, task.id)
        kw = calls[0]
        assert kw["role"] == "tasks" and kw["max_searches"] >= 20 and kw["max_fetches"] >= 10
        assert "WHAT THE BOARD ALREADY KNOWS" in kw["user"] and "NBC Studios" in kw["user"]
        assert trips.task_plan(task).startswith("Sun 10:00") and trips.task_picks(task)[0]["name"].startswith("The Shop")
        trips.run_task(s, task.id)  # redo replaces the earlier picks instead of piling up
        assert len(list(s.scalars(select(TripItem).where(TripItem.trip_id == trip.id, TripItem.source == "task")))) == 1
        assert len(list(s.scalars(select(Thread).where(Thread.topic.has(slug=trips.TASK_SLUG))))) == 1
        for th in s.scalars(select(Thread).where(Thread.topic.has(slug=trips.TASK_SLUG))):
            s.delete(th)
        s.flush()
        for row in s.scalars(select(Item).where(Item.topic.has(slug=trips.TASK_SLUG))):
            s.delete(row)
        tt = s.scalar(select(Topic).where(Topic.slug == trips.TASK_SLUG))
        if tt:
            s.delete(tt)
        for ti in s.scalars(select(TripItem).where(TripItem.trip_id == trip.id)):
            s.delete(ti)
        s.delete(task)
        s.delete(trip)
        s.delete(item)
        if t.slug == "kn-test":
            s.delete(t)
        del topic
