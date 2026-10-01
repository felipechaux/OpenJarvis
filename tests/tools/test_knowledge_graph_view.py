"""show_knowledge_graph opens the user's knowledge graph in Obsidian."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from openjarvis.agents.fast_paths import match_fast_path
from openjarvis.tools import knowledge_graph_view as kg

TOOLS = {"show_knowledge_graph", "open_app"}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    corpus = tmp_path / "corpus"
    (corpus / "graphify-out" / "obsidian").mkdir(parents=True)
    graph = {"nodes": [{"id": "a"}, {"id": "b"}], "links": [{"source": "a"}]}
    (corpus / "graphify-out" / "graph.json").write_text(json.dumps(graph))
    app = tmp_path / "Obsidian.app"
    app.mkdir()
    monkeypatch.setattr(kg, "_CORPUS", corpus)
    monkeypatch.setattr(kg, "_OBSIDIAN_APP", app)
    monkeypatch.setattr(kg, "_OBSIDIAN_CONFIG", tmp_path / "obsidian.json")
    monkeypatch.setattr(kg.sys, "platform", "darwin")
    monkeypatch.setattr(kg, "graphify_bin", lambda: None)
    return corpus


def test_opens_vault_in_graph_view(env: Path) -> None:
    with patch.object(kg.subprocess, "run") as run:
        res = kg.ShowKnowledgeGraphTool().execute()
    assert res.success
    assert res.metadata["nodes"] == 2 and res.metadata["edges"] == 1
    vault = env / "graphify-out" / "obsidian"
    url = run.call_args.args[0][1]
    assert url.startswith("obsidian://open?path=") and "obsidian" in url
    workspace = json.loads((vault / ".obsidian" / "workspace.json").read_text())
    assert workspace["active"] == "jarvis-graph"
    config = json.loads(kg._OBSIDIAN_CONFIG.read_text())
    assert [v["path"] for v in config["vaults"].values()] == [str(vault)]


def test_keeps_existing_layout_and_vaults(env: Path) -> None:
    vault = env / "graphify-out" / "obsidian"
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "workspace.json").write_text('{"mine": 1}')
    kg._OBSIDIAN_CONFIG.write_text(
        json.dumps({"vaults": {"x": {"path": str(vault)}, "y": {"path": "/o"}}})
    )
    with patch.object(kg.subprocess, "run"):
        assert kg.ShowKnowledgeGraphTool().execute().success
    assert (vault / ".obsidian" / "workspace.json").read_text() == '{"mine": 1}'
    assert len(json.loads(kg._OBSIDIAN_CONFIG.read_text())["vaults"]) == 2


def test_missing_graph_starts_a_build(env: Path) -> None:
    (env / "graphify-out" / "graph.json").unlink()
    with patch.object(kg, "schedule_sync", return_value=True) as schedule:
        res = kg.ShowKnowledgeGraphTool().execute()
    assert not res.success and "building it now" in res.content
    schedule.assert_called_once()


def test_missing_graph_without_graphify(env: Path) -> None:
    (env / "graphify-out" / "graph.json").unlink()
    with patch.object(kg, "schedule_sync", return_value=False):
        res = kg.ShowKnowledgeGraphTool().execute()
    assert not res.success and "graphify is not installed" in res.content


@pytest.mark.parametrize(
    "text",
    [
        "abre Obsidian",
        "Jarvis, muéstrame mi grafo de conocimiento",
        "abre el grafo",
        "enséñame nuestro grafo en obsidian, por favor",
    ],
)
def test_fast_path_matches(text: str) -> None:
    path = match_fast_path(text, TOOLS)
    assert path is not None and path.tool == "show_knowledge_graph"


def test_other_apps_still_go_to_open_app() -> None:
    path = match_fast_path("abre Spotify", TOOLS)
    assert path is not None and path.tool == "open_app"


def test_reply_mentions_node_count() -> None:
    path = match_fast_path("abre obsidian", TOOLS)
    result = kg.ToolResult(
        tool_name="show_knowledge_graph",
        content="ok",
        success=True,
        metadata={"nodes": 38},
    )
    assert path.reply(result) == (
        "Su grafo de conocimiento está abierto en Obsidian con 38 nodos, señor."
    )


class TestSync:
    @pytest.fixture(autouse=True)
    def _script(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        script = tmp_path / "sync.sh"
        script.write_text("#!/bin/bash\n")
        monkeypatch.setattr(kg, "_SYNC_SCRIPT", script)
        monkeypatch.setattr(kg, "_SYNC_LOG", tmp_path / "sync.log")
        monkeypatch.setattr(kg, "_KNOWLEDGE", tmp_path)
        self.home = tmp_path / "home"
        monkeypatch.setattr(
            kg, "_SYNCED_SOURCES", {self.home / "MEMORY.md", self.home / "USER.md"}
        )
        yield
        if kg._sync_timer is not None:
            kg._sync_timer.cancel()

    def _ok(self) -> kg.ToolResult:
        return kg.ToolResult(tool_name="memory_manage", content="ok", success=True)

    def test_write_to_memory_schedules_one_batched_run(self) -> None:
        with patch.object(kg.subprocess, "Popen") as popen:
            kg.sync_after_write(self.home / "MEMORY.md", self._ok())
            kg.sync_after_write(self.home / "USER.md", self._ok())
            timer = kg._sync_timer
            assert timer is not None and timer.is_alive()
            timer.cancel()
            kg._run_sync()  # what the timer would do, once
        popen.assert_called_once()
        assert popen.call_args.args[0][1] == str(kg._SYNC_SCRIPT)

    def test_without_script_runs_builtin_sync(self) -> None:
        kg._SYNC_SCRIPT.unlink()
        with (
            patch.object(kg, "graphify_bin", return_value="/bin/graphify"),
            patch.object(kg.subprocess, "Popen") as popen,
        ):
            kg._run_sync()
        assert popen.call_args.args[0][1:] == [
            "-m", "openjarvis.tools.knowledge_sync"
        ]

    def test_nothing_to_sync_with(self) -> None:
        kg._SYNC_SCRIPT.unlink()
        with patch.object(kg, "graphify_bin", return_value=None):
            assert kg.schedule_sync() is False

    def test_other_files_and_failures_do_not_sync(self, tmp_path: Path) -> None:
        with patch.object(kg, "schedule_sync") as schedule:
            kg.sync_after_write(tmp_path / "MEMORY.md", self._ok())
            failed = kg.ToolResult(tool_name="m", content="x", success=False)
            kg.sync_after_write(self.home / "MEMORY.md", failed)
        schedule.assert_not_called()

    def test_run_in_flight_is_followed_up(self) -> None:
        busy = type("P", (), {"poll": lambda self: None})()
        with (
            patch.object(kg, "_sync_proc", busy),
            patch.object(kg.subprocess, "Popen") as popen,
        ):
            kg._run_sync()
        popen.assert_not_called()
        assert kg._sync_timer is not None and kg._sync_timer.is_alive()

    def test_memory_manage_triggers_sync(self) -> None:
        from openjarvis.tools.memory_manage import MemoryManageTool

        tool = MemoryManageTool(memory_path=self.home / "MEMORY.md")
        with patch.object(kg, "schedule_sync") as schedule:
            assert tool.execute(action="add", entry="Le gusta el café").success
            tool.execute(action="read")
        schedule.assert_called_once()


class TestPeriodicRefresh:
    @pytest.fixture(autouse=True)
    def _reset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(kg, "_refresh_thread", None)
        monkeypatch.setattr(kg, "_SYNC_SCRIPT", tmp_path / "missing.sh")
        monkeypatch.setattr(kg, "graphify_bin", lambda: "/bin/graphify")

    def _run_once(self, changed: bool):
        """Run the refresh loop body once by making the second sleep stop it."""
        sleeps = iter([None, SystemExit()])

        def fake_sleep(_s):
            step = next(sleeps)
            if step is not None:
                raise step

        with (
            patch.object(kg.time, "sleep", side_effect=fake_sleep),
            patch.object(kg, "_sources_changed", return_value=changed),
            patch.object(kg, "schedule_sync") as schedule,
            patch.object(kg.threading, "Thread") as thread,
        ):
            assert kg.start_periodic_refresh(60)
            loop = thread.call_args.kwargs["target"]
            with pytest.raises(SystemExit):
                loop()
        return schedule

    def test_rebuilds_when_sources_changed(self) -> None:
        self._run_once(changed=True).assert_called_once_with(delay=1.0)

    def test_quiet_when_nothing_changed(self) -> None:
        self._run_once(changed=False).assert_not_called()

    def test_disabled_with_zero_interval(self) -> None:
        assert kg.start_periodic_refresh(0) is False
