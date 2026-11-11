"""computer_task and its MCP server: limits that do not depend on the model."""

from __future__ import annotations

import json
import subprocess

import pytest

from openjarvis.agents import session_guard
from openjarvis.core.types import ToolCall
from openjarvis.tools import computer_mcp, computer_task
from openjarvis.tools.computer_mcp import ActionRefused
from openjarvis.tools.computer_task import ComputerTaskTool
from openjarvis.tools.displays import Display


def rpc(method, params=None):
    return computer_mcp.handle({"id": 1, "method": method, "params": params or {}})


def test_initialize_echoes_the_protocol_version():
    result = rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert "tools" in result["capabilities"]


def test_notifications_get_no_response():
    assert computer_mcp.handle({"method": "notifications/initialized"}) is None


def test_unknown_method_is_an_error():
    assert rpc("nope")["error"]["code"] == -32601


def test_no_shell_or_file_tool_is_exposed():
    names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
    assert names == {
        "screenshot", "click", "type_text", "key", "scroll", "open_app", "wait",
    }


def test_coordinates_map_from_image_to_screen(monkeypatch):
    monkeypatch.setattr(
        computer_mcp.displays, "active_displays",
        lambda: [Display(1, 0, 0, 1568.0 * 2, 1000.0, main=True)],
    )
    assert computer_mcp._to_screen(100, 50) == (200, 100)


def test_coordinates_reach_the_second_monitor(monkeypatch):
    laptop = Display(1, 0, 0, 1470, 956, main=True, builtin=True)
    monitor = Display(2, 1470, 0, 1920, 1080)
    monkeypatch.setattr(
        computer_mcp.displays, "active_displays", lambda: [laptop, monitor]
    )
    lay = computer_mcp._layout()
    tile = lay.tiles[1]
    assert tile.display == monitor
    x, y = computer_mcp._to_screen(tile.left + 10 * lay.scale, tile.top)
    assert (round(x), round(y)) == (1480, 0)


@pytest.mark.parametrize(
    "bundle",
    ["com.apple.mail", "company.thebrowser.Browser", "com.apple.Terminal"],
)
def test_blocked_apps_are_refused(bundle):
    with pytest.raises(ActionRefused):
        computer_mcp._check_target(bundle)


def test_other_apps_are_allowed():
    computer_mcp._check_target("com.apple.Notes")


@pytest.mark.parametrize("name", ["Mail", "Arc", "Terminal.app", "system settings"])
def test_blocked_apps_cannot_be_opened(name):
    with pytest.raises(ActionRefused):
        computer_mcp.open_app(name)


def test_open_app_cannot_smuggle_flags():
    with pytest.raises(ValueError):
        computer_mcp.open_app("-b com.apple.mail")


def test_combos_parse():
    code, flags = computer_mcp.parse_combo("cmd+shift+n")
    assert code == computer_mcp.KEY_CODES["n"]
    assert flags == computer_mcp.MODIFIERS["cmd"] | computer_mcp.MODIFIERS["shift"]


@pytest.mark.parametrize("combo", ["cmd+space", "CMD + TAB", "ctrl+space"])
def test_launcher_combos_are_refused(combo):
    with pytest.raises(ActionRefused):
        computer_mcp.parse_combo(combo)


def test_refusal_reaches_the_agent_as_a_tool_error():
    result = rpc("tools/call", {"name": "open_app", "arguments": {"name": "Mail"}})
    assert result["result"]["isError"] is True


def test_command_only_allows_the_desktop_server():
    command = computer_task.build_command("claude")
    allowed = command[command.index("--allowedTools") + 1]
    assert allowed == "mcp__jarvis-desktop__*"
    assert "--strict-mcp-config" in command
    assert "--dangerously-skip-permissions" not in command


def test_empty_task_is_refused():
    assert not ComputerTaskTool().execute(task=" ").success


def test_timeout_is_reported(monkeypatch):
    def slow(task):
        raise subprocess.TimeoutExpired("claude", 1)

    monkeypatch.setattr(computer_task, "run_task", slow)
    result = ComputerTaskTool().execute(task="x")
    assert not result.success and "stopped" in result.content


def test_it_is_refused_after_reading_third_party_text():
    call = ToolCall(id="1", name="computer_task", arguments=json.dumps({"task": "x"}))
    assert session_guard.check(call, True) is not None
    assert session_guard.check(call, False) is None


def test_its_output_taints_the_turn():
    assert session_guard.taints("computer_task")
