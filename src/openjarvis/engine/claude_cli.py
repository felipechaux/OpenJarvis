"""Claude Code CLI engine — shells out to the ``claude`` command-line tool.

Runs ``claude -p`` as a plain LLM (no tools, no MCP servers, no settings
files, no session persistence) so inference uses the user's Claude Code
login (Pro/Max subscription) instead of an API key.

Model ids exposed by this engine are ``claude-cli/<alias>`` where
``<alias>`` is passed straight to ``claude --model`` (``sonnet``, ``opus``,
``haiku``).  The ``claude-cli/`` prefix is deliberately excluded from
cloud-provider routing in :mod:`openjarvis.server.cloud_router`.

Environment overrides:

* ``OPENJARVIS_CLAUDE_BIN`` — path to the ``claude`` binary.
* ``OPENJARVIS_CLAUDE_CLI_USE_API_KEY=1`` — keep ``ANTHROPIC_API_KEY`` in
  the child environment.  By default it is removed so the CLI authenticates
  with the subscription login rather than an (often unfunded) API key.
* ``OPENJARVIS_CLAUDE_CLI_TIMEOUT`` — per-call timeout in seconds (300).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.core.registry import EngineRegistry
from openjarvis.core.types import Message, Role
from openjarvis.engine._base import InferenceEngine, estimate_prompt_tokens

logger = logging.getLogger(__name__)

MODEL_PREFIX = "claude-cli/"
CLAUDE_CLI_MODELS = [
    "claude-cli/sonnet",
    "claude-cli/opus",
    "claude-cli/haiku",
]

# macOS .app bundles do not inherit the shell PATH, so probe common spots.
_FALLBACK_BIN_DIRS = (
    "~/.local/bin",
    "~/.claude/local",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)

# Session markers that would make a nested ``claude`` think it runs inside
# another Claude Code session (only present when the server itself was
# launched from Claude Code).  Auth vars like CLAUDE_CODE_OAUTH_TOKEN are kept.
_STRIP_ENV_VARS = frozenset({
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_EXECPATH",
})


def _resolve_claude_bin() -> str | None:
    override = os.environ.get("OPENJARVIS_CLAUDE_BIN")
    if override:
        return override if os.path.isfile(override) else None
    found = shutil.which("claude")
    if found:
        return found
    for d in _FALLBACK_BIN_DIRS:
        cand = Path(d).expanduser() / "claude"
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _alias_for(model: str) -> str:
    """Map ``claude-cli/sonnet`` → ``sonnet`` (empty → CLI default)."""
    alias = model[len(MODEL_PREFIX):] if model.startswith(MODEL_PREFIX) else ""
    return "" if alias in ("", "default") else alias


@EngineRegistry.register("claude_cli")
class ClaudeCLIEngine(InferenceEngine):
    """Inference via the local Claude Code CLI (``claude -p``)."""

    engine_id = "claude_cli"
    is_cloud = False  # routed locally by the server, not via cloud_router

    def __init__(self, binary: str | None = None, timeout: float | None = None):
        self._bin = binary or _resolve_claude_bin()
        self._timeout = timeout or float(
            os.environ.get("OPENJARVIS_CLAUDE_CLI_TIMEOUT", "300")
        )
        # Neutral cwd so no project CLAUDE.md / settings get picked up.
        self._cwd = tempfile.gettempdir()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _split_messages(messages: Sequence[Message]) -> tuple[str, str]:
        """Return ``(system_prompt, prompt)`` for a ``claude -p`` call."""
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
            return system, convo[0].content or ""

        labels = {
            Role.USER: "User",
            Role.ASSISTANT: "Assistant",
            Role.TOOL: "Tool result",
        }
        lines = ["Conversation so far:"]
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

    def _build_cmd(self, model: str, system: str, *, stream: bool) -> list[str]:
        if not self._bin:
            raise RuntimeError(
                "Claude Code CLI not found. Install it or set OPENJARVIS_CLAUDE_BIN."
            )
        cmd = [
            self._bin,
            "-p",
            "--output-format",
            "stream-json" if stream else "json",
            "--no-session-persistence",
            "--tools",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--setting-sources",
            "",
        ]
        if stream:
            cmd += ["--verbose", "--include-partial-messages"]
        if system:
            cmd += ["--system-prompt", system]
        alias = _alias_for(model)
        if alias:
            cmd += ["--model", alias]
        return cmd

    @staticmethod
    def _child_env() -> Dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV_VARS}
        if os.environ.get("OPENJARVIS_CLAUDE_CLI_USE_API_KEY") != "1":
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    @staticmethod
    def _usage_from(data: Dict[str, Any], messages: Sequence[Message], text: str):
        u = data.get("usage") or {}
        cache_read = int(u.get("cache_read_input_tokens") or 0)
        prompt = (
            int(u.get("input_tokens") or 0)
            + cache_read
            + int(u.get("cache_creation_input_tokens") or 0)
        ) or estimate_prompt_tokens(messages)
        completion = int(u.get("output_tokens") or 0) or max(1, len(text) // 4)
        return {
            "prompt_tokens": prompt,
            # Tokens actually processed: prompt-cache reads are excluded.
            "prompt_tokens_evaluated": max(prompt - cache_read, 0),
            "cache_read_tokens": cache_read,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

    # --------------------------------------------------------------- interface

    def generate(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        # temperature / max_tokens / tools are not supported by ``claude -p``.
        system, prompt = self._split_messages(messages)
        cmd = self._build_cmd(model, system, stream=False)
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=self._child_env(),
                cwd=self._cwd,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Claude CLI timed out after {self._timeout:.0f}s"
            ) from exc

        try:
            data = json.loads(proc.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Claude CLI failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()[:500]}"
            ) from exc

        text = data.get("result") or ""
        if data.get("is_error"):
            fallback = _safeguard_fallback_model(model, text)
            if fallback:
                logger.warning(
                    "%s refused by safeguards; retrying with %s", model, fallback
                )
                return self.generate(
                    messages, model=fallback, temperature=temperature,
                    max_tokens=max_tokens, **kwargs,
                )
            raise RuntimeError(f"Claude CLI error: {text or data.get('subtype')}")
        return {
            "content": text,
            "usage": self._usage_from(data, messages, text),
            "model": model,
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
        system, prompt = self._split_messages(messages)
        cmd = self._build_cmd(model, system, stream=True)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._child_env(),
            cwd=self._cwd,
            limit=16 * 1024 * 1024,
        )
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        emitted = False
        error: str | None = None
        try:
            while True:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=self._timeout
                )
                if not line:
                    break
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type")
                if etype == "stream_event":
                    inner = evt.get("event") or {}
                    delta = inner.get("delta") or {}
                    if (
                        inner.get("type") == "content_block_delta"
                        and delta.get("type") == "text_delta"
                        and delta.get("text")
                    ):
                        emitted = True
                        yield delta["text"]
                elif etype == "result":
                    if evt.get("is_error"):
                        error = evt.get("result") or evt.get("subtype") or "error"
                    elif not emitted and evt.get("result"):
                        # No partial deltas (older CLI) — emit the final text.
                        emitted = True
                        yield evt["result"]
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise RuntimeError(
                f"Claude CLI timed out after {self._timeout:.0f}s"
            ) from exc
        finally:
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()

        if error:
            fallback = None if emitted else _safeguard_fallback_model(model, error)
            if fallback:
                logger.warning(
                    "%s refused by safeguards; retrying with %s", model, fallback
                )
                async for chunk in self.stream(
                    messages, model=fallback, temperature=temperature,
                    max_tokens=max_tokens, **kwargs,
                ):
                    yield chunk
                return
            raise RuntimeError(f"Claude CLI error: {error}")
        if not emitted and proc.returncode:
            raw = await proc.stderr.read() if proc.stderr else b""
            stderr = raw.decode(errors="replace")
            raise RuntimeError(
                f"Claude CLI failed (exit {proc.returncode}): {stderr.strip()[:500]}"
            )

    def describe_image(self, image: Path, prompt: str, *, model: str) -> str:
        """Answer *prompt* about a JPEG/PNG, via ``--input-format stream-json``."""
        import base64

        media = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
        message = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media,
                            "data": base64.b64encode(image.read_bytes()).decode(),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            },
        }
        cmd = self._build_cmd(model, "", stream=True)
        at = cmd.index("stream-json") + 1
        cmd[at:at] = ["--input-format", "stream-json"]
        try:
            proc = subprocess.run(
                cmd,
                input=json.dumps(message) + "\n",
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=self._child_env(),
                cwd=self._cwd,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Claude CLI timed out after {self._timeout:.0f}s"
            ) from exc
        for line in reversed(proc.stdout.splitlines()):
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "result":
                text = evt.get("result") or ""
                if evt.get("is_error"):
                    raise RuntimeError(f"Claude CLI error: {text or evt.get('subtype')}")
                return text
        raise RuntimeError(
            f"Claude CLI failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:500]}"
        )

    def list_models(self) -> List[str]:
        return list(CLAUDE_CLI_MODELS) if self._bin else []

    def health(self) -> bool:
        return bool(self._bin) and os.access(self._bin, os.X_OK)



# Opus-tier safeguards occasionally refuse ordinary requests ("safeguards
# flagged this message").  Rather than failing the user's turn, retry once on
# Sonnet, which the CLI still serves for the same message.
_SAFEGUARD_MARKER = "safeguards flagged"
_SAFEGUARD_FALLBACK_MODEL = f"{MODEL_PREFIX}sonnet"


def _safeguard_fallback_model(model: str, error_text: str) -> Optional[str]:
    if _SAFEGUARD_MARKER not in (error_text or ""):
        return None
    if model == _SAFEGUARD_FALLBACK_MODEL:
        return None
    return _SAFEGUARD_FALLBACK_MODEL

__all__ = ["CLAUDE_CLI_MODELS", "ClaudeCLIEngine", "MODEL_PREFIX"]
