"""Session prompts for the notch: parsing menus, live state, answering."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from openjarvis.server import coding_sessions_router as rm  # noqa: E402
from openjarvis.server import session_watcher as sw  # noqa: E402
from openjarvis.tools import session_control as sc  # noqa: E402

BOXED = """\
╭──────────────────────────────────────────────────────────╮
│ Bash command                                             │
│                                                          │
│   rm -rf build                                           │
│   Remove the build directory                             │
│                                                          │
│ Do you want to proceed?                                  │
│ ❯ 1. Yes                                                 │
│   2. Yes, and don't ask again for rm commands in /x/app  │
│   3. No, and tell Claude what to do differently (esc)    │
╰──────────────────────────────────────────────────────────╯
"""

PLAIN = """\
 Edit file
 src/app.py
──────────────────────────────
 Do you want to make this edit to app.py?
 ❯ 1. Yes
   2. Yes, allow all edits during this session (shift+tab)
   3. No (esc)
"""

QUESTION = """\
 ☐ Base de datos
 Which database should we use?
 ❯ 1. Postgres
   2. SQLite
   3. Type something.
"""

OUTPUT_LIST = """\
⏺ Steps:
  1. Build the app
  2. Run the tests
> try again
"""


def test_boxed_permission_dialog():
    p = sw.parse_prompt(BOXED)
    assert p is not None
    assert p["question"] == "Do you want to proceed?"
    assert "rm -rf build" in p["detail"]
    assert [(o["key"], o["label"]) for o in p["options"]] == [
        ("1", "Sí"),
        ("2", "Sí, siempre"),
        ("Escape", "No"),
    ]


def test_plain_edit_dialog():
    p = sw.parse_prompt(PLAIN)
    assert p["question"].startswith("Do you want to make this edit")
    assert "src/app.py" in p["detail"]
    assert [o["label"] for o in p["options"]] == ["Sí", "Sí, siempre", "No"]


def test_question_menu_keeps_its_labels():
    p = sw.parse_prompt(QUESTION)
    assert [o["label"] for o in p["options"]] == ["Postgres", "SQLite", "Type something."]
    assert [o["key"] for o in p["options"]] == ["1", "2", "3"]


@pytest.mark.parametrize("screen", [OUTPUT_LIST, "", "> hola\n esc to interrupt"])
def test_not_a_prompt(screen):
    assert sw.parse_prompt(screen) is None


def test_prompt_id_tracks_the_dialog():
    assert sw.parse_prompt(BOXED)["id"] == sw.parse_prompt(BOXED)["id"]
    assert sw.parse_prompt(BOXED)["id"] != sw.parse_prompt(PLAIN)["id"]


def test_edited_files_collected_while_working():
    sw._state.clear()
    sw.step("jarvis-x", "x", "esc to interrupt", [("edit", "a.py"), ("read", "b.py")], 100.0, 60)
    sw.step("jarvis-x", "x", "esc to interrupt", [("edit", "a.py"), ("edit", "c.py")], 104.0, 60)
    assert sw._state["jarvis-x"]["edited"] == ["a.py", "c.py"]
    sw.step("jarvis-x", "x", "", [], 108.0, 60)  # stops: list kept for the notch
    assert sw._state["jarvis-x"]["edited"] == ["a.py", "c.py"]
    sw.step("jarvis-x", "x", "esc to interrupt", [], 112.0, 60)  # new stretch
    assert sw._state["jarvis-x"]["edited"] == []
    sw._state.clear()


@pytest.fixture()
def session(monkeypatch):
    screen = {"text": BOXED}
    pressed = []
    monkeypatch.setattr(sc, "list_sessions", lambda: [("jarvis-app", "/x/app")])
    monkeypatch.setattr(sc, "capture_screen", lambda name, lines=20: screen["text"])
    monkeypatch.setattr(sc, "press_keys", lambda name, keys: pressed.append((name, keys)))
    app = FastAPI()
    app.include_router(rm.router)
    return TestClient(app), screen, pressed


def test_live_reports_the_prompt(session):
    client, _, _ = session
    [s] = client.get("/v1/coding-sessions/live").json()["sessions"]
    assert s["name"] == "jarvis-app"
    assert s["project"] == "app"
    assert s["status"] == "prompt"
    assert s["prompt"]["options"][0]["label"] == "Sí"


def test_answer_presses_the_key(session):
    client, _, pressed = session
    pid = sw.parse_prompt(BOXED)["id"]
    r = client.post("/v1/coding-sessions/jarvis-app/answer", json={"prompt_id": pid, "key": "1"})
    assert r.json() == {"ok": True}
    assert pressed == [("jarvis-app", ["1"])]


def test_answer_refuses_a_stale_prompt(session):
    client, screen, pressed = session
    pid = sw.parse_prompt(BOXED)["id"]
    screen["text"] = "> listo\n"
    r = client.post("/v1/coding-sessions/jarvis-app/answer", json={"prompt_id": pid, "key": "1"})
    assert r.json()["ok"] is False
    assert pressed == []


def test_answer_refuses_keys_outside_the_menu(session):
    client, _, pressed = session
    pid = sw.parse_prompt(BOXED)["id"]
    r = client.post("/v1/coding-sessions/jarvis-app/answer", json={"prompt_id": pid, "key": "3"})
    assert r.json()["ok"] is False  # "No" answers with Escape, not 3
    assert pressed == []


def test_answer_needs_json_content_type(session):
    client, _, pressed = session
    pid = sw.parse_prompt(BOXED)["id"]
    r = client.post(
        "/v1/coding-sessions/jarvis-app/answer",
        content=f'{{"prompt_id": "{pid}", "key": "1"}}',
        headers={"content-type": "text/plain"},
    )
    assert r.json()["ok"] is False
    assert pressed == []


def test_answer_unknown_session(session):
    client, _, _ = session
    r = client.post("/v1/coding-sessions/jarvis-nope/answer", json={"prompt_id": "x", "key": "1"})
    assert r.json()["ok"] is False


def test_message_is_typed_into_the_session(session, monkeypatch):
    client, screen, _ = session
    typed = []
    monkeypatch.setattr(sc, "type_message", lambda name, text: typed.append((name, text)))
    screen["text"] = "> listo\n"
    r = client.post("/v1/coding-sessions/jarvis-app/message", json={"text": " corre los tests "})
    assert r.json() == {"ok": True}
    assert typed == [("jarvis-app", "corre los tests")]


def test_message_refused_while_a_menu_shows(session, monkeypatch):
    client, _, _ = session
    typed = []
    monkeypatch.setattr(sc, "type_message", lambda name, text: typed.append(text))
    r = client.post("/v1/coding-sessions/jarvis-app/message", json={"text": "hola"})
    assert r.json()["ok"] is False
    assert typed == []


def test_message_needs_json(session, monkeypatch):
    client, screen, _ = session
    typed = []
    monkeypatch.setattr(sc, "type_message", lambda name, text: typed.append(text))
    screen["text"] = "> listo\n"
    r = client.post(
        "/v1/coding-sessions/jarvis-app/message",
        content='{"text": "rm -rf /"}',
        headers={"content-type": "text/plain"},
    )
    assert r.json()["ok"] is False
    assert typed == []
