"""Switch JARVIS between its model providers by hand.

"Usa Gemini" parks the Claude subscription so turns start on the fallback
chain (Antigravity, then the Gemini API); "vuelve a Claude" lifts that (and
any quota cooldown on Claude).  "¿Qué modelo estás usando?" reports which
provider answers now and which are parked.  State lives in
:mod:`openjarvis.engine.quota`, the same place automatic quota cooldowns go.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, List

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.engine import quota
from openjarvis.tools._stubs import BaseTool, ToolSpec

TOOL = "model_switch"
CLAUDE = "claude-cli/"

PROVIDER_NAMES = {
    "claude-cli": "Claude",
    "antigravity": "Antigravity (Gemini)",
    "gemini-cli": "Gemini API",
    "kiro-cli": "Kiro",
}


def _chain() -> List[str]:
    """Models in the order a chat turn tries them."""
    try:
        from openjarvis.core.config import load_config

        intel = load_config().intelligence
    except Exception:  # noqa: BLE001
        return []
    primary = intel.fast_model or intel.default_model
    backups = list(intel.fallback_models or []) or (
        [intel.fallback_model] if intel.fallback_model else []
    )
    return [m for m in [primary, *backups] if m]


def _name(model_or_provider: str) -> str:
    provider = quota.provider_of(model_or_provider)
    return PROVIDER_NAMES.get(provider, provider)


def _until_text(until: float) -> str:
    if math.isinf(until):
        return "hasta que diga 'vuelve a Claude'"
    when = datetime.fromtimestamp(until)
    fmt = "%H:%M" if when.date() == datetime.now().date() else "%d/%m %H:%M"
    return f"hasta las {when.strftime(fmt)}"


def status() -> str:
    """One Spanish paragraph: who answers now and who is parked."""
    chain = _chain()
    active = next((m for m in chain if quota.is_available(m)), "")
    parts = [
        f"Ahora respondo con {_name(active)} ({active})."
        if active
        else "Todos los proveedores están en pausa."
    ]
    for provider, until in sorted(quota.parked().items()):
        parts.append(f"{_name(provider)} en pausa {_until_text(until)}.")
    return " ".join(parts)


@ToolRegistry.register(TOOL)
class ModelSwitchTool(BaseTool):
    """Park or restore the Claude subscription, or report the active model."""

    tool_id = TOOL

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=TOOL,
            description=(
                "Switch which model provider JARVIS uses. action='use_gemini' "
                "stops using Claude (turns go to Antigravity/Gemini) until "
                "action='use_claude'; action='status' says which model answers "
                "now and which providers are paused (out of quota or by choice)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["use_gemini", "use_claude", "status"],
                    },
                },
                "required": ["action"],
            },
        )

    def execute(self, **params: Any) -> ToolResult:
        action = str(params.get("action") or "status")
        if action == "use_gemini":
            quota.park(CLAUDE)
        elif action == "use_claude":
            quota.clear(CLAUDE)
        elif action != "status":
            return ToolResult(
                tool_name=TOOL, content=f"Unknown action {action!r}.", success=False
            )
        return ToolResult(
            tool_name=TOOL,
            content=status(),
            success=True,
            metadata={"action": action},
        )


__all__ = ["ModelSwitchTool", "status"]
