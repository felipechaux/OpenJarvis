"""Show the user's knowledge graph in Obsidian.

The graph is built by ``~/.openjarvis/knowledge/sync.sh`` (graphify over the
user's Claude memory files and ~/.openjarvis USER.md / SOUL.md).  This tool
re-exports it as an Obsidian vault so the notes match the current graph,
makes sure the vault is registered and opens in graph view, then opens it.

``schedule_sync`` rebuilds the graph in the background; memory_manage and
user_profile_manage call it after every change, so what the user asks
JARVIS to remember reaches the graph (and an open Obsidian) by itself.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

TOOL = "show_knowledge_graph"
_KNOWLEDGE = Path.home() / ".openjarvis" / "knowledge"
_CORPUS = _KNOWLEDGE / "corpus"
_SYNC_SCRIPT = _KNOWLEDGE / "sync.sh"
_SYNC_LOG = _KNOWLEDGE / "sync.log"
# Changes within this window are rebuilt together (one Haiku call, not one
# per remembered fact).
_SYNC_DELAY_S = 30.0
_sync_lock = threading.Lock()
_sync_timer: Optional[threading.Timer] = None
_sync_proc: Optional[subprocess.Popen] = None
_OBSIDIAN_APP = Path("/Applications/Obsidian.app")
_OBSIDIAN_CONFIG = (
    Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
)
# Opens the vault on a single graph-view tab (only written when the vault has
# no layout yet, so the user's own arrangement is kept).
_GRAPH_WORKSPACE = {
    "main": {
        "id": "jarvis-main",
        "type": "split",
        "children": [
            {
                "id": "jarvis-tabs",
                "type": "tabs",
                "children": [
                    {
                        "id": "jarvis-graph",
                        "type": "leaf",
                        "state": {"type": "graph", "state": {}},
                    }
                ],
            }
        ],
        "direction": "vertical",
    },
    "active": "jarvis-graph",
}


def _graphify_bin() -> Optional[str]:
    found = shutil.which("graphify")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "graphify"
    return str(fallback) if fallback.exists() else None


def _counts(graph: Path) -> tuple[int, int]:
    try:
        data = json.loads(graph.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, 0
    edges = data.get("links") or data.get("edges") or []
    return len(data.get("nodes") or []), len(edges)


def _register_vault(vault: Path) -> None:
    """Add ``vault`` to Obsidian's vault list if it isn't there yet."""
    try:
        config = (
            json.loads(_OBSIDIAN_CONFIG.read_text())
            if _OBSIDIAN_CONFIG.exists()
            else {}
        )
    except (OSError, ValueError):
        return  # unreadable: never clobber Obsidian's own config
    vaults = config.setdefault("vaults", {})
    if any(v.get("path") == str(vault) for v in vaults.values()):
        return
    vaults[secrets.token_hex(8)] = {"path": str(vault), "ts": int(time.time() * 1000)}
    try:
        _OBSIDIAN_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        _OBSIDIAN_CONFIG.write_text(json.dumps(config))
    except OSError:
        pass


def _start_timer(delay: float) -> None:
    global _sync_timer
    if _sync_timer is not None:
        _sync_timer.cancel()
    _sync_timer = threading.Timer(delay, _run_sync)
    _sync_timer.daemon = True
    _sync_timer.start()


def schedule_sync(delay: float = _SYNC_DELAY_S) -> bool:
    """Rebuild the knowledge graph in the background after ``delay`` seconds.

    Calls within the delay are batched into one run.  Returns False when
    there is no sync script to run.
    """
    if not _SYNC_SCRIPT.exists():
        return False
    with _sync_lock:
        _start_timer(delay)
    return True


# Files sync.sh copies into the corpus that JARVIS itself edits.
_SYNCED_SOURCES = {
    Path.home() / ".openjarvis" / "MEMORY.md",
    Path.home() / ".openjarvis" / "USER.md",
}


def sync_after_write(path: Path, result: ToolResult) -> ToolResult:
    """Schedule a graph rebuild when a successful write touched a corpus source."""
    if result.success and path.expanduser().absolute() in _SYNCED_SOURCES:
        schedule_sync()
    return result


def _run_sync() -> None:
    global _sync_proc
    with _sync_lock:
        if _sync_proc is not None and _sync_proc.poll() is None:
            _start_timer(_SYNC_DELAY_S)  # a run is in flight; follow up after it
            return
        try:
            with _SYNC_LOG.open("ab") as log:
                _sync_proc = subprocess.Popen(
                    ["/bin/bash", str(_SYNC_SCRIPT)],
                    stdout=log,
                    stderr=log,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
        except OSError:
            _sync_proc = None


@ToolRegistry.register(TOOL)
class ShowKnowledgeGraphTool(BaseTool):
    """Open the user's knowledge graph (about them and JARVIS) in Obsidian."""

    tool_id = TOOL

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=TOOL,
            description=(
                "Show the user's knowledge graph — what JARVIS and Claude know "
                "about the user, their projects and how JARVIS is set up — as an "
                "interactive graph in Obsidian. Use when the user asks to see or "
                "open their knowledge graph, 'mi grafo', or Obsidian. This tool "
                "only shows the graph; nodes cannot be edited directly. To "
                "change what it says, save the fact with user_profile_manage "
                "(about the user) or memory_manage (JARVIS's own notes); the "
                "graph rebuilds from them within a few minutes."
            ),
            parameters={"type": "object", "properties": {}},
            category="system",
            timeout_seconds=60.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        if sys.platform != "darwin":
            return ToolResult(
                tool_name=TOOL, content="This tool only works on macOS.", success=False
            )
        graph = _CORPUS / "graphify-out" / "graph.json"
        if not graph.exists():
            return ToolResult(
                tool_name=TOOL,
                content=(
                    "The knowledge graph has not been built yet. The user can "
                    "build it by running ~/.openjarvis/knowledge/sync.sh."
                ),
                success=False,
            )
        if not _OBSIDIAN_APP.exists():
            return ToolResult(
                tool_name=TOOL,
                content="Obsidian is not installed (brew install --cask obsidian).",
                success=False,
            )
        vault = _CORPUS / "graphify-out" / "obsidian"
        graphify = _graphify_bin()
        if graphify:
            try:  # refresh the notes from the current graph (~1 s)
                subprocess.run(
                    [graphify, "export", "obsidian"],
                    cwd=_CORPUS,
                    capture_output=True,
                    timeout=45,
                )
            except (subprocess.SubprocessError, OSError):
                pass  # an older export is still worth showing
        if not vault.is_dir():
            return ToolResult(
                tool_name=TOOL,
                content="Could not export the knowledge graph as an Obsidian vault.",
                success=False,
            )
        workspace = vault / ".obsidian" / "workspace.json"
        if not workspace.exists():
            workspace.parent.mkdir(parents=True, exist_ok=True)
            workspace.write_text(json.dumps(_GRAPH_WORKSPACE))
        _register_vault(vault)
        try:
            subprocess.run(
                ["open", f"obsidian://open?path={quote(str(vault))}"],
                check=True,
                capture_output=True,
                timeout=15,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return ToolResult(
                tool_name=TOOL,
                content=f"Could not open Obsidian: {exc}",
                success=False,
            )
        nodes, edges = _counts(graph)
        return ToolResult(
            tool_name=TOOL,
            content=(
                f"Opened the knowledge graph in Obsidian ({nodes} nodes, "
                f"{edges} relations)."
            ),
            success=True,
            metadata={"nodes": nodes, "edges": edges, "vault": str(vault)},
        )


__all__ = ["ShowKnowledgeGraphTool"]
