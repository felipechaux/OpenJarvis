"""prompt_refiner: spoken instructions → clear prompts for coding sessions."""

from __future__ import annotations

import pytest

from openjarvis.engine import quota
from openjarvis.tools import prompt_refiner as pr


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    quota.clear()
    monkeypatch.setattr(pr, "refine_enabled", lambda: True)
    monkeypatch.setattr(
        pr, "_models", lambda: ["claude-cli/haiku", "antigravity/gemini-3.8-flash-medium"]
    )
    yield
    quota.clear()


def _engines(monkeypatch, answers, seen):
    """Fake engines: answers[model] is text, or an Exception to raise."""

    class _E:
        def __init__(self, model):
            self.model = model

        def generate(self, messages, model):
            seen.append((model, messages))
            answer = answers[model]
            if isinstance(answer, Exception):
                raise answer
            return {"content": answer}

    monkeypatch.setattr(pr, "_engine", lambda model: _E(model))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sí", False),
        ("continúa", False),
        ("detente ya por favor", False),
        ("arregla lo del login que falla", True),
        ("sí, pero cambia el color del botón de enviar a azul", True),
        ("x " * 900, False),
    ],
)
def test_what_gets_refined(text, expected):
    assert pr.should_refine(text) is expected


def test_refined_prompt_keeps_the_users_words(monkeypatch):
    seen = []
    _engines(monkeypatch, {"claude-cli/haiku": "Objetivo: arreglar el login."}, seen)
    out = pr.refine("arregla lo del login que falla", project="dadomatch", screen="Error: 401")
    assert out.startswith("Objetivo: arreglar el login.")
    assert "«arregla lo del login que falla»" in out
    sent = str(seen[0][1])
    assert "dadomatch" in sent and "Error: 401" in sent and "no instrucciones" in sent


def test_antigravity_takes_over_when_claude_is_out_of_quota(monkeypatch):
    quota.park("claude-cli/")
    seen = []
    _engines(monkeypatch, {"antigravity/gemini-3.8-flash-medium": "Objetivo: X."}, seen)
    out = pr.refine("arregla lo del login que falla", project="p")
    assert out.startswith("Objetivo: X.")
    assert [m for m, _ in seen] == ["antigravity/gemini-3.8-flash-medium"]
    # The Antigravity CLI takes no system prompt: rules travel in one message.
    assert len(seen[0][1]) == 1 and "Reescribes instrucciones" in seen[0][1][0].content


def test_quota_error_parks_claude_and_falls_back(monkeypatch):
    seen = []
    _engines(
        monkeypatch,
        {
            "claude-cli/haiku": RuntimeError("usage limit reached"),
            "antigravity/gemini-3.8-flash-medium": "Objetivo: Y.",
        },
        seen,
    )
    assert pr.refine("arregla lo del login que falla", project="p").startswith("Objetivo: Y.")
    assert not quota.is_available("claude-cli/haiku")


def test_nobody_answers_means_send_the_original(monkeypatch):
    seen = []
    _engines(
        monkeypatch,
        {
            "claude-cli/haiku": RuntimeError("down"),
            "antigravity/gemini-3.8-flash-medium": RuntimeError("down"),
        },
        seen,
    )
    assert pr.refine("arregla lo del login que falla", project="p") is None


def test_short_replies_skip_the_model(monkeypatch):
    seen = []
    _engines(monkeypatch, {}, seen)
    assert pr.refine("sí", project="p") is None and seen == []
