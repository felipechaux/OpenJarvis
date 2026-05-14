"""NativeReActAgent -- Thought-Action-Observation loop agent.

Renamed from ``ReActAgent`` to clarify this is OpenJarvis's native
implementation, not an integration with an external project.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional

from openjarvis.agents._stubs import AgentContext, AgentResult, ToolUsingAgent
from openjarvis.agents.prompt_loader import (
    load_few_shot_exemplars,
    load_system_prompt_override,
)
from openjarvis.core.events import EventBus
from openjarvis.core.registry import AgentRegistry
from openjarvis.core.types import Message, Role, ToolCall, ToolResult, _message_to_dict
from openjarvis.engine._stubs import InferenceEngine
from openjarvis.tools._stubs import BaseTool, build_tool_descriptions

REACT_SYSTEM_PROMPT = """\
You are a ReAct agent. For each step, respond with exactly one of:

1. To think and act:
Thought: <your reasoning>
Action: <tool_name>
Action Input: <json arguments>

2. To give a final answer:
Thought: <your reasoning>
Final Answer: <your answer>

# Output Format — STRICT

You communicate with tools ONLY through the literal `Action:` / `Action Input:`
lines above.  The harness parses those lines; nothing else invokes a tool.

NEVER emit any of the following — they are NOT executed and will surface as
visible garbage or, worse, as hallucinated answers to the user:

- ```` ```tool_code ```` blocks or any other fenced code block describing a
  tool call (e.g. ``` ```json ```, ``` ```python ```, ``` ```bash ```).
- Headings like `TOOL CALL`, `TOOL RESPONSE`, `## TOOL RESPONSE`.
- Python-style invocations such as `print(some_tool(...))` or
  `some_tool(query=...)`.
- English-prose announcements like "I'll use the X tool", "Let me call X",
  "Calling X with query: ...", "I'll search for ...".  These are NOT tool
  calls.  The harness ignores prose; the tool does NOT run; whatever you
  write after is a hallucination.
- Bracketed pseudo-observations like `[Knowledge search results returned…]`,
  `[Tool returned…]`, `[Search results:…]`.  You CANNOT write your own
  `Observation:` — only the harness writes one, and only after a real
  `Action:` line was parsed.
- Fabricated tool output of ANY kind.  If you have not received an
  `Observation:` line from the harness in a NEW message, you have not
  called the tool — do not invent note titles, dates, results, or content.

WRONG (this is what a hallucinating model does — do NOT do this):

    I'll check your notes for "Deuda padre" using the knowledge_search tool.
    Calling knowledge_search with query: "Deuda padre"
    [Knowledge search results returned 2 matches:]
    Note titled "Deuda padre - Financial Planning" (April 2025): ...

RIGHT — the only correct way to invoke a tool:

    Thought: I should look up the user's note titled "Deuda padre".
    Action: knowledge_search
    Action Input: {{"query": "Deuda padre", "source": "apple_notes"}}

After your `Action Input:` line you STOP generating.  The harness will then
append a real `Observation: ...` line containing the tool's actual output in
a separate message.  Only after you see that real `Observation:` do you
produce a `Final Answer:` summarising what the tool returned.  Quote the
note's text verbatim where possible — never paraphrase into invented
structure (no "Key points:", no fabricated dates).

# Using Skills

Tools whose names begin with `skill_` are SKILLS. When you call a skill tool,
the response can take one of two forms:

- **Computed result**: The skill ran a deterministic pipeline and returned a
  value (number, string, JSON, etc.). Use the value directly in your answer.

- **Procedural instructions**: The skill returned markdown text describing
  HOW to accomplish a task. Recognize this when the response starts with
  `#` headings, contains bullet lists, or uses phrases like "When asked
  to...", "First...", "Steps:". When you receive instructions:
  1. READ the instructions carefully — they are your playbook
  2. FOLLOW the steps using your OTHER tools (e.g. calculator, web_search,
     shell_exec, file_read) — not the same skill
  3. DO NOT call the same skill again — you already have its instructions
  4. Synthesize a Final Answer from what you learned

{skill_examples}{tool_descriptions}"""


@AgentRegistry.register("native_react")
class NativeReActAgent(ToolUsingAgent):
    """ReAct agent: Thought -> Action -> Observation loop."""

    agent_id = "native_react"
    _default_temperature = 0.7
    _default_max_tokens = 1024
    _default_max_turns = 10

    def __init__(
        self,
        engine: InferenceEngine,
        model: str,
        *,
        tools: Optional[List[BaseTool]] = None,
        bus: Optional[EventBus] = None,
        max_turns: Optional[int] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        interactive: bool = False,
        confirm_callback=None,
        skill_few_shot_examples: Optional[List[str]] = None,
    ) -> None:
        super().__init__(
            engine,
            model,
            tools=tools,
            bus=bus,
            max_turns=max_turns,
            temperature=temperature,
            max_tokens=max_tokens,
            interactive=interactive,
            confirm_callback=confirm_callback,
            skill_few_shot_examples=skill_few_shot_examples,
        )

    _GEMINI_TOOL_CODE_RE = re.compile(
        r"```(?:tool_code|python|json|bash)\s*\n.*?\n```",
        re.DOTALL | re.IGNORECASE,
    )
    _LEAKED_TOOL_HEADER_RE = re.compile(
        r"^\s*(?:##\s*)?TOOL (?:CALL|RESPONSE)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    # English-prose tool announcements emitted by chat-tuned models that did
    # not learn the ReAct protocol (Nemotron, some Llama instructs).  Capture
    # the tool name in group 1 and the inline argument value in group 2.
    _PROSE_TOOL_CALL_RE = re.compile(
        r"""(?:I[' ]?ll\s+(?:use|call|search\s+with|look\s+(?:this\s+)?up\s+(?:with|using))
            |Let\s+me\s+(?:use|call|search\s+(?:for|with))
            |Calling
            |Using\s+the
            |I[' ]?ll\s+now\s+call)
            \s+(?:the\s+)?
            (?:`)?([a-z_][a-z0-9_]*)(?:`)?       # group 1: tool name
            \s+(?:tool\s+)?
            (?:with\s+(?:query|input|args?)?[:\s]*)?
            (?:[\"'`]([^\"'`\n]+)[\"'`])?         # group 2: optional value
        """,
        re.IGNORECASE | re.VERBOSE,
    )
    # Bracketed pseudo-observations a hallucinating model writes for itself —
    # ``[Knowledge search results returned 2 matches:]``, ``[Tool returned …]``,
    # ``[Search results: …]``.  Everything after such a header through the next
    # blank line is fabricated and must not survive into the chat bubble.
    _FAKE_OBSERVATION_RE = re.compile(
        r"""\[\s*(?:knowledge\s+search\s+results?|tool\s+(?:returned|output|results?)
            |search\s+results?|results?\s+returned)
            [^\]]*\][^\n]*(?:\n(?!\n).+)*""",
        re.IGNORECASE | re.VERBOSE,
    )

    def _scrub_tool_artifacts(self, text: str) -> str:
        """Remove leaked ``tool_code`` blocks and ``TOOL CALL/RESPONSE`` headers.

        Acts as a last line of defence if the model produced a final answer
        that nevertheless contains protocol garbage — keeps the chat clean
        instead of pushing raw blocks into the bubble.
        """
        cleaned = self._GEMINI_TOOL_CODE_RE.sub("", text)
        cleaned = self._LEAKED_TOOL_HEADER_RE.sub("", cleaned)
        cleaned = self._FAKE_OBSERVATION_RE.sub("", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _try_recover_prose_tool_call(self, text: str, tool_names: set[str]) -> tuple[str, str]:
        """Recover a (tool, args) pair from English-prose tool announcements.

        Chat-tuned models that did not learn the ReAct protocol (Nemotron,
        some Llama instructs) emit things like ``Calling knowledge_search
        with query: "Deuda padre"`` instead of an ``Action:`` line.  We
        salvage the call so the tool actually runs and the model gets a
        real ``Observation`` next turn — much better than letting it
        hallucinate results into the final answer.
        """
        for match in self._PROSE_TOOL_CALL_RE.finditer(text):
            name = match.group(1).strip()
            if name not in tool_names:
                continue
            value = (match.group(2) or "").strip()
            if not value:
                # Try to pull the first quoted string after the announcement.
                tail = text[match.end():match.end() + 240]
                q = re.search(r"[\"'`]([^\"'`\n]+)[\"'`]", tail)
                value = q.group(1).strip() if q else ""
            import json as _json
            args = _json.dumps({"query": value}) if value else "{}"
            return name, args
        return "", ""

    def _try_recover_gemini_tool_call(self, text: str) -> tuple[str, str]:
        """Recover a (tool, args) pair from a Gemini-style ``tool_code`` block.

        Gemini occasionally emits ``print(tool_name(arg="value"))`` instead of
        the ReAct text protocol.  When the ReAct parser finds no ``Action:``
        line, this helper extracts the call so the agent can still execute the
        tool rather than dumping the raw block into the chat.
        """
        block = self._GEMINI_TOOL_CODE_RE.search(text)
        body = block.group(0) if block else text
        # Drop a leading ``print(`` wrapper so the inner call surfaces as the
        # tool name.  Repeat once to handle deeper nesting like ``print(repr(``.
        inner = re.sub(r"\bprint\s*\(", "", body, count=1)
        match = re.search(
            r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\((.*)\)",
            inner,
            re.DOTALL,
        )
        if not match:
            return "", ""
        name, args_src = match.group(1), match.group(2)
        if name in {"print", "repr", "str"}:
            return "", ""
        kwargs: dict[str, str] = {}
        for kv in re.finditer(
            r"([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
            args_src,
        ):
            kwargs[kv.group(1)] = kv.group(2) or kv.group(3) or ""
        import json as _json
        return name, _json.dumps(kwargs)

    def _parse_response(self, text: str) -> dict:
        """Parse ReAct structured output."""
        result = {"thought": "", "action": "", "action_input": "", "final_answer": ""}

        # Extract Thought
        thought_match = re.search(
            r"Thought:\s*(.+?)(?=\nAction:|\nFinal Answer:|\Z)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if thought_match:
            result["thought"] = thought_match.group(1).strip()

        # Check for Final Answer
        final_match = re.search(
            r"Final Answer:\s*(.+)", text, re.DOTALL | re.IGNORECASE
        )
        if final_match:
            result["final_answer"] = final_match.group(1).strip()
            return result

        # Extract Action and Action Input
        action_match = re.search(r"Action:\s*(.+)", text, re.IGNORECASE)
        if action_match:
            result["action"] = action_match.group(1).strip()

        input_match = re.search(
            r"Action Input:\s*(.+?)(?=\n\n|\nThought:|\Z)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if input_match:
            result["action_input"] = input_match.group(1).strip()

        # Recovery path A: model (typically Gemini) emitted a ``tool_code`` /
        # ``print(tool(...))`` block instead of ``Action:``.  Extract the call
        # so the agent actually invokes the tool rather than leaking the raw
        # block to the user as a final answer.
        if not result["action"] and self._GEMINI_TOOL_CODE_RE.search(text):
            name, args_json = self._try_recover_gemini_tool_call(text)
            if name:
                result["action"] = name
                result["action_input"] = args_json

        # Recovery path B: model narrated the call in English prose
        # (Nemotron, Llama instructs).  Validate against the actual tool set
        # so we don't fire on incidental verbs like "I'll use this approach".
        if not result["action"]:
            tool_names = {t.tool_id for t in self._tools} if self._tools else set()
            if tool_names:
                name, args_json = self._try_recover_prose_tool_call(text, tool_names)
                if name:
                    result["action"] = name
                    result["action_input"] = args_json

        return result

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        self._emit_turn_start(input)

        # Build system prompt with rich tool descriptions
        tool_desc = build_tool_descriptions(self._tools)
        # Plan 2B I3: render optimized few-shot skill examples as a section
        # before the tool descriptions. Empty string when not present.
        if self._skill_few_shot_examples:
            skill_examples_block = (
                "## Skill Examples\n\n"
                + "\n\n".join(self._skill_few_shot_examples)
                + "\n\n"
            )
        else:
            skill_examples_block = ""
        # Load configuration for prompt building
        try:
            from openjarvis.core.config import load_config
            cfg = load_config()
            persona_template = cfg.agent.system_prompt or cfg.agent.default_system_prompt
        except Exception:
            persona_template = ""

        # Use SystemPromptBuilder to assemble the full persona (SOUL, MEMORY, USER)
        # and then append the ReAct-specific instructions.
        from openjarvis.prompt.builder import SystemPromptBuilder
        builder = SystemPromptBuilder(
            agent_template=persona_template,
            skill_few_shot_examples=self._skill_few_shot_examples,
        )

        base_persona = builder.build()
        react_instructions = REACT_SYSTEM_PROMPT.format(
            tool_descriptions=tool_desc,
            skill_examples=skill_examples_block,
        )

        system_prompt = base_persona + "\n\n---\n\n" + react_instructions

        messages = self._build_messages(input, context, system_prompt=system_prompt)

        # Inject few-shot exemplars before the user input
        for ex in load_few_shot_exemplars("native_react"):
            if ex.get("input") and ex.get("output"):
                messages.insert(-1, Message(role=Role.USER, content=ex["input"]))
                messages.insert(-1, Message(role=Role.ASSISTANT, content=ex["output"]))

        all_tool_results: list[ToolResult] = []
        turns = 0
        total_usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        for _turn in range(self._max_turns):
            turns += 1

            if self._loop_guard:
                messages = self._loop_guard.compress_context(messages)

            result = self._generate(messages)
            usage = result.get("usage", {})
            for k in total_usage:
                total_usage[k] += usage.get(k, 0)

            content = result.get("content", "")
            parsed = self._parse_response(content)

            # Final answer?
            if parsed["final_answer"]:
                self._emit_turn_end(turns=turns)
                msg_dicts = [_message_to_dict(m) for m in messages]
                return AgentResult(
                    content=self._scrub_tool_artifacts(parsed["final_answer"]),
                    tool_results=all_tool_results,
                    turns=turns,
                    metadata={**total_usage, "messages": msg_dicts},
                )

            # No action? Treat content as final answer
            if not parsed["action"]:
                self._emit_turn_end(turns=turns)
                msg_dicts = [_message_to_dict(m) for m in messages]
                return AgentResult(
                    content=self._scrub_tool_artifacts(content),
                    tool_results=all_tool_results,
                    turns=turns,
                    metadata={**total_usage, "messages": msg_dicts},
                )

            # Execute action
            messages.append(Message(role=Role.ASSISTANT, content=content))

            tool_call = ToolCall(
                id=f"react_{turns}",
                name=parsed["action"],
                arguments=parsed["action_input"] or "{}",
            )

            # Loop guard check before execution
            if self._loop_guard:
                verdict = self._loop_guard.check_call(
                    tool_call.name,
                    tool_call.arguments,
                )
                if verdict.blocked:
                    tool_result = ToolResult(
                        tool_name=tool_call.name,
                        content=f"Loop guard: {verdict.reason}",
                        success=False,
                    )
                    all_tool_results.append(tool_result)
                    observation = f"Observation: {tool_result.content}"
                    messages.append(Message(role=Role.USER, content=observation))
                    continue

            tool_result = self._executor.execute(tool_call)
            all_tool_results.append(tool_result)

            observation = f"Observation: {tool_result.content}"
            messages.append(Message(role=Role.USER, content=observation))

        # Max turns exceeded
        msg_dicts = [_message_to_dict(m) for m in messages]
        return self._max_turns_result(
            all_tool_results,
            turns,
            metadata={**total_usage, "messages": msg_dicts},
        )


__all__ = ["NativeReActAgent", "REACT_SYSTEM_PROMPT"]
