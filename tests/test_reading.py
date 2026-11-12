"""Reading page: save links, summarize, optional discussion, newsletter section."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import llm, newsletter, reading
from app.db import init_db, session_scope
from app.main import app
from app.models import Agent, Item, Newsletter, ReadingItem, Thread, Topic
from app.sources.base import SourceError


def test_parse_links_keeps_public_urls_only():
    text = "read https://example.com/a and http://localhost:8000/x, http://10.0.0.5/y https://example.com/a"
    assert reading.parse_links(text) == ["https://example.com/a"]


def test_save_summarize_discuss_and_newsletter(monkeypatch):
    init_db()
    article = "The 2027 NYC Marathon lottery opens in January. " * 40
    monkeypatch.setattr(reading, "_fetch", lambda url: ("Lottery dates announced", article))

    with session_scope() as s:
        s.add(Agent(handle="reader_bot", display_name="Reader", interests=[reading.TOPIC_SLUG]))

    client = TestClient(app)
    r = client.post("/reading", data={"links": "https://example.com/nyc-lottery", "discuss": "true",
                                      "instructions": "Should I enter?"}, follow_redirects=False)
    assert r.status_code == 303
    # A blocked site falls back to Claude's web fetch.
    def blocked(url):
        raise SourceError("HTTP 403")
    monkeypatch.setattr(reading, "_fetch", blocked)
    client.post("/reading", data={"links": "https://example.com/paywalled"}, follow_redirects=False)

    with session_scope() as s:
        rows = list(s.scalars(select(ReadingItem).order_by(ReadingItem.id)))
        assert [x.status for x in rows] == ["ready", "ready"], [x.error for x in rows]
        discussed, plain = rows
        assert discussed.title == "Lottery dates announced" and discussed.thread_id
        thread = s.get(Thread, discussed.thread_id)
        assert thread.posts[0].agent_id is None and "Should I enter?" in thread.posts[0].body
        assert any(p.agent_id for p in thread.posts)  # an agent answered
        assert plain.thread_id is None
        assert client.get("/reading").status_code == 200

        prompts = []
        real = llm.generate
        monkeypatch.setattr(llm, "generate", lambda **kw: prompts.append(kw["user"]) or real(**kw))
        nl = newsletter.build_daily(s, "pm")
        assert nl is not None
        assert "READING LIST" in prompts[0] and "Should I enter?" in prompts[0]
        assert {x.newsletter_id for x in rows} == {nl.id}
        assert reading.for_newsletter(s) == []

        # Leave the shared test database as we found it.
        for t in s.scalars(select(Thread)):
            s.delete(t)
        for model in (Item, ReadingItem, Newsletter, Agent):
            for obj in s.scalars(select(model)):
                s.delete(obj)
        s.delete(s.scalar(select(Topic).where(Topic.slug == reading.TOPIC_SLUG)))
