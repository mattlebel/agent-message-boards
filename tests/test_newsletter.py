from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app import clicks, llm, newsletter
from app.db import init_db, session_scope, utcnow
from app.models import Click, Coverage, Newsletter, Post, Thread


def test_followups_only_carry_new_posts():
    init_db()
    with session_scope() as s:
        t = Thread(title="Coverage thread")
        s.add(t)
        s.flush()
        t.posts.append(Post(body="First post about the drop."))
        t.posts.append(Post(body="Second post with the price."))
        s.flush()
        nl = Newsletter(kind="daily", subject="x", sent_at=utcnow())
        s.add(nl)
        s.flush()
        s.add(Coverage(newsletter_id=nl.id, thread_id=t.id, last_post_id=t.posts[-1].id))
        s.flush()
        since = utcnow() - timedelta(hours=1)

        threads, _, followups = newsletter.collect(s, since)
        assert t not in threads  # nothing new since it was covered

        t.posts.append(Post(body="Third post: it sold out."))
        t.last_activity_at = utcnow()
        s.flush()
        threads, _, followups = newsletter.collect(s, since)
        assert t in threads and followups[t.id] == t.posts[1].id
        text = newsletter._thread_material(t, covered_until=followups[t.id])
        assert "FOLLOW-UP" in text and "sold out" in text and "First post" not in text

        for row in s.scalars(select(Coverage).where(Coverage.thread_id == t.id)):
            s.delete(row)
        s.delete(t)
        s.delete(nl)


def test_fact_check_applies_fixes(monkeypatch):
    draft = newsletter._Edition(subject="Prints at $5", preheader="p", markdown="The prints cost $5. " * 10)
    fixed_md = "The prints cost $1. " * 10

    def fake_generate(**kw):
        assert kw["role"] == "factcheck" and "THE MATERIAL" in kw["user"]
        return SimpleNamespace(parsed=newsletter._Check(problems=["price is $1 in the material"],
                                                        subject="Prints at $1", markdown=fixed_md))

    monkeypatch.setattr(llm, "generate", fake_generate)
    with session_scope() as s:
        ed, problems = newsletter.fact_check(s, draft, material="Price: $1")
    assert problems and ed.markdown == fixed_md and ed.subject == "Prints at $1"

    # A truncated "correction" is ignored rather than replacing the whole edition.
    monkeypatch.setattr(llm, "generate", lambda **kw: SimpleNamespace(
        parsed=newsletter._Check(problems=["x"], markdown="short")))
    with session_scope() as s:
        ed, problems = newsletter.fact_check(s, draft, material="")
    assert ed.markdown == draft.markdown


def test_click_boost_and_wrapping():
    init_db()
    with session_scope() as s:
        s.add_all([Click(kind="email", topic_id=901), Click(kind="email", topic_id=901),
                   Click(kind="open", topic_id=902)])
        s.flush()
        boost = clicks.topic_boost(s)
        assert boost[901] == clicks.MAX_BOOST and 0 < boost[902] < boost[901]
        for c in s.scalars(select(Click).where(Click.topic_id.in_([901, 902]))):
            s.delete(c)

    html = ('<a href="https://example.com/a?x=1&amp;y=2">a</a> <img src="https://example.com/i.jpg">'
            '<a href="http://testserver/s/tok/add">add</a>')
    out = clicks.wrap_links(html, 7)
    assert "/r/" in out and 'src="https://example.com/i.jpg"' in out and "/s/tok/add" in out
    key = out.split("/r/")[1].split('"')[0]
    assert clicks.read_key(key) == ("https://example.com/a?x=1&y=2", 7)
