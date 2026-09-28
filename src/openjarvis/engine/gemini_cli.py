"""Gemini CLI engine — shells out to the Google ``gemini`` command-line tool.

Runs ``gemini -p … -o stream-json`` as an inference backend, the same way
``claude_cli`` uses the Claude Code subscription: by default the CLI's own
login (``gemini`` → /auth) is used.  Set ``OPENJARVIS_GEMINI_AUTH=api_key``
to force the Gemini API key from ``~/.openjarvis/cloud-keys.env`` instead.

Model ids exposed by this engine are ``gemini-cli/<model>`` where ``<model>``
is passed to ``gemini -m``; ``gemini-cli/default`` lets the CLI pick.

The CLI runs in a private empty workspace: from the system temp dir it
tried to index every sandboxed app folder there and spammed EPERM warnings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, Dict, Iterable, List

from openjarvis.core.registry import EngineRegistry
from openjarvis.core.types import Message, Role
from openjarvis.engine._base import InferenceEngine, estimate_prompt_tokens

logger = logging.getLogger(__name__)

MODEL_PREFIX = "gemini-cli/"
# Text models verified against the Gemini API (2026-09-28).  Pro models need
# a paid plan; the rest also work on the free API tier.
GEMINI_CLI_MODELS = [
    "gemini-cli/default",
    "gemini-cli/gemini-3.8-flash",
    "gemini-cli/gemini-3.7-flash",
    "gemini-cli/gemini-3.6-flash",
    "gemini-cli/gemini-3.5-flash",
    "gemini-cli/gemini-3.5-flash-lite",
    "gemini-cli/gemini-3-flash-preview",
    "gemini-cli/gemini-3.1-pro-preview",
    "gemini-cli/gemini-2.5-flash",
]

_FALLBACK_BIN_DIRS = (
    "~/.local/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)

_HOME = Path.home() / ".openjarvis" / "gemini-cli"
# stderr lines that are never the actual error.
_NOISE = re.compile(
    r"^(\s+at |\[WARN\]|\[STARTUP\]|Skill conflict|Server '.*' supports|"
    r"Loaded cached credentials|YOLO mode|Attempt \d+ failed)"
)


def _resolve_gemini_bin() -> str | None:
    override = os.environ.get("OPENJARVIS_GEMINI_BIN")
    if override:
        return override if os.path.isfile(override) else None
    found = shutil.which("gemini")
    if found:
        return found
    for d in _FALLBACK_BIN_DIRS:
        cand = Path(d).expanduser() / "gemini"
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _alias_for(model: str) -> str:
    """Map ``gemini-cli/gemini-3.7-flash`` → ``gemini-3.7-flash`` ("" = CLI default)."""
    alias = model[len(MODEL_PREFIX) :] if model.startswith(MODEL_PREFIX) else ""
    return "" if alias in ("", "default") else alias


# Tried in order when the requested model is overloaded or out of quota.
_FALLBACKS = ("gemini-2.5-flash", "gemini-3.6-flash")


def _auth_mode() -> str:
    """``subscription`` (the CLI's own Google login) or ``api_key``."""
    mode = os.environ.get("OPENJARVIS_GEMINI_AUTH", "").strip().lower()
    if not mode:
        try:
            from openjarvis.core.config import load_config

            mode = str(getattr(load_config().engine, "gemini_cli_auth", "") or "")
        except Exception:  # noqa: BLE001
            mode = ""
    return "api_key" if mode.replace("-", "_") == "api_key" else "subscription"


def _api_key_home() -> Path:
    """Private ``GEMINI_CLI_HOME`` for API-key mode.

    Keeps the user's ``~/.gemini`` login untouched and skips their MCP
    servers and skills, and caps retries — the CLI otherwise backs off for
    ~4 minutes on a 503 before giving up.
    """
    home = _HOME / "home"
    settings = {
        "security": {"auth": {"selectedType": "gemini-api-key"}},
        "general": {"maxAttempts": 2},
    }
    for folder in (home, home / ".gemini"):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "settings.json").write_text(json.dumps(settings))
    return home


def is_retryable(text: str) -> bool:
    """Overload / quota / unavailable-model errors that another model may avoid."""
    return bool(
        re.search(
            r"RESOURCE_EXHAUSTED|exhausted your|quota|\b503\b|UNAVAILABLE|"
            r"high demand|\b404\b|NOT_FOUND|no longer available|timed out",
            text,
            re.I,
        )
    )


def _api_key() -> str:
    try:
        from openjarvis.server.cloud_router import _load_keys

        keys = _load_keys()
    except Exception:  # noqa: BLE001
        keys = dict(os.environ)
    return keys.get("GEMINI_API_KEY") or keys.get("GOOGLE_API_KEY") or ""


def explain_error(stderr: str, result_error: str = "") -> str:
    """Turn the CLI's noisy stderr into one actionable line."""
    text = f"{result_error}\n{stderr}"
    if "UNSUPPORTED_CLIENT" in text or "IneligibleTierError" in text:
        return (
            "the Google account logged into Gemini CLI has no supported plan "
            "(free Code Assist for individuals was discontinued). Log in with an "
            "account that has Google AI Pro/Ultra (`gemini` → /auth), or set "
            "gemini_cli_auth = \"api_key\" under [engine] in config.toml."
        )
    if "not running in a trusted directory" in text:
        return "the workspace is not trusted (set GEMINI_CLI_TRUST_WORKSPACE=true)."
    if re.search(r"RESOURCE_EXHAUSTED|exhausted your|quota", text, re.I):
        return (
            "quota exhausted for this model — try a Flash model or wait for the reset."
        )
    if re.search(r"\b503\b|UNAVAILABLE|high demand", text):
        return "the model is overloaded right now (503) — try another model."
    if re.search(r"\b404\b|NOT_FOUND|no longer available", text):
        return "this model is not available to your account — pick another one."
    lines = [ln for ln in stderr.splitlines() if ln.strip() and not _NOISE.match(ln)]
    return result_error or (lines[0][:300] if lines else "unknown error")


def parse_stream_line(raw: str) -> tuple[str, str]:
    """``(kind, payload)`` for one ``stream-json`` line.

    kind is ``"text"`` (assistant delta), ``"error"`` (result error) or ``""``.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    etype = data.get("type")
    if etype == "message" and data.get("role") == "assistant":
        content = data.get("content")
        if isinstance(content, list):
            content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        return "text", content or ""
    if etype == "result" and data.get("status") == "error":
        err = data.get("error") or {}
        return "error", err.get("message", "") if isinstance(err, dict) else str(err)
    if etype == "error":
        return "error", str(data.get("message") or data.get("error") or "")
    return "", ""


@EngineRegistry.register("gemini_cli")
class GeminiCLIEngine(InferenceEngine):
    """Inference via the local Gemini CLI (``gemini -p``)."""

    engine_id = "gemini_cli"
    is_cloud = False  # routed locally by the server, not via cloud_router

    def __init__(self, binary: str | None = None, timeout: float | None = None):
        self._bin = binary or _resolve_gemini_bin()
        self._timeout = timeout or float(
            os.environ.get("OPENJARVIS_GEMINI_CLI_TIMEOUT", "60")
        )
        self._cwd = str(_HOME / "workspace")

    @staticmethod
    def _split_messages(messages: Sequence[Message]) -> tuple[str, str]:
        """Return ``(system_prompt, prompt)`` for a ``gemini -p`` call."""
        system_parts: list[str] = []
        convo: list[Message] = []
        for m in messages:
            if m.role == Role.SYSTEM:
                if m.content:
                    system_parts.append(m.content)
            else:
                convo.append(m)
        system = "\n\n".join(system_parts)

        if len(convo) == 1 and convo[0].role == Role.USER:
            user_content = convo[0].content or ""
            if system:
                return system, f"{system}\n\n{user_content}"
            return "", user_content

        labels = {
            Role.USER: "User",
            Role.ASSISTANT: "Assistant",
            Role.TOOL: "Tool result",
        }
        lines = []
        if system:
            lines.append(f"Instructions: {system}\n")
        lines.append("Conversation so far:")
        for m in convo:
            label = labels.get(m.role, m.role.value)
            if m.role == Role.TOOL and m.name:
                label = f"Tool result ({m.name})"
            lines.append(f"\n{label}: {m.content or ''}")
        lines.append(
            "\nContinue the conversation: reply as the Assistant to the last "
            "User message. Output only the reply."
        )
        return system, "\n".join(lines)

    def _build_cmd(self, model: str, prompt: str) -> list[str]:
        if not self._bin:
            raise RuntimeError(
                "Gemini CLI not found. Install it (npm i -g @google/gemini-cli) "
                "or set OPENJARVIS_GEMINI_BIN."
            )
        cmd = [self._bin, "-p", prompt, "-o", "stream-json", "--skip-trust"]
        alias = _alias_for(model)
        if alias:
            cmd += ["-m", alias]
        return cmd

    def _child_env(self) -> Dict[str, str]:
        Path(self._cwd).mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        if _auth_mode() == "api_key":
            key = _api_key()
            if key:
                env["GEMINI_API_KEY"] = key
                env["GEMINI_CLI_HOME"] = str(_api_key_home())
        return env

    @staticmethod
    def _attempts(model: str) -> List[str]:
        """``model`` first, then Flash fallbacks for overload / quota errors."""
        chain = [model]
        for alias in _FALLBACKS:
            fb = MODEL_PREFIX + alias
            if fb not in chain:
                chain.append(fb)
        return chain

    def _run_once(self, model: str, prompt: str) -> tuple[str, str, str, int]:
        """``(text, result_error, stderr, returncode)`` for one CLI call."""
        try:
            proc = subprocess.run(
                self._build_cmd(model, prompt),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=self._cwd,
                env=self._child_env(),
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return "", f"timed out after {self._timeout:.0f}s", "", 1
        except OSError as exc:
            raise RuntimeError(f"Failed to execute Gemini CLI: {exc}") from exc
        text, error = self._collect(proc.stdout.splitlines())
        return text, error, proc.stderr, proc.returncode

    @staticmethod
    def _collect(lines: Iterable[str]) -> tuple[str, str]:
        text, error = [], ""
        for raw in lines:
            kind, payload = parse_stream_line(raw.strip())
            if kind == "text":
                text.append(payload)
            elif kind == "error":
                error = payload
        return "".join(text).strip(), error

    def health(self) -> bool:
        return bool(self._bin)

    def generate(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        _, prompt = self._split_messages(messages)
        failure = ""
        for attempt in self._attempts(model):
            text, error, stderr, code = self._run_once(attempt, prompt)
            if not error and (code == 0 or text):
                break
            failure = explain_error(stderr, error)
            if not is_retryable(f"{error}\n{stderr}"):
                raise RuntimeError(f"Gemini CLI failed: {failure}")
            logger.warning("Gemini CLI %s failed (%s); trying next", attempt, failure)
        else:
            raise RuntimeError(f"Gemini CLI failed: {failure}")

        prompt_tokens = estimate_prompt_tokens(messages)
        completion_tokens = max(1, len(text) // 4)
        return {
            "content": text,
            "role": "assistant",
            "model": model,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "finish_reason": "stop",
        }

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        _, prompt = self._split_messages(messages)
        failure = ""
        for attempt in self._attempts(model):
            produced = False
            error, stderr, code = "", "", 0
            async for kind, payload in self._stream_once(attempt, prompt):
                if kind == "text":
                    produced = True
                    yield payload
                elif kind == "error":
                    error = payload
                elif kind == "exit":
                    stderr, code = payload
            if not error and (code == 0 or produced):
                return
            failure = explain_error(stderr, error)
            # Once text reached the user we cannot restart on another model.
            if produced or not is_retryable(f"{error}\n{stderr}"):
                raise RuntimeError(f"Gemini CLI failed: {failure}")
            logger.warning("Gemini CLI %s failed (%s); trying next", attempt, failure)
        raise RuntimeError(f"Gemini CLI failed: {failure}")

    async def _stream_once(self, model: str, prompt: str) -> AsyncIterator[tuple]:
        """Yield ``("text", …)`` / ``("error", …)``, then ``("exit", (stderr, rc))``."""
        proc = await asyncio.create_subprocess_exec(
            *self._build_cmd(model, prompt),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._child_env(),
        )
        assert proc.stdout is not None and proc.stderr is not None
        stderr_task = asyncio.create_task(proc.stderr.read())
        try:
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), self._timeout)
                if not line:
                    break
                kind, payload = parse_stream_line(line.decode(errors="replace").strip())
                if kind == "text" and payload:
                    yield "text", payload
                elif kind == "error":
                    yield "error", payload
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            # Silent for too long — usually the CLI backing off on a 503.
            yield "error", f"timed out after {self._timeout:.0f}s"
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        stderr = (await stderr_task).decode(errors="replace")
        yield "exit", (stderr, proc.returncode or 0)

    def list_models(self) -> List[str]:
        return list(GEMINI_CLI_MODELS) if self._bin else []

    def model_info(self, model: str) -> Dict[str, Any]:
        return {
            "id": model,
            "name": f"Gemini CLI ({_alias_for(model) or 'default'})",
            "provider": "gemini_cli",
            "context_length": 1_000_000,
        }
