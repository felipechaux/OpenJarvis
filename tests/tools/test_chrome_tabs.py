"""chrome_tabs (tools/chrome_tabs.py): Chrome tabs over AppleScript."""

from __future__ import annotations

import pytest

from openjarvis.agents.session_guard import TRUSTED_OUTPUT_TOOLS
from openjarvis.tools import chrome_tabs
from openjarvis.tools.chrome_tabs import ChromeTabsTool

SEP = "\x1f"
LISTING = "\n".join(
    SEP.join(row)
    for row in [
        ("1", "11", "false", "Inbox - Gmail", "https://mail.google.com/mail/u/0/"),
        ("1", "12", "true", "YouTube", "https://www.youtube.com/watch?v=x&t=1"),
        ("2", "21", "true", "Gmail - Promociones", "https://mail.google.com/#promo"),
        ("2", "22", "false", "Login", "https://example.com/cb?token=secret"),
    ]
)


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake(script, *args):
        seen.append((script, args))
        return LISTING if script == chrome_tabs._LIST else ""

    monkeypatch.setattr(chrome_tabs, "_osascript", fake)
    return seen


def test_list_numbers_tabs_and_hides_query_strings(calls):
    result = ChromeTabsTool().execute(action="list")
    assert result.success and "4 tabs open" in result.content
    assert "2. YouTube (activa) — https://www.youtube.com/watch\n" in result.content
    assert "secret" not in result.content and "#promo" not in result.content


def test_focus_by_word_uses_window_and_tab_ids(calls):
    result = ChromeTabsTool().execute(action="focus", tab="youtube")
    assert result.success and result.content == "Focused: YouTube"
    assert calls[-1] == (chrome_tabs._FOCUS, ("1", "12"))


def test_close_by_number(calls):
    result = ChromeTabsTool().execute(action="close", tab="4")
    assert result.success
    assert calls[-1] == (chrome_tabs._CLOSE, ("2", "22"))


def test_ambiguous_match_acts_on_nothing_and_keeps_full_numbers(calls):
    result = ChromeTabsTool().execute(action="close", tab="gmail")
    assert not result.success
    assert "1. Inbox - Gmail" in result.content and "3. Gmail - Promociones" in result.content
    assert [script for script, _ in calls] == [chrome_tabs._LIST]


def test_no_match_lists_the_tabs(calls):
    result = ChromeTabsTool().execute(action="focus", tab="github")
    assert not result.success and "No tab matches" in result.content


def test_chrome_not_running(monkeypatch):
    monkeypatch.setattr(chrome_tabs, "_osascript", lambda script, *a: "NOT_RUNNING\n")
    result = ChromeTabsTool().execute(action="list")
    assert not result.success and "not open" in result.content


def test_tab_titles_are_untrusted():
    assert "chrome_tabs" not in TRUSTED_OUTPUT_TOOLS
