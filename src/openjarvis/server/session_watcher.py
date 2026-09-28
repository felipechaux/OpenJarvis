"""Watch JARVIS-started Claude sessions and narrate their progress.

For every live ``jarvis-*`` Claude tmux session the watcher:

* reads new entries in that session's own transcript and turns tool calls
  into actions — edited files, commands, reads.  The transcript is the one
  its Stop/Notification hooks reported, else the file of the
  ``--session-id`` JARVIS launched it with.  It is never guessed from "the
  newest file in the project folder": other Claude sessions (e.g. the
  user's own, in the same folder) write there too;
* checks the terminal screen: Claude shows "esc to interrupt" only while
  it is working, and its spinner line carries the elapsed time.

While a session works it yields one progress event per interval: the
actions since the last update, or — during long thinking with no tool
calls — a heartbeat with the elapsed time.  Nothing is replayed from before
the watcher first saw a transcript.

Unlike hooks this needs no changes to how Claude was started, so it also
covers sessions launched before a hooks update.  ``scan`` is called from
the events endpoint the app polls; it rate-limits itself.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_WORKING_MARK = "esc to interrupt"
_FIRST_PROGRESS_S = 20.0
_HEARTBEAT_FACTOR = 3  # heartbeats are this many intervals apart
_SCAN_EVERY_S = 4.0
_MAX_READ = 2 * 1024 * 1024

_state: Dict[str, Dict[str, Any]] = {}
# tmux session name → transcript path, learned from hook payloads.
_transcripts: Dict[str, str] = {}
_last_scan = 0.0


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


def _elapsed_from_screen(screen: str) -> Optional[int]:
    """Seconds from Claude's spinner, e.g. "(12m 28s · ↓ 25.1k tokens)"."""
    m = re.search(r"\((?:(\d+)h\s*)?(?:(\d+)m\s*)?(\d+)s\s*·", screen)
    if not m:
        return None
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + se


def _spoken_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "menos de un minuto"
    if minutes == 1:
        return "1 minuto"
    if minutes < 60:
        return f"{minutes} minutos"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min"


def remember_transcript(cwd: str, transcript_path: str) -> None:
    """Called for every hook: hooks only come from JARVIS-started sessions."""
    if not cwd or not transcript_path:
        return
    from openjarvis.tools import session_control as sc

    _transcripts[sc.session_name(Path(cwd))] = transcript_path


def _transcript_for(name: str, launch_dir: str) -> Optional[Path]:
    from openjarvis.tools import coding_sessions as cs
    from openjarvis.tools import session_control as sc

    known = _transcripts.get(name)
    if known and Path(known).exists():
        return Path(known)
    sid = sc.claude_session_id(name)
    if sid and launch_dir:
        path = cs.CLAUDE_DIR / cs._claude_slug(launch_dir) / f"{sid}.jsonl"
        if path.exists():
            return path
    return None


def read_new_actions(st: Dict[str, Any], path: Path) -> List[Tuple[str, str]]:
    """Tool calls appended to ``path`` since the last read.

    The first time a transcript is seen only its end is recorded, so old
    history is never narrated.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if st.get("path") != str(path) or size < st.get("offset", 0):
        st.update(path=str(path), offset=size, partial="")
        return []
    if size == st["offset"]:
        return []
    with path.open("rb") as fh:
        fh.seek(st["offset"])
        chunk = fh.read(min(size - st["offset"], _MAX_READ))
    st["offset"] += len(chunk)
    text = st.get("partial", "") + chunk.decode("utf-8", errors="ignore")
    lines = text.split("\n")
    st["partial"] = lines.pop()  # keep an unfinished last line
    actions = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                args = block.get("input")
                actions.append(
                    describe_action(
                        str(block.get("name") or ""),
                        args if isinstance(args, dict) else {},
                    )
                )
    return actions


def _progress_event(name: str, project: str, text: str) -> Dict[str, Any]:
    return {
        "kind": "progress",
        "project": project,
        "session_id": name,  # stable per tmux session, used to supersede
        "message": "",
        "text": text,
        "announced": False,
        "transcript_path": "",
    }


def step(
    name: str,
    project: str,
    screen: str,
    actions: List[Tuple[str, str]],
    now: float,
    interval: float,
) -> Optional[Dict[str, Any]]:
    """Advance one session's state; return a progress event when due."""
    st = _state.setdefault(name, {})
    if _WORKING_MARK not in screen:
        st.update(working_since=None, actions=[], last_emit=0.0)
        return None
    if not st.get("working_since"):
        elapsed = _elapsed_from_screen(screen)
        st.update(working_since=now - (elapsed or 0), actions=[], last_emit=0.0)
    st["actions"] = st.get("actions", []) + actions
    reference = st["last_emit"] or st["working_since"]
    if st["actions"]:
        wait = interval if st["last_emit"] else _FIRST_PROGRESS_S
        if now - reference < wait:
            return None
        text = f"Claude sigue en {project}: {summarize_actions(st['actions'])}."
    else:
        if now - reference < max(interval * _HEARTBEAT_FACTOR, _FIRST_PROGRESS_S):
            return None
        elapsed = _elapsed_from_screen(screen)
        worked = elapsed if elapsed is not None else now - st["working_since"]
        text = (
            f"Claude sigue trabajando en {project}; lleva {_spoken_duration(worked)}."
        )
    st["actions"] = []
    st["last_emit"] = now
    return _progress_event(name, project, text)


def scan(now: Optional[float] = None, interval: float = 60.0) -> List[Dict[str, Any]]:
    """Progress events due across all live JARVIS Claude sessions."""
    global _last_scan
    from openjarvis.tools import session_control as sc

    now = now or time.time()
    if now - _last_scan < _SCAN_EVERY_S:
        return []
    _last_scan = now
    events = []
    live = set()
    for name, launch_dir in sc.list_sessions():
        if sc.session_cli(name) != "claude":
            continue
        live.add(name)
        st = _state.setdefault(name, {})
        transcript = _transcript_for(name, launch_dir)
        actions = read_new_actions(st, transcript) if transcript else []
        screen = sc.capture_screen(name, lines=15)
        project = sc.project_label(Path(launch_dir)) if launch_dir else name
        event = step(name, project, screen, actions, now, interval)
        if event is not None:
            events.append(event)
    for gone in set(_state) - live:
        _state.pop(gone, None)
    return events
