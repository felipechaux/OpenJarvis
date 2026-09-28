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
# Progress while Claude works: PostToolUse hooks are gathered per session
# and spoken as one summary — the first after _FIRST_PROGRESS_S of work,
# then at most every progress_interval_s ([tools.launcher], 0 = off).
_FIRST_PROGRESS_S = 20.0
_DEFAULT_PROGRESS_S = 60.0
_progress: Dict[str, Dict[str, Any]] = {}
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


_interval_cache: tuple[float, float] = (0.0, _DEFAULT_PROGRESS_S)


def _progress_interval() -> float:
    """``[tools.launcher] progress_interval_s``, re-read at most every 30 s."""
    global _interval_cache
    checked, value = _interval_cache
    if time.time() - checked < 30:
        return value
    try:
        from openjarvis.core.config import load_config

        raw = getattr(load_config().tools.launcher, "progress_interval_s", None)
        value = float(_DEFAULT_PROGRESS_S if raw is None else raw)
    except Exception:  # noqa: BLE001
        value = _DEFAULT_PROGRESS_S
    _interval_cache = (time.time(), value)
    return value


def describe_action(tool: str, args: Dict[str, Any]) -> tuple[str, str]:
    """``(category, detail)`` for one tool call, used to build a summary."""
    path = args.get("file_path") or args.get("notebook_path") or args.get("path") or ""
    name = Path(str(path)).name if path else ""
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return "edit", name or "un archivo"
    if tool == "Bash":
        desc = str(args.get("description") or "").strip()
        if not desc:
            desc = " ".join(str(args.get("command") or "").split()[:3])
        return "run", desc[:60]
    if tool in ("Read", "Grep", "Glob", "LS"):
        return "read", name
    if tool in ("WebFetch", "WebSearch"):
        return "web", ""
    if tool in ("Task", "Agent"):
        return "agent", str(args.get("description") or "")[:60]
    return "other", tool


def _join(items: list) -> str:
    items = list(dict.fromkeys(i for i in items if i))  # unique, in order
    if len(items) > 3:
        items = items[:3] + [f"{len(items) - 3} más"]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " y " + items[-1]


def summarize_actions(actions: list) -> str:
    """Spanish one-liner for a batch of ``(category, detail)`` actions."""
    by: Dict[str, list] = {}
    for cat, detail in actions:
        by.setdefault(cat, []).append(detail)
    parts = []
    if by.get("edit"):
        parts.append(f"editó {_join(by['edit'])}")
    if by.get("run"):
        parts.append(f"ejecutó {_join(by['run'])}")
    if by.get("agent"):
        parts.append(f"lanzó subagentes ({_join(by['agent'])})")
    if by.get("web"):
        parts.append("consultó la web")
    reads = len(by.get("read", []))
    if reads and not parts:
        parts.append(f"está revisando el código ({reads} lecturas)")
    elif reads:
        parts.append(f"revisó {reads} archivo" + ("s" if reads != 1 else ""))
    if not parts:
        parts.append(f"usó {_join(by.get('other', []))}")
    return _join(parts) if len(parts) > 1 else parts[0]


def _track_progress(payload: Dict[str, Any], now: float) -> Optional[Dict[str, Any]]:
    """Record one PostToolUse; return a progress event when one is due."""
    interval = _progress_interval()
    if interval <= 0:
        return None
    sid = str(payload.get("session_id") or "")
    args = payload.get("tool_input")
    action = describe_action(
        str(payload.get("tool_name") or ""), args if isinstance(args, dict) else {}
    )
    state = _progress.setdefault(
        sid, {"actions": [], "started": now, "last_emit": 0.0, "project": ""}
    )
    state["project"] = _project(str(payload.get("cwd") or ""))
    state["actions"].append(action)
    return _due_progress(sid, state, now, interval)


def _due_progress(
    sid: str, state: Dict[str, Any], now: float, interval: float
) -> Optional[Dict[str, Any]]:
    if not state["actions"]:
        return None
    wait = _FIRST_PROGRESS_S if not state["last_emit"] else interval
    since = now - (state["last_emit"] or state["started"])
    if since < wait:
        return None
    text = f"Claude sigue en {state['project']}: {summarize_actions(state['actions'])}."
    state["actions"] = []
    state["last_emit"] = now
    return {
        "kind": "progress",
        "project": state["project"],
        "session_id": sid,
        "message": "",
        "text": text,
        "announced": False,
        "transcript_path": "",
    }


def event_from_hook(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn a Claude Code hook payload into an announcement, or None to skip."""
    kind = str(payload.get("hook_event_name") or "")
    project = _project(str(payload.get("cwd") or ""))
    message = str(payload.get("message") or "")[:300]
    if kind == "Stop":
        if payload.get("stop_hook_active"):
            return None  # a Stop hook re-entering; not a new turn end
        # The turn is over: its final summary replaces pending progress.
        _progress.pop(str(payload.get("session_id") or ""), None)
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
    if payload.get("hook_event_name") == "PostToolUse":
        with _lock:
            event = _track_progress(payload, time.time())
    else:
        event = event_from_hook(payload)
    if event is None:
        return {"accepted": True, "id": None}
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
    interval = _progress_interval()
    with _lock:
        if interval > 0:
            for sid, state in list(_progress.items()):
                due = _due_progress(sid, state, now, interval)
                if due is not None:
                    _events.append({"id": next(_ids), "ts": now, **due})
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
