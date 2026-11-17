"""The twice-daily recap (a Money Stuff parody) and urgent alert emails."""

from __future__ import annotations

import html as html_lib
import logging
import re
from datetime import datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.wallet import profile_text as wallet_profile
from app import clicks, config, emailer, images, llm, mailcommands, races, reading, settings_store, share, style, travel, trips
from app.db import utcnow
from app.discussion import thread_heat
from app.models import Coverage, Item, Newsletter, Source, Thread, Topic
from app.triage import AlertDecision

log = logging.getLogger(__name__)

STYLE_GUIDE = """You write a twice-daily email newsletter that recaps a private message board.
It's a loving parody of Matt Levine's "Money Stuff" from Bloomberg: the same shape and spirit, with your own
original jokes and sentences. Never copy his actual text, never claim to be him, and don't mention him.

Voice:
- Conversational, wry, deadpan, smart. You find the hidden mechanics in everything: incentives, arbitrage,
  why a rule exists, who is the "counterparty". A $1 mini-print vending machine is a market-structure story.
  A credit card offer is a structured product. A marathon lottery is an auction with weird clearing rules.
- You explain things patiently, as if to a smart friend, with "the way this works is..." moments.
- Long, winding sentences followed by short punchlines, an occasional rhetorical question, self-aware asides.
  Use these devices with intent and variety; when one starts repeating, it has become a tic.
- Footnotes are part of the voice: use markdown footnotes ([^1]) for asides and jokes. Aim for 2-5.
- Recurring bits are good. Running jokes can build over editions (e.g. "everything is points arbitrage").
- Lead with what matters most to the reader: something to act on (a drop, a deadline, a race opening, an
  offer about to expire) beats an interesting argument. Say the action, the date and the link plainly.
- Board members are sources, not the story. Quote one by @handle only when they said something genuinely
  sharp or useful. Don't narrate a thread's back-and-forth, and don't recap who agreed with whom.
- Don't write about what isn't known ("the price hasn't been posted", "nobody knows when"). Leave it out.

Structure (markdown):
1. A one-line greeting-free opener is fine, or jump straight in.
2. 1-2 main sections with "## " headings on the most important item(s): the facts first, then your take on
   the mechanics (400-700 words total).
3. "## Things happened" - a bulleted list of 6-12 short items, each one sentence with a link, often with a
   dry comment after it.
4. If the reader saved links, a "## From your reading list" section (see the material).
5. If the reader has upcoming trips, a "## Upcoming trips" section (see the material).
6. If there are new reviews from the reader's places, a "## Reviews from your places" list (see the material).
7. If there are new subscription videos, a "## New on YouTube" list (see the material).
8. If there are dated actions, end with "## Dates" - a short list of what to do and when.

Links: link to original sources with their URLs, and to board threads with the board URLs given. Never invent URLs.
Images: when an item in the material has an "Image:" URL, you may show it once, with markdown
![a short plain description](url) on its own line right after the paragraph about that item. Use up to 4,
favoring drops, alerts, prints and places. Only use Image URLs from the material.
Facts: only use facts present in the material. Opinions and jokes are yours.
Length: 700-1,200 words, shorter on a slow day. Never pad.
Subject line: specific and wry, about the lead item. No counted lists ("five X, two Y, zero Z").

The parody's signature moves (footnotes, the patient "here's how this works" explanation, running jokes,
the odd economics metaphor) are welcome when they're doing real work. The writing rules below still apply:
a newsletter that reads like a language model imitating a columnist is the failure mode to avoid."""


MORNING_FORMAT = """This is the MORNING edition: a brief that reads in about three minutes. It replaces the
Structure and Length rules above (voice and everything else still apply).
- 300-500 words. No essay and no "## " section longer than a short paragraph.
- Optional one-line opener, then "## Today": anything to act on today or this week, each with the date and
  link. Leave the section out if there's nothing to act on.
- "## Overnight": 4-8 bullets, one sentence each with a link, a dry aside where it earns it.
- "## From your reading list" (one line per link) and "## Upcoming trips" (only trips within 7 days) when
  the material has them.
- At most one footnote and one image. Longer explanations wait for the evening edition."""

EVENING_NOTE = ("This is the EVENING edition: the full column described above. YouTube videos and trip "
                "reports are saved for this edition.")


class _Edition(BaseModel):
    subject: str = Field(description="Email subject line: witty, specific, under 80 characters")
    preheader: str = Field(description="Inbox preview text, under 110 characters")
    markdown: str = Field(description="The full newsletter body in markdown, with footnotes")


def keep_known_images(md: str, allowed: set[str], limit: int = 4) -> str:
    """Remove markdown images whose URL we didn't provide (no invented images), and cap the count."""
    kept = 0

    def repl(m):
        nonlocal kept
        if m.group(2) in allowed and kept < limit:
            kept += 1
            return m.group(0)
        return ""

    return re.sub(r"!\[([^\]]*)\]\((\S+?)\)", repl, md or "")


def thread_url(thread_id: int) -> str:
    return f"{config.BASE_URL}/t/{thread_id}"


def _thread_material(t: Thread, max_chars: int = 3500, covered_until: int | None = None) -> str:
    head = f"### Thread: {t.title}\nBoard URL: {thread_url(t.id)}\nSection: {t.topic.name if t.topic else 'General'}"
    if t.topic and t.topic.slug == mailcommands.ASK_SLUG:
        head += ("\nTHE READER ASKED THIS by replying to an earlier edition. Answer it near the top of this "
                 "edition: the question in a line, the board's best answer, and the thread link.")
    if covered_until:
        head += ("\nFOLLOW-UP: an earlier edition already covered this thread. Only the posts below are new; "
                 "mention it only for what they add, in a line or two, and don't re-explain the story.")
    if t.item:
        head += f"\nOriginal source: {t.item.url or '(none)'}\nAbout: {t.item.title}. {t.item.body[:700]}"
        if images.for_item(t.item):
            head += f"\nImage: {images.for_item(t.item)}"
    posts = []
    for p in t.posts:
        if covered_until and p.id <= covered_until:
            continue
        who = f"@{p.agent.handle}" if p.agent else "@owner (the reader)"
        reaction = ""
        if p.feedback is not None:
            reaction = " [the reader liked this]" if p.feedback.value > 0 else " [the reader disliked this]"
        posts.append(f"{who}{reaction}: {p.body}")
    body = "\n\n".join(posts)
    return head + "\n\n" + body[:max_chars]


def covered_posts(session: Session) -> dict[int, int]:
    """{thread_id: last post id already covered by a sent newsletter}."""
    rows = session.execute(
        select(Coverage.thread_id, Coverage.last_post_id).join(Newsletter, Newsletter.id == Coverage.newsletter_id)
        .where(Newsletter.sent_at.is_not(None))
    )
    out: dict[int, int] = {}
    for tid, last in rows:
        out[tid] = max(out.get(tid, 0), last)
    return out


def collect(session: Session, since: datetime) -> tuple[list[Thread], list[Item], dict[int, int]]:
    """Threads and items for the next edition, plus {thread_id: covered-up-to post id} for follow-ups.

    A thread a previous edition covered only comes back when it has new posts, and then only those."""
    threads = list(session.scalars(select(Thread).where(Thread.last_activity_at >= since)))
    # Reading-list threads get their own section, so they aren't counted twice.
    threads = [t for t in threads if not (t.topic and t.topic.slug == reading.TOPIC_SLUG)]
    covered = covered_posts(session)
    followups: dict[int, int] = {}
    fresh = []
    for t in threads:
        done = covered.get(t.id)
        if done:
            if not any(p.id > done for p in t.posts):
                continue
            followups[t.id] = done
        fresh.append(t)
    threads = fresh
    boost = clicks.topic_boost(session)
    # Follow-ups rank below new threads; topics you click rank a little higher.
    threads.sort(key=lambda t: thread_heat(t) + 2 * boost.get(t.topic_id, 0) - (8 if t.id in followups else 0)
                 + (30 if t.topic and t.topic.slug == mailcommands.ASK_SLUG else 0),  # your questions first
                 reverse=True)
    # At most N threads per topic (alerts always make it), so one busy topic can't fill the edition.
    cap = int(settings_store.get(session, "max_newsletter_threads_per_topic") or 2)
    per_topic: dict[int, int] = {}
    kept = []
    for t in threads:
        if not t.is_alert:
            if per_topic.get(t.topic_id, 0) >= cap:
                continue
            per_topic[t.topic_id] = per_topic.get(t.topic_id, 0) + 1
        kept.append(t)
    threads = kept
    # Items that never got a thread but scored well (e.g. YouTube videos) for "Things happened".
    discussed = {t.item_id for t in threads if t.item_id}
    leftovers = list(
        session.scalars(
            select(Item)
            .where(Item.fetched_at >= since, Item.score >= 6, Item.status.in_(["scored", "picked"]))
            .order_by(Item.score.desc())
            .limit(15)
        )
    )
    leftovers = [i for i in leftovers if i.id not in discussed and (i.extra or {}).get("source_kind") != "youtube"]
    capped = []
    for i in leftovers:
        if per_topic.get(i.topic_id, 0) < cap + 1:  # "Things happened" lines: a little more room
            per_topic[i.topic_id] = per_topic.get(i.topic_id, 0) + 1
            capped.append(i)
    threads = threads[:12]
    return threads, capped, {k: v for k, v in followups.items() if k in {t.id for t in threads}}


def failing_sources_note(session: Session) -> str:
    """One footer line naming sources whose last fetch failed, so breakage shows up in the inbox."""
    rows = list(session.scalars(
        select(Source).join(Topic)
        .where(Source.active, Topic.active, Source.last_error != "")
        .order_by(Topic.name, Source.name)
    ))
    if not rows:
        return ""

    def reason(err: str) -> str:
        err = re.sub(r"^\w+(Error|Exception):\s*", "", err)  # drop the "SourceError: " prefix
        return err[:70].rstrip() + ("…" if len(err) > 70 else "")

    listed = "; ".join(
        f"{html_lib.escape(src.name or src.kind)} ({html_lib.escape(reason(src.last_error))})" for src in rows[:6]
    )
    more = f" and {len(rows) - 6} more" if len(rows) > 6 else ""
    return (f'<b>{len(rows)} source{"s" if len(rows) != 1 else ""} failing:</b> {listed}{more}. '
            f'<a href="{config.BASE_URL}/topics" style="color:#777;">Check Topics</a>')


def new_videos(session: Session) -> list[Item]:
    """Subscription videos at or above the YouTube threshold, not yet in a newsletter or a thread."""
    from app.triage import threshold_for

    since = utcnow() - timedelta(days=3)
    rows = list(session.scalars(
        select(Item).join(Topic)
        .where(Item.fetched_at >= since, Item.score.is_not(None), Item.status.in_(["scored", "picked"]))
    ))
    rows = [i for i in rows if (i.extra or {}).get("source_kind") == "youtube"
            and not (i.extra or {}).get("newsletter_id") and i.score >= threshold_for(session, i.topic)]
    rows.sort(key=lambda i: ((i.extra or {}).get("channel") or "", i.published_at or i.fetched_at))
    return rows[:30]


def _video_material(videos: list[Item]) -> str:
    return "\n".join(
        f"- {(v.extra or {}).get('channel', '')}: {v.title} ({v.url}) | {(v.body or '')[:300].replace(chr(10), ' ')}"
        + (f" | Image: {images.for_item(v)}" if images.for_item(v) else "")
        for v in videos
    )


def build_daily(session: Session, edition: str, critical: bool = True) -> Newsletter | None:
    last = session.scalar(
        select(Newsletter).where(Newsletter.kind == "daily", Newsletter.sent_at.is_not(None))
        .order_by(Newsletter.created_at.desc())
    )
    since = last.created_at if last else utcnow() - timedelta(hours=14)
    threads, leftovers, followups = collect(session, since)
    morning = edition == "am"
    saved = reading.for_newsletter(session)
    videos = [] if morning else new_videos(session)  # the brief morning edition leaves these for the evening
    reviews = [] if morning else travel.for_newsletter(session)
    trip_list, trip_items = trips.for_newsletter(session)
    if morning:
        trip_list = [t for t in trip_list if t.start_date and t.start_date <= trips.today() + timedelta(days=7)]
        trip_items = [i for i in trip_items if i.trip_id in {t.id for t in trip_list}]
    race_rows = races.for_newsletter(session, morning)
    review_ids = {r.id for r in reviews}
    leftovers = [i for i in leftovers if i.id not in review_ids]
    if not threads and not leftovers and not saved and not videos and not reviews and not trip_items \
            and not race_rows:
        log.info("nothing new for the newsletter")
        return None
    name = settings_store.get(session, "newsletter_name")
    owner = settings_store.get(session, "owner_name")
    profile = wallet_profile(session)
    now_et = datetime.now(config.TZ)
    recent = list(
        session.scalars(
            select(Newsletter).where(Newsletter.kind == "daily").order_by(Newsletter.created_at.desc()).limit(2)
        )
    )
    previous = "\n\n".join(f"Subject: {n.subject}\n{n.markdown[:1500]}" for n in recent)
    images.fill_missing(session, [t.item for t in threads[:6] if t.item])
    session.commit()
    allowed_images = ({images.for_item(t.item) for t in threads if t.item}
                      | {images.for_item(v) for v in videos}
                      | {images.for_item(r) for r in reviews}) - {""}
    material = "\n\n---\n\n".join(_thread_material(t, covered_until=followups.get(t.id)) for t in threads)
    clicked = clicks.summary(session)
    extras = "\n".join(
        f"- [{i.topic.name}] {i.title} ({i.url}) {i.triage_note}" for i in leftovers
    )
    user = (
        f"Newsletter name: {name}. Edition: {'morning' if edition == 'am' else 'evening'}, "
        f"{now_et:%A %B %d, %Y}.\n{MORNING_FORMAT if morning else EVENING_NOTE}\n\n"
        f"The reader is {owner}. About them:\n{profile}\n\n"
        + (f"Topics the reader has clicked most in the last month (lean toward these when choosing the lead): "
           f"{clicked}\n\n" if clicked else "")
        + f"BOARD THREADS since the last edition (hottest first):\n\n{material}\n\n"
        + (f"OTHER ITEMS that came in (good for 'Things happened'):\n{extras}\n\n" if extras else "")
        + (
            "THE READER'S READING LIST (links they saved to read). Give these their own section, "
            "'## From your reading list', after the main section: one short paragraph per link with "
            "what it says, why it matters to them if it does, the link, and the board's takes when "
            "there's a thread (link it).\n\n" + reading.newsletter_material(session, saved, thread_url,
                                                                            allowed_images) + "\n\n"
            if saved else ""
        )
        + (
            "THE READER'S UPCOMING TRIPS. Give them a section, '## Upcoming trips', placed by importance (right "
            "after the lead if a trip is days away): for each trip the countdown, the best new ideas and events "
            "with links and dates, and, when it's within a week, the flight and booking details as a quick list.\n"
            + trips.newsletter_material(session, trip_list, trip_items) + "\n\n"
            if trip_list else ""
        )
        + (
            "THE READER'S RACE CALENDAR (their target races). Put any date within a week in the actions "
            "('## Today' in the morning, '## Dates' in the evening) with its link. In the evening edition, "
            "a short '## Races' section can cover the rest. Say 'expected' for dates not yet announced.\n"
            + races.newsletter_material(race_rows) + "\n\n"
            if race_rows else ""
        )
        + (
            "NEW ONE MILE AT A TIME REVIEWS FROM PLACES THE READER IS WATCHING. Give them a section, "
            "'## Reviews from your places': one line each with the type (flight, lounge, hotel), the linked "
            "title and the one thing worth knowing from it.\n"
            + travel.newsletter_material(reviews, travel.regions(session)) + "\n\n"
            if reviews else ""
        )
        + (
            "NEW VIDEOS FROM THE READER'S YOUTUBE SUBSCRIPTIONS. List every one under '## New on YouTube', "
            "near the end: one line each with the channel, the linked title, and a few words on what it is "
            "(from the description). Group a channel's videos together. No commentary beyond that unless a "
            "video ties into something else in this edition.\n" + _video_material(videos) + "\n\n"
            if videos else ""
        )
        + (f"YOUR LAST EDITIONS (for running jokes; don't repeat the same items):\n{previous}\n" if previous else "")
    )
    system = f"{STYLE_GUIDE}\n\nWriting rules:\n{style.guide(session)}"
    res = llm.generate(
        purpose="newsletter",
        role="newsletter",
        system=system,
        user=user,
        max_tokens=12000,
        effort="medium",
        schema=_Edition,
        critical=critical,
        fake=lambda: _Edition(
            subject=f"{name}: fake edition",
            preheader="Fake preheader",
            markdown="## Fake essay\n\nSomething happened.[^1]\n\n## Things happened\n\n"
            + "\n".join(f"- [{t.title}]({thread_url(t.id)})" for t in threads)
            + "\n\n[^1]: A footnote.",
        ),
    )
    ed: _Edition = res.parsed
    ed = style.edit(ed, purpose="newsletter", voice=STYLE_GUIDE, critical=critical, session=session)
    ed = style.polish(ed, purpose="newsletter", role="newsletter", system=system, user=user,
                      critical=critical, long_form=True, max_tokens=12000)
    ed, problems = fact_check(session, ed, material=user, critical=critical)
    ed.markdown = keep_known_images(ed.markdown, allowed_images, limit=1 if morning else 4)
    kicker = f"{name} · {now_et:%a %b %-d} · {'Morning' if edition == 'am' else 'Evening'}"
    ask = (f"Reply to this email to ask the board something; the answer comes in the next edition. "
           f"Send links to <b>read@{html_lib.escape(config.INBOUND_DOMAIN)}</b> for your reading list.")
    failing = failing_sources_note(session)
    html = emailer.wrap(ed.subject, emailer.markdown_to_html(ed.markdown), kicker,
                        emailer.footer(ask + (f"<br>{failing}" if failing else "")), ed.preheader)
    key = f"daily:{now_et:%Y-%m-%d}:{edition}"
    existing = session.scalar(select(Newsletter).where(Newsletter.edition_key == key))
    if existing is not None:
        key = f"{key}:{utcnow():%H%M%S}"  # manual re-run on the same day
    nl = Newsletter(kind="daily", edition_key=key, subject=ed.subject, markdown=ed.markdown, html=html,
                    thread_ids=[t.id for t in threads])
    session.add(nl)
    session.flush()
    nl.fact_check_note = (f"fact-check fixed {len(problems)}: " + "; ".join(problems))[:600] if problems else ""
    for t in threads:
        if t.posts:
            session.add(Coverage(newsletter_id=nl.id, thread_id=t.id, last_post_id=max(p.id for p in t.posts)))
    for r in saved:
        r.newsletter_id = nl.id
    for v in videos:
        v.extra = {**(v.extra or {}), "newsletter_id": nl.id}
    for r in reviews:
        r.extra = {**(r.extra or {}), "newsletter_reviews": nl.id}
    for t in trip_items:
        t.newsletter_id = nl.id
    session.commit()
    return nl


class _Check(llm.Lenient):
    problems: list[str] = Field(default_factory=list, description=(
        "Each factual claim in the draft that the material doesn't support or contradicts, with what's wrong. "
        "Empty if every claim checks out."))
    subject: str = Field(default="", description="Corrected subject line, only if it had a problem")
    markdown: str = Field(default="", description="The full corrected newsletter, only if there were problems")


FACT_CHECK = """You fact-check a newsletter against the material it was written from.
Check every factual claim: names, numbers, prices, dates, times, places, who said what, what a link leads to,
what's available and until when. Opinions, jokes, metaphors and obvious comic exaggeration are fine.
A claim is a problem when the material doesn't support it or contradicts it.
If there are problems, return the whole newsletter with only those claims fixed or removed. Change nothing
else: keep the voice, every link, image and footnote. If there are none, return an empty problems list and
leave subject and markdown empty."""


def fact_check(session: Session, ed: _Edition, *, material: str, critical: bool = True) -> tuple[_Edition, list[str]]:
    """Compare the draft's claims with the material; fix unsupported ones. Returns (edition, problems)."""
    try:
        res = llm.generate(
            purpose="newsletter_factcheck", role="factcheck", system=FACT_CHECK,
            user=f"THE MATERIAL:\n\n{material}\n\n=====\n\nTHE DRAFT:\nSubject: {ed.subject}\n\n{ed.markdown}",
            max_tokens=12000, effort="medium", schema=_Check, critical=critical, fake=lambda: _Check(),
        )
    except (llm.BudgetExceeded, llm.LlmError) as e:
        log.warning("fact-check skipped: %s", e)
        return ed, []
    check: _Check = res.parsed
    problems = [p for p in check.problems if p.strip()]
    if not problems:
        return ed, []
    log.info("fact-check found %d problems: %s", len(problems), problems)
    fixed = ed.model_copy()
    if check.markdown.strip() and len(check.markdown) > len(ed.markdown) * 0.6:  # not a truncated rewrite
        fixed.markdown = check.markdown
    if check.subject.strip():
        fixed.subject = check.subject.strip()[:150]
    return fixed, problems


def send_newsletter(session: Session, nl: Newsletter, to: list[str] | None = None,
                    scheduled_at: datetime | None = None) -> None:
    # Board links get a share key that works for 24 hours after delivery, without signing in.
    # The stored copy keeps plain links (the admin view is behind sign-in anyway).
    html = share.tokenize_links(nl.html, scheduled_at)
    text = share.tokenize_links(nl.markdown, scheduled_at)
    if nl.kind == "daily":
        html = clicks.wrap_links(html, nl.id)  # clicks steer which topics get picked
    try:
        kwargs = {"reply_to": mailcommands.ask_address()} if nl.kind == "daily" else {}
        nl.resend_id = emailer.send(
            to or emailer.recipients(session), nl.subject, html, text,
            scheduled_at=scheduled_at.isoformat() if scheduled_at else "", **kwargs,
        )
        nl.sent_at = utcnow()
        nl.error = ""
    except emailer.EmailError as e:
        nl.error = str(e)[:2000]
        log.error("send failed: %s", e)
    session.commit()


def send_alert(session: Session, item: Item, decision: AlertDecision, thread: Thread | None) -> Newsletter:
    topic = session.get(Topic, item.topic_id)
    subject = f"🚨 {topic.name}: {decision.headline or item.title}"[:150]
    rows = [
        ("What", decision.what or item.title),
        ("Where", decision.where),
        ("When", decision.when),
        ("Getting there", decision.travel),
    ]
    body = ""
    picture = images.for_item(item)
    if not picture and item.url:
        picture = images.fetch_preview(item.url)
        if picture:
            item.extra = {**(item.extra or {}), "image": picture}
    if picture:
        body += f'<p><img src="{html_lib.escape(picture)}" alt="{html_lib.escape(item.title[:120])}"></p>'
    body += "".join(
        f"<p><b>{html_lib.escape(k)}:</b> {html_lib.escape(v)}</p>" for k, v in rows if v
    )
    if item.url:
        body += f'<p><a href="{html_lib.escape(item.url)}">Original source</a></p>'
    if thread:
        body += f'<p><a href="{thread_url(thread.id)}">What the board is saying</a></p>'
    name = settings_store.get(session, "newsletter_name")
    html = emailer.wrap(subject, body, f"{name} · Alert", emailer.footer())
    text = "\n".join(f"{k}: {v}" for k, v in rows if v) + f"\n{item.url}"
    nl = Newsletter(kind="alert", subject=subject, markdown=text, html=html,
                    thread_ids=[thread.id] if thread else [])
    session.add(nl)
    session.flush()
    send_newsletter(session, nl)
    return nl
