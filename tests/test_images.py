from app import emailer, images, newsletter
from app.db import init_db, session_scope
from app.models import Item, Topic
from app.sources.base import dedupe_key
from app.triage import AlertDecision


def test_image_extraction():
    page = '<html><head><meta property="og:image" content="/img/drop.jpg"></head><body></body></html>'
    assert images.from_html(page, "https://inciardiprints.com/news") == "https://inciardiprints.com/img/drop.jpg"
    entry = {"media_content": [{"url": "https://scontent.cdninstagram.com/p.jpg", "type": "image/jpeg"}]}
    assert images.from_feed_entry(entry) == "https://scontent.cdninstagram.com/p.jpg"
    entry = {"summary": '<p>New machine!</p><img src="https://rss.app/img/x.jpg">'}
    assert images.from_feed_entry(entry) == "https://rss.app/img/x.jpg"
    assert images.from_feed_entry({"summary": '<img src="http://insecure.example/x.jpg">'}) == ""


def test_newsletter_keeps_only_provided_images():
    md = ("Intro\n\n![The drop](https://a.example/1.jpg)\n\n![Invented](https://made.up/x.jpg)\n\n"
          + "".join(f"![p{i}](https://a.example/{i}.jpg)\n" for i in range(2, 7)))
    allowed = {f"https://a.example/{i}.jpg" for i in range(1, 7)}
    out = newsletter.keep_known_images(md, allowed)
    assert "made.up" not in out
    assert out.count("![") == 4


def test_alert_email_leads_with_picture(monkeypatch):
    init_db()
    sent = []
    monkeypatch.setattr(emailer, "send", lambda to, subject, html, text="", scheduled_at="": sent.append(html) or "id")
    with session_scope() as s:
        topic = Topic(slug="img-alert", name="Inciardi")
        s.add(topic)
        s.flush()
        item = Item(topic_id=topic.id, dedupe_key=dedupe_key("img-alert-1"), title="Holiday Drop",
                    url="", extra={"image": "https://scontent.cdninstagram.com/drop.jpg"})
        s.add(item)
        s.flush()
        newsletter.send_alert(s, item, AlertDecision(id=item.id, is_alert=True, headline="Holiday Drop"), None)
        s.delete(topic)
    assert '<img width="576"' in sent[0] and "drop.jpg" in sent[0]
