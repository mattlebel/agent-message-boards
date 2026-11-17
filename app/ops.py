"""Tell the owner when a scheduled job keeps failing, and export the board's setup as a backup."""

from __future__ import annotations

import html as html_lib
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, emailer, settings_store
from app.db import utcnow
from app.models import (Agent, JobRun, Race, RaceDate, ReadingItem, Setting, Source, Topic, Trip, TripItem,
                        WalletCard, YoutubeChannel)

log = logging.getLogger(__name__)

QUIET_HOURS = 12  # at most one email per failing job in this window


def notify_if_failing(session: Session, job: str) -> bool:
    """Email once when a job's last two runs both failed. Returns True if an email went out."""
    last = list(session.scalars(select(JobRun).where(JobRun.job == job, JobRun.finished_at.is_not(None))
                                .order_by(JobRun.id.desc()).limit(2)))
    if len(last) < 2 or any(r.status != "error" for r in last):
        return False
    sent = dict(settings_store.get(session, "ops_alerts") or {})
    prev = sent.get(job)
    if prev and datetime.fromisoformat(prev) > utcnow() - timedelta(hours=QUIET_HOURS):
        return False
    rows = "".join(
        f"<p><b>{r.finished_at:%b %-d %H:%M} UTC</b><br><code>{html_lib.escape(r.detail[:800])}</code></p>"
        for r in last)
    body = (f"<p>The <b>{html_lib.escape(job)}</b> job failed twice in a row.</p>{rows}"
            f'<p><a href="{config.BASE_URL}/settings">Recent jobs on the Settings page</a>. Render\'s logs have '
            f"the full traceback. You won't get another email about this job for {QUIET_HOURS} hours.</p>")
    subject = f"⚠️ The Board: {job} job is failing"
    try:
        emailer.send(emailer.recipients(session), subject, emailer.wrap(subject, body, "The Board · Ops", ""))
    except emailer.EmailError:
        log.exception("couldn't send the failure email")
        return False
    sent[job] = utcnow().isoformat()
    settings_store.set(session, "ops_alerts", sent)
    session.commit()
    return True


# --- backup ---------------------------------------------------------------------------------

# Settings that are secrets or machine state, never exported.
PRIVATE_SETTINGS = {"share_secret", "strava", "ops_alerts", "applied_migrations", "travel_backfilled",
                    "youtube_unresolved"}


def _row(obj, skip: tuple[str, ...] = ()) -> dict:
    out = {}
    for col in obj.__table__.columns:
        if col.name in skip:
            continue
        value = getattr(obj, col.name)
        out[col.name] = value.isoformat() if isinstance(value, datetime) else value
    return out


def backup(session: Session) -> dict:
    """Everything you set up by hand (topics, sources, personas, settings, wallet, trips, races, reading
    list, YouTube channels), as JSON. Fetched items, posts and newsletters aren't included; Render's
    database backups cover those."""
    return {
        "exported_at": utcnow().isoformat() + "Z",
        "settings": {s.key: s.value for s in session.scalars(select(Setting)) if s.key not in PRIVATE_SETTINGS},
        "topics": [_row(t) for t in session.scalars(select(Topic))],
        "sources": [_row(s, skip=("state",)) for s in session.scalars(select(Source))],
        "agents": [_row(a) for a in session.scalars(select(Agent))],
        "wallet_cards": [_row(c) for c in session.scalars(select(WalletCard))],
        "trips": [_row(t) for t in session.scalars(select(Trip))],
        "trip_items": [_row(i) for i in session.scalars(select(TripItem))],
        "races": [_row(r) for r in session.scalars(select(Race))],
        "race_dates": [_row(d) for d in session.scalars(select(RaceDate))],
        "reading_items": [_row(r) for r in session.scalars(select(ReadingItem))],
        "youtube_channels": [_row(c) for c in session.scalars(select(YoutubeChannel))],
    }
