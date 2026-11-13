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


def test_youtube_uses_a_lower_threshold(monkeypatch):
    from sqlalchemy import select

    from app import llm, triage

    init_db()
    with session_scope() as s:
        yt = Topic(slug="youtube-t", name="YouTube test")
        news = Topic(slug="news-t", name="News test")
        s.add_all([yt, news])
        s.flush()
        from app import settings_store
        settings_store.set(s, "topic_thresholds", {"youtube-t": 5})
        video = Item(topic_id=yt.id, dedupe_key=dedupe_key("yt:abc"), title="Norwegian Singles Method, Week 1",
                     url="https://www.youtube.com/watch?v=abc", body="Marathon block vlog.", status="scored", score=6,
                     extra={"source_kind": "youtube", "channel": "kofuzi", "video_id": "abc"})
        story = Item(topic_id=news.id, dedupe_key=dedupe_key("news:1"), title="A 6/10 news item",
                     status="scored", score=6, extra={})
        s.add_all([video, story])
        s.commit()

        assert triage.threshold_for(s, yt) == 5 and triage.threshold_for(s, news) == 7
        picked = triage.pick_for_discussion(s, 5)
        assert video in picked and story not in picked  # 6 clears YouTube's 5, not the default 7
        video.status = "scored"  # pretend it didn't get a thread, so it's listed instead
        s.commit()

        prompts = []
        real = llm.generate
        monkeypatch.setattr(llm, "generate", lambda **kw: prompts.append(kw["user"]) or real(**kw))
        nl = newsletter.build_daily(s, "pm")
        assert nl is not None
        assert "New on YouTube" in prompts[0] and "kofuzi: Norwegian Singles Method" in prompts[0]
        assert "i.ytimg.com/vi/abc" in prompts[0]  # thumbnail offered as an image
        assert s.scalar(select(Item).where(Item.id == video.id)).extra.get("newsletter_id") == nl.id
        assert newsletter.new_videos(s) == []  # listed once
        settings_store.set(s, "topic_thresholds", {"youtube": 5})
        s.delete(nl)
        s.delete(yt)
        s.delete(news)


def test_topic_threshold_form_saves_exact_values():
    from fastapi.testclient import TestClient

    from app import settings_store, triage
    from app.main import app

    init_db()
    client = TestClient(app)
    with session_scope() as s:
        topic = Topic(slug="youtube", name="YouTube")
        s.add(topic)
        s.flush()
        topic_id = topic.id
    for value, expected in (("7", 7.0), ("4.5", 4.5), ("", 5.0)):  # blank = default (5 for youtube)
        r = client.post(f"/topics/{topic_id}", data={"name": "YouTube", "threshold": value, "active": "true"},
                        follow_redirects=False)
        assert r.status_code == 303
        with session_scope() as s:
            assert triage.threshold_for(s, s.get(Topic, topic_id)) == expected
    assert "Thread threshold" in client.get(f"/topics/{topic_id}").text
    with session_scope() as s:
        settings_store.set(s, "topic_thresholds", {"youtube": 5})
        s.delete(s.get(Topic, topic_id))
