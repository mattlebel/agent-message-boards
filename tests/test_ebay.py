"""eBay tracker: catalog, first sightings after warm-up, auction results, weekly snapshot."""

from datetime import timedelta

from app.db import init_db, session_scope, utcnow
from app.models import Source, Topic
from app.sources import ebay


def _listing(item_id, title, price, auction=False, bids=0, end=None):
    it = {"itemId": item_id, "title": title, "price": {"value": str(price)}, "itemWebUrl": f"https://ebay.com/itm/{item_id}",
          "buyingOptions": ["AUCTION"] if auction else ["FIXED_PRICE"], "bidCount": bids}
    if end:
        it["itemEndDate"] = end.isoformat() + "Z"
    return it


def test_ebay_tracker(monkeypatch):
    init_db()
    with session_scope() as s:
        topic = Topic(slug="ebay-test", name="t")
        s.add(topic)
        s.flush()
        src = Source(topic_id=topic.id, kind="ebay", config={"query": "inciardi", "min_price": 25})
        s.add(src)
        s.flush()

        soon = utcnow() + timedelta(hours=1)
        listings = [
            _listing("1", "Inciardi mini print Sardines", 8),
            _listing("2", "Rare custom Inciardi Kune Kune Pig", 70),
            _listing("3", "Inciardi mini print auction Tomato", 5, auction=True, bids=3, end=soon),
        ]
        monkeypatch.setattr(ebay, "search", lambda q, limit=200: listings)
        items = ebay.fetch_ebay(src, s)
        # Cheap ordinary listing is cataloged silently; the $70 "rare custom" one is posted.
        assert [i.key for i in items] == ["ebay:2"]
        assert len(src.state["catalog"]) == 3

        # The auction ends with bids -> recorded as an approximate sale.
        listings[:] = listings[:2]
        src.state = {**src.state, "live": {**src.state["live"], "3": {**src.state["live"]["3"],
                     "end": (utcnow() - timedelta(minutes=1)).isoformat()}}}
        ebay.fetch_ebay(src, s)
        assert src.state["catalog"]["tomato"]["sold"] == [5.0]

        # After warm-up, a never-seen design is a first sighting, and a weekly snapshot appears.
        src.state = {**src.state, "started": (utcnow() - timedelta(days=15)).isoformat()}
        listings.append(_listing("4", "Inciardi mini print Lighthouse", 6))
        items = ebay.fetch_ebay(src, s)
        kinds = {i.extra.get("kind") for i in items}
        assert "first_sighting" in kinds and "market_snapshot" in kinds
        s.delete(topic)  # leave the shared test DB empty for the seed test
