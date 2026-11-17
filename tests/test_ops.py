import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import emailer, jobs, ops, settings_store
from app.db import init_db, session_scope
from app.main import app
from app.models import JobRun


def test_failure_email_after_two_failures(monkeypatch):
    init_db()
    sent = []
    monkeypatch.setattr(emailer, "send", lambda to, subject, html, text="", scheduled_at="", **kw: sent.append(subject) or "id")

    def boom(s):
        raise RuntimeError("source exploded")

    jobs.run_job("opstest", boom)
    assert not sent  # one failure isn't worth an email
    jobs.run_job("opstest", boom)
    assert sent == ["⚠️ The Board: opstest job is failing"]
    jobs.run_job("opstest", boom)
    assert len(sent) == 1  # quiet for 12 hours
    with session_scope() as s:
        for r in s.scalars(select(JobRun).where(JobRun.job == "opstest")):
            s.delete(r)
        settings_store.set(s, "ops_alerts", {})


def test_backup_has_no_secrets():
    init_db()
    with session_scope() as s:
        settings_store.set(s, "strava", {"refresh_token": "SECRET-TOKEN"})
        settings_store.set(s, "share_secret", "SECRET-SHARE")
        data = ops.backup(s)
        assert "strava" not in data["settings"] and "share_secret" not in data["settings"]
    r = TestClient(app).get("/settings/backup.json")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert "SECRET" not in r.text and "topics" in json.loads(r.text)
    with session_scope() as s:
        settings_store.set(s, "strava", {})
        settings_store.set(s, "share_secret", "")
