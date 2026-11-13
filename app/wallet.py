"""Your wallet: the cards you hold, Chase 5/24, and filtering card offers you can't (or won't) get.

Clear-cut rules are enforced in code (business cards, Chase 5/24, one Sapphire, a card you already
hold). Everything else goes through Claude with the editable rules text from the Wallet page.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import llm, settings_store
from app.db import utcnow
from app.models import Item, Topic, WalletCard

log = logging.getLogger(__name__)

ISSUERS = ["Chase", "American Express", "Citi", "Capital One", "Bank of America", "Wells Fargo", "U.S. Bank",
           "Barclays", "Bilt", "Discover", "Other"]

DEFAULT_CARD_RULES = """\
- Chase 5/24: Chase declines applicants who opened 5 or more personal cards (any issuer) in the last 24 months.
  Authorized-user cards count. Business cards from Chase, Amex, Citi, Bank of America, Barclays, U.S. Bank and
  Wells Fargo don't count; Capital One, Discover and TD business cards do.
- Sapphire: only one Sapphire card (Preferred or Reserve) at a time. If I hold one, the other isn't for me.
- No sign-up bonus on a card I already hold.
- Amex: a card's welcome bonus is generally once per lifetime, so a card I hold or held isn't eligible.
- I don't want business cards or business-only offers at all.
"""

_CARD_WORDS = re.compile(
    r"\b(credit card|card|bonus|sign[- ]?up|welcome offer|membership rewards|ultimate rewards|thankyou points|"
    r"sapphire|platinum|gold card|business|amex|american express|chase|citi|capital one|venture|bilt|"
    r"aeroplan|marriott bonvoy card|hilton honors card|ink)\b", re.I)


def opened_label(card: WalletCard) -> str:
    return card.opened.strftime("%b %Y") if card.opened else "date unknown"


def count_524(session: Session, today: datetime | None = None) -> tuple[int, list[WalletCard], datetime | None]:
    """(count, the cards counting, when the oldest of them stops counting)."""
    today = today or utcnow()
    cutoff = today - timedelta(days=730)
    cards = [c for c in session.scalars(select(WalletCard)) if c.counts_524 and c.opened and c.opened >= cutoff]
    cards.sort(key=lambda c: c.opened)
    next_drop = (cards[0].opened + timedelta(days=730)) if cards else None
    return len(cards), cards, next_drop


def rules_text(session: Session) -> str:
    return settings_store.get(session, "card_rules") or DEFAULT_CARD_RULES


def summary(session: Session) -> str:
    cards = list(session.scalars(select(WalletCard).order_by(WalletCard.closed, WalletCard.opened)))
    if not cards:
        return ""
    n, _, _ = count_524(session)
    lines = ", ".join(f"{c.name} ({c.issuer or 'issuer?'}{', business' if c.business else ''}, opened "
                      f"{opened_label(c)}{', closed' if c.closed else ''})" for c in cards)
    return f"Cards in my wallet: {lines}. Chase 5/24 count: {n}/5."


def profile_text(session: Session) -> str:
    """The owner's profile plus wallet summary, for every prompt that reads the profile."""
    profile = settings_store.get(session, "profile")
    wallet = summary(session)
    return profile + (f"\n{wallet}\n" if wallet else "")


# --- filtering card offers ------------------------------------------------------------------


class _CardCheck(BaseModel):
    id: int
    is_card_offer: bool = Field(description="A credit card sign-up/welcome bonus, upgrade, or 'apply now' offer")
    business: bool = Field(description="True if the offer is only for business cards or business accounts")
    card: str = Field(default="", description="The card's name, e.g. 'Chase Sapphire Reserve'")
    issuer: str = Field(default="", description="Chase, American Express, Citi, ...")
    eligible: bool = Field(default=True, description="Whether the owner can get this offer under the rules")
    reason: str = Field(default="", description="One plain line: why not eligible (empty if eligible)")


class _CardChecks(BaseModel):
    items: list[_CardCheck]


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (name or "").lower().replace("american express", "amex")).strip()


def _rule_override(session: Session, check: _CardCheck) -> str:
    """Hard rules applied in code. Returns a reason to filter, or ''."""
    if check.business:
        return "business card offer"
    if not check.is_card_offer:
        return ""
    card, issuer = _norm(check.card), _norm(check.issuer)
    held = [c for c in session.scalars(select(WalletCard).where(WalletCard.closed.is_(False)))]
    if card:
        for c in held:
            mine = _norm(c.name)
            if mine and (mine in card or card in mine):
                return f"you already have the {c.name}"
    if "sapphire" in card and any("sapphire" in _norm(c.name) for c in held):
        return "you already hold a Sapphire card (one at a time)"
    if "chase" in issuer or "chase" in card or "sapphire" in card or re.search(r"\bink\b", card):
        n, _, next_drop = count_524(session)
        if n >= 5:
            when = f"; next card drops off {next_drop:%b %Y}" if next_drop else ""
            return f"over Chase 5/24 ({n} new cards in 24 months{when})"
    return ""


def check_offers(session: Session, limit: int = 60) -> int:
    """Check recent unchecked items that mention cards; filter business and ineligible offers."""
    since = utcnow() - timedelta(days=3)
    rows = list(session.scalars(
        select(Item).join(Topic)
        .where(Item.fetched_at >= since, Item.status.in_(["new", "scored"]), Topic.active)
    ))
    todo = [i for i in rows if not (i.extra or {}).get("card_checked")
            and (i.extra or {}).get("kind") != "ebay_digest"
            and (i.extra or {}).get("source_kind") != "youtube"
            and _CARD_WORDS.search(f"{i.title} {(i.body or '')[:400]}")][:limit]
    if not todo:
        return 0
    from app.triage import item_brief

    profile = profile_text(session)
    filtered = 0
    for start in range(0, len(todo), 30):
        chunk = todo[start:start + 30]
        res = llm.generate(
            purpose="card_check", role="fast",
            system=("You check credit card offers against one person's wallet and rules. Merchant deals like "
                    "Amex Offers (statement credits at a store) are not card offers, but mark them business "
                    "if they only apply to business cards.\n\nAbout the owner:\n" + profile
                    + "\n\nTheir card rules:\n" + rules_text(session)),
            user="Items:\n\n" + "\n\n".join(item_brief(i, 600) for i in chunk),
            max_tokens=3000, effort="low", schema=_CardChecks, critical=True,
            fake=lambda c=chunk: _CardChecks(items=[_CardCheck(id=i.id, is_card_offer=False, business=False) for i in c]),
        )
        by_id = {c.id: c for c in res.parsed.items}
        for it in chunk:
            check = by_id.get(it.id)
            extra = {**(it.extra or {}), "card_checked": True}
            if check is not None:
                reason = _rule_override(session, check) or (
                    "" if check.eligible or not check.is_card_offer else (check.reason or "not eligible"))
                if check.is_card_offer:
                    extra["card"] = {"name": check.card, "issuer": check.issuer}
                if reason:
                    it.status = "skipped"
                    it.triage_note = f"Filtered: {reason}"[:500]
                    filtered += 1
            it.extra = extra
        session.commit()
    return filtered
