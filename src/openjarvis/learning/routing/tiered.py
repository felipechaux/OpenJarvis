"""Tiered model routing — a cheap model by default, the strong one on demand.

Most assistant traffic (greetings, quick facts, "open Spotify", "what's the
weather") does not need a frontier model.  :func:`route_tier` sends those
queries to ``fast_model`` and reserves ``strong_model`` for queries that look
hard.  When the fast model is chosen, the strong model is returned as the
*escalation* model so the agent can still hand the turn over mid-flight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from openjarvis.learning.routing.complexity import score_complexity

# Requests that deserve the strong model outright (Spanish + English).
_DEEP_PATTERNS = re.compile(
    r"\banaliz\w*|\ban[aá]lisis\b|\bcompar[ae]\w*|\beval[uú]\w*"
    r"|\bestrategi\w*|\bplanific\w*|\bplan de\b|\bdise[ñn]\w*|\barquitectur\w*"
    r"|\brefactori\w*|\bdepur\w*|\bdebug\w*|\binvestig\w*|\brazon\w*"
    r"|\bpaso a paso\b|\bpros y contras\b|\bventajas y desventajas\b"
    r"|\ba fondo\b|\ben profundidad\b|\bdetalladamente\b"
    r"|\bredact\w*|\bescrib\w* (?:un|una) (?:ensayo|art[ií]culo|informe|documento|propuesta|correo)"
    r"|\banaly[sz]\w*|\bcompare\b|\bevaluate\b|\bstrategy\b|\barchitecture\b"
    r"|\brefactor\w*|\bstep[- ]by[- ]step\b|\bpros and cons\b|\btrade-?offs?\b"
    r"|\bin depth\b|\bwrite (?:an? )?(?:essay|article|report|proposal|document)\b",
    re.IGNORECASE,
)

# Memory and personal-knowledge requests go to the strong model: the fast
# one claimed to have saved facts it never saved, and repeated retrieved
# past replies as if they were its own answer.
_MEMORY_PATTERNS = re.compile(
    r"\brecuerd\w*|\bacu[eé]rdate\b|\bmemoriz\w*|\bolvid\w*|\banot[ae]\w*"
    r"|\bapunt[ae]\w*|\bguard[ae]\w*"
    r"|\bqu[eé] (?:hablamos|dijimos|charlamos|conversamos|platicamos)\b"
    r"|\b(?:hablamos|conversamos|charlamos) (?:de|sobre)\b"
    r"|\bsobre m[ií]\b|\bde m[ií]\b|\bmi perfil\b|\bmis datos\b"
    r"|\bnodos?\b|\bgrafo\b"
    r"|\bremember\w*|\bforget\b|\babout me\b|\bmy profile\b"
    r"|\bwhat did (?:we|i) (?:talk|discuss|say)\w*",
    re.IGNORECASE,
)

# The desktop app's daily briefing prompt: long, but it only asks for a
# summary of tool output, which the fast model handles.
from openjarvis.agents.fast_paths import BRIEFING_MARKER as _BRIEFING_MARKER  # noqa: E402

_STRONG_TIERS = frozenset({"moderate", "complex", "very_complex"})
_LONG_QUERY_CHARS = 500


@dataclass(frozen=True)
class TierDecision:
    """Result of :func:`route_tier`."""

    model: str
    escalation_model: str  # "" when the chosen model cannot escalate
    reason: str


def needs_strong_model(query: str) -> tuple[bool, str]:
    """Return ``(True, reason)`` when *query* looks too hard for a fast model."""
    if len(query) > _LONG_QUERY_CHARS:
        return True, "long query"
    result = score_complexity(query)
    if result.tier in _STRONG_TIERS:
        return True, f"complexity tier {result.tier}"
    if result.signals.get("has_code"):
        return True, "code"
    match = _DEEP_PATTERNS.search(query)
    if match:
        return True, f"keyword {match.group(0)!r}"
    match = _MEMORY_PATTERNS.search(query)
    if match:
        return True, f"memory {match.group(0)!r}"
    return False, f"complexity tier {result.tier}"


def route_tier(
    query: str,
    requested_model: str,
    *,
    fast_model: str,
    strong_model: str,
) -> TierDecision:
    """Pick the model for *query*.

    Routing only applies when the caller asked for the strong model (or for
    no model in particular); an explicit pick of any other model is honoured.
    """
    if not fast_model or not strong_model:
        return TierDecision(requested_model, "", "routing disabled")
    if requested_model not in ("", "default", strong_model):
        return TierDecision(requested_model, "", "explicit model")
    if query.lstrip().startswith(_BRIEFING_MARKER):
        return TierDecision(fast_model, "", "daily briefing")
    strong, reason = needs_strong_model(query)
    if strong:
        return TierDecision(strong_model, "", reason)
    return TierDecision(fast_model, strong_model, reason)


__all__ = ["TierDecision", "needs_strong_model", "route_tier"]
