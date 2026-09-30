"""briefing_cache: the daily briefing's data warmed at server boot."""

from __future__ import annotations

import pytest

from openjarvis.agents import briefing_cache
from openjarvis.core.types import ToolResult

WEATHER = ("get_weather", {"location": "Bogotá", "units": "metric"})


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    briefing_cache.clear()
    calls = []

    def fake_run(name, args):
        calls.append(name)
        return ToolResult(name, f"{name} data", True)

    monkeypatch.setattr(briefing_cache, "_run", fake_run)
    yield calls
    briefing_cache.clear()


def test_warm_fetches_every_briefing_call_and_hands_each_out_once(_fresh):
    briefing_cache.warm()
    result = briefing_cache.take(*WEATHER)
    assert result is not None and result.content == "get_weather data"
    assert briefing_cache.take(*WEATHER) is None  # consumed
    digest = briefing_cache.take("digest_collect", {"sources": ["gcalendar", "gmail"]})
    assert digest is not None and digest.content == "digest_collect data"
    assert sorted(_fresh) == ["digest_collect", "get_weather"]


def test_nothing_warmed_means_run_live():
    assert briefing_cache.take(*WEATHER) is None


def test_stale_results_are_not_used(monkeypatch):
    briefing_cache.warm()
    monkeypatch.setattr(briefing_cache, "MAX_AGE_S", -1.0)
    assert briefing_cache.take(*WEATHER) is None


def test_failed_fetch_falls_back_to_live(monkeypatch):
    monkeypatch.setattr(briefing_cache, "_run", lambda n, a: ToolResult(n, "boom", False))
    briefing_cache.warm()
    assert briefing_cache.take(*WEATHER) is None


def test_briefing_uses_the_warmed_data_without_running_tools():
    from unittest.mock import MagicMock

    from openjarvis.agents.native_react import NativeReActAgent
    from openjarvis.tools._stubs import BaseTool, ToolSpec

    ran = []

    def _tool(name):
        class _T(BaseTool):
            tool_id = name

            @property
            def spec(self):
                return ToolSpec(name=name, description=name, parameters={})

            def execute(self, **params):
                ran.append(name)
                return ToolResult(name, "live", True)

        return _T()

    briefing_cache.warm()
    engine = MagicMock()
    engine.engine_id = "mock"
    engine.generate.return_value = {"content": "Final Answer: Listo.", "usage": {}}
    agent = NativeReActAgent(
        engine, "claude-cli/haiku", tools=[_tool("get_weather"), _tool("digest_collect")]
    )
    agent.run("JARVIS_WELCOME_TRIGGER: briefing")
    assert ran == []
    sent = str(engine.generate.call_args)
    assert "Observation: get_weather data" in sent and "Observation: digest_collect data" in sent
