"""smart_light — switch, dim and colour Tuya WiFi bulbs over the LAN.

Commands go straight to the bulb with ``tinytuya``: no cloud round trip, and
it keeps working without internet.  The bulbs and their local keys live in
``~/.openjarvis/smart_lights.json`` (mode 600); a key is obtained once, through
a Smart Life QR login, and changes only if the bulb is paired again.

If a bulb stops answering, its broadcast is looked up in case DHCP moved it,
and the new address is written back.

Output is the bulb's name and state, so this tool is in
``session_guard.TRUSTED_OUTPUT_TOOLS``.  The local key is never returned.
"""

from __future__ import annotations

import colorsys
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List

from openjarvis.core.config import DEFAULT_CONFIG_DIR
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

TOOL = "smart_light"
LIGHTS_PATH: Path = DEFAULT_CONFIG_DIR / "smart_lights.json"
TIMEOUT_S = 5

# Data points of a Tuya "dj" (light) device with v2 functions.
DP_POWER = "20"
DP_MODE = "21"
DP_BRIGHT = "22"
DP_TEMP = "23"
DP_COLOUR = "24"
MODE_WHITE = "white"
MODE_COLOUR = "colour"
MIN_LEVEL = 10
MAX_LEVEL = 1000

# name -> (hue 0-360, saturation 0-1000)
COLOURS = {
    "rojo": (0, 1000), "roja": (0, 1000), "red": (0, 1000),
    "naranja": (30, 1000), "orange": (30, 1000),
    "amarillo": (55, 1000), "amarilla": (55, 1000), "yellow": (55, 1000),
    "verde": (120, 1000), "green": (120, 1000),
    "cian": (180, 1000), "turquesa": (175, 1000), "cyan": (180, 1000),
    "azul": (240, 1000), "blue": (240, 1000),
    "morado": (275, 1000), "morada": (275, 1000), "violeta": (275, 1000),
    "purple": (275, 1000),
    "fucsia": (310, 1000), "magenta": (300, 1000),
    "rosa": (330, 600), "rosado": (330, 600), "rosada": (330, 600),
    "pink": (330, 600),
}

# name -> colour temperature 0-1000; None keeps the current one.
WHITE_TONES = {
    "blanco": None, "blanca": None, "white": None,
    "calido": 0, "calida": 0, "warm": 0,
    "neutro": 500, "neutra": 500, "neutral": 500,
    "frio": 1000, "fria": 1000, "cold": 1000, "cool": 1000,
}

_HEX_RE = re.compile(r"^#?[0-9a-f]{6}$")


class LightError(RuntimeError):
    """The bulb could not be reached or refused the command."""


def _word(value: str) -> str:
    """Lowercase without accents, so 'Cálida' and 'calida' are the same."""
    decomposed = unicodedata.normalize("NFKD", value.strip().lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def load_lights() -> List[Dict[str, Any]]:
    data = json.loads(LIGHTS_PATH.read_text(encoding="utf-8"))
    return list(data["lights"])


def _save_lights(lights: List[Dict[str, Any]]) -> None:
    # mkstemp creates the file with mode 600; the key must never be readable
    # by anyone else, not even briefly.
    fd, tmp = tempfile.mkstemp(dir=LIGHTS_PATH.parent, prefix=".smart_lights")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"lights": lights}, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, LIGHTS_PATH)


def _device(light: Dict[str, Any]) -> Any:
    import tinytuya

    dev = tinytuya.Device(
        light["id"],
        light["ip"],
        light["local_key"],
        version=float(light["version"]),
    )
    dev.set_socketTimeout(TIMEOUT_S)
    dev.set_socketRetryLimit(1)
    return dev


def _rediscover(light: Dict[str, Any]) -> bool:
    """Look for the bulb's broadcast; True when it moved to a new address."""
    import tinytuya

    found = tinytuya.find_device(dev_id=light["id"])
    ip = (found or {}).get("ip")
    return bool(ip) and ip != light["ip"] and _store_ip(light, ip)


def _store_ip(light: Dict[str, Any], ip: str) -> bool:
    lights = load_lights()
    for entry in lights:
        if entry["id"] == light["id"]:
            entry["ip"] = ip
    _save_lights(lights)
    light["ip"] = ip
    return True


def _checked(response: Any) -> Any:
    if isinstance(response, dict) and "Error" in response:
        raise LightError(str(response["Error"]))
    return response


def _state(dev: Any) -> Dict[str, Any]:
    response = _checked(dev.status())
    dps = response.get("dps") if isinstance(response, dict) else None
    if not dps:
        raise LightError("empty status")
    return dps


def _send(dev: Any, values: Dict[str, Any]) -> None:
    _checked(dev.set_multiple_values(values))


def _level(dps: Dict[str, Any]) -> int:
    """Current brightness on the 10-1000 scale, whatever the mode."""
    if dps.get(DP_MODE) == MODE_COLOUR:
        return max(MIN_LEVEL, int(str(dps[DP_COLOUR])[8:12], 16))
    return int(dps.get(DP_BRIGHT, MAX_LEVEL))


def _colour_hex(hue: int, sat: int, level: int) -> str:
    return f"{hue:04x}{sat:04x}{level:04x}"


def _percent(value: str) -> int:
    text = value.strip().rstrip("%").strip()
    if not text.isdigit() or not 0 <= int(text) <= 100:
        raise ValueError(f"'{value}' is not a percentage from 0 to 100.")
    return int(text)


def _parse_colour(value: str) -> tuple[int, int]:
    word = _word(value)
    if word in COLOURS:
        return COLOURS[word]
    if _HEX_RE.match(word):
        raw = word.lstrip("#")
        r, g, b = (int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4))
        hue, sat, _ = colorsys.rgb_to_hsv(r, g, b)
        return round(hue * 360), round(sat * 1000)
    raise ValueError(
        f"Unknown colour '{value}'. Use a colour name or a #rrggbb value."
    )


def _set_white(dev: Any, tone: str) -> str:
    values: Dict[str, Any] = {DP_POWER: True, DP_MODE: MODE_WHITE}
    word = _word(tone)
    if word in WHITE_TONES:
        temperature = WHITE_TONES[word]
    elif word:
        temperature = _percent(word) * 10
    else:
        temperature = None
    if temperature is not None:
        values[DP_TEMP] = temperature
    _send(dev, values)
    return f"white{f' ({tone})' if tone else ''}"


def _set_colour(dev: Any, value: str) -> str:
    if _word(value) in WHITE_TONES:
        return _set_white(dev, value)
    hue, sat = _parse_colour(value)
    level = _level(_state(dev))
    _send(
        dev,
        {
            DP_POWER: True,
            DP_MODE: MODE_COLOUR,
            DP_COLOUR: _colour_hex(hue, sat, level),
        },
    )
    return f"colour {value}"


def _set_brightness(dev: Any, value: str) -> str:
    percent = _percent(value)
    if percent == 0:
        _send(dev, {DP_POWER: False})
        return "off"
    level = max(MIN_LEVEL, percent * 10)
    dps = _state(dev)
    if dps.get(DP_MODE) == MODE_COLOUR:
        hue = int(str(dps[DP_COLOUR])[:4], 16)
        sat = int(str(dps[DP_COLOUR])[4:8], 16)
        _send(dev, {DP_POWER: True, DP_COLOUR: _colour_hex(hue, sat, level)})
    else:
        _send(dev, {DP_POWER: True, DP_BRIGHT: level})
    return f"brightness {percent}%"


def _describe(dev: Any) -> str:
    dps = _state(dev)
    if not dps.get(DP_POWER):
        return "off"
    level = round(_level(dps) / 10)
    if dps.get(DP_MODE) == MODE_COLOUR:
        hue = int(str(dps[DP_COLOUR])[:4], 16)
        return f"on, colour (hue {hue}), brightness {level}%"
    temperature = round(int(dps.get(DP_TEMP, 0)) / 10)
    return (
        f"on, white, brightness {level}%, "
        f"colour temperature {temperature}% (0 warm, 100 cold)"
    )


def _on(dev: Any) -> str:
    _send(dev, {DP_POWER: True})
    return "on"


def _off(dev: Any) -> str:
    _send(dev, {DP_POWER: False})
    return "off"


def _operation(action: str, value: str) -> Callable[[Any], str]:
    operations: Dict[str, Callable[[Any], str]] = {
        "on": _on,
        "off": _off,
        "status": _describe,
        "brightness": lambda dev: _set_brightness(dev, value),
        "color": lambda dev: _set_colour(dev, value),
        "white": lambda dev: _set_white(dev, value),
    }
    if action not in operations:
        raise ValueError(f"Unknown action '{action}'.")
    if action in ("brightness", "color") and not value:
        raise ValueError(f"Action '{action}' needs a value.")
    return operations[action]


def _run(light: Dict[str, Any], operation: Callable[[Any], str]) -> str:
    try:
        return operation(_device(light))
    except (LightError, OSError):
        if not _rediscover(light):
            raise
    return operation(_device(light))


@ToolRegistry.register(TOOL)
class SmartLightTool(BaseTool):
    """Control the user's WiFi light bulbs on the local network."""

    tool_id = TOOL

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=TOOL,
            description=(
                "The user's smart WiFi light bulb: turn it 'on' or 'off', read "
                "its 'status', set 'brightness' (value 1-100), 'color' (value: "
                "a colour name in Spanish or English, or #rrggbb) or 'white' "
                "(value: calida, neutra, fria, or 0-100 from warm to cold)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "on", "off", "status", "brightness", "color", "white",
                        ],
                    },
                    "value": {
                        "type": "string",
                        "description": "Percentage, colour or white tone, "
                        "depending on the action.",
                    },
                },
                "required": ["action"],
            },
            # Long enough for a failed attempt plus a LAN rediscovery.
            timeout_seconds=45,
        )

    def execute(self, **params: Any) -> ToolResult:
        action = str(params.get("action") or "status")
        value = str(params.get("value") or "").strip()
        try:
            operation = _operation(action, value)
            lights = load_lights()
        except FileNotFoundError:
            return self._result(f"No lights configured in {LIGHTS_PATH}.", False)
        except (KeyError, ValueError) as exc:
            return self._result(str(exc), False)

        lines, ok = [], True
        for light in lights:
            try:
                lines.append(f"{light['name']}: {_run(light, operation)}")
            except ValueError as exc:
                lines.append(str(exc))
                ok = False
            except (LightError, OSError, ImportError) as exc:
                lines.append(
                    f"{light['name']} did not answer ({exc}). "
                    "It may be switched off at the wall."
                )
                ok = False
        return self._result("\n".join(lines), ok)

    @staticmethod
    def _result(content: str, ok: bool) -> ToolResult:
        return ToolResult(tool_name=TOOL, content=content, success=ok)


__all__ = ["LIGHTS_PATH", "LightError", "SmartLightTool", "load_lights"]
