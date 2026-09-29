"""Per-turn tool selection for text-protocol agents.

Full tool descriptions are the largest part of the ReAct system prompt and
most turns (small talk, follow-ups) need none of them.  :func:`select_tools`
keeps full descriptions only for tools the recent conversation mentions; the
others are rendered as one-line signatures so they stay callable.

Tools without a trigger entry (MCP tools, skills, anything new) are always
described in full, so adding a tool never silently hides it.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

from openjarvis.tools._stubs import BaseTool, build_tool_descriptions

_SESSIONS = (
    r"sesi[oó]n|sesiones|session|claude code|codex|gemini|antigravity"
    r"|c[oó]digo|proyecto|repo|programa|coding|terminal|tmux"
)

_TOOL_TRIGGERS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in {
        "digest_collect": (
            r"digest|briefing|resumen|qu[eé] (?:hay|tengo)|agenda|hoy|today"
            r"|correo|email|inbox|bandeja|calendario|reuni[oó]n|evento"
        ),
        "knowledge_search": (
            r"nota|notes?|correo|e-?mail|mail|mensaje|calendario|reuni[oó]n"
            r"|evento|contacto|escrib[ií]|guard[eé]|busca|encuentra|recuerd"
            r"|search|find|what did i"
            r"|hablamos|hablaste|charlamos|conversamos|dijimos|dije|platicamos"
            r"|conversaci[oó]n|chat|talked"
        ),
        "open_app": r"abr[eai]|abrir|open|lanza|inicia|app|aplicaci[oó]n",
        "open_url": (
            r"https?://|www\.|\.com\b|\.co\b|p[aá]gina|sitio|web|url|link"
            r"|enlace|abr[eai]|open"
        ),
        "spotify": (
            r"spotify|m[uú]sica|canci[oó]n|\bpon(?:me|er)?\b|reproduc|play"
            r"|pausa|paus[ae]|siguiente|anterior|playlist|lista de|[aá]lbum"
            r"|artista|volumen|music|song|skip"
        ),
        "start_coding_session": _SESSIONS,
        "coding_sessions": _SESSIONS,
        "send_to_session": _SESSIONS + r"|env[ií]a|manda|dile|escr[ií]bele",
        "apple_notes": r"nota|notes?|apunt|anota|apple",
        "user_profile_manage": (
            r"perfil|profile|sobre m[ií]|about me|mi trabajo|mis datos"
            r"|actualiza|update|recuerd|anota|apunta|guarda|olvid"
            r"|modific|cambi|corrig|nodo|grafo|graph|remember|forget"
        ),
        "memory_manage": (
            r"recuerd|memoriz|olvid|remember|forget|memoria|memory"
            r"|nodo|grafo|graph|anota|guarda"
        ),
        "show_knowledge_graph": r"grafo|graph|obsidian|conocimiento|knowledge",
        "model_switch": (
            r"modelo|model|claude|gemini|antigravity|cuota|quota|l[ií]mite"
            r"|proveedor|provider"
        ),
        "get_weather": (
            r"clima|el tiempo|temperatura|llov|lluvia|pron[oó]stico|weather"
            r"|forecast|fr[ií]o|calor|paraguas"
        ),
    }.items()
}


def select_tools(
    tools: List[BaseTool], texts: Iterable[str]
) -> Tuple[List[BaseTool], List[BaseTool]]:
    """Split *tools* into ``(full, brief)`` based on *texts*."""
    haystack = "\n".join(t for t in texts if t)
    full: list[BaseTool] = []
    brief: list[BaseTool] = []
    for tool in tools:
        pattern = _TOOL_TRIGGERS.get(tool.spec.name)
        if pattern is None or pattern.search(haystack):
            full.append(tool)
        else:
            brief.append(tool)
    return full, brief


def _signature(tool: BaseTool) -> str:
    s = tool.spec
    props = s.parameters.get("properties", {})
    required = set(s.parameters.get("required", []))
    params = ", ".join(f"{p}{'*' if p in required else ''}" for p in props)
    summary = re.split(r"(?<=[.!?])\s", s.description.strip(), maxsplit=1)[0]
    return f"- {s.name}({params}): {summary}"


def build_selected_tool_descriptions(
    tools: List[BaseTool], texts: Iterable[str]
) -> str:
    """Tool-description block with full entries only for relevant tools."""
    full, brief = select_tools(tools, texts)
    if not brief:
        return build_tool_descriptions(full)
    parts = [build_tool_descriptions(full)] if full else []
    parts.append(
        "### Other tools\n"
        "Signatures only (* = required). Call one with its JSON arguments "
        "if the request needs it.\n" + "\n".join(_signature(t) for t in brief)
    )
    return "\n\n".join(parts)


__all__ = ["build_selected_tool_descriptions", "select_tools"]
