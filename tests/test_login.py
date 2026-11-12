"""Emailed sign-in links: one use, right domain, no open redirects."""

import re
from urllib.parse import unquote

from fastapi.testclient import TestClient

from app import config, emailer
from app.db import init_db
from app.main import app


def _setup(monkeypatch):
    init_db()
    monkeypatch.setattr(config, "DEV_MODE", False)
    monkeypatch.setattr(config, "RESEND_API_KEY", "test-key")
    sent = []
    monkeypatch.setattr(emailer, "send", lambda to, subject, html, text="", scheduled_at="": sent.append((to, text)) or "id")
    return TestClient(app), sent


def _token(text):
    return unquote(re.search(r"token=(\S+)", text).group(1))


def test_link_signs_in_once(monkeypatch):
    client, sent = _setup(monkeypatch)
    assert client.get("/", follow_redirects=False).headers["location"].startswith("/login")

    r = client.post("/auth/email", data={"email": "Matt@Lebel.io", "next": "/settings"})
    assert r.status_code == 200 and "Check your email" in r.text
    assert len(sent) == 1 and sent[0][0] == ["matt@lebel.io"]
    token = _token(sent[0][1])

    # Opening the link only shows a button; it doesn't use the link up.
    page = client.get("/auth/email/verify", params={"token": token})
    assert "matt@lebel.io" in page.text
    assert client.get("/auth/email/verify", params={"token": token}).text.count("Sign in") >= 1

    r = client.post("/auth/email/verify", data={"token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    assert client.get("/settings").status_code == 200

    other = TestClient(app)
    r = other.post("/auth/email/verify", data={"token": token}, follow_redirects=False)
    assert r.status_code == 200 and "expired or was already used" in r.text


def test_other_domains_get_no_email(monkeypatch):
    client, sent = _setup(monkeypatch)
    r = client.post("/auth/email", data={"email": "someone@example.com"})
    assert "Check your email" in r.text  # same page either way
    assert sent == []


def test_tampered_and_redirect_links(monkeypatch):
    client, sent = _setup(monkeypatch)
    assert "expired" in client.get("/auth/email/verify", params={"token": "nope"}).text
    client.post("/auth/email", data={"email": "alex@lebel.io", "next": "//evil.example"})
    token = _token(sent[-1][1])
    r = client.post("/auth/email/verify", data={"token": token[:-2] + "xx"}, follow_redirects=False)
    assert r.status_code == 200
    r = client.post("/auth/email/verify", data={"token": token}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_rate_limit(monkeypatch):
    client, sent = _setup(monkeypatch)
    for _ in range(8):
        client.post("/auth/email", data={"email": "limit@lebel.io"})
    assert len(sent) == 5


def test_public_pages_dont_reveal_the_allowed_domain(monkeypatch):
    client, _ = _setup(monkeypatch)
    domain = config.ALLOWED_EMAIL_DOMAIN
    assert domain not in client.get("/login").text
    r = client.post("/auth/email", data={"email": "someone@example.com"})
    assert domain not in r.text


def test_email_share_links(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app import share
    from app.db import session_scope
    from app.models import Newsletter, Post, Thread
    from app.newsletter import send_newsletter

    client, sent = _setup(monkeypatch)
    with session_scope() as s:
        a = Thread(title="Holiday Drop thread")
        b = Thread(title="Other thread")
        s.add_all([a, b])
        s.flush()
        a.posts.append(Post(body="Mystery packs Friday at noon."))
        a_id, b_id = a.id, b.id

    now = datetime.now(timezone.utc)
    good = share.make_token(a_id, now + timedelta(hours=24))

    page = client.get(f"/t/{a_id}", params={"k": good})
    assert page.status_code == 200 and "Holiday Drop thread" in page.text
    assert "Post reply" not in page.text and "noindex" in page.text

    expired = share.make_token(a_id, now - timedelta(minutes=1))
    assert "expired" in client.get(f"/t/{a_id}", params={"k": expired}).text
    assert "expired" in client.get(f"/t/{b_id}", params={"k": good}).text  # key is for another thread
    assert "expired" in client.get(f"/t/{a_id}", params={"k": good + "x"}).text  # tampered
    assert client.get(f"/t/{a_id}", follow_redirects=False).headers["location"].startswith("/login")

    # Links get keys at send time, fragments survive, other URLs don't change.
    base = config.BASE_URL
    html = f'<a href="{base}/t/{a_id}#p3">x</a> <a href="{base}/t/{a_id}/count">c</a> <a href="https://x.example/t/{a_id}">o</a>'
    out = share.tokenize_links(html)
    assert f"{base}/t/{a_id}?k=" in out and out.count("?k=") == 1 and "#p3" in out

    captured = []
    monkeypatch.setattr(emailer, "send", lambda to, subject, html, text="", scheduled_at="": captured.append(html) or "id")
    with session_scope() as s:
        nl = Newsletter(kind="daily", subject="s", markdown=f"{base}/t/{a_id}", html=f'<a href="{base}/t/{a_id}">t</a>')
        s.add(nl)
        s.flush()
        send_newsletter(s, nl)
        assert "?k=" not in nl.html  # stored copy keeps plain links
    assert "?k=" in captured[0]
    with session_scope() as s:  # leave the shared test DB as we found it
        for t in s.scalars(select(Thread).where(Thread.id.in_([a_id, b_id]))):
            s.delete(t)
