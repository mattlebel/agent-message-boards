"""Admin web app: the board, topics, agents, newsletters, settings."""

from __future__ import annotations

import csv
import html as html_lib
import io
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import quote

import httpx
import markdown as md
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import config, discussion, emailer, ingest, jobs, llm, personas, settings_store, suggestions
from app.auth import LoginRequired, current_user, email_allowed, oauth, require_user, require_user_api
from app.db import get_session, init_db, session_scope, utcnow
from app.inbound import process_inbound, record_webhook_event
from app.models import (
    Agent, Feedback, InboundEmail, Item, JobRun, LlmCall, Newsletter, Post, Source, Thread, Topic,
    TopicSuggestion, YoutubeChannel,
)
from app.seed import seed
from app.sources import KINDS, LIST_FIELDS, NUMERIC_FIELDS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("web")

@asynccontextmanager
async def lifespan(_app):
    init_db()
    with session_scope() as s:
        seed(s)
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
    return render(request, "login.html", next=next, google=bool(config.GOOGLE_CLIENT_ID))


@app.get("/auth/google")
async def auth_google(request: Request, next: str = "/"):
    if not config.GOOGLE_CLIENT_ID:
        raise HTTPException(500, "Google sign-in is not configured (GOOGLE_CLIENT_ID).")
    request.session["next"] = next if next.startswith("/") else "/"
    return await oauth.google.authorize_redirect(
        request, f"{config.BASE_URL}/auth/callback", hd=config.ALLOWED_EMAIL_DOMAIN
    )


@app.get("/auth/callback")
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    info = token.get("userinfo") or {}
    email = info.get("email", "")
    if not info.get("email_verified") or not email_allowed(email):
        request.session.clear()
        return HTMLResponse(f"<p>{html_lib.escape(email)} isn't allowed here.</p><p><a href='/login'>Back</a></p>", 403)
    request.session["user"] = {"email": email, "name": info.get("name", ""), "picture": info.get("picture", "")}
    return back(request.session.pop("next", "/"))


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


@app.get("/t/{thread_id}", response_class=HTMLResponse)
def thread_page(request: Request, thread_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    thread = session.get(Thread, thread_id)
    if thread is None:
        raise HTTPException(404)
    return render(request, "thread.html", thread=thread, nav="board")


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
                  kinds_json=json.dumps(KINDS))


@app.post("/topics/{topic_id}")
def update_topic(request: Request, topic_id: int, name: str = Form(...), description: str = Form(""),
                 priority: int = Form(3), urgent: bool = Form(False), urgent_criteria: str = Form(""),
                 active: bool = Form(False), user=Depends(require_user), session: Session = Depends(get_session)):
    topic = session.get(Topic, topic_id)
    topic.name, topic.description, topic.priority = name.strip(), description.strip(), priority
    topic.urgent, topic.urgent_criteria, topic.active = urgent, urgent_criteria.strip(), active
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
def fetch_source(request: Request, source_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    src = session.get(Source, source_id)
    new = ingest.fetch_one(session, src)
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


@app.post("/agents/{agent_id}")
async def update_agent(request: Request, agent_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    form = await request.form()
    agent = session.get(Agent, agent_id)
    agent.interests = form.getlist("interests")
    agent.steering_notes = (form.get("steering_notes") or "").strip()
    agent.active = form.get("active") == "on"
    session.commit()
    flash(request, "Saved.")
    return back(f"/agents/{agent.handle}")


@app.post("/agents/create")
def create_agents(request: Request, background: BackgroundTasks, count: int = Form(1),
                  user=Depends(require_user), session: Session = Depends(get_session)):
    has_agents = session.scalar(select(Agent.id).limit(1)) is not None
    if not has_agents:
        background.add_task(jobs.run_named, "bootstrap")
        flash(request, "Creating the cast. Each agent writes its own persona; refresh in a few minutes.")
    else:
        for _ in range(max(1, min(count, 3))):
            background.add_task(run_bg, lambda s: personas.create_persona(s, personas.coverage_gaps(s) or None))
        flash(request, "A new member is creating their persona. Refresh in a minute.")
    return back("/agents")


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


# --- suggestions -------------------------------------------------------------------------


@app.get("/suggestions", response_class=HTMLResponse)
def suggestions_page(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    rows = list(session.scalars(select(TopicSuggestion).order_by(TopicSuggestion.id.desc()).limit(80)))
    return render(request, "suggestions.html", rows=rows)


@app.get("/s/{token}/add")
def suggestion_one_click(request: Request, token: str, user=Depends(require_user), session: Session = Depends(get_session)):
    sug = session.scalar(select(TopicSuggestion).where(TopicSuggestion.token == token))
    if sug is None:
        raise HTTPException(404)
    topic = suggestions.add_from_suggestion(session, sug)
    flash(request, f"Added “{topic.name}”. The agents will pick it up next cycle.")
    return back(f"/topics/{topic.id}")


@app.post("/suggestions/{sug_id}/add")
def suggestion_add(request: Request, sug_id: int, user=Depends(require_user), session: Session = Depends(get_session)):
    sug = session.get(TopicSuggestion, sug_id)
    topic = suggestions.add_from_suggestion(session, sug)
    flash(request, f"Added “{topic.name}”.")
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
    return render(request, "youtube.html", channels=channels, google=bool(config.GOOGLE_CLIENT_ID))


@app.get("/youtube/connect")
async def youtube_connect(request: Request, user=Depends(require_user)):
    return await oauth.google_yt.authorize_redirect(
        request, f"{config.BASE_URL}/youtube/callback", prompt="select_account consent"
    )


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


@app.get("/youtube/callback")
async def youtube_callback(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    token = await oauth.google_yt.authorize_access_token(request)
    rows: list[tuple[str, str]] = []
    page_token = None
    async with httpx.AsyncClient(timeout=20) as client:
        for _ in range(40):
            params = {"part": "snippet", "mine": "true", "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            resp = await client.get("https://www.googleapis.com/youtube/v3/subscriptions", params=params,
                                    headers={"Authorization": f"Bearer {token['access_token']}"})
            if resp.status_code != 200:
                flash(request, f"YouTube API error {resp.status_code}: {resp.text[:200]}")
                return back("/youtube")
            data = resp.json()
            for it in data.get("items", []):
                sn = it.get("snippet", {})
                rows.append((sn.get("resourceId", {}).get("channelId", ""), sn.get("title", "")))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    added = _save_channels(session, rows, "oauth")
    flash(request, f"Found {len(rows)} subscriptions, added {added} new channels.")
    return back("/youtube")


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
def youtube_add(request: Request, value: str = Form(...), user=Depends(require_user), session: Session = Depends(get_session)):
    value = value.strip()
    m = re.search(r"(UC[\w-]{20,})", value)
    cid, title = (m.group(1) if m else ""), ""
    if not cid:
        url = value if value.startswith("http") else f"https://www.youtube.com/{value if value.startswith('@') else '@' + value}"
        try:
            page = httpx.get(url, timeout=15, follow_redirects=True, headers={"User-Agent": config.USER_AGENT}).text
            m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{20,})"', page)
            cid = m.group(1) if m else ""
            t = re.search(r"<title>(.*?)</title>", page)
            title = html_lib.unescape(t.group(1).replace(" - YouTube", "")) if t else ""
        except httpx.HTTPError:
            pass
    if not cid:
        flash(request, "Couldn't find a channel ID there. Paste the channel URL or its UC… ID.")
    else:
        _save_channels(session, [(cid, title)], "manual")
        flash(request, "Channel added.")
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
            "min_replies", "max_replies", "schedule", "recipients", "paused"]
    values = {k: settings_store.get(session, k) for k in keys}
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
        "Google sign-in": bool(config.GOOGLE_CLIENT_ID),
        "Reddit API": bool(config.REDDIT_CLIENT_ID),
        "eBay API": bool(config.EBAY_CLIENT_ID),
    }
    return render(request, "settings.html", values=values, models=MODEL_CHOICES, by_purpose=by_purpose,
                  by_day=by_day, runs=runs, env=env, spent=llm.spent_today())


@app.post("/settings")
async def save_settings(request: Request, user=Depends(require_user), session: Session = Depends(get_session)):
    form = await request.form()
    settings_store.set(session, "profile", (form.get("profile") or "").strip())
    for key in ("owner_name", "newsletter_name"):
        settings_store.set(session, key, (form.get(key) or "").strip() or settings_store.DEFAULTS[key])
    settings_store.set(session, "models", {
        role: form.get(f"model_{role}") for role in settings_store.DEFAULTS["models"]
        if form.get(f"model_{role}") in MODEL_CHOICES
    })
    try:
        settings_store.set(session, "daily_budget_usd", float(form.get("daily_budget_usd") or 3))
        for key in ("threads_per_cycle", "min_replies", "max_replies"):
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
