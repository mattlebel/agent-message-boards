"""Admin web app: the board, topics, agents, newsletters, settings."""

from __future__ import annotations

import csv
import html as html_lib
import io
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from urllib.parse import quote

import markdown as md
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import (clicks, config, races, strava, discussion, emailer, ingest, jobs, llm, personas, reading, settings_store, share, style,
                 suggestions, travel, triage, trips, wallet, youtube)
from app.auth import (
    LoginRequired, can_send_link, current_user, email_allowed, make_login_token, peek_login_token,
    redeem_login_token, require_user, require_user_api, safe_next,
)
from app.db import get_session, init_db, session_scope, utcnow
from app.inbound import process_inbound, record_webhook_event
from app.models import (
    Agent, Feedback, InboundEmail, Item, JobRun, LlmCall, Newsletter, Post, ReadingItem, Source, Thread, Topic,
    Race, RaceDate, Trip, TripItem, WalletCard,
    TopicSuggestion, YoutubeChannel,
)
from app.seed import run_migrations, seed
from app.sources import KINDS, LIST_FIELDS, NUMERIC_FIELDS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("web")

@asynccontextmanager
async def lifespan(_app):
    init_db()
    with session_scope() as s:
        seed(s)
        run_migrations(s)
    yield


app = FastAPI(title="Agent Message Board", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    https_only=config.BASE_URL.startswith("https"),
    same_site="lax",
    max_age=60 * 60 * 24 * 30,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.exception_handler(LoginRequired)
async def _login_redirect(request: Request, exc: LoginRequired):
    return RedirectResponse(f"/login?next={quote(str(request.url.path))}", status_code=303)


# --- template helpers ---------------------------------------------------------------


def _et(dt, fmt="%b %-d, %-I:%M %p"):
    if dt is None:
        return ""
    from datetime import timezone

    return dt.replace(tzinfo=timezone.utc).astimezone(config.TZ).strftime(fmt)


def _ago(dt):
    if dt is None:
        return "never"
    secs = (utcnow() - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


_URL_RE = re.compile(r"(?<![\(\[<\"'=])(https?://[^\s<>\)\]]+)")


def _render_post(text: str) -> str:
    safe = html_lib.escape(text or "", quote=False)
    safe = _URL_RE.sub(lambda m: f"[{m.group(1)}]({m.group(1)})", safe)
    out = md.markdown(safe, extensions=["sane_lists", "nl2br"])
    out = re.sub(r"@([a-z0-9_]+)", r'<span class="mention">@\1</span>', out)
    return out.replace("<a ", '<a target="_blank" rel="noopener" ')


templates.env.filters["et"] = _et
templates.env.filters["ago"] = _ago
templates.env.filters["post"] = _render_post
templates.env.globals["config"] = config


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("user", current_user(request))
    ctx.setdefault("flash", request.session.pop("flash", None))
    ctx.setdefault("nav", name.split(".")[0])
    return templates.TemplateResponse(request, name, ctx)


def flash(request: Request, msg: str) -> None:
    request.session["flash"] = msg


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def run_bg(fn, *args) -> None:
    """Background work gets its own DB session."""
    try:
        with session_scope() as s:
            fn(s, *args)
    except Exception:
        log.exception("background task failed")


# --- auth -------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if current_user(request):
        return back(next)
    return render(request, "login.html", next=safe_next(next),
                  email_ready=bool(config.RESEND_API_KEY), sent=False)


@app.post("/auth/email", response_class=HTMLResponse)
def auth_email(request: Request, email: str = Form(...), next: str = Form("/"),
               session: Session = Depends(get_session)):
    email = email.strip().lower()
    nxt = safe_next(next)
    # Same answer whether or not the address is allowed, so the page doesn't reveal who can sign in.
    if email_allowed(email) and can_send_link(session, email):
        token = make_login_token(session, email, nxt)
        session.commit()
        link = f"{config.BASE_URL}/auth/email/verify?token={quote(token)}"
        body = (f'<p>Here is your link to sign in to The Board. It works once and expires in 15 minutes.</p>'
                f'<p><a href="{html_lib.escape(link)}">Sign in to The Board</a></p>')
        try:
            emailer.send([email], "Your sign-in link for The Board",
                         emailer.wrap("Sign in", body, "The Board", "If you didn't ask for this, you can ignore it."),
                         text=f"Sign in to The Board (works once, expires in 15 minutes):\n{link}\n")
        except emailer.EmailError:
            log.exception("couldn't send sign-in link")
    return render(request, "login.html", next=nxt,
                  email_ready=bool(config.RESEND_API_KEY), sent=True)


@app.get("/auth/email/verify", response_class=HTMLResponse)
def auth_email_verify(request: Request, token: str = "", session: Session = Depends(get_session)):
    # Shows a button instead of signing in on GET, so a mail app that pre-opens links can't use it up.
    return render(request, "login_confirm.html", token=token, email=peek_login_token(session, token))


@app.post("/auth/email/verify")
def auth_email_redeem(request: Request, token: str = Form(""), session: Session = Depends(get_session)):
    found = redeem_login_token(session, token)
    if not found:
        return render(request, "login_confirm.html", token="", email=None)
    email, nxt = found
    request.session.clear()
    request.session["user"] = {"email": email, "name": "", "picture": ""}
    return back(nxt)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/login")


@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")


# --- the board ------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def board(request: Request, topic: str = "", page: int = 1, user=Depends(require_user),
          session: Session = Depends(get_session)):
    topics = list(session.scalars(select(Topic).order_by(Topic.priority.desc(), Topic.name)))
    q = select(Thread).order_by(Thread.last_activity_at.desc())
    current = None
    if topic:
        current = next((t for t in topics if t.slug == topic), None)
        if current:
            q = q.where(Thread.topic_id == current.id)
    per = 30
    threads = list(session.scalars(q.offset((page - 1) * per).limit(per + 1)))
    has_more = len(threads) > per
    threads = threads[:per]
    counts = dict(session.execute(
        select(Post.thread_id, func.count(Post.id)).where(Post.thread_id.in_([t.id for t in threads] or [0]))
        .group_by(Post.thread_id)
    ).all())
    since = utcnow() - timedelta(hours=24)
    stats = {
        "spent": llm.spent_today(),
        "budget": settings_store.get(session, "daily_budget_usd"),
        "posts_24h": discussion.count_posts_since(session, since),
        "items_24h": session.scalar(select(func.count(Item.id)).where(Item.fetched_at >= since)) or 0,
        "last_cycle": session.scalar(select(JobRun).where(JobRun.job == "cycle").order_by(JobRun.id.desc())),
        "agents": session.scalar(select(func.count(Agent.id)).where(Agent.active)) or 0,
    }
    return render(request, "board.html", topics=topics, threads=threads, counts=counts, current=current,
                  page=page, has_more=has_more, stats=stats)


@app.get("/r/{key}")
def click_redirect(key: str, session: Session = Depends(get_session)):
    """Newsletter links pass through here: record the click, then go to the real URL."""
    found = clicks.read_key(key)
    if found is None:
        raise HTTPException(404)
    url, newsletter_id = found
    try:
        clicks.record_email_click(session, url, newsletter_id)
    except Exception:
        log.exception("couldn't record click")
        session.rollback()
    return RedirectResponse(url, status_code=302)


@app.get("/t/{thread_id}", response_class=HTMLResponse)
def thread_page(request: Request, thread_id: int, k: str = "", session: Session = Depends(get_session)):
    user = current_user(request)
    if user is None:
        # A share link from an email opens this one thread, read-only, until it expires.
        expires = share.check_token(thread_id, k) if k else None
        if expires is None:
            if k:
                return render(request, "share_expired.html", next=f"/t/{thread_id}", nav="")
            raise LoginRequired()
        thread = session.get(Thread, thread_id)
        if thread is None:
            raise HTTPException(404)
        clicks.record_open(session, thread)
        return render(request, "thread.html", thread=thread, nav="", readonly=True,
                      share_expires=expires.replace(tzinfo=None))
    thread = session.get(Thread, thread_id)
    if thread is None:
        raise HTTPException(404)
    clicks.record_open(session, thread)
    return render(request, "thread.html", thread=thread, nav="board", readonly=False)


@app.get("/t/{thread_id}/count")
def thread_count(thread_id: int, user=Depends(require_user_api), session: Session = Depends(get_session)):
    n = session.scalar(select(func.count(Post.id)).where(Post.thread_id == thread_id)) or 0
    return {"count": n}


@app.post("/t/{thread_id}/reply")
def thread_reply(request: Request, background: BackgroundTasks, thread_id: int, body: str = Form(...),
                 user=Depends(require_user), session: Session = Depends(get_session)):
    thread = session.get(Thread, thread_id)
    if thread is None or not body.strip():
        raise HTTPException(400)
    thread.posts.append(Post(agent_id=None, body=body.strip()))
    thread.last_activity_at = utcnow()
    session.commit()
    background.add_task(run_bg, discussion.respond_to_owner, thread_id)
    return back(f"/t/{thread_id}?waiting=1#bottom")


@app.post("/threads")
def new_thread(request: Request, background: BackgroundTasks, topic_id: int = Form(...), title: str = Form(...),
               body: str = Form(...), user=Depends(require_user), session: Session = Depends(get_session)):
    thread = Thread(topic_id=topic_id, title=title.strip()[:200], by_user=True)
    session.add(thread)
    session.flush()
    thread.posts.append(Post(agent_id=None, body=body.strip()))
    session.commit()
    background.add_task(run_bg, discussion.respond_to_owner, thread.id)
    return back(f"/t/{thread.id}?waiting=1#bottom")


class FeedbackIn(BaseModel):
    post_id: int
    value: int


class CommentIn(BaseModel):
    comment: str


@app.post("/api/feedback")
def api_feedback(data: FeedbackIn, user=Depends(require_user_api), session: Session = Depends(get_session)):
    post = session.get(Post, data.post_id)
    if post is None or post.agent_id is None or data.value not in (1, -1):
        raise HTTPException(400)
    fb = post.feedback
    if fb is None:
        fb = Feedback(post_id=post.id, agent_id=post.agent_id, value=data.value)
        session.add(fb)
    else:
        fb.value = data.value
        fb.updated_at = utcnow()
    session.commit()
    return {"id": fb.id, "value": fb.value, "comment": fb.comment}


@app.post("/api/feedback/{feedback_id}/comment")
def api_feedback_comment(feedback_id: int, data: CommentIn, user=Depends(require_user_api),
                         session: Session = Depends(get_session)):
    fb = session.get(Feedback, feedback_id)
    if fb is None:
        raise HTTPException(404)
    fb.comment = data.comment.strip()[:2000]
    fb.updated_at = utcnow()
    session.commit()
    return {"ok": True}


@app.delete("/api/feedback/{feedback_id}")
def api_feedback_delete(feedback_id: int, user=Depends(require_user_api), session: Session = Depends(get_session)):
    fb = session.get(Feedback, feedback_id)
    if fb:
        session.delete(fb)
        session.commit()
    return {"ok": True}


# --- topics & sources ------------------------------------------------------------------


@app.get("/topics", response_class=HTMLResponse)
def topics_page(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    topics = list(session.scalars(select(Topic).order_by(Topic.active.desc(), Topic.priority.desc(), Topic.name)))
    agents = list(session.scalars(select(Agent).where(Agent.active)))
    return render(request, "topics.html", topics=topics, agents=agents)


def _assign_topic_bg(session: Session, topic_id: int) -> None:
    topic = session.get(Topic, topic_id)
    if topic:
        personas.assign_topic(session, topic)


@app.post("/topics")
def create_topic(request: Request, background: BackgroundTasks, name: str = Form(...), description: str = Form(""),
                 priority: int = Form(3), urgent: bool = Form(False), urgent_criteria: str = Form(""),
                 user=Depends(require_user), session: Session = Depends(get_session)):
    slug, n = suggestions.slugify(name), 2
    base = slug
    while session.scalar(select(Topic.id).where(Topic.slug == slug)):
        slug, n = f"{base}-{n}", n + 1
    topic = Topic(slug=slug, name=name.strip(), description=description.strip(), priority=priority,
                  urgent=urgent, urgent_criteria=urgent_criteria.strip())
    session.add(topic)
    session.commit()
    background.add_task(run_bg, _assign_topic_bg, topic.id)
    flash(request, f"Added “{topic.name}”. Now add a source or two.")
    return back(f"/topics/{topic.id}")


@app.get("/topics/{topic_id}", response_class=HTMLResponse)
def topic_page(request: Request, topic_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(404)
    items = list(session.scalars(select(Item).where(Item.topic_id == topic.id).order_by(Item.id.desc()).limit(40)))
    agents = [a for a in session.scalars(select(Agent).where(Agent.active)) if topic.slug in (a.interests or [])]
    return render(request, "topic.html", topic=topic, items=items, kinds=KINDS, agents=agents, nav="topics",
                  kinds_json=json.dumps(KINDS), threshold=triage.threshold_for(session, topic))


@app.post("/topics/{topic_id}")
def update_topic(request: Request, topic_id: int, name: str = Form(...), description: str = Form(""),
                 priority: int = Form(3), urgent: bool = Form(False), urgent_criteria: str = Form(""),
                 active: bool = Form(False), threshold: str = Form(""), user=Depends(require_user),
                 session: Session = Depends(get_session)):
    topic = session.get(Topic, topic_id)
    topic.name, topic.description, topic.priority = name.strip(), description.strip(), priority
    topic.urgent, topic.urgent_criteria, topic.active = urgent, urgent_criteria.strip(), active
    thresholds = dict(settings_store.get(session, "topic_thresholds") or {})
    try:
        value = float(threshold) if threshold.strip() else None
    except ValueError:
        value = None
    if value is None:
        thresholds.pop(topic.slug, None)  # back to the default (YouTube's default is 5, others 7)
    else:
        thresholds[topic.slug] = max(0.0, min(10.0, value))  # stored as entered, even if it's 7
    settings_store.set(session, "topic_thresholds", thresholds)
    session.commit()
    flash(request, "Saved.")
    return back(f"/topics/{topic_id}")


@app.post("/topics/{topic_id}/toggle")
def toggle_topic(request: Request, topic_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    topic = session.get(Topic, topic_id)
    topic.active = not topic.active
    session.commit()
    flash(request, f"{'Resumed' if topic.active else 'Paused'} “{topic.name}”.")
    return back("/topics")


@app.post("/topics/{topic_id}/delete")
def delete_topic(request: Request, topic_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    topic = session.get(Topic, topic_id)
    if topic:
        for a in session.scalars(select(Agent)):
            if topic.slug in (a.interests or []):
                a.interests = [s for s in a.interests if s != topic.slug]
        session.delete(topic)
        session.commit()
        flash(request, f"Removed “{topic.name}”. Its threads stay on the board.")
    return back("/topics")


def _source_config_from_form(kind: str, form) -> dict:
    cfg = {}
    for field in KINDS[kind]["fields"]:
        val = (form.get(f"cfg_{field}") or "").strip()
        if not val:
            continue
        if field in NUMERIC_FIELDS:
            try:
                cfg[field] = float(val) if "." in val else int(val)
            except ValueError:
                continue
        elif field in LIST_FIELDS:
            cfg[field] = [v.strip() for v in val.split(",") if v.strip()]
        else:
            cfg[field] = val
    return cfg


@app.post("/topics/{topic_id}/sources")
async def add_source(request: Request, topic_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    form = await request.form()
    kind = form.get("kind")
    if kind not in KINDS:
        raise HTTPException(400, "unknown kind")
    interval = (form.get("interval_minutes") or "").strip()
    src = Source(topic_id=topic_id, kind=kind, name=(form.get("name") or KINDS[kind]["label"]).strip(),
                 config=_source_config_from_form(kind, form), interval_minutes=int(interval) if interval else None)
    session.add(src)
    session.commit()
    flash(request, "Source added. Use “Fetch now” to test it.")
    return back(f"/topics/{topic_id}")


@app.post("/sources/{source_id}/toggle")
def toggle_source(source_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    src = session.get(Source, source_id)
    src.active = not src.active
    session.commit()
    return back(f"/topics/{src.topic_id}")


@app.post("/sources/{source_id}/delete")
def delete_source(source_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    src = session.get(Source, source_id)
    topic_id = src.topic_id
    session.delete(src)
    session.commit()
    return back(f"/topics/{topic_id}")


@app.post("/sources/{source_id}/fetch")
def fetch_source(request: Request, background: BackgroundTasks, source_id: int, user=Depends(require_user),
                 session: Session = Depends(get_session)):
    src = session.get(Source, source_id)
    new = ingest.fetch_one(session, src)
    if new and src.topic.urgent:
        background.add_task(jobs.alerts_after_manual_fetch)
    if src.last_error:
        flash(request, f"Fetch failed: {src.last_error}")
    else:
        note = " (first run of a page/sitemap/shop watcher just saves a baseline)" if src.kind in (
            "page_watch", "sitemap", "shopify") and not new else ""
        flash(request, f"Fetched {src.name}: {len(new)} new items{note}.")
    return back(f"/topics/{src.topic_id}")


# --- agents ------------------------------------------------------------------------------


@app.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    agents = list(session.scalars(select(Agent).order_by(Agent.active.desc(), Agent.id)))
    stats = {}
    for agent_id, value, n in session.execute(
        select(Feedback.agent_id, Feedback.value, func.count(Feedback.id)).group_by(Feedback.agent_id, Feedback.value)
    ):
        stats.setdefault(agent_id, {"up": 0, "down": 0})["up" if value > 0 else "down"] = n
    posts = dict(session.execute(select(Post.agent_id, func.count(Post.id)).group_by(Post.agent_id)).all())
    topics = {t.slug: t.name for t in session.scalars(select(Topic))}
    return render(request, "agents.html", agents=agents, stats=stats, posts=posts, topics=topics)


@app.get("/agents/{handle}", response_class=HTMLResponse)
def agent_page(request: Request, handle: str, user=Depends(require_user), session: Session = Depends(get_session)):
    agent = session.scalar(select(Agent).where(Agent.handle == handle))
    if agent is None:
        raise HTTPException(404)
    posts = list(session.scalars(select(Post).where(Post.agent_id == agent.id).order_by(Post.id.desc()).limit(30)))
    topics = list(session.scalars(select(Topic).order_by(Topic.name)))
    return render(request, "agent.html", agent=agent, posts=posts, topics=topics, nav="agents")


@app.post("/agents/create")
def create_agents(request: Request, background: BackgroundTasks, count: int = Form(1),
                  user=Depends(require_user), session: Session = Depends(get_session)):
    active = session.scalar(select(func.count(Agent.id)).where(Agent.active)) or 0
    if active < 4:
        background.add_task(jobs.run_named, "bootstrap")
        flash(request, "Filling out the cast to 4. Each agent writes its own persona; refresh in a few minutes.")
    else:
        for _ in range(max(1, min(count, 3))):
            background.add_task(run_bg, lambda s: personas.create_persona(s, personas.coverage_gaps(s) or None))
        flash(request, "A new member is creating their persona. Refresh in a minute.")
    return back("/agents")


@app.post("/agents/{agent_id:int}")
async def update_agent(request: Request, agent_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    form = await request.form()
    agent = session.get(Agent, agent_id)
    agent.interests = form.getlist("interests")
    agent.steering_notes = (form.get("steering_notes") or "").strip()
    agent.active = form.get("active") == "on"
    session.commit()
    flash(request, "Saved.")
    return back(f"/agents/{agent.handle}")


# --- newsletters --------------------------------------------------------------------------


@app.get("/newsletters", response_class=HTMLResponse)
def newsletters_page(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    rows = list(session.scalars(select(Newsletter).order_by(Newsletter.id.desc()).limit(60)))
    return render(request, "newsletters.html", rows=rows)


@app.get("/newsletters/{nl_id}", response_class=HTMLResponse)
def newsletter_page(request: Request, nl_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    nl = session.get(Newsletter, nl_id)
    if nl is None:
        raise HTTPException(404)
    return render(request, "newsletter.html", nl=nl, nav="newsletters")


@app.get("/newsletters/{nl_id}/raw", response_class=HTMLResponse)
def newsletter_raw(nl_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    nl = session.get(Newsletter, nl_id)
    return HTMLResponse(nl.html if nl else "")


@app.post("/newsletters/{nl_id}/send")
def newsletter_send(request: Request, nl_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    from app.newsletter import send_newsletter

    nl = session.get(Newsletter, nl_id)
    send_newsletter(session, nl)
    flash(request, f"Sent to {', '.join(emailer.recipients(session))}." if not nl.error else f"Send failed: {nl.error}")
    return back(f"/newsletters/{nl_id}")


# --- reading list ------------------------------------------------------------------------


@app.get("/reading", response_class=HTMLResponse)
def reading_page(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    rows = list(session.scalars(select(ReadingItem).order_by(ReadingItem.id.desc()).limit(60)))
    return render(request, "reading.html", rows=rows)


@app.post("/reading")
def reading_add(request: Request, background: BackgroundTasks, links: str = Form(...), discuss: bool = Form(False),
                instructions: str = Form(""), user=Depends(require_user), session: Session = Depends(get_session)):
    rows = reading.add_links(session, links, discuss, instructions)
    session.commit()
    for r in rows:
        background.add_task(run_bg, reading.process, r.id)
    if not rows:
        flash(request, "No links found. Paste full URLs starting with http:// or https://.")
    else:
        what = "summarized" + (" and posted for the agents" if discuss else "")
        flash(request, f"Saved {len(rows)} link{'s' if len(rows) != 1 else ''}. Being {what}; refresh in a minute.")
    return back("/reading")


@app.post("/reading/{row_id}/retry")
def reading_retry(background: BackgroundTasks, row_id: int, user=Depends(require_user),
                  session: Session = Depends(get_session)):
    row = session.get(ReadingItem, row_id)
    if row and row.status == "error":
        row.status, row.error = "queued", ""
        session.commit()
        background.add_task(run_bg, reading.process, row.id)
    return back("/reading")


@app.post("/reading/{row_id}/delete")
def reading_delete(row_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    row = session.get(ReadingItem, row_id)
    if row:
        session.delete(row)
        session.commit()
    return back("/reading")


# --- wallet (card eligibility) --------------------------------------------------------------


@app.get("/wallet", response_class=HTMLResponse)
def wallet_page(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    cards = list(session.scalars(select(WalletCard).order_by(WalletCard.closed, WalletCard.opened.desc())))
    n, counting, next_drop = wallet.count_524(session)
    since = utcnow() - timedelta(days=14)
    filtered = [i for i in session.scalars(select(Item).where(Item.fetched_at >= since, Item.status == "skipped"))
                if (i.triage_note or "").startswith("Filtered:")][:30]
    return render(request, "wallet.html", cards=cards, n524=n, counting=counting, next_drop=next_drop,
                  rules=wallet.rules_text(session), issuers=wallet.ISSUERS, filtered=filtered,
                  opened_label=wallet.opened_label)


@app.post("/wallet")
def wallet_add(request: Request, name: str = Form(...), issuer: str = Form(""), opened: str = Form(""),
               business: bool = Form(False), counts_524: str = Form("auto"), user=Depends(require_user),
               session: Session = Depends(get_session)):
    from datetime import datetime as _dt

    when = None
    if re.fullmatch(r"\d{4}-\d{2}", opened.strip()):
        when = _dt.strptime(opened.strip() + "-01", "%Y-%m-%d")
    if counts_524 == "auto":
        # Personal cards count; business cards only from issuers that report them to personal credit.
        counts = (not business) or issuer in ("Capital One", "Discover")
    else:
        counts = counts_524 == "yes"
    session.add(WalletCard(name=name.strip()[:200], issuer=issuer, opened=when, business=business, counts_524=counts))
    session.commit()
    flash(request, f"Added {name.strip()}.")
    return back("/wallet")


@app.post("/wallet/rules")
def wallet_rules(request: Request, rules: str = Form(""), user=Depends(require_user),
                 session: Session = Depends(get_session)):
    text = rules.strip()
    settings_store.set(session, "card_rules", "" if text == wallet.DEFAULT_CARD_RULES.strip() else text)
    session.commit()
    flash(request, "Rules saved. They apply from the next cycle.")
    return back("/wallet")


@app.post("/wallet/{card_id}/toggle")
def wallet_toggle(card_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    card = session.get(WalletCard, card_id)
    if card:
        card.closed = not card.closed
        session.commit()
    return back("/wallet")


@app.post("/wallet/{card_id}/delete")
def wallet_delete(card_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    card = session.get(WalletCard, card_id)
    if card:
        session.delete(card)
        session.commit()
    return back("/wallet")


# --- trips (planned travel) ------------------------------------------------------------------


def _date(value: str):
    from datetime import datetime as _dt

    try:
        return _dt.strptime(value.strip(), "%Y-%m-%d") if value and value.strip() else None
    except ValueError:
        return None


@app.get("/trips", response_class=HTMLResponse)
def trips_page(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    all_trips = list(session.scalars(select(Trip).order_by(Trip.start_date)))
    upcoming = trips.upcoming(session)
    past = [t for t in all_trips if t not in upcoming]
    return render(request, "trips.html", upcoming=upcoming, past=past[::-1][:10], unsorted=trips.unsorted(session),
                  countdown=trips.countdown, when_label=trips.when_label, kind_labels=trips.KIND_LABELS)


def _research_bg(session: Session, trip_id: int) -> None:
    trip = session.get(Trip, trip_id)
    if trip and trips.research_due(trip):
        trips.research(session, trip)


# --- races ------------------------------------------------------------------------------


@app.get("/races", response_class=HTMLResponse)
def races_page(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    rows = list(session.scalars(select(Race)))
    order = {"registered": 0, "target": 1, "done": 2, "dropped": 3}
    rows.sort(key=lambda r: (order.get(r.status, 9), r.race_date or datetime.max))
    dates = {r.id: races.dates_for(session, r) for r in rows}
    return render(request, "races.html", races=rows, dates=dates, soon=races.upcoming_dates(session, 45),
                  kinds=races.KINDS, countdown=races.countdown, today=races.today())


def _race_research_bg(session: Session, race_id: int) -> None:
    race = session.get(Race, race_id)
    if race:
        races.research(session, race)


@app.post("/races")
def races_add(request: Request, background: BackgroundTasks, name: str = Form(...), kind: str = Form("other"),
              race_date: str = Form(""), location: str = Form(""), url: str = Form(""), notes: str = Form(""),
              user=Depends(require_user), session: Session = Depends(get_session)):
    race = Race(name=name.strip()[:200], kind=kind if kind in ("major", "mile", "other") else "other",
                race_date=_date(race_date), location=location.strip()[:200],
                url=url.strip() if url.strip().startswith("http") else "", notes=notes.strip())
    session.add(race)
    session.commit()
    background.add_task(run_bg, _race_research_bg, race.id)
    flash(request, f"Added {race.name}. Looking up its registration dates now; refresh in a minute.")
    return back(f"/races#race-{race.id}")


@app.post("/races/{race_id}")
def race_update(request: Request, race_id: int, status: str = Form("target"), notes: str = Form(""),
                race_date: str = Form(""), user=Depends(require_user), session: Session = Depends(get_session)):
    race = session.get(Race, race_id)
    if race is None:
        raise HTTPException(404)
    if status in ("target", "registered", "done", "dropped"):
        race.status = status
    race.notes = notes.strip()
    race.race_date = _date(race_date) or race.race_date
    session.commit()
    flash(request, "Saved.")
    return back(f"/races#race-{race_id}")


@app.post("/races/{race_id}/research")
def race_research_now(request: Request, background: BackgroundTasks, race_id: int, user=Depends(require_user)):
    background.add_task(run_bg, _race_research_bg, race_id)
    flash(request, "Researching. Refresh in a minute.")
    return back(f"/races#race-{race_id}")


@app.post("/races/{race_id}/dates")
def race_add_date(request: Request, race_id: int, date: str = Form(...), kind: str = Form("other"),
                  label: str = Form(""), url: str = Form(""), user=Depends(require_user),
                  session: Session = Depends(get_session)):
    when = _date(date)
    if when is None:
        flash(request, "That date didn't parse.")
        return back(f"/races#race-{race_id}")
    kind = kind if kind in races.KINDS else "other"
    session.add(RaceDate(race_id=race_id, kind=kind, label=label.strip()[:300] or races.KINDS[kind], date=when,
                         confirmed=True, url=url.strip() if url.strip().startswith("http") else "", source="manual"))
    session.commit()
    return back(f"/races#race-{race_id}")


@app.post("/race-dates/{date_id}/delete")
def race_date_delete(date_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    d = session.get(RaceDate, date_id)
    race_id = d.race_id if d else 0
    if d:
        session.delete(d)
        session.commit()
    return back(f"/races#race-{race_id}")


@app.post("/races/{race_id}/delete")
def race_delete(request: Request, race_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    race = session.get(Race, race_id)
    if race:
        for d in races.dates_for(session, race):
            session.delete(d)
        session.delete(race)
        session.commit()
        flash(request, f"Removed {race.name}.")
    return back("/races")


@app.post("/trips")
def trips_add(request: Request, background: BackgroundTasks, name: str = Form(...), destination: str = Form(""),
              start_date: str = Form(""), end_date: str = Form(""), flights: str = Form(""), notes: str = Form(""),
              user=Depends(require_user), session: Session = Depends(get_session)):
    trip = Trip(name=name.strip()[:200], destination=destination.strip()[:300], start_date=_date(start_date),
                end_date=_date(end_date) or _date(start_date), flights=flights.strip(), notes=notes.strip())
    session.add(trip)
    session.commit()
    background.add_task(run_bg, _research_bg, trip.id)
    flash(request, f"Added {trip.name}. Looking for ideas and events now; refresh in a minute or two.")
    return back(f"/trips/{trip.id}")


@app.get("/trips/{trip_id}", response_class=HTMLResponse)
def trip_page(request: Request, trip_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(404)
    reviews = []
    if trip.destination:
        wanted = [trip.destination] + [p.strip() for p in trip.destination.split(",") if p.strip()]
        reviews = [i for i in session.scalars(select(Item)) if travel.is_omaat(i) and travel.matched_regions(i, wanted)][:12]
    return render(request, "trip.html", trip=trip, items=trips.items_for(session, trip), reviews=reviews,
                  countdown=trips.countdown, when_label=trips.when_label, kind_labels=trips.KIND_LABELS,
                  nav="trips")


@app.post("/trips/{trip_id}")
def trip_update(request: Request, trip_id: int, name: str = Form(...), destination: str = Form(""),
                start_date: str = Form(""), end_date: str = Form(""), flights: str = Form(""), notes: str = Form(""),
                user=Depends(require_user), session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    trip.name, trip.destination = name.strip()[:200], destination.strip()[:300]
    trip.start_date, trip.end_date = _date(start_date), _date(end_date) or _date(start_date)
    trip.flights, trip.notes = flights.strip(), notes.strip()
    session.commit()
    flash(request, "Saved.")
    return back(f"/trips/{trip_id}")


@app.post("/trips/{trip_id}/research")
def trip_research_now(request: Request, background: BackgroundTasks, trip_id: int, user=Depends(require_user),
                      session: Session = Depends(get_session)):
    background.add_task(run_bg, lambda s: trips.research(s, s.get(Trip, trip_id)))
    flash(request, "Looking for ideas and events; refresh in a minute or two.")
    return back(f"/trips/{trip_id}")


@app.post("/trips/{trip_id}/note")
def trip_note(trip_id: int, title: str = Form(...), url: str = Form(""), user=Depends(require_user),
              session: Session = Depends(get_session)):
    session.add(TripItem(trip_id=trip_id, kind="note", title=title.strip()[:500],
                         url=url.strip() if url.strip().startswith("http") else "", source="manual"))
    session.commit()
    return back(f"/trips/{trip_id}")


@app.post("/trips/{trip_id}/delete")
def trip_delete(request: Request, trip_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    trip = session.get(Trip, trip_id)
    if trip:
        for it in session.scalars(select(TripItem).where(TripItem.trip_id == trip_id)):
            session.delete(it)
        session.delete(trip)
        session.commit()
        flash(request, f"Deleted {trip.name}.")
    return back("/trips")


@app.post("/trip-items/{item_id}/assign")
def trip_item_assign(item_id: int, trip_id: int = Form(...), user=Depends(require_user),
                     session: Session = Depends(get_session)):
    item = session.get(TripItem, item_id)
    if item and session.get(Trip, trip_id):
        item.trip_id = trip_id
        session.commit()
    return back("/trips")


@app.post("/trip-items/{item_id}/dismiss")
def trip_item_dismiss(request: Request, item_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    item = session.get(TripItem, item_id)
    if item:
        item.dismissed = True
        session.commit()
    return back(request.headers.get("referer") or "/trips")


# --- travel (trip reports by region) --------------------------------------------------------


@app.get("/travel", response_class=HTMLResponse)
def travel_page(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    return render(request, "travel.html", regions=travel.regions(session), data=travel.page_data(session),
                  types=travel.TYPES)


@app.post("/travel")
def travel_save(request: Request, background: BackgroundTasks, regions: str = Form(""),
                user=Depends(require_user), session: Session = Depends(get_session)):
    wanted = list(dict.fromkeys(r.strip() for r in re.split(r"[\n,]+", regions) if r.strip()))[:30]
    settings_store.set(session, "travel_regions", wanted)
    session.commit()
    background.add_task(run_bg, lambda s: (travel.classify_new(s), travel.backfill_new_regions(s)))
    flash(request, "Saved. Searching One Mile at a Time for reviews from new regions; refresh in a minute or two.")
    return back("/travel")


# --- suggestions -------------------------------------------------------------------------


@app.get("/suggestions", response_class=HTMLResponse)
def suggestions_page(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    rows = list(session.scalars(select(TopicSuggestion).order_by(TopicSuggestion.id.desc()).limit(80)))
    return render(request, "suggestions.html", rows=rows)


@app.get("/s/{token}/add")
def suggestion_one_click(request: Request, background: BackgroundTasks, token: str, user=Depends(require_user),
                         session: Session = Depends(get_session)):
    sug = session.scalar(select(TopicSuggestion).where(TopicSuggestion.token == token))
    if sug is None:
        raise HTTPException(404)
    fresh = sug.status != "added"
    topic = suggestions.add_from_suggestion(session, sug)
    if fresh:
        background.add_task(run_bg, suggestions.first_fetch, topic.id)
    flash(request, f"Added “{topic.name}”. Its sources are being fetched now; refresh to see what came in.")
    return back(f"/topics/{topic.id}")


@app.post("/suggestions/{sug_id}/add")
def suggestion_add(request: Request, background: BackgroundTasks, sug_id: int, user=Depends(require_user),
                   session: Session = Depends(get_session)):
    sug = session.get(TopicSuggestion, sug_id)
    fresh = sug.status != "added"
    topic = suggestions.add_from_suggestion(session, sug)
    if fresh:
        background.add_task(run_bg, suggestions.first_fetch, topic.id)
    flash(request, f"Added “{topic.name}”. Fetching its sources now.")
    return back("/suggestions")


@app.post("/suggestions/{sug_id}/dismiss")
def suggestion_dismiss(sug_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    sug = session.get(TopicSuggestion, sug_id)
    if sug.status == "pending":
        sug.status = "dismissed"
        session.commit()
    return back("/suggestions")


# --- inbox (forwarded newsletters) --------------------------------------------------------


@app.get("/inbox", response_class=HTMLResponse)
def inbox_page(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    rows = list(session.scalars(select(InboundEmail).order_by(InboundEmail.id.desc()).limit(60)))
    topics = list(session.scalars(select(Topic).where(Topic.active).order_by(Topic.name)))
    topic_names = {t.id: t.name for t in session.scalars(select(Topic))}
    return render(request, "inbox.html", rows=rows, topics=topics, topic_names=topic_names)


@app.post("/webhooks/resend")
async def resend_webhook(request: Request, background: BackgroundTasks):
    payload = (await request.body()).decode("utf-8")
    if config.RESEND_WEBHOOK_SECRET:
        import resend

        try:
            event = resend.Webhooks.verify({
                "payload": payload,
                "headers": {
                    "id": request.headers.get("svix-id", ""),
                    "timestamp": request.headers.get("svix-timestamp", ""),
                    "signature": request.headers.get("svix-signature", ""),
                },
                "webhook_secret": config.RESEND_WEBHOOK_SECRET,
            })
        except ValueError as e:
            log.warning("bad webhook signature: %s", e)
            raise HTTPException(401, "bad signature")
    elif config.DEV_MODE:
        event = json.loads(payload)
    else:
        raise HTTPException(503, "RESEND_WEBHOOK_SECRET is not set")
    with session_scope() as s:
        row = record_webhook_event(s, dict(event))
        row_id = row.id if row is not None and row.status == "new" else None
    if row_id:
        background.add_task(run_bg, lambda s: process_inbound(s, s.get(InboundEmail, row_id)))
    return {"ok": True}


# --- YouTube subscriptions ----------------------------------------------------------------


@app.get("/youtube", response_class=HTMLResponse)
def youtube_page(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    channels = list(session.scalars(select(YoutubeChannel).order_by(YoutubeChannel.title)))
    return render(request, "youtube.html", channels=channels,
                  unresolved=settings_store.get(session, "youtube_unresolved") or [])


def _save_channels(session: Session, rows: list[tuple[str, str]], via: str) -> int:
    added = 0
    for cid, title in rows:
        if not cid.startswith("UC"):
            continue
        ch = session.scalar(select(YoutubeChannel).where(YoutubeChannel.channel_id == cid))
        if ch is None:
            session.add(YoutubeChannel(channel_id=cid, title=title[:300], added_via=via))
            added += 1
        elif title and not ch.title:
            ch.title = title[:300]
    session.commit()
    return added


@app.post("/youtube/csv")
async def youtube_csv(request: Request, file: UploadFile, user=Depends(require_user), session: Session = Depends(get_session)):
    text = (await file.read()).decode("utf-8", errors="replace")
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        rec = {k.strip().lower(): (v or "").strip() for k, v in rec.items() if k}
        rows.append((rec.get("channel id", ""), rec.get("channel title", "")))
    added = _save_channels(session, rows, "csv")
    flash(request, f"Imported {added} channels from the CSV.")
    return back("/youtube")


@app.post("/youtube/add")
def youtube_add(request: Request, background: BackgroundTasks, value: str = Form(...),
                user=Depends(require_user), session: Session = Depends(get_session)):
    values = youtube.parse_channels(value)[:300]
    if not values:
        flash(request, "No channels found. Paste @handles, channel URLs or UC… ids.")
    elif len(values) == 1:
        added, missing = youtube.add_many(session, values)
        flash(request, "Channel added." if added else ("Already added." if not missing else
              f"Couldn't find {missing[0]} on YouTube. Check the spelling, or paste the channel URL."))
    else:
        background.add_task(run_bg, youtube.import_in_background, values)
        flash(request, f"Looking up {len(values)} channels. Refresh in a minute; any that couldn't be found "
                       "will be listed below.")
    return back("/youtube")


@app.post("/youtube/{ch_id}/delete")
def youtube_delete(ch_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    ch = session.get(YoutubeChannel, ch_id)
    if ch:
        session.delete(ch)
        session.commit()
    return back("/youtube")


# --- settings & jobs -----------------------------------------------------------------------

MODEL_CHOICES = list(llm.PRICES)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    keys = ["profile", "owner_name", "newsletter_name", "models", "daily_budget_usd", "threads_per_cycle",
            "max_threads_per_topic", "min_replies", "max_replies", "schedule", "recipients", "paused"]
    values = {k: settings_store.get(session, k) for k in keys}
    values["style_guide"] = style.guide(session)
    week_ago = utcnow() - timedelta(days=7)
    by_purpose = list(session.execute(
        select(LlmCall.purpose, func.count(LlmCall.id), func.sum(LlmCall.cost_usd))
        .where(LlmCall.ts >= week_ago).group_by(LlmCall.purpose).order_by(func.sum(LlmCall.cost_usd).desc())
    ))
    by_day = {}
    for ts, cost in session.execute(select(LlmCall.ts, LlmCall.cost_usd).where(LlmCall.ts >= week_ago)):
        day = _et(ts, "%a %b %-d")
        by_day[day] = by_day.get(day, 0) + (cost or 0)
    runs = list(session.scalars(select(JobRun).order_by(JobRun.id.desc()).limit(25)))
    env = {
        "Anthropic API key": bool(config.ANTHROPIC_API_KEY),
        "Resend API key": bool(config.RESEND_API_KEY),
        "Resend webhook secret": bool(config.RESEND_WEBHOOK_SECRET),
        "Email sign-in links (Resend)": bool(config.RESEND_API_KEY),
        "eBay API": bool(config.EBAY_CLIENT_ID),
        "Strava app (STRAVA_CLIENT_ID / SECRET)": strava.configured(),
    }
    strava_info = {"configured": strava.configured(), "connected": strava.connected(session),
                   "athlete": (settings_store.get(session, "strava") or {}).get("athlete", ""),
                   "summary": settings_store.get(session, "strava_summary") or ""}
    return render(request, "settings.html", values=values, models=MODEL_CHOICES, by_purpose=by_purpose,
                  by_day=by_day, runs=runs, env=env, spent=llm.spent_today(), strava=strava_info)


@app.get("/strava/connect")
def strava_connect(user=Depends(require_user)):
    if not strava.configured():
        raise HTTPException(400, "Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET on Render first.")
    return RedirectResponse(strava.authorize_url(), status_code=303)


@app.get("/strava/callback")
def strava_callback(request: Request, code: str = "", scope: str = "", state: str = "", error: str = "",
                    user=Depends(require_user), session: Session = Depends(get_session)):
    if error or not code:
        flash(request, f"Strava wasn't connected ({error or 'no code'}).")
        return back("/settings#strava")
    if not strava.state_ok(state):
        flash(request, "That Strava link expired. Try Connect again.")
        return back("/settings#strava")
    try:
        strava.exchange_code(session, code, scope)
        summary = strava.refresh_summary(session)
    except Exception as e:  # show any failure on the page
        log.exception("Strava connect failed")
        flash(request, f"Strava: {e}")
        return back("/settings#strava")
    flash(request, "Strava connected." + ("" if summary else " No runs in the last six weeks yet."))
    return back("/settings#strava")


@app.post("/strava/disconnect")
def strava_disconnect(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    strava.disconnect(session)
    flash(request, "Strava disconnected and its tokens deleted.")
    return back("/settings#strava")


@app.post("/strava/refresh")
def strava_refresh(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    flash(request, "Training summary refreshed." if strava.refresh_summary(session) else "No runs found.")
    return back("/settings#strava")


@app.post("/settings")
async def save_settings(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    form = await request.form()
    settings_store.set(session, "profile", (form.get("profile") or "").strip())
    guide_text = (form.get("style_guide") or "").strip()
    # Store blank when it matches the built-in rules, so future improvements to them still apply.
    settings_store.set(session, "style_guide",
                       "" if guide_text == style.DEFAULT_STYLE_GUIDE.strip() else guide_text)
    for key in ("owner_name", "newsletter_name"):
        settings_store.set(session, key, (form.get(key) or "").strip() or settings_store.DEFAULTS[key])
    settings_store.set(session, "models", {
        role: form.get(f"model_{role}") for role in settings_store.DEFAULTS["models"]
        if form.get(f"model_{role}") in MODEL_CHOICES
    })
    try:
        settings_store.set(session, "daily_budget_usd", float(form.get("daily_budget_usd") or 3))
        for key in ("threads_per_cycle", "max_threads_per_topic", "min_replies", "max_replies"):
            settings_store.set(session, key, int(form.get(key) or settings_store.DEFAULTS[key]))
    except ValueError:
        flash(request, "Numbers please.")
        return back("/settings")
    schedule = {}
    for key in settings_store.DEFAULTS["schedule"]:
        val = (form.get(f"sched_{key}") or "").strip()
        if key == "weekly_weekday":
            schedule[key] = int(val or 6)
        elif re.fullmatch(r"\d{1,2}:\d{2}", val):
            schedule[key] = val.zfill(5)
    settings_store.set(session, "schedule", schedule)
    recips = [r.strip() for r in re.split(r"[,\s]+", form.get("recipients") or "") if "@" in r]
    settings_store.set(session, "recipients", recips)
    settings_store.set(session, "paused", form.get("paused") == "on")
    session.commit()
    flash(request, "Settings saved.")
    return back("/settings")


@app.post("/jobs/{name}")
def run_job_now(request: Request, background: BackgroundTasks, name: str, edition: str = Form("pm"),
                user=Depends(require_user)):
    if name not in {"cycle", "hot", "newsletter", "weekly", "triage"}:
        raise HTTPException(404)
    args = (edition,) if name == "newsletter" else ()
    background.add_task(jobs.run_named, name, *args)
    labels = {"cycle": "Fetching, triaging and discussing", "hot": "Checking urgent sources",
              "newsletter": "Writing and sending the newsletter", "weekly": "Finding new topic ideas",
              "triage": "Scoring new items"}
    flash(request, f"{labels[name]} in the background. Check back in a few minutes (see “Recent jobs” below).")
    return back(request.headers.get("referer") or "/settings")
