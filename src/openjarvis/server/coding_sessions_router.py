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
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from fastapi import APIRouter, Request

router = APIRouter(prefix="/v1/coding-sessions", tags=["coding-sessions"])

_MAX_EVENTS = 100
# Claude runs the Stop hook before its final reply is flushed to the
# transcript, so the summary is read this long after the event instead.
_SUMMARY_DELAY_S = 2.0
_events: Deque[Dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
_ids = itertools.count(1)
_lock = threading.Lock()


def _project(cwd: str) -> str:
    if not cwd:
        return "tu proyecto"
    p = Path(cwd)
    return p.parent.name if p.name == "repo" else p.name


def summarize_reply(text: str, limit: int = 180) -> str:
    """First sentence of Claude's last reply, stripped of markdown, for speech."""
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    text = re.sub(r"[`*#>|]+|__", "", text)  # keep snake_case underscores
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [label](url) → label
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    first = match.group(1) if match else text
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def _last_reply(transcript_path: str) -> str:
    if not transcript_path:
        return ""
    try:
        from openjarvis.tools.coding_sessions import parse_claude_session

        session = parse_claude_session(Path(transcript_path))
    except Exception:  # noqa: BLE001 — a summary is a nice-to-have
        return ""
    return session.last_reply if session else ""


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
            tool = re.search(r"to use (\w+)", message)
            what = f" para usar {tool.group(1)}" if tool else ""
            text = f"Claude pide permiso en {project}{what}."
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
        "announced": False,
        # Stop: summary of the last reply is added once the transcript settles.
        "transcript_path": str(payload.get("transcript_path") or "")
        if kind == "Stop"
        else "",
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


def _finalize(event: Dict[str, Any], now: float) -> bool:
    """Add the reply summary to a Stop event; False while it's too fresh."""
    path = event.get("transcript_path")
    if not path:
        return True
    if now - event["ts"] < _SUMMARY_DELAY_S:
        return False
    summary = summarize_reply(_last_reply(path))
    if summary:
        event["text"] = f"Claude terminó en {event['project']}: {summary}"
    event["transcript_path"] = ""
    return True


@router.get("/events")
async def get_events(after: int = 0) -> Dict[str, Any]:
    now = time.time()
    with _lock:
        new = []
        for e in _events:
            if e["id"] <= after:
                continue
            if not _finalize(e, now):
                break  # keep order: later events wait for this one
            new.append(e)
        last = new[-1]["id"] if new else after
    public = [{k: v for k, v in e.items() if k != "transcript_path"} for e in new]
    return {"events": public, "last_id": last}


@router.post("/events/{event_id}/announced")
async def mark_announced(event_id: int) -> Dict[str, Any]:
    """The app calls this after speaking an event (lets us verify delivery)."""
    with _lock:
        for event in _events:
            if event["id"] == event_id:
                event["announced"] = True
                return {"ok": True}
    return {"ok": False}
