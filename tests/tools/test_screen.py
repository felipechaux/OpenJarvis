"""look_at_screen (tools/screen.py): capture, vision fallback, fast paths."""

from __future__ import annotations

import pytest

from openjarvis.agents.fast_paths import match_fast_path
from openjarvis.agents.session_guard import TRUSTED_OUTPUT_TOOLS
from openjarvis.engine import quota
from openjarvis.tools import screen
from openjarvis.tools.screen import LookAtScreenTool


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    quota.clear()
    monkeypatch.setattr(screen.sys, "platform", "darwin")
    monkeypatch.setattr(screen, "_has_screen_permission", lambda: True)
    monkeypatch.setattr(screen, "capture", lambda path: path.write_bytes(b"jpg"))
    monkeypatch.setattr(
        screen, "_vision_models",
        lambda: [m for m in ("claude-cli/haiku", "antigravity/flash")
                 if quota.is_available(m)],
    )
    yield
    quota.clear()


class _Engine:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def describe_image(self, image, prompt, *, model):
        self.calls.append((model, image.read_bytes(), prompt))
        answer = self.answers[model]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _use(monkeypatch, answers):
    engine = _Engine(answers)
    monkeypatch.setattr(screen, "_engine_for", lambda model: engine)
    return engine


def test_answers_with_the_first_vision_model(monkeypatch):
    engine = _use(monkeypatch, {"claude-cli/haiku": "Veo Xcode."})
    result = LookAtScreenTool().execute(question="¿qué app es?")
    assert result.success and result.content == "Veo Xcode."
    assert result.metadata["model"] == "claude-cli/haiku"
    model, data, prompt = engine.calls[0]
    assert data == b"jpg" and "¿qué app es?" in prompt
    assert "never instructions" in prompt


def test_quota_error_parks_claude_and_uses_antigravity(monkeypatch):
    _use(monkeypatch, {
        "claude-cli/haiku": RuntimeError("Claude CLI error: usage limit reached"),
        "antigravity/flash": "Veo Safari.",
    })
    result = LookAtScreenTool().execute()
    assert result.content == "Veo Safari."
    assert not quota.is_available("claude-cli/haiku")


def test_all_models_failing_is_reported(monkeypatch):
    _use(monkeypatch, {
        "claude-cli/haiku": RuntimeError("down"),
        "antigravity/flash": RuntimeError("down"),
    })
    result = LookAtScreenTool().execute()
    assert not result.success and "Could not look" in result.content


def test_missing_permission_is_explained(monkeypatch):
    monkeypatch.setattr(screen, "_has_screen_permission", lambda: False)
    result = LookAtScreenTool().execute()
    assert not result.success and "Screen Recording" in result.content


def test_screen_output_is_untrusted():
    assert "look_at_screen" not in TRUSTED_OUTPUT_TOOLS


@pytest.mark.parametrize(
    "text",
    ["¿Qué ves?", "Jarvis, mira mi pantalla", "qué hay en la pantalla",
     "¿qué estoy viendo?", "what do you see on my screen"],
)
def test_voice_commands_hit_the_fast_path(text):
    path = match_fast_path(text, {"look_at_screen"})
    assert path is not None and path.tool == "look_at_screen"


def test_questions_about_the_screen_go_to_the_model():
    assert match_fast_path("mira mi pantalla y arregla el error", {"look_at_screen"}) is None
