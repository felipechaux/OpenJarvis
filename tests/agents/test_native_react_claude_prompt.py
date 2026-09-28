"""Claude models get the ReAct protocol without the written ``Thought:`` step."""

from __future__ import annotations

from openjarvis.agents.native_react import (
    REACT_SYSTEM_PROMPT,
    _omits_written_reasoning,
    _strip_thought_protocol,
)


def test_claude_models_omit_written_reasoning():
    assert _omits_written_reasoning("claude-cli/opus")
    assert _omits_written_reasoning("claude-sonnet-4-6")
    assert not _omits_written_reasoning("openrouter/nvidia/llama-3.3-nemotron")


def test_strip_thought_protocol_keeps_action_and_final_answer():
    prompt = _strip_thought_protocol(
        REACT_SYSTEM_PROMPT.format(tool_descriptions="", skill_examples="")
    )
    assert "Thought" not in prompt
    assert "Action Input:" in prompt
    assert "Final Answer:" in prompt
