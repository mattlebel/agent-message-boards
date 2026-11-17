from datetime import timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import emailer, llm, races
from app.db import init_db, session_scope
from app.main import app
from app.models import Race, RaceDate


def _cleanup(s, race):
    for d in races.dates_for(s, race):
        s.delete(d)
    s.delete(race)


def test_research_alerts_and_page(monkeypatch):
    init_db()
    today = races.today()
    opens = today.strftime("%Y-%m-%d")
    closes = (today + timedelta(days=5)).strftime("%Y-%m-%d")

    def fake_generate(**kw):
        assert kw["web_search"] and kw["purpose"] == "race_research"
        return SimpleNamespace(parsed=races._Found(
            race_date="2027-10-10", url="https://example.com/race", how_to_enter="Lottery, or a time qualifier.",
            dates=[races._Date(kind="opens", label="Lottery opens", date=opens, confirmed=True),
                   races._Date(kind="closes", label="Lottery closes", date=closes, confirmed=False),
                   races._Date(kind="whatever", label="Bad date", date="soon")]))

    monkeypatch.setattr(llm, "generate", fake_generate)
    sent = []
    monkeypatch.setattr(emailer, "send", lambda to, subject, html, text="", scheduled_at="": sent.append(subject) or "id")
    with session_scope() as s:
        race = Race(name="Test City Marathon 2027", kind="major")
        s.add(race)
        s.flush()
        assert races.research_due(s, race)
        assert races.research(s, race) == 2
        assert race.race_date.year == 2027 and race.research_notes.startswith("Lottery")
        assert not races.research_due(s, race)
        assert races.research(s, race) == 0  # same dates again: updated, not duplicated
        assert len(races.dates_for(s, race)) == 2

        due = {(d.kind, which) for _, d, which in races.due_alerts(s)}
        assert ("opens", "today") in due and ("closes", "week") in due
        assert races.send_alerts(s) >= 2 and any("Lottery opens today" in x for x in sent)
        assert races.due_alerts(s) == [] or all(r.id != race.id for r, _, _ in races.due_alerts(s))
        assert "Lottery closes" in races.newsletter_material(races.for_newsletter(s, morning=True))
        race_id = race.id

    page = TestClient(app).get("/races")
    assert page.status_code == 200 and "Test City Marathon 2027" in page.text and "(expected)" in page.text

    with session_scope() as s:
        _cleanup(s, s.get(Race, race_id))


def test_default_races_seed_once():
    init_db()
    with session_scope() as s:
        existing = list(s.scalars(select(Race)))
        if existing:
            return
        assert races.seed_defaults(s) == len(races.DEFAULT_RACES)
        assert races.seed_defaults(s) == 0
        for r in s.scalars(select(Race)):
            _cleanup(s, r)
        assert not s.scalar(select(RaceDate.id).limit(1))
