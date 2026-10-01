"""Kiro CLI engine — shells out to the ``kiro-cli`` command-line agent.

Like ``claude_cli`` it runs on the user's own subscription: ``kiro-cli``
reuses its cached login (sign in once with ``kiro-cli login``).

Model ids exposed by this engine are ``kiro-cli/<model_id>`` where
``<model_id>`` is one of ``kiro-cli chat --list-models`` (e.g.
``claude-sonnet-4.6``); ``kiro-cli/auto`` lets Kiro pick.

``kiro-cli chat --model`` is ignored by the default (v2) agent engine, so
each model gets a small agent profile (``.kiro/agents/jarvis-<model>.json``)
in a private workspace: the profile sets the model and turns every tool off.
Kiro does not apply the profile's ``prompt`` as a system prompt, so the
system prompt travels inside the user turn, as with ``gemini_cli``.

Environment overrides:

* ``OPENJARVIS_KIRO_BIN`` — path to the ``kiro-cli`` binary.
* ``OPENJARVIS_KIRO_TIMEOUT`` — per-call timeout in seconds (180).
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

MODEL_PREFIX = "kiro-cli/"
# ``kiro-cli chat --list-models`` on 2026-09-30; refreshed from the CLI at runtime.
KIRO_MODELS = [
    "kiro-cli/auto",
    "kiro-cli/claude-opus-5.5",
    "kiro-cli/claude-opus-5",
    "kiro-cli/claude-sonnet-5",
    "kiro-cli/claude-sonnet-4.6",
    "kiro-cli/claude-haiku-4.5",
    "kiro-cli/gpt-5.6-terra",
    "kiro-cli/gpt-5.6-luna",
]

_FALLBACK_BIN_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
_WORKSPACE = Path.home() / ".openjarvis" / "kiro-cli" / "workspace"
_MODELS_TTL_S = 3600.0
_models_cache: tuple[float, List[str]] = (0.0, [])


def _resolve_kiro_bin() -> str | None:
    override = os.environ.get("OPENJARVIS_KIRO_BIN")
    if override:
        return override if os.path.isfile(override) else None
    found = shutil.which("kiro-cli")
    if found:
        return found
    for d in _FALLBACK_BIN_DIRS:
        cand = Path(d).expanduser() / "kiro-cli"
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _model_id_for(model: str) -> str:
    """``kiro-cli/claude-sonnet-4.6`` → ``claude-sonnet-4.6`` ("" = auto)."""
    mid = model[len(MODEL_PREFIX) :] if model.startswith(MODEL_PREFIX) else ""
    return "" if mid in ("", "auto", "default") else mid


def agent_name_for(model: str) -> str:
    """Name of the agent profile that pins *model*."""
    mid = _model_id_for(model) or "auto"
    return "jarvis-" + re.sub(r"[^a-z0-9]+", "-", mid.lower()).strip("-")


def parse_models(output: str) -> List[str]:
    """Model ids from ``kiro-cli chat --list-models -f json``."""
    try:
        data = json.loads(output)
    except ValueError:
        return []
    models = []
    for entry in (data or {}).get("models") or []:
        mid = entry.get("model_id") if isinstance(entry, dict) else None
        if mid:
            models.append(MODEL_PREFIX + mid)
    return models


def parse_event(raw: str) -> tuple[str, str]:
    """``(kind, payload)`` for one ``stream-json`` line.

    kind is ``"text"`` (reply delta), ``"final"`` (full reply of a finished
    run), ``"error"`` (failed run) or ``""`` for everything else.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    etype = data.get("type")
    body = data.get("data") or {}
    if etype == "sessionUpdate":
        update = body.get("update") or {}
        content = update.get("content") or {}
        if (
            update.get("sessionUpdate") == "agent_message_chunk"
            and content.get("type") == "text"
            and content.get("text")
        ):
            return "text", content["text"]
    elif etype == "runError":
        return "error", str(body.get("message") or "run failed")
    elif etype == "runFinished":
        if body.get("status") not in (None, "success"):
            return "error", f"run ended with status {body.get('status')}"
        return "final", body.get("finalText") or ""
    return "", ""


def explain_error(stderr: str, run_error: str = "") -> str:
    text = f"{run_error}\n{stderr}"
    if re.search(
        r"not (signed|logged) in|log ?in|unauthori[sz]ed|expired token", text, re.I
    ):
        return "not signed in — run `kiro-cli login` once in a terminal."
    if re.search(r"is not available", text, re.I):
        return "that model is not available on your Kiro plan — pick another model."
    if re.search(r"quota|limit|throttl|too many requests", text, re.I):
        return "Kiro usage limit reached — pick another model or provider."
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    return run_error[:300] or (lines[-1][:300] if lines else "unknown error")


@EngineRegistry.register("kiro_cli")
class KiroCLIEngine(InferenceEngine):
    """Inference via the local Kiro CLI (``kiro-cli chat --no-interactive``)."""

    engine_id = "kiro_cli"
    is_cloud = False  # routed locally by the server, not via cloud_router

    def __init__(self, binary: str | None = None, timeout: float | None = None):
        self._bin = binary or _resolve_kiro_bin()
        self._timeout = timeout or float(
            os.environ.get("OPENJARVIS_KIRO_TIMEOUT", "180")
        )
        self._cwd = str(_WORKSPACE)

    _split_messages = staticmethod(GeminiCLIEngine._split_messages)

    def _prepare(self, model: str) -> str:
        """Write the agent profile for *model*; return its name."""
        name = agent_name_for(model)
        agents = Path(self._cwd) / ".kiro" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        profile: Dict[str, Any] = {
            "name": name,
            "description": "OpenJarvis inference profile (no tools).",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {},
            "includeMcpJson": False,
            "resources": [],
        }
        mid = _model_id_for(model)
        if mid:
            profile["model"] = mid
        path = agents / f"{name}.json"
        text = json.dumps(profile, indent=2)
        if not path.is_file() or path.read_text() != text:
            path.write_text(text)
        return name

    def _build_cmd(self, agent: str) -> list[str]:
        if not self._bin:
            raise RuntimeError(
                "Kiro CLI not found. Install it (https://kiro.dev/cli) "
                "or set OPENJARVIS_KIRO_BIN."
            )
        return [
            self._bin,
            "chat",
            "--output-format",
            "stream-json",
            "--no-interactive",
            "--agent",
            agent,
            "--trust-tools=",
            "--wrap",
            "never",
        ]

    def health(self) -> bool:
        return bool(self._bin) and os.access(self._bin, os.X_OK)

    def generate(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        # temperature / max_tokens / tools are not supported by ``kiro-cli``.
        _, prompt = self._split_messages(messages)
        agent = self._prepare(model)
        try:
            proc = subprocess.run(
                self._build_cmd(agent),
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=self._cwd,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Kiro CLI timed out after {self._timeout:.0f}s"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"Failed to execute Kiro CLI: {exc}") from exc

        chunks, final, error = [], "", ""
        for line in proc.stdout.splitlines():
            kind, payload = parse_event(line.strip())
            if kind == "text":
                chunks.append(payload)
            elif kind == "final":
                final = payload
            elif kind == "error":
                error = payload
        content = (final or "".join(chunks)).strip()
        if error or (proc.returncode != 0 and not content):
            raise RuntimeError(f"Kiro CLI failed: {explain_error(proc.stderr, error)}")

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
        agent = self._prepare(model)
        proc = await asyncio.create_subprocess_exec(
            *self._build_cmd(agent),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            limit=16 * 1024 * 1024,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None and proc.stderr is not None
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        stderr_task = asyncio.create_task(proc.stderr.read())
        produced, error = False, ""
        try:
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), self._timeout)
                if not line:
                    break
                kind, payload = parse_event(line.decode(errors="replace").strip())
                if kind == "text":
                    produced = True
                    yield payload
                elif kind == "final" and not produced and payload:
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
            raise RuntimeError(f"Kiro CLI failed: {explain_error(stderr, error)}")

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
                [self._bin, "chat", "--list-models", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=20,
                stdin=subprocess.DEVNULL,
            ).stdout
            models = parse_models(out)
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("kiro-cli --list-models failed: %s", exc)
        if not models:
            return list(KIRO_MODELS)
        _models_cache = (time.time(), models)
        return list(models)

    def model_info(self, model: str) -> Dict[str, Any]:
        return {
            "id": model,
            "name": f"Kiro ({_model_id_for(model) or 'auto'})",
            "provider": "kiro_cli",
            "context_length": 200_000,
        }


__all__ = ["KIRO_MODELS", "KiroCLIEngine", "MODEL_PREFIX"]
