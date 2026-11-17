from sqlalchemy import select

from app import discussion, mailcommands, reading, settings_store
from app.db import init_db, session_scope
from app.inbound import process_email
from app.models import InboundEmail, Newsletter, ReadingItem, Thread, Topic


def _row(s, to, frm, subject, text):
    row = InboundEmail(resend_id=f"t-{to}-{subject}-{len(text)}", from_addr=frm, to_addrs=[to], subject=subject, text=text)
    s.add(row)
    s.flush()
    return row


def test_own_words_strips_quotes():
    text = "What's the 9+1 deadline?\n\nOn Tue, Nov 17, 2026 at 6:00 AM Board Stuff <\nboard@lebel.io> wrote:\n> old stuff"
    assert mailcommands.own_words(text) == "What's the 9+1 deadline?"
    assert mailcommands.command_for(["Read <read+x@in.lebel.io>"]) == "read"
    assert mailcommands.command_for(["ask@in.lebel.io"]) == "ask"
    assert mailcommands.command_for(["feeds@in.lebel.io"]) == ""


def test_read_and_ask(monkeypatch):
    init_db()
    processed, answered = [], []
    monkeypatch.setattr(reading, "process", lambda s, rid: processed.append(rid))
    monkeypatch.setattr(discussion, "respond_to_owner", lambda s, tid: answered.append(tid))
    with session_scope() as s:
        settings_store.set(s, "trusted_senders", ["friend@gmail.com"])
        # Untrusted sender: ignored.
        row = _row(s, "read@in.lebel.io", "Stranger <x@evil.example>", "hi", "https://example.com/a")
        process_email(s, row, {"text": row.text, "to": row.to_addrs, "subject": row.subject})
        assert row.status == "ignored" and not processed

        row = _row(s, "read@in.lebel.io", "Friend <friend@gmail.com>", "Discuss this",
                   "Is this right about the mile?\nhttps://example.com/mile-story\n")
        process_email(s, row, {"text": row.text, "to": row.to_addrs, "subject": row.subject})
        item = s.get(ReadingItem, processed[0])
        assert item.url == "https://example.com/mile-story" and item.discuss
        assert item.instructions == "Is this right about the mile?"

        nl = Newsletter(kind="daily", subject="The mile is a market", markdown="Body of the edition.")
        s.add(nl)
        s.flush()
        row = _row(s, mailcommands.ask_address(), "me@lebel.io", "Re: The mile is a market",
                   "Which mile race should I enter?\n\nOn Tue, Nov 17, 2026, Board Stuff wrote:\n> quoted")
        process_email(s, row, {"text": row.text, "to": row.to_addrs, "subject": row.subject})
        thread = s.get(Thread, answered[0])
        assert thread.topic.slug == "ask" and thread.posts[0].body == "Which mile race should I enter?"
        assert "Body of the edition." in thread.item.body
        settings_store.set(s, "trusted_senders", [])
        for r in s.scalars(select(ReadingItem).where(ReadingItem.id.in_(processed))):
            s.delete(r)
        # The smoke test expects to seed an empty DB, so remove the topics this test created.
        topic, item = thread.topic, thread.item
        for r in s.scalars(select(InboundEmail).where(InboundEmail.topic_id == topic.id)):
            r.topic_id = None
        s.delete(thread)
        s.flush()
        s.delete(item)
        s.delete(topic)
        s.delete(nl)
        reading_topic = s.scalar(select(Topic).where(Topic.slug == reading.TOPIC_SLUG))
        if reading_topic is not None and not reading_topic.sources:
            s.delete(reading_topic)
