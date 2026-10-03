"""macOS browser tools — open web pages and control the Spotify web player.

Lets the user say:
- "Jarvis, abre youtube.com en Safari"
- "Jarvis, reproduce Spotify" / "pausa Spotify" / "siguiente canción"
- "Jarvis, pon Feid en Spotify"

* ``open_url``  opens an http(s) URL in a browser (``[tools.launcher]
                browser``, else the system default; Arc, Safari, Chrome…).
* ``spotify``   drives open.spotify.com in the user's own browser session:
                it focuses the existing Spotify tab (or opens one) and
                clicks the player buttons through AppleScript-injected
                JavaScript.  Works in Arc, Google Chrome and Safari.

Guard rails — the agent also reads untrusted text (emails, web pages):

* only ``http``/``https`` URLs are opened (no ``file:``, ``javascript:`` or
  app schemes);
* the injected JavaScript is fixed in this module; only the action name
  reaches it, and arguments go to AppleScript through ``on run argv``.

Arc and Chrome put background tabs to sleep, so a tab must be brought to
the front before its page answers; that is why ``spotify`` selects it.
Safari and Chrome need "Allow JavaScript from Apple Events" enabled in
their Develop menu; Arc needs nothing.
"""

# AppleScript bodies below keep their natural line lengths.
# ruff: noqa: E501

from __future__ import annotations

import json
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec
from openjarvis.tools.launcher import _launcher_config, _norm, resolve_app

_SPOTIFY_HOST = "open.spotify.com"

# Browsers whose tabs we can script, keyed by normalised app name.
_SCRIPTABLE = {"arc": "arc", "googlechrome": "chrome", "safari": "safari"}

_DEFAULT_BROWSER_BUNDLES = {
    "company.thebrowser.browser": "Arc",
    "com.google.chrome": "Google Chrome",
    "com.apple.safari": "Safari",
}


def _not_macos(tool: str) -> Optional[ToolResult]:
    if sys.platform != "darwin":
        return ToolResult(
            tool_name=tool, content="This tool only works on macOS.", success=False
        )
    return None


# ── URLs ────────────────────────────────────────────────────────────────


def normalize_url(raw: str) -> Optional[str]:
    """Return an ``http(s)`` URL for ``raw``, or None if it isn't one.

    Bare domains ("youtube.com", "github.com/foo") get ``https://``.
    """
    text = raw.strip()
    if not text or any(c.isspace() for c in text):
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        if not re.match(r"^(localhost|[\w-]+(\.[\w-]+)+)(:\d+)?([/?#]|$)", text):
            return None
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return text


# ── Browsers ────────────────────────────────────────────────────────────


def _default_browser() -> str:
    """App name of the system default browser ("" if unknown)."""
    plist = (
        Path.home()
        / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    )
    try:
        handlers = plistlib.loads(plist.read_bytes()).get("LSHandlers", [])
    except (OSError, plistlib.InvalidFileException, ValueError):
        return ""
    for h in handlers:
        if h.get("LSHandlerURLScheme") == "https":
            bundle = str(h.get("LSHandlerRoleAll", "")).lower()
            return _DEFAULT_BROWSER_BUNDLES.get(bundle, "")
    return ""


def resolve_browser(requested: str, cfg) -> Tuple[Optional[Path], str]:
    """Resolve the browser to use → ``(app_path, error)``.

    ``app_path`` is None with an empty error when the system default should
    be used as-is (plain ``open <url>``).
    """
    name = requested.strip() or (getattr(cfg, "browser", "") or "")
    if not name:
        return None, ""
    app, candidates = resolve_app(name, cfg.app_aliases)
    if app is None:
        hint = f" Similar installed apps: {', '.join(candidates)}." if candidates else ""
        return None, f"Browser '{name}' is not installed.{hint}"
    return app, ""


def _scriptable_kind(app_name: str) -> Optional[str]:
    return _SCRIPTABLE.get(_norm(app_name))


def _osascript(script: str, *args: str, timeout: float = 20.0) -> str:
    proc = subprocess.run(
        ["osascript", "-", *args],
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "osascript failed")
    return proc.stdout.strip()


def _open_in_browser(url: str, app: Optional[Path]) -> None:
    cmd = ["open", "-a", str(app), url] if app else ["open", url]
    subprocess.run(cmd, check=True, capture_output=True, timeout=15)


# ── Tab scripting (Arc / Chrome / Safari) ───────────────────────────────

_LIST_TABS = {
    # One line per tab: "<window>\t<tab>\t<url>".  URLs are fetched per
    # window in one Apple Event — per-tab access is far too slow with
    # hundreds of Arc tabs.
    "arc": 'tell application "Arc"',
    "chrome": 'tell application "Google Chrome"',
    "safari": 'tell application "Safari"',
}

# Inside the browser's ``tell`` block ``tab`` means the tab class, not the
# tab character, so the separator is spelled ``character id 9``.
_LIST_TABS_BODY = """
  set sep to character id 9
  set out to {}
  repeat with w from 1 to count of windows
    set us to URL of every tab of window w
    repeat with k from 1 to count of us
      set u to item k of us
      if u is missing value then set u to ""
      set end of out to (w as text) & sep & (k as text) & sep & u
    end repeat
  end repeat
  set AppleScript's text item delimiters to linefeed
  return out as text
end tell
"""

_SELECT_TAB = {
    "arc": """on run argv
  set w to (item 1 of argv) as integer
  set k to (item 2 of argv) as integer
  tell application "Arc"
    tell tab k of window w to select
    activate
  end tell
end run""",
    "chrome": """on run argv
  set w to (item 1 of argv) as integer
  set k to (item 2 of argv) as integer
  tell application "Google Chrome"
    set active tab index of window w to k
    set index of window w to 1
    activate
  end tell
end run""",
    "safari": """on run argv
  set w to (item 1 of argv) as integer
  set k to (item 2 of argv) as integer
  tell application "Safari"
    set current tab of window w to tab k of window w
    set index of window w to 1
    activate
  end tell
end run""",
}

_RUN_JS = {
    "arc": """on run argv
  tell application "Arc" to tell front window to tell active tab
    return execute javascript (item 1 of argv)
  end tell
end run""",
    "chrome": """on run argv
  tell application "Google Chrome"
    return execute active tab of front window javascript (item 1 of argv)
  end tell
end run""",
    "safari": """on run argv
  tell application "Safari"
    return do JavaScript (item 1 of argv) in current tab of front window
  end tell
end run""",
}


def find_tabs(kind: str, host: str) -> List[Tuple[int, int, str]]:
    """``(window, tab, url)`` of every open tab whose URL is on ``host``."""
    out = _osascript(_LIST_TABS[kind] + _LIST_TABS_BODY)
    found = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        try:
            netloc = urlsplit(parts[2]).netloc.lower()
        except ValueError:
            continue
        if netloc == host:
            found.append((int(parts[0]), int(parts[1]), parts[2]))
    return found


def select_tab(kind: str, window: int, tab: int) -> None:
    _osascript(_SELECT_TAB[kind], str(window), str(tab))


def run_js(kind: str, js: str) -> str:
    """Run ``js`` in the front tab; returns its result as a string."""
    out = _osascript(_RUN_JS[kind], js)
    # Arc / Chrome hand strings back JSON-quoted.
    if len(out) >= 2 and out[0] == out[-1] == '"':
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return out[1:-1]
    return out


# ── Spotify web player ──────────────────────────────────────────────────

# Runs in open.spotify.com.  Returns "waiting" until the player has
# rendered, "login" when signed out, else a short status.  Button labels
# are localised, so "is playing" checks both English and Spanish.
# ``search_play`` first navigates the tab to /search/<query> (if it isn't
# there yet) so the previous search's top result is never played.
_SPOTIFY_JS = """(() => {
  const action = %s;
  const query = %s;
  const q = s => document.querySelector(s);
  if (action === 'search_play') {
    const here = decodeURIComponent(location.pathname).toLowerCase();
    if (!here.startsWith('/search/' + query.toLowerCase())) {
      location.assign('/search/' + encodeURIComponent(query));
      return 'waiting';
    }
  }
  if (q('[data-testid="login-button"]') && !q('[data-testid="user-widget-link"]')) return 'login';
  const pp = q('[data-testid="control-button-playpause"]');
  if (!pp) return 'waiting';
  const playing = /^(pause|pausar)/i.test(pp.getAttribute('aria-label') || '');
  if (action === 'status') return playing ? 'playing' : 'paused';
  if (action === 'play') { if (playing) return 'already'; pp.click(); return 'ok'; }
  if (action === 'pause') { if (!playing) return 'already'; pp.click(); return 'ok'; }
  if (action === 'search_play') {
    const b = q('main button[data-testid="play-button"]');
    if (!b) return 'waiting';
    b.click(); return 'ok';
  }
  const skip = {next: 'control-button-skip-forward', previous: 'control-button-skip-back'}[action];
  if (skip) { const b = q('[data-testid="' + skip + '"]'); if (!b) return 'waiting'; b.click(); return 'ok'; }
  return 'unknown action';
})()"""

_ACTIONS = ("play", "pause", "next", "previous")


def spotify_js(action: str, query: str = "") -> str:
    return _SPOTIFY_JS % (json.dumps(action), json.dumps(query))


def _poll(
    kind: str, action: str, timeout: float, query: str = "", interval: float = 1.0
) -> str:
    """Retry ``action`` until the page stops answering "waiting"."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = run_js(kind, spotify_js(action, query))
        except RuntimeError:
            result = "waiting"  # tab still loading / waking up
        if result != "waiting" or time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def _js_error_hint(kind: str, err: str) -> str:
    if kind in ("safari", "chrome") and "javascript" in err.lower():
        menu = "Develop" if kind == "safari" else "View > Developer"
        return (
            f" Enable 'Allow JavaScript from Apple Events' in {menu} and try again."
        )
    return ""


# ── Spotify without touching the user's tabs (desktop notch) ────────────
# The tool above brings the Spotify tab to the front because it runs JS in
# the active tab.  The notch polls what is playing every few seconds and
# its buttons must not steal focus, so these run JS in the Spotify tab
# where it is, by window/tab index (cached, re-found when it moves).

_RUN_JS_IN_TAB = {
    "arc": """on run argv
  set w to (item 1 of argv) as integer
  set k to (item 2 of argv) as integer
  tell application "Arc" to tell window w to tell tab k
    return execute javascript (item 3 of argv)
  end tell
end run""",
    "chrome": """on run argv
  set w to (item 1 of argv) as integer
  set k to (item 2 of argv) as integer
  tell application "Google Chrome"
    return execute tab k of window w javascript (item 3 of argv)
  end tell
end run""",
    "safari": """on run argv
  set w to (item 1 of argv) as integer
  set k to (item 2 of argv) as integer
  tell application "Safari"
    return do JavaScript (item 3 of argv) in tab k of window w
  end tell
end run""",
}

# What the player shows: the Media Session metadata Spotify publishes (with
# the DOM as a fallback), as JSON.  ``host`` lets the caller notice that the
# cached window/tab index now points at another page.
_NOW_PLAYING_JS = """(() => {
  const q = s => document.querySelector(s);
  const host = location.host;
  const pp = q('[data-testid="control-button-playpause"]');
  if (!pp) return JSON.stringify({host, state: 'none'});
  const m = navigator.mediaSession && navigator.mediaSession.metadata;
  const text = s => ((q(s) || {}).textContent || '').trim();
  const art = m && m.artwork && m.artwork.length
    ? m.artwork[m.artwork.length - 1].src
    : ((q('[data-testid="now-playing-widget"] img') || {}).src || '');
  return JSON.stringify({
    host,
    state: /^(pause|pausar)/i.test(pp.getAttribute('aria-label') || '') ? 'playing' : 'paused',
    title: (m && m.title) || text('[data-testid="context-item-info-title"]'),
    artist: (m && m.artist) || text('[data-testid="context-item-info-artist"]'),
    art,
  });
})()"""

_spotify_tab: Optional[Tuple[str, int, int]] = None


def _unquote(out: str) -> str:
    if len(out) >= 2 and out[0] == out[-1] == '"':
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return out[1:-1]
    return out


def run_js_in_tab(kind: str, window: int, tab: int, js: str) -> str:
    """Run ``js`` in a given tab without selecting it or raising the browser."""
    return _unquote(_osascript(_RUN_JS_IN_TAB[kind], str(window), str(tab), js, timeout=6))


def _spotify_browser_kind() -> Optional[str]:
    cfg = _launcher_config()
    app, error = resolve_browser(_default_browser(), cfg)
    if error or app is None:
        return None
    return _scriptable_kind(app.stem)


def _in_spotify_tab(js: str, here: Callable[[str], bool]) -> Optional[str]:
    """Run ``js`` in the open Spotify tab; None when there is none.

    ``here(output)`` says whether the JS ran on Spotify; when the cached tab
    index points elsewhere (tabs moved), the tab is looked up again once.
    """
    global _spotify_tab
    for attempt in range(2):
        if _spotify_tab is None or attempt:
            kind = _spotify_browser_kind()
            if kind is None:
                return None
            tabs = find_tabs(kind, _SPOTIFY_HOST)
            if not tabs:
                _spotify_tab = None
                return None
            _spotify_tab = (kind, tabs[0][0], tabs[0][1])
        kind, window, tab = _spotify_tab
        try:
            out = run_js_in_tab(kind, window, tab, js)
        except (subprocess.SubprocessError, OSError, RuntimeError):
            continue
        if here(out):
            return out
    return None


def spotify_now_playing() -> Dict[str, Any]:
    """``{"state": "playing"|"paused"|"none", "title", "artist", "art"}``."""
    if sys.platform != "darwin":
        return {"state": "none"}
    try:
        out = _in_spotify_tab(_NOW_PLAYING_JS, lambda o: _SPOTIFY_HOST in o)
        data = json.loads(out) if out else {}
    except (ValueError, TypeError):
        data = {}
    if data.get("host") != _SPOTIFY_HOST:
        return {"state": "none"}
    data.pop("host", None)
    return data


def spotify_control(action: str) -> bool:
    """play / pause / next / previous in the Spotify tab, in the background."""
    if action not in _ACTIONS or sys.platform != "darwin":
        return False
    guard = "if (location.host !== %s) return 'elsewhere';" % json.dumps(_SPOTIFY_HOST)
    js = spotify_js(action).replace("(() => {", "(() => { " + guard, 1)
    return _in_spotify_tab(js, lambda o: o != "elsewhere") in ("ok", "already")


# ── Tools ───────────────────────────────────────────────────────────────


@ToolRegistry.register("open_url")
class OpenUrlTool(BaseTool):
    """Open a web page in the user's browser."""

    tool_id = "open_url"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="open_url",
            description=(
                "Open a web page in the user's browser. Pass a full URL or a "
                "domain (e.g. 'youtube.com', 'https://github.com/x/y'); build the "
                "URL yourself when the user names a site. Optionally choose the "
                "browser ('Arc', 'Safari', 'Chrome'); otherwise the user's "
                "configured/default browser is used. For playing music use the "
                "spotify tool instead."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "http(s) URL or domain to open.",
                    },
                    "browser": {
                        "type": "string",
                        "description": "Optional browser name the user asked for.",
                    },
                },
                "required": ["url"],
            },
            category="system",
            timeout_seconds=20.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        tool = "open_url"
        if (err := _not_macos(tool)) is not None:
            return err
        raw = str(params.get("url") or "")
        url = normalize_url(raw)
        if url is None:
            return ToolResult(
                tool_name=tool,
                content=f"'{raw}' is not a web address (only http/https URLs open).",
                success=False,
            )
        cfg = _launcher_config()
        app, error = resolve_browser(str(params.get("browser") or ""), cfg)
        if error:
            return ToolResult(tool_name=tool, content=error, success=False)
        try:
            _open_in_browser(url, app)
        except (subprocess.SubprocessError, OSError) as exc:
            return ToolResult(
                tool_name=tool, content=f"Could not open {url}: {exc}", success=False
            )
        where = f" in {app.stem}" if app else ""
        return ToolResult(
            tool_name=tool, content=f"Opened {url}{where}.", success=True
        )


@ToolRegistry.register("spotify")
class SpotifyTool(BaseTool):
    """Play, pause or skip music in the Spotify web player."""

    tool_id = "spotify"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="spotify",
            description=(
                "Control Spotify in the user's browser (open.spotify.com, signed "
                "in as the user). action='play' resumes playback — or, with a "
                "query, searches Spotify and plays the top result (artist, song, "
                "album or playlist); 'pause', 'next' and 'previous' control the "
                "player. Use it for 'reproduce Spotify', 'pon <artista>', "
                "'pausa la música', 'siguiente canción'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(_ACTIONS),
                        "description": "What to do. Default 'play'.",
                    },
                    "query": {
                        "type": "string",
                        "description": "With action='play': what to search and play.",
                    },
                    "browser": {
                        "type": "string",
                        "description": "Optional browser: 'Arc', 'Chrome' or 'Safari'.",
                    },
                },
                "required": [],
            },
            category="system",
            timeout_seconds=45.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        tool = "spotify"
        if (err := _not_macos(tool)) is not None:
            return err
        action = str(params.get("action") or "play").lower()
        if action not in _ACTIONS:
            return ToolResult(
                tool_name=tool,
                content=f"Unknown action '{action}'; use one of {', '.join(_ACTIONS)}.",
                success=False,
            )
        query = str(params.get("query") or "").strip()

        cfg = _launcher_config()
        requested = str(params.get("browser") or "")
        app, error = resolve_browser(requested or _default_browser(), cfg)
        if error:
            return ToolResult(tool_name=tool, content=error, success=False)
        kind = _scriptable_kind(app.stem) if app else None
        if kind is None:
            name = app.stem if app else "the default browser"
            return ToolResult(
                tool_name=tool,
                content=(
                    f"Spotify control needs Arc, Google Chrome or Safari; "
                    f"{name} can't be scripted. Ask which of those to use."
                ),
                success=False,
            )

        try:
            # Reuse the open Spotify tab (one web player, no tab pile-up).
            tabs = find_tabs(kind, _SPOTIFY_HOST)
            if tabs:
                window, tab, _ = tabs[0]
                select_tab(kind, window, tab)
            elif action == "play":
                path = f"search/{quote(query, safe='')}" if query else ""
                _open_in_browser(f"https://{_SPOTIFY_HOST}/{path}", app)
            else:
                return ToolResult(
                    tool_name=tool,
                    content=f"Spotify isn't open in {app.stem}; nothing to {action}.",
                    success=False,
                )
            if action == "play" and query:
                result = _poll(kind, "search_play", timeout=20, query=query)
            else:
                result = _poll(kind, action, timeout=20)

            if result == "ok" and action == "play":
                # Confirm the player actually started (autoplay can be blocked).
                time.sleep(2)
                if _poll(kind, "status", timeout=5) != "playing":
                    result = "not_started"
        except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
            msg = str(exc)
            return ToolResult(
                tool_name=tool,
                content=f"Could not control Spotify in {app.stem}: {msg}.{_js_error_hint(kind, msg)}",
                success=False,
            )

        what = f"'{query}'" if query else "Spotify"
        messages = {
            "ok": {
                "play": f"Playing {what} in {app.stem}.",
                "pause": "Paused Spotify.",
                "next": "Skipped to the next track.",
                "previous": "Went back to the previous track.",
            }[action],
            "already": f"Spotify was already {'playing' if action == 'play' else 'paused'}.",
            "login": f"Spotify in {app.stem} is signed out; the user must log in at open.spotify.com.",
            "waiting": "Spotify didn't finish loading in time; try again.",
            "not_started": (
                f"Pressed play on {what} but playback didn't start — the "
                "browser may be blocking autoplay; the user can press play once."
            ),
        }
        content = messages.get(result, f"Unexpected response from Spotify: {result}")
        return ToolResult(
            tool_name=tool,
            content=content,
            success=result in ("ok", "already"),
            metadata={"browser": app.stem, "action": action, "result": result},
        )
