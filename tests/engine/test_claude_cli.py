"""Tests for the Claude Code CLI engine backend."""

from __future__ import annotations

import asyncio
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from openjarvis.core.types import Message, Role
from openjarvis.engine.claude_cli import CLAUDE_CLI_MODELS, ClaudeCLIEngine
from openjarvis.server.cloud_router import is_cloud_model


def _engine() -> ClaudeCLIEngine:
    return ClaudeCLIEngine(binary="/fake/claude", timeout=5)


class TestPromptBuilding:
    def test_single_user_message_passthrough(self) -> None:
        system, prompt = ClaudeCLIEngine._split_messages(
            [
                Message(role=Role.SYSTEM, content="Eres Jarvis."),
                Message(role=Role.USER, content="Hola"),
            ]
        )
        assert system == "Eres Jarvis."
        assert prompt == "Hola"

    def test_multi_turn_transcript(self) -> None:
        _, prompt = ClaudeCLIEngine._split_messages(
            [
                Message(role=Role.USER, content="Hi"),
                Message(role=Role.ASSISTANT, content="Hello!"),
                Message(role=Role.USER, content="How are you?"),
            ]
        )
        assert "User: Hi" in prompt
        assert "Assistant: Hello!" in prompt
        assert prompt.index("Hello!") < prompt.index("How are you?")

    def test_cmd_flags(self) -> None:
        cmd = _engine()._build_cmd("claude-cli/opus", "sys", stream=True)
        assert cmd[:2] == ["/fake/claude", "-p"]
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert cmd[cmd.index("--tools") + 1] == ""
        assert cmd[cmd.index("--model") + 1] == "opus"
        assert cmd[cmd.index("--system-prompt") + 1] == "sys"
        assert "--include-partial-messages" in cmd

    def test_default_alias_omits_model_flag(self) -> None:
        cmd = _engine()._build_cmd("claude-cli/default", "", stream=False)
        assert "--model" not in cmd
        assert "--system-prompt" not in cmd


class TestEnv:
    def test_strips_api_key_and_session_markers(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        monkeypatch.delenv("OPENJARVIS_CLAUDE_CLI_USE_API_KEY", raising=False)
        env = ClaudeCLIEngine._child_env()
        assert "ANTHROPIC_API_KEY" not in env
        assert "CLAUDECODE" not in env
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"

    def test_api_key_opt_in(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        monkeypatch.setenv("OPENJARVIS_CLAUDE_CLI_USE_API_KEY", "1")
        assert ClaudeCLIEngine._child_env()["ANTHROPIC_API_KEY"] == "sk-x"


class TestGenerate:
    def test_usage_separates_prompt_cache_reads(self) -> None:
        out = json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": "ok",
                "usage": {
                    "input_tokens": 50,
                    "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 300,
                    "output_tokens": 4,
                },
            }
        )
        fake = subprocess.CompletedProcess([], 0, stdout=out + "\n", stderr="")
        with patch("subprocess.run", return_value=fake):
            res = _engine().generate(
                [Message(role=Role.USER, content="Hola")], model="claude-cli/haiku"
            )
        assert res["usage"]["prompt_tokens"] == 2350
        assert res["usage"]["prompt_tokens_evaluated"] == 350
        assert res["usage"]["cache_read_tokens"] == 2000

    def test_generate_parses_json_result(self) -> None:
        out = json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": "Hola, señor.",
                "usage": {"input_tokens": 10, "output_tokens": 4},
            }
        )
        fake = subprocess.CompletedProcess([], 0, stdout=out + "\n", stderr="")
        with patch("subprocess.run", return_value=fake) as run:
            res = _engine().generate(
                [Message(role=Role.USER, content="Hola")], model="claude-cli/sonnet"
            )
        assert res["content"] == "Hola, señor."
        assert res["usage"]["prompt_tokens"] == 10
        assert res["usage"]["completion_tokens"] == 4
        assert run.call_args.kwargs["input"] == "Hola"

    def test_generate_raises_on_error_result(self) -> None:
        out = json.dumps({"type": "result", "is_error": True, "result": "boom"})
        fake = subprocess.CompletedProcess([], 1, stdout=out, stderr="")
        with patch("subprocess.run", return_value=fake):
            with pytest.raises(RuntimeError, match="boom"):
                _engine().generate(
                    [Message(role=Role.USER, content="x")], model="claude-cli/sonnet"
                )


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class TestStream:
    def test_stream_yields_text_deltas(self) -> None:
        events = [
            {"type": "system", "subtype": "init"},
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "hmm"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hola"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": ", señor."},
                },
            },
            {"type": "result", "is_error": False, "result": "Hola, señor."},
        ]
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.drain = MagicMock(return_value=asyncio.sleep(0))
        proc.stdout = _FakeStdout([(json.dumps(e) + "\n").encode() for e in events])
        proc.returncode = 0

        async def fake_exec(*args, **kwargs):  # noqa: ANN002, ANN003
            return proc

        async def collect() -> list[str]:
            with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
                return [
                    t
                    async for t in _engine().stream(
                        [Message(role=Role.USER, content="Hola")],
                        model="claude-cli/sonnet",
                    )
                ]

        assert asyncio.run(collect()) == ["Hola", ", señor."]


class TestDiscovery:
    def test_models_and_health(self) -> None:
        with patch("os.access", return_value=True):
            eng = _engine()
            assert eng.health()
        assert eng.list_models() == CLAUDE_CLI_MODELS

    def test_no_binary_unhealthy(self) -> None:
        with patch(
            "openjarvis.engine.claude_cli._resolve_claude_bin", return_value=None
        ):
            eng = ClaudeCLIEngine()
        assert not eng.health()
        assert eng.list_models() == []

    def test_not_routed_to_cloud(self) -> None:
        for m in CLAUDE_CLI_MODELS:
            assert not is_cloud_model(m)
        assert is_cloud_model("claude-sonnet-4-6")


def test_safeguard_fallback_model():
    from openjarvis.engine.claude_cli import _safeguard_fallback_model

    refusal = "API Error: Opus 5.5's safeguards flagged this message"
    assert _safeguard_fallback_model("claude-cli/opus", refusal) == "claude-cli/sonnet"
    assert _safeguard_fallback_model("claude-cli/sonnet", refusal) is None
    assert _safeguard_fallback_model("claude-cli/opus", "rate limited") is None


def test_fast_tier_runs_without_extended_thinking(monkeypatch):
    from openjarvis.engine.claude_cli import ClaudeCLIEngine

    monkeypatch.delenv("MAX_THINKING_TOKENS", raising=False)
    assert ClaudeCLIEngine._child_env("claude-cli/haiku")["MAX_THINKING_TOKENS"] == "0"
    assert "MAX_THINKING_TOKENS" not in ClaudeCLIEngine._child_env("claude-cli/opus")
    assert "MAX_THINKING_TOKENS" not in ClaudeCLIEngine._child_env()
    monkeypatch.setenv("MAX_THINKING_TOKENS", "2048")
    assert ClaudeCLIEngine._child_env("claude-cli/haiku")["MAX_THINKING_TOKENS"] == "2048"
