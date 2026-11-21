from unittest import mock

from sqlalchemy import select

from app import settings_store, suggestions, triage
from app.db import init_db, session_scope, utcnow
from app.discussion import is_rich
from app.models import Agent, Item, Topic
from app.seed import _cast_of_four
from app.sources.base import RawItem, dedupe_key


def _topic(s, slug):
    """A topic for the test; the caller deletes it if it was created here (the smoke test seeds an empty DB)."""
    t = s.scalar(select(Topic).where(Topic.slug == slug))
    created = t is None
    if created:
        t = Topic(slug=slug, name=slug, description="test")
        s.add(t)
        s.flush()
    return t, created


def test_per_topic_cap_on_picks():
    init_db()
    with session_scope() as s:
        for it in s.scalars(select(Item).where(Item.status == "scored")):
            it.status = "skipped"
        t, created = _topic(s, "cap-test")
        for i in range(5):
            s.add(Item(topic_id=t.id, dedupe_key=dedupe_key(f"cap:{i}:{utcnow()}"), title=f"x{i}",
                       status="scored", score=9.5))
        s.flush()
        settings_store.set(s, "max_threads_per_topic", 2)
        picked = triage.pick_for_discussion(s, 5)
        assert sum(1 for i in picked if i.topic_id == t.id) == 2
        for it in s.scalars(select(Item).where(Item.topic_id == t.id)):
            s.delete(it)
        if created:
            s.delete(t)


def test_travel_threshold_default_and_focus():
    init_db()
    with session_scope() as s:
        travel, created = _topic(s, "travel")
        assert triage.threshold_for(s, travel) == 8
        assert "Boston (BOS)" in triage.travel_focus(s)
        if created:
            s.delete(travel)


def test_rich_material():
    assert is_rich(Item(body="x" * 2000, extra={}))
    assert is_rich(Item(body="", extra={"kind": "ebay_digest"}))
    assert not is_rich(Item(body="short", extra={}))


def test_suggested_sources_are_checked():
    init_db()
    with session_scope() as s:
        ok = {"kind": "rss", "name": "good", "config": {"url": "https://example.com/feed"}}
        empty = {"kind": "rss", "name": "empty", "config": {"url": "https://example.com/empty"}}
        search = {"kind": "websearch", "name": "search", "config": {"query": "x"}}

        def fake_rss(source, session):
            if "empty" in source.config["url"]:
                return []
            return [RawItem(key="k", title="t", url="https://example.com/a")]

        with mock.patch.dict("app.sources.FETCHERS", {"rss": fake_rss}):
            assert suggestions.checked_sources(s, [ok, empty, search]) == [ok, search]
            assert suggestions.checked_sources(s, [empty]) == []


def test_cast_of_four():
    init_db()
    with session_scope() as s:
        before = list(s.scalars(select(Agent).where(Agent.active)))
        added = [Agent(handle=f"cast{i}{utcnow():%f}", display_name=f"Cast {i}", interests=[])
                 for i in range(6 - len(before))]
        s.add_all(added)
        s.flush()
        note = _cast_of_four(s)
        active = list(s.scalars(select(Agent).where(Agent.active)))
        assert len(active) == 4, note
        slugs = {t.slug for t in s.scalars(select(Topic).where(Topic.active))}
        covered = {slug for a in active for slug in (a.interests or [])}
        assert slugs <= covered
        for a in before:
            a.active = True  # leave the shared DB as other tests expect
        for a in added:
            s.delete(a)


def test_no_trip_suggestions_migration():
    from app.seed import _no_trip_suggestions

    init_db()
    with session_scope() as s:
        before = settings_store.get(s, "profile")
        settings_store.set(s, "profile", "I collect prints.\r\nthe moment they're announced. I'll travel the same week if "
                                         "it's within a 2-3 hour flight or a\r\n4-5 hour train ride or drive from Boston.\r\nI like travel.")
        t, created = _topic(s, "inciardi")
        old_criteria = t.urgent_criteria
        t.urgent_criteria = ("A NEW machine that just went live. Alert especially when it's reachable from Boston within a "
                             "2-3 hour flight (roughly: all of New England, the Southeast coast). A machine closing soon.")
        note = _no_trip_suggestions(s)
        assert "profile" in note and "Inciardi" in note
        assert "Don't suggest trips" in settings_store.get(s, "profile")
        assert "reachable" not in t.urgent_criteria and "never suggest" in t.urgent_criteria
        settings_store.set(s, "profile", before)
        t.urgent_criteria = old_criteria
        if created:
            s.delete(t)


def test_drop_listings_and_miniprint():
    from app.models import Source
    from app.seed import _drop_listings_and_miniprint

    init_db()
    with session_scope() as s:
        t, created = _topic(s, "inciardi")
        old = (t.description, t.urgent_criteria)
        t.description = "Care about rare prints, and resale prices on eBay and Depop."
        t.urgent_criteria = "A new machine. An eBay 'first sighting' of a design counts if new (not just an old print we hadn't catalogued)."
        keep = Source(topic_id=t.id, kind="page_watch", name="shop", config={"url": "https://inciardiprints.com/"})
        gone = [Source(topic_id=t.id, kind="ebay", name="eBay", config={"query": "x"}),
                Source(topic_id=t.id, kind="sitemap", name="mp", config={"url": "https://miniprint.io/sitemap.xml"}),
                Source(topic_id=t.id, kind="websearch", name="resale", config={"query": "q", "domains": "depop.com, etsy.com"})]
        s.add_all([keep, *gone])
        s.flush()
        item = Item(topic_id=t.id, source_id=gone[0].id, dedupe_key=dedupe_key(f"eb:{utcnow()}"), title="x",
                    status="scored", extra={"source_kind": "ebay"})
        s.add(item)
        s.flush()
        note = _drop_listings_and_miniprint(s)
        s.flush()
        assert "removed 3" in note and item.status == "skipped"
        assert s.get(Source, keep.id) is not None
        assert "eBay" not in t.description and "first sighting" not in t.urgent_criteria
        s.delete(item)
        s.delete(keep)
        t.description, t.urgent_criteria = old
        if created:
            s.delete(t)
