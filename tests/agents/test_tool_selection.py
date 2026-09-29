"""Per-turn tool selection keeps full descriptions for relevant tools only."""

from __future__ import annotations

from openjarvis.agents.tool_selection import (
    build_selected_tool_descriptions,
    select_tools,
)
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


def _tool(name: str) -> BaseTool:
    class _Stub(BaseTool):
        tool_id = name

        @property
        def spec(self) -> ToolSpec:
            return ToolSpec(
                name=name,
                description=f"Does {name} things. Long explanation follows.",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "limit": {}},
                    "required": ["query"],
                },
            )

        def execute(self, **params) -> ToolResult:
            return ToolResult(tool_name=name, content="", success=True)

    return _Stub()


TOOLS = [_tool("spotify"), _tool("get_weather"), _tool("custom_mcp_tool")]


def _names(tools):
    return [t.spec.name for t in tools]


def test_small_talk_keeps_only_untriggered_tools_in_full():
    full, brief = select_tools(TOOLS, ["hola, cómo estás?"])
    assert _names(full) == ["custom_mcp_tool"]
    assert _names(brief) == ["spotify", "get_weather"]


def test_matching_tool_gets_full_description():
    full, _ = select_tools(TOOLS, ["pon música de Coldplay"])
    assert "spotify" in _names(full)
    assert "get_weather" not in _names(full)


def test_previous_user_messages_count():
    full, _ = select_tools(TOOLS, ["y mañana?", "qué clima hace en Bogotá?"])
    assert "get_weather" in _names(full)


def test_brief_tools_are_listed_as_signatures():
    text = build_selected_tool_descriptions(TOOLS, ["hola"])
    assert "### custom_mcp_tool" in text
    assert "- spotify(query*, limit): Does spotify things." in text
    assert "Long explanation" not in text.split("### Other tools")[1]
