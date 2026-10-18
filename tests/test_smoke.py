"""End-to-end smoke test with fake model output and no network."""

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import config, discussion, jobs, newsletter, personas, suggestions, triage
from app.db import init_db, session_scope
from app.main import app
from app.models import Agent, Feedback, InboundEmail, Item, JobRun, Newsletter, Post, Thread, Topic, TopicSuggestion
from app.seed import seed
from app.sources.base import dedupe_key


def _items(s, n_per_topic=3):
    for t in s.scalars(select(Topic)):
        for i in range(n_per_topic):
            s.add(Item(topic_id=t.id, dedupe_key=dedupe_key(f"t:{t.id}:{i}"), url=f"https://example.com/{t.slug}/{i}",
                       title=f"{t.name} news {i}", body="Something happened.", extra={"source_name": "test"}))
    s.commit()


def test_full_flow():
    init_db()
    with session_scope() as s:
        assert seed(s)
        assert s.scalar(select(func.count(Topic.id))) == 8
        personas.bootstrap(s, count=8)
        agents = list(s.scalars(select(Agent)))
        assert len(agents) >= 8
        assert not personas.coverage_gaps(s) or len(agents) == 11
        _items(s)
        assert triage.triage(s) > 0
        picked = triage.pick_for_discussion(s, 5)
        assert len(picked) == 5
        assert len({i.topic_id for i in picked}) >= 3  # spread across topics
        threads = discussion.discuss(s, picked)
        assert len(threads) == 5
        assert all(len(t.posts) >= 3 for t in threads)
        assert discussion.revisit(s) > 0
        nl = newsletter.build_daily(s, "am")
        assert nl is not None and "Things happened" in nl.markdown and "<html>" in nl.html
        newsletter.send_newsletter(s, nl)  # no API key: no-op but marks sent
        rows = suggestions.generate(s)
        assert rows
        topic = suggestions.add_from_suggestion(s, rows[0])
        assert topic.sources

    client = TestClient(app)
    for path in ["/", "/topics", "/agents", "/newsletters", "/suggestions", "/inbox", "/youtube", "/settings",
                 "/login", "/healthz"]:
        r = client.get(path)
        assert r.status_code == 200, path

    with session_scope() as s:
        thread = s.scalar(select(Thread).limit(1))
        before = len(thread.posts)
        topic_page = s.scalar(select(Topic).limit(1))
        agent = s.scalar(select(Agent).limit(1))
        agent_post = s.scalar(select(Post).where(Post.agent_id.is_not(None)).limit(1))
        nl_id = s.scalar(select(Newsletter.id).limit(1))
    assert client.get(f"/t/{thread.id}").status_code == 200
    assert client.get(f"/topics/{topic_page.id}").status_code == 200
    assert client.get(f"/agents/{agent.handle}").status_code == 200
    assert client.get(f"/newsletters/{nl_id}").status_code == 200

    # Thumbs: recorded immediately, then an optional comment.
    r = client.post("/api/feedback", json={"post_id": agent_post.id, "value": 1})
    assert r.status_code == 200
    fid = r.json()["id"]
    assert client.post(f"/api/feedback/{fid}/comment", json={"comment": "more race tips"}).status_code == 200
    with session_scope() as s:
        fb = s.get(Feedback, fid)
        assert fb.value == 1 and fb.comment == "more race tips"
        a = s.get(Agent, agent_post.agent_id)
        assert "more race tips" in discussion.steering_for(s, a)

    # Owner replies; agents answer in the background task.
    r = client.post(f"/t/{thread.id}/reply", data={"body": "What do you all think?"}, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        assert len(s.get(Thread, thread.id).posts) >= before + 2

    # New topic via the form.
    r = client.post("/topics", data={"name": "Track miles", "description": "Mile races", "priority": 4},
                    follow_redirects=False)
    assert r.status_code == 303

    # Add a source via the form.
    r = client.post(f"/topics/{topic_page.id}/sources",
                    data={"kind": "rss", "name": "Test feed", "cfg_url": "https://example.com/feed"},
                    follow_redirects=False)
    assert r.status_code == 303

    # Inbound email webhook (dev mode: unsigned).
    r = client.post("/webhooks/resend", json={
        "type": "email.received",
        "data": {"email_id": "em_1", "from": "Wirecutter <deals@nytimes.com>", "to": ["wirecutter@in.lebel.io"],
                 "subject": "Today's best deals", "text": "AirPods are $99 https://example.com/airpods"},
    })
    assert r.status_code == 200
    with session_scope() as s:
        em = s.scalar(select(InboundEmail).where(InboundEmail.resend_id == "em_1"))
        assert em.status == "processed", em.error
        assert s.get(Topic, em.topic_id).slug == "wirecutter"
        assert em.items_created == 1

    # One-click suggestion add.
    with session_scope() as s:
        assert suggestions.generate(s) == []  # already-added topics aren't suggested again
        s.add(TopicSuggestion(batch_key="x", name="Cycling", slug="cycling", token="tok123",
                              sources=[{"kind": "websearch", "name": "x", "config": {"query": "x"}}]))
        token = "tok123"
    r = client.get(f"/s/{token}/add", follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        assert s.scalar(select(Topic).where(Topic.slug == "cycling")) is not None


def test_tick_schedule(monkeypatch):
    init_db()
    calls = []
    monkeypatch.setattr(jobs, "cycle", lambda s: calls.append("cycle") or "ok")
    monkeypatch.setattr(jobs, "send_daily", lambda s, slot, at=None: calls.append(f"nl:{slot}") or "ok")
    monkeypatch.setattr(jobs, "weekly", lambda s: calls.append("weekly") or "ok")
    monkeypatch.setattr(jobs, "run_hot_quietly", lambda: calls.append("hot"))
    with session_scope() as s:
        for r in s.scalars(select(JobRun)):
            s.delete(r)

    def at(y, mo, d, h, mi):
        return datetime(y, mo, d, h, mi, tzinfo=config.TZ)

    jobs.tick(at(2026, 10, 18, 5, 0))  # Sunday 5:00 ET
    assert calls == ["cycle", "hot"]
    calls.clear()
    jobs.tick(at(2026, 10, 18, 5, 45))  # builds the 6am newsletter early
    assert calls == ["nl:am", "hot"]
    calls.clear()
    jobs.tick(at(2026, 10, 18, 6, 0))  # already done
    assert calls == ["hot"]
    calls.clear()
    jobs.tick(at(2026, 10, 18, 15, 0))  # Sunday afternoon ideas
    assert calls == ["weekly", "hot"]
    calls.clear()
    jobs.tick(at(2026, 10, 18, 18, 0))
    jobs.tick(at(2026, 10, 18, 18, 45))
    assert calls == ["cycle", "hot", "nl:pm", "hot"]
