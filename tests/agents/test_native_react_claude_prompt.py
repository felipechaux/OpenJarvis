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


def _agent(model, engine=None, escalation=""):
    from unittest.mock import MagicMock

    from openjarvis.agents.native_react import NativeReActAgent

    engine = engine or MagicMock()
    engine.engine_id = "mock"
    agent = NativeReActAgent(engine, model, temperature=0.2, max_tokens=64)
    agent._escalation_model = escalation
    return agent


def _response(content):
    return {
        "content": content,
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def test_claude_models_get_compact_prompt():
    prompt = _agent("claude-cli/haiku")._compose_system_prompt(
        "hola", None, allow_escalation=False
    )
    assert "Thought" not in prompt
    assert "NEVER emit" not in prompt
    assert "Action Input:" in prompt
    assert "Escalation" not in prompt


def test_escalation_section_only_when_allowed():
    agent = _agent("claude-cli/haiku", escalation="claude-cli/opus")
    assert agent._can_escalate()
    prompt = agent._compose_system_prompt("hola", None, allow_escalation=True)
    assert "Action: escalate" in prompt


def test_escalate_action_switches_model_and_rebuilds_prompt():
    from unittest.mock import MagicMock

    engine = MagicMock()
    seen = []

    def _generate(messages, *, model, **kwargs):
        seen.append((model, messages[0].content))
        if model == "claude-cli/haiku":
            return _response("Action: escalate\nAction Input: {}")
        return _response("Final Answer: análisis profundo")

    engine.generate.side_effect = _generate
    agent = _agent("claude-cli/haiku", engine, escalation="claude-cli/opus")
    result = agent.run("algo difícil")

    assert result.content == "análisis profundo"
    assert [m for m, _ in seen] == ["claude-cli/haiku", "claude-cli/opus"]
    assert "Action: escalate" in seen[0][1]
    assert "Action: escalate" not in seen[1][1]


def test_escalate_without_target_asks_for_direct_answer():
    from unittest.mock import MagicMock

    engine = MagicMock()
    engine.generate.side_effect = [
        _response("Action: escalate\nAction Input: {}"),
        _response("Final Answer: ok"),
    ]
    agent = _agent("claude-cli/opus", engine)
    assert agent.run("hola").content == "ok"
    last_messages = engine.generate.call_args.args[0]
    assert "escalation is unavailable" in last_messages[-1].content
