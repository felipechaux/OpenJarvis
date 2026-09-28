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
        assert perm["text"] == "Claude pide permiso en dadomatch."

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
        assert client.post(
            "/v1/coding-sessions/events", content=b"not json"
        ).json() == {"accepted": False}
