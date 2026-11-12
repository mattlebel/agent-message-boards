from sqlalchemy import select

from app import youtube
from app.db import init_db, session_scope
from app.models import YoutubeChannel


def test_parse_channels():
    text = ("@2Danny2Furious, @allie_ostrander, @Danny-Gonzalez,\n@hoog-youtube "
            "https://www.youtube.com/@mkbhd/videos UCBJycsmduvYEL83R_U4JriQ @casey, @casey")
    assert youtube.parse_channels(text) == [
        "@2Danny2Furious", "@allie_ostrander", "@Danny-Gonzalez", "@hoog-youtube", "@mkbhd",
        "UCBJycsmduvYEL83R_U4JriQ", "@casey",
    ]


def test_add_many_resolves_and_reports_missing(monkeypatch):
    init_db()

    class Resp:
        def __init__(self, status, text):
            self.status_code, self.text = status, text

    def fake_get(url, **kw):
        if url.endswith("@casey"):
            return Resp(200, '<meta property="og:title" content="Casey Neistat">'
                             '<link rel="canonical" href="https://www.youtube.com/channel/UCtinbF-Q-fVthA0qrFQTgXQ">'
                             '"channelId":"UCxxxxxxxxxxxxxxxxxxxxxx"')
        return Resp(404, "")

    monkeypatch.setattr(youtube.httpx, "get", fake_get)
    with session_scope() as s:
        added, missing = youtube.add_many(s, ["@casey", "@nosuchchannel"])
        assert (added, missing) == (1, ["@nosuchchannel"])
        ch = s.scalar(select(YoutubeChannel).where(YoutubeChannel.channel_id == "UCtinbF-Q-fVthA0qrFQTgXQ"))
        assert ch.title == "Casey Neistat"
        s.delete(ch)
