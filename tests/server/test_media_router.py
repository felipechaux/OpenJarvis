"""Now playing / media controls for the notch (server/media_router.py)."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from openjarvis.server import media_router as mr  # noqa: E402
from openjarvis.tools import browser_launcher as bl  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    mr._cache.update(ts=0.0, data={"state": "none"})
    calls = {"now": 0, "control": []}

    def now():
        calls["now"] += 1
        return {"state": "playing", "title": "Song", "artist": "Feid", "art": ""}

    monkeypatch.setattr(bl, "spotify_now_playing", now)
    monkeypatch.setattr(bl, "spotify_control", lambda a: calls["control"].append(a) or True)
    app = FastAPI()
    app.include_router(mr.router)
    return TestClient(app), calls


def test_now_playing_is_cached(client):
    c, calls = client
    assert c.get("/v1/media/now-playing").json()["title"] == "Song"
    c.get("/v1/media/now-playing")
    assert calls["now"] == 1


def test_control_runs_and_refreshes(client):
    c, calls = client
    c.get("/v1/media/now-playing")
    assert c.post("/v1/media/next", json={}).json() == {"ok": True}
    assert calls["control"] == ["next"]
    c.get("/v1/media/now-playing")
    assert calls["now"] == 2


def test_control_rejects_unknown_actions_and_plain_posts(client):
    c, calls = client
    assert c.post("/v1/media/shutdown", json={}).json()["ok"] is False
    r = c.post("/v1/media/pause", content="{}", headers={"content-type": "text/plain"})
    assert r.json()["ok"] is False
    assert calls["control"] == []


def test_now_playing_off_spotify(monkeypatch):
    monkeypatch.setattr(bl.sys, "platform", "darwin")
    monkeypatch.setattr(bl, "_in_spotify_tab", lambda js, here: '{"host": "example.com", "state": "playing"}')
    assert bl.spotify_now_playing() == {"state": "none"}
    monkeypatch.setattr(bl, "_in_spotify_tab", lambda js, here: None)
    assert bl.spotify_now_playing() == {"state": "none"}


def test_tab_lookup_retries_when_the_tab_moved(monkeypatch):
    monkeypatch.setattr(bl, "_spotify_tab", ("chrome", 1, 1))
    monkeypatch.setattr(bl, "_spotify_browser_kind", lambda: "chrome")
    monkeypatch.setattr(bl, "find_tabs", lambda kind, host: [(2, 5, "https://open.spotify.com/")])
    ran = []

    def run(kind, w, t, js):
        ran.append((w, t))
        return "ok" if (w, t) == (2, 5) else "elsewhere"

    monkeypatch.setattr(bl, "run_js_in_tab", run)
    assert bl._in_spotify_tab("x", lambda o: o != "elsewhere") == "ok"
    assert ran == [(1, 1), (2, 5)]
    assert bl._spotify_tab == ("chrome", 2, 5)
