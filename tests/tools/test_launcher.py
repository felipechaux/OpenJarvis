"""Tests for macOS launcher tools (open_app, start_coding_session)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from openjarvis.core.registry import ToolRegistry
from openjarvis.tools.launcher import (
    OpenAppTool,
    StartCodingSessionTool,
    _is_project,
    _norm,
    build_session_command,
    discover_projects,
    resolve_app,
    resolve_project,
)


class TestAppResolution:
    def test_norm(self) -> None:
        assert _norm("Antigravity IDE") == "antigravityide"
        assert _norm("Dado-Match!") == "dadomatch"

    def test_resolve_with_alias(self, tmp_path: Path) -> None:
        fake_app = tmp_path / "Antigravity IDE.app"
        fake_app.mkdir()

        with patch(
            "openjarvis.tools.launcher._installed_apps",
            return_value={"Antigravity IDE": fake_app},
        ):
            app, candidates = resolve_app(
                "antigravity", aliases={"antigravity": "Antigravity IDE"}
            )
            assert app == fake_app
            assert candidates == []

    def test_resolve_exact(self, tmp_path: Path) -> None:
        fake_app = tmp_path / "Cursor.app"
        fake_app.mkdir()

        with patch(
            "openjarvis.tools.launcher._installed_apps",
            return_value={"Cursor": fake_app},
        ):
            app, candidates = resolve_app("Cursor")
            assert app == fake_app
            assert candidates == []

    def test_resolve_unknown_returns_candidates(self, tmp_path: Path) -> None:
        fake_app = tmp_path / "Cursor.app"
        with patch(
            "openjarvis.tools.launcher._installed_apps",
            return_value={"Cursor": fake_app},
        ):
            app, candidates = resolve_app("Cursr")
            assert app is None
            assert "Cursor" in candidates


class TestProjectDiscovery:
    def test_is_project_markers(self, tmp_path: Path) -> None:
        proj_git = tmp_path / "p_git"
        proj_git.mkdir()
        (proj_git / ".git").mkdir()
        assert _is_project(proj_git) is True

        proj_claude = tmp_path / "p_claude"
        proj_claude.mkdir()
        (proj_claude / ".claude").mkdir()
        assert _is_project(proj_claude) is True

        proj_gemini = tmp_path / "p_gemini"
        proj_gemini.mkdir()
        (proj_gemini / ".gemini").mkdir()
        assert _is_project(proj_gemini) is True

        proj_web = tmp_path / "synesthia"
        proj_web.mkdir()
        (proj_web / "index.html").write_text("<html></html>")
        assert _is_project(proj_web) is True

        not_proj = tmp_path / "plain_dir"
        not_proj.mkdir()
        assert _is_project(not_proj) is False

    def test_is_project_repo_subfolder(self, tmp_path: Path) -> None:
        # e.g. AI/openjarvis containing repo/ with pyproject.toml
        proj = tmp_path / "openjarvis"
        repo = proj / "repo"
        repo.mkdir(parents=True)
        (repo / "pyproject.toml").write_text("[project]")
        assert _is_project(proj) is True

    def test_discover_projects_stops_at_project(self, tmp_path: Path) -> None:
        root = tmp_path / "Developer"
        proj1 = root / "AI" / "openjarvis"
        proj1.mkdir(parents=True)
        (proj1 / ".claude").mkdir()
        # subfolder inside project shouldn't be separate
        (proj1 / "repo").mkdir()
        (proj1 / "repo" / ".git").mkdir()

        proj2 = root / "synesthia"
        proj2.mkdir(parents=True)
        (proj2 / "index.html").write_text("<!doctype html>")

        found = discover_projects([root], max_depth=3)
        assert proj1 in found
        assert proj2 in found
        # repo is not discovered separately: openjarvis is already a project
        assert (proj1 / "repo") not in found


class TestProjectResolution:
    def test_resolve_exact(self, tmp_path: Path) -> None:
        root = tmp_path / "Dev"
        p = root / "openjarvis"
        p.mkdir(parents=True)
        (p / ".claude").mkdir()

        res, cands = resolve_project("openjarvis", [root])
        assert res == p
        assert cands == []

    def test_resolve_ambiguous_substring_returns_candidates(
        self, tmp_path: Path
    ) -> None:
        # "jarvis" should return both "openjarvis" and "jarvis-personal"
        root = tmp_path / "Dev"
        oj = root / "openjarvis"
        oj.mkdir(parents=True)
        (oj / ".claude").mkdir()

        jp = root / "jarvis-personal"
        jp.mkdir(parents=True)
        (jp / ".git").mkdir()

        res, cands = resolve_project("jarvis", [root])
        assert res is None
        assert "openjarvis" in cands
        assert "jarvis-personal" in cands

    def test_resolve_relative_path(self, tmp_path: Path) -> None:
        root = tmp_path / "Dev"
        p = root / "AI" / "openjarvis"
        p.mkdir(parents=True)
        (p / ".git").mkdir()

        res, cands = resolve_project("AI/openjarvis", [root])
        assert res == p
        assert cands == []

    def test_resolve_path_traversal_guarded(self, tmp_path: Path) -> None:
        root = tmp_path / "Dev"
        root.mkdir()
        outside = tmp_path / "Outside"
        outside.mkdir()
        (outside / ".git").mkdir()

        res, _ = resolve_project("../Outside", [root])
        assert res is None


class TestToolsExecution:
    def test_tools_registration(self) -> None:
        ToolRegistry.register("open_app")(OpenAppTool)
        ToolRegistry.register("start_coding_session")(StartCodingSessionTool)
        assert ToolRegistry.contains("open_app")
        assert ToolRegistry.contains("start_coding_session")

    @patch("openjarvis.tools.launcher.sys.platform", "darwin")
    @patch("openjarvis.tools.launcher.subprocess.run")
    def test_open_app_tool(self, mock_run: MagicMock, tmp_path: Path) -> None:
        app_path = tmp_path / "Antigravity IDE.app"
        app_path.mkdir()
        proj_path = tmp_path / "openjarvis"
        proj_path.mkdir()
        (proj_path / ".git").mkdir()

        cfg = MagicMock()
        cfg.project_roots = [str(tmp_path)]
        cfg.max_depth = 3
        cfg.app_aliases = {}
        cfg.terminal = "Terminal"

        with patch(
            "openjarvis.tools.launcher._installed_apps",
            return_value={"Antigravity IDE": app_path},
        ):
            with patch("openjarvis.tools.launcher._launcher_config", return_value=cfg):
                tool = OpenAppTool()
                res = tool.execute(app="Antigravity IDE", project="openjarvis")
                assert res.success is True
                assert "Opened Antigravity IDE with project" in res.content
                mock_run.assert_called_once()
                args = mock_run.call_args[0][0]
                assert args == ["open", "-a", str(app_path), str(proj_path)]

    @patch("openjarvis.tools.session_control.tmux_bin", lambda: None)
    @patch("openjarvis.tools.launcher.sys.platform", "darwin")
    @patch("openjarvis.tools.launcher.subprocess.run")
    def test_start_session_defaults_to_claude(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        proj_path = tmp_path / "openjarvis"
        proj_path.mkdir()
        (proj_path / ".claude").mkdir()

        cfg = MagicMock()
        cfg.project_roots = [str(tmp_path)]
        cfg.max_depth = 3
        cfg.app_aliases = {}
        cfg.terminal = "Terminal"

        with patch("openjarvis.tools.launcher._launcher_config", return_value=cfg):
            with patch(
                "openjarvis.tools.launcher._claude_binary",
                return_value="/usr/local/bin/claude",
            ):
                res = StartCodingSessionTool().execute(
                    project="openjarvis", task="arregla los tests"
                )
                assert res.success is True
                assert "Started Claude Code in Terminal" in res.content
                assert res.metadata["cli"] == "claude"
                assert res.metadata["session_id"]
                mock_run.assert_called_once()
                script = mock_run.call_args[0][0][2]
                assert 'tell application "Terminal"' in script
                assert (
                    "/usr/local/bin/claude --session-id " + res.metadata["session_id"]
                    in script
                )
                assert "arregla los tests" in script

    @patch("openjarvis.tools.session_control.tmux_bin", lambda: None)
    @patch("openjarvis.tools.launcher.sys.platform", "darwin")
    @patch("openjarvis.tools.launcher.subprocess.run")
    def test_start_session_gemini_uses_antigravity(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        proj_path = tmp_path / "openjarvis"
        proj_path.mkdir()
        (proj_path / ".gemini").mkdir()

        cfg = MagicMock()
        cfg.project_roots = [str(tmp_path)]
        cfg.max_depth = 3
        cfg.app_aliases = {}
        cfg.terminal = "Terminal"

        with patch("openjarvis.tools.launcher._launcher_config", return_value=cfg):
            with patch(
                "openjarvis.tools.launcher._agy_binary",
                return_value="/Users/me/.local/bin/agy",
            ):
                res = StartCodingSessionTool().execute(
                    project="openjarvis", cli="gemini", task="optimiza la consulta"
                )
                assert res.success is True
                assert "Started Antigravity CLI in Terminal" in res.content
                assert res.metadata["cli"] == "antigravity"
                script = mock_run.call_args[0][0][2]
                assert "/Users/me/.local/bin/agy -i " in script
                assert "optimiza la consulta" in script


class TestTmuxLaunch:
    def _cfg(self, tmp_path: Path) -> MagicMock:
        cfg = MagicMock()
        cfg.project_roots = [str(tmp_path)]
        cfg.max_depth = 3
        cfg.terminal = "Terminal"
        return cfg

    def test_new_session_runs_in_tmux_and_attaches(self, tmp_path: Path) -> None:
        (tmp_path / "openjarvis" / ".git").mkdir(parents=True)
        sc = "openjarvis.tools.session_control"
        with (
            patch("openjarvis.tools.launcher.sys.platform", "darwin"),
            patch(
                "openjarvis.tools.launcher._launcher_config",
                return_value=self._cfg(tmp_path),
            ),
            patch(f"{sc}.tmux_bin", return_value="/bin/tmux"),
            patch(f"{sc}.has_session", return_value=False),
            patch(f"{sc}.hooks_settings_file", return_value=Path("/h.json")),
            patch(f"{sc}.new_session") as new,
            patch("openjarvis.tools.launcher._run_in_terminal") as term,
            patch("openjarvis.tools.launcher._claude_binary", return_value="claude"),
        ):
            res = StartCodingSessionTool().execute(project="openjarvis")
        assert res.success and res.metadata["tmux"] == "jarvis-openjarvis"
        name, cwd, command = new.call_args.args
        assert name == "jarvis-openjarvis" and cwd == tmp_path / "openjarvis"
        assert "--settings /h.json" in command
        assert "attach -t jarvis-openjarvis" in term.call_args.args[1]

    def test_existing_session_is_reused(self, tmp_path: Path) -> None:
        (tmp_path / "openjarvis" / ".git").mkdir(parents=True)
        sc = "openjarvis.tools.session_control"
        with (
            patch("openjarvis.tools.launcher.sys.platform", "darwin"),
            patch(
                "openjarvis.tools.launcher._launcher_config",
                return_value=self._cfg(tmp_path),
            ),
            patch(f"{sc}.tmux_bin", return_value="/bin/tmux"),
            patch(f"{sc}.has_session", return_value=True),
            patch(f"{sc}.new_session") as new,
            patch(f"{sc}.type_message") as typed,
            patch("openjarvis.tools.launcher._run_in_terminal"),
        ):
            res = StartCodingSessionTool().execute(project="openjarvis", task="sigue")
        assert res.success and "already running" in res.content
        new.assert_not_called()
        typed.assert_called_once_with("jarvis-openjarvis", "sigue")


class TestSessionCommand:
    def test_claude_task_is_positional_and_interactive(self, tmp_path: Path) -> None:
        with patch("openjarvis.tools.launcher._claude_binary", return_value="claude"):
            cmd = build_session_command("claude", tmp_path / "p", "hola mundo", "abc")
        assert cmd.endswith("claude --session-id abc --name 'jarvis: p' 'hola mundo'")

    def test_gemini_task_uses_prompt_interactive(self, tmp_path: Path) -> None:
        # A bare positional prompt makes gemini run one-shot and exit.
        with patch("openjarvis.tools.launcher._gemini_binary", return_value="gemini"):
            cmd = build_session_command("gemini", tmp_path / "p", "hola")
        assert cmd.endswith("gemini -i hola")

    def test_antigravity_task_uses_prompt_interactive(self, tmp_path: Path) -> None:
        with patch("openjarvis.tools.launcher._agy_binary", return_value="agy"):
            cmd = build_session_command("antigravity", tmp_path / "p", "hola")
        assert cmd.endswith("agy -i hola")

    def test_no_task(self, tmp_path: Path) -> None:
        with patch("openjarvis.tools.launcher._gemini_binary", return_value="gemini"):
            assert build_session_command("gemini", tmp_path / "p").endswith("&& gemini")
