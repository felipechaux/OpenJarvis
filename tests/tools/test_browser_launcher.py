"""Tests for the browser tools (open_url, spotify)."""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from openjarvis.core.config import LauncherConfig
from openjarvis.tools import browser_launcher as bl

ARC = Path("/Applications/Arc.app")


@pytest.fixture(autouse=True)
def _macos(monkeypatch):
    monkeypatch.setattr(bl.sys, "platform", "darwin")
    monkeypatch.setattr(bl.time, "sleep", lambda _s: None)
    monkeypatch.setattr(bl, "_launcher_config", lambda: LauncherConfig())


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("youtube.com", "https://youtube.com"),
        ("github.com/x/y?tab=1", "https://github.com/x/y?tab=1"),
        ("http://example.org", "http://example.org"),
        ("https://open.spotify.com/", "https://open.spotify.com/"),
        ("localhost:3000/app", "https://localhost:3000/app"),
    ],
)
def test_normalize_url_accepts_web_addresses(raw, expected):
    assert bl.normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "mailto:a@b.com",
        "spotify:track:123",
        "ftp://example.org",
        "not a url",
        "youtube",
    ],
)
def test_normalize_url_rejects_everything_else(raw):
    assert bl.normalize_url(raw) is None


def test_resolve_browser_uses_config_then_default():
    cfg = LauncherConfig(browser="Safari")
    with patch.object(bl, "resolve_app", return_value=(Path("/A/Safari.app"), [])):
        app, err = bl.resolve_browser("", cfg)
    assert app == Path("/A/Safari.app") and err == ""

    app, err = bl.resolve_browser("", LauncherConfig())
    assert app is None and err == ""  # plain `open <url>`


def test_resolve_browser_reports_missing_app():
    with patch.object(bl, "resolve_app", return_value=(None, ["Arc"])):
        app, err = bl.resolve_browser("Ark", LauncherConfig())
    assert app is None and "Arc" in err


def test_scriptable_kind():
    assert bl._scriptable_kind("Arc") == "arc"
    assert bl._scriptable_kind("Google Chrome") == "chrome"
    assert bl._scriptable_kind("Safari") == "safari"
    assert bl._scriptable_kind("Firefox") is None


def test_find_tabs_matches_host_only():
    listing = "\n".join(
        [
            "1\t1\thttps://github.com",
            "1\t2\thttps://open.spotify.com/search/feid",
            "2\t5\thttps://evil.com/?u=open.spotify.com",
        ]
    )
    with patch.object(bl, "_osascript", return_value=listing):
        assert bl.find_tabs("arc", "open.spotify.com") == [
            (1, 2, "https://open.spotify.com/search/feid")
        ]


def test_run_js_unquotes_result():
    with patch.object(bl, "_osascript", return_value='"playing"'):
        assert bl.run_js("arc", "x") == "playing"


def test_spotify_js_embeds_action_as_json_literal():
    js = bl.spotify_js("search_play", 'Bad "Bunny"')
    assert 'const action = "search_play";' in js
    assert 'const query = "Bad \\"Bunny\\"";' in js


@patch("openjarvis.tools.browser_launcher.subprocess.run")
def test_open_url_opens_in_named_browser(mock_run: MagicMock):
    with patch.object(bl, "resolve_app", return_value=(ARC, [])):
        result = bl.OpenUrlTool().execute(url="youtube.com", browser="arc")
    assert result.success
    assert mock_run.call_args[0][0] == ["open", "-a", str(ARC), "https://youtube.com"]


@patch("openjarvis.tools.browser_launcher.subprocess.run")
def test_open_url_rejects_non_web_scheme(mock_run: MagicMock):
    result = bl.OpenUrlTool().execute(url="file:///etc/passwd")
    assert not result.success
    mock_run.assert_not_called()


def _spotify(monkeypatch, js_results: List[str], tabs=None):
    """Wire SpotifyTool to Arc with scripted page answers."""
    monkeypatch.setattr(bl, "_default_browser", lambda: "Arc")
    monkeypatch.setattr(bl, "resolve_app", lambda *_a: (ARC, []))
    opened, selected, actions = [], [], []
    monkeypatch.setattr(bl, "_open_in_browser", lambda url, app: opened.append(url))
    monkeypatch.setattr(bl, "find_tabs", lambda kind, host: tabs or [])
    monkeypatch.setattr(bl, "select_tab", lambda kind, w, t: selected.append((w, t)))
    answers = iter(js_results)

    def fake_run_js(kind, js):
        actions.append(js.split('const action = "', 1)[1].split('"', 1)[0])
        return next(answers)

    monkeypatch.setattr(bl, "run_js", fake_run_js)
    return opened, selected, actions


def test_spotify_play_resumes_existing_tab(monkeypatch):
    opened, selected, actions = _spotify(
        monkeypatch,
        ["waiting", "ok", "playing"],
        tabs=[(1, 242, "https://open.spotify.com/search/feid")],
    )
    result = bl.SpotifyTool().execute(action="play")
    assert result.success, result.content
    assert selected == [(1, 242)] and opened == []
    assert actions == ["play", "play", "status"]


def test_spotify_play_opens_player_when_no_tab(monkeypatch):
    opened, _, _ = _spotify(monkeypatch, ["ok", "playing"])
    result = bl.SpotifyTool().execute()
    assert result.success
    assert opened == ["https://open.spotify.com/"]


def test_spotify_play_query_opens_search_when_no_tab(monkeypatch):
    opened, _, actions = _spotify(monkeypatch, ["waiting", "ok", "playing"])
    result = bl.SpotifyTool().execute(action="play", query="Bad Bunny")
    assert result.success
    assert opened == ["https://open.spotify.com/search/Bad%20Bunny"]
    assert actions == ["search_play", "search_play", "status"]


def test_spotify_play_query_reuses_existing_tab(monkeypatch):
    opened, selected, actions = _spotify(
        monkeypatch,
        ["waiting", "ok", "playing"],
        tabs=[(1, 242, "https://open.spotify.com/search/feid")],
    )
    result = bl.SpotifyTool().execute(action="play", query="Karol G")
    assert result.success
    assert opened == [] and selected == [(1, 242)]
    assert actions == ["search_play", "search_play", "status"]


def test_spotify_reports_blocked_autoplay(monkeypatch):
    _spotify(monkeypatch, ["ok", "paused"], tabs=[(1, 1, "https://open.spotify.com/")])
    result = bl.SpotifyTool().execute(action="play")
    assert not result.success
    assert result.metadata["result"] == "not_started"


def test_spotify_pause_without_tab_fails_cleanly(monkeypatch):
    opened, _, _ = _spotify(monkeypatch, [])
    result = bl.SpotifyTool().execute(action="pause")
    assert not result.success and opened == []


def test_spotify_next_and_login(monkeypatch):
    tabs = [(2, 3, "https://open.spotify.com/")]
    _spotify(monkeypatch, ["ok"], tabs=tabs)
    assert bl.SpotifyTool().execute(action="next").success

    _spotify(monkeypatch, ["login"], tabs=tabs)
    result = bl.SpotifyTool().execute(action="pause")
    assert not result.success and "log in" in result.content


def test_spotify_rejects_unscriptable_browser(monkeypatch):
    monkeypatch.setattr(bl, "resolve_app", lambda *_a: (Path("/A/Firefox.app"), []))
    result = bl.SpotifyTool().execute(browser="Firefox")
    assert not result.success and "Arc" in result.content


def test_tools_registered():
    from openjarvis.core.registry import ToolRegistry

    for name, cls in (("open_url", bl.OpenUrlTool), ("spotify", bl.SpotifyTool)):
        if not ToolRegistry.contains(name):
            ToolRegistry.register(name)(cls)
        assert ToolRegistry.contains(name)
