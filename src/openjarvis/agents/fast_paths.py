"""Deterministic fast paths: common commands handled without any LLM call.

"Pausa la música", "abre Xcode" or "¿qué clima hace?" need one tool call and
a one-line confirmation.  Through the ReAct loop that costs two full model
calls; here it costs none.  Patterns are anchored to the whole utterance so
anything more elaborate ("abre Xcode y crea un proyecto") still goes to the
model, and every reply builder may return ``None`` to hand the turn back to
the model (e.g. when the tool failed and needs explaining).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from openjarvis.core.types import ToolResult


@dataclass(frozen=True)
class FastPath:
    tool: str
    arguments: Dict[str, Any]
    reply: Callable[[ToolResult], Optional[str]]


_FILLER_RE = re.compile(
    r"^(?:(?:oye|hey|ok|okay)\s*,?\s*)?(?:jarvis\s*,?\s*)?"
    r"|(?:\s*,?\s*(?:por favor|porfa|please))?\s*[.!?¡¿]*\s*$",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    text = text.strip().lstrip("¿¡").strip()
    text = _FILLER_RE.sub("", text).strip()
    text = re.sub(r"^(?:por favor\s*,?\s*)", "", text, flags=re.IGNORECASE)
    return text.strip(" ,")


# ── Spotify ────────────────────────────────────────────────────────────────

_MUSIC = r"(?:la\s+)?(?:m[uú]sica|canci[oó]n|spotify|reproducci[oó]n)"

_SPOTIFY_CONTROLS = [
    ("pause", re.compile(
        rf"^(?:pausa|pausar|pon(?:le)?\s+pausa|det[eé]n|para)(?:\s+{_MUSIC})?$",
        re.IGNORECASE)),
    ("next", re.compile(
        rf"^(?:siguiente(?:\s+canci[oó]n)?|(?:pasa|salta|cambia)(?:\s+{_MUSIC})?"
        r"|next|skip)$",
        re.IGNORECASE)),
    ("previous", re.compile(
        r"^(?:(?:la\s+)?canci[oó]n\s+anterior|anterior|vuelve\s+a\s+la\s+anterior"
        r"|pon\s+la\s+anterior)$",
        re.IGNORECASE)),
    ("play", re.compile(
        rf"^(?:reanuda|contin[uú]a|sigue|play|dale\s+play|reproduce|quita\s+la\s+pausa)"
        rf"(?:\s+{_MUSIC})?$",
        re.IGNORECASE)),
]

_SPOTIFY_QUERY = [
    re.compile(
        r"^(?:pon(?:me)?|reproduce|toca)\s+(?:algo\s+de\s+|m[uú]sica\s+de\s+"
        r"|canciones\s+de\s+|la\s+canci[oó]n\s+|el\s+[aá]lbum\s+|la\s+playlist\s+)"
        r"(?P<q>.+?)(?:\s+en\s+spotify)?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:pon(?:me)?|reproduce|toca)\s+(?P<q>.+?)\s+en\s+spotify$",
        re.IGNORECASE,
    ),
]

_SPOTIFY_OK = {
    "pause": "Música en pausa, señor.",
    "next": "Siguiente canción, señor.",
    "previous": "De vuelta a la canción anterior, señor.",
    "play": "Reanudando la música, señor.",
}


def _spotify_reply(action: str, query: str) -> Callable[[ToolResult], Optional[str]]:
    def reply(result: ToolResult) -> Optional[str]:
        meta = result.metadata or {}
        code = meta.get("result")
        browser = meta.get("browser", "el navegador")
        if code == "ok":
            if action == "play" and query:
                return f"Reproduciendo {query} en Spotify, señor."
            return _SPOTIFY_OK[action]
        if code == "already":
            state = "sonando" if action == "play" else "en pausa"
            return f"La música ya estaba {state}, señor."
        if code == "login":
            return (
                f"Spotify en {browser} tiene la sesión cerrada, señor; "
                "necesita iniciar sesión en open.spotify.com."
            )
        if code == "waiting":
            return "Spotify no terminó de cargar a tiempo, señor. ¿Lo intento de nuevo?"
        if code == "not_started":
            return (
                "Le di play, señor, pero el navegador parece bloquear la "
                "reproducción automática; basta con pulsar play una vez."
            )
        return None  # unexpected failure: let the model explain it

    return reply


def _match_spotify(text: str) -> Optional[FastPath]:
    for action, pattern in _SPOTIFY_CONTROLS:
        if pattern.match(text):
            return FastPath("spotify", {"action": action}, _spotify_reply(action, ""))
    for pattern in _SPOTIFY_QUERY:
        m = pattern.match(text)
        if m:
            query = m.group("q").strip(" \"'")
            if query:
                return FastPath(
                    "spotify",
                    {"action": "play", "query": query},
                    _spotify_reply("play", query),
                )
    return None


# ── open_app ───────────────────────────────────────────────────────────────

_OPEN_APP_RE = re.compile(
    r"^(?:abre|abrir|[aá]breme|lanza|inicia|open)\s+"
    r"(?:la\s+(?:app|aplicaci[oó]n)\s+(?:de\s+)?|el\s+programa\s+)?"
    r"(?P<app>[\w][\w\s\-]{0,40}?)$",
    re.IGNORECASE,
)
# Things that look like "abre X" but are not apps.
_NOT_AN_APP_RE = re.compile(
    r"\b(?:sesi[oó]n|proyecto|nota|notas|p[aá]gina|web|sitio|link|enlace|url"
    r"|correo|carpeta|archivo|documento|pesta[ñn]a|una|un|con|en|y)\b|\.",
    re.IGNORECASE,
)
_OPENED_RE = re.compile(r"^Opened (?P<name>.+?)\.$")


def _open_app_reply(result: ToolResult) -> Optional[str]:
    if not result.success:
        return None
    m = _OPENED_RE.match(result.content or "")
    name = m.group("name") if m else "La aplicación"
    return f"{name} abierto, señor."


def _match_open_app(text: str) -> Optional[FastPath]:
    m = _OPEN_APP_RE.match(text)
    if not m:
        return None
    app = m.group("app").strip()
    if not app or len(app.split()) > 3 or _NOT_AN_APP_RE.search(app):
        return None
    return FastPath("open_app", {"app": app}, _open_app_reply)


# ── show_knowledge_graph ───────────────────────────────────────────────────

# "abre Obsidian", "muéstrame mi grafo (de conocimiento)", "abre el grafo".
# Obsidian is matched here, before open_app: its only vault is this graph.
_KNOWLEDGE_GRAPH_RE = re.compile(
    r"^(?:(?:abre|abrir|[aá]breme|lanza|open)\s+obsidian"
    r"|(?:mu[eé]strame|mu[eé]stranos|ens[eé][ñn]ame|abre|abrir|[aá]breme|ver|show)"
    r"\s+(?:mi|el|nuestro|my|the)\s+(?:grafo|graph)"
    r"(?:\s+(?:de\s+conocimiento|knowledge))?(?:\s+en\s+obsidian)?)$",
    re.IGNORECASE,
)


def _knowledge_graph_reply(result: ToolResult) -> Optional[str]:
    if not result.success:
        return None
    nodes = (result.metadata or {}).get("nodes")
    what = f" con {nodes} nodos" if nodes else ""
    return f"Su grafo de conocimiento está abierto en Obsidian{what}, señor."


def _match_knowledge_graph(text: str) -> Optional[FastPath]:
    if not _KNOWLEDGE_GRAPH_RE.match(text):
        return None
    return FastPath("show_knowledge_graph", {}, _knowledge_graph_reply)


# ── get_weather ────────────────────────────────────────────────────────────

_WEATHER_RE = re.compile(
    r"^(?:(?:qu[eé]|c[oó]mo)\s+)?(?:(?:est[aá]|hace|tal)\s+)?(?:el\s+)?"
    r"(?:clima|tiempo)(?:\s+hace)?(?:\s+(?:hoy|ahora))?"
    r"(?:\s+(?:en|de)\s+(?P<loc>[\w\s\-]{2,40}?))?(?:\s+(?:hoy|ahora))?$",
    re.IGNORECASE,
)

# wttr.in condition → Spanish.  Unknown conditions fall back to the model so
# the reply never mixes languages.
_CONDITIONS = {
    "sunny": "soleado",
    "clear": "despejado",
    "partly cloudy": "parcialmente nublado",
    "cloudy": "nublado",
    "overcast": "cubierto",
    "mist": "con neblina",
    "fog": "con niebla",
    "light rain": "con lluvia ligera",
    "light drizzle": "con llovizna",
    "patchy rain possible": "con posibles lluvias aisladas",
    "patchy rain nearby": "con lluvias aisladas cerca",
    "light rain shower": "con chubascos ligeros",
    "moderate rain": "con lluvia moderada",
    "heavy rain": "con lluvia fuerte",
    "rain": "lluvioso",
    "thundery outbreaks possible": "con posibles tormentas",
    "thunderstorm": "con tormenta",
}
_WEATHER_OUT_RE = re.compile(
    r"^(?P<loc>[^:]+):\s*(?P<cond>[A-Za-z ,]+?)\s*(?P<temp>[+-]?\d+)°C"
)


def _weather_reply(result: ToolResult) -> Optional[str]:
    if not result.success:
        return None
    m = _WEATHER_OUT_RE.match(result.content or "")
    if not m:
        return None
    cond = _CONDITIONS.get(m.group("cond").strip().lower())
    if cond is None:
        return None
    temp = int(m.group("temp"))
    return f"En {m.group('loc').strip()} está {cond}, a {temp} °C, señor."


def _match_weather(text: str) -> Optional[FastPath]:
    m = _WEATHER_RE.match(text)
    if not m:
        return None
    loc = (m.group("loc") or "").strip() or "Bogotá"
    return FastPath(
        "get_weather", {"location": loc, "units": "metric"}, _weather_reply
    )


# ── model_switch ───────────────────────────────────────────────────────────

_USE_GEMINI_RE = re.compile(
    r"^(?:usa|use|c[aá]mbia(?:te)?\s+a|p[aá]sate\s+a|switch\s+to)\s+"
    r"(?:el\s+modelo\s+(?:de\s+)?)?(?:gemini|antigravity)$",
    re.IGNORECASE,
)
_USE_CLAUDE_RE = re.compile(
    r"^(?:vuelve\s+a|regresa\s+a|usa|use|c[aá]mbia(?:te)?\s+a|p[aá]sate\s+a"
    r"|switch\s+(?:back\s+)?to)\s+(?:el\s+modelo\s+(?:de\s+)?)?claude$",
    re.IGNORECASE,
)
_MODEL_STATUS_RE = re.compile(
    r"^(?:qu[eé]|cu[aá]l)\s+modelo\s+(?:est[aá]s\s+usando|usas|tienes|es)"
    r"(?:\s+ahora)?$|^(?:which|what)\s+model\s+are\s+you\s+using(?:\s+now)?$",
    re.IGNORECASE,
)


def _model_switch_reply(result: ToolResult) -> Optional[str]:
    if not result.success:
        return None
    action = (result.metadata or {}).get("action")
    lead = {
        "use_gemini": "Entendido, señor: dejo Claude en pausa. ",
        "use_claude": "De vuelta con Claude, señor. ",
    }.get(action, "")
    return lead + (result.content or "")


def _match_model_switch(text: str) -> Optional[FastPath]:
    for action, pattern in (
        ("use_gemini", _USE_GEMINI_RE),
        ("use_claude", _USE_CLAUDE_RE),
        ("status", _MODEL_STATUS_RE),
    ):
        if pattern.match(text):
            return FastPath("model_switch", {"action": action}, _model_switch_reply)
    return None


# ── look_at_screen ─────────────────────────────────────────────────────────

_SCREEN = r"(?:mi\s+|la\s+)?(?:pantalla|screen)"
_LOOK_AT_SCREEN_RE = re.compile(
    rf"^(?:qu[eé]\s+ves(?:\s+en\s+{_SCREEN})?"
    rf"|mira\s+{_SCREEN}|qu[eé]\s+hay\s+en\s+{_SCREEN}"
    rf"|describe\s+{_SCREEN}|qu[eé]\s+estoy\s+viendo"
    rf"|what\s+do\s+you\s+see(?:\s+on\s+my\s+screen)?|look\s+at\s+my\s+screen)$",
    re.IGNORECASE,
)


def _screen_reply(result: ToolResult) -> Optional[str]:
    return (result.content or None) if result.success else None


def _match_look_at_screen(text: str) -> Optional[FastPath]:
    if _LOOK_AT_SCREEN_RE.match(text):
        return FastPath("look_at_screen", {}, _screen_reply)
    return None


_MATCHERS = (
    _match_look_at_screen,
    _match_model_switch,
    _match_spotify,
    _match_weather,
    _match_knowledge_graph,
    _match_open_app,
)


# ── Daily briefing ─────────────────────────────────────────────────────────
# The desktop app's once-a-day briefing prompt starts with this marker.  Its
# data is always the same two tool calls, so they run before the model sees
# the prompt: the fast model tended to call only the weather and then invent
# a "reconnect Calendar/Gmail" excuse instead of calling digest_collect.

BRIEFING_MARKER = "JARVIS_WELCOME_TRIGGER"

_BRIEFING_CALLS: tuple[tuple[str, Dict[str, Any]], ...] = (
    ("get_weather", {"location": "Bogotá", "units": "metric"}),
    ("digest_collect", {"sources": ["gcalendar", "gmail"]}),
)


def briefing_prefetch(text: str, tool_names: set[str]) -> list[tuple[str, Dict[str, Any]]]:
    """Tool calls to run before the model for a briefing prompt, else ``[]``."""
    if not text.lstrip().startswith(BRIEFING_MARKER):
        return []
    return [(name, args) for name, args in _BRIEFING_CALLS if name in tool_names]


def match_fast_path(text: str, tool_names: set[str]) -> Optional[FastPath]:
    """The fast path for *text*, or ``None`` when the model should handle it."""
    normalized = _normalize(text)
    if not normalized or len(normalized) > 80 or "\n" in normalized:
        return None
    for matcher in _MATCHERS:
        path = matcher(normalized)
        if path is not None:
            return path if path.tool in tool_names else None
    return None


__all__ = ["BRIEFING_MARKER", "FastPath", "briefing_prefetch", "match_fast_path"]
