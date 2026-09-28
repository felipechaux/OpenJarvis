"""Events from Claude Code sessions started by JARVIS.

Sessions launched by ``start_coding_session`` carry Stop / Notification
hooks (see ``tools/session_control.py``) that POST their payload here.  The
desktop app polls ``GET /v1/coding-sessions/events`` and speaks each new
event ("Claude terminó en openjarvis"), so the user hears about progress
without asking.

Events live in memory only — they are transient announcements.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from fastapi import APIRouter, Request

router = APIRouter(prefix="/v1/coding-sessions", tags=["coding-sessions"])

_MAX_EVENTS = 100
_events: Deque[Dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
_ids = itertools.count(1)
_lock = threading.Lock()


def _project(cwd: str) -> str:
    if not cwd:
        return "tu proyecto"
    p = Path(cwd)
    return p.parent.name if p.name == "repo" else p.name


def event_from_hook(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn a Claude Code hook payload into an announcement, or None to skip."""
    kind = str(payload.get("hook_event_name") or "")
    project = _project(str(payload.get("cwd") or ""))
    message = str(payload.get("message") or "")[:300]
    if kind == "Stop":
        if payload.get("stop_hook_active"):
            return None  # a Stop hook re-entering; not a new turn end
        text = f"Claude terminó en {project}."
    elif kind == "Notification":
        if "permission" in message.lower():
            text = f"Claude pide permiso en {project}."
        else:
            # "waiting for your input" repeats the Stop announcement.
            return None
    else:
        return None
    return {
        "kind": kind.lower(),
        "project": project,
        "session_id": str(payload.get("session_id") or ""),
        "message": message,
        "text": text,
    }


def add_event(event: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        event = {"id": next(_ids), "ts": time.time(), **event}
        _events.append(event)
    return event


@router.post("/events")
async def post_event(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — hooks must never see an error
        return {"accepted": False}
    if not isinstance(payload, dict):
        return {"accepted": False}
    event = event_from_hook(payload)
    if event is None:
        return {"accepted": False}
    return {"accepted": True, "id": add_event(event)["id"]}


@router.get("/events")
async def get_events(after: int = 0) -> Dict[str, Any]:
    with _lock:
        new = [e for e in _events if e["id"] > after]
        last = _events[-1]["id"] if _events else 0
    return {"events": new, "last_id": last}
