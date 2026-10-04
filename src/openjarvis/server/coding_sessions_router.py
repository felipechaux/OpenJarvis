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


def build_summarizer(
    engine: Any, model: str, fast_model: str = ""
) -> Callable[[str], str]:
    """Summarise a final reply for speech with ``engine`` ("" on failure).

    Tries the fast model first (``fast_model`` from config, else Haiku when
    the engine is the Claude CLI), then the server's own model.
    """
    models: List[str] = []
    if fast_model:
        models.append(fast_model)
    elif str(model).startswith("claude-cli/"):
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
        config = getattr(state, "config", None)
        fast_model = getattr(getattr(config, "intelligence", None), "fast_model", "")
        set_summarizer(
            build_summarizer(state.engine, getattr(state, "model", ""), fast_model)
        )


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


def _hook_cli(payload: Dict[str, Any]) -> str:
    """ "antigravity" for ``agy`` hook payloads (camelCase keys), else "claude"."""
    agy_keys = (
        "conversationId",
        "transcriptPath",
        "terminationReason",
        "workspacePaths",
    )
    return "antigravity" if any(k in payload for k in agy_keys) else "claude"


def _hook_kind(payload: Dict[str, Any], declared: str = "") -> str:
    """Hook event name: the one the hook command declared, else the payload's."""
    kind = declared or str(payload.get("hook_event_name") or "")
    if not kind and payload.get("terminationReason"):
        kind = "Stop"
    return kind


def _hook_cwd(payload: Dict[str, Any]) -> str:
    cwd = str(payload.get("cwd") or "")
    paths = payload.get("workspacePaths")
    if not cwd and isinstance(paths, list) and paths:
        cwd = str(paths[0])
    return cwd.removeprefix("file://")


def _hook_transcript(payload: Dict[str, Any]) -> str:
    path = str(payload.get("transcript_path") or payload.get("transcriptPath") or "")
    if not path and payload.get("conversationId"):
        path = session_watcher.antigravity_transcript(str(payload["conversationId"]))
    return path


def _is_jarvis_agy_session(cwd: str) -> bool:
    """Whether ``cwd`` belongs to a live ``jarvis-*-agy`` tmux session.

    The agy hooks live in its global config, so they fire for every
    Antigravity run — the user's own and the server's AntigravityCLIEngine
    calls too.  Only sessions JARVIS started are announced.
    """
    if not cwd:
        return False
    from openjarvis.tools import session_control as sc

    return sc.session_name(Path(cwd), "antigravity") in {
        name for name, _ in sc.list_sessions()
    }


def event_from_hook(
    payload: Dict[str, Any], hook: str = "", cli: str = ""
) -> Optional[Dict[str, Any]]:
    """Turn a Claude Code / Antigravity hook payload into an announcement.

    ``hook`` / ``cli`` are what the hook command declared in its
    ``X-OpenJarvis-Hook`` / ``X-OpenJarvis-CLI`` headers (the agy hooks
    do); without them both are inferred from the payload.  Returns None
    when there is nothing to announce.
    """
    cli = cli or _hook_cli(payload)
    cli_label = "Antigravity" if cli == "antigravity" else "Claude"
    kind = _hook_kind(payload, hook)
    project = _project(_hook_cwd(payload))

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
    transcript_path = _hook_transcript(payload)

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
    hook = request.headers.get("x-openjarvis-hook", "")
    cli = request.headers.get("x-openjarvis-cli", "") or _hook_cli(payload)
    cwd = _hook_cwd(payload)
    if cli == "antigravity" and not _is_jarvis_agy_session(cwd):
        return {"accepted": True, "id": None}
    # PostToolUse only teaches session_watcher the transcript (agy hooks send
    # it; Claude's came from an earlier build) — progress is derived there.
    session_watcher.remember_transcript(cwd, _hook_transcript(payload), cli)
    event = event_from_hook(payload, hook, cli)
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
    public, last = events_after(after, now)
    return {"events": public, "last_id": last}


def events_after(
    after: int, now: float | None = None
) -> tuple[List[Dict[str, Any]], int]:
    """Announceable events newer than ``after`` and the last id served.

    Shared by the desktop poll and the mobile channel (``/v1/mobile/ws``).
    """
    now = time.time() if now is None else now
    with _lock:
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
    return public, last


def latest_event_id() -> int:
    with _lock:
        return _events[-1]["id"] if _events else 0


@router.post("/events/{event_id}/announced")
async def mark_announced(event_id: int) -> Dict[str, Any]:
    """The app calls this after speaking an event (lets us verify delivery)."""
    with _lock:
        for event in _events:
            if event["id"] == event_id:
                event["announced"] = True
                return {"ok": True}
    return {"ok": False}


@router.get("/live")
async def live_sessions() -> Dict[str, Any]:
    """Live JARVIS sessions with their state and any pending prompt (notch)."""
    import asyncio

    return {"sessions": await asyncio.to_thread(session_watcher.live)}


@router.post("/{name}/answer")
async def answer_prompt(name: str, request: Request) -> Dict[str, Any]:
    """Answer the menu a session is waiting on, from the notch.

    Body: ``{"prompt_id": …, "key": "1" | "2" | … | "Escape"}``.  The key is
    pressed only if that same prompt is still on screen, so a stale click
    never types a digit into the session's input box.  JSON content type is
    required: it forces a CORS preflight, so other web pages cannot answer.
    """
    body = await _json_body(request)
    if body is None:
        return {"ok": False, "error": "invalid body"}
    return await answer_session_prompt(
        name, body.get("prompt_id"), str(body.get("key") or "")
    )


async def answer_session_prompt(name: str, prompt_id: Any, key: str) -> Dict[str, Any]:
    """Press ``key`` on the menu ``name`` shows, if it is still ``prompt_id``."""
    import asyncio

    from openjarvis.tools import session_control as sc

    if not sc.has_session(name):
        return {"ok": False, "error": "no such session"}
    screen = await asyncio.to_thread(sc.capture_screen, name, 40)
    prompt = session_watcher.parse_prompt(screen)
    if not prompt or prompt["id"] != prompt_id:
        return {"ok": False, "error": "prompt no longer showing"}
    if key not in {o["key"] for o in prompt["options"]}:
        return {"ok": False, "error": "not an option of this prompt"}
    await asyncio.to_thread(sc.press_keys, name, [key])
    return {"ok": True}


async def _json_body(request: Request) -> Dict[str, Any] | None:
    """The body of a notch request, or None.  JSON content type is required
    so other web pages (no CORS preflight allowed) cannot drive sessions."""
    if not request.headers.get("content-type", "").startswith("application/json"):
        return None
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return None
    return body if isinstance(body, dict) else None


@router.post("/{name}/message")
async def message_session(name: str, request: Request) -> Dict[str, Any]:
    """Type the user's message into a session, from the notch's text box.

    Body: ``{"text": …}``.  Refused while the session shows a menu: Enter
    would pick its highlighted option instead of sending the text.
    """
    body = await _json_body(request)
    text = str((body or {}).get("text") or "").strip()
    if body is None or not text:
        return {"ok": False, "error": "invalid body"}
    return await message_to_session(name, text)


async def message_to_session(name: str, text: str) -> Dict[str, Any]:
    """Type ``text`` into session ``name`` unless it is showing a menu."""
    import asyncio

    from openjarvis.tools import session_control as sc

    text = text.strip()
    if not text:
        return {"ok": False, "error": "empty message"}
    if not sc.has_session(name):
        return {"ok": False, "error": "no such session"}
    screen = await asyncio.to_thread(sc.capture_screen, name, 40)
    if session_watcher.parse_prompt(screen):
        return {"ok": False, "error": "the session is waiting on a menu"}
    await asyncio.to_thread(sc.type_message, name, text[:4000])
    return {"ok": True}


@router.post("/{name}/open")
async def open_session(name: str, request: Request) -> Dict[str, Any]:
    """Open the user's terminal attached to a session (notch chip)."""
    import asyncio

    from openjarvis.tools import session_control as sc
    from openjarvis.tools.launcher import _launcher_config, _run_in_terminal

    if await _json_body(request) is None:
        return {"ok": False, "error": "invalid body"}
    if not sc.has_session(name):
        return {"ok": False, "error": "no such session"}
    terminal = getattr(_launcher_config(), "terminal", "") or "Terminal"
    try:
        await asyncio.to_thread(_run_in_terminal, terminal, sc.attach_command(name))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True}


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
