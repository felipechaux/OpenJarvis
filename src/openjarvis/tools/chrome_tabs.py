"""chrome_tabs — list, focus or close the user's Chrome tabs via AppleScript.

The fast path for "¿qué tengo abierto?", "ve a la pestaña de Gmail" or
"cierra YouTube": one ``osascript`` call, no delegated agent.  Anything
inside a page (clicks, forms, reading) stays with ``browser_task``.

Tabs are addressed by the ids from a fresh listing, so window order changing
between the listing and the action cannot hit the wrong tab.  A query that
matches several tabs is not guessed at: the matches are listed back.  The
first run makes macOS ask once to let JARVIS control Google Chrome.

Tab titles are page text, so this tool is NOT in
``session_guard.TRUSTED_OUTPUT_TOOLS``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, List

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

TOOL = "chrome_tabs"
TIMEOUT_S = 10
MAX_LISTED = 30

_LIST = """
set sep to ASCII character 31
set out to ""
if application "Google Chrome" is not running then return "NOT_RUNNING"
tell application "Google Chrome"
  repeat with w in windows
    set wid to id of w
    set active to active tab index of w
    set i to 0
    repeat with t in tabs of w
      set i to i + 1
      set out to out & wid & sep & (id of t) & sep & (i = active) & sep ¬
        & (title of t) & sep & (URL of t) & linefeed
    end repeat
  end repeat
end tell
return out
"""

_FOCUS = """
on run argv
  set wid to (item 1 of argv) as integer
  set tid to (item 2 of argv) as integer
  tell application "Google Chrome"
    set w to window id wid
    set i to 0
    repeat with t in tabs of w
      set i to i + 1
      if id of t is tid then set active tab index of w to i
    end repeat
    set index of w to 1
    activate
  end tell
end run
"""

_CLOSE = """
on run argv
  tell application "Google Chrome"
    close (tab id ((item 2 of argv) as integer) of window id ((item 1 of argv) as integer))
  end tell
end run
"""


@dataclass
class Tab:
    window: str
    tab: str
    active: bool
    title: str
    url: str

    def line(self, n: int) -> str:
        mark = " (activa)" if self.active else ""
        # Query strings and fragments can carry tokens: never show them.
        url = self.url.split("?", 1)[0].split("#", 1)[0]
        return f"{n}. {self.title or '(sin título)'}{mark} — {url}"


def _osascript(script: str, *args: str) -> str:
    proc = subprocess.run(
        ["osascript", "-e", script, *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"osascript exit {proc.returncode}")
    return proc.stdout


def list_tabs() -> List[Tab] | None:
    """All tabs, window by window; ``None`` when Chrome is not running."""
    out = _osascript(_LIST).strip()
    if out == "NOT_RUNNING":
        return None
    tabs = []
    for row in out.splitlines():
        parts = row.split("\x1f")
        if len(parts) == 5:
            window, tab, active, title, url = parts
            tabs.append(Tab(window, tab, active == "true", title.strip(), url.strip()))
    return tabs


def find(tabs: List[Tab], query: str) -> List[Tab]:
    """Tabs by 1-based listing number, else by text in title or URL."""
    query = query.strip()
    if query.isdigit():
        n = int(query)
        return [tabs[n - 1]] if 1 <= n <= len(tabs) else []
    q = query.lower()
    return [t for t in tabs if q in t.title.lower() or q in t.url.lower()]


def _listing(tabs: List[Tab], only: List[Tab] | None = None) -> str:
    """Numbered lines; numbers always follow the full listing, as find() does."""
    shown = [(n, t) for n, t in enumerate(tabs, 1) if only is None or t in only]
    lines = [t.line(n) for n, t in shown[:MAX_LISTED]]
    if len(shown) > MAX_LISTED:
        lines.append(f"… y {len(shown) - MAX_LISTED} más.")
    return "\n".join(lines)


@ToolRegistry.register(TOOL)
class ChromeTabsTool(BaseTool):
    """List, focus or close tabs in the user's Chrome."""

    tool_id = TOOL

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=TOOL,
            description=(
                "The user's open Google Chrome tabs, instantly: 'list' them, "
                "'focus' one (bring it to the front) or 'close' one. Pick a tab "
                "by a word from its title or URL (e.g. 'gmail', 'youtube') or "
                "by its number from the last list. For anything inside a page "
                "use browser_task."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "focus", "close"]},
                    "tab": {
                        "type": "string",
                        "description": "For focus/close: a word from the tab's "
                        "title or URL, or its number from the list.",
                    },
                },
                "required": ["action"],
            },
            timeout_seconds=2 * TIMEOUT_S + 5,
        )

    def execute(self, **params: Any) -> ToolResult:
        action = str(params.get("action") or "list")
        query = str(params.get("tab") or "")
        try:
            content, ok = self._run(action, query)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            content, ok = f"Chrome did not answer: {exc}", False
        return ToolResult(tool_name=TOOL, content=content, success=ok)

    def _run(self, action: str, query: str) -> tuple[str, bool]:
        tabs = list_tabs()
        if tabs is None:
            return "Google Chrome is not open.", False
        if action == "list":
            if not tabs:
                return "Chrome has no open tabs.", True
            return f"{len(tabs)} tabs open:\n{_listing(tabs)}", True
        if action not in ("focus", "close"):
            return f"Unknown action '{action}'.", False
        if not query.strip():
            return "Say which tab.", False
        matches = find(tabs, query)
        if not matches:
            return f"No tab matches '{query}'. Open tabs:\n{_listing(tabs)}", False
        if len(matches) > 1:
            return (
                f"{len(matches)} tabs match '{query}'; ask which one:\n"
                f"{_listing(tabs, only=matches)}"
            ), False
        target = matches[0]
        _osascript(_FOCUS if action == "focus" else _CLOSE, target.window, target.tab)
        done = "Focused" if action == "focus" else "Closed"
        return f"{done}: {target.title or target.url}", True


__all__ = ["ChromeTabsTool", "Tab", "find", "list_tabs"]
