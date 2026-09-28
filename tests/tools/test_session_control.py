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
        assert set(data["hooks"]) == {"Stop", "Notification", "PostToolUse"}


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
    def _hook(self, tool: str, **args) -> dict:
        return {
            "hook_event_name": "PostToolUse",
            "session_id": "sess-p",
            "cwd": "/x/openjarvis/repo",
            "tool_name": tool,
            "tool_input": args,
        }

    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(router_mod, "_progress", {})
        monkeypatch.setattr(router_mod, "_progress_interval", lambda: 60.0)

    def test_summary_groups_actions(self) -> None:
        actions = [
            router_mod.describe_action("Read", {"file_path": "/a/x.py"}),
            router_mod.describe_action("Edit", {"file_path": "/a/launcher.py"}),
            router_mod.describe_action("Edit", {"file_path": "/a/launcher.py"}),
            router_mod.describe_action("Bash", {"description": "Run the tests"}),
        ]
        assert router_mod.summarize_actions(actions) == (
            "editó launcher.py, ejecutó Run the tests y revisó 1 archivo"
        )
        reads = [router_mod.describe_action("Grep", {})] * 3
        assert router_mod.summarize_actions(reads) == (
            "está revisando el código (3 lecturas)"
        )

    def test_first_update_after_20s_then_every_interval(self) -> None:
        t0 = 1000.0
        track = router_mod._track_progress
        assert track(self._hook("Read", file_path="/a/x.py"), t0) is None
        assert track(self._hook("Edit", file_path="/a/b.py"), t0 + 5) is None
        first = track(self._hook("Bash", command="pytest -q"), t0 + 21)
        assert first["kind"] == "progress"
        assert first["text"] == (
            "Claude sigue en openjarvis: editó b.py, ejecutó pytest -q y revisó "
            "1 archivo."
        )
        # Next one waits a full interval after the last update.
        assert track(self._hook("Edit", file_path="/a/c.py"), t0 + 50) is None
        second = track(self._hook("Edit", file_path="/a/d.py"), t0 + 82)
        assert second["text"] == "Claude sigue en openjarvis: editó c.py y d.py."

    def test_stop_clears_pending_progress(self) -> None:
        router_mod._track_progress(self._hook("Edit", file_path="/a/b.py"), 1000.0)
        router_mod.event_from_hook(
            {"hook_event_name": "Stop", "session_id": "sess-p", "cwd": "/x/o"}
        )
        assert "sess-p" not in router_mod._progress

    def test_disabled_with_zero_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router_mod, "_progress_interval", lambda: 0.0)
        assert router_mod._track_progress(self._hook("Edit"), 1000.0) is None
        assert router_mod._progress == {}

    def test_hooks_include_post_tool_use(self, tmp_path: Path) -> None:
        with patch.object(sc, "_HOOKS_FILE", tmp_path / "hooks.json"):
            data = json.loads(sc.hooks_settings_file().read_text())
        assert "PostToolUse" in data["hooks"]


class TestAntigravitySessions:
    LIVE = [
        ("jarvis-openjarvis", "/p"),
        ("jarvis-openjarvis-agy", "/p"),
        ("jarvis-dadomatch-agy", "/q"),
    ]

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
