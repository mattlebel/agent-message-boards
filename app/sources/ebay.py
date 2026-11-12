"""eBay as a collector's market tracker (Browse API, free keys from developer.ebay.com).

eBay's sold-history API is closed to individual developers, so this builds its own memory:
- a catalog of every print design seen in listings, with asking-price ranges
- auctions followed until they end, so final bids become rough "sold" prices
- "first sightings": a design never seen before can mean an unannounced new machine or edition
- a weekly market snapshot (most expensive, rarest, most common, auction results)

Only notable listings become board items; the rest just feed the catalog.

No eBay user data is kept (no seller or buyer names), which is what lets the keyset claim
eBay's Marketplace Account Deletion exemption.
"""

from __future__ import annotations

import base64
import re
import time
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from app import config
from app.db import utcnow
from app.sources.base import RawItem, SourceError, http

_token: dict = {"value": None, "expires": 0.0}

RARE_WORDS = r"\b(custom|exclusive|collab|limited|rare|ooc|retired|pop[- ]?up|special edition|museum|signed|test print|artist proof|\d+/\d+)\b"
WARMUP_DAYS = 14  # don't call anything a "first sighting" until the catalog has had time to fill in


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


def search(query: str, limit: int = 200) -> list[dict]:
    token = _access_token()
    with http() as c:
        resp = c.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            params={"q": query, "limit": min(limit, 200), "sort": "newlyListed"},
            headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        )
    if resp.status_code >= 400:
        raise SourceError(f"eBay search HTTP {resp.status_code}")
    return resp.json().get("itemSummaries", []) or []


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


class _Subject(BaseModel):
    id: str
    design: str = Field(description="Short canonical name of the print design, e.g. 'Sardines' or 'Kune Kune Pig'. "
                                    "'lot' for multi-print lots, 'not a print' for unrelated items.")
    series: str = Field(default="", description="Special series / collab / venue exclusive if the title says so")


class _Subjects(BaseModel):
    items: list[_Subject]


def _identify(listings: list[dict], known: list[str]) -> dict[str, _Subject]:
    """Map listing titles to canonical print designs (one cheap Haiku call per batch)."""
    from app import llm

    out: dict[str, _Subject] = {}
    for start in range(0, len(listings), 60):
        batch = listings[start : start + 60]
        lines = "\n".join(f"{it['itemId']}: {it.get('title', '')}" for it in batch)
        res = llm.generate(
            purpose="ebay_identify",
            role="fast",
            system=(
                "You catalog Inciardi mini prints (small $1 linocut vending machine prints) from eBay listing "
                "titles. Name the design the way a collector would, reusing an existing name when it's the same "
                "design so the catalog stays consistent."
            ),
            user=f"Designs already in the catalog:\n{', '.join(known[:400]) or '(none yet)'}\n\nListings:\n{lines}",
            max_tokens=4000,
            effort="low",
            schema=_Subjects,
            fake=lambda b=batch: _Subjects(items=[
                _Subject(id=it["itemId"], design=(it.get("title") or "x").split()[-1].lower()) for it in b
            ]),
        )
        for s in res.parsed.items:
            out[s.id] = s
    return out


def fetch_ebay(source, session) -> list[RawItem]:
    cfg = source.config or {}
    query = cfg.get("query")
    if not query:
        raise SourceError("ebay needs a search query")
    notable_price = float(cfg.get("min_price", 25))
    listings = search(query, int(cfg.get("limit", 200)))

    now = utcnow()
    state = dict(source.state or {})
    catalog: dict = dict(state.get("catalog") or {})
    live: dict = dict(state.get("live") or {})
    started = _when(state.get("started")) or now
    warmed_up = now >= started + timedelta(days=WARMUP_DAYS)
    items: list[RawItem] = []

    # 1. Auctions that ended since last time: record the final bid as an approximate sale.
    current_ids = {it["itemId"] for it in listings}
    for item_id, rec in list(live.items()):
        if item_id in current_ids:
            continue
        end = _when(rec.get("end"))
        if rec.get("auction") and end and end <= now and rec.get("bids", 0) > 0:
            entry = catalog.setdefault(rec["design"], {"first_seen": now.isoformat(), "listings": 1})
            entry["sold"] = (entry.get("sold") or [])[-9:] + [rec["price"]]
            entry["last_sold"] = now.isoformat()
            live.pop(item_id)
        elif not end or end <= now or _when(rec.get("seen", "")) and _when(rec["seen"]) < now - timedelta(days=45):
            live.pop(item_id)

    # 2. New listings: identify the design, update the catalog, surface the interesting ones.
    new = [it for it in listings if it["itemId"] not in live]
    designs = _identify(new, sorted(catalog)) if new else {}
    for it in new:
        subj = designs.get(it["itemId"])
        design = (subj.design if subj else "unknown").strip().lower()
        price = _price(it)
        is_auction = "AUCTION" in (it.get("buyingOptions") or [])
        live[it["itemId"]] = {
            "design": design, "price": price, "bids": it.get("bidCount", 0) or 0,
            "end": it.get("itemEndDate", ""), "auction": is_auction, "seen": now.isoformat(),
        }
        if design in ("not a print", "unknown"):
            continue
        first_time = design not in catalog and design != "lot"
        entry = catalog.setdefault(design, {"first_seen": now.isoformat(), "listings": 0})
        entry["listings"] = entry.get("listings", 0) + 1
        entry["min"] = min(entry.get("min", price), price) if price else entry.get("min")
        entry["max"] = max(entry.get("max", price), price)
        if subj and subj.series:
            entry["series"] = subj.series

        title = it.get("title", "")
        rare_words = bool(re.search(RARE_WORDS, title, re.I))
        stats = (f"Catalog: '{design}' seen in {entry['listings']} listing(s), asking ${entry.get('min') or 0:,.0f}"
                 f"-${entry.get('max') or 0:,.0f}" + (f", recent auction results ${entry['sold']}" if entry.get("sold") else ""))
        if first_time and warmed_up:
            items.append(RawItem(
                key=f"ebay-first:{design}",
                title=f"New Inciardi design spotted on eBay: {design.title()}" + (f" ({subj.series})" if subj and subj.series else ""),
                url=it.get("itemWebUrl", ""),
                body=(f"A design the board has never seen in {WARMUP_DAYS}+ days of tracking just got listed: “{title}” "
                      f"at ${price:,.2f}. That can mean a new machine, a venue exclusive or a new series went live "
                      f"somewhere. {stats}."),
                published_at=_when(it.get("itemCreationDate")),
                extra={"kind": "first_sighting", "price": price, "design": design,
                       "image": (it.get("image") or {}).get("imageUrl")},
            ))
        elif price >= notable_price or rare_words:
            items.append(RawItem(
                key=f"ebay:{it['itemId']}",
                title=f"eBay: {title} (${price:,.2f}{', auction' if is_auction else ''})",
                url=it.get("itemWebUrl", ""),
                body=f"{'Auction, current bid' if is_auction else 'Asking'} ${price:,.2f}. Condition: "
                     f"{it.get('condition', 'n/a')}. {stats}.",
                published_at=_when(it.get("itemCreationDate")),
                extra={"kind": "listing", "price": price, "design": design,
                       "image": (it.get("image") or {}).get("imageUrl")},
            ))

    # Keep current bids/prices fresh for auctions still running.
    for it in listings:
        if it["itemId"] in live:
            live[it["itemId"]]["price"] = _price(it)
            live[it["itemId"]]["bids"] = it.get("bidCount", 0) or 0

    # 3. Weekly market snapshot for the agents (and the newsletter) to chew on.
    last_report = _when(state.get("last_report"))
    if catalog and (last_report is None or last_report < now - timedelta(days=7)) and now >= started + timedelta(days=7):
        items.append(_snapshot(catalog, live, now))
        state["last_report"] = now.isoformat()

    state.update(catalog=catalog, live=live, started=started.isoformat())
    source.state = state
    return items


def _snapshot(catalog: dict, live: dict, now: datetime) -> RawItem:
    rows = [(d, e) for d, e in catalog.items() if d not in ("lot",)]
    priciest = sorted(rows, key=lambda r: r[1].get("max") or 0, reverse=True)[:8]
    rarest = [r for r in rows if r[1].get("listings", 0) <= 1][:10]
    common = sorted(rows, key=lambda r: r[1].get("listings", 0), reverse=True)[:6]
    week_ago = now - timedelta(days=7)
    newly = [d for d, e in rows if (_when(e.get("first_seen")) or now) >= week_ago]
    sold = [(d, e["sold"]) for d, e in rows if e.get("sold") and (_when(e.get("last_sold")) or week_ago) >= week_ago]

    def fmt(d, e):
        return f"{d.title()} (seen {e.get('listings', 0)}x, ${e.get('min') or 0:,.0f}-${e.get('max') or 0:,.0f}" + (
            f", {e['series']}" if e.get("series") else "") + ")"

    body = "\n".join([
        f"{len(live)} Inciardi listings live on eBay; {len(rows)} designs in the catalog.",
        "Highest asks: " + "; ".join(fmt(d, e) for d, e in priciest),
        "Rarest (seen once): " + ("; ".join(fmt(d, e) for d, e in rarest) or "none"),
        "Most common (likely still in machines): " + "; ".join(fmt(d, e) for d, e in common),
        "First seen this week: " + (", ".join(x.title() for x in newly) or "none"),
        "Auctions that closed with bids this week: " + ("; ".join(f"{d.title()} ${p[-1]:,.0f}" for d, p in sold) or "none"),
    ])
    return RawItem(
        key=f"ebay-snapshot:{now:%G-W%V}",
        title=f"Inciardi resale market, week of {now:%b %-d}",
        url="https://www.ebay.com/sch/i.html?_nkw=inciardi+mini+print&_sop=10",
        body=body,
        extra={"kind": "market_snapshot"},
    )
