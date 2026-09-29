"""History window: recent turns verbatim, older turns summarised and cached."""

from __future__ import annotations

import pytest

from openjarvis.core.types import Message, Role
from openjarvis.server import history
from openjarvis.server.history import SUMMARY_HEADER, window_history


@pytest.fixture(autouse=True)
def _clear_cache():
    history._cache.clear()


def _convo(n_turns: int, reply: str = "respuesta") -> list[Message]:
    msgs = [Message(role=Role.SYSTEM, content="persona")]
    for i in range(n_turns):
        msgs.append(Message(role=Role.USER, content=f"pregunta {i}"))
        msgs.append(Message(role=Role.ASSISTANT, content=f"{reply} {i}"))
    msgs.append(Message(role=Role.USER, content="última"))
    return msgs


class _Summarizer:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, previous: str, text: str) -> str:
        self.calls.append((previous, text))
        return f"resumen#{len(self.calls)}"


def test_short_conversations_are_untouched():
    msgs = _convo(3)
    assert window_history(msgs, keep=16, summarize=_Summarizer()) == msgs


def test_old_turns_become_a_summary_message():
    summarize = _Summarizer()
    out = window_history(_convo(12), keep=16, summarize=summarize)
    convo = [m for m in out if m.role != Role.SYSTEM]
    assert len(convo) <= 16
    assert convo[0].role == Role.USER
    assert convo[-1].content == "última"
    assert out[0].content == "persona"
    assert out[1].content.startswith(SUMMARY_HEADER)
    assert "pregunta 0" in summarize.calls[0][1]


def test_summary_is_cached_between_turns():
    summarize = _Summarizer()
    msgs = _convo(12)
    window_history(msgs, keep=16, summarize=summarize)
    # Next turn: one more exchange, still inside the same chunk step.
    msgs = msgs + [
        Message(role=Role.ASSISTANT, content="ok"),
        Message(role=Role.USER, content="otra"),
    ]
    window_history(msgs, keep=16, summarize=summarize)
    assert len(summarize.calls) == 1


def test_summary_is_incremental_when_more_is_dropped():
    summarize = _Summarizer()
    window_history(_convo(12), keep=16, summarize=summarize)
    window_history(_convo(16), keep=16, summarize=summarize)
    assert len(summarize.calls) == 2
    previous, text = summarize.calls[1]
    assert previous == "resumen#1"
    assert "pregunta 0" not in text


def test_without_summarizer_old_turns_are_dropped():
    out = window_history(_convo(12), keep=16, summarize=None)
    assert not any(SUMMARY_HEADER in m.content for m in out)
    assert len([m for m in out if m.role != Role.SYSTEM]) <= 16


def test_old_long_replies_are_clipped_but_recent_ones_are_not():
    out = window_history(_convo(6, reply="x" * 3000), keep=16)
    replies = [m.content for m in out if m.role == Role.ASSISTANT]
    assert replies[0].endswith("[…]") and len(replies[0]) < 1300
    assert len(replies[-1]) > 3000


def test_disabled_with_zero_keep():
    msgs = _convo(30)
    assert window_history(msgs, keep=0) is msgs
