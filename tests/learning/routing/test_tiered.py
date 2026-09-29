"""Tiered routing: fast model by default, strong model for hard queries."""

from __future__ import annotations

import pytest

from openjarvis.learning.routing.tiered import needs_strong_model, route_tier

FAST = "claude-cli/haiku"
STRONG = "claude-cli/opus"


def _route(query: str, requested: str = STRONG):
    return route_tier(query, requested, fast_model=FAST, strong_model=STRONG)


@pytest.mark.parametrize(
    "query",
    ["hola jarvis", "pon música en spotify", "qué clima hace hoy?", "abre Safari"],
)
def test_simple_queries_go_to_fast_model_with_escalation(query):
    decision = _route(query)
    assert decision.model == FAST
    assert decision.escalation_model == STRONG


@pytest.mark.parametrize(
    "query",
    [
        "analiza la arquitectura de mi proyecto",
        "compara Kotlin Multiplatform con Flutter",
        "explícame paso a paso cómo migrar a Compose",
        "redacta una propuesta para el cliente",
        "why does `val x = foo()` crash?",
        "x" * 600,
    ],
)
def test_hard_queries_go_to_strong_model(query):
    decision = _route(query)
    assert decision.model == STRONG
    assert decision.escalation_model == ""


def test_default_model_request_is_routed():
    assert _route("hola", requested="default").model == FAST


def test_explicit_other_model_is_honoured():
    decision = _route("hola", requested="claude-cli/sonnet")
    assert decision.model == "claude-cli/sonnet"
    assert decision.escalation_model == ""


def test_disabled_without_fast_model():
    decision = route_tier("hola", STRONG, fast_model="", strong_model=STRONG)
    assert decision.model == STRONG
    assert decision.escalation_model == ""



def test_daily_briefing_uses_fast_model_without_escalation():
    decision = _route("JARVIS_WELCOME_TRIGGER: DEBES llamar a get_weather..." + "x" * 900)
    assert decision.model == FAST
    assert decision.escalation_model == ""


@pytest.mark.parametrize(
    "query",
    [
        "¿Qué hablamos tú y yo sobre DadoMatch?",
        "Recuerda que DadoMatch ya está en las tiendas de iOS",
        "modifica el nodo de DadoMatch",
        "¿qué sabes de mí?",
        "what did we talk about yesterday",
    ],
)
def test_memory_requests_use_the_strong_model(query: str) -> None:
    strong, reason = needs_strong_model(query)
    assert strong and reason.startswith("memory")


@pytest.mark.parametrize("query", ["pon música", "¿qué clima hace?", "hola jarvis"])
def test_small_talk_stays_fast(query: str) -> None:
    assert needs_strong_model(query)[0] is False
