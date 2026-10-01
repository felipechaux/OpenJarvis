"""Tests for the Kiro CLI (``kiro-cli``) engine backend."""

from __future__ import annotations

import asyncio
import json
import subprocess
from unittest.mock import patch

import pytest

from openjarvis.core.types import Message, Role
from openjarvis.engine import kiro_cli as kc
from openjarvis.engine.kiro_cli import KiroCLIEngine
from openjarvis.server.cloud_router import is_cloud_model

HOLA = [
    Message(role=Role.SYSTEM, content="Eres JARVIS."),
    Message(role=Role.USER, content="Hola"),
]


def _engine() -> KiroCLIEngine:
    return KiroCLIEngine(binary="/fake/kiro-cli", timeout=30)


def _events(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _chunk(text: str) -> dict:
    return {
        "type": "sessionUpdate",
        "data": {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    }


def _finished(text: str, status: str = "success") -> dict:
    return {
        "type": "runFinished",
        "data": {"sessionId": "s", "status": status, "finalText": text},
    }


def _run_error(message: str) -> dict:
    return {"type": "runError", "data": {"stage": "prompt", "message": message}}


REPLY = _events(
    {"type": "runStarted", "data": {"engine": "v2"}},
    {"type": "metadata", "data": {"sessionId": "s"}},
    _chunk("Hola, "),
    _chunk("señor."),
    _finished("Hola, señor."),
)


@pytest.fixture(autouse=True)
def _tmp_workspace(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(kc, "_WORKSPACE", tmp_path / "ws")
    monkeypatch.setattr(kc, "_models_cache", (0.0, []))
    monkeypatch.setattr(kc, "_failing", {})


class TestAgentProfile:
    def test_profile_pins_model_without_tools(self, tmp_path) -> None:
        name = _engine()._prepare("kiro-cli/claude-sonnet-4.6")
        assert name == "jarvis-claude-sonnet-4-6"
        path = tmp_path / "ws" / ".kiro" / "agents" / f"{name}.json"
        profile = json.loads(path.read_text())
        assert profile["name"] == name
        assert profile["model"] == "claude-sonnet-4.6"
        assert profile["tools"] == [] and profile["mcpServers"] == {}

    def test_auto_leaves_model_to_kiro(self, tmp_path) -> None:
        name = _engine()._prepare("kiro-cli/auto")
        path = tmp_path / "ws" / ".kiro" / "agents" / f"{name}.json"
        assert "model" not in json.loads(path.read_text())

    def test_flags(self) -> None:
        cmd = _engine()._build_cmd("jarvis-auto")
        assert cmd[:2] == ["/fake/kiro-cli", "chat"]
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert cmd[cmd.index("--agent") + 1] == "jarvis-auto"
        assert "--no-interactive" in cmd and "--trust-tools=" in cmd


class TestParsing:
    def test_text_chunks_and_final(self) -> None:
        kinds = [kc.parse_event(line) for line in REPLY.splitlines()]
        assert [k for k in kinds if k[0]] == [
            ("text", "Hola, "),
            ("text", "señor."),
            ("final", "Hola, señor."),
        ]

    def test_run_error(self) -> None:
        line = json.dumps(_run_error("The model 'x' is not available."))
        assert kc.parse_event(line) == ("error", "The model 'x' is not available.")

    def test_errors_explained(self) -> None:
        assert "kiro-cli login" in kc.explain_error("", "You are not logged in")
        assert "not available" in kc.explain_error("", "model 'x' is not available")

    def test_parse_models(self) -> None:
        out = json.dumps({
            "models": [{"model_id": "auto"}, {"model_id": "claude-haiku-4.5"}],
            "default_model": "auto",
        })
        assert kc.parse_models(out) == ["kiro-cli/auto", "kiro-cli/claude-haiku-4.5"]
        assert kc.parse_models("not json") == []


class TestGenerate:
    def test_success_sends_system_prompt_on_stdin(self) -> None:
        fake = subprocess.CompletedProcess([], 0, stdout=REPLY, stderr="")
        with patch("subprocess.run", return_value=fake) as run:
            res = _engine().generate(HOLA, model="kiro-cli/auto")
        assert res["content"] == "Hola, señor."
        assert run.call_args.kwargs["cwd"].endswith("ws")
        assert "Eres JARVIS." in run.call_args.kwargs["input"]

    def test_error_raises(self) -> None:
        fake = subprocess.CompletedProcess(
            [], 1, stdout=_events(_run_error("model 'x' is not available")), stderr=""
        )
        with patch("subprocess.run", return_value=fake):
            with pytest.raises(RuntimeError, match="not available"):
                _engine().generate(HOLA, model="kiro-cli/x")


class _FakeStream:
    def __init__(self, data: bytes):
        self._lines = data.splitlines(keepends=True)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    async def read(self) -> bytes:
        return b""


class _FakeStdin:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeProc:
    def __init__(self, stdout: str, rc: int = 0):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout.encode())
        self.stderr = _FakeStream(b"")
        self.returncode = None
        self._rc = rc

    async def wait(self) -> int:
        self.returncode = self._rc
        return self._rc

    def kill(self) -> None:
        self.returncode = -9


def _collect(model: str = "kiro-cli/auto") -> list[str]:
    async def run():
        return [d async for d in _engine().stream(HOLA, model=model)]

    return asyncio.run(run())


class TestStream:
    def test_stream_yields_chunks_once(self) -> None:
        async def fake_exec(*args, **kwargs):
            return _FakeProc(REPLY)

        with patch("asyncio.create_subprocess_exec", fake_exec):
            assert "".join(_collect()) == "Hola, señor."

    def test_stream_falls_back_to_final_text(self) -> None:
        async def fake_exec(*args, **kwargs):
            return _FakeProc(_events(_finished("Solo final.")))

        with patch("asyncio.create_subprocess_exec", fake_exec):
            assert _collect() == ["Solo final."]

    def test_stream_error(self) -> None:
        async def fake_exec(*args, **kwargs):
            return _FakeProc(_events(_run_error("Too many requests")), rc=1)

        with patch("asyncio.create_subprocess_exec", fake_exec):
            with pytest.raises(RuntimeError, match="usage limit"):
                _collect()


class TestModels:
    def test_list_models_from_cli(self) -> None:
        out = json.dumps({"models": [{"model_id": "auto"}, {"model_id": "glm-5"}]})
        fake = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with patch("subprocess.run", return_value=fake):
            assert _engine().list_models() == ["kiro-cli/auto", "kiro-cli/glm-5"]

    def test_list_models_falls_back_to_static(self) -> None:
        with patch("subprocess.run", side_effect=OSError("boom")):
            assert _engine().list_models() == kc.KIRO_MODELS

    def test_not_routed_to_cloud(self) -> None:
        assert not is_cloud_model("kiro-cli/claude-sonnet-4.6")
        assert not is_cloud_model("kiro-cli/auto")


GEN_FAILED = "Internal error (code -32603): Kiro failed to generate a response"


class TestGenerationFailureFallback:
    def test_chain(self) -> None:
        assert kc._fallback_model("kiro-cli/claude-opus-5.5", GEN_FAILED) == (
            "kiro-cli/claude-opus-4.8"
        )
        assert kc._fallback_model("kiro-cli/claude-opus-4.8", GEN_FAILED) == (
            "kiro-cli/auto"
        )
        assert kc._fallback_model("kiro-cli/auto", GEN_FAILED) is None
        assert kc._fallback_model("kiro-cli/claude-opus-5.5", "quota") is None

    def test_generate_retries_on_strong_fallback(self) -> None:
        failed = subprocess.CompletedProcess(
            [], 1, stdout=_events(_run_error(GEN_FAILED)), stderr=""
        )
        ok = subprocess.CompletedProcess([], 0, stdout=REPLY, stderr="")
        eng = _engine()
        with patch("subprocess.run", side_effect=[failed, ok]) as run:
            res = eng.generate(HOLA, model="kiro-cli/claude-opus-5.5")
        assert res["content"] == "Hola, señor."
        assert res["model"] == "kiro-cli/claude-opus-4.8"
        agents = [c.args[0][c.args[0].index("--agent") + 1] for c in run.call_args_list]
        assert agents == ["jarvis-claude-opus-5-5", "jarvis-claude-opus-4-8"]

    def test_stream_retries_when_nothing_was_streamed(self) -> None:
        procs = iter([
            _FakeProc(_events(_run_error(GEN_FAILED)), rc=1),
            _FakeProc(REPLY),
        ])

        async def fake_exec(*args, **kwargs):
            return next(procs)

        with patch("asyncio.create_subprocess_exec", fake_exec):
            assert "".join(_collect("kiro-cli/claude-opus-5")) == "Hola, señor."

    def test_failing_model_is_skipped_afterwards(self) -> None:
        failed = subprocess.CompletedProcess(
            [], 1, stdout=_events(_run_error(GEN_FAILED)), stderr=""
        )
        ok = subprocess.CompletedProcess([], 0, stdout=REPLY, stderr="")
        eng = _engine()
        with patch("subprocess.run", side_effect=[failed, ok, ok]) as run:
            eng.generate(HOLA, model="kiro-cli/claude-opus-5.5")
            res = eng.generate(HOLA, model="kiro-cli/claude-opus-5.5")
        assert run.call_count == 3  # one failing call, then straight to 4.8
        assert res["model"] == "kiro-cli/claude-opus-4.8"
