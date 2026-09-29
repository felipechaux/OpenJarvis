"""Manual provider switching (tools/model_switch.py) and its voice fast paths."""

from __future__ import annotations

import pytest

from openjarvis.agents.fast_paths import match_fast_path
from openjarvis.engine import quota
from openjarvis.tools import model_switch
from openjarvis.tools.model_switch import ModelSwitchTool

CHAIN = ["claude-cli/haiku", "antigravity/flash", "gemini-cli/flash"]


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    quota.clear()
    monkeypatch.setattr(model_switch, "_chain", lambda: list(CHAIN))
    yield
    quota.clear()


def test_use_gemini_parks_claude_until_use_claude():
    tool = ModelSwitchTool()
    result = tool.execute(action="use_gemini")
    assert not quota.is_available("claude-cli/opus")
    assert "Antigravity" in result.content
    assert "vuelve a Claude" in result.content
    tool.execute(action="use_claude")
    assert quota.is_available("claude-cli/haiku")


def test_use_claude_also_lifts_a_quota_cooldown():
    quota.mark_exhausted("claude-cli/haiku", "usage limit reached")
    ModelSwitchTool().execute(action="use_claude")
    assert quota.is_available("claude-cli/haiku")


def test_status_names_active_and_paused():
    quota.mark_exhausted("claude-cli/haiku", "usage limit reached")
    content = ModelSwitchTool().execute(action="status").content
    assert content.startswith("Ahora respondo con Antigravity")
    assert "Claude en pausa hasta las" in content


@pytest.mark.parametrize(
    "text, action",
    [
        ("Jarvis, usa Gemini", "use_gemini"),
        ("cámbiate a antigravity por favor", "use_gemini"),
        ("vuelve a Claude", "use_claude"),
        ("usa claude", "use_claude"),
        ("¿qué modelo estás usando?", "status"),
        ("cuál modelo usas ahora", "status"),
    ],
)
def test_voice_commands_hit_the_fast_path(text, action):
    path = match_fast_path(text, {"model_switch"})
    assert path is not None and path.arguments == {"action": action}


def test_longer_requests_go_to_the_model():
    assert match_fast_path("usa gemini para resumir mi correo", {"model_switch"}) is None
