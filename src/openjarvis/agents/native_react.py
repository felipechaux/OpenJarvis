"""NativeReActAgent -- Thought-Action-Observation loop agent.

Renamed from ``ReActAgent`` to clarify this is OpenJarvis's native
implementation, not an integration with an external project.
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

from openjarvis.agents import session_guard
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

logger = logging.getLogger(__name__)

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

# Language

Match the user's language.  If the user writes in Spanish, your `Thought:` and
`Final Answer:` content must be in Spanish; if English, English.  Code-switch
naturally if the user does.

The protocol KEYWORDS themselves stay in English (`Thought:`, `Action:`,
`Action Input:`, `Final Answer:`) — only the prose that follows them is
translated.  The harness only parses these English literals; emitting
`Pensamiento:` / `Acción:` / `Respuesta Final:` would not be parsed and the
turn would be wasted.

# When NOT to call tools

For casual conversation — greetings ("hola", "hi", "cómo estás"), small talk,
opinions, definitions you already know, follow-ups that don't reference the
user's data — go DIRECTLY to a `Final Answer:` on turn 1.  Do NOT call
`knowledge_search` or `digest_collect` for these.  Calling tools speculatively
on casual Spanish/English input is the #1 cause of the agent looping until
max_turns and the user seeing a spinner with no response.

Only call a tool when the user's message references THEIR data (their notes,
emails, calendar, contacts) or asks something only a tool can answer
(weather, web search, calculations).

{skill_examples}{tool_descriptions}"""


# Claude follows the protocol without the long anti-hallucination catalogue
# above (written for Gemini / Nemotron), so Claude models get this version.
REACT_SYSTEM_PROMPT_COMPACT = """\
# Tool protocol

To call a tool, reply with exactly these two lines and then STOP:
Action: <tool_name>
Action Input: <json arguments>

The harness runs the tool and answers in a new message starting with
`Observation:`.  Only the harness writes observations: never invent tool
output, and never describe a tool call in prose instead of making it.

To answer, reply with:
Final Answer: <your answer>

Keep the keywords `Action:`, `Action Input:` and `Final Answer:` in English;
write the answer itself in the user's language.  Do not write out your
reasoning.

Answer greetings, small talk and general knowledge directly with a
`Final Answer:`.  Call a tool only when the request involves the user's own
data or something only a tool can do.  Tools named `skill_*` may return
instructions instead of a value: follow them with your other tools and do not
call the same skill again.

{skill_examples}{tool_descriptions}"""

ESCALATE_ACTION = "escalate"

ESCALATION_PROMPT = """\
# Escalation

You are the fast model.  If the request needs deep reasoning, careful or
long-form writing, code, detailed analysis or multi-step planning, do not
attempt it; reply with only:
Action: escalate
Action Input: {}
A stronger model then takes over the turn.  Never escalate greetings, small
talk, simple facts or simple tool requests.  Coding sessions (Claude Code,
Antigravity) do their own heavy work: starting one, relaying instructions to
it with `send_to_session`, or summarising what it did is never a reason to
escalate, even when the task itself is about code.

"""


@AgentRegistry.register("native_react")
class NativeReActAgent(ToolUsingAgent):
    """ReAct agent: Thought -> Action -> Observation loop."""

    agent_id = "native_react"
    # The server skips its own persona injection for agents that build one.
    builds_own_persona = True
    # Set per request by the tiered router; "" disables escalation.
    _escalation_model = ""
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
        """Parse ReAct structured output.

        Accepts the canonical English labels (the prompt explicitly tells the
        model to keep these in English) and their Spanish equivalents as a
        defensive fallback — without this, a Spanish user query like "hola"
        could push the model into emitting "Respuesta Final: Hola, ¿en qué
        puedo ayudarle?" which the parser would not recognize, sending the
        agent into a max_turns loop that the user perceives as a hang.
        """
        result = {"thought": "", "action": "", "action_input": "", "final_answer": ""}

        # Extract Thought / Pensamiento
        thought_match = re.search(
            r"(?:Thought|Pensamiento):\s*(.+?)(?=\n(?:Action|Acción)|\n(?:Final Answer|Respuesta Final):|\Z)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if thought_match:
            result["thought"] = thought_match.group(1).strip()

        # Check for Final Answer / Respuesta Final
        final_match = re.search(
            r"(?:Final Answer|Respuesta Final):\s*(.+)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if final_match:
            result["final_answer"] = final_match.group(1).strip()
            return result

        # Extract Action / Acción and Action Input / Entrada de Acción
        action_match = re.search(
            r"(?:Action|Acción):\s*(.+)", text, re.IGNORECASE
        )
        if action_match:
            result["action"] = action_match.group(1).strip()

        input_match = re.search(
            r"(?:Action Input|Entrada de Acción):\s*(.+?)(?=\n\n|\n(?:Thought|Pensamiento):|\Z)",
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

    def _can_escalate(self) -> bool:
        target = self._escalation_model
        return bool(target) and target != self._model

    def _compose_system_prompt(
        self,
        input: str,
        context: Optional[AgentContext],
        *,
        allow_escalation: bool,
    ) -> str:
        """Persona (SOUL, MEMORY, USER) followed by the ReAct instructions."""
        try:
            from openjarvis.core.config import load_config
            cfg = load_config()
        except Exception:
            cfg = None

        persona_template = ""
        dynamic_tools = True
        if cfg is not None:
            persona_template = (
                cfg.agent.system_prompt or cfg.agent.default_system_prompt
            )
            dynamic_tools = cfg.agent.dynamic_tools
            # SOUL.md already carries the full persona; the config default
            # is a shorter copy of it, so sending both only burns tokens.
            if not cfg.agent.system_prompt and _has_soul(cfg):
                persona_template = ""

        from openjarvis.prompt.builder import SystemPromptBuilder
        builder = SystemPromptBuilder(
            agent_template=persona_template,
            skill_few_shot_examples=self._skill_few_shot_examples,
        )
        base_persona = builder.build().strip()

        if dynamic_tools:
            from openjarvis.agents.tool_selection import (
                build_selected_tool_descriptions,
            )
            tool_desc = build_selected_tool_descriptions(
                self._tools, _recent_user_texts(input, context)
            )
        else:
            tool_desc = build_tool_descriptions(self._tools)

        # Plan 2B I3: render optimized few-shot skill examples as a section
        # before the tool descriptions. Empty string when not present.
        examples_block = ESCALATION_PROMPT if allow_escalation else ""
        if self._skill_few_shot_examples:
            examples_block += (
                "## Skill Examples\n\n"
                + "\n\n".join(self._skill_few_shot_examples)
                + "\n\n"
            )

        compact = _uses_compact_prompt(self._model)
        template = REACT_SYSTEM_PROMPT_COMPACT if compact else REACT_SYSTEM_PROMPT
        react_instructions = template.format(
            tool_descriptions=tool_desc,
            skill_examples=examples_block,
        )
        if _omits_written_reasoning(self._model) and not compact:
            react_instructions = _strip_thought_protocol(react_instructions)
        return base_persona + "\n\n---\n\n" + react_instructions

    def _try_fast_path(self, input: str) -> Optional[AgentResult]:
        """Handle simple commands with one tool call and no model call."""
        try:
            from openjarvis.core.config import load_config
            if not load_config().agent.fast_paths:
                return None
        except Exception:
            pass
        from openjarvis.agents.fast_paths import match_fast_path

        path = match_fast_path(input, {t.spec.name for t in self._tools})
        if path is None:
            return None
        import json as _json

        tool_result = self._executor.execute(
            ToolCall(
                id="fast_1", name=path.tool, arguments=_json.dumps(path.arguments)
            )
        )
        reply = path.reply(tool_result)
        if reply is None:
            # Failed or unexpected output: let the model explain it.  The
            # failed call is not retried here; the model decides.
            return None
        logger.info("native_react: fast path %s(%s)", path.tool, path.arguments)
        self._emit_turn_end(turns=0)
        return AgentResult(
            content=reply,
            tool_results=[tool_result],
            turns=0,
            metadata={
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "fast_path": path.tool,
            },
        )

    def _escalate(
        self,
        messages: list[Message],
        input: str,
        context: Optional[AgentContext],
    ) -> None:
        """Hand the turn to the escalation model and rebuild its prompt."""
        logger.info(
            "native_react: %s escalated to %s", self._model, self._escalation_model
        )
        self._switch_model(messages, input, context, self._escalation_model)

    def _fallback_model(self) -> str:
        """The configured backup model, or "" when there is none to try."""
        try:
            from openjarvis.core.config import load_config
            fallback = load_config().intelligence.fallback_model
        except Exception:
            return ""
        return fallback if fallback and fallback != self._model else ""

    def _switch_model(
        self,
        messages: list[Message],
        input: str,
        context: Optional[AgentContext],
        model: str,
    ) -> None:
        """Continue the turn on *model*, with the system prompt rebuilt for it."""
        self._model = model
        self._escalation_model = ""
        system_prompt = self._compose_system_prompt(
            input, context, allow_escalation=False
        )
        if messages and messages[0].role == Role.SYSTEM:
            messages[0] = Message(role=Role.SYSTEM, content=system_prompt)
        else:
            messages.insert(0, Message(role=Role.SYSTEM, content=system_prompt))

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        self._emit_turn_start(input)

        fast = self._try_fast_path(input)
        if fast is not None:
            return fast

        self._reset_loop_guard()

        system_prompt = self._compose_system_prompt(
            input, context, allow_escalation=self._can_escalate()
        )
        omit_thoughts = _omits_written_reasoning(self._model)

        messages = self._build_messages(input, context, system_prompt=system_prompt)

        # Inject few-shot exemplars before the user input
        for ex in load_few_shot_exemplars("native_react"):
            if ex.get("input") and ex.get("output"):
                messages.insert(-1, Message(role=Role.USER, content=ex["input"]))
                output = ex["output"]
                if omit_thoughts:
                    output = _strip_thought_lines(output)
                messages.insert(-1, Message(role=Role.ASSISTANT, content=output))

        all_tool_results: list[ToolResult] = []
        turns = 0
        fallback_used = False
        # Session guard: once third-party text is in play, this turn may not
        # steer a coding session (see agents/session_guard.py).
        tainted = session_guard.has_injected_context(messages)
        total_usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        for _turn in range(self._max_turns):
            turns += 1

            if self._loop_guard:
                messages = self._loop_guard.compress_context(messages)

            try:
                result = self._generate(messages)
            except Exception as exc:
                # The primary provider is down or out of quota: finish the
                # turn on the backup model (another provider) rather than
                # failing it.  Only once per turn.
                fallback = "" if fallback_used else self._fallback_model()
                if not fallback:
                    raise
                logger.warning(
                    "native_react: %s failed (%s); falling back to %s",
                    self._model, str(exc)[:200], fallback,
                )
                fallback_used = True
                self._switch_model(messages, input, context, fallback)
                continue
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

            if parsed["action"] == ESCALATE_ACTION:
                if self._can_escalate():
                    self._escalate(messages, input, context)
                else:
                    messages.append(Message(role=Role.ASSISTANT, content=content))
                    messages.append(Message(
                        role=Role.USER,
                        content="Observation: escalation is unavailable; "
                        "answer directly.",
                    ))
                continue

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

            tool_result = session_guard.check(tool_call, tainted)
            if tool_result is not None:
                logger.warning(
                    "native_react: session guard blocked %s(%s)",
                    tool_call.name, tool_call.arguments[:200],
                )
            else:
                tool_result = self._executor.execute(tool_call)
                if session_guard.taints(tool_call.name):
                    tainted = True
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



# Claude models (Opus 5.5 in particular) have safeguards that refuse requests
# asking them to write out their internal reasoning ("[reasoning_extraction]"),
# which the ``Thought:`` protocol line does.  The parser treats ``Thought:`` as
# optional, so for those models we simply don't ask for it.
_WRITTEN_REASONING_UNSUPPORTED_PREFIXES = ("claude-cli/", "claude-")

_THOUGHT_LINE_RE = re.compile(r"^[ \t]*Thought:.*\n?", re.MULTILINE)


def _uses_compact_prompt(model: str) -> bool:
    return (model or "").startswith(_WRITTEN_REASONING_UNSUPPORTED_PREFIXES)


def _has_soul(cfg: Any) -> bool:
    from pathlib import Path

    try:
        path = Path(cfg.memory_files.soul_path).expanduser()
        return path.is_file() and bool(path.read_text().strip())
    except (AttributeError, OSError):
        return False


def _recent_user_texts(
    input: str, context: Optional[AgentContext], limit: int = 2
) -> list[str]:
    """The current input plus the last *limit* user messages of the context."""
    texts = [input]
    if context is not None:
        prior = [
            m.content or ""
            for m in context.conversation.messages
            if m.role == Role.USER
        ]
        texts.extend(prior[-limit:])
    return texts


def _omits_written_reasoning(model: str) -> bool:
    return (model or "").startswith(_WRITTEN_REASONING_UNSUPPORTED_PREFIXES)


def _strip_thought_lines(text: str) -> str:
    return _THOUGHT_LINE_RE.sub("", text)


def _strip_thought_protocol(prompt: str) -> str:
    """Rewrite the ReAct instructions without the ``Thought:`` step."""
    prompt = _strip_thought_lines(prompt)
    prompt = prompt.replace(
        "Match the user's language.  If the user writes in Spanish, "
        "your `Thought:` and\n"
        "`Final Answer:` content must be in Spanish",
        "Match the user's language.  If the user writes in Spanish, your\n"
        "`Final Answer:` content must be in Spanish",
    )
    prompt = prompt.replace("(`Thought:`, `Action:`,", "(`Action:`,")
    return prompt + (
        "\n\nDo not write out your reasoning.  Reply with only the `Action:` / "
        "`Action Input:` lines or a `Final Answer:` line."
    )

__all__ = ["NativeReActAgent", "REACT_SYSTEM_PROMPT", "REACT_SYSTEM_PROMPT_COMPACT"]
