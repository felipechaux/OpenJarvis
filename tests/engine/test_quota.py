"""Provider quota cooldowns (engine/quota.py)."""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from openjarvis.engine import quota


@pytest.fixture(autouse=True)
def _fresh():
    quota.clear()
    yield
    quota.clear()


@pytest.mark.parametrize(
    "text",
    [
        "Claude CLI error: Claude AI usage limit reached|1759230000",
        "Claude CLI error: You've hit your limit · resets 3pm (America/Bogota)",
        "Claude CLI error: 5-hour limit reached ∙ resets 3pm",
        "Antigravity CLI failed: quota reached for this model on your plan",
        "Gemini CLI failed: RESOURCE_EXHAUSTED",
    ],
)
def test_quota_errors_are_recognised(text):
    assert quota.is_quota_error(text)


@pytest.mark.parametrize("text", ["Claude CLI timed out after 300s", "exit 1: boom"])
def test_outages_are_not_quota_errors(text):
    assert not quota.is_quota_error(text)


def test_epoch_reset():
    assert quota.parse_reset("usage limit reached|1759230000") == 1759230000.0


def test_clock_reset_is_the_next_occurrence():
    now = datetime(2026, 9, 29, 16, 0).timestamp()
    later = quota.parse_reset("resets 5:30pm (America/Bogota)", now)
    assert datetime.fromtimestamp(later) == datetime(2026, 9, 29, 17, 30)
    tomorrow = quota.parse_reset("resets 3pm", now)
    assert datetime.fromtimestamp(tomorrow) == datetime(2026, 9, 30, 15, 0)


def test_cooldown_covers_the_whole_provider_and_expires():
    quota.mark_exhausted("claude-cli/haiku", "usage limit reached")
    assert not quota.is_available("claude-cli/opus")
    assert quota.is_available("antigravity/flash")
    quota._until["claude-cli"] = time.time() - 1
    assert quota.is_available("claude-cli/opus")


def test_unknown_reset_uses_the_default_cooldown():
    until = quota.mark_exhausted("antigravity/flash", "quota")
    assert until == pytest.approx(time.time() + quota.DEFAULT_COOLDOWN_S, abs=5)
