"""mac_files (tools/mac_files.py): folders and moves inside home only."""

from __future__ import annotations

import json

import pytest

from openjarvis.agents import session_guard
from openjarvis.core.types import ToolCall
from openjarvis.tools.mac_files import MacFilesTool


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / "Desktop").mkdir()
    return tmp_path


def run(**params):
    return MacFilesTool().execute(**params)


def test_mkdir_with_a_spanish_alias(home):
    assert run(action="mkdir", path="escritorio/Facturas/2026").success
    assert (home / "Desktop" / "Facturas" / "2026").is_dir()


def test_mkdir_on_an_existing_folder_is_harmless(home):
    assert run(action="mkdir", path="Desktop").success


def test_move_into_a_folder(home):
    (home / "a.txt").write_text("x")
    assert run(action="move", path="a.txt", destination="escritorio").success
    assert (home / "Desktop" / "a.txt").read_text() == "x"


def test_move_never_overwrites(home):
    (home / "a.txt").write_text("new")
    (home / "Desktop" / "a.txt").write_text("old")
    assert not run(action="move", path="a.txt", destination="Desktop").success
    assert (home / "Desktop" / "a.txt").read_text() == "old"


@pytest.mark.parametrize(
    "path", ["/etc/x", "../outside", ".ssh/config", "Library/Keychains", ".openjarvis"]
)
def test_protected_paths_are_refused(home, path):
    result = run(action="mkdir", path=path)
    assert not result.success
    assert "outside" in result.content or "protected" in result.content


def test_symlink_out_of_home_is_refused(home, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (home / "link").symlink_to(outside)
    assert not run(action="mkdir", path="link/x").success
    assert not (outside / "x").exists()


def test_list_hides_dotfiles(home):
    (home / ".secret").write_text("x")
    (home / "visible.txt").write_text("x")
    result = run(action="list", path="~")
    assert "visible.txt" in result.content and ".secret" not in result.content


def test_no_delete_action_exists():
    actions = MacFilesTool().spec.parameters["properties"]["action"]["enum"]
    assert set(actions) == {"mkdir", "move", "list"}


def _call(action):
    arguments = json.dumps({"action": action, "path": "x"})
    return ToolCall(id="1", name="mac_files", arguments=arguments)


def test_changes_are_refused_after_reading_third_party_text():
    assert session_guard.check(_call("mkdir"), True) is not None
    assert session_guard.check(_call("move"), True) is not None
    assert session_guard.check(_call("list"), True) is None
    assert session_guard.check(_call("mkdir"), False) is None
