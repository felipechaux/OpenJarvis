"""Tests for tmux-backed session control and the session events endpoint."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from openjarvis.server import coding_sessions_router as router_mod
from openjarvis.server import session_watcher as watcher
from openjarvis.tools import session_control as sc
from openjarvis.tools.launcher import build_assistant_command


class TestNames:
    def test_session_name_uses_parent_of_repo(self) -> None:
        assert sc.session_name(Path("/x/AI/openjarvis/repo")) == "jarvis-openjarvis"
        assert sc.session_name(Path("/x/Dado Match.app")) == "jarvis-dado-match-app"

    def test_resolve_only_jarvis_sessions(self) -> None:
        live = [("jarvis-openjarvis", "/p"), ("jarvis-dadomatch", "/q")]
        with patch.object(sc, "list_sessions", return_value=live):
            assert sc.resolve_session("OpenJarvis") == ("jarvis-openjarvis", [])
            name, options = sc.resolve_session("")
            assert name is None and len(options) == 2
        with patch.object(sc, "list_sessions", return_value=[]):
            assert sc.resolve_session("openjarvis") == (None, [])


class TestSendTool:
    def _run(self, **params):
        return sc.SendToSessionTool().execute(**params)

    def test_types_message(self) -> None:
        with (
            patch.object(sc, "tmux_bin", return_value="/bin/tmux"),
            patch.object(sc, "resolve_session", return_value=("jarvis-openjarvis", [])),
            patch.object(sc, "type_message") as typed,
            patch.object(sc, "capture_screen", return_value="> corre los tests"),
            patch.object(sc.time, "sleep"),
        ):
            res = self._run(project="openjarvis", message="corre los tests")
        assert res.success
        typed.assert_called_once_with("jarvis-openjarvis", "corre los tests")
        assert "corre los tests" in res.content

    def test_approve_presses_one(self) -> None:
        with (
            patch.object(sc, "tmux_bin", return_value="/bin/tmux"),
            patch.object(sc, "resolve_session", return_value=("jarvis-openjarvis", [])),
            patch.object(sc, "press_keys") as pressed,
            patch.object(sc, "capture_screen", return_value=""),
            patch.object(sc.time, "sleep"),
        ):
            assert self._run(project="openjarvis", keys="approve").success
        pressed.assert_called_once_with("jarvis-openjarvis", ["1"])

    def test_no_session_explains(self) -> None:
        with (
            patch.object(sc, "tmux_bin", return_value="/bin/tmux"),
            patch.object(sc, "resolve_session", return_value=(None, [])),
        ):
            res = self._run(project="openjarvis", message="hola")
        assert not res.success
        assert "start_coding_session" in res.content

    def test_rejects_bad_input(self) -> None:
        assert not self._run(project="p").success
        assert not self._run(project="p", keys="rm -rf").success
        assert not self._run(project="p", message="x" * 5000).success

    def test_capture_refuses_foreign_sessions(self) -> None:
        assert sc.capture_screen("my-own-session") == ""


class TestLaunchCommand:
    def test_claude_gets_hooks_settings(self, tmp_path: Path) -> None:
        with patch("openjarvis.tools.launcher._claude_binary", return_value="claude"):
            cmd = build_assistant_command(
                "claude", tmp_path / "p", "hola", "sid", "/h/hooks.json"
            )
        expected = "claude --session-id sid --name 'jarvis: p' --settings /h/hooks.json"
        assert cmd == expected + " hola"

    def test_hooks_file_posts_to_jarvis(self, tmp_path: Path) -> None:
        with patch.object(sc, "_HOOKS_FILE", tmp_path / "hooks.json"):
            data = json.loads(sc.hooks_settings_file().read_text())
        command = data["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert "/v1/coding-sessions/events" in command and "|| true" in command
        assert set(data["hooks"]) == {"Stop", "Notification"}


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
class TestRealTmux:
    def test_type_and_capture(self, tmp_path: Path) -> None:
        name = f"jarvis-test-{uuid.uuid4().hex[:6]}"
        try:
            sc.new_session(name, tmp_path, "cat")
            assert sc.has_session(name)
            sc.type_message(name, "hola\ndesde jarvis")
            time.sleep(0.5)
            assert "hola desde jarvis" in sc.capture_screen(name)
        finally:
            subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


class TestEventsRouter:
    def test_stop_and_permission_are_announced(self) -> None:
        stop = router_mod.event_from_hook(
            {"hook_event_name": "Stop", "cwd": "/x/openjarvis/repo", "session_id": "s"}
        )
        assert stop["text"] == "Claude terminó en openjarvis."
        perm = router_mod.event_from_hook(
            {
                "hook_event_name": "Notification",
                "cwd": "/x/dadomatch",
                "message": "Claude needs your permission to use Bash",
            }
        )
        assert perm["text"] == "Claude pide permiso en dadomatch para usar Bash."

    def test_stop_includes_summary_of_last_reply(self, tmp_path: Path) -> None:
        transcript = tmp_path / "s.jsonl"
        rows = [
            {"type": "user", "cwd": "/x/openjarvis", "message": {"content": "arregla"}},
            {
                "type": "assistant",
                "cwd": "/x/openjarvis",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Arreglé los **tests** de `coding_sessions`. "
                            "Detalles:\n```py\nx=1\n```",
                        }
                    ]
                },
            },
        ]
        transcript.write_text("\n".join(json.dumps(r) for r in rows))
        event = router_mod.event_from_hook(
            {
                "hook_event_name": "Stop",
                "cwd": "/x/openjarvis",
                "transcript_path": str(transcript),
            }
        )
        # Too fresh: the reply may not be flushed yet, so it isn't served.
        event = router_mod.add_event(event)
        assert router_mod._finalize(event, event["ts"]) is False
        assert router_mod._finalize(event, event["ts"] + 5) is True
        assert event["text"] == (
            "Claude terminó en openjarvis: Arreglé los tests de coding_sessions."
        )

    def test_summary_is_truncated(self) -> None:
        long = "palabra " * 60
        assert len(router_mod.summarize_reply(long, limit=50)) == 50

    def test_idle_notice_and_reentrant_stop_are_skipped(self) -> None:
        assert (
            router_mod.event_from_hook(
                {
                    "hook_event_name": "Notification",
                    "message": "Claude is waiting for your input",
                }
            )
            is None
        )
        assert (
            router_mod.event_from_hook(
                {"hook_event_name": "Stop", "stop_hook_active": True}
            )
            is None
        )

    def test_endpoint_roundtrip(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router_mod.router)
        client = TestClient(app)
        before = client.get("/v1/coding-sessions/events").json()["last_id"]
        r = client.post(
            "/v1/coding-sessions/events",
            json={"hook_event_name": "Stop", "cwd": "/x/openjarvis"},
        )
        assert r.json()["accepted"] is True
        events = client.get(f"/v1/coding-sessions/events?after={before}").json()[
            "events"
        ]
        assert [e["text"] for e in events] == ["Claude terminó en openjarvis."]
        eid = events[0]["id"]
        assert client.post(f"/v1/coding-sessions/events/{eid}/announced").json()["ok"]
        after = client.get(f"/v1/coding-sessions/events?after={before}").json()
        assert after["events"][0]["announced"] is True
        assert client.post(
            "/v1/coding-sessions/events", content=b"not json"
        ).json() == {"accepted": False}


class TestProgress:
    SCREEN_WORKING = (
        "✽ Mustering… (12m 28s · ↓ 25.1k tokens · thinking)\n esc to interrupt"
    )
    SCREEN_IDLE = "❯ \n  ⏵⏵ auto mode on"

    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(watcher, "_state", {})

    def test_summary_groups_actions(self) -> None:
        actions = [
            watcher.describe_action("Read", {"file_path": "/a/x.py"}),
            watcher.describe_action("Edit", {"file_path": "/a/launcher.py"}),
            watcher.describe_action("Edit", {"file_path": "/a/launcher.py"}),
            watcher.describe_action("Bash", {"description": "Run the tests"}),
        ]
        assert watcher.summarize_actions(actions) == (
            "editó launcher.py, ejecutó Run the tests y revisó 1 archivo"
        )

    def test_elapsed_from_spinner(self) -> None:
        assert watcher._elapsed_from_screen(self.SCREEN_WORKING) == 748
        assert watcher._elapsed_from_screen("(1h 2m 3s · x)") == 3723
        assert watcher._elapsed_from_screen("nothing") is None

    def test_actions_then_interval(self) -> None:
        edit = [("edit", "b.py")]
        t0 = 1000.0
        idle = "x esc to interrupt"  # working, no spinner time
        assert watcher.step("s", "openjarvis", idle, edit, t0, 60) is None
        first = watcher.step("s", "openjarvis", idle, [("run", "pytest")], t0 + 21, 60)
        assert (
            first["text"] == "Claude sigue en openjarvis: editó b.py y ejecutó pytest."
        )
        assert watcher.step("s", "openjarvis", idle, edit, t0 + 50, 60) is None
        second = watcher.step("s", "openjarvis", idle, [], t0 + 82, 60)
        assert second["text"] == "Claude sigue en openjarvis: editó b.py."

    def test_heartbeat_during_long_thinking(self) -> None:
        # Seen for the first time 12 min into the turn: no tool calls at all.
        event = watcher.step("s", "openjarvis", self.SCREEN_WORKING, [], 5000.0, 60)
        assert event["text"] == (
            "Claude sigue trabajando en openjarvis; lleva 12 minutos."
        )
        assert (
            watcher.step("s", "openjarvis", self.SCREEN_WORKING, [], 5060.0, 60) is None
        )

    def test_idle_screen_resets(self) -> None:
        watcher.step("s", "p", "esc to interrupt", [("edit", "a")], 1000.0, 60)
        assert watcher.step("s", "p", self.SCREEN_IDLE, [], 1030.0, 60) is None
        assert watcher._state["s"]["actions"] == []

    def test_read_new_actions_skips_history(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        old = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {"file_path": "/old.py"},
                    }
                ]
            },
        }
        path.write_text(json.dumps(old) + "\n")
        st: dict = {}
        assert watcher.read_new_actions(st, path) == []  # history not replayed
        new = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "pytest -q"},
                    }
                ]
            },
        }
        with path.open("a") as fh:
            fh.write(json.dumps(new) + "\n")
            fh.write('{"type": "assistant", "mess')  # partial line
        assert watcher.read_new_actions(st, path) == [("run", "pytest -q")]
        with path.open("a") as fh:
            fh.write('age": {"content": []}}\n')
        assert watcher.read_new_actions(st, path) == []
        assert st["partial"] == ""

    def test_summary_antigravity_tools(self) -> None:
        actions = [
            watcher.describe_action("view_file", {"AbsolutePath": "/a/x.py"}),
            watcher.describe_action(
                "replace_file_content", {"TargetFile": "/a/launcher.py"}
            ),
            watcher.describe_action(
                "run_command", {"toolSummary": "Run pytest", "CommandLine": "pytest"}
            ),
            watcher.describe_action("invoke_subagent", {"Role": "Codebase Researcher"}),
        ]
        assert watcher.summarize_actions(actions) == (
            "editó launcher.py, ejecutó Run pytest, "
            "lanzó subagentes (Codebase Researcher) y 1 más"
        )

    def test_read_new_actions_antigravity(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.jsonl"
        old = {
            "type": "PLANNER_RESPONSE",
            "tool_calls": [
                {
                    "name": "view_file",
                    "args": {"AbsolutePath": "/old.py"},
                }
            ],
        }
        path.write_text(json.dumps(old) + "\n")
        st: dict = {}
        assert watcher.read_new_actions(st, path) == []
        new = {
            "type": "PLANNER_RESPONSE",
            "tool_calls": [
                {
                    "name": "run_command",
                    "args": {"CommandLine": "pytest -q"},
                }
            ],
        }
        with path.open("a") as fh:
            fh.write(json.dumps(new) + "\n")
        assert watcher.read_new_actions(st, path) == [("run", "pytest -q")]

    def test_antigravity_step_progress_text(self) -> None:
        edit = [("edit", "main.py")]
        t0 = 1000.0
        screen = "Thinking... Esc to cancel"
        assert (
            watcher.step("s-agy", "openjarvis", screen, edit, t0, 60, cli="antigravity")
            is None
        )
        res = watcher.step(
            "s-agy",
            "openjarvis",
            screen,
            [("run", "pytest")],
            t0 + 25,
            60,
            cli="antigravity",
        )
        assert res is not None
        assert (
            res["text"]
            == "Antigravity sigue en openjarvis: editó main.py y ejecutó pytest."
        )
        assert res["cli"] == "antigravity"
        assert res["cli_label"] == "Antigravity"

    def test_antigravity_idle_when_transcript_stops(self) -> None:
        t0 = 5000.0
        # "Thinking" left on screen must not keep an idle session "working".
        screen = "Thinking about it"
        watcher.step("s-idle", "p", screen, [("edit", "a.py")], t0, 60, "antigravity")
        late = t0 + watcher._AGY_IDLE_S + 1
        assert watcher.step("s-idle", "p", screen, [], late, 60, "antigravity") is None
        assert watcher._state["s-idle"]["working_since"] is None

    def test_antigravity_args_are_json_decoded(self) -> None:
        # agy's transcript stores every argument JSON-encoded.
        assert watcher.describe_action(
            "run_command",
            {"CommandLine": '"git status -s"', "toolSummary": '"Git status check"'},
        ) == ("run", "Git status check")
        assert watcher.describe_action(
            "replace_file_content", {"TargetFile": '"/a/launcher.py"'}
        ) == ("edit", "launcher.py")

    def test_hooks_only_stop_and_notification(self, tmp_path: Path) -> None:
        with patch.object(sc, "_HOOKS_FILE", tmp_path / "hooks.json"):
            data = json.loads(sc.hooks_settings_file().read_text())
        assert set(data["hooks"]) == {"Stop", "Notification"}


class TestAntigravitySessions:
    LIVE = [
        ("jarvis-openjarvis", "/p"),
        ("jarvis-openjarvis-agy", "/p"),
        ("jarvis-dadomatch-agy", "/q"),
    ]

    def test_antigravity_hooks_file(self, tmp_path: Path) -> None:
        g = tmp_path / "config" / "hooks.json"
        g.parent.mkdir()
        g.write_text(json.dumps({"mine": {"Stop": []}}))
        with patch.object(sc, "_ANTIGRAVITY_GLOBAL_HOOKS_FILE", g):
            sc.antigravity_hooks_file()
        data = json.loads(g.read_text())
        assert data["mine"] == {"Stop": []}  # the user's own group is kept
        stop = data["openjarvis"]["Stop"][0]["command"]
        post = data["openjarvis"]["PostToolUse"][0]["hooks"][0]["command"]
        assert "X-OpenJarvis-Hook: Stop" in stop
        assert "X-OpenJarvis-Hook: PostToolUse" in post
        assert "X-OpenJarvis-CLI: antigravity" in stop

    def test_antigravity_hooks_file_leaves_broken_json(self, tmp_path: Path) -> None:
        g = tmp_path / "hooks.json"
        g.write_text("{not json")
        with patch.object(sc, "_ANTIGRAVITY_GLOBAL_HOOKS_FILE", g):
            sc.antigravity_hooks_file()
        assert g.read_text() == "{not json"

    def test_names_do_not_collide(self) -> None:
        p = Path("/x/AI/openjarvis/repo")
        assert sc.session_name(p, "claude") == "jarvis-openjarvis"
        assert sc.session_name(p, "antigravity") == "jarvis-openjarvis-agy"
        assert sc.session_cli("jarvis-openjarvis-agy") == "antigravity"
        assert sc.session_cli("jarvis-openjarvis") == "claude"

    def test_resolve_prefers_claude_unless_asked(self) -> None:
        with patch.object(sc, "list_sessions", return_value=self.LIVE):
            assert sc.resolve_session("openjarvis") == ("jarvis-openjarvis", [])
            assert sc.resolve_session("openjarvis", "antigravity") == (
                "jarvis-openjarvis-agy",
                [],
            )
            assert sc.resolve_session("dadomatch") == ("jarvis-dadomatch-agy", [])

    def test_send_to_antigravity(self) -> None:
        with (
            patch.object(sc, "tmux_bin", return_value="/bin/tmux"),
            patch.object(sc, "list_sessions", return_value=self.LIVE),
            patch.object(sc, "type_message") as typed,
            patch.object(sc, "capture_screen", return_value=""),
            patch.object(sc.time, "sleep"),
        ):
            res = sc.SendToSessionTool().execute(
                project="openjarvis", cli="gemini", message="revisa el README"
            )
        assert res.success
        typed.assert_called_once_with("jarvis-openjarvis-agy", "revisa el README")

    @pytest.mark.parametrize(
        "keys,pressed_keys",
        [
            ("approve", ["1"]),
            ("approve_always", ["2"]),  # conversation scope, never "3" (persist)
            ("deny", ["Escape"]),
            ("interrupt", ["Escape"]),
        ],
    )
    def test_antigravity_permission_keys(self, keys, pressed_keys) -> None:
        with (
            patch.object(sc, "tmux_bin", return_value="/bin/tmux"),
            patch.object(sc, "list_sessions", return_value=self.LIVE),
            patch.object(sc, "press_keys") as pressed,
            patch.object(sc, "capture_screen", return_value=""),
            patch.object(sc.time, "sleep"),
        ):
            res = sc.SendToSessionTool().execute(
                project="openjarvis", cli="antigravity", keys=keys
            )
        assert res.success
        pressed.assert_called_once_with("jarvis-openjarvis-agy", pressed_keys)

    def test_coding_sessions_reports_agy_screen(self) -> None:
        from openjarvis.tools import coding_sessions as cs

        with (
            patch.object(sc, "list_sessions", return_value=self.LIVE),
            patch.object(sc, "capture_screen", return_value="> revisando README"),
        ):
            res = cs.CodingSessionsTool().execute(
                action="status", project="openjarvis", cli="antigravity"
            )
        assert "Antigravity CLI in openjarvis" in res.content
        assert "revisando README" in res.content


class TestTranscriptSelection:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        from openjarvis.tools import coding_sessions as cs

        monkeypatch.setattr(watcher, "_transcripts", {})
        monkeypatch.setattr(cs, "CLAUDE_DIR", tmp_path)
        self.folder = tmp_path / cs._claude_slug("/x/openjarvis")
        self.folder.mkdir()

    def test_uses_session_id_not_newest_file(self) -> None:
        mine = self.folder / "11111111-1111-1111-1111-111111111111.jsonl"
        other = self.folder / "22222222-2222-2222-2222-222222222222.jsonl"
        mine.write_text("{}\n")
        other.write_text("{}\n")  # newer, but belongs to another session
        with patch.object(sc, "claude_session_id", return_value=mine.stem):
            assert watcher._transcript_for("jarvis-openjarvis", "/x/openjarvis") == mine

    def test_hook_transcript_wins(self, tmp_path: Path) -> None:
        resumed = tmp_path / "resumed.jsonl"
        resumed.write_text("{}\n")
        watcher.remember_transcript("/x/openjarvis", str(resumed))
        with patch.object(sc, "claude_session_id", return_value="nope"):
            assert (
                watcher._transcript_for("jarvis-openjarvis", "/x/openjarvis") == resumed
            )

    def test_no_guessing_without_id(self) -> None:
        (self.folder / "33333333-3333-3333-3333-333333333333.jsonl").write_text("{}\n")
        with patch.object(sc, "claude_session_id", return_value=""):
            assert watcher._transcript_for("jarvis-openjarvis", "/x/openjarvis") is None

    def test_hook_transcript_stays_with_its_cli(self, tmp_path: Path) -> None:
        agy = tmp_path / "transcript.jsonl"
        agy.write_text("{}\n")
        watcher.remember_transcript("/x/openjarvis", str(agy), "antigravity")
        with patch.object(sc, "claude_session_id", return_value=""):
            assert watcher._transcript_for("jarvis-openjarvis", "/x/openjarvis") is None
            assert (
                watcher._transcript_for("jarvis-openjarvis-agy", "/x/openjarvis") == agy
            )

    def test_antigravity_transcript_from_conversation_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(watcher, "_AGY_BRAIN_DIR", tmp_path)
        logs = tmp_path / "abc" / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        (logs / "transcript.jsonl").write_text("{}\n")
        assert watcher.antigravity_transcript("abc") == str(logs / "transcript.jsonl")
        assert watcher.antigravity_transcript("missing") == ""
        assert watcher.antigravity_transcript("../abc") == ""
