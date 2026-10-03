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
  Antigravity (``agy``) sessions instead count as working while their
  transcript — reported by the PostToolUse/Stop hooks — keeps growing.

While a session works it yields one progress event per interval: the
actions since the last update, or — during long thinking with no tool
calls — a heartbeat with the elapsed time.  Nothing is replayed from before
the watcher first saw a transcript.

Unlike hooks this needs no changes to how Claude was started, so it also
covers sessions launched before a hooks update.  ``scan`` is called from
the events endpoint the app polls; it rate-limits itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_WORKING_MARK = "esc to interrupt"
# Antigravity has no reliable on-screen busy marker ("Esc to cancel" also
# labels its dialogs), so it counts as working while its transcript keeps
# growing: this long without a new entry means it stopped.
_AGY_IDLE_S = 90.0
_AGY_BRAIN_DIR = Path.home() / ".gemini" / "antigravity-cli" / "brain"
_FIRST_PROGRESS_S = 20.0
_HEARTBEAT_FACTOR = 3  # heartbeats are this many intervals apart
_SCAN_EVERY_S = 4.0
_MAX_READ = 2 * 1024 * 1024

_state: Dict[str, Dict[str, Any]] = {}
# tmux session name → transcript path, learned from hook payloads.
_transcripts: Dict[str, str] = {}
_last_scan = 0.0


def _decode_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Antigravity stores each argument JSON-encoded (``"\\"pytest\\""``)."""
    out = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except ValueError:
                pass
        out[key] = value
    return out


def describe_action(tool: str, args: Dict[str, Any]) -> tuple[str, str]:
    """``(category, detail)`` for one tool call, used to build a summary."""
    args = _decode_args(args)
    path = (
        args.get("file_path")
        or args.get("notebook_path")
        or args.get("path")
        or args.get("TargetFile")
        or args.get("AbsolutePath")
        or args.get("target_file")
        or args.get("absolute_path")
        or ""
    )
    name = Path(str(path)).name if path else ""
    t_lower = tool.lower()
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit") or t_lower in (
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "edit",
        "write",
    ):
        return "edit", name or "un archivo"
    if tool == "Bash" or t_lower in ("run_command", "bash"):
        desc = str(args.get("description") or args.get("toolSummary") or "").strip()
        if not desc:
            cmd = str(args.get("command") or args.get("CommandLine") or "")
            desc = " ".join(cmd.split()[:3])
        return "run", desc[:60]
    if tool in ("Read", "Grep", "Glob", "LS") or t_lower in (
        "view_file",
        "read",
        "grep",
        "glob",
        "list_directory",
        "list_dir",
        "grep_search",
        "find_by_name",
    ):
        return "read", name
    if tool in ("WebFetch", "WebSearch") or t_lower in (
        "webfetch",
        "websearch",
        "search_web",
        "read_url_content",
    ):
        return "web", ""
    if tool in ("Task", "Agent") or t_lower in (
        "invoke_subagent",
        "task",
        "agent",
        "subagent",
    ):
        desc = str(
            args.get("description") or args.get("toolSummary") or args.get("Role") or ""
        )
        return "agent", desc[:60]
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


def antigravity_transcript(conversation_id: str) -> str:
    """Where ``agy`` logs a conversation ("" when it doesn't exist)."""
    if not conversation_id or "/" in conversation_id:
        return ""
    path = (
        _AGY_BRAIN_DIR
        / conversation_id
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )
    return str(path) if path.exists() else ""


def remember_transcript(cwd: str, transcript_path: str, cli: str = "claude") -> None:
    """Called for every hook: hooks only come from JARVIS-started sessions.

    Only the session of the hook's own ``cli`` is updated, so Claude and
    Antigravity running in the same project never share a transcript.
    """
    if not cwd or not transcript_path:
        return
    from openjarvis.tools import session_control as sc

    _transcripts[sc.session_name(Path(cwd), cli)] = transcript_path


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
        if entry.get("type") == "assistant":
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
        elif entry.get("type") == "PLANNER_RESPONSE":
            for tc in entry.get("tool_calls") or []:
                if isinstance(tc, dict):
                    tname = tc.get("name") or tc.get("toolAction") or ""
                    targs = tc.get("args") or {}
                    actions.append(
                        describe_action(
                            str(tname),
                            targs if isinstance(targs, dict) else {},
                        )
                    )
    return actions


# ── Prompts waiting on the user (permission dialogs, numbered questions) ──

_BOX_CHARS = "│╭╮╰╯─┃┏┓┗┛━ \t"
_OPTION_RE = re.compile(r"^(?:[❯›>]\s*)?(\d)[.)]\s+(.+?)\s*$")
_RULE_RE = re.compile(r"^[─━\-=_ ]{3,}$")


def _short_label(label: str) -> str:
    """Button text for an option: Claude's long permission choices shortened."""
    low = label.lower()
    if low.startswith("yes, and don't ask again") or low.startswith("yes, allow all"):
        return "Sí, siempre"
    if low.startswith("yes, allow") and "always" in low:
        return "Sí, siempre"
    if low in ("yes", "allow", "allow once"):
        return "Sí"
    if low.startswith("no"):
        return "No"
    label = re.sub(r"\s*\(esc\)$", "", label)
    return label if len(label) <= 28 else label[:27].rstrip() + "…"


def parse_prompt(screen: str) -> Optional[Dict[str, Any]]:
    """The numbered menu the assistant is waiting on, or None.

    Claude Code and Antigravity ask for permission (and AskUserQuestion asks
    its questions) with a numbered menu where the digit selects directly.
    Returns ``{"id", "question", "detail", "options": [{"key", "label",
    "title"}]}``; ``id`` fingerprints the dialog so an answer is only typed
    while that same dialog is still on screen.  A "No…" option answers with
    Escape, the CLI's reliable way to decline.
    """
    rows = [r.strip(_BOX_CHARS) for r in screen.splitlines()]
    rows = [r for r in rows if r and not _RULE_RE.match(r)]
    # The last run of consecutive numbered options (1., 2., …) on screen.
    end = None
    for i in range(len(rows) - 1, -1, -1):
        if _OPTION_RE.match(rows[i]):
            end = i
            break
    if end is None:
        return None
    start = end
    while start > 0 and _OPTION_RE.match(rows[start - 1]):
        start -= 1
    found = [_OPTION_RE.match(r) for r in rows[start : end + 1]]
    numbers = [int(m.group(1)) for m in found if m]
    if len(numbers) < 2 or numbers != list(range(1, len(numbers) + 1)):
        return None
    q = start - 1
    while q >= 0 and not rows[q].endswith("?"):
        q -= 1
    if q < 0 or start - q > 3:
        return None  # a numbered list in the output, not a question
    question = rows[q]
    detail = [r for r in rows[max(0, q - 4) : q] if not r.endswith("?")][-3:]
    options = []
    for m in found:
        if not m:
            continue
        label = m.group(2)
        decline = label.lower().startswith("no")
        options.append(
            {
                "key": "Escape" if decline else m.group(1),
                "label": _short_label(label),
                "title": label,
            }
        )
    digest = hashlib.sha1(
        "\n".join([question, *detail, *[o["title"] for o in options]]).encode()
    ).hexdigest()[:12]
    return {
        "id": digest,
        "question": question,
        "detail": "\n".join(detail),
        "options": options,
    }


def _is_working(st: Dict[str, Any], screen: str, now: float, cli: str) -> bool:
    if cli == "antigravity":
        return now - st.get("last_activity", 0.0) < _AGY_IDLE_S
    return _WORKING_MARK in screen


def _progress_event(
    name: str, project: str, text: str, cli: str = "claude"
) -> Dict[str, Any]:
    cli_label = "Antigravity" if cli == "antigravity" else "Claude"
    return {
        "kind": "progress",
        "cli": cli,
        "cli_label": cli_label,
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
    cli: str = "claude",
) -> Optional[Dict[str, Any]]:
    """Advance one session's state; return a progress event when due."""
    st = _state.setdefault(name, {})
    if actions:
        st["last_activity"] = now
    working = _is_working(st, screen, now, cli)
    if working and not st.get("working_since"):
        st["edited"] = []  # files edited in this stretch of work (notch)
    st.setdefault("edited", [])
    for cat, detail in actions:
        if cat == "edit" and detail and detail not in st["edited"]:
            st["edited"].append(detail)
    if not working:
        st.update(working_since=None, actions=[], last_emit=0.0)
        return None
    if not st.get("working_since"):
        elapsed = _elapsed_from_screen(screen)
        st.update(working_since=now - (elapsed or 0), actions=[], last_emit=0.0)
    st["actions"] = st.get("actions", []) + actions
    reference = st["last_emit"] or st["working_since"]
    cli_label = "Antigravity" if cli == "antigravity" else "Claude"
    if st["actions"]:
        wait = interval if st["last_emit"] else _FIRST_PROGRESS_S
        if now - reference < wait:
            return None
        text = f"{cli_label} sigue en {project}: {summarize_actions(st['actions'])}."
    else:
        if now - reference < max(interval * _HEARTBEAT_FACTOR, _FIRST_PROGRESS_S):
            return None
        elapsed = _elapsed_from_screen(screen)
        worked = elapsed if elapsed is not None else now - st["working_since"]
        text = (
            f"{cli_label} sigue trabajando en {project}; "
            f"lleva {_spoken_duration(worked)}."
        )
    st["actions"] = []
    st["last_emit"] = now
    return _progress_event(name, project, text, cli=cli)


def live(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """What each live JARVIS session is doing right now, for the notch.

    ``status`` is "prompt" (a menu waits on the user; see ``prompt``),
    "working" or "idle".  ``edited`` lists the files edited since the
    session last started working (as seen by ``scan``).
    """
    from openjarvis.tools import session_control as sc

    now = now or time.time()
    out = []
    for name, launch_dir in sc.list_sessions():
        cli = sc.session_cli(name)
        screen = sc.capture_screen(name, lines=40)
        st = _state.get(name, {})
        prompt = parse_prompt(screen)
        if prompt:
            status = "prompt"
        elif _is_working(st, screen, now, cli):
            status = "working"
        else:
            status = "idle"
        elapsed = _elapsed_from_screen(screen) if status == "working" else None
        if status == "working" and elapsed is None and st.get("working_since"):
            elapsed = int(now - st["working_since"])
        out.append(
            {
                "name": name,
                "project": sc.project_label(Path(launch_dir)) if launch_dir else name,
                "cli": cli,
                "status": status,
                "elapsed_s": elapsed,
                "edited": list(st.get("edited", [])),
                "prompt": prompt,
            }
        )
    return out


def scan(now: Optional[float] = None, interval: float = 60.0) -> List[Dict[str, Any]]:
    """Progress events due across all live JARVIS Claude/Antigravity sessions."""
    global _last_scan
    from openjarvis.tools import session_control as sc

    now = now or time.time()
    if now - _last_scan < _SCAN_EVERY_S:
        return []
    _last_scan = now
    events = []
    live = set()
    for name, launch_dir in sc.list_sessions():
        cli = sc.session_cli(name)
        if cli not in ("claude", "antigravity"):
            continue
        live.add(name)
        st = _state.setdefault(name, {})
        transcript = _transcript_for(name, launch_dir)
        seen = st.get("path") == str(transcript) and "offset" in st
        before = st.get("offset")
        actions = read_new_actions(st, transcript) if transcript else []
        if transcript and seen and st.get("offset") != before:
            st["last_activity"] = now  # new entries, even without tool calls
        screen = sc.capture_screen(name, lines=15)
        project = sc.project_label(Path(launch_dir)) if launch_dir else name
        event = step(name, project, screen, actions, now, interval, cli=cli)
        if event is not None:
            events.append(event)
    for gone in set(_state) - live:
        _state.pop(gone, None)
    return events
