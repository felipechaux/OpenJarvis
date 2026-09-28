"""Tests for the Antigravity CLI (``agy``) engine backend."""

from __future__ import annotations

import asyncio
import json
import subprocess
from unittest.mock import patch

import pytest

from openjarvis.core.types import Message, Role
from openjarvis.engine import antigravity_cli as ag
from openjarvis.engine.antigravity_cli import AntigravityCLIEngine
from openjarvis.server.cloud_router import is_cloud_model

HOLA = [Message(role=Role.USER, content="Hola")]


def _engine() -> AntigravityCLIEngine:
    return AntigravityCLIEngine(binary="/fake/agy", timeout=30)


def _events(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _step(step_type: str, delta: str = "", state: str = "ACTIVE") -> dict:
    step = {"step_index": 1, "state": state, "step_type": step_type}
    if delta:
        step["text_delta"] = delta
    return {"event": "step_update", "step_update": step}


def _result(status: str = "SUCCESS", error: str = "") -> dict:
    res = {"status": status, "response": ""}
    if error:
        res["error"] = error
    return {"event": "result", "result": res}


REPLY = _events(
    {"event": "init", "conversation_id": "c", "init": {"cwd": "/w", "tools": []}},
    _step("user_input", state="DONE"),
    _step("tool", "should not leak"),
    _step("agent_response", "Hola, "),
    _step("agent_response", "señor.", state="DONE"),
    _result(),
)


@pytest.fixture(autouse=True)
def _tmp_workspace(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ag, "_WORKSPACE", tmp_path / "ws")
    monkeypatch.setattr(ag, "_models_cache", (0.0, []))


class TestCommand:
    def test_flags(self) -> None:
        eng = AntigravityCLIEngine(binary="/fake/agy", timeout=30)
        cmd = eng._build_cmd("antigravity/gemini-3.1-pro-high", "hola")
        assert cmd[:3] == ["/fake/agy", "-p", "hola"]
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert cmd[cmd.index("--model") + 1] == "gemini-3.1-pro-high"
        assert cmd[cmd.index("--print-timeout") + 1] == "30s"
        assert "--sandbox" in cmd and "--disable-slash-commands" in cmd

    def test_default_model_omits_flag(self) -> None:
        assert "--model" not in _engine()._build_cmd("antigravity/default", "hola")


class TestParsing:
    def test_only_agent_response_text(self) -> None:
        kinds = [ag.parse_event(line) for line in REPLY.splitlines()]
        assert [k for k in kinds if k[0]] == [("text", "Hola, "), ("text", "señor.")]

    def test_failed_result(self) -> None:
        line = json.dumps(_result("ERROR", "authentication required"))
        assert ag.parse_event(line) == ("error", "authentication required")

    def test_auth_error_explained(self) -> None:
        assert "run `agy`" in ag.explain_error("", "authentication required")

    def test_parse_models(self) -> None:
        out = (
            "Fetching available models...\n"
            "gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
            "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
        )
        assert ag.parse_models(out) == [
            "antigravity/gemini-3.1-pro-high",
            "antigravity/claude-sonnet-4-6",
        ]


class TestGenerate:
    def test_success(self) -> None:
        fake = subprocess.CompletedProcess([], 0, stdout=REPLY, stderr="")
        with patch("subprocess.run", return_value=fake) as run:
            res = _engine().generate(HOLA, model="antigravity/default")
        assert res["content"] == "Hola, señor."
        assert run.call_args.kwargs["cwd"].endswith("ws")

    def test_error_raises(self) -> None:
        fake = subprocess.CompletedProcess(
            [],
            1,
            stdout=_events(_result("ERROR", "authentication required")),
            stderr="",
        )
        with patch("subprocess.run", return_value=fake):
            with pytest.raises(RuntimeError, match="run `agy`"):
                _engine().generate(HOLA, model="antigravity/default")


class _FakeStream:
    def __init__(self, data: bytes):
        self._lines = data.splitlines(keepends=True)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    async def read(self) -> bytes:
        return b""


class _FakeProc:
    def __init__(self, stdout: str, rc: int = 0):
        self.stdout = _FakeStream(stdout.encode())
        self.stderr = _FakeStream(b"")
        self.returncode = None
        self._rc = rc

    async def wait(self) -> int:
        self.returncode = self._rc
        return self._rc

    def kill(self) -> None:
        self.returncode = -9


class TestStream:
    def test_stream_yields_text(self) -> None:
        async def fake_exec(*args, **kwargs):
            return _FakeProc(REPLY)

        async def collect():
            return [
                d async for d in _engine().stream(HOLA, model="antigravity/default")
            ]

        with patch("asyncio.create_subprocess_exec", fake_exec):
            assert "".join(asyncio.run(collect())) == "Hola, señor."

    def test_stream_error(self) -> None:
        async def fake_exec(*args, **kwargs):
            return _FakeProc(_events(_result("ERROR", "quota exceeded")), rc=1)

        async def collect():
            return [
                d async for d in _engine().stream(HOLA, model="antigravity/default")
            ]

        with patch("asyncio.create_subprocess_exec", fake_exec):
            with pytest.raises(RuntimeError, match="quota"):
                asyncio.run(collect())


class TestModels:
    def test_list_models_from_cli(self) -> None:
        fake = subprocess.CompletedProcess(
            [], 0, stdout="gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n", stderr=""
        )
        with patch("subprocess.run", return_value=fake):
            models = _engine().list_models()
        assert models == ["antigravity/default", "antigravity/gemini-3.8-flash-high"]

    def test_list_models_falls_back_to_static(self) -> None:
        with patch("subprocess.run", side_effect=OSError("boom")):
            assert _engine().list_models() == ag.ANTIGRAVITY_MODELS

    def test_not_routed_to_cloud(self) -> None:
        assert not is_cloud_model("antigravity/gemini-3.1-pro-high")
        assert not is_cloud_model("antigravity/claude-sonnet-4-6")
