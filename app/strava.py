"""Strava: your recent training, so running items are read against how you're actually running.

Connect once from Settings (OAuth, read-only scopes). Each cycle the last six weeks of activities are
summarized in plain numbers (weekly miles, long runs, races, recent fast efforts) and stored in the
"strava_summary" setting. wallet.profile_text() adds that summary to the profile, so triage, the
agents and the newsletter all see it.

Needs STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET (an API application at strava.com/settings/api whose
Authorization Callback Domain is the app's host). Tokens are kept in the "strava" setting; refresh
tokens are rotated as Strava issues new ones.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app import config, settings_store

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API = "https://www.strava.com/api/v3"
SCOPES = "read,activity:read_all"
METERS_PER_MILE = 1609.344
RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}


class StravaError(Exception):
    pass


def configured() -> bool:
    return bool(config.STRAVA_CLIENT_ID and config.STRAVA_CLIENT_SECRET)


def connected(session: Session) -> bool:
    return bool((settings_store.get(session, "strava") or {}).get("refresh_token"))


def _state_signer() -> URLSafeTimedSerializer:
    from app.share import _secret

    return URLSafeTimedSerializer(_secret(), salt="strava-oauth")


def authorize_url() -> str:
    params = {
        "client_id": config.STRAVA_CLIENT_ID,
        "redirect_uri": f"{config.BASE_URL}/strava/callback",
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": SCOPES,
        "state": _state_signer().dumps("strava"),
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def state_ok(state: str) -> bool:
    try:
        return _state_signer().loads(state, max_age=900) == "strava"
    except BadSignature:
        return False


def _token_request(data: dict) -> dict:
    resp = httpx.post(TOKEN_URL, data={"client_id": config.STRAVA_CLIENT_ID,
                                       "client_secret": config.STRAVA_CLIENT_SECRET, **data}, timeout=20)
    if resp.status_code != 200:
        raise StravaError(f"Strava token request failed: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()


def _save(session: Session, tok: dict, athlete: dict | None = None) -> None:
    current = settings_store.get(session, "strava") or {}
    name = current.get("athlete", "")
    if athlete:
        name = " ".join(x for x in (athlete.get("firstname"), athlete.get("lastname")) if x)
    settings_store.set(session, "strava", {
        "access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
        "expires_at": int(tok["expires_at"]), "athlete": name,
    })
    session.commit()


def exchange_code(session: Session, code: str, scope: str = "") -> None:
    if "activity:read" not in scope:
        raise StravaError("Strava access to activities wasn't granted. Connect again and leave "
                          "'View data about your activities' ticked.")
    tok = _token_request({"code": code, "grant_type": "authorization_code"})
    _save(session, tok, tok.get("athlete"))


def disconnect(session: Session) -> None:
    tok = settings_store.get(session, "strava") or {}
    if tok.get("access_token"):
        try:
            httpx.post("https://www.strava.com/oauth/deauthorize",
                       data={"access_token": tok["access_token"]}, timeout=15)
        except httpx.HTTPError:
            pass
    settings_store.set(session, "strava", {})
    settings_store.set(session, "strava_summary", "")
    session.commit()


def _access_token(session: Session) -> str:
    tok = settings_store.get(session, "strava") or {}
    if not tok.get("refresh_token"):
        raise StravaError("Strava isn't connected")
    if tok.get("expires_at", 0) - 300 > time.time():
        return tok["access_token"]
    fresh = _token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    _save(session, fresh)
    return fresh["access_token"]


def activities(session: Session, days: int = 42) -> list[dict]:
    token = _access_token(session)
    after = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    out: list[dict] = []
    with httpx.Client(timeout=20, headers={"Authorization": f"Bearer {token}"}) as c:
        for page in range(1, 6):
            resp = c.get(f"{API}/athlete/activities", params={"after": after, "per_page": 100, "page": page})
            if resp.status_code != 200:
                raise StravaError(f"Strava activities: HTTP {resp.status_code}")
            rows = resp.json()
            out += rows
            if len(rows) < 100:
                break
    return out


def _miles(meters: float) -> float:
    return (meters or 0) / METERS_PER_MILE


def _pace(seconds: float, meters: float) -> str:
    if not meters or not seconds:
        return ""
    per_mile = seconds / _miles(meters)
    return f"{int(per_mile // 60)}:{int(per_mile % 60):02d}/mi"


def _start(a: dict) -> datetime:
    raw = a.get("start_date_local") or a.get("start_date") or ""
    return datetime.fromisoformat(raw.replace("Z", "")).replace(tzinfo=None)


def summarize(acts: list[dict], today: datetime | None = None) -> str:
    """Plain numbers about the last six weeks of running. Empty if there were no runs."""
    today = (today or datetime.now(config.TZ).replace(tzinfo=None)).replace(hour=23, minute=59)
    runs = [a for a in acts if (a.get("sport_type") or a.get("type")) in RUN_TYPES]
    if not runs:
        return ""
    weeks: dict[int, float] = defaultdict(float)
    for a in runs:
        weeks[(today - _start(a)).days // 7] += _miles(a.get("distance", 0))
    this_week = weeks.get(0, 0.0)
    prior = [weeks.get(w, 0.0) for w in range(1, 5)]
    lines = [f"Training from Strava (as of {today:%a %b %-d}):",
             f"- Last 7 days: {this_week:.1f} mi over {sum(1 for a in runs if (today - _start(a)).days < 7)} runs. "
             f"Average of the 4 weeks before: {sum(prior) / 4:.1f} mi/week."]
    recent = [a for a in runs if (today - _start(a)).days < 14]
    if recent:
        long_run = max(recent, key=lambda a: a.get("distance", 0))
        lines.append(f"- Longest run in the last 2 weeks: {_miles(long_run['distance']):.1f} mi on "
                     f"{_start(long_run):%a %b %-d} ({_pace(long_run.get('moving_time'), long_run['distance'])}).")
    races = sorted((a for a in runs if a.get("workout_type") == 1), key=_start, reverse=True)
    for r in races[:2]:
        lines.append(f"- Race: {r.get('name', 'race')} on {_start(r):%b %-d}, {_miles(r['distance']):.2f} mi in "
                     f"{timedelta(seconds=int(r.get('moving_time', 0)))} ({_pace(r.get('moving_time'), r['distance'])}).")
    workouts = sorted((a for a in runs if a.get("workout_type") == 3 and (today - _start(a)).days < 14),
                      key=_start, reverse=True)
    if workouts:
        lines.append("- Workouts in the last 2 weeks: " + "; ".join(
            f"{w.get('name', 'workout')} ({_start(w):%b %-d}, {_miles(w['distance']):.1f} mi)" for w in workouts[:3]) + ".")
    return "\n".join(lines)


def refresh_summary(session: Session) -> str:
    """Called each cycle. Keeps the last good summary if Strava is unreachable."""
    if not configured() or not connected(session):
        return ""
    try:
        text = summarize(activities(session))
    except (StravaError, httpx.HTTPError) as e:
        log.warning("Strava refresh failed: %s", e)
        return settings_store.get(session, "strava_summary") or ""
    settings_store.set(session, "strava_summary", text)
    session.commit()
    return text
