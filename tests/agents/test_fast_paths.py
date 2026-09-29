"""Fast paths: simple commands handled with one tool call and no model call."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openjarvis.agents.fast_paths import match_fast_path
from openjarvis.core.types import ToolResult

TOOLS = {"spotify", "open_app", "get_weather", "knowledge_search"}


@pytest.mark.parametrize(
    ("text", "tool", "args"),
    [
        ("Pausa la música", "spotify", {"action": "pause"}),
        ("Jarvis, siguiente canción por favor.", "spotify", {"action": "next"}),
        ("canción anterior", "spotify", {"action": "previous"}),
        ("reanuda la música", "spotify", {"action": "play"}),
        ("pon música de Coldplay", "spotify", {"action": "play", "query": "Coldplay"}),
        (
            "ponme Bohemian Rhapsody en Spotify",
            "spotify",
            {"action": "play", "query": "Bohemian Rhapsody"},
        ),
        ("abre Xcode", "open_app", {"app": "Xcode"}),
        ("Abre Android Studio", "open_app", {"app": "Android Studio"}),
        ("¿Qué clima hace?", "get_weather", {"location": "Bogotá", "units": "metric"}),
        (
            "cómo está el clima en Medellín",
            "get_weather",
            {"location": "Medellín", "units": "metric"},
        ),
    ],
)
def test_matches(text, tool, args):
    path = match_fast_path(text, TOOLS)
    assert path is not None
    assert (path.tool, path.arguments) == (tool, args)


@pytest.mark.parametrize(
    "text",
    [
        "hola jarvis",
        "abre una sesión de Claude Code en openjarvis",
        "abre el proyecto DadoMatch",
        "abre google.com",
        "abre Xcode con openjarvis",
        "abre Xcode y crea un proyecto nuevo",
        "pon un recordatorio para mañana",
        "va a llover mañana en Bogotá?",
        "cuánto tiempo falta para el viernes",
        "busca mis notas sobre la deuda",
    ],
)
def test_does_not_match(text):
    assert match_fast_path(text, TOOLS) is None


def test_tool_must_be_available():
    assert match_fast_path("pausa la música", {"open_app"}) is None


def test_spotify_replies_by_result_code():
    path = match_fast_path("pon música de Coldplay", TOOLS)
    ok = ToolResult("spotify", "Playing", True, metadata={"result": "ok"})
    assert path.reply(ok) == "Reproduciendo Coldplay en Spotify, señor."
    odd = ToolResult("spotify", "Could not control Spotify", False)
    assert path.reply(odd) is None


def test_weather_reply_is_spanish_or_defers():
    path = match_fast_path("qué clima hace", TOOLS)
    good = ToolResult("get_weather", "Bogotá: Sunny +19°C (41% humidity)", True)
    assert path.reply(good) == "En Bogotá está soleado, a 19 °C, señor."
    unknown = ToolResult("get_weather", "Bogotá: Blowing snow -2°C", True)
    assert path.reply(unknown) is None


def test_agent_answers_without_calling_the_model():
    from openjarvis.agents.native_react import NativeReActAgent
    from openjarvis.tools._stubs import BaseTool, ToolSpec

    class _Weather(BaseTool):
        tool_id = "get_weather"

        @property
        def spec(self):
            return ToolSpec(name="get_weather", description="Weather.", parameters={})

        def execute(self, **params):
            return ToolResult("get_weather", "Bogotá: Cloudy +15°C", True)

    engine = MagicMock()
    engine.engine_id = "mock"
    agent = NativeReActAgent(
        engine, "claude-cli/haiku", tools=[_Weather()], temperature=0.2, max_tokens=64
    )
    result = agent.run("¿qué tiempo hace?")
    assert result.content == "En Bogotá está nublado, a 15 °C, señor."
    engine.generate.assert_not_called()
