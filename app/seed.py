"""Starting topics and sources. Runs once (only when there are no topics yet)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Source, Topic

TOPICS = [
    {
        "slug": "inciardi",
        "name": "Inciardi mini prints",
        "priority": 5,
        "urgent": True,
        "description": (
            "Inciardi Prints: Ana Inciardi's $1 linocut mini print vending machines (studio in Portland, ME). "
            "Care about: new machine locations, limited-time machines, pop-ups, special editions and collabs, "
            "which prints are rare / exclusive / out of circulation (OOC), and resale prices on eBay and Depop. "
            "Exclusives usually come from collabs and venues (e.g. Only NY's 8-print set, the Whitney's limited "
            "edition, the Richard Scarry series, seasonal runs). @inciardimachines on Instagram tracks locations. "
            "Ignore rehashed press about the origin story."
        ),
        "urgent_criteria": (
            "A NEW machine location, a limited-time or temporary machine, a pop-up, or a special/limited "
            "edition that was just announced or just went live, anywhere. Say where it is as a plain fact; "
            "never suggest the owner travel to it. A machine that is closing "
            "soon is also worth an alert. An eBay 'first sighting' of a design never seen before counts if it "
            "looks like a genuinely new print or venue exclusive (not just an old print we hadn't catalogued)."
        ),
        "sources": [
            {"kind": "page_watch", "name": "inciardiprints.com home", "config": {"url": "https://inciardiprints.com/"}, "interval_minutes": 30},
            {"kind": "sitemap", "name": "inciardiprints.com new pages", "config": {"url": "https://inciardiprints.com/sitemap.xml"}, "interval_minutes": 60},
            {"kind": "shopify", "name": "Inciardi shop products", "config": {"url": "https://inciardiprints.com"}, "interval_minutes": 60},
            {"kind": "page_watch", "name": "miniprint.io home", "config": {"url": "https://miniprint.io/"}, "interval_minutes": 30},
            {"kind": "sitemap", "name": "miniprint.io new pages", "config": {"url": "https://miniprint.io/sitemap.xml"}, "interval_minutes": 60},
            {"kind": "websearch", "name": "Venue announcements and pop-ups", "interval_minutes": 360, "config": {
                "query": "Inciardi mini print vending machine: new host locations, limited-time machines, pop-ups, collab or venue-exclusive print series",
                "instructions": "Look for museums, bookstores, cafes, shops and events announcing a machine or exclusive prints, "
                                "and posts from @inciardimachines / @inciardiprints. Skip origin-story features.",
                "days": 4}},
            {"kind": "websearch", "name": "Collector chatter (Instagram, TikTok)", "interval_minutes": 720, "config": {
                "query": "Inciardi mini prints collectors: new prints pulled, rare finds, trades, machine restocks",
                "domains": "instagram.com, tiktok.com",
                "days": 5}},
            {"kind": "ebay", "name": "eBay sales and new listings", "interval_minutes": 30, "config": {
                "query": "inciardi print", "min_sale_price": 10}},
            {"kind": "websearch", "name": "Depop / Mercari / Etsy resale", "interval_minutes": 4320, "config": {
                "query": "Inciardi mini print listings for sale, especially rare, custom, exclusive or collab prints, with prices",
                "domains": "depop.com, mercari.com, etsy.com, poshmark.com",
                "days": 14, "max_results": 8}},
        ],
    },
    {
        "slug": "runnyc",
        "name": "r/runnyc",
        "priority": 4,
        "description": (
            "The r/runnyc subreddit, read through an RSS.app feed (Reddit closed its API and blocks Claude's "
            "web search): NYC running community chatter. Care about NYRR race talk, 9+1, NYC Marathon "
            "prep and logistics, run clubs, routes, gear, and funny or heated threads."
        ),
        # Add the RSS.app feed for r/runnyc on the topic page (it's tied to your RSS.app account).
        "sources": [],
    },
    {
        "slug": "nyc-running",
        "name": "NYRR & NYC running",
        "priority": 4,
        "description": (
            "New York Road Runners and the NYC running scene: race announcements and registration windows, "
            "NYC Marathon news (the owner is running 2026 on Nov 1 and 2027), 9+1 rules, mile races "
            "(e.g. the 5th Avenue Mile, NYRR track miles), results and community news."
        ),
        "sources": [
            {"kind": "websearch", "name": "NYRR news and race registration", "interval_minutes": 720, "config": {
                "model": "claude-haiku-5-5",
                "query": "New York Road Runners (NYRR) news: race announcements, registration openings, NYC Marathon updates", "days": 3}},
            {"kind": "websearch", "name": "NYC running community", "interval_minutes": 1440, "config": {
                "model": "claude-haiku-5-5",
                "query": "NYC running community news: run clubs, mile races, track meets, road races in New York City", "days": 4}},
        ],
    },
    {
        "slug": "boston-running",
        "name": "Back Bay running: clubs, group runs, races",
        "priority": 4,
        "description": (
            "Very local Boston running around Back Bay: Tracksmith group runs on Newbury Street, the Dick's "
            "Sporting Goods run club, Boylston Street shops' runs, BAA races (and Boston Marathon qualifying/"
            "registration since Boston is one of the owner's remaining majors), mile races around Boston. "
            "Skip clubs far from Back Bay unless it's a big event."
        ),
        "sources": [
            {"kind": "websearch", "name": "Back Bay group runs", "interval_minutes": 1440, "config": {
                "model": "claude-haiku-5-5",
                "query": "Back Bay Boston group runs and run clubs this week: Tracksmith Newbury Street, Dick's Sporting Goods run club, Boylston Street running stores",
                "days": 7}},
            {"kind": "websearch", "name": "BAA and Boston races", "interval_minutes": 1440, "config": {
                "model": "claude-haiku-5-5",
                "query": "Boston Athletic Association (BAA) news, Boston Marathon registration and qualifying, mile races in Boston", "days": 4}},
        ],
    },
    {
        "slug": "wirecutter",
        "name": "Wirecutter deals",
        "priority": 3,
        "description": (
            "Deals recommended by NYT Wirecutter: only real discounts on things Wirecutter actually recommends. "
            "Running gear, travel gear and home stuff are most interesting. Tip: forward the Wirecutter deals "
            "newsletter to wirecutter@ the inbox domain."
        ),
        "sources": [
            {"kind": "page_watch", "name": "Wirecutter deals page", "config": {"url": "https://www.nytimes.com/wirecutter/deals/"}},
            {"kind": "websearch", "name": "Wirecutter deals search", "active": False, "config": {
                "query": "Wirecutter deals of the day (nytimes.com/wirecutter/deals)", "days": 2}},
        ],
    },
    {
        "slug": "amex",
        "name": "AmEx Offers",
        "priority": 3,
        "description": (
            "New American Express Offers (statement credits and bonus points at merchants), plus AmEx card news "
            "and limited-time card bonuses. Care about offers with real value, especially travel, dining in "
            "Boston/NYC, and running/fitness brands. Note expiration dates."
        ),
        "sources": [
            {"kind": "rss", "name": "Doctor of Credit (AmEx)", "config": {
                "url": "https://www.doctorofcredit.com/feed/", "include": "amex|american express"}},
            {"kind": "rss", "name": "Frequent Miler (AmEx)", "config": {
                "url": "https://frequentmiler.com/feed/", "include": "amex|american express"}},
        ],
    },
    {
        "slug": "travel",
        "name": "Travel (One Mile at a Time)",
        "priority": 3,
        "description": (
            "Travel news from the One Mile at a Time blog: airline and hotel loyalty, premium cabins, lounges, "
            "deals, points strategy. Especially relevant: routes from BOS, trips to Chicago and London "
            "(marathon majors the owner still needs), and anything useful for race travel."
        ),
        "sources": [
            {"kind": "rss", "name": "One Mile at a Time", "config": {"url": "https://onemileatatime.com/feed/"}},
        ],
    },
    {
        "slug": "youtube",
        "name": "YouTube: new from my subscriptions",
        "priority": 2,
        "description": (
            "New videos from channels the owner subscribes to. Summarize what's new and worth watching; "
            "skip shorts and filler."
        ),
        "sources": [
            {"kind": "youtube", "name": "My subscriptions", "config": {"use_subscriptions": True}},
        ],
    },
]


def seed(session: Session) -> bool:
    if session.scalar(select(Topic.id).limit(1)):
        return False
    for t in TOPICS:
        topic = Topic(
            slug=t["slug"], name=t["name"], description=t["description"], priority=t.get("priority", 3),
            urgent=t.get("urgent", False), urgent_criteria=t.get("urgent_criteria", ""),
        )
        session.add(topic)
        session.flush()
        for src in t["sources"]:
            session.add(Source(
                topic_id=topic.id, kind=src["kind"], name=src["name"], config=src["config"],
                interval_minutes=src.get("interval_minutes"), active=src.get("active", True),
            ))
    session.commit()
    return True


# --- one-time changes to an existing database --------------------------------------------
# Each runs once; its name is recorded in the "applied_migrations" setting.


def _drop_google_news(session: Session) -> str:
    """Google News answers Render's servers with HTTP 503, so those feeds never work."""
    rows = [src for src in session.scalars(select(Source).where(Source.kind == "rss"))
            if "news.google.com" in str((src.config or {}).get("url", ""))]
    for src in rows:
        session.delete(src)  # items it already fetched stay (their source link is cleared)
    return f"removed {len(rows)} Google News sources"


def _haiku_for_running_searches(session: Session) -> str:
    """The twice-daily running web searches run on Haiku (Opus stays on the newsletter)."""
    rows = list(session.scalars(
        select(Source).join(Topic)
        .where(Source.kind == "websearch", Topic.slug.in_(["nyc-running", "boston-running"]))
    ))
    for src in rows:
        src.config = {**(src.config or {}), "model": "claude-haiku-5-5"}
    return f"{len(rows)} running searches now use Haiku"


def _ebay_digests(session: Session) -> str:
    """eBay switches from one post per listing to two digests a day (recent sales, newest listings)."""
    from app.models import Item

    srcs = list(session.scalars(select(Source).where(Source.kind == "ebay")))
    for src in srcs:
        src.config = {"query": (src.config or {}).get("query") or "inciardi print", "min_sale_price": 10}
        src.interval_minutes = 30
        src.state = {}
        if src.name == "eBay market tracker":
            src.name = "eBay sales and new listings"
    skipped = 0
    for item in session.scalars(select(Item).where(Item.status.in_(["new", "scored", "picked"]))):
        if (item.extra or {}).get("source_kind") == "ebay":
            item.status = "skipped"
            skipped += 1
    return f"{len(srcs)} eBay sources switched to digests, {skipped} single-listing items skipped"


def _videos_to_newsletter(session: Session) -> str:
    """Subscription videos stop going through scoring; recent ones queue for the YouTube section."""
    from datetime import timedelta

    from app.db import utcnow
    from app.models import Item

    since = utcnow() - timedelta(days=3)
    moved = 0
    for item in session.scalars(select(Item).where(Item.status.in_(["new", "scored"]), Item.fetched_at >= since)):
        if (item.extra or {}).get("source_kind") == "youtube" and "/shorts/" not in (item.url or ""):
            item.status = "video"
            moved += 1
    return f"{moved} recent videos queued for the newsletter"


def _rescore_videos(session: Session) -> str:
    """Recent subscription videos get scored again with the YouTube brief and the lower threshold."""
    from datetime import timedelta

    from app.db import utcnow
    from app.models import Item

    since = utcnow() - timedelta(days=3)
    n = 0
    for item in session.scalars(select(Item).where(Item.status.in_(["video", "scored"]), Item.fetched_at >= since)):
        if (item.extra or {}).get("source_kind") == "youtube" and "/shorts/" not in (item.url or ""):
            item.status, item.score, item.triage_note = "new", None, ""
            n += 1
    return f"{n} videos queued for rescoring"


def _retry_travel_backfill(session: Session) -> str:
    """OMAAT's search feed returns 404, so regions marked as searched found nothing. Search again."""
    from app import settings_store

    settings_store.set(session, "travel_backfilled", [])
    return "travel regions queued for a new archive search"


def _cast_of_four(session: Session) -> str:
    """Bench all but the four members the owner has liked most (and who post most); the four
    left pick up the benched members' topics so every topic still has someone who cares."""
    from sqlalchemy import func

    from app.models import Agent, Feedback, Post

    agents = list(session.scalars(select(Agent).where(Agent.active)))
    if len(agents) <= 4:
        return "cast already 4 or fewer"

    def standing(a: Agent) -> float:
        votes = session.scalar(select(func.coalesce(func.sum(Feedback.value), 0))
                               .join(Post, Feedback.post_id == Post.id).where(Post.agent_id == a.id)) or 0
        posts = session.scalar(select(func.count(Post.id)).where(Post.agent_id == a.id)) or 0
        return votes * 2 + posts * 0.1

    agents.sort(key=standing, reverse=True)
    keep, bench = agents[:4], agents[4:]
    for a in bench:
        a.active = False
    slugs = [t.slug for t in session.scalars(select(Topic).where(Topic.active))]
    for i, slug in enumerate(slugs):
        if not any(slug in (a.interests or []) for a in keep):
            a = keep[i % len(keep)]
            a.interests = [*(a.interests or []), slug]
    settings_store_set_threads(session)
    return f"kept {', '.join('@' + a.handle for a in keep)}; benched {len(bench)}"


def settings_store_set_threads(session: Session) -> None:
    """A saved threads_per_cycle above the new default of 4 comes down to 4."""
    from app import settings_store

    if int(settings_store.get(session, "threads_per_cycle") or 4) > 4:
        settings_store.set(session, "threads_per_cycle", 4)


def _drop_reddit_sources(session: Session) -> str:
    """The Reddit fetcher is gone; remove Reddit-kind sources and the disabled reddit.com web search."""
    rows = [src for src in session.scalars(select(Source))
            if src.kind == "reddit"
            or (src.kind == "websearch" and not src.active and "reddit.com" in str((src.config or {}).get("domains", "")))]
    for src in rows:
        session.delete(src)
    return f"removed {len(rows)} Reddit sources"


def _default_races(session: Session) -> str:
    """Start the Races page with the remaining majors, NYC 2027 and mile races."""
    from app import races

    return f"added {races.seed_defaults(session)} races"


def _no_trip_suggestions(session: Session) -> str:
    """The owner doesn't want trips suggested for drops. Rewrite the profile line and the Inciardi alert
    rule that invited it (only if they still read the way they were seeded)."""
    import re

    from app import settings_store

    done = []
    profile = settings_store.get(session, "profile") or ""
    new = re.sub(r"I'll travel the same week if it's within a 2-3 hour flight or a\s+4-5 hour train ride or drive "
                 r"from Boston\.",
                 "Don't suggest trips to go get prints; I decide that myself. A drop in a\ncity I already have a "
                 "trip planned to is worth pointing out.", profile)
    if new != profile:
        settings_store.set(session, "profile", new)
        done.append("profile")
    topic = session.scalar(select(Topic).where(Topic.slug == "inciardi"))
    if topic is not None and "reachable from Boston" in (topic.urgent_criteria or ""):
        topic.urgent_criteria = re.sub(
            r"Alert especially when it's reachable from Boston.*?Southeast coast\)\.",
            "Say where it is as a plain fact; never suggest the owner travel to it.",
            topic.urgent_criteria, flags=re.S)
        topic.urgent_criteria = topic.urgent_criteria.replace("just went live. Say", "just went live, anywhere. Say")
        done.append("Inciardi alert rule")
    return "rewrote " + (", ".join(done) or "nothing (already edited)")


MIGRATIONS = [
    ("2026-11-12-drop-google-news", _drop_google_news),
    ("2026-11-12-haiku-running-searches", _haiku_for_running_searches),
    ("2026-11-12-ebay-digests", _ebay_digests),
    ("2026-11-13-videos-to-newsletter", _videos_to_newsletter),
    ("2026-11-13-rescore-videos", _rescore_videos),
    ("2026-11-15-retry-travel-backfill", _retry_travel_backfill),
    ("2026-11-17-cast-of-four", _cast_of_four),
    ("2026-11-17-drop-reddit-sources", _drop_reddit_sources),
    ("2026-11-17-default-races", _default_races),
    ("2026-11-19-no-trip-suggestions", _no_trip_suggestions),
]


def run_migrations(session: Session) -> list[str]:
    from app import settings_store

    applied = list(settings_store.get(session, "applied_migrations") or [])
    done = []
    for name, fn in MIGRATIONS:
        if name in applied:
            continue
        done.append(f"{name}: {fn(session)}")
        applied.append(name)
        settings_store.set(session, "applied_migrations", applied)
        session.commit()
    return done
