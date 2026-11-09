"""mac_files — create folders and move files inside the user's home.

Deliberately small: it cannot delete or overwrite anything, and hidden paths
and ``~/Library`` are off limits, which keeps credentials and app data out of
reach.  Spanish folder names ("escritorio", "descargas") work as the first
part of a path.

Changes are refused in a turn that read third-party text
(``session_guard.check``).  Output is JARVIS's own confirmation or a file
listing, so this tool is in ``session_guard.TRUSTED_OUTPUT_TOOLS``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

TOOL = "mac_files"
MAX_LISTED = 50

_ALIASES = {
    "escritorio": "Desktop",
    "desktop": "Desktop",
    "documentos": "Documents",
    "documents": "Documents",
    "descargas": "Downloads",
    "downloads": "Downloads",
    "imagenes": "Pictures",
    "imágenes": "Pictures",
    "fotos": "Pictures",
    "pictures": "Pictures",
    "musica": "Music",
    "música": "Music",
    "music": "Music",
    "peliculas": "Movies",
    "películas": "Movies",
    "movies": "Movies",
}


class PathRefused(ValueError):
    """The path is outside what this tool may touch."""


def resolve(raw: str) -> Path:
    """An absolute path under home, with aliases applied; refuses anything else."""
    home = Path.home().resolve()
    text = raw.strip()
    if not text:
        raise PathRefused("Say which path.")
    if text.startswith("~"):
        text = text[1:].lstrip("/")
    candidate = Path(text)
    if not candidate.is_absolute():
        parts = list(candidate.parts)
        if parts:
            parts[0] = _ALIASES.get(parts[0].lower(), parts[0])
        candidate = home.joinpath(*parts)
    path = candidate.resolve()
    try:
        inside = path.relative_to(home).parts
    except ValueError as exc:
        raise PathRefused(f"{raw} is outside the home folder.") from exc
    if any(part.startswith(".") for part in inside) or inside[:1] == ("Library",):
        raise PathRefused(f"{raw} is a protected location.")
    return path


def _mkdir(path: Path) -> tuple[str, bool]:
    if path.exists():
        kind = "Folder" if path.is_dir() else "A file"
        return f"{kind} already exists at {path}.", path.is_dir()
    path.mkdir(parents=True)
    return f"Created folder {path}.", True


def _move(source: Path, destination: Path) -> tuple[str, bool]:
    if not source.exists():
        return f"{source} does not exist.", False
    target = destination / source.name if destination.is_dir() else destination
    if target.exists():
        return f"{target} already exists; nothing was moved.", False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return f"Moved {source} to {target}.", True


def _list(path: Path) -> tuple[str, bool]:
    if not path.is_dir():
        return f"{path} is not a folder.", False
    entries = sorted(
        p.name + ("/" if p.is_dir() else "")
        for p in path.iterdir()
        if not p.name.startswith(".")
    )
    shown = entries[:MAX_LISTED]
    extra = f"\n… and {len(entries) - MAX_LISTED} more" if entries[MAX_LISTED:] else ""
    return f"{path}:\n" + "\n".join(shown) + extra, True


@ToolRegistry.register(TOOL)
class MacFilesTool(BaseTool):
    """Create folders, move files and list folders inside the user's home."""

    tool_id = TOOL

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=TOOL,
            description=(
                "The user's Mac folders: 'mkdir' creates a folder (path), "
                "'move' moves a file or folder (path to destination), 'list' "
                "shows a folder. Paths are relative to home; 'escritorio', "
                "'documentos' and 'descargas' work as the first part, e.g. "
                "'escritorio/Facturas'. It cannot delete or overwrite."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["mkdir", "move", "list"]},
                    "path": {"type": "string"},
                    "destination": {"type": "string"},
                },
                "required": ["action", "path"],
            },
            timeout_seconds=30,
        )

    def execute(self, **params: Any) -> ToolResult:
        action = str(params.get("action") or "")
        try:
            content, ok = self._run(action, params)
        except (OSError, ValueError) as exc:
            content, ok = str(exc), False
        return ToolResult(tool_name=TOOL, content=content, success=ok)

    @staticmethod
    def _run(action: str, params: dict[str, Any]) -> tuple[str, bool]:
        path = resolve(str(params.get("path") or ""))
        if action == "mkdir":
            return _mkdir(path)
        if action == "list":
            return _list(path)
        if action == "move":
            return _move(path, resolve(str(params.get("destination") or "")))
        return f"Unknown action '{action}'.", False


__all__ = ["MacFilesTool", "PathRefused", "resolve"]
