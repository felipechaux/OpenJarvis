"""Weather tool using wttr.in public API."""

from __future__ import annotations

import httpx
from typing import Any, Optional

from openjarvis.tools._stubs import BaseTool, ToolSpec
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult


@ToolRegistry.register("get_weather")
class WeatherTool(BaseTool):
    """Fetch current weather for a location using wttr.in.
    
    If the location is not provided, it defaults to the user's IP-based location.
    """

    tool_id = "get_weather"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_weather",
            description="Get current weather conditions and temperature for a given location (e.g. 'Bogotá' or 'London').",
            parameters={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The city or location to get weather for.",
                    },
                    "units": {
                        "type": "string",
                        "enum": ["metric", "imperial"],
                        "description": "Units to use (default is metric/Celsius).",
                    }
                },
                "required": [],
            },
        )

    def execute(self, **params: Any) -> ToolResult:
        """Run the weather tool."""
        location = params.get("location")
        units = params.get("units", "metric")
        
        # Use wttr.in for a zero-config weather API.
        # Format strings: %l (location), %C (conditions), %t (temperature), %h (humidity), %w (wind)
        # ?m for metric, ?u for imperial
        
        target = location if location else ""
        unit_flag = "m" if units == "metric" else "u"
        
        url = f"https://wttr.in/{target}?format=%l:+%C+%t+(%h+humidity,+%w+wind)&{unit_flag}"
        
        try:
            resp = httpx.get(url, timeout=10.0)
            resp.raise_for_status()
            result = resp.text.strip()
            if "Unknown location" in result or "404" in result:
                return ToolResult(
                    tool_name="get_weather",
                    content=f"Could not find weather for '{location}'.",
                    success=False
                )
            return ToolResult(
                tool_name="get_weather",
                content=result,
                success=True
            )
        except Exception as e:
            return ToolResult(
                tool_name="get_weather",
                content=f"Error fetching weather: {str(e)}",
                success=False
            )
