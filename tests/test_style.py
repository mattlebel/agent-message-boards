from app import style

FLAGGED = [
    "Great point, @rosa_runs! This pop-up is a game-changer.",
    "It's not about the price, it's about the scarcity.",
    "Let's dive in. Three things stand out here.",
    "The machine sold out in an hour, highlighting the demand for $1 art.",
    "It's worth noting that the 5th Ave Mile opens Monday.",
    "In short, go early. Thoughts?",
    "Grand Central — again — is the place — obviously.",
    "Studies show runners love Tracksmith.",
]

CLEAN = [
    "Went to the Grand Central machine at 7am, line was already 40 deep. Got a sardine. Again.",
    "NYRR's 9+1 volunteer credit posts about two weeks after the event, so don't panic yet.",
    "The Amex offer is $50 back on $250 at Tracksmith, ends Nov 30. Stacks with their sale if you time it.",
    "Not sure the eBay asks mean much; one seller listing ten 'rare' pigs at $70 isn't a market.",
]


def test_flags_common_tropes():
    for text in FLAGGED:
        assert style.find_tropes(text), text


def test_leaves_normal_posts_alone():
    for text in CLEAN:
        assert style.find_tropes(text) == [], (text, style.find_tropes(text))


def test_long_form_allows_headings():
    md = "## Things happened\n\n- A print sold for $70.[^1]\n\n[^1]: Allegedly."
    assert style.find_tropes(md, long_form=True) == []
    assert style.find_tropes(md) != []


def test_write_revises_once_when_tropes_found(monkeypatch):
    from app import discussion, llm
    from app.db import init_db, session_scope
    from app.models import Agent

    init_db()
    calls = []

    def fake_generate(**kw):
        calls.append(kw["purpose"])
        body = "Great point! Let's dive in." if len(calls) == 1 else "Line was 40 deep at 7am."
        return llm.LlmResult(text=body, parsed=discussion._Reply(body=body))

    monkeypatch.setattr(llm, "generate", fake_generate)
    with session_scope() as s:
        agent = Agent(handle="tester_style", display_name="T")
        s.add(agent)
        s.flush()
        out = discussion._write(s, agent, purpose="reply", user="u", schema=discussion._Reply,
                                critical=False, fake=None, max_tokens=100)
        s.delete(agent)
    assert calls == ["reply", "reply_revise"]
    assert out.body == "Line was 40 deep at 7am."


def test_websearch_drops_blocked_domains(monkeypatch):
    import pytest

    from app import llm
    from app.models import Source
    from app.sources import web
    from app.sources.base import SourceError

    calls = []

    def fake_generate(**kw):
        calls.append(kw.get("allowed_domains"))
        if any(d.split("/")[0] in ("reddit.com", "threads.net") for d in kw.get("allowed_domains") or []):
            raise Exception("Error code: 400 - The following domains are not accessible to our user agent: "
                            "['reddit.com', 'threads.net']. Read more: https://support.anthropic.com/x")
        return llm.LlmResult(text="{}", parsed=web._FoundList(items=[]))

    monkeypatch.setattr(llm, "generate", fake_generate)
    src = Source(kind="websearch", name="x", config={"query": "q", "domains": "reddit.com, instagram.com, threads.net"})
    assert web.fetch_websearch(src, None) == []
    assert calls == [["reddit.com", "instagram.com", "threads.net"], ["instagram.com"]]
    assert src.config["domains"] == "instagram.com"

    only_reddit = Source(kind="websearch", name="y", config={"query": "q", "domains": "reddit.com/r/runnyc"})
    with pytest.raises(SourceError, match="can't read reddit.com"):
        web.fetch_websearch(only_reddit, None)


def test_suggestions_accept_null_fields():
    from app.suggestions import _source_config, _Suggestions

    raw = {"suggestions": [{
        "name": "Track miles", "description": "d", "why": "w", "urgent": None,
        "sources": [{"kind": "rss", "name": "Feed", "url": "https://x.example/feed", "subreddit": None, "query": None},
                    {"kind": "websearch", "name": "Search", "url": None, "query": "road mile races"}],
    }]}
    parsed = _Suggestions.model_validate(raw)
    s = parsed.suggestions[0]
    assert s.urgent is False
    assert _source_config(s.sources[0]) == {"url": "https://x.example/feed"}
    assert _source_config(s.sources[1]) == {"query": "road mile races"}


def test_bootstrap_skips_a_refused_persona(monkeypatch):
    from app import llm, personas
    from app.db import init_db, session_scope
    from app.models import Agent

    init_db()
    real = personas.create_persona
    calls = {"n": 0}

    def flaky(session, needs_voices_for=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise llm.LlmError("model declined (persona)")
        return real(session, needs_voices_for)

    monkeypatch.setattr(personas, "create_persona", flaky)
    with session_scope() as s:
        for a in s.scalars(personas.select(Agent)):
            a.active = False
        s.flush()
        personas.bootstrap(s, count=3, max_total=3)
        assert len(list(s.scalars(personas.select(Agent).where(Agent.active)))) == 3
        for a in s.scalars(personas.select(Agent)):
            s.delete(a)


def test_reply_can_pass(monkeypatch):
    from app import discussion, llm
    from app.db import init_db, session_scope
    from app.models import Agent, Thread

    init_db()
    monkeypatch.setattr(llm, "generate",
                        lambda **kw: llm.LlmResult(text="", parsed=discussion._Reply(skip=True)))
    with session_scope() as s:
        agent = Agent(handle="passer", display_name="P")
        thread = Thread(title="t")
        s.add_all([agent, thread])
        s.flush()
        assert discussion.add_reply(s, thread, agent=agent) is None
        assert thread.posts == []
        s.delete(thread)
        s.delete(agent)


def test_manually_fetched_urgent_items_get_alert_checked():
    from sqlalchemy import select

    from app import jobs, triage
    from app.db import init_db, session_scope
    from app.models import Item, Topic
    from app.sources.base import dedupe_key

    init_db()
    with session_scope() as s:
        topic = Topic(slug="urgent-test", name="Urgent", urgent=True)
        s.add(topic)
        s.flush()
        s.add(Item(topic_id=topic.id, dedupe_key=dedupe_key("urgent-test-1"), title="New machine at the Met",
                   status="discussed", extra={}))
        s.flush()
        mine = lambda: [i for i in triage.pending_urgent(s) if i.topic_id == topic.id]  # noqa: E731
        assert len(mine()) == 1
        jobs.alerts_for_pending(s)  # fake model says "not an alert"
        assert mine() == []
        item = s.scalar(select(Item).where(Item.topic_id == topic.id))
        assert item.extra.get("urgent_checked") is True
        s.delete(topic)


def test_headline_counted_lists():
    assert style.find_tropes("Fifteen prints, five per pack, two packs, zero prices", headline=True)
    assert style.find_tropes("Holiday Drop: mystery packs Friday at noon", headline=True) == []
    # Fact-heavy body text is fine; the counted-list check only applies to headlines.
    assert style.find_tropes("Saturday Nov 14, 8am, $25 entry, 2 miles from the start.") == []
    md = "## Three races, two deadlines, one bib\n\nBody."
    assert any("counted-list" in t for t in style.find_tropes(md, long_form=True))


def test_polish_rewrites_a_newsletter_subject(monkeypatch):
    from app import llm
    from app.newsletter import _Edition

    draft = _Edition(subject="Fifteen prints, five per pack, two packs, zero prices", preheader="p",
                     markdown="## Mystery packs\n\nThey go on sale Friday at noon.")
    calls = []

    def fake_generate(**kw):
        calls.append(kw["purpose"])
        return llm.LlmResult(text="", parsed=draft.model_copy(update={"subject": "The mystery pack math"}))

    monkeypatch.setattr(llm, "generate", fake_generate)
    out = style.polish(draft, purpose="newsletter", role="newsletter", system="s", user="u", long_form=True)
    assert calls == ["newsletter_revise"] and out.subject == "The mystery pack math"

    calls.clear()
    clean = draft.model_copy(update={"subject": "The mystery pack math"})
    assert style.polish(clean, purpose="newsletter", role="newsletter", system="s", user="u",
                        long_form=True) is clean
    assert calls == []


def test_google_news_cleanup_runs_once():
    from sqlalchemy import select

    from app import settings_store
    from app.db import init_db, session_scope
    from app.models import Item, Source, Topic
    from app.seed import run_migrations
    from app.sources.base import dedupe_key

    init_db()
    with session_scope() as s:
        settings_store.set(s, "applied_migrations", [])
        topic = Topic(slug="gn-test", name="GN")
        s.add(topic)
        s.flush()
        gn = Source(topic_id=topic.id, kind="rss", name="Google News: x",
                    config={"url": "https://news.google.com/rss/search?q=x"})
        keep = Source(topic_id=topic.id, kind="rss", name="Blog", config={"url": "https://blog.example/feed"})
        s.add_all([gn, keep])
        s.flush()
        s.add(Item(topic_id=topic.id, source_id=gn.id, dedupe_key=dedupe_key("gn-item"), title="kept"))
        s.commit()
        assert any("drop-google-news" in d for d in run_migrations(s))
        names = [x.name for x in s.scalars(select(Source).where(Source.topic_id == topic.id))]
        assert names == ["Blog"]
        assert s.scalar(select(Item).where(Item.title == "kept")) is not None
        assert run_migrations(s) == []  # already applied
        s.delete(topic)


def test_failing_sources_footer_and_haiku_override(monkeypatch):
    from app import llm
    from app.db import init_db, session_scope
    from app.models import Source, Topic
    from app.newsletter import failing_sources_note
    from app.sources import web

    init_db()
    with session_scope() as s:
        topic = Topic(slug="footer-test", name="Footer")
        s.add(topic)
        s.flush()
        s.add(Source(topic_id=topic.id, kind="page_watch", name="Wirecutter deals page",
                     last_error="SourceError: HTTP 403 from https://www.nytimes.com/wirecutter/deals/"))
        s.flush()
        note = failing_sources_note(s)
        assert "Wirecutter deals page (HTTP 403 from" in note and "SourceError" not in note
        s.delete(topic)

    seen = {}

    def fake_generate(**kw):
        seen["model"] = kw.get("model")
        return llm.LlmResult(text="{}", parsed=web._FoundList(items=[]))

    monkeypatch.setattr(llm, "generate", fake_generate)
    src = Source(kind="websearch", name="x", config={"query": "q", "model": "claude-haiku-5-5"})
    web.fetch_websearch(src, None)
    assert seen["model"] == "claude-haiku-5-5"
