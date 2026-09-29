"""macOS launcher tools — open apps and start Claude Code or Gemini CLI on a project.

Lets the user say:
- "Jarvis, abre Antigravity y arranca Claude en openjarvis"
- "Jarvis, arranca Gemini en openjarvis"

* ``open_app``     opens an installed application, optionally on a project
                   folder (``open -a <App> <project>``).
* ``start_coding_session`` opens a terminal in the project and runs Claude
                   Code (default) or Gemini CLI there, optionally seeded
                   with a first task.

Guard rails — the chat has no confirmation UI and the agent also reads
untrusted text (emails, web pages), so the tools stay narrow:

* apps must be installed ``.app`` bundles (no paths, URLs or shell);
* projects must resolve to a folder under ``[tools.launcher] project_roots``;
* Claude Code and Gemini CLI always start with their normal permission prompts.
"""

from __future__ import annotations

import difflib
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools import session_control as sc
from openjarvis.tools._stubs import BaseTool, ToolSpec

_APP_DIRS = (
    Path("/Applications"),
    Path.home() / "Applications",
    Path("/System/Applications"),
    Path("/System/Applications/Utilities"),
)

# Files that mark a folder as a project root (we stop descending there).
_PROJECT_MARKERS = (
    ".git",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "pubspec.yaml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "Package.swift",
    "CLAUDE.md",
    ".claude",
    ".gemini",
    "index.html",
    "Makefile",
    "CMakeLists.txt",
    "requirements.txt",
    "Pipfile",
)
_SKIP_DIRS = {"node_modules", "build", "dist", "target", "venv", ".venv", "Pods"}


def _norm(text: str) -> str:
    """Lowercase, strip accents-ish noise and non-alphanumerics."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _launcher_config():
    try:
        from openjarvis.core.config import load_config

        return load_config().tools.launcher
    except Exception:  # noqa: BLE001 — fall back to defaults
        from openjarvis.core.config import LauncherConfig

        return LauncherConfig()


def _roots(cfg) -> List[Path]:
    roots = cfg.project_roots
    if isinstance(roots, str):
        roots = [r for r in roots.split(",") if r.strip()]
    out = []
    for r in roots:
        p = Path(os.path.expanduser(str(r).strip())).resolve()
        if p.is_dir():
            out.append(p)
    return out


# ── Apps ────────────────────────────────────────────────────────────────


def _installed_apps() -> Dict[str, Path]:
    """Map app display name → bundle path for every installed .app."""
    apps: Dict[str, Path] = {}
    for d in _APP_DIRS:
        try:
            for entry in d.iterdir():
                if entry.suffix == ".app":
                    apps.setdefault(entry.stem, entry)
        except OSError:
            continue
    return apps


def resolve_app(
    name: str, aliases: Optional[Dict[str, str]] = None
) -> Tuple[Optional[Path], List[str]]:
    """Resolve a spoken app name to an installed bundle.

    Returns ``(path, candidates)`` — ``path`` is None when nothing (or
    several equally good things) matched; ``candidates`` then lists the
    closest installed names for the agent to ask about.
    """
    apps = _installed_apps()
    wanted = name.strip()
    for spoken, real in (aliases or {}).items():
        if _norm(spoken) == _norm(wanted):
            wanted = real
            break

    target = _norm(wanted)
    if not target:
        return None, []
    by_norm = {_norm(n): n for n in apps}
    if target in by_norm:
        return apps[by_norm[target]], []

    prefix = sorted(n for k, n in by_norm.items() if k.startswith(target))
    if len(prefix) == 1:
        return apps[prefix[0]], []
    contains = sorted(n for k, n in by_norm.items() if target in k)
    if len(contains) == 1 and not prefix:
        return apps[contains[0]], []

    candidates = (
        prefix
        or contains
        or difflib.get_close_matches(wanted, list(apps), n=5, cutoff=0.5)
    )
    return None, candidates[:6]


# ── Projects ────────────────────────────────────────────────────────────


def _is_project(path: Path) -> bool:
    if any((path / m).exists() for m in _PROJECT_MARKERS):
        return True
    repo_sub = path / "repo"
    if repo_sub.is_dir() and any((repo_sub / m).exists() for m in _PROJECT_MARKERS):
        return True
    try:
        return any(p.suffix == ".xcodeproj" for p in path.iterdir())
    except OSError:
        return False


def discover_projects(roots: List[Path], max_depth: int = 3) -> List[Path]:
    """Every project folder under ``roots`` (not descending into projects)."""
    found: List[Path] = []

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(p for p in d.iterdir() if p.is_dir())
        except OSError:
            return
        for child in children:
            if child.name.startswith(".") or child.name in _SKIP_DIRS:
                continue
            if _is_project(child):
                found.append(child)
            else:
                walk(child, depth + 1)

    for root in roots:
        walk(root, 1)
    return found


def _within(path: Path, roots: List[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def resolve_project(
    name: str, roots: List[Path], max_depth: int = 3
) -> Tuple[Optional[Path], List[str]]:
    """Resolve a spoken project name (or root-relative path) to a folder.

    Returns ``(path, candidates)`` like :func:`resolve_app`; candidates are
    shown relative to their root so the agent can read them back.
    """
    query = name.strip()
    if not query or not roots:
        return None, []

    # Explicit relative path, e.g. "AI/openjarvis".
    if "/" in query:
        for root in roots:
            p = (root / os.path.expanduser(query)).resolve()
            if p.is_dir() and _within(p, roots):
                return p, []

    projects = discover_projects(roots, max_depth)

    def rel(p: Path) -> str:
        for r in roots:
            if r in p.parents:
                return str(p.relative_to(r))
        return p.name

    target = _norm(query)
    exact = [p for p in projects if _norm(p.name) == target]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, [rel(p) for p in exact]

    contains = [p for p in projects if target in _norm(p.name)]
    if len(contains) == 1:
        return contains[0], []
    if len(contains) > 1:
        return None, [rel(p) for p in contains]

    names = {p.name: p for p in projects}
    pool = [
        names[n] for n in difflib.get_close_matches(query, list(names), n=5, cutoff=0.5)
    ]
    return None, [rel(p) for p in pool[:6]]


def _not_macos(tool: str) -> Optional[ToolResult]:
    if sys.platform != "darwin":
        return ToolResult(
            tool_name=tool, content="This tool only works on macOS.", success=False
        )
    return None


def _project_or_error(
    tool: str, project: str, cfg
) -> Tuple[Optional[Path], Optional[ToolResult]]:
    roots = _roots(cfg)
    if not roots:
        return None, ToolResult(
            tool_name=tool,
            content="No project roots configured ([tools.launcher] project_roots).",
            success=False,
        )
    path, candidates = resolve_project(project, roots, cfg.max_depth)
    if path is None:
        hint = (
            f" Closest matches: {', '.join(candidates)}. Ask the user which one."
            if candidates
            else " No similar project found."
        )
        return None, ToolResult(
            tool_name=tool,
            content=f"Project '{project}' not found.{hint}",
            success=False,
        )
    return path, None


# ── Tools ───────────────────────────────────────────────────────────────


@ToolRegistry.register("open_app")
class OpenAppTool(BaseTool):
    """Open an installed macOS application, optionally on a project folder."""

    tool_id = "open_app"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="open_app",
            description=(
                "Open an installed macOS application by name (e.g. 'Antigravity', "
                "'Cursor', 'Android Studio', 'Xcode', 'Spotify'). Optionally open "
                "one of the user's code projects in it — pass the project name "
                "(e.g. 'openjarvis') and it is found under the user's project folders."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "description": "Application name as the user said it.",
                    },
                    "project": {
                        "type": "string",
                        "description": "Optional project name or folder to open in it.",
                    },
                },
                "required": ["app"],
            },
            category="system",
            timeout_seconds=20.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        if (err := _not_macos("open_app")) is not None:
            return err
        cfg = _launcher_config()
        app_name = str(params.get("app") or "")
        app, candidates = resolve_app(app_name, cfg.app_aliases)
        if app is None:
            hint = (
                f" Installed apps with similar names: {', '.join(candidates)}."
                " Ask the user which one."
                if candidates
                else ""
            )
            return ToolResult(
                tool_name="open_app",
                content=f"App '{app_name}' is not installed.{hint}",
                success=False,
            )

        cmd = ["open", "-a", str(app)]
        project_path = None
        if params.get("project"):
            project_path, err = _project_or_error(
                "open_app", str(params["project"]), cfg
            )
            if err is not None:
                return err
            cmd.append(str(project_path))

        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=15)
        except (subprocess.SubprocessError, OSError) as exc:
            return ToolResult(
                tool_name="open_app",
                content=f"Could not open {app.stem}: {exc}",
                success=False,
            )

        where = f" with project {project_path}" if project_path else ""
        return ToolResult(
            tool_name="open_app", content=f"Opened {app.stem}{where}.", success=True
        )


def _claude_binary() -> str:
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (
        Path.home() / ".local/bin/claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ):
        if candidate.exists():
            return str(candidate)
    return "claude"


def _agy_binary() -> str:
    found = shutil.which("agy")
    if found:
        return found
    for candidate in (
        Path.home() / ".local/bin/agy",
        Path("/opt/homebrew/bin/agy"),
        Path("/usr/local/bin/agy"),
    ):
        if candidate.exists():
            return str(candidate)
    return "agy"


def _gemini_binary() -> str:
    found = shutil.which("gemini")
    if found:
        return found
    for candidate in (
        Path.home() / ".local/bin/gemini",
        Path("/opt/homebrew/bin/gemini"),
        Path("/usr/local/bin/gemini"),
    ):
        if candidate.exists():
            return str(candidate)
    return "gemini"


def _applescript_str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run_in_terminal(terminal: str, shell_cmd: str) -> None:
    term = terminal or "Terminal"
    if _norm(term) not in ("terminal", "iterm", "iterm2"):
        term = "Terminal"  # only scriptable terminals are supported
    if _norm(term) == "terminal":
        script = (
            'tell application "Terminal"\n'
            f"  do script {_applescript_str(shell_cmd)}\n"
            "  activate\n"
            "end tell"
        )
    else:
        script = (
            'tell application "iTerm"\n'
            "  create window with default profile\n"
            "  tell current session of current window to write text "
            f"{_applescript_str(shell_cmd)}\n"
            "  activate\n"
            "end tell"
        )
    subprocess.run(
        ["osascript", "-e", script], check=True, capture_output=True, timeout=15
    )


_CLIS = {
    "claude": "Claude Code",
    "antigravity": "Antigravity CLI",
    "gemini": "Gemini CLI",
}


def _pick_cli(requested: str) -> str:
    """Normalise the requested CLI; Claude unless another is clearly asked for.

    "Gemini" maps to Antigravity (``agy``): it is how Google serves Gemini to
    personal subscriptions now that Gemini CLI's free login was closed.
    """
    cli = _norm(requested or "claude")
    if (
        cli in ("antigravity", "agy", "gemini")
        or "gemini" in cli
        or "antigravity" in cli
    ):
        return "antigravity"
    return "claude"


def build_assistant_command(
    cli: str,
    project: Path,
    task: str = "",
    session_id: str = "",
    settings: str = "",
    dangerously_skip_permissions: bool = False,
) -> str:
    """Command line that starts an interactive ``cli`` session (no ``cd``).

    Claude takes the first task as a positional prompt and stays
    interactive; ``agy`` and ``gemini`` need ``-i`` (a bare prompt runs
    one-shot and exits).  Claude also gets a known ``--session-id`` so
    ``coding_sessions`` can follow exactly this session, and optional
    ``--settings`` carrying the JARVIS notification hooks.
    """
    binaries = {
        "claude": _claude_binary,
        "antigravity": _agy_binary,
        "gemini": _gemini_binary,
    }
    parts = [shlex.quote(binaries[cli]())]
    if dangerously_skip_permissions:
        parts.append("--dangerously-skip-permissions")
    if cli == "claude":
        if session_id:
            parts += ["--session-id", shlex.quote(session_id)]
        parts += ["--name", shlex.quote(f"jarvis: {project.name}")]
        if settings:
            parts += ["--settings", shlex.quote(settings)]
        if task:
            parts.append(shlex.quote(task))
    elif task:
        parts += ["-i", shlex.quote(task)]
    return " ".join(parts)


def build_session_command(
    cli: str,
    project: Path,
    task: str = "",
    session_id: str = "",
    dangerously_skip_permissions: bool = False,
) -> str:
    """Shell command that ``cd``s into ``project`` and starts ``cli`` there."""
    command = build_assistant_command(
        cli,
        project,
        task,
        session_id,
        dangerously_skip_permissions=dangerously_skip_permissions,
    )
    return f"cd {shlex.quote(str(project))} && {command}"


@ToolRegistry.register("start_coding_session")
class StartCodingSessionTool(BaseTool):
    """Open a terminal in a project and start Claude Code (default) or Antigravity."""

    tool_id = "start_coding_session"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="start_coding_session",
            description=(
                "Start an interactive coding-assistant session in a new terminal "
                "window inside one of the user's projects. Uses Claude Code by "
                "default; use cli='antigravity' ONLY when the user explicitly asks "
                "for Antigravity, agy or Gemini (Google's agent). Optionally give "
                "the assistant a first task. The session keeps its normal "
                "permission prompts unless dangerously_skip_permissions is enabled. "
                "Pair with open_app to also open the project in an editor such as "
                "Antigravity, and use coding_sessions afterwards to check on its "
                "progress."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name or folder (e.g. 'openjarvis').",
                    },
                    "cli": {
                        "type": "string",
                        "enum": ["claude", "antigravity"],
                        "description": "Which assistant to start. Default 'claude'.",
                    },
                    "task": {
                        "type": "string",
                        "description": "Optional first instruction the user gave.",
                    },
                    "dangerously_skip_permissions": {
                        "type": "boolean",
                        "description": (
                            "Bypass tool permission prompts. Default comes from "
                            "[tools.launcher] dangerously_skip_permissions (false)."
                        ),
                    },
                },
                "required": ["project"],
            },
            category="system",
            timeout_seconds=20.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        tool = "start_coding_session"
        if (err := _not_macos(tool)) is not None:
            return err
        cli = _pick_cli(str(params.get("cli") or ""))
        label = _CLIS[cli]
        cfg = _launcher_config()
        project, err = _project_or_error(tool, str(params.get("project") or ""), cfg)
        if err is not None:
            return err

        skip_perms = params.get("dangerously_skip_permissions")
        if skip_perms is None:
            raw = getattr(cfg, "dangerously_skip_permissions", False)
            skip_perms = raw if isinstance(raw, bool) else False
        else:
            skip_perms = bool(skip_perms)

        task = str(params.get("task") or "").strip()
        terminal = cfg.terminal or "Terminal"
        session_id = str(uuid.uuid4()) if cli == "claude" else ""
        tmux_name = ""
        try:
            if sc.tmux_bin():
                # Inside tmux so send_to_session can type into it later.
                tmux_name = sc.session_name(project, cli)
                if sc.has_session(tmux_name):
                    if task:
                        sc.type_message(tmux_name, task)
                    _run_in_terminal(terminal, sc.attach_command(tmux_name))
                    sent = f" and sent it: {task}" if task else ""
                    return ToolResult(
                        tool_name=tool,
                        content=(
                            f"A JARVIS session for {project.name} was already "
                            f"running ({tmux_name}); reopened it{sent}. Use "
                            "send_to_session to give it instructions."
                        ),
                        success=True,
                        metadata={
                            "cli": cli,
                            "project": str(project),
                            "tmux": tmux_name,
                        },
                    )
                if cli == "claude":
                    settings = str(sc.hooks_settings_file())
                elif cli == "antigravity":
                    sc.antigravity_hooks_file(project)
                    settings = ""
                else:
                    settings = ""
                command = build_assistant_command(
                    cli,
                    project,
                    task,
                    session_id,
                    settings,
                    dangerously_skip_permissions=skip_perms,
                )
                sc.new_session(tmux_name, project, command)
                _run_in_terminal(terminal, sc.attach_command(tmux_name))
            else:
                if cli == "antigravity":
                    sc.antigravity_hooks_file(project)
                _run_in_terminal(
                    terminal,
                    build_session_command(
                        cli,
                        project,
                        task,
                        session_id,
                        dangerously_skip_permissions=skip_perms,
                    ),
                )
        except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
            detail = getattr(exc, "stderr", b"") or b""
            return ToolResult(
                tool_name=tool,
                content=(
                    f"Could not start {label} in {terminal}: {exc} "
                    f"{detail.decode(errors='ignore').strip()}"
                ),
                success=False,
            )

        extra = f" with the task: {task}" if task else ""
        return ToolResult(
            tool_name=tool,
            content=(
                f"Started {label} in {terminal} for project {project.name}{extra}. "
                + (
                    f"It runs in tmux session {tmux_name}: use send_to_session to "
                    "give it instructions and coding_sessions to follow it."
                    if tmux_name
                    else "Use coding_sessions to follow its progress."
                )
            ),
            success=True,
            metadata={
                "cli": cli,
                "project": str(project),
                "session_id": session_id,
                "tmux": tmux_name,
            },
        )
