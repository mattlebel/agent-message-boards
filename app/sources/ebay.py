"""eBay for Inciardi collectors (Browse API, free keys from developer.ebay.com).

Two posts per cycle, which the agents then discuss:
- the last 10 sales over $10, highest price first
- the 10 newest listings, newest first

eBay's sold-listings API is closed to individual developers, so "sales" are built by watching
listings: an auction that closes with bids is a sale at the last bid seen, and a fixed-price
listing that disappears before its end is a likely sale at its asking price. The tracker checks
every 30 minutes to catch closing bids. Listings are never posted one at a time.

No eBay user data is kept (no seller or buyer names), which is what lets the keyset claim
eBay's Marketplace Account Deletion exemption.
"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta
from urllib.parse import quote_plus

from app import config
from app.db import utcnow
from app.sources.base import RawItem, SourceError, http

_token: dict = {"value": None, "expires": 0.0}
SEARCH_LIMIT = 200
KEEP_SALES = 200


def _access_token() -> str:
    if not (config.EBAY_CLIENT_ID and config.EBAY_CLIENT_SECRET):
        raise SourceError("set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET (developer.ebay.com) to watch eBay")
    if _token["value"] and time.time() < _token["expires"] - 60:
        return _token["value"]
    auth = base64.b64encode(f"{config.EBAY_CLIENT_ID}:{config.EBAY_CLIENT_SECRET}".encode()).decode()
    with http() as c:
        resp = c.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
            headers={"Authorization": f"Basic {auth}"},
        )
    if resp.status_code != 200:
        raise SourceError(f"eBay auth failed: HTTP {resp.status_code}")
    data = resp.json()
    _token.update(value=data["access_token"], expires=time.time() + data.get("expires_in", 7200))
    return _token["value"]


def search(query: str, limit: int = SEARCH_LIMIT) -> tuple[list[dict], int | None]:
    """Newest listings for a query, plus eBay's total count of matches."""
    token = _access_token()
    with http() as c:
        resp = c.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            params={"q": query, "limit": min(limit, 200), "sort": "newlyListed"},
            headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        )
    if resp.status_code >= 400:
        raise SourceError(f"eBay search HTTP {resp.status_code}")
    data = resp.json()
    return data.get("itemSummaries", []) or [], data.get("total")


def _price(it: dict) -> float:
    for key in ("currentBidPrice", "price"):
        val = (it.get(key) or {}).get("value")
        if val:
            return float(val)
    return 0.0


def _when(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _is_inciardi(it: dict) -> bool:
    return "inciardi" in (it.get("title") or "").lower()


def fetch_ebay(source, session) -> list[RawItem]:
    """Update the listing tracker and sales log. Posts come from build_digests() in the cycle."""
    cfg = source.config or {}
    query = cfg.get("query")
    if not query:
        raise SourceError("ebay needs a search query")
    listings, total = search(query)
    now = utcnow()
    state = dict(source.state or {})
    live: dict = dict(state.get("live") or {})
    sales: list = list(state.get("sales") or [])
    current = {it["itemId"]: it for it in listings if _is_inciardi(it)}
    # If eBay returned every match, a listing missing from the results has really ended.
    complete_view = total is not None and total <= SEARCH_LIMIT

    for item_id, rec in list(live.items()):
        if item_id in current:
            continue
        end = _when(rec.get("end"))
        stale = (_when(rec.get("first_seen")) or now) < now - timedelta(days=45)
        if rec.get("auction"):
            if end and end <= now:
                if rec.get("bids", 0) > 0:
                    sales.append({"id": item_id, "title": rec.get("title", ""), "price": rec.get("price", 0),
                                  "kind": "auction", "bids": rec.get("bids", 0), "url": rec.get("url", ""),
                                  "ended": now.isoformat()})
                live.pop(item_id)
            elif stale:
                live.pop(item_id)
        elif complete_view and not (end and end <= now):
            sales.append({"id": item_id, "title": rec.get("title", ""), "price": rec.get("price", 0),
                          "kind": "fixed", "bids": 0, "url": rec.get("url", ""), "ended": now.isoformat()})
            live.pop(item_id)
        elif stale or (end and end <= now):
            live.pop(item_id)

    for item_id, it in current.items():
        rec = dict(live.get(item_id) or {"first_seen": now.isoformat()})
        rec.update(
            title=(it.get("title") or "")[:200], price=_price(it), bids=it.get("bidCount", 0) or 0,
            end=it.get("itemEndDate", ""), auction="AUCTION" in (it.get("buyingOptions") or []),
            url=(it.get("itemWebUrl") or "").split("?")[0], created=it.get("itemCreationDate", ""),
            condition=it.get("condition", ""),
        )
        live[item_id] = rec

    state.update(live=live, sales=sales[-KEEP_SALES:], total=total, checked=now.isoformat())
    state.pop("catalog", None)  # from the old per-listing tracker
    source.state = state
    return []


def _sale_line(n: int, s: dict) -> str:
    how = (f"auction, {s.get('bids', 0)} bid{'s' if s.get('bids', 0) != 1 else ''}, closing bid"
           if s.get("kind") == "auction" else "fixed price, listing ended (likely sold)")
    when = _when(s.get("ended"))
    return f"{n}. ${s.get('price', 0):,.2f} — {s.get('title', '')} ({how}{f', {when:%b %-d}' if when else ''}) {s.get('url', '')}"


def _listing_line(n: int, rec: dict) -> str:
    kind = f"auction, {rec.get('bids', 0)} bids so far" if rec.get("auction") else "buy it now"
    created = _when(rec.get("created"))
    return (f"{n}. ${rec.get('price', 0):,.2f} — {rec.get('title', '')} ({kind}"
            f"{f', listed {created:%b %-d}' if created else ''}) {rec.get('url', '')}")


def build_digests(source, now: datetime | None = None) -> list[RawItem]:
    """The two posts for this cycle: recent sales and newest listings. Skips a post if nothing changed."""
    now = now or utcnow()
    cfg = source.config or {}
    state = dict(source.state or {})
    live = state.get("live") or {}
    min_sale = float(cfg.get("min_sale_price", 10))
    last = dict(state.get("digest") or {})
    stamp = now.strftime("%Y-%m-%d-%H")
    query = quote_plus(cfg.get("query") or "inciardi print")
    note = ("A sample from eBay's search, not every Inciardi listing on eBay. Don't judge rarity or how many "
            "exist from this list alone.")
    items: list[RawItem] = []

    recent = [s for s in state.get("sales") or [] if (s.get("price") or 0) > min_sale][-10:]
    recent.sort(key=lambda s: s.get("price") or 0, reverse=True)
    sale_ids = [s["id"] for s in recent]
    if recent and sale_ids != last.get("sale_ids"):
        items.append(RawItem(
            key=f"ebay-digest:{source.id}:sales:{stamp}",
            title=f"Inciardi on eBay: last {len(recent)} sales over ${min_sale:,.0f}, highest first",
            url=f"https://www.ebay.com/sch/i.html?_nkw={query}&LH_Sold=1&LH_Complete=1",
            body="\n".join(_sale_line(i + 1, s) for i, s in enumerate(recent)) + f"\n\n{note}",
            extra={"kind": "ebay_digest", "digest": "sales", "urgent_checked": True},
        ))
        last["sale_ids"] = sale_ids

    newest = sorted(live.items(), key=lambda kv: kv[1].get("created") or kv[1].get("first_seen") or "",
                    reverse=True)[:10]
    newest_ids = [k for k, _ in newest]
    if newest and newest_ids != last.get("newest_ids"):
        items.append(RawItem(
            key=f"ebay-digest:{source.id}:newest:{stamp}",
            title=f"Inciardi on eBay: {len(newest)} newest listings",
            url=f"https://www.ebay.com/sch/i.html?_nkw={query}&_sop=10",
            body="\n".join(_listing_line(i + 1, rec) for i, (_, rec) in enumerate(newest)) + f"\n\n{note}",
            extra={"kind": "ebay_digest", "digest": "newest", "urgent_checked": True},
        ))
        last["newest_ids"] = newest_ids

    if items:
        state["digest"] = last
        source.state = state
    return items


def is_digest(item) -> bool:
    return (item.extra or {}).get("kind") == "ebay_digest"

