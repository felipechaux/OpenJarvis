"""Antigravity CLI engine — shells out to Google's ``agy`` command-line agent.

Google's supported terminal agent for personal accounts since Gemini CLI's
free Code Assist tier was closed.  Like ``claude_cli`` it runs on the user's
own subscription: ``agy`` reuses the Antigravity login cached in the macOS
Keychain (sign in once by running ``agy`` interactively).

Model ids exposed by this engine are ``antigravity/<slug>`` where ``<slug>``
is one of ``agy models`` (e.g. ``gemini-3.1-pro-high``); ``antigravity/default``
lets the CLI pick.

``agy`` is a full agent with its own tools and no switch to turn them off,
so it runs in a private empty workspace with ``--sandbox``; in print mode
shell commands are denied unless pre-approved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, Dict, List

from openjarvis.core.registry import EngineRegistry
from openjarvis.core.types import Message
from openjarvis.engine._base import InferenceEngine, estimate_prompt_tokens
from openjarvis.engine.gemini_cli import GeminiCLIEngine

logger = logging.getLogger(__name__)

MODEL_PREFIX = "antigravity/"
# ``agy models`` on 2026-09-28; refreshed from the CLI at runtime.
ANTIGRAVITY_MODELS = [
    "antigravity/default",
    "antigravity/gemini-3.8-flash-medium",
    "antigravity/gemini-3.8-flash-high",
    "antigravity/gemini-3.7-flash-medium",
    "antigravity/gemini-3.6-flash-medium",
    "antigravity/gemini-3.1-pro-high",
    "antigravity/gemini-3.1-pro-low",
    "antigravity/claude-sonnet-4-6",
    "antigravity/claude-opus-4-6-thinking",
    "antigravity/gpt-oss-120b-medium",
]

_FALLBACK_BIN_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
_WORKSPACE = Path.home() / ".openjarvis" / "antigravity-cli" / "workspace"
_MODELS_TTL_S = 3600.0
_models_cache: tuple[float, List[str]] = (0.0, [])


def _resolve_agy_bin() -> str | None:
    override = os.environ.get("OPENJARVIS_AGY_BIN")
    if override:
        return override if os.path.isfile(override) else None
    found = shutil.which("agy")
    if found:
        return found
    for d in _FALLBACK_BIN_DIRS:
        cand = Path(d).expanduser() / "agy"
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _slug_for(model: str) -> str:
    """``antigravity/gemini-3.1-pro-high`` → ``gemini-3.1-pro-high`` ("" = default)."""
    slug = model[len(MODEL_PREFIX) :] if model.startswith(MODEL_PREFIX) else ""
    return "" if slug in ("", "default") else slug


def parse_models(output: str) -> List[str]:
    """Model ids from ``agy models`` output (``<slug>\\t<display name>`` lines)."""
    models = []
    for line in output.splitlines():
        slug = line.split("\t", 1)[0].strip()
        if re.fullmatch(r"[a-z0-9][a-z0-9.\-]+", slug) and "\t" in line:
            models.append(MODEL_PREFIX + slug)
    return models


def parse_event(raw: str) -> tuple[str, str]:
    """``(kind, payload)`` for one ``stream-json`` line.

    kind is ``"text"`` (agent response delta), ``"error"`` (failed result)
    or ``""`` for everything else (init, tool steps, success result).
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    event = data.get("event")
    if event == "step_update":
        step = data.get("step_update") or {}
        if step.get("step_type") == "agent_response" and step.get("text_delta"):
            return "text", step["text_delta"]
    elif event == "result":
        result = data.get("result") or {}
        if result.get("status") not in (None, "SUCCESS"):
            return "error", str(
                result.get("error") or f"run ended with status {result.get('status')}"
            )
    return "", ""


def explain_error(stderr: str, result_error: str = "") -> str:
    text = f"{result_error}\n{stderr}"
    if re.search(r"authentication required|not (signed|logged) in|login", text, re.I):
        return "not signed in — run `agy` once in a terminal to log in."
    if re.search(r"quota|RESOURCE_EXHAUSTED|rate.?limit", text, re.I):
        return "quota reached for this model on your plan — pick another model."
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    return result_error or (lines[-1][:300] if lines else "unknown error")


@EngineRegistry.register("antigravity_cli")
class AntigravityCLIEngine(InferenceEngine):
    """Inference via the local Antigravity CLI (``agy -p``)."""

    engine_id = "antigravity_cli"
    is_cloud = False  # routed locally by the server, not via cloud_router

    def __init__(self, binary: str | None = None, timeout: float | None = None):
        self._bin = binary or _resolve_agy_bin()
        self._timeout = timeout or float(
            os.environ.get("OPENJARVIS_AGY_TIMEOUT", "180")
        )
        self._cwd = str(_WORKSPACE)

    _split_messages = staticmethod(GeminiCLIEngine._split_messages)

    def _build_cmd(self, model: str, prompt: str) -> list[str]:
        if not self._bin:
            raise RuntimeError(
                "Antigravity CLI not found. Install it "
                "(curl -fsSL https://antigravity.google/cli/install.sh | bash) "
                "or set OPENJARVIS_AGY_BIN."
            )
        cmd = [
            self._bin,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--disable-slash-commands",
            "--sandbox",
            "--print-timeout",
            f"{int(self._timeout)}s",
        ]
        slug = _slug_for(model)
        if slug:
            cmd += ["--model", slug]
        return cmd

    def _prepare(self) -> None:
        Path(self._cwd).mkdir(parents=True, exist_ok=True)

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
        self._prepare()
        try:
            proc = subprocess.run(
                self._build_cmd(model, prompt),
                capture_output=True,
                text=True,
                timeout=self._timeout + 15,
                cwd=self._cwd,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Antigravity CLI timed out after {self._timeout:.0f}s"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"Failed to execute Antigravity CLI: {exc}") from exc

        text, error = [], ""
        for line in proc.stdout.splitlines():
            kind, payload = parse_event(line.strip())
            if kind == "text":
                text.append(payload)
            elif kind == "error":
                error = payload
        content = "".join(text).strip()
        if error or (proc.returncode != 0 and not content):
            raise RuntimeError(
                f"Antigravity CLI failed: {explain_error(proc.stderr, error)}"
            )

        prompt_tokens = estimate_prompt_tokens(messages)
        completion_tokens = max(1, len(content) // 4)
        return {
            "content": content,
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
        self._prepare()
        proc = await asyncio.create_subprocess_exec(
            *self._build_cmd(model, prompt),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
        )
        assert proc.stdout is not None and proc.stderr is not None
        stderr_task = asyncio.create_task(proc.stderr.read())
        produced, error = False, ""
        try:
            while True:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), self._timeout + 15
                )
                if not line:
                    break
                kind, payload = parse_event(line.decode(errors="replace").strip())
                if kind == "text":
                    produced = True
                    yield payload
                elif kind == "error":
                    error = payload
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            error = f"timed out after {self._timeout:.0f}s"
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        stderr = (await stderr_task).decode(errors="replace")
        if error or (proc.returncode not in (0, None) and not produced):
            raise RuntimeError(
                f"Antigravity CLI failed: {explain_error(stderr, error)}"
            )

    def list_models(self) -> List[str]:
        global _models_cache
        if not self._bin:
            return []
        fetched_at, cached = _models_cache
        if cached and time.time() - fetched_at < _MODELS_TTL_S:
            return list(cached)
        models: List[str] = []
        try:
            out = subprocess.run(
                [self._bin, "models"],
                capture_output=True,
                text=True,
                timeout=20,
                stdin=subprocess.DEVNULL,
            ).stdout
            models = parse_models(out)
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("agy models failed: %s", exc)
        if not models:
            return list(ANTIGRAVITY_MODELS)
        models = [f"{MODEL_PREFIX}default", *models]
        _models_cache = (time.time(), models)
        return list(models)

    def model_info(self, model: str) -> Dict[str, Any]:
        return {
            "id": model,
            "name": f"Antigravity ({_slug_for(model) or 'default'})",
            "provider": "antigravity_cli",
            "context_length": 1_000_000,
        }


__all__ = ["ANTIGRAVITY_MODELS", "AntigravityCLIEngine", "MODEL_PREFIX"]
