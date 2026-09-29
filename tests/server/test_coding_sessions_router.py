"""Spoken summary when a JARVIS-started Claude Code session finishes."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import List

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from openjarvis.server import coding_sessions_router as rm  # noqa: E402

FINAL_REPLY = (
    "Restauré google.json y apliqué la recomendación 1. Contactos ya funciona. "
    "Tienes que habilitar las APIs de Drive y Tasks."
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    rm._events.clear()
    rm.set_summarizer(None)
    monkeypatch.setattr(rm, "_SUMMARY_DELAY_S", 0.0)
    monkeypatch.setattr(rm.session_watcher, "scan", lambda *_a: [])
    monkeypatch.setattr(rm.session_watcher, "remember_transcript", lambda *_a: None)
    yield
    rm._events.clear()
    rm.set_summarizer(None)


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(rm.router)
    return TestClient(app)


@pytest.fixture()
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / "s.jsonl"
    rows = [
        {"type": "user", "cwd": "/x/openjarvis", "message": {"content": "hazlo"}},
        {
            "type": "assistant",
            "cwd": "/x/openjarvis",
            "message": {"content": [{"type": "text", "text": FINAL_REPLY}]},
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    return path


def _stop(client: TestClient, transcript: Path) -> None:
    client.post(
        "/v1/coding-sessions/events",
        json={
            "hook_event_name": "Stop",
            "cwd": "/x/openjarvis/repo",
            "session_id": "s1",
            "transcript_path": str(transcript),
        },
    )


def _poll(client: TestClient, after: int = 0) -> dict:
    return client.get("/v1/coding-sessions/events", params={"after": after}).json()


def _wait_until(pred, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while not pred():
        assert time.time() < deadline, "timed out"
        time.sleep(0.01)


def test_fast_summary_is_part_of_the_announcement(client, transcript):
    seen: List[str] = []

    def summarize(reply: str) -> str:
        seen.append(reply)
        return "Se arregló Contactos. Tienes que habilitar Drive y Tasks."

    rm.set_summarizer(summarize)
    _stop(client, transcript)
    _wait_until(lambda: rm._events[0].get("_summary_done"))

    events = _poll(client)["events"]
    assert [e["text"] for e in events] == [
        "Claude terminó en openjarvis. Se arregló Contactos. "
        "Tienes que habilitar Drive y Tasks."
    ]
    assert seen == [FINAL_REPLY]
    assert not any(k.startswith("_") for k in events[0])


def test_announcement_waits_for_the_summary(client, transcript):
    release = threading.Event()
    rm.set_summarizer(lambda _r: (release.wait(3), "Resumen listo para decir.")[1])
    _stop(client, transcript)
    assert _poll(client)["events"] == []  # held while the model works
    release.set()
    _wait_until(lambda: rm._events[0].get("_summary_done"))
    assert _poll(client)["events"][0]["text"].endswith("Resumen listo para decir.")


def test_slow_summary_follows_as_its_own_announcement(
    client, transcript, monkeypatch
):
    monkeypatch.setattr(rm, "_SUMMARY_MAX_WAIT_S", 0.05)
    release = threading.Event()
    rm.set_summarizer(lambda _r: (release.wait(3), "Se arregló todo al final.")[1])
    _stop(client, transcript)
    time.sleep(0.1)

    first = _poll(client)
    assert [e["text"] for e in first["events"]] == ["Claude terminó en openjarvis."]

    release.set()
    _wait_until(lambda: rm._events[0].get("_summary_done"))
    follow = _poll(client, after=first["last_id"])["events"]
    assert [(e["kind"], e["text"]) for e in follow] == [
        ("summary", "Resumen de la sesión en openjarvis: Se arregló todo al final.")
    ]


def test_failed_summary_falls_back_to_first_sentence(client, transcript):
    rm.set_summarizer(lambda _r: "")
    _stop(client, transcript)
    _wait_until(lambda: rm._events[0].get("_summary_done"))
    assert _poll(client)["events"][0]["text"] == (
        "Claude terminó en openjarvis: Restauré google.json y apliqué la "
        "recomendación 1."
    )


def test_without_engine_uses_first_sentence(client, transcript):
    _stop(client, transcript)
    events = _poll(client)["events"]
    assert events[0]["text"].startswith("Claude terminó en openjarvis: Restauré")


@pytest.mark.parametrize(
    "text, ok",
    [
        ("Se corrigieron las fechas de las notas. Reinicia la app.", True),
        ("**Listo**: se corrigió todo el conector.", True),
        ("Hecho.", False),
        ("No veo el transcript de la sesión en tu mensaje, compártelo.", False),
        ("<<<MENSAJE algo MENSAJE>>> resumen de prueba aquí", False),
        ("palabra " * 120, False),
    ],
)
def test_clean_spoken_summary(text, ok):
    assert bool(rm.clean_spoken_summary(text)) is ok


class FakeEngine:
    def __init__(self, answers):
        self.answers = list(answers)
        self.models: List[str] = []

    def generate(self, messages, *, model):
        self.models.append(model)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {"content": answer}


def test_build_summarizer_prefers_haiku_then_server_model():
    engine = FakeEngine([RuntimeError("haiku down"), "Se hizo el cambio pedido ya."])
    summarize = rm.build_summarizer(engine, "claude-cli/opus")
    assert summarize("respuesta final larga") == "Se hizo el cambio pedido ya."
    assert engine.models == ["claude-cli/haiku", "claude-cli/opus"]


def test_build_summarizer_wraps_reply_as_data():
    engine = FakeEngine(["Resumen corto de la sesión."])
    captured = {}
    original = engine.generate

    def spy(messages, *, model):
        captured["user"] = messages[-1].content
        return original(messages, model=model)

    engine.generate = spy
    rm.build_summarizer(engine, "ollama/llama3")("Hice X")
    assert captured["user"].startswith("<<<MENSAJE") and "Hice X" in captured["user"]
    assert engine.models == ["ollama/llama3"]


@pytest.fixture()
def antigravity_transcript(tmp_path: Path) -> Path:
    path = tmp_path / "transcript.jsonl"
    rows = [
        {"type": "USER_INPUT", "content": "hazlo"},
        {"type": "PLANNER_RESPONSE", "content": FINAL_REPLY},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    return path


def test_antigravity_fast_summary_announcement(client, antigravity_transcript):
    def summarize(_reply: str) -> str:
        return "Se arregló Contactos. Tienes que habilitar Drive y Tasks."

    rm.set_summarizer(summarize)
    res = client.post(
        "/v1/coding-sessions/events",
        json={
            "terminationReason": "model_stop",
            "conversationId": "agy-s1",
            "workspacePaths": ["/x/openjarvis/repo"],
            "transcriptPath": str(antigravity_transcript),
        },
    )
    assert res.json()["accepted"] is True
    assert res.json()["id"] is not None
    _wait_until(lambda: rm._events[0].get("_summary_done"))

    events = _poll(client)["events"]
    assert [e["text"] for e in events] == [
        "Antigravity terminó en openjarvis. Se arregló Contactos. "
        "Tienes que habilitar Drive y Tasks."
    ]
    assert events[0]["cli"] == "antigravity"


def test_antigravity_fallback_to_first_sentence(client, antigravity_transcript):
    rm.set_summarizer(lambda _r: "")
    client.post(
        "/v1/coding-sessions/events",
        json={
            "terminationReason": "model_stop",
            "conversationId": "agy-s1",
            "workspacePaths": ["/x/openjarvis/repo"],
            "transcriptPath": str(antigravity_transcript),
        },
    )
    _wait_until(lambda: rm._events[0].get("_summary_done"))
    events = _poll(client)["events"]
    assert events[0]["text"] == (
        "Antigravity terminó en openjarvis: Restauré google.json y apliqué la "
        "recomendación 1."
    )

