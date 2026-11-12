"""eBay: tracker records sales as listings end; the cycle posts two digests."""

from datetime import timedelta

from sqlalchemy import select

from app.db import init_db, session_scope, utcnow
from app.models import Item, Source, Topic
from app.sources import ebay


def _listing(item_id, title, price, auction=False, bids=0, end=None, created=None):
    it = {"itemId": item_id, "title": title, "price": {"value": str(price)},
          "itemWebUrl": f"https://www.ebay.com/itm/{item_id}?hash=x",
          "buyingOptions": ["AUCTION"] if auction else ["FIXED_PRICE"], "bidCount": bids,
          "itemCreationDate": (created or utcnow()).isoformat() + "Z"}
    if end:
        it["itemEndDate"] = end.isoformat() + "Z"
    return it


def test_tracker_and_digests(monkeypatch):
    init_db()
    with session_scope() as s:
        topic = Topic(slug="ebay-test", name="t")
        s.add(topic)
        s.flush()
        src = Source(topic_id=topic.id, kind="ebay", config={"query": "inciardi", "min_sale_price": 10})
        s.add(src)
        s.flush()

        now = utcnow()
        listings = [
            _listing("1", "Inciardi mini print Sardines", 8, created=now - timedelta(hours=3)),
            _listing("2", "Inciardi Disco Rat Harry Styles pop-up print", 36.95, created=now - timedelta(hours=2)),
            _listing("3", "Inciardi Tomato print auction", 15, auction=True, bids=4, end=now + timedelta(minutes=20),
                     created=now - timedelta(hours=1)),
            _listing("4", "Unrelated vintage print", 99),  # not Inciardi: ignored
        ]
        total = {"n": 4}
        monkeypatch.setattr(ebay, "search", lambda q, limit=200: (listings, total["n"]))

        assert ebay.fetch_ebay(src, s) == []  # the tracker never posts single listings
        assert set(src.state["live"]) == {"1", "2", "3"}

        # The auction closes with bids, and the $36.95 buy-it-now disappears (all results visible).
        src.state = {**src.state, "live": {**src.state["live"],
                     "3": {**src.state["live"]["3"], "end": (now - timedelta(minutes=1)).isoformat()}}}
        listings[:] = [listings[0]]
        total["n"] = 1
        ebay.fetch_ebay(src, s)
        sales = {x["id"]: x for x in src.state["sales"]}
        assert sales["3"]["kind"] == "auction" and sales["3"]["price"] == 15.0
        assert sales["2"]["kind"] == "fixed"

        digests = ebay.build_digests(src)
        titles = [d.title for d in digests]
        assert titles[0].startswith("Inciardi on eBay: last 2 sales over $10")
        assert digests[0].body.index("36.95") < digests[0].body.index("15.00")  # highest first
        assert "1 newest listings" in titles[1] and "Sardines" in digests[1].body
        assert all(d.extra["kind"] == "ebay_digest" for d in digests)
        # Nothing changed: no repeat posts.
        assert ebay.build_digests(src) == []
        s.delete(topic)


def test_cycle_posts_digests_and_always_discusses_them(monkeypatch):
    from app import jobs, triage

    init_db()
    with session_scope() as s:
        topic = Topic(slug="ebay-cycle", name="Inciardi-ish", urgent=True, priority=5)
        s.add(topic)
        s.flush()
        src = Source(topic_id=topic.id, kind="ebay", config={"query": "inciardi"}, last_ok_at=utcnow(),
                     state={"live": {"9": {"title": "Inciardi print", "price": 12, "created": utcnow().isoformat()}},
                            "sales": []})
        s.add(src)
        s.flush()
        made = jobs.ebay_digests(s)
        assert len(made) == 1 and made[0].title.endswith("newest listings")
        assert triage.pending_urgent(s) == [] or all(i.topic_id != topic.id for i in triage.pending_urgent(s))
        triage.triage(s)
        picked = triage.pick_for_discussion(s, 0)
        assert made[0] in picked
        for it in s.scalars(select(Item).where(Item.topic_id == topic.id)):
            s.delete(it)
        s.delete(topic)
