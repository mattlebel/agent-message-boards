from datetime import datetime

from fastapi.testclient import TestClient

from app import config, settings_store, strava, wallet
from app.db import init_db, session_scope
from app.main import app


def _run(day, miles, minutes, workout_type=0, name="Run"):
    return {"sport_type": "Run", "start_date_local": f"2026-11-{day:02d}T07:00:00Z", "distance": miles * 1609.344,
            "moving_time": minutes * 60, "workout_type": workout_type, "name": name}


def test_summary_numbers():
    today = datetime(2026, 11, 17)
    acts = [
        _run(16, 14, 112, 2, "Long run"), _run(14, 6, 45, 3, "Mile repeats"), _run(12, 5, 40),
        _run(8, 3.11, 18, 1, "Turkey Trot 5K"), _run(1, 10, 80),
        {"sport_type": "Ride", "start_date_local": "2026-11-15T07:00:00Z", "distance": 40000, "moving_time": 3600},
    ]
    text = strava.summarize(acts, today)  # Tue Nov 17: this week is Mon Nov 16-Sun Nov 22
    assert "This week so far (Mon Nov 16-Tue Nov 17): 14.0 mi over 1 run." in text
    assert "Last week (Mon Nov 9-Sun Nov 15): 11.0 mi over 2 runs." in text  # Sat 14th + Thu 12th
    assert "averaged 3.3 mi/week" in text  # Sun Nov 8 (3.11) + Sun Nov 1 (10) over Oct 12-Nov 8
    assert "Longest run this week or last: 14.0 mi on Mon Nov 16 (8:00/mi)" in text
    assert "Race: Turkey Trot 5K on Nov 8" in text and "Mile repeats" in text
    # Sunday still belongs to the week that started the Monday before.
    sunday = strava.summarize([_run(22, 5, 40)], datetime(2026, 11, 22))
    assert "This week so far (Mon Nov 16-Sun Nov 22): 5.0 mi over 1 run." in sunday
    assert strava.summarize([], today) == ""


def test_connect_flow(monkeypatch):
    init_db()
    monkeypatch.setattr(config, "STRAVA_CLIENT_ID", "123")
    monkeypatch.setattr(config, "STRAVA_CLIENT_SECRET", "shh")
    calls = []

    def fake_token(data):
        calls.append(data)
        return {"access_token": "a", "refresh_token": "r", "expires_at": 4102444800,
                "athlete": {"firstname": "Test", "lastname": "Runner"}}

    monkeypatch.setattr(strava, "_token_request", fake_token)
    monkeypatch.setattr(strava, "activities", lambda s, days=42: [_run(16, 8, 64)])
    client = TestClient(app)
    r = client.get("/strava/connect", follow_redirects=False)
    assert r.headers["location"].startswith(strava.AUTHORIZE_URL)
    state = r.headers["location"].split("state=")[1]
    bad = client.get("/strava/callback", params={"code": "c", "scope": "read,activity:read_all", "state": "nope"})
    assert "expired" in bad.text
    ok = client.get("/strava/callback", params={"code": "c", "scope": "read,activity:read_all", "state": state})
    assert ok.status_code == 200 and calls[0]["grant_type"] == "authorization_code"
    with session_scope() as s:
        assert strava.connected(s)
        assert "Training from Strava" in wallet.profile_text(s)
    assert "Test Runner" in client.get("/settings").text
    client.post("/strava/disconnect")
    with session_scope() as s:
        assert not strava.connected(s) and settings_store.get(s, "strava_summary") == ""
