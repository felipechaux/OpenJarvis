"""Tests for the Gemini CLI engine backend."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from openjarvis.core.types import Message, Role
from openjarvis.engine import gemini_cli as gc
from openjarvis.engine.gemini_cli import GEMINI_CLI_MODELS, GeminiCLIEngine
from openjarvis.server.cloud_router import is_cloud_model

HOLA = [Message(role=Role.USER, content="Hola")]


def _engine() -> GeminiCLIEngine:
    return GeminiCLIEngine(binary="/fake/gemini", timeout=5)


def _stream_json(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _reply(text: str) -> str:
    return _stream_json(
        {"type": "init", "model": "x"},
        {"type": "message", "role": "user", "content": "Hola"},
        {"type": "message", "role": "assistant", "content": text, "delta": True},
        {"type": "result", "status": "success"},
    )


def _api_error(message: str) -> str:
    return _stream_json(
        {"type": "init", "model": "x"},
        {"type": "result", "status": "error", "error": {"message": message}},
    )


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gc, "_HOME", tmp_path / "gemini-cli")
    monkeypatch.delenv("OPENJARVIS_GEMINI_AUTH", raising=False)


class TestPromptBuilding:
    def test_single_user_message_passthrough(self) -> None:
        system, prompt = GeminiCLIEngine._split_messages(
            [
                Message(role=Role.SYSTEM, content="Eres Jarvis."),
                Message(role=Role.USER, content="Hola"),
            ]
        )
        assert system == "Eres Jarvis."
        assert "Hola" in prompt

    def test_multi_turn_transcript(self) -> None:
        _, prompt = GeminiCLIEngine._split_messages(
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
        cmd = _engine()._build_cmd("gemini-cli/gemini-3.7-flash", "hola")
        assert cmd[0] == "/fake/gemini"
        assert cmd[cmd.index("-p") + 1] == "hola"
        assert cmd[cmd.index("-m") + 1] == "gemini-3.7-flash"
        assert cmd[cmd.index("-o") + 1] == "stream-json"
        assert "--skip-trust" in cmd

    def test_default_model_omits_flag(self) -> None:
        assert "-m" not in _engine()._build_cmd("gemini-cli/default", "hola")


class TestStreamParsing:
    def test_only_assistant_text_is_emitted(self) -> None:
        kinds = [
            gc.parse_stream_line(line) for line in _reply("Hola, señor.").splitlines()
        ]
        assert [k for k in kinds if k[0]] == [("text", "Hola, señor.")]

    def test_result_error(self) -> None:
        line = _api_error("[API Error: quota]").splitlines()[-1]
        assert gc.parse_stream_line(line) == ("error", "[API Error: quota]")

    def test_garbage_is_ignored(self) -> None:
        assert gc.parse_stream_line("[WARN] Skipping unreadable directory") == ("", "")


class TestErrors:
    def test_unsupported_plan_is_explained(self) -> None:
        msg = gc.explain_error(
            "Error authenticating: IneligibleTierError: UNSUPPORTED_CLIENT"
        )
        assert "no supported plan" in msg

    def test_warnings_are_not_the_error(self) -> None:
        stderr = (
            "[WARN] Skipping unreadable directory: /private/var/x (EPERM)\n"
            "    at foo (bar.js:1)\n"
            "Real failure here\n"
        )
        assert gc.explain_error(stderr) == "Real failure here"

    def test_retryable(self) -> None:
        assert gc.is_retryable("503 high demand")
        assert gc.is_retryable("RESOURCE_EXHAUSTED")
        assert not gc.is_retryable("UNSUPPORTED_CLIENT")


class TestGenerate:
    def test_generate_success(self) -> None:
        fake = subprocess.CompletedProcess(
            [], 0, stdout=_reply("Hola, señor."), stderr=""
        )
        with patch("subprocess.run", return_value=fake) as run:
            res = _engine().generate(HOLA, model="gemini-cli/default")
        assert res["content"] == "Hola, señor."
        assert res["model"] == "gemini-cli/default"
        assert run.call_args.kwargs["env"]["GEMINI_CLI_TRUST_WORKSPACE"] == "true"
        # Never the system temp dir: gemini would index it and spam EPERM.
        assert run.call_args.kwargs["cwd"].endswith("workspace")

    def test_overload_falls_back_to_next_model(self) -> None:
        results = [
            subprocess.CompletedProcess(
                [], 1, stdout=_api_error("503 high demand"), stderr=""
            ),
            subprocess.CompletedProcess([], 0, stdout=_reply("ok"), stderr=""),
        ]
        with patch("subprocess.run", side_effect=results) as run:
            res = _engine().generate(HOLA, model="gemini-cli/gemini-3.7-flash")
        assert res["content"] == "ok"
        second = run.call_args_list[1].args[0]
        assert second[second.index("-m") + 1] == "gemini-2.5-flash"

    def test_auth_error_is_not_retried(self) -> None:
        fake = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="IneligibleTierError: UNSUPPORTED_CLIENT"
        )
        with patch("subprocess.run", return_value=fake) as run:
            with pytest.raises(RuntimeError, match="no supported plan"):
                _engine().generate(HOLA, model="gemini-cli/default")
        assert run.call_count == 1


class TestAuthModes:
    def test_subscription_uses_users_login(self) -> None:
        env = _engine()._child_env()
        assert "GEMINI_CLI_HOME" not in env

    def test_api_key_mode_uses_private_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENJARVIS_GEMINI_AUTH", "api_key")
        with patch.object(gc, "_api_key", return_value="k"):
            env = _engine()._child_env()
        home = Path(env["GEMINI_CLI_HOME"])
        assert env["GEMINI_API_KEY"] == "k"
        settings = json.loads((home / ".gemini" / "settings.json").read_text())
        assert settings["security"]["auth"]["selectedType"] == "gemini-api-key"
        assert settings["general"]["maxAttempts"] == 2


class TestStream:
    def test_stream_yields_text(self) -> None:
        async def fake_once(self, model, prompt):
            yield "text", "Hola"
            yield "text", " señor"
            yield "exit", ("", 0)

        async def collect():
            return [d async for d in _engine().stream(HOLA, model="gemini-cli/default")]

        with patch.object(GeminiCLIEngine, "_stream_once", fake_once):
            assert "".join(asyncio.run(collect())) == "Hola señor"

    def test_stream_falls_back_before_any_text(self) -> None:
        calls = []

        async def fake_once(self, model, prompt):
            calls.append(model)
            if len(calls) == 1:
                yield "error", "503 high demand"
                yield "exit", ("", 1)
            else:
                yield "text", "ok"
                yield "exit", ("", 0)

        async def collect():
            return [
                d
                async for d in _engine().stream(
                    HOLA, model="gemini-cli/gemini-3.7-flash"
                )
            ]

        with patch.object(GeminiCLIEngine, "_stream_once", fake_once):
            assert asyncio.run(collect()) == ["ok"]
        assert calls == ["gemini-cli/gemini-3.7-flash", "gemini-cli/gemini-2.5-flash"]


class TestModels:
    def test_listed_models_are_real_ids(self) -> None:
        # gemini-3-flash / gemini-3-pro never existed; 2.5-pro is retired.
        for bad in (
            "gemini-cli/gemini-3-flash",
            "gemini-cli/gemini-3-pro",
            "gemini-cli/gemini-2.5-pro",
        ):
            assert bad not in GEMINI_CLI_MODELS
        assert "gemini-cli/default" in GEMINI_CLI_MODELS


class TestRouting:
    def test_not_routed_to_cloud(self) -> None:
        assert not is_cloud_model("gemini-cli/gemini-3.7-flash")
        assert not is_cloud_model("gemini-cli/default")
