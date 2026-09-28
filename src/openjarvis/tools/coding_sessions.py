"""Follow Claude Code / Gemini CLI sessions from JARVIS.

Lets the user ask:
- "Jarvis, ¿cómo va Claude en openjarvis?"
- "Jarvis, ¿qué sesiones tengo abiertas?"

Both CLIs persist every session on disk, so we read those transcripts
instead of scraping terminal windows:

* Claude Code — ``~/.claude/projects/<slug>/<session-id>.jsonl``, one JSON
  entry per line, each carrying the session's ``cwd``.
* Gemini CLI — ``~/.gemini/tmp/<project>/chats/session-*.json`` (or
  ``.jsonl``); the project folder holds a ``.project_root`` file, or is the
  sha256 of the project path.

Read-only: this module never writes to a session or its terminal.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

CLAUDE_DIR = Path.home() / ".claude" / "projects"
GEMINI_DIR = Path.home() / ".gemini"

# A pending tool call with no activity for this long is most likely an
# unanswered permission prompt rather than a slow tool.
_PERMISSION_IDLE_S = 20.0
# A turn with no activity for this long was probably interrupted.
_STALLED_S = 600.0
_TAIL_BYTES = 512 * 1024
_EDIT_TOOLS = {
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "replace",
    "write_file",
    "edit",
}

# States, phrased for the agent to read back.
WORKING = "working"
WAITING_PERMISSION = "waiting_permission"
IDLE = "idle"  # turn finished — waiting for the user
STALLED = "stalled"


@dataclass
class Session:
    cli: str  # "claude" | "gemini"
    session_id: str
    cwd: str
    path: Path
    updated: float
    state: str = IDLE
    last_prompt: str = ""
    last_reply: str = ""
    pending_tool: str = ""
    files_edited: List[str] = field(default_factory=list)
    title: str = ""
    open: bool = False

    @property
    def project(self) -> str:
        if not self.cwd:
            return "?"
        p = Path(self.cwd)
        return p.parent.name if p.name == "repo" else p.name

    @property
    def launch_key(self) -> str:
        """Identifies the folder the CLI was started in (matches a process cwd)."""
        return self.path.parent.name if self.cli == "claude" else self.cwd


def _short(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ago(ts: float, now: Optional[float] = None) -> str:
    secs = max(0, int((now or time.time()) - ts))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60} min ago"
    return f"{secs // 3600} h ago"


def _read_tail_lines(path: Path) -> List[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # drop the partial first line
            data = fh.read()
    except OSError:
        return []
    return data.decode("utf-8", errors="ignore").splitlines()


def _tool_summary(name: str, args: Any) -> str:
    if not isinstance(args, dict):
        return name
    for key in (
        "command",
        "file_path",
        "path",
        "absolute_path",
        "pattern",
        "url",
        "description",
    ):
        if args.get(key):
            return f"{name}: {_short(str(args[key]), 120)}"
    return name


def _state_from(pending: bool, last_role: str, idle_s: float) -> str:
    if pending:
        return WAITING_PERMISSION if idle_s >= _PERMISSION_IDLE_S else WORKING
    if last_role == "user":
        return STALLED if idle_s >= _STALLED_S else WORKING
    return IDLE


# ── Claude Code ─────────────────────────────────────────────────────────


def _is_human_prompt(entry: Dict[str, Any]) -> Optional[str]:
    """The prompt text if ``entry`` is something the user typed, else None."""
    if entry.get("isMeta") or entry.get("isSidechain"):
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return None
        content = " ".join(
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    if not isinstance(content, str) or not content.strip():
        return None
    if content.lstrip().startswith(
        ("<command-", "<local-command", "<system-reminder", "Caveat:")
    ):
        return None
    return content


def parse_claude_session(path: Path, now: Optional[float] = None) -> Optional[Session]:
    now = now or time.time()
    try:
        updated = path.stat().st_mtime
    except OSError:
        return None
    sess = Session(
        cli="claude", session_id=path.stem, cwd="", path=path, updated=updated
    )
    pending: Dict[str, str] = {}  # tool_use id → summary
    last_role = ""
    for line in _read_tail_lines(path):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("cwd"):
            sess.cwd = entry["cwd"]
        etype = entry.get("type")
        if etype in ("custom-title", "summary") and (
            entry.get("customTitle") or entry.get("summary")
        ):
            sess.title = entry.get("customTitle") or entry.get("summary")
            continue
        if etype not in ("user", "assistant") or entry.get("isSidechain"):
            continue
        content = (entry.get("message") or {}).get("content")
        if etype == "user":
            prompt = _is_human_prompt(entry)
            if prompt is not None:
                if "[Request interrupted by user" in prompt:
                    pending.clear()
                    last_role = "assistant"
                    continue
                sess.last_prompt = prompt
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        pending.pop(c.get("tool_use_id", ""), None)
            last_role = "user"
            continue
        # assistant
        last_role = "assistant"
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and c.get("text", "").strip():
                sess.last_reply = c["text"]
            elif c.get("type") == "tool_use":
                args = c.get("input") or {}
                pending[c.get("id", "")] = _tool_summary(c.get("name", "tool"), args)
                if c.get("name") in _EDIT_TOOLS:
                    fp = args.get("file_path") or args.get("notebook_path")
                    if fp and fp not in sess.files_edited:
                        sess.files_edited.append(fp)
    if not sess.cwd:
        return None
    if pending:
        sess.pending_tool = list(pending.values())[-1]
    sess.state = _state_from(bool(pending), last_role, now - updated)
    return sess


def _claude_files(since: float) -> Iterable[Path]:
    if not CLAUDE_DIR.is_dir():
        return []
    out = []
    for p in CLAUDE_DIR.glob("*/*.jsonl"):
        try:
            if p.stat().st_mtime >= since:
                out.append(p)
        except OSError:
            continue
    return out


# ── Gemini CLI ──────────────────────────────────────────────────────────


def _gemini_project_paths() -> Dict[str, str]:
    """Map ``~/.gemini/tmp/<dir>`` name → project path."""
    known: List[str] = []
    try:
        data = json.loads((GEMINI_DIR / "projects.json").read_text())
        known = list((data.get("projects") or {}).keys())
        names = {v: k for k, v in (data.get("projects") or {}).items()}
    except (OSError, ValueError, AttributeError):
        names = {}
    mapping: Dict[str, str] = dict(names)
    for path in known:
        mapping.setdefault(hashlib.sha256(path.encode()).hexdigest(), path)
    return mapping


def _gemini_messages(path: Path) -> List[Dict[str, Any]]:
    if path.suffix == ".jsonl":
        rows = []
        for line in _read_tail_lines(path):
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        msgs: List[Dict[str, Any]] = []
        for r in rows:
            if isinstance(r.get("messages"), list):
                msgs.extend(r["messages"])
            elif "type" in r:
                msgs.append(r)
        return msgs
    try:
        return json.loads(path.read_text()).get("messages") or []
    except (OSError, ValueError, AttributeError):
        return []


def _gemini_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def parse_gemini_session(
    path: Path, cwd: str, now: Optional[float] = None
) -> Optional[Session]:
    now = now or time.time()
    try:
        updated = path.stat().st_mtime
    except OSError:
        return None
    sess = Session(
        cli="gemini",
        session_id=path.stem.replace("session-", ""),
        cwd=cwd,
        path=path,
        updated=updated,
    )
    last_role = ""
    pending = False
    for m in _gemini_messages(path):
        mtype = m.get("type")
        if mtype == "user":
            text = _gemini_text(m.get("content"))
            if text.strip():
                sess.last_prompt = text
            last_role, pending = "user", False
        elif mtype == "gemini":
            last_role = "assistant"
            text = _gemini_text(m.get("content"))
            if text.strip():
                sess.last_reply = text
            calls = m.get("toolCalls") or []
            pending = False
            for call in calls:
                name = call.get("name") or call.get("displayName") or "tool"
                args = call.get("args") or {}
                status = str(call.get("status") or "").lower()
                if status not in ("success", "error", "cancelled", "canceled"):
                    pending = True
                    sess.pending_tool = _tool_summary(name, args)
                if name in _EDIT_TOOLS:
                    fp = (
                        args.get("file_path")
                        or args.get("absolute_path")
                        or args.get("path")
                    )
                    if fp and fp not in sess.files_edited:
                        sess.files_edited.append(fp)
        elif mtype in ("error", "info"):
            if last_role == "user":
                last_role = "assistant"
    if not sess.last_prompt and not sess.last_reply:
        return None  # login / info-only session
    if not pending:
        sess.pending_tool = ""
    sess.state = _state_from(pending, last_role, now - updated)
    return sess


def _gemini_files(since: float) -> Iterable[tuple]:
    tmp = GEMINI_DIR / "tmp"
    if not tmp.is_dir():
        return []
    mapping = _gemini_project_paths()
    out = []
    for proj in tmp.iterdir():
        chats = proj / "chats"
        if not chats.is_dir():
            continue
        cwd = ""
        root_file = proj / ".project_root"
        if root_file.exists():
            try:
                cwd = root_file.read_text().strip()
            except OSError:
                pass
        cwd = cwd or mapping.get(proj.name, "")
        for p in chats.iterdir():
            if p.suffix not in (".json", ".jsonl"):
                continue
            try:
                if p.stat().st_mtime >= since:
                    out.append((p, cwd))
            except OSError:
                continue
    return out


# ── Running processes ───────────────────────────────────────────────────


def _running_cwds() -> Dict[str, set]:
    """``{"claude": {cwd, ...}, "gemini": {...}}`` for live CLI processes."""
    out: Dict[str, set] = {"claude": set(), "gemini": set()}
    try:
        ps = subprocess.run(
            ["ps", "-axo", "pid=,comm=,args="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return out
    pids: Dict[str, str] = {}
    for line in ps.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        pid, comm = parts[0], parts[1]
        args = parts[2] if len(parts) > 2 else ""
        if Path(comm).name == "claude":
            pids[pid] = "claude"
        elif re.search(r"(^|/)gemini(\s|$)", args) or Path(comm).name == "gemini":
            pids[pid] = "gemini"
    if not pids:
        return out
    try:
        lsof = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(pids)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return out
    current = ""
    for line in lsof.splitlines():
        if line.startswith("p"):
            current = line[1:]
        elif line.startswith("n") and current in pids:
            out[pids[current]].add(line[1:])
    return out


def _claude_slug(path: str) -> str:
    """Claude Code's ``~/.claude/projects`` folder name for a launch dir."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def find_sessions(hours: float = 12.0, now: Optional[float] = None) -> List[Session]:
    """All sessions touched in the last ``hours``, newest first."""
    now = now or time.time()
    since = now - hours * 3600
    sessions: List[Session] = []
    for p in _claude_files(since):
        s = parse_claude_session(p, now)
        if s is not None:
            sessions.append(s)
    for p, cwd in _gemini_files(since):
        s = parse_gemini_session(p, cwd, now)
        if s is not None:
            sessions.append(s)
    sessions.sort(key=lambda s: s.updated, reverse=True)

    live = _running_cwds()
    live_keys = {
        "claude": {_claude_slug(c) for c in live["claude"]},
        "gemini": live["gemini"],
    }
    seen = set()
    for s in sessions:  # newest session per launch folder is the open one
        key = (s.cli, s.launch_key)
        if key not in seen and s.launch_key in live_keys.get(s.cli, set()):
            s.open = True
        seen.add(key)
    return sessions


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def filter_sessions(
    sessions: List[Session], project: str = "", cli: str = "", session_id: str = ""
) -> List[Session]:
    out = sessions
    if session_id:
        out = [s for s in out if s.session_id.startswith(session_id)]
    if cli:
        out = [s for s in out if s.cli == _norm(cli)]
    if project:
        target = _norm(project)
        # Match the folder or its parent: ".../openjarvis/repo" → "openjarvis".
        out = [
            s
            for s in out
            if any(
                target in _norm(n) for n in (Path(s.cwd).name, Path(s.cwd).parent.name)
            )
        ]
    return out


_STATE_TEXT = {
    WORKING: "working",
    WAITING_PERMISSION: "waiting for the user's approval in the terminal",
    IDLE: "finished its turn, waiting for the user",
    STALLED: "no activity for a while (probably interrupted)",
}


def describe(s: Session, detailed: bool, now: Optional[float] = None) -> str:
    label = "Claude Code" if s.cli == "claude" else "Gemini CLI"
    head = (
        f"{label} in {s.project} ({s.cwd}) — {_STATE_TEXT.get(s.state, s.state)}; "
        f"last activity {_ago(s.updated, now)}; "
        f"{'terminal open' if s.open else 'not running'}; "
        f"id {s.session_id[:8]}"
    )
    if s.title:
        head += f"; title: {_short(s.title, 80)}"
    if not detailed:
        return head
    lines = [head]
    if s.last_prompt:
        lines.append(f"  Last request: {_short(s.last_prompt, 300)}")
    if s.pending_tool:
        lines.append(f"  Pending tool: {s.pending_tool}")
    if s.files_edited:
        names = [Path(f).name for f in s.files_edited[-8:]]
        lines.append(f"  Files edited ({len(s.files_edited)}): {', '.join(names)}")
    if s.last_reply:
        lines.append(f"  Last reply: {_short(s.last_reply, 700)}")
    return "\n".join(lines)


def _tmux_screen(s: Session) -> str:
    """Screen of the JARVIS tmux session running ``s``, if any."""
    from openjarvis.tools import session_control as sc

    for name, path in sc.list_sessions():
        if path == s.cwd or name == sc.session_name(Path(s.cwd)):
            return sc.capture_screen(name, lines=15)
    return ""


@ToolRegistry.register("coding_sessions")
class CodingSessionsTool(BaseTool):
    """List and check on the user's Claude Code / Gemini CLI sessions."""

    tool_id = "coding_sessions"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="coding_sessions",
            description=(
                "Check on the user's Claude Code and Gemini CLI coding sessions "
                "(including ones started with start_coding_session). "
                "action='list' shows recent sessions with their state (working, "
                "waiting for approval, finished, stalled). action='status' gives "
                "detail for one session: last request, pending tool, files edited "
                "and the assistant's last reply. Filter by project and/or cli. "
                "Read-only — it cannot type into the session."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "status"],
                        "description": "Default 'list'.",
                    },
                    "project": {
                        "type": "string",
                        "description": "Optional project name, e.g. 'openjarvis'.",
                    },
                    "cli": {
                        "type": "string",
                        "enum": ["claude", "gemini"],
                        "description": "Optional filter.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional session id (or its prefix).",
                    },
                    "hours": {
                        "type": "number",
                        "description": "Look-back window in hours. Default 12.",
                    },
                },
            },
            category="system",
            timeout_seconds=20.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        tool = "coding_sessions"
        action = str(params.get("action") or "list").lower()
        try:
            hours = float(params.get("hours") or 12.0)
        except (TypeError, ValueError):
            hours = 12.0
        now = time.time()
        sessions = filter_sessions(
            find_sessions(hours, now),
            project=str(params.get("project") or ""),
            cli=str(params.get("cli") or ""),
            session_id=str(params.get("session_id") or ""),
        )
        if not sessions:
            return ToolResult(
                tool_name=tool,
                content=(
                    "No Claude Code or Gemini CLI sessions matched "
                    f"in the last {hours:g} h."
                ),
                success=True,
            )
        if action == "status":
            content = describe(sessions[0], True, now)
            screen = _tmux_screen(sessions[0])
            if screen:
                content += f"\n  Terminal screen now (tmux):\n{screen}"
            return ToolResult(tool_name=tool, content=content, success=True)
        shown = sessions[:10]
        body = "\n".join(f"- {describe(s, False, now)}" for s in shown)
        more = (
            f"\n(+{len(sessions) - len(shown)} older)"
            if len(sessions) > len(shown)
            else ""
        )
        return ToolResult(
            tool_name=tool,
            content=f"{len(sessions)} session(s):\n{body}{more}",
            success=True,
        )
