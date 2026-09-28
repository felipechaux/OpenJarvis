"""Tests for the coding_sessions tool (following Claude Code / Gemini CLI sessions)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

from openjarvis.tools import coding_sessions as cs

CWD = "/Users/me/Developer/AI/openjarvis/repo"


def _claude(
    tmp_path: Path, entries: list, age_s: float = 0.0, sid: str = "sess-1"
) -> Path:
    folder = tmp_path / "claude" / cs._claude_slug(CWD)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_text(
        "\n".join(json.dumps({"cwd": CWD, "sessionId": sid, **e}) for e in entries)
        + "\n"
    )
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(*blocks, stop="end_turn"):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": list(blocks), "stop_reason": stop},
    }


def _tool_use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _tool_result(tid):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tid}],
        },
    }


class TestClaudeParsing:
    def test_finished_turn_is_idle(self, tmp_path: Path) -> None:
        p = _claude(
            tmp_path,
            [
                _user("arregla los tests"),
                _assistant(
                    _tool_use("t1", "Edit", file_path="/x/a.py"), stop="tool_use"
                ),
                _tool_result("t1"),
                _assistant({"type": "text", "text": "Listo, tests arreglados."}),
            ],
        )
        s = cs.parse_claude_session(p)
        assert s.state == cs.IDLE
        assert s.last_prompt == "arregla los tests"
        assert s.last_reply == "Listo, tests arreglados."
        assert s.files_edited == ["/x/a.py"]
        assert s.project == "openjarvis"

    def test_unanswered_tool_goes_quiet_is_permission(self, tmp_path: Path) -> None:
        p = _claude(
            tmp_path,
            [
                _user("borra build"),
                _assistant(
                    _tool_use("t1", "Bash", command="rm -rf build"), stop="tool_use"
                ),
            ],
            age_s=60,
        )
        s = cs.parse_claude_session(p)
        assert s.state == cs.WAITING_PERMISSION
        assert s.pending_tool == "Bash: rm -rf build"

    def test_fresh_tool_call_is_working(self, tmp_path: Path) -> None:
        p = _claude(
            tmp_path,
            [
                _user("corre los tests"),
                _assistant(_tool_use("t1", "Bash", command="pytest"), stop="tool_use"),
            ],
        )
        assert cs.parse_claude_session(p).state == cs.WORKING

    def test_user_turn_without_reply(self, tmp_path: Path) -> None:
        assert (
            cs.parse_claude_session(_claude(tmp_path, [_user("hola")])).state
            == cs.WORKING
        )
        p = _claude(tmp_path, [_user("hola")], age_s=3600, sid="old")
        assert cs.parse_claude_session(p).state == cs.STALLED

    def test_interrupt_clears_pending(self, tmp_path: Path) -> None:
        p = _claude(
            tmp_path,
            [
                _user("x"),
                _assistant(
                    _tool_use("t1", "Bash", command="sleep 100"), stop="tool_use"
                ),
                _user("[Request interrupted by user for tool use]"),
            ],
            age_s=60,
        )
        s = cs.parse_claude_session(p)
        assert s.state == cs.IDLE
        assert s.pending_tool == ""

    def test_meta_and_command_entries_are_not_prompts(self, tmp_path: Path) -> None:
        p = _claude(
            tmp_path,
            [
                _user("pregunta real"),
                {**_user("<command-name>/clear</command-name>")},
                {**_user("caveat text"), "isMeta": True},
                _assistant({"type": "text", "text": "ok"}),
            ],
        )
        assert cs.parse_claude_session(p).last_prompt == "pregunta real"


class TestGeminiParsing:
    def _chat(self, tmp_path: Path, messages: list) -> Path:
        p = tmp_path / "session-2026-09-28T10-00-abc.json"
        p.write_text(json.dumps({"sessionId": "abc", "messages": messages}))
        return p

    def test_reply_is_idle(self, tmp_path: Path) -> None:
        p = self._chat(
            tmp_path,
            [
                {"type": "user", "content": [{"text": "optimiza la consulta"}]},
                {
                    "type": "gemini",
                    "content": "Hecho.",
                    "toolCalls": [
                        {
                            "name": "replace",
                            "args": {"file_path": "/x/q.sql"},
                            "status": "success",
                        }
                    ],
                },
            ],
        )
        s = cs.parse_gemini_session(p, CWD)
        assert s.state == cs.IDLE
        assert s.last_prompt == "optimiza la consulta"
        assert s.files_edited == ["/x/q.sql"]

    def test_pending_tool_call(self, tmp_path: Path) -> None:
        p = self._chat(
            tmp_path,
            [
                {"type": "user", "content": "x"},
                {
                    "type": "gemini",
                    "content": "",
                    "toolCalls": [
                        {
                            "name": "run_shell_command",
                            "args": {"command": "npm test"},
                            "status": "awaiting_approval",
                        }
                    ],
                },
            ],
        )
        t = time.time() - 60
        os.utime(p, (t, t))
        s = cs.parse_gemini_session(p, CWD)
        assert s.state == cs.WAITING_PERMISSION
        assert s.pending_tool == "run_shell_command: npm test"

    def test_info_only_session_is_skipped(self, tmp_path: Path) -> None:
        p = self._chat(
            tmp_path, [{"type": "info", "content": "Authentication succeeded"}]
        )
        assert cs.parse_gemini_session(p, CWD) is None


class TestFindAndTool:
    def test_newest_session_per_folder_is_open(self, tmp_path: Path) -> None:
        _claude(
            tmp_path,
            [_user("old"), _assistant({"type": "text", "text": "a"})],
            age_s=600,
            sid="old",
        )
        _claude(
            tmp_path,
            [_user("new"), _assistant({"type": "text", "text": "b"})],
            sid="new",
        )
        with (
            patch.object(cs, "CLAUDE_DIR", tmp_path / "claude"),
            patch.object(cs, "GEMINI_DIR", tmp_path / "nogemini"),
            patch.object(
                cs, "_running_cwds", return_value={"claude": {CWD}, "gemini": set()}
            ),
        ):
            sessions = cs.find_sessions(hours=1)
        assert [s.session_id for s in sessions] == ["new", "old"]
        assert [s.open for s in sessions] == [True, False]

    def test_filter_by_project_matches_parent_of_repo(self, tmp_path: Path) -> None:
        s = cs.parse_claude_session(_claude(tmp_path, [_user("x")]))
        assert cs.filter_sessions([s], project="openjarvis") == [s]
        assert cs.filter_sessions([s], project="dadomatch") == []
        assert cs.filter_sessions([s], cli="gemini") == []

    def test_tool_status_and_empty(self, tmp_path: Path) -> None:
        _claude(
            tmp_path, [_user("arregla"), _assistant({"type": "text", "text": "Listo."})]
        )
        with (
            patch.object(cs, "CLAUDE_DIR", tmp_path / "claude"),
            patch.object(cs, "GEMINI_DIR", tmp_path / "nogemini"),
            patch.object(
                cs, "_running_cwds", return_value={"claude": set(), "gemini": set()}
            ),
        ):
            tool = cs.CodingSessionsTool()
            res = tool.execute(action="status", project="openjarvis")
            assert res.success
            assert "Claude Code in openjarvis" in res.content
            assert "Last reply: Listo." in res.content
            assert "No Claude Code" in tool.execute(project="nada").content
