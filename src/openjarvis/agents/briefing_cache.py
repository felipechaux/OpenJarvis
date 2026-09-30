"""Warm cache for the daily briefing's data.

The desktop app asks for its once-a-day briefing right after launch, while
its intro plays.  The server starts fetching the briefing's tool calls
(weather, Gmail + Calendar digest) as soon as it boots, so by the time the
briefing prompt arrives the data is ready — or already in flight, in which
case the briefing waits for it instead of fetching twice.

Results are handed out once and only while fresh; anything else falls back
to running the tool normally (see ``NativeReActAgent.run``).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, Optional

from openjarvis.core.types import ToolResult

logger = logging.getLogger(__name__)

# Older than this, the data is refetched (mail keeps arriving).
MAX_AGE_S = 600.0
# How long a briefing waits for a fetch that is still running.
WAIT_S = 45.0

_lock = threading.Lock()
_pending: Dict[str, tuple[float, Future]] = {}
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="briefing-warm")


def _key(name: str, args: Dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"


def _run(name: str, args: Dict[str, Any]) -> ToolResult:
    import openjarvis.tools  # noqa: F401  (registers the tools)
    from openjarvis.core.registry import ToolRegistry

    return ToolRegistry.get(name)().execute(**args)


def warm() -> None:
    """Start fetching the briefing's data in the background."""
    from openjarvis.agents.fast_paths import BRIEFING_CALLS

    with _lock:
        for name, args in BRIEFING_CALLS:
            _pending[_key(name, args)] = (time.monotonic(), _pool.submit(_run, name, args))
    logger.info("briefing cache: warming %s", [n for n, _ in BRIEFING_CALLS])


def take(name: str, args: Dict[str, Any]) -> Optional[ToolResult]:
    """The warmed result for this call (once), or ``None`` to run it live."""
    with _lock:
        entry = _pending.pop(_key(name, args), None)
    if entry is None:
        return None
    started, future = entry
    if time.monotonic() - started > MAX_AGE_S:
        return None
    try:
        result = future.result(timeout=WAIT_S)
    except Exception as exc:  # failed or too slow: fetch live instead
        logger.warning("briefing cache: %s unavailable (%s)", name, exc)
        return None
    return result if result.success else None


def clear() -> None:
    """Forget every warmed result (tests)."""
    with _lock:
        _pending.clear()


__all__ = ["MAX_AGE_S", "clear", "take", "warm"]
