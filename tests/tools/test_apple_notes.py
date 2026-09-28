"""Tests for the Apple Notes tool (osascript mocked)."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from openjarvis.tools import apple_notes as an

SEP, ROW = an._SEP, an._ROW


@pytest.fixture(autouse=True)
def _macos(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(an.sys, "platform", "darwin")
    monkeypatch.setattr(an, "_notes_folder", lambda: "JARVIS")


def _proc(stdout: str = "", rc: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


def _tool(**params):
    return an.AppleNotesTool().execute(**params)


def test_html_escapes_user_text() -> None:
    body = an.to_html("Compras", "leche & <pan>\n")
    assert body.startswith("<div><b>Compras</b></div>")
    assert "leche &amp; &lt;pan&gt;" in body


def test_create_passes_args_not_interpolated() -> None:
    with patch("subprocess.run", return_value=_proc("Compras\n")) as run:
        res = _tool(action="create", title="Compras", text='x" & do shell script "rm')
    assert res.success and "Compras" in res.content
    argv = run.call_args.args[0]
    assert argv[0] == "osascript" and argv[3] == "JARVIS"
    # User text only ever travels as an argv item, HTML-escaped.
    assert "rm" not in argv[2] and "&quot;" in argv[4]


def test_append_missing_note() -> None:
    with patch("subprocess.run", return_value=_proc("")):
        res = _tool(action="append", title="Nope", text="hola")
    assert not res.success and "No note titled" in res.content


def test_read_formats_note() -> None:
    out = f"Ideas{SEP}JARVIS{SEP}Ideas\nuna app"
    with patch("subprocess.run", return_value=_proc(out)):
        res = _tool(action="read", title="Ideas")
    assert res.content == "Note 'Ideas' (folder JARVIS):\nIdeas\nuna app"


def test_search_rows() -> None:
    out = f"Robot idea{SEP}{ROW}Cena idea{SEP}Notes{ROW}"
    with patch("subprocess.run", return_value=_proc(out)):
        res = _tool(action="search", query="idea")
    assert res.content == "- Robot idea\n- Cena idea (folder Notes)"


def test_permission_denied_is_explained() -> None:
    err = "execution error: Not authorized to send Apple events to Notes. (-1743)"
    with patch("subprocess.run", return_value=_proc(rc=1, stderr=err)):
        res = _tool(action="list")
    assert not res.success and "Automation" in res.content


def test_validation() -> None:
    assert not _tool(action="create").success
    assert not _tool(action="append", title="x").success
    assert not _tool(action="read").success
    assert not _tool(action="bogus").success
