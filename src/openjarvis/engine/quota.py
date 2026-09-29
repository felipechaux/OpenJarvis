"""Provider quota cooldowns — skip a subscription that ran out until it resets.

When the Claude or Antigravity subscription hits its usage limit, every call
to it fails until the plan resets (hours later).  Without memory, each turn
would first burn a failing call on the exhausted provider before falling back.
This module remembers, per provider, when it may be tried again.

Providers are the model-id prefix: ``claude-cli/haiku`` → ``claude-cli``,
``antigravity/gemini-3.8-flash-medium`` → ``antigravity``.

State is process-wide and in memory: a server restart forgets it, which only
costs one failing call.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Used when the error names no reset time.
DEFAULT_COOLDOWN_S = 30 * 60
# Never trust a parsed reset further out than this (weekly limits included).
MAX_COOLDOWN_S = 7 * 24 * 3600

_QUOTA_RE = re.compile(
    r"usage limit|limit reached|hit your limit|out of (extra )?usage|"
    r"RESOURCE_EXHAUSTED|exhausted your|quota|rate.?limit|\b429\b|"
    r"too many requests",
    re.I,
)
# Claude CLI (older): "Claude AI usage limit reached|1759230000"
_EPOCH_RE = re.compile(r"limit reached\|(\d{10})")
# Claude CLI (newer): "… resets 3pm", "resets 3:30pm (America/Bogota)",
# "resets Oct 2, 9am".  Only the clock time is read; a date part pushes the
# reset out by whole days via the "next occurrence" rule below at worst.
_RESETS_RE = re.compile(r"resets?\s+(?:at\s+)?(?:[A-Za-z]{3,9}\s+\d{1,2},?\s+)?"
                        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
_RETRY_AFTER_RE = re.compile(r"retry (?:in|after)\s+(\d+(?:\.\d+)?)\s*(s|sec|m|min|h)", re.I)

_lock = threading.Lock()
_until: Dict[str, float] = {}


def provider_of(model: str) -> str:
    """``claude-cli/haiku`` → ``claude-cli``; a bare id is its own provider."""
    return model.split("/", 1)[0] if "/" in model else model


def is_quota_error(text: str) -> bool:
    return bool(_QUOTA_RE.search(text or ""))


def parse_reset(text: str, now: Optional[float] = None) -> Optional[float]:
    """Epoch seconds when the limit named in *text* resets, if it says."""
    now = time.time() if now is None else now
    if m := _EPOCH_RE.search(text):
        return float(m.group(1))
    if m := _RETRY_AFTER_RE.search(text):
        n, unit = float(m.group(1)), m.group(2).lower()
        mult = 3600 if unit == "h" else 60 if unit.startswith("m") else 1
        return now + n * mult
    if m := _RESETS_RE.search(text):
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        minute = int(m.group(2) or 0)
        base = datetime.fromtimestamp(now)
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += timedelta(days=1)
        return target.timestamp()
    return None


def mark_exhausted(model: str, error_text: str = "") -> float:
    """Put *model*'s provider on cooldown; returns when it may be retried."""
    now = time.time()
    until = parse_reset(error_text, now) or now + DEFAULT_COOLDOWN_S
    until = min(max(until, now + 60), now + MAX_COOLDOWN_S)
    provider = provider_of(model)
    with _lock:
        _until[provider] = until
    logger.warning(
        "quota: %s exhausted until %s",
        provider, datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M"),
    )
    return until


def park(model: str) -> None:
    """Keep *model*'s provider out of use until :func:`clear` (user's choice)."""
    with _lock:
        _until[provider_of(model)] = float("inf")


def parked() -> Dict[str, float]:
    """Providers currently out of use → when they return (inf = until cleared)."""
    now = time.time()
    with _lock:
        for provider in [p for p, t in _until.items() if t <= now]:
            del _until[provider]
        return dict(_until)


def cooldown_until(model: str) -> Optional[float]:
    """When *model*'s provider may be tried again, or None if it is available."""
    provider = provider_of(model)
    with _lock:
        until = _until.get(provider)
        if until is None:
            return None
        if until <= time.time():
            del _until[provider]
            return None
        return until


def is_available(model: str) -> bool:
    return cooldown_until(model) is None


def clear(model: str = "") -> None:
    """Forget one provider's cooldown (or all, with no argument)."""
    with _lock:
        if model:
            _until.pop(provider_of(model), None)
        else:
            _until.clear()


__all__ = [
    "clear",
    "cooldown_until",
    "is_available",
    "is_quota_error",
    "mark_exhausted",
    "park",
    "parked",
    "parse_reset",
    "provider_of",
]
