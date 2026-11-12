"""Scheduled work. Render runs `python -m app.jobs tick` every 15 minutes; tick decides what's due.

Manual runs: python -m app.jobs cycle | hot | newsletter am|pm | weekly | bootstrap | seed
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config, discussion, inbound, ingest, llm, newsletter, personas, settings_store, suggestions, triage
from app.db import init_db, job_lock, session_scope, utcnow
from app.models import Agent, JobRun

log = logging.getLogger("jobs")


# --- the actual jobs ------------------------------------------------------------------


def alerts_for_pending(session: Session) -> int:
    """Alert-check every item in an urgent topic that hasn't been checked yet; email the real drops."""
    from app.models import Thread

    sent = 0
    for item, decision in triage.check_urgent(session, triage.pending_urgent(session)):
        thread = session.scalar(select(Thread).where(Thread.item_id == item.id).limit(1))
        if thread is None:
            try:
                thread = discussion.alert_thread(session, item, decision)
            except Exception:
                log.exception("alert thread failed")
                session.rollback()
        newsletter.send_alert(session, item, decision, thread)
        sent += 1
    return sent


def alerts_after_manual_fetch() -> None:
    """Run from the web app after "Fetch now"; the next scheduled check covers it if a job is running."""
    with job_lock("tick") as got:
        if got:
            with session_scope() as s:
                alerts_for_pending(s)


def hot(session: Session) -> str:
    new = ingest.run_ingest(session, hot_only=True)
    alerts = alerts_for_pending(session)
    return f"{len(new)} new items, {alerts} alerts"


def cycle(session: Session) -> str:
    if not session.scalar(select(Agent.id).limit(1)):
        personas.bootstrap(session)
    inbound.process_pending(session)
    new = ingest.run_ingest(session)
    alerts = alerts_for_pending(session)
    scored = triage.triage(session)
    n = int(settings_store.get(session, "threads_per_cycle"))
    picked = triage.pick_for_discussion(session, n)
    threads = discussion.discuss(session, picked)
    revisited = discussion.revisit(session)
    return (
        f"{len(new)} new items, {alerts} alerts, {scored} scored, {len(picked)} picked, "
        f"{len(threads)} threads, {revisited} revisits, spent today ${llm.spent_today():.2f}"
    )


def send_daily(session: Session, edition: str, deliver_at: datetime | None = None) -> str:
    nl = newsletter.build_daily(session, edition)
    if nl is None:
        return "nothing to send"
    scheduled = deliver_at if deliver_at and deliver_at > datetime.now(config.TZ) + timedelta(minutes=1) else None
    newsletter.send_newsletter(session, nl, scheduled_at=scheduled)
    return f"newsletter {nl.id}: {nl.subject}" + (f" (scheduled {scheduled:%H:%M})" if scheduled else "")


def weekly(session: Session) -> str:
    rows = suggestions.generate(session)
    suggestions.send_weekly(session, rows)
    distilled = suggestions.distill_feedback(session)
    return f"{len(rows)} suggestions, {distilled} agents' notes updated"


# --- bookkeeping ------------------------------------------------------------------------


def run_job(job: str, fn, *args, key: str | None = None) -> str | None:
    """Run fn(session, *args) with a JobRun record. With a key, each slot only runs once."""
    with session_scope() as s:
        if key and s.scalar(select(JobRun.id).where(JobRun.key == key)):
            return None
        run = JobRun(job=job, key=key)
        s.add(run)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            return None
        run_id = run.id
    detail, status = "", "ok"
    try:
        with session_scope() as s:
            detail = fn(s, *args) or ""
    except Exception as e:
        log.exception("job %s failed", job)
        detail, status = f"{type(e).__name__}: {e}", "error"
    with session_scope() as s:
        run = s.get(JobRun, run_id)
        run.status, run.detail, run.finished_at = status, detail[:4000], utcnow()
    log.info("job %s %s: %s", job, status, detail)
    return detail


def _at(day: datetime, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def tick(now: datetime | None = None) -> None:
    now = now or datetime.now(config.TZ)
    with job_lock("tick") as got:
        if not got:
            log.info("another job is running; skipping this tick")
            return
        with session_scope() as s:
            if settings_store.get(s, "paused"):
                log.info("paused")
                return
            sched = settings_store.get(s, "schedule")
        day = now.strftime("%Y-%m-%d")

        for slot in ("am", "pm"):
            start = _at(now, sched[f"cycle_{slot}"])
            if start <= now < start + timedelta(hours=6):
                run_job("cycle", cycle, key=f"cycle:{day}:{slot}")

        for slot in ("am", "pm"):
            deliver = _at(now, sched[f"newsletter_{slot}"])
            # Build up to 20 minutes early and let Resend deliver on the minute.
            if deliver - timedelta(minutes=20) <= now < deliver + timedelta(hours=3):
                run_job("newsletter", send_daily, slot, deliver, key=f"newsletter:{day}:{slot}")

        weekly_at = _at(now, sched["weekly_time"])
        if now.weekday() == int(sched["weekly_weekday"]) and weekly_at <= now < weekly_at + timedelta(hours=6):
            run_job("weekly", weekly, key=f"weekly:{now:%G-W%V}")

        run_hot_quietly()


def run_hot_quietly() -> None:
    """The 15-minute urgent check only shows up in the job log when something happened."""
    try:
        with session_scope() as s:
            new = ingest.run_ingest(s, hot_only=True)
            alerts = alerts_for_pending(s)
        detail, status = f"{len(new)} new items, {alerts} alerts", "ok"
        if not new and not alerts:
            return
    except Exception as e:
        log.exception("hot check failed")
        detail, status = f"{type(e).__name__}: {e}", "error"
    with session_scope() as s:
        s.add(JobRun(job="hot", status=status, detail=detail[:4000], finished_at=utcnow()))


def run_named(name: str, *args) -> str | None:
    """Manual runs from the admin page or the command line."""
    with job_lock("tick") as got:
        if not got:
            return "Another job is running. Try again in a few minutes."
        if name == "cycle":
            return run_job("cycle", cycle)
        if name == "hot":
            return run_job("hot", hot)
        if name == "newsletter":
            return run_job("newsletter", send_daily, args[0] if args else "pm")
        if name == "weekly":
            return run_job("weekly", weekly)
        if name == "bootstrap":
            return run_job("bootstrap", lambda s: f"{len(personas.bootstrap(s))} personas")
        if name == "triage":
            return run_job("triage", lambda s: f"{triage.triage(s)} scored")
        raise ValueError(f"unknown job {name}")


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    from app.seed import run_migrations, seed

    with session_scope() as s:
        seed(s)
        run_migrations(s)
    cmd = argv[1] if len(argv) > 1 else "tick"
    if cmd == "tick":
        tick()
    elif cmd == "seed":
        pass
    else:
        print(run_named(cmd, *argv[2:]))


if __name__ == "__main__":
    main(sys.argv)
