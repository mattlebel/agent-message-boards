from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import llm, travel, wallet
from app.db import init_db, session_scope, utcnow
from app.main import app
from app.models import Item, Topic, WalletCard
from app.sources.base import dedupe_key


def _clear(s):
    for c in s.scalars(select(WalletCard)):
        s.delete(c)
    s.flush()


def test_524_and_hard_rules():
    init_db()
    with session_scope() as s:
        _clear(s)
        now = utcnow()
        s.add_all([
            WalletCard(name="Chase Sapphire Preferred", issuer="Chase", opened=now - timedelta(days=100)),
            WalletCard(name="Amex Gold", issuer="American Express", opened=now - timedelta(days=200)),
            WalletCard(name="Ink Business Cash", issuer="Chase", opened=now - timedelta(days=50), business=True,
                       counts_524=False),
            WalletCard(name="Citi Premier", issuer="Citi", opened=now - timedelta(days=900)),  # too old
        ])
        s.flush()
        n, counting, _ = wallet.count_524(s)
        assert n == 2 and {c.name for c in counting} == {"Chase Sapphire Preferred", "Amex Gold"}

        C = wallet._CardCheck
        assert wallet._rule_override(s, C(id=1, is_card_offer=True, business=True, card="Amex Business Gold")) == "business card offer"
        assert "Sapphire" in wallet._rule_override(s, C(id=1, is_card_offer=True, business=False, card="Chase Sapphire Reserve", issuer="Chase"))
        assert "already have" in wallet._rule_override(s, C(id=1, is_card_offer=True, business=False, card="American Express Gold Card"))
        assert wallet._rule_override(s, C(id=1, is_card_offer=True, business=False, card="Chase Freedom Unlimited", issuer="Chase")) == ""
        for d in (300, 400, 500):
            s.add(WalletCard(name=f"Card {d}", issuer="Other", opened=now - timedelta(days=d)))
        s.flush()
        assert "5/24" in wallet._rule_override(s, C(id=1, is_card_offer=True, business=False, card="Chase Freedom Unlimited", issuer="Chase"))
        assert "Cards in my wallet" in wallet.profile_text(s)
        _clear(s)


def test_offer_check_filters_items(monkeypatch):
    init_db()
    with session_scope() as s:
        _clear(s)
        topic = Topic(slug="cards-t", name="Cards")
        s.add(topic)
        s.flush()
        biz = Item(topic_id=topic.id, dedupe_key=dedupe_key("c1"), title="Amex Business Gold: 200K points bonus")
        ok = Item(topic_id=topic.id, dedupe_key=dedupe_key("c2"), title="Chase Freedom Unlimited bonus is back")
        s.add_all([biz, ok])
        s.flush()

        def fake(**kw):
            return llm.LlmResult(text="", parsed=wallet._CardChecks(items=[
                wallet._CardCheck(id=biz.id, is_card_offer=True, business=True, card="Amex Business Gold"),
                wallet._CardCheck(id=ok.id, is_card_offer=True, business=False, card="Chase Freedom Unlimited",
                                  issuer="Chase", eligible=True),
            ]))

        monkeypatch.setattr(llm, "generate", fake)
        assert wallet.check_offers(s) == 1
        assert biz.status == "skipped" and biz.triage_note == "Filtered: business card offer"
        assert ok.status == "new" and ok.extra["card_checked"]
        assert wallet.check_offers(s) == 0  # checked once
        s.delete(topic)


def test_travel_matching_and_pages(monkeypatch):
    init_db()
    client = TestClient(app)
    monkeypatch.setattr(travel, "backfill", lambda session, region, limit=30: 0)
    r = client.post("/travel", data={"regions": "Japan\nSoutheast Asia, London"}, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        assert travel.regions(s) == ["Japan", "Southeast Asia", "London"]
        topic = Topic(slug="travel-t", name="Travel")
        s.add(topic)
        s.flush()
        hotel = Item(topic_id=topic.id, dedupe_key=dedupe_key("t1"), url="https://onemileatatime.com/reviews/x",
                     title="Review: Aman Tokyo", extra={"review": {"type": "hotel", "places": ["Tokyo", "Japan", "East Asia"],
                                                                   "subject": "Aman Tokyo"}})
        news = Item(topic_id=topic.id, dedupe_key=dedupe_key("t2"), url="https://onemileatatime.com/news/y",
                    title="United changes", extra={"review": {"type": "none", "places": ["Japan"]}})
        uk = Item(topic_id=topic.id, dedupe_key=dedupe_key("t3"), url="https://onemileatatime.com/reviews/z",
                  title="Review: BA lounge", extra={"review": {"type": "lounge", "places": ["Ukraine"]}})
        s.add_all([hotel, news, uk])
        s.flush()
        assert travel.matched_regions(hotel, travel.regions(s)) == ["Japan"]
        assert travel.matched_regions(news, travel.regions(s)) == []  # not a review
        assert travel.matched_regions(uk, ["UK"]) == []  # whole words only
        data = travel.page_data(s)
        assert data["Japan"]["hotel"] == [hotel]
        assert hotel in travel.for_newsletter(s)
        s.delete(topic)
    page = client.get("/travel")
    assert page.status_code == 200 and "Southeast Asia" in page.text
    assert client.get("/wallet").status_code == 200
    r = client.post("/wallet", data={"name": "Chase Sapphire Reserve", "issuer": "Chase", "opened": "2026-03"},
                    follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        card = s.scalar(select(WalletCard).where(WalletCard.name == "Chase Sapphire Reserve"))
        assert card.opened.month == 3 and card.counts_524
        _clear(s)
        from app import settings_store
        settings_store.set(s, "travel_regions", [])
        settings_store.set(s, "travel_backfilled", [])
