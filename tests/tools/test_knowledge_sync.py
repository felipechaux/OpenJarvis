"""knowledge_sync builds the graphify knowledge graph through JARVIS."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openjarvis.tools import knowledge_sync as ks


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(ks.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ks, "_CLAUDE_PROJECTS", tmp_path / ".claude" / "projects")
    corpus = tmp_path / "corpus"
    monkeypatch.setattr(ks, "CORPUS", corpus)
    monkeypatch.setattr(ks, "GRAPH", corpus / "graphify-out" / "graph.json")
    monkeypatch.setattr(ks, "VAULT", corpus / "graphify-out" / "obsidian")
    monkeypatch.setattr(ks, "BUILT_STAMP", tmp_path / ".last-build")
    monkeypatch.setattr(ks, "KNOWLEDGE", tmp_path / "knowledge")
    (tmp_path / ".openjarvis").mkdir()
    (tmp_path / ".openjarvis" / "USER.md").write_text("# User\n\nVive en Bogotá.\n")
    (tmp_path / ".openjarvis" / "MEMORY.md").write_text("# Agent Memory\n\n")
    mem = tmp_path / ".claude" / "projects" / "-Users-x-app" / "memory"
    mem.mkdir(parents=True)
    (mem / "stack.md").write_text("Usa React Native.")
    (mem / "mongo-staging-credentials.md").write_text("user: x / pass: y")
    (mem / "API_TOKEN_notes.md").write_text("tok")
    return tmp_path


_LOAD_CONFIG = "openjarvis.core.config.load_config"


def _config(model: str, fast: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        intelligence=SimpleNamespace(default_model=model, fast_model=fast),
        server=SimpleNamespace(port=8000),
    )


def test_collects_jarvis_files_and_claude_memories(home: Path) -> None:
    rels = sorted(str(r) for r in ks.collect_sources().values())
    # An empty MEMORY.md template and credential files are skipped.
    assert rels == ["claude/-Users-x-app/stack.md", "jarvis/USER.md"]


def test_mirror_copies_and_drops_stale(home: Path) -> None:
    stale = ks.CORPUS / "claude" / "old" / "gone.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("x")
    assert ks.mirror_corpus(ks.collect_sources(), ks.CORPUS)
    assert not stale.exists() and not stale.parent.exists()
    assert (ks.CORPUS / "jarvis" / "USER.md").read_text().endswith("Bogotá.\n")
    assert not ks.mirror_corpus(ks.collect_sources(), ks.CORPUS)  # no change


@pytest.mark.parametrize(
    ("default", "fast", "expected"),
    [
        ("claude-cli/sonnet", "", "claude-cli/haiku"),
        ("kiro-cli/auto", "", "kiro-cli/claude-haiku-4.5"),
        ("antigravity/default", "", "antigravity/default"),
        ("claude-cli/opus", "gemini-cli/default", "gemini-cli/default"),
    ],
)
def test_extraction_model(default: str, fast: str, expected: str) -> None:
    cfg = _config(default, fast)
    with patch("openjarvis.core.config.load_config", return_value=cfg):
        assert ks.extraction_model() == expected


def test_sync_runs_graphify_against_jarvis_server(home: Path) -> None:
    def fake_run(cmd, **kwargs):
        if cmd[1] == "extract":
            ks.GRAPH.parent.mkdir(parents=True, exist_ok=True)
            ks.GRAPH.write_text(json.dumps({"nodes": [{"id": "a"}], "links": []}))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patch.object(ks, "graphify_bin", return_value="/bin/graphify"),
        patch(_LOAD_CONFIG, return_value=_config("claude-cli/sonnet")),
        patch.object(ks.subprocess, "run", side_effect=fake_run) as run,
        patch("openjarvis.tools.knowledge_graph_view.prepare_vault"),
    ):
        summary = ks.sync()
        assert ks.sync() == "Knowledge graph already up to date."
    extract = run.call_args_list[0]
    assert extract.args[0][:3] == ["/bin/graphify", "extract", str(ks.CORPUS)]
    env = extract.kwargs["env"]
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:8000/v1"
    assert env["OPENAI_MODEL"] == "claude-cli/haiku"
    assert run.call_args_list[1].args[0] == ["/bin/graphify", "export", "obsidian"]
    assert "1 nodes" in summary


def test_sync_reports_graphify_failure(home: Path) -> None:
    failed = subprocess.CompletedProcess([], 1, "", "boom: server down")
    with (
        patch.object(ks, "graphify_bin", return_value="/bin/graphify"),
        patch(_LOAD_CONFIG, return_value=_config("claude-cli/sonnet")),
        patch.object(ks.subprocess, "run", return_value=failed),
    ):
        with pytest.raises(RuntimeError, match="server down"):
            ks.sync()


def test_sync_without_graphify(home: Path) -> None:
    with patch.object(ks, "graphify_bin", return_value=None):
        with pytest.raises(RuntimeError, match="graphify is not installed"):
            ks.sync()


def test_exclude_patterns_come_from_config(home: Path) -> None:
    cfg = SimpleNamespace(tools=SimpleNamespace(
        knowledge=SimpleNamespace(exclude=["stack*"])
    ))
    with patch(_LOAD_CONFIG, return_value=cfg):
        rels = sorted(str(r) for r in ks.collect_sources().values())
    assert "claude/-Users-x-app/stack.md" not in rels
    assert "claude/-Users-x-app/mongo-staging-credentials.md" in rels


def _fake_graphify(fail: bool = False):
    def run(cmd, **kwargs):
        if fail:
            return subprocess.CompletedProcess(cmd, 1, "", "server down")
        if cmd[1] == "extract":
            ks.GRAPH.parent.mkdir(parents=True, exist_ok=True)
            ks.GRAPH.write_text(json.dumps({"nodes": [], "links": []}))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return run


def test_failed_build_is_retried_next_time(home: Path) -> None:
    common = (
        patch.object(ks, "graphify_bin", return_value="/bin/graphify"),
        patch(_LOAD_CONFIG, return_value=_config("claude-cli/sonnet")),
        patch("openjarvis.tools.knowledge_graph_view.prepare_vault"),
    )
    with common[0], common[1], common[2]:
        with patch.object(ks.subprocess, "run", side_effect=_fake_graphify(True)):
            with pytest.raises(RuntimeError):
                ks.sync()
        assert ks.needs_rebuild()  # sources copied, but no successful build
        with patch.object(ks.subprocess, "run", side_effect=_fake_graphify()):
            assert "nodes" in ks.sync()
        assert not ks.needs_rebuild()


def test_needs_rebuild_sees_new_claude_memory(home: Path) -> None:
    ks.mirror_corpus(ks.collect_sources())
    ks.GRAPH.parent.mkdir(parents=True)
    ks.GRAPH.write_text("{}")
    ks.BUILT_STAMP.touch()
    assert not ks.needs_rebuild()
    mem = home / ".claude" / "projects" / "-Users-x-app" / "memory"
    (mem / "nuevo.md").write_text("Le gusta el ciclismo.")
    assert ks.needs_rebuild()
    assert not (ks.CORPUS / "claude" / "-Users-x-app" / "nuevo.md").exists()
