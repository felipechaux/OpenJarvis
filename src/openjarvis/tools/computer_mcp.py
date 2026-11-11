"""MCP server giving a delegated agent a screen, a mouse and a keyboard.

Started by ``computer_task`` as ``python -m openjarvis.tools.computer_mcp`` and
spoken to over stdio.  macOS only; it needs Screen Recording and Accessibility
for the app that launched it.

The agent sees whatever is on screen, and screen text can try to steer it, so
some limits live here rather than in its instructions: mouse and keyboard are
refused while the target is a browser, Mail, a terminal, a password manager or
System Settings.  Email and browsing have their own tools with their own
guards.  This narrows the risk without removing it: anything the agent can
reach in other apps it can still change.
"""

from __future__ import annotations

import base64
import ctypes
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from openjarvis.tools import displays

MAX_IMAGE_SIDE = 1568
_APP_SERVICES = (
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)

BLOCKED_BUNDLES = {
    "com.apple.mail",
    "com.apple.Safari",
    "com.google.Chrome",
    "company.thebrowser.Browser",
    "org.mozilla.firefox",
    "com.microsoft.edgemac",
    "com.brave.Browser",
    "com.apple.Terminal",
    "com.googlecode.iterm2",
    "dev.warp.Warp-Stable",
    "com.apple.keychainaccess",
    "com.apple.Passwords",
    "com.1password.1password",
    "com.apple.systempreferences",
}
BLOCKED_APP_NAMES = {
    "mail", "safari", "google chrome", "chrome", "arc", "firefox", "brave browser",
    "microsoft edge", "terminal", "iterm", "warp", "keychain access", "passwords",
    "1password", "system settings",
}
# Launchers and app switching reach blocked apps without clicking them.
BLOCKED_COMBOS = {"cmd+space", "ctrl+space", "cmd+tab", "cmd+shift+tab"}

KEY_CODES = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126,
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29, "o": 31,
    "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
}
MODIFIERS = {
    "cmd": 1 << 20, "command": 1 << 20, "shift": 1 << 17, "ctrl": 1 << 18,
    "control": 1 << 18, "alt": 1 << 19, "option": 1 << 19,
}


class ActionRefused(RuntimeError):
    """The action is off limits or macOS has not granted the permission."""


def _front_bundle() -> Optional[str]:
    from AppKit import NSWorkspace

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return str(app.bundleIdentifier()) if app else None


def _bundle_at(x: float, y: float) -> Optional[str]:
    """The app owning the topmost normal window under a point."""
    import Quartz
    from AppKit import NSRunningApplication

    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    for window in windows or []:
        if window.get("kCGWindowLayer") != 0:
            continue
        bounds = window.get("kCGWindowBounds") or {}
        left, top = bounds.get("X", 0), bounds.get("Y", 0)
        if left <= x <= left + bounds.get("Width", 0) and (
            top <= y <= top + bounds.get("Height", 0)
        ):
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(
                window["kCGWindowOwnerPID"]
            )
            return str(app.bundleIdentifier()) if app else None
    return None


def _require_input_permission() -> None:
    if not ctypes.CDLL(_APP_SERVICES).AXIsProcessTrusted():
        raise ActionRefused(
            "macOS has not given OpenJarvis the Accessibility permission "
            "(System Settings > Privacy & Security > Accessibility)."
        )


def _check_target(bundle: Optional[str]) -> None:
    if bundle in BLOCKED_BUNDLES:
        raise ActionRefused(
            f"Refused: {bundle} is off limits for computer control. "
            "Tell the user this step needs another tool or their own hands."
        )


def _layout() -> displays.Layout:
    """Where each screen sits in the screenshots this server returns.  Derived
    from the current displays, so clicks map the same way the last shot did."""
    return displays.layout(displays.active_displays(), MAX_IMAGE_SIDE)


def _to_screen(x: float, y: float) -> tuple[float, float]:
    """Image pixel → global screen point (any monitor)."""
    return _layout().to_screen(x, y)


def screenshot() -> Dict[str, Any]:
    import Quartz

    if not Quartz.CGPreflightScreenCaptureAccess():
        raise ActionRefused(
            "macOS has not given OpenJarvis the Screen Recording permission "
            "(System Settings > Privacy & Security > Screen Recording)."
        )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "screen.jpg"
        lay = displays.capture(path, MAX_IMAGE_SIDE)
        data = base64.b64encode(path.read_bytes()).decode()
    return [
        {"type": "image", "data": data, "mimeType": "image/jpeg"},
        {"type": "text", "text": lay.describe()},
    ]


def click(x: float, y: float, button: str = "left", double: bool = False) -> str:
    import Quartz

    _require_input_permission()
    sx, sy = _to_screen(x, y)
    _check_target(_bundle_at(sx, sy))
    right = button == "right"
    down = Quartz.kCGEventRightMouseDown if right else Quartz.kCGEventLeftMouseDown
    up = Quartz.kCGEventRightMouseUp if right else Quartz.kCGEventLeftMouseUp
    which = Quartz.kCGMouseButtonRight if right else Quartz.kCGMouseButtonLeft
    for count in range(1, 3 if double else 2):
        for kind in (down, up):
            event = Quartz.CGEventCreateMouseEvent(None, kind, (sx, sy), which)
            Quartz.CGEventSetIntegerValueField(
                event, Quartz.kCGMouseEventClickState, count
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    return f"Clicked at ({x:.0f}, {y:.0f})."


def type_text(text: str) -> str:
    import Quartz

    _require_input_permission()
    _check_target(_front_bundle())
    for start in range(0, len(text), 20):
        chunk = text[start : start + 20]
        for pressed in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, pressed)
            Quartz.CGEventKeyboardSetUnicodeString(event, len(chunk), chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    return f"Typed {len(text)} characters."


def parse_combo(combo: str) -> tuple[int, int]:
    """``'cmd+shift+n'`` to ``(key code, modifier flags)``."""
    normalized = combo.strip().lower().replace(" ", "")
    if normalized in BLOCKED_COMBOS:
        raise ActionRefused(f"Refused: {combo} can reach apps that are off limits.")
    *mods, key = normalized.split("+")
    if key not in KEY_CODES or any(m not in MODIFIERS for m in mods):
        raise ValueError(f"Unknown key combination '{combo}'.")
    flags = 0
    for mod in mods:
        flags |= MODIFIERS[mod]
    return KEY_CODES[key], flags


def key(combo: str) -> str:
    import Quartz

    code, flags = parse_combo(combo)
    _require_input_permission()
    _check_target(_front_bundle())
    for pressed in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, code, pressed)
        Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    return f"Pressed {combo}."


def scroll(amount: int) -> str:
    import Quartz

    _require_input_permission()
    _check_target(_front_bundle())
    event = Quartz.CGEventCreateScrollWheelEvent(
        None, Quartz.kCGScrollEventUnitLine, 1, int(amount)
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    return f"Scrolled {amount}."


def open_app(name: str) -> str:
    cleaned = name.strip()
    if not cleaned or cleaned.startswith("-"):
        raise ValueError("Say which app.")
    if cleaned.lower().removesuffix(".app") in BLOCKED_APP_NAMES:
        raise ActionRefused(f"Refused: {cleaned} is off limits for computer control.")
    proc = subprocess.run(
        ["open", "-a", cleaned], capture_output=True, text=True, timeout=15
    )
    if proc.returncode != 0:
        raise ValueError(f"Could not open '{cleaned}'.")
    return f"Opened {cleaned}."


def wait(seconds: float) -> str:
    time.sleep(max(0.0, min(float(seconds), 10.0)))
    return "Waited."


def _schema(properties: Dict[str, Any], required: list[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


_NUMBER = {"type": "number"}
_STRING = {"type": "string"}
TOOLS = [
    {
        "name": "screenshot",
        "description": "See every connected screen, tiled into one image. "
        "Coordinates in other tools are pixels of this image.",
        "inputSchema": _schema({}, []),
    },
    {
        "name": "click",
        "description": "Click at image coordinates.",
        "inputSchema": _schema(
            {
                "x": _NUMBER,
                "y": _NUMBER,
                "button": {"type": "string", "enum": ["left", "right"]},
                "double": {"type": "boolean"},
            },
            ["x", "y"],
        ),
    },
    {
        "name": "type_text",
        "description": "Type text into the focused field.",
        "inputSchema": _schema({"text": _STRING}, ["text"]),
    },
    {
        "name": "key",
        "description": "Press a key or combination, e.g. 'return' or 'cmd+n'.",
        "inputSchema": _schema({"combo": _STRING}, ["combo"]),
    },
    {
        "name": "scroll",
        "description": "Scroll lines: positive up, negative down.",
        "inputSchema": _schema({"amount": {"type": "integer"}}, ["amount"]),
    },
    {
        "name": "open_app",
        "description": "Open an application by name.",
        "inputSchema": _schema({"name": _STRING}, ["name"]),
    },
    {
        "name": "wait",
        "description": "Wait up to 10 seconds for the screen to settle.",
        "inputSchema": _schema({"seconds": _NUMBER}, ["seconds"]),
    },
]
_FUNCTIONS = {
    "click": click,
    "type_text": type_text,
    "key": key,
    "scroll": scroll,
    "open_app": open_app,
    "wait": wait,
}


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if name == "screenshot":
            return {"content": screenshot()}
        if name not in _FUNCTIONS:
            raise ValueError(f"Unknown tool '{name}'.")
        text = _FUNCTIONS[name](**arguments)
    except (ActionRefused, ValueError, TypeError, OSError) as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    except subprocess.SubprocessError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    return {"content": [{"type": "text", "text": text}]}


def handle(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The JSON-RPC response to *message*, or ``None`` for a notification."""
    if "id" not in message:
        return None
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        result: Dict[str, Any] = {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "jarvis-desktop", "version": "1.0"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = call_tool(params.get("name", ""), params.get("arguments") or {})
    elif method == "ping":
        result = {}
    else:
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32601, "message": f"Unknown method {method}"},
        }
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        response = handle(json.loads(line))
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
