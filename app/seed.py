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
            "Ignore rehashed press about the origin story."
        ),
        "urgent_criteria": (
            "A NEW machine location, a limited-time or temporary machine, a pop-up, or a special/limited "
            "edition that was just announced or just went live. Alert especially when it's reachable from "
            "Boston within a 2-3 hour flight or a 4-5 hour train ride or drive (roughly: all of New England, "
            "NYC, Philly, DC, Montreal, Toronto, Chicago, the Southeast coast). A machine that is closing "
            "soon is also worth an alert."
        ),
        "sources": [
            {"kind": "page_watch", "name": "inciardiprints.com home", "config": {"url": "https://inciardiprints.com/"}, "interval_minutes": 30},
            {"kind": "sitemap", "name": "inciardiprints.com new pages", "config": {"url": "https://inciardiprints.com/sitemap.xml"}, "interval_minutes": 60},
            {"kind": "shopify", "name": "Inciardi shop products", "config": {"url": "https://inciardiprints.com"}, "interval_minutes": 60},
            {"kind": "page_watch", "name": "miniprint.io home", "config": {"url": "https://miniprint.io/"}, "interval_minutes": 30},
            {"kind": "sitemap", "name": "miniprint.io new pages", "config": {"url": "https://miniprint.io/sitemap.xml"}, "interval_minutes": 60},
            {"kind": "websearch", "name": "News of new machines and pop-ups", "interval_minutes": 360, "config": {
                "query": "Inciardi mini print vending machine: new locations, limited-time machines, pop-ups, special editions",
                "instructions": "Local news, museum/shop announcements, Instagram or Reddit posts about a machine arriving or a pop-up. Skip the generic origin-story articles.",
                "days": 4}},
            {"kind": "ebay", "name": "eBay listings", "config": {"query": "inciardi mini print"}},
        ],
    },
    {
        "slug": "runnyc",
        "name": "r/runnyc",
        "priority": 4,
        "description": (
            "The r/runnyc subreddit: NYC running community chatter. Care about NYRR race talk, 9+1, NYC Marathon "
            "prep and logistics, run clubs, routes, gear, and funny or heated threads."
        ),
        "sources": [
            {"kind": "reddit", "name": "r/runnyc", "config": {"subreddit": "runnyc", "sort": "new", "min_score": 2}},
            {"kind": "websearch", "name": "Backup: search r/runnyc (turn on if Reddit access stops)", "active": False,
             "config": {"query": "site:reddit.com/r/runnyc new posts", "days": 2}},
        ],
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
                "query": "New York Road Runners (NYRR) news: race announcements, registration openings, NYC Marathon updates", "days": 3}},
            {"kind": "websearch", "name": "NYC running community", "interval_minutes": 1440, "config": {
                "query": "NYC running community news: run clubs, mile races, track meets, road races in New York City", "days": 4}},
            {"kind": "reddit", "name": "r/running (NYC / marathon mentions)", "config": {
                "subreddit": "running", "sort": "hot", "min_score": 50, "include": "NYC|New York|NYRR|marathon major|abbott|mile"}},
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
                "query": "Back Bay Boston group runs and run clubs this week: Tracksmith Newbury Street, Dick's Sporting Goods run club, Boylston Street running stores",
                "days": 7}},
            {"kind": "websearch", "name": "BAA and Boston races", "interval_minutes": 1440, "config": {
                "query": "Boston Athletic Association (BAA) news, Boston Marathon registration and qualifying, mile races in Boston", "days": 4}},
            {"kind": "reddit", "name": "r/boston (running mentions)", "config": {
                "subreddit": "boston", "sort": "hot", "min_score": 20, "include": "\\brun(ning|ners?)?\\b|marathon|\\b5k\\b|\\bmile\\b|tracksmith"}},
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
            {"kind": "reddit", "name": "r/amex", "config": {"subreddit": "amex", "sort": "hot", "min_score": 10}},
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
