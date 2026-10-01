"""Build the user's knowledge graph with graphify, ready for Obsidian.

Run as ``python -m openjarvis.tools.knowledge_sync``.  Mirrors the sources
into ``~/.openjarvis/knowledge/corpus`` (JARVIS's USER.md / SOUL.md /
MEMORY.md and the user's Claude Code memory files), extracts the graph with
``graphify extract`` and exports it as an Obsidian vault.

The extraction model is the user's own JARVIS server: graphify's ``openai``
backend talks to ``jarvis serve``'s OpenAI-compatible API, so it runs on
whatever CLI subscription JARVIS uses (Claude Code, Kiro, Antigravity…) and
needs no API key.  The server must be running.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

KNOWLEDGE = Path.home() / ".openjarvis" / "knowledge"
CORPUS = KNOWLEDGE / "corpus"
GRAPH = CORPUS / "graphify-out" / "graph.json"
VAULT = CORPUS / "graphify-out" / "obsidian"
# Written after a build succeeds: copies mirrored by a failed build must not
# look up to date to the next run.
BUILT_STAMP = KNOWLEDGE / ".last-build"
_JARVIS_FILES = ("USER.md", "SOUL.md", "MEMORY.md")
_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
# Cheap models for extraction, by provider: the graph is rebuilt often.
_FAST_MODELS = {
    "claude-cli": "claude-cli/haiku",
    "kiro-cli": "kiro-cli/claude-haiku-4.5",
}


def graphify_bin() -> Optional[str]:
    found = shutil.which("graphify")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "graphify"
    return str(fallback) if fallback.exists() else None


def _has_content(path: Path) -> bool:
    """False for the empty templates ``jarvis init`` writes (headings only)."""
    lines = path.read_text(errors="replace").splitlines()
    return any(ln.strip() and not ln.lstrip().startswith("#") for ln in lines)


def _excluded_patterns() -> List[str]:
    from openjarvis.core.config import KnowledgeConfig, load_config

    try:
        return list(load_config().tools.knowledge.exclude)
    except Exception:  # noqa: BLE001 — never graph secrets on a bad config
        return list(KnowledgeConfig().exclude)


def is_excluded(path: Path, patterns: List[str]) -> bool:
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pat.lower()) for pat in patterns)


def collect_sources() -> Dict[Path, Path]:
    """``{source: path inside the corpus}`` for every file the graph reads.

    Files whose name matches ``[tools.knowledge] exclude`` (credentials,
    secrets…) are left out.
    """
    sources: Dict[Path, Path] = {}
    excluded = _excluded_patterns()
    home = Path.home() / ".openjarvis"
    for name in _JARVIS_FILES:
        src = home / name
        if src.is_file() and _has_content(src) and not is_excluded(src, excluded):
            sources[src] = Path("jarvis") / name
    for memory_dir in sorted(_CLAUDE_PROJECTS.glob("*/memory")):
        for src in sorted(memory_dir.glob("*.md")):
            if is_excluded(src, excluded):
                continue
            sources[src] = Path("claude") / memory_dir.parent.name / src.name
    return sources


def mirror_corpus(
    sources: Dict[Path, Path],
    corpus: Optional[Path] = None,
    *,
    dry_run: bool = False,
) -> bool:
    """Copy *sources* into *corpus*, dropping stale copies.  True if changed.

    With ``dry_run`` nothing is written: it only answers whether a copy
    would change the corpus (a cheap local check, no model call).
    """
    corpus = corpus or CORPUS
    changed = False
    wanted = {corpus / rel for rel in sources.values()}
    for sub in ("jarvis", "claude"):
        for old in (corpus / sub).rglob("*.md") if (corpus / sub).is_dir() else []:
            if old not in wanted:
                if dry_run:
                    return True
                old.unlink()
                changed = True
    for sub in ("jarvis", "claude"):  # folders of projects that are gone
        root = corpus / sub
        if root.is_dir() and not dry_run:
            for folder in sorted(root.rglob("*"), reverse=True):
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
    for src, rel in sources.items():
        dst = corpus / rel
        data = src.read_bytes()
        if dst.is_file() and dst.read_bytes() == data:
            continue
        if dry_run:
            return True
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        changed = True
    return changed


def needs_rebuild() -> bool:
    """True when a source changed or the last build did not finish."""
    if not BUILT_STAMP.exists() or not GRAPH.exists():
        return bool(collect_sources())
    return mirror_corpus(collect_sources(), dry_run=True)


def extraction_model() -> str:
    """The model JARVIS answers with, swapped for its provider's cheap tier."""
    from openjarvis.core.config import load_config

    intel = load_config().intelligence
    model = intel.fast_model or intel.default_model
    provider = model.split("/", 1)[0] if "/" in model else ""
    return _FAST_MODELS.get(provider, model)


def _server_url() -> str:
    from openjarvis.core.config import load_config

    port = load_config().server.port
    return f"http://127.0.0.1:{port}/v1"


def _graphify_env(model: str) -> Dict[str, str]:
    env = dict(os.environ)
    env["OPENAI_BASE_URL"] = _server_url()
    env["OPENAI_MODEL"] = model
    env["OPENAI_API_KEY"] = os.environ.get("OPENJARVIS_API_KEY") or "local"
    return env


def sync(force: bool = False) -> str:
    """Rebuild the graph and its Obsidian vault.  Returns a one-line summary."""
    graphify = graphify_bin()
    if not graphify:
        raise RuntimeError(
            'graphify is not installed (uv tool install "graphifyy[openai]").'
        )
    sources = collect_sources()
    if not sources:
        return "Nothing to graph yet: no USER.md / MEMORY.md / Claude memories."
    changed = mirror_corpus(sources)
    if not changed and GRAPH.exists() and BUILT_STAMP.exists() and not force:
        return "Knowledge graph already up to date."
    BUILT_STAMP.unlink(missing_ok=True)

    model = extraction_model()
    cmd = [
        graphify, "extract", str(CORPUS),
        "--backend", "openai",
        # The JARVIS server answers one CLI call at a time per model.
        "--max-concurrency", "1",
    ]
    if force:
        cmd.append("--force")
    proc = subprocess.run(
        cmd, env=_graphify_env(model), capture_output=True, text=True, timeout=1800
    )
    if proc.returncode != 0 or not GRAPH.exists():
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise RuntimeError("graphify extract failed: " + " | ".join(tail))

    subprocess.run(
        [graphify, "export", "obsidian"],
        cwd=CORPUS, capture_output=True, timeout=120,
    )
    from openjarvis.tools.knowledge_graph_view import _counts, prepare_vault

    prepare_vault(VAULT)
    BUILT_STAMP.parent.mkdir(parents=True, exist_ok=True)
    BUILT_STAMP.touch()
    nodes, edges = _counts(GRAPH)
    return (
        f"Knowledge graph built with {model}: {len(sources)} files, "
        f"{nodes} nodes, {edges} relations."
    )


def main(argv: List[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        logger.info(sync(force="--force" in argv))
    except Exception as exc:  # noqa: BLE001 — a CLI entry point
        logger.error("Knowledge sync failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
