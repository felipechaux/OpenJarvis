"""Conversation-history window for chat requests.

The desktop client re-sends the whole conversation on every turn, so long
chats grow the prompt without bound.  :func:`window_history` keeps the most
recent messages verbatim and folds older ones into a short summary written by
the fast model.

Messages are dropped in fixed-size chunks so the dropped prefix — and with it
the cached summary — only changes every ``chunk`` messages; between those
steps each turn reuses the cached summary at no cost.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from typing import Callable, List, Optional, Sequence

from openjarvis.core.types import Message, Role

logger = logging.getLogger(__name__)

SUMMARY_HEADER = "## Earlier in this conversation (summary)"

_SUMMARY_SYSTEM = (
    "Resume la parte anterior de una conversación entre Felipe y su asistente "
    "JARVIS para que el asistente pueda continuarla.  Conserva hechos, "
    "decisiones, nombres, cifras y tareas pendientes; omite saludos y relleno.  "
    "Máximo 150 palabras, en español, prosa compacta sin encabezados."
)

# Older assistant replies (e.g. the long daily briefing) are clipped to this.
_OLD_REPLY_MAX_CHARS = 1200
# Messages at the end of the window that are never clipped.
_UNCLIPPED_TAIL = 4
# Input cap for one summary call.
_SUMMARY_INPUT_MAX_CHARS = 12000
_CACHE_SIZE = 64

Summarizer = Callable[[str, str], str]  # (previous summary, new text) → summary

_cache: "OrderedDict[str, str]" = OrderedDict()


def _key(messages: Sequence[Message]) -> str:
    h = hashlib.sha256()
    for m in messages:
        h.update(m.role.value.encode())
        h.update(b"\0")
        h.update((m.content or "").encode())
        h.update(b"\1")
    return h.hexdigest()


def _render(messages: Sequence[Message]) -> str:
    labels = {Role.USER: "Felipe", Role.ASSISTANT: "JARVIS"}
    return "\n".join(
        f"{labels.get(m.role, m.role.value)}: {m.content or ''}" for m in messages
    )


def _remember(key: str, summary: str) -> str:
    _cache[key] = summary
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return summary


def _summary_for(
    dropped: List[Message], chunk: int, summarize: Summarizer
) -> str:
    key = _key(dropped)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    previous = ""
    new = dropped
    if len(dropped) > chunk:
        prev_key = _key(dropped[:-chunk])
        if prev_key in _cache:
            # Incremental: fold only the newly dropped chunk into the summary.
            previous = _cache[prev_key]
            new = dropped[-chunk:]
    text = _render(new)[-_SUMMARY_INPUT_MAX_CHARS:]
    try:
        summary = summarize(previous, text).strip()
    except Exception:  # noqa: BLE001 — a summary is a nice-to-have
        logger.debug("History summary failed", exc_info=True)
        summary = ""
    return _remember(key, summary) if summary else previous


def _clip(message: Message) -> Message:
    content = message.content or ""
    if message.role != Role.ASSISTANT or len(content) <= _OLD_REPLY_MAX_CHARS:
        return message
    return Message(
        role=message.role,
        content=content[:_OLD_REPLY_MAX_CHARS] + " […]",
        name=message.name,
        tool_call_id=message.tool_call_id,
    )


def window_history(
    messages: List[Message],
    *,
    keep: int,
    chunk: int = 8,
    summarize: Optional[Summarizer] = None,
) -> List[Message]:
    """Return *messages* with old turns summarised and old replies clipped.

    System messages are kept.  Of the rest, between ``keep - chunk`` and
    ``keep`` of the most recent stay verbatim; the older ones become one
    system message holding their summary (or are dropped when *summarize*
    is ``None`` or fails).  ``keep <= 0`` disables the window.
    """
    if keep <= 0:
        return messages
    chunk = max(1, min(chunk, keep))
    system = [m for m in messages if m.role == Role.SYSTEM]
    convo = [m for m in messages if m.role != Role.SYSTEM]

    if len(convo) > keep:
        excess = len(convo) - keep
        n_drop = -(-excess // chunk) * chunk  # round up to a whole chunk
        dropped, convo = convo[:n_drop], convo[n_drop:]
        # Never start the window on an assistant reply.
        while convo and convo[0].role != Role.USER and len(convo) > 1:
            dropped.append(convo.pop(0))
        summary = _summary_for(dropped, chunk, summarize) if summarize else ""
        if summary:
            system.append(
                Message(role=Role.SYSTEM, content=f"{SUMMARY_HEADER}\n\n{summary}")
            )

    tail = len(convo) - _UNCLIPPED_TAIL
    convo = [_clip(m) if i < tail else m for i, m in enumerate(convo)]
    return system + convo


def engine_summarizer(engine, model: str) -> Summarizer:
    """A :data:`Summarizer` backed by ``engine.generate`` on *model*."""

    def summarize(previous: str, text: str) -> str:
        body = text
        if previous:
            body = f"Resumen previo:\n{previous}\n\nContinuación:\n{text}"
        result = engine.generate(
            [
                Message(role=Role.SYSTEM, content=_SUMMARY_SYSTEM),
                Message(role=Role.USER, content=body),
            ],
            model=model,
            temperature=0.2,
            max_tokens=400,
        )
        return str(result.get("content", "")) if isinstance(result, dict) else ""

    return summarize


__all__ = ["SUMMARY_HEADER", "engine_summarizer", "window_history"]
