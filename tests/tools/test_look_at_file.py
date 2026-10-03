"""look_at_file (tools/look_at_file.py): text excerpts, vision, refusals, fast path."""

from __future__ import annotations

from pathlib import Path

import pytest

from openjarvis.agents.fast_paths import match_fast_path
from openjarvis.agents.session_guard import TRUSTED_OUTPUT_TOOLS
from openjarvis.core.types import ToolResult
from openjarvis.tools import look_at_file, screen
from openjarvis.tools.look_at_file import LookAtFileTool


@pytest.fixture
def seen(monkeypatch):
    """Vision calls, answered without a model."""
    calls = []

    def fake_describe(image, question, template):
        calls.append((Path(image).name, question, template))
        return "Es una foto de un gato.", "claude-cli/haiku"

    monkeypatch.setattr(look_at_file.sys, "platform", "darwin")
    monkeypatch.setattr(screen, "describe", fake_describe)
    monkeypatch.setattr(
        look_at_file, "_as_image", lambda path, tmp: (tmp / "file.jpg")
    )
    return calls


def test_text_file_comes_back_as_an_excerpt(tmp_path, seen):
    f = tmp_path / "notes.md"
    f.write_text("# Plan\nlanzar el viernes")
    result = LookAtFileTool().execute(path=str(f))
    assert result.success
    assert result.metadata["kind"] == "text"
    assert "lanzar el viernes" in result.content
    assert seen == []


def test_long_text_is_truncated(tmp_path, seen):
    f = tmp_path / "big.txt"
    f.write_text("a" * (look_at_file.MAX_TEXT_CHARS + 500))
    result = LookAtFileTool().execute(path=str(f))
    assert result.content.endswith("[…truncated]")


def test_image_goes_to_vision_with_the_file_name(tmp_path, seen):
    f = tmp_path / "gato {1}.png"
    f.write_bytes(b"\x89PNG\0fake")
    result = LookAtFileTool().execute(path=str(f), question="¿qué es?")
    assert result.success
    assert result.metadata["kind"] == "vision"
    assert result.content == "Es una foto de un gato."
    (_, question, template) = seen[0]
    assert question == "¿qué es?"
    assert "gato {1}.png" in template.format(question=question)


def test_binary_without_preview_fails(tmp_path, monkeypatch, seen):
    monkeypatch.setattr(look_at_file, "_as_image", lambda path, tmp: None)
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\0\1\2")
    result = LookAtFileTool().execute(path=str(f))
    assert not result.success


@pytest.mark.parametrize("name", [".env", "server.pem", "id_rsa"])
def test_secrets_are_refused(tmp_path, seen, name):
    f = tmp_path / name
    f.write_text("SECRET=1")
    result = LookAtFileTool().execute(path=str(f))
    assert not result.success
    assert result.metadata["reason"] == "denied"


def test_jarvis_tokens_are_refused(tmp_path, monkeypatch, seen):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    f = tmp_path / ".openjarvis" / "tokens.json"
    f.parent.mkdir()
    f.write_text("{}")
    result = LookAtFileTool().execute(path=str(f))
    assert not result.success


def test_missing_file(tmp_path):
    assert not LookAtFileTool().execute(path=str(tmp_path / "nope.txt")).success


def test_file_text_is_untrusted():
    assert "look_at_file" not in TRUSTED_OUTPUT_TOOLS


def test_notch_drop_fast_path_keeps_long_paths():
    p = "/Users/felipe/Desktop/Captura de pantalla 2026-10-03 a la(s) 10.15.22 a. m..png"
    path = match_fast_path(f"Revisa este archivo: {p}", {"look_at_file"})
    assert path is not None
    assert path.arguments == {"path": p}
    assert match_fast_path(f"Revisa este archivo: {p}", {"spotify"}) is None


def test_drop_reply_speaks_vision_and_defers_text():
    path = match_fast_path("Revisa este archivo: /tmp/a.png", {"look_at_file"})
    vision = ToolResult(tool_name="look_at_file", content="Un gato.", success=True,
                        metadata={"kind": "vision"})
    text = ToolResult(tool_name="look_at_file", content="Contents…", success=True,
                      metadata={"kind": "text"})
    assert path.reply(vision) == "Un gato."
    assert path.reply(text) is None
