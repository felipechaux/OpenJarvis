"""Organizing Apple Notes: moving is direct, deleting needs the user's words."""

from __future__ import annotations

import json
import stat
import subprocess
import time
from unittest.mock import patch

import pytest

from openjarvis.agents import session_guard
from openjarvis.agents.fast_paths import match_fast_path
from openjarvis.core.types import ToolCall
from openjarvis.tools import apple_notes as an

SEP, ROW = an._SEP, an._ROW


@pytest.fixture(autouse=True)
def _macos(monkeypatch, tmp_path):
    monkeypatch.setattr(an.sys, "platform", "darwin")
    monkeypatch.setattr(an, "_notes_folder", lambda: "JARVIS")
    monkeypatch.setattr(an, "PENDING_DELETE", tmp_path / "pending.json")


def _proc(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _tool(**params):
    return an.AppleNotesTool().execute(**params)


def test_folders_hide_the_trash():
    out = f"Ideas{SEP}4{ROW}Recently Deleted{SEP}9{ROW}"
    with patch("subprocess.run", return_value=_proc(out)):
        res = _tool(action="folders")
    assert "Ideas (4 notes)" in res.content and "Deleted" not in res.content


def test_list_any_folder_with_paging():
    out = "".join(f"n{i}{SEP}hoy{ROW}" for i in range(5))
    with patch("subprocess.run", return_value=_proc(out)) as run:
        res = _tool(action="list", folder="Ideas", limit=2, offset=1)
    assert run.call_args.args[0][3] == "Ideas"
    assert "n1" in res.content and "n3" not in res.content
    assert "offset=3" in res.content and "5 total" in res.content


def test_list_defaults_to_the_jarvis_folder():
    with patch("subprocess.run", return_value=_proc(f"a{SEP}hoy{ROW}")) as run:
        _tool(action="list")
    assert run.call_args.args[0][3] == "JARVIS"


def test_create_folder_reports_an_existing_one():
    with patch("subprocess.run", return_value=_proc("exists")):
        assert "already" in _tool(action="create_folder", folder="Ideas").content


def test_move_passes_titles_as_arguments():
    out = f"Uno{SEP}moved{ROW}Dos{SEP}moved{ROW}"
    with patch("subprocess.run", return_value=_proc(out)) as run:
        res = _tool(action="move", titles=["Uno", "Dos"], folder="Ideas")
    assert res.success and "Moved 2 of 2" in res.content
    assert run.call_args.args[0][3:] == ["Ideas", "", "Uno", "Dos"]


def test_move_does_not_guess_a_repeated_title():
    out = f"Uno{SEP}ambiguous{SEP}Ideas, Notes, {ROW}"
    with patch("subprocess.run", return_value=_proc(out)):
        res = _tool(action="move", titles=["Uno"], folder="X")
    assert not res.success
    assert "Ideas, Notes" in res.content and "from_folder" in res.content


def test_move_to_a_missing_folder():
    with patch("subprocess.run", return_value=_proc("NO_FOLDER")):
        res = _tool(action="move", titles=["Uno"], folder="X")
    assert not res.success and "no folder X" in res.content


def test_cannot_move_into_the_trash():
    with patch("subprocess.run") as run:
        assert not _tool(action="move", title="U", folder="Recently Deleted").success
    run.assert_not_called()


def _stage(monkeypatch=None):
    out = f"Uno{SEP}found{SEP}x-id-1{ROW}Dos{SEP}missing{ROW}"
    with patch("subprocess.run", return_value=_proc(out)):
        return _tool(action="delete", titles=["Uno", "Dos"])


def test_delete_only_stages():
    out = f"Uno{SEP}found{SEP}id1{ROW}"
    with patch("subprocess.run", return_value=_proc(out)) as r:
        res = _tool(action="delete", titles=["Uno"])
    assert res.success and "Not deleted yet" in res.content
    assert r.call_count == 1 and "delete note id" not in r.call_args.args[0][2]


def test_stage_reports_missing_titles_and_is_private():
    res = _stage()
    assert "Dos" in res.content
    assert stat.S_IMODE(an.PENDING_DELETE.stat().st_mode) == 0o600


def test_confirm_deletes_only_the_staged_notes_once():
    _stage()
    with patch("subprocess.run", return_value=_proc("ok")) as run:
        res = _tool(action="confirm_delete")
    assert res.success
    assert run.call_args.args[0][3:] == ["x-id-1"]
    assert not _tool(action="confirm_delete").success


def test_expired_stage_cannot_be_confirmed():
    _stage()
    pending = json.loads(an.PENDING_DELETE.read_text())
    pending["created_at"] = time.time() - an._DELETE_TTL_S - 1
    an.PENDING_DELETE.write_text(json.dumps(pending))
    with patch("subprocess.run") as run:
        assert not _tool(action="confirm_delete").success
    run.assert_not_called()


def test_cancel_delete():
    _stage()
    assert _tool(action="cancel_delete").success
    assert not an.PENDING_DELETE.exists()


def test_confirm_is_not_offered_to_the_model():
    enum = an.AppleNotesTool().spec.parameters["properties"]["action"]["enum"]
    assert "confirm_delete" not in enum and "delete" in enum


@pytest.mark.parametrize("tainted", [False, True])
def test_guard_refuses_every_model_issued_confirm(tainted):
    args = json.dumps({"action": "confirm_delete"})
    call = ToolCall(id="1", name="apple_notes", arguments=args)
    assert session_guard.check(call, tainted) is not None


def test_moving_is_allowed_after_listing():
    args = json.dumps({"action": "move", "titles": ["a"], "folder": "b"})
    call = ToolCall(id="1", name="apple_notes", arguments=args)
    assert session_guard.check(call, True) is None


@pytest.mark.parametrize("text", ["Bórralas", "sí, bórrala", "confirmo el borrado"])
def test_the_users_words_confirm(text):
    path = match_fast_path(text, {"apple_notes"})
    assert path is not None and path.arguments == {"action": "confirm_delete"}


@pytest.mark.parametrize("text", ["cancela el borrado", "no las borres"])
def test_the_users_words_cancel(text):
    path = match_fast_path(text, {"apple_notes"})
    assert path is not None and path.arguments == {"action": "cancel_delete"}


@pytest.mark.parametrize("text", ["borra la nota de compras", "bórralas todas"])
def test_other_phrasing_stays_with_the_model(text):
    assert match_fast_path(text, {"apple_notes"}) is None
