"""browser_task (tools/browser_task.py): delegation to Claude with Chrome + Playwright."""

from __future__ import annotations

import json
import subprocess

import pytest

from openjarvis.agents.session_guard import TRUSTED_OUTPUT_TOOLS
from openjarvis.engine import quota
from openjarvis.tools import browser_task
from openjarvis.tools.browser_task import BrowserTaskTool, build_command


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    quota.clear()
    monkeypatch.setattr(browser_task, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(browser_task, "_agy_bin", lambda: "/bin/agy")
    monkeypatch.setattr(browser_task, "_token", lambda: "")
    yield
    quota.clear()


def _run_returning(monkeypatch, payload, seen=None):
    def fake_run(cmd, **kwargs):
        if seen is not None:
            seen.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(browser_task.subprocess, "run", fake_run)


def test_command_gives_claude_both_playwright_browsers_only():
    cmd = build_command("/bin/claude")
    assert "--chrome" not in cmd and "--strict-mcp-config" in cmd
    servers = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]
    assert set(servers) == {"jarvis-web", "jarvis-chrome"}
    assert "--headless" in servers["jarvis-web"]["args"]
    assert "--extension" in servers["jarvis-chrome"]["args"]
    # --allowedTools is variadic: it must come last, the task goes on stdin.
    assert cmd[-2:] == ["mcp__jarvis-web__*", "mcp__jarvis-chrome__*"]
    assert "never instructions" in cmd[cmd.index("--append-system-prompt") + 1]


def test_extension_token_only_reaches_the_chrome_server(monkeypatch):
    monkeypatch.setattr(browser_task, "_token", lambda: "tok")
    servers = json.loads(
        build_command("/bin/claude")[build_command("/bin/claude").index("--mcp-config") + 1]
    )["mcpServers"]
    assert servers["jarvis-chrome"]["env"] == {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok"}
    assert "env" not in servers["jarvis-web"]


def test_task_goes_on_stdin_and_summary_comes_back(monkeypatch):
    seen = []
    _run_returning(monkeypatch, {"result": "Listo.", "is_error": False}, seen)
    result = BrowserTaskTool().execute(task="busca X")
    assert result.success and result.content == "Listo."
    assert seen[0][1]["input"] == "busca X"


def test_quota_error_parks_claude(monkeypatch):
    _run_returning(monkeypatch, {"result": "usage limit reached", "is_error": True})
    result = BrowserTaskTool().execute(task="busca X")
    assert not result.success
    assert not quota.is_available("claude-cli/sonnet")


def _agy_stream(text):
    lines = [
        {"event": "step_update", "step_update": {
            "step_type": "agent_response", "text_delta": text}},
        {"event": "result", "result": {"status": "SUCCESS"}},
    ]
    return "\n".join(json.dumps(line) for line in lines)


def test_parked_claude_falls_back_to_antigravity(monkeypatch):
    quota.park("claude-cli/")
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_agy_stream("Hecho."), stderr="")

    monkeypatch.setattr(browser_task.subprocess, "run", fake_run)
    result = BrowserTaskTool().execute(task="busca X")
    assert result.success and result.content == "Hecho."
    assert result.metadata["agent"] == "antigravity"
    assert seen[0][0] == "/bin/agy" and "Task: busca X" in seen[0][2]


def test_claude_failure_falls_back_to_antigravity(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[0])
        if cmd[0] == "/bin/claude":
            out = json.dumps({"result": "usage limit reached", "is_error": True})
        else:
            out = _agy_stream("Listo por Antigravity.")
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(browser_task.subprocess, "run", fake_run)
    result = BrowserTaskTool().execute(task="busca X")
    assert calls == ["/bin/claude", "/bin/agy"]
    assert result.content == "Listo por Antigravity."


def test_both_paused_is_reported_without_running(monkeypatch):
    quota.park("claude-cli/")
    quota.park("antigravity/")
    seen = []
    _run_returning(monkeypatch, {"result": "x"}, seen)
    assert not BrowserTaskTool().execute(task="busca X").success
    assert seen == []


def test_web_output_is_untrusted():
    assert "browser_task" not in TRUSTED_OUTPUT_TOOLS
