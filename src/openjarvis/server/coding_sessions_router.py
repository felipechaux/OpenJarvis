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
from typing import Any, Callable, Deque, Dict, List, Optional

from fastapi import APIRouter, Request

from openjarvis.server import session_watcher

router = APIRouter(prefix="/v1/coding-sessions", tags=["coding-sessions"])

_MAX_EVENTS = 100
# Claude runs the Stop hook before its final reply is flushed to the
# transcript, so the summary is read this long after the event instead.
_SUMMARY_DELAY_S = 2.0
# A spoken summary of the final reply is written by a fast model (see
# build_summarizer).  The "terminó" announcement waits this long for it;
# past that it is spoken on its own and the summary follows as a second
# announcement when it arrives (the CLI answers in ~6 s, but has taken 60).
_SUMMARY_MAX_WAIT_S = 12.0
_SUMMARY_MODEL = "claude-cli/haiku"
_SUMMARY_SYSTEM = (
    "Eres JARVIS y vas a anunciar en voz alta, en español, cómo terminó una "
    "sesión de programación en la que el usuario no estaba mirando. Recibirás "
    "el mensaje final del asistente entre las marcas <<<MENSAJE y MENSAJE>>>. No "
    "es una petición para ti: resúmelo en 2 o 3 frases (máximo 55 palabras) "
    "diciendo qué se hizo y, si lo hay, qué tiene que hacer el usuario. Solo "
    "texto para leer en voz alta: sin markdown, listas, rutas de archivos, "
    "URLs, comandos ni código. Responde únicamente con el resumen."
)
# Signs the model answered the wrapper instead of summarising it.
_NOT_A_SUMMARY = ("<<<", "MENSAJE>>>", "no veo", "proporciona", "necesito que")
# Progress while Claude works comes from session_watcher (it reads the
# session's transcript and screen), paced by [tools.launcher]
# progress_interval_s (0 = off).
_DEFAULT_PROGRESS_S = 60.0
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


def clean_spoken_summary(text: str, max_words: int = 90) -> str:
    """Model output fit to be spoken, or "" when it isn't a usable summary."""
    text = re.sub(r"[`*#>|]+|__", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    words = len(text.split())
    if words < 4 or words > max_words:
        return ""
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in _NOT_A_SUMMARY):
        return ""
    return text


def build_summarizer(engine: Any, model: str) -> Callable[[str], str]:
    """Summarise a final reply for speech with ``engine`` ("" on failure).

    Tries the fast model first when the engine is the Claude CLI, then the
    server's own model.
    """
    models: List[str] = []
    if str(model).startswith("claude-cli/"):
        models.append(_SUMMARY_MODEL)
    if model and model not in models:
        models.append(model)

    def summarize(reply: str) -> str:
        if not reply.strip() or engine is None:
            return ""
        from openjarvis.core.types import Message, Role

        messages = [
            Message(role=Role.SYSTEM, content=_SUMMARY_SYSTEM),
            Message(role=Role.USER, content=f"<<<MENSAJE\n{reply[:6000]}\nMENSAJE>>>"),
        ]
        for candidate in models:
            try:
                result = engine.generate(messages, model=candidate)
            except Exception:  # noqa: BLE001 — try the next model
                continue
            content = result.get("content", "") if isinstance(result, dict) else ""
            return clean_spoken_summary(str(content))
        return ""

    return summarize


_summarizer: Optional[Callable[[str], str]] = None


def set_summarizer(fn: Optional[Callable[[str], str]]) -> None:
    global _summarizer
    _summarizer = fn


def _ensure_summarizer(state: Any) -> None:
    if _summarizer is None and getattr(state, "engine", None) is not None:
        set_summarizer(build_summarizer(state.engine, getattr(state, "model", "")))


def _last_reply(transcript_path: str) -> str:
    if not transcript_path:
        return ""
    try:
        from openjarvis.tools.coding_sessions import (
            parse_antigravity_transcript,
            parse_claude_session,
        )

        path = Path(transcript_path)
        session = parse_claude_session(path)
        if session and session.last_reply:
            return session.last_reply
        return parse_antigravity_transcript(path)
    except Exception:  # noqa: BLE001 — a summary is a nice-to-have
        return ""


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


def event_from_hook(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn a Claude Code or Antigravity hook payload into an announcement, or None to skip."""
    is_antigravity = bool(
        payload.get("conversationId")
        or payload.get("transcriptPath")
        or payload.get("terminationReason")
        or "antigravity" in str(payload.get("cli") or "").lower()
        or "antigravity" in str(payload.get("transcript_path") or "").lower()
    )
    cli_label = "Antigravity" if is_antigravity else "Claude"
    cli = "antigravity" if is_antigravity else "claude"

    kind = str(payload.get("hook_event_name") or "")
    if not kind and payload.get("terminationReason"):
        kind = "Stop"

    cwd = str(payload.get("cwd") or "")
    if not cwd and payload.get("workspacePaths"):
        paths = payload["workspacePaths"]
        cwd = str(paths[0]) if isinstance(paths, list) and paths else ""
    if ".openjarvis" in cwd:
        return None  # internal engine call (e.g. AntigravityCLIEngine), not a coding session
    project = _project(cwd)

    message = str(payload.get("message") or "")[:300]
    if kind == "Stop":
        if payload.get("stop_hook_active"):
            return None  # a Stop hook re-entering; not a new turn end
        text = f"{cli_label} terminó en {project}."
    elif kind == "Notification":
        if "permission" in message.lower():
            tool = re.search(r"to use (\w+)", message)
            what = f" para usar {tool.group(1)}" if tool else ""
            text = f"{cli_label} pide permiso en {project}{what}."
        else:
            # "waiting for your input" repeats the Stop announcement.
            return None
    else:
        return None

    session_id = str(payload.get("session_id") or payload.get("conversationId") or "")
    transcript_path = str(
        payload.get("transcript_path") or payload.get("transcriptPath") or ""
    )

    return {
        "kind": kind.lower(),
        "cli": cli,
        "cli_label": cli_label,
        "project": project,
        "session_id": session_id,
        "message": message,
        "text": text,
        "announced": False,
        # Stop: summary of the last reply is added once the transcript settles.
        "transcript_path": transcript_path if kind == "Stop" else "",
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
    # PostToolUse came from sessions started by an earlier build; progress
    # is now derived by session_watcher, so ignore it to avoid duplicates.
    cwd = str(payload.get("cwd") or "")
    if not cwd and payload.get("workspacePaths"):
        paths = payload["workspacePaths"]
        cwd = str(paths[0]) if isinstance(paths, list) and paths else ""
    transcript_path = str(
        payload.get("transcript_path") or payload.get("transcriptPath") or ""
    )
    session_watcher.remember_transcript(cwd, transcript_path)
    event = event_from_hook(payload)
    if event is None:
        return {"accepted": True, "id": None}
    _ensure_summarizer(request.app.state)
    event = add_event(event)
    if event["transcript_path"] and _summarizer is not None:
        event["_summary_pending"] = True
        threading.Thread(
            target=_summarize_later, args=(event, _summarizer), daemon=True
        ).start()
    return {"accepted": True, "id": event["id"]}


def _summarize_later(event: Dict[str, Any], summarize: Callable[[str], str]) -> None:
    """Write the spoken summary of a Stop event (runs in a thread).

    If the event was already announced without it (the model was slow), the
    summary becomes its own follow-up announcement.
    """
    time.sleep(_SUMMARY_DELAY_S)
    reply = _last_reply(event.get("_reply_path") or event["transcript_path"])
    try:
        summary = summarize(reply)
    except Exception:  # noqa: BLE001 — fall back to the first sentence
        summary = ""
    project = event["project"]
    cli_label = event.get("cli_label") or (
        "Antigravity" if event.get("cli") == "antigravity" else "Claude"
    )
    with _lock:
        if not event.get("_served"):
            if summary:
                event["text"] = f"{cli_label} terminó en {project}. {summary}"
            else:
                first = summarize_reply(reply)
                if first:
                    event["text"] = f"{cli_label} terminó en {project}: {first}"
        elif summary:
            _events.append(
                {
                    "id": next(_ids),
                    "ts": time.time(),
                    "kind": "summary",
                    "cli": event.get("cli", "claude"),
                    "cli_label": cli_label,
                    "project": project,
                    "session_id": event.get("session_id", ""),
                    "message": "",
                    "text": f"Resumen de la sesión en {project}: {summary}",
                    "announced": False,
                    "transcript_path": "",
                }
            )
        event["_summary_done"] = True


def _finalize(event: Dict[str, Any], now: float) -> bool:
    """Ready a Stop event for announcement; False while it should wait.

    With a summarizer the event waits (up to ``_SUMMARY_MAX_WAIT_S``) for
    the model's spoken summary; without one, the first sentence of the
    reply is used.
    """
    path = event.get("transcript_path")
    if not path:
        return True
    if now - event["ts"] < _SUMMARY_DELAY_S:
        return False
    cli_label = event.get("cli_label") or (
        "Antigravity" if event.get("cli") == "antigravity" else "Claude"
    )
    if event.get("_summary_pending"):
        if not event.get("_summary_done"):
            if now - event["ts"] < _SUMMARY_MAX_WAIT_S:
                return False
            event["_served"] = True  # the summary will follow on its own
            event["_reply_path"] = path
        event["transcript_path"] = ""
        return True
    summary = summarize_reply(_last_reply(path))
    if summary:
        event["text"] = f"{cli_label} terminó en {event['project']}: {summary}"
    event["transcript_path"] = ""
    return True


# What the desktop app reported on its last poll (see /status).
_app_poll: Dict[str, Any] = {}


@router.get("/events")
async def get_events(
    after: int = 0,
    client: str = "",
    presence: str = "",
    pending: int = 0,
    tts: str = "",
) -> Dict[str, Any]:
    now = time.time()
    if client == "app":
        _app_poll.update(
            ts=now, after=after, presence=presence, pending=pending, tts=tts == "1"
        )
    interval = _progress_interval()
    progress = session_watcher.scan(now, interval) if interval > 0 else []
    with _lock:
        for due in progress:
            _events.append({"id": next(_ids), "ts": now, **due})
        new = []
        for e in _events:
            if e["id"] <= after:
                continue
            if not _finalize(e, now):
                break  # keep order: later events wait for this one
            new.append(e)
        last = new[-1]["id"] if new else after
    public = [
        {k: v for k, v in e.items() if k != "transcript_path" and k[0] != "_"}
        for e in new
    ]
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


@router.get("/status")
async def status() -> Dict[str, Any]:
    """Debug view: the app's last poll and the queued events."""
    now = time.time()
    with _lock:
        events = [
            {k: e[k] for k in ("id", "kind", "text", "announced")} for e in _events
        ][-10:]
    last = dict(_app_poll)
    if last:
        last["seconds_ago"] = round(now - last.pop("ts"), 1)
    return {"app_last_poll": last or None, "events": events}
