"""Agentic loop with streaming, prompt caching, and progressive-disclosure Skills.

This file is the core learning artifact of the project. The big ideas:

1. **Three cache_control markers per request** to maximize hit rate:
   - One on the last `tools` entry  → caches the tool definitions
   - One on the last `system` block → caches the skills catalog
   - One on the last prior message  → caches conversation history (turns 2+)

   Order on the wire is `tools` → `system` → `messages`. Any byte change at
   position N invalidates the cache for everything ≥ N. We sort the skills
   catalog alphabetically and never interpolate timestamps anywhere.

2. **Progressive disclosure**: the system prompt only lists skill names +
   short descriptions. The agent calls `read_skill(name)` to load the full
   SKILL.md body when a task calls for it. This keeps the cached prefix
   small even with many skills.

3. **Manual agentic loop** (rather than `messages.tool_runner`) so each
   stop_reason — `end_turn`, `tool_use`, `pause_turn`, `refusal`,
   `max_tokens` — is visible and explicit.

4. **Thinking blocks preserved verbatim**: when the assistant turn loops
   back into `messages` for the next iteration, we pass `final_message.content`
   as-is. Stripping or reordering thinking blocks (with their `signature`
   fields) breaks adaptive thinking.

5. **No `temperature`, `top_p`, `top_k`, or `budget_tokens`** — all four
   were removed on Opus 4.7 and would return a 400.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import anthropic

from .config import settings
from .skills import Skill, get_skill_body, render_skill_catalog

logger = logging.getLogger(__name__)


SYSTEM_INSTRUCTIONS = """You are LearnSkills, an AI agent built to demonstrate Anthropic's Skills pattern.

You have access to a set of **Skills** — folders containing detailed instructions for specific task domains. Below is the catalog of available skills with short descriptions.

When a user's request matches a skill's domain, **first call `read_skill(name)`** to load the full instructions for that skill, then follow them. Don't try to handle a skill's task from memory or general knowledge — the full SKILL.md often contains specific output formats, anti-patterns, and process steps you need.

You also have two server-side tools available when relevant:
- **`web_search`** — search the web for current information
- **`code_execution`** — run Python in a sandbox (pandas, numpy, matplotlib, etc.)

If no skill applies, respond directly using your general knowledge. Be concise unless the task warrants depth."""


def build_system(skills: dict[str, Skill]) -> list[dict[str, Any]]:
    """Build the system prompt as two blocks.

    Block 1: stable instructions (no cache_control — first cacheable region
    starts here, runs through block 2's marker).
    Block 2: skills catalog with cache_control. The catalog is the largest
    stable chunk in the system prompt, so caching it is the biggest win.
    """
    catalog = render_skill_catalog(skills)
    catalog_text = f"## Available skills\n\n{catalog}"
    return [
        {"type": "text", "text": SYSTEM_INSTRUCTIONS},
        {
            "type": "text",
            "text": catalog_text,
            "cache_control": {"type": "ephemeral"},
        },
    ]


def build_tools(skills: dict[str, Skill]) -> list[dict[str, Any]]:
    """Build the tools array.

    Note: the `read_skill` tool description is intentionally generic and
    stable. The skill *catalog* lives in the system prompt (see
    build_system). If we inlined the catalog here, every skill addition
    would invalidate the tool cache and bloat tool defs on every request.

    The last tool gets cache_control to cache the entire tool block.
    """
    tools: list[dict[str, Any]] = [
        {
            "type": "web_search_20260209",
            "name": "web_search",
        },
        {
            "type": "code_execution_20260120",
            "name": "code_execution",
        },
        {
            "name": "read_skill",
            "description": (
                "Read the full contents of a skill by name. Use when the user's "
                "task matches one of the skills listed in the system prompt — "
                "the full SKILL.md contains process, output format, and anti-patterns "
                "that are not in the short description."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The exact skill name from the catalog.",
                    },
                },
                "required": ["name"],
            },
            # Cache marker on the last tool — caches the entire tools block.
            "cache_control": {"type": "ephemeral"},
        },
    ]
    return tools


def apply_message_cache_control(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add `cache_control` to the last block of the last message.

    Only effective from turn 2 onward (turn 1 has no prior messages to cache).
    Returns a shallow-copied list of messages with the last message replaced;
    the original list is not mutated. The cache marker is placed inside the
    last content block of the last message — that block becomes the third
    breakpoint, caching the entire conversation history up to that point.
    """
    if not messages:
        return messages

    out = list(messages)
    last = dict(out[-1])  # shallow copy
    content = last.get("content")

    if isinstance(content, str):
        # Convert to block list so we can attach cache_control
        last["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
    elif isinstance(content, list) and content:
        new_content = [dict(b) for b in content]
        new_content[-1] = {
            **new_content[-1],
            "cache_control": {"type": "ephemeral"},
        }
        last["content"] = new_content
    else:
        # Empty content — nothing to cache.
        return out

    out[-1] = last
    return out


def _execute_read_skill(
    tool_use_block: dict[str, Any],
    skills: dict[str, Skill],
) -> dict[str, Any]:
    """Execute the `read_skill` custom tool. Returns a tool_result block."""
    tool_use_id = tool_use_block["id"]
    raw_input = tool_use_block.get("input", {}) or {}
    name = raw_input.get("name") if isinstance(raw_input, dict) else None

    try:
        body = get_skill_body(name, skills)
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": body,
        }
    except (KeyError, ValueError) as exc:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": f"Error: {exc}",
            "is_error": True,
        }


def _content_to_dict_list(content: Any) -> list[dict[str, Any]]:
    """Convert the SDK's response content (list of Pydantic models) into
    plain dicts suitable for the next request and for SQLite persistence.
    """
    out: list[dict[str, Any]] = []
    for block in content:
        if hasattr(block, "model_dump"):
            out.append(block.model_dump(exclude_none=True))
        elif isinstance(block, dict):
            out.append(block)
        else:
            # Defensive fallback — shouldn't happen with the SDK
            out.append(json.loads(json.dumps(block, default=str)))
    return out


def _is_cancelled(cancel_flag: asyncio.Event | None) -> bool:
    return cancel_flag is not None and cancel_flag.is_set()


async def run_turn(
    history: list[dict[str, Any]],
    skills: dict[str, Skill],
    cancel_flag: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run one full agent turn (potentially across multiple tool-use iterations).

    Args:
        history: Prior messages in Anthropic shape `[{role, content}, ...]`,
            ending with the new user message that triggered this turn.
        skills: Skill registry.
        cancel_flag: Optional asyncio.Event — when set, the loop will exit
            at the next iteration boundary (we don't interrupt mid-stream;
            partial output is preserved).

    Yields:
        Typed events for SSE forwarding. Event shapes:
            {"type": "text_delta", "text": str}
            {"type": "thinking_delta", "thinking": str}
            {"type": "tool_use_start", "id": str, "name": str}
            {"type": "tool_use_input_delta", "id": str, "partial_json": str}
            {"type": "tool_use_complete", "id": str, "name": str, "input": dict}
            {"type": "tool_result", "tool_use_id": str, "content": str, "is_error": bool}
            {"type": "usage", "usage": dict}
            {"type": "iteration_end", "stop_reason": str, "iteration": int}
            {"type": "turn_end", "reason": "end_turn"|"refusal"|"max_tokens"|"max_iterations"|"cancelled", "final_content": list}
            {"type": "error", "message": str, "code": str}
    """
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    messages: list[dict[str, Any]] = list(history)
    system = build_system(skills)
    tools = build_tools(skills)

    # Track the final assistant content we'll persist after the turn.
    final_assistant_content: list[dict[str, Any]] = []
    cumulative_usage: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }

    for iteration in range(settings.max_iterations):
        if _is_cancelled(cancel_flag):
            yield {
                "type": "turn_end",
                "reason": "cancelled",
                "final_content": final_assistant_content,
                "usage": cumulative_usage,
            }
            return

        # Apply cache_control to the last prior message (turns 2+ benefit).
        api_messages = apply_message_cache_control(messages)

        try:
            async with client.messages.stream(
                model=settings.model,
                max_tokens=settings.max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": settings.effort},
                system=system,
                tools=tools,
                messages=api_messages,
            ) as stream:
                async for event in stream:
                    et = getattr(event, "type", None)

                    if et == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            yield {
                                "type": "tool_use_start",
                                "id": block.id,
                                "name": block.name,
                            }

                    elif et == "content_block_delta":
                        delta = event.delta
                        dt = getattr(delta, "type", None)
                        if dt == "text_delta":
                            yield {"type": "text_delta", "text": delta.text}
                        elif dt == "thinking_delta":
                            yield {"type": "thinking_delta", "thinking": delta.thinking}
                        elif dt == "input_json_delta":
                            # Streamed tool args — the index points to the
                            # block in the response. We forward the partial
                            # JSON for UI rendering only; we never parse it
                            # ourselves (we use the final parsed input).
                            block_index = getattr(event, "index", None)
                            yield {
                                "type": "tool_use_input_delta",
                                "index": block_index,
                                "partial_json": delta.partial_json,
                            }

                    # Other events (message_start, message_delta, etc.) are
                    # ignored — we get everything we need from final_message.

                final_message = await stream.get_final_message()
        except anthropic.RateLimitError as exc:
            logger.warning("Rate limited: %s", exc)
            yield {
                "type": "error",
                "code": "rate_limit",
                "message": "Rate limited by Anthropic. Try again in a moment.",
            }
            yield {
                "type": "turn_end",
                "reason": "error",
                "final_content": final_assistant_content,
                "usage": cumulative_usage,
            }
            return
        except anthropic.APIStatusError as exc:
            logger.error("API status error %s: %s", exc.status_code, exc.message)
            yield {
                "type": "error",
                "code": f"api_status_{exc.status_code}",
                "message": f"API error ({exc.status_code}): {exc.message}",
            }
            yield {
                "type": "turn_end",
                "reason": "error",
                "final_content": final_assistant_content,
                "usage": cumulative_usage,
            }
            return
        except anthropic.APIConnectionError as exc:
            logger.error("API connection error: %s", exc)
            yield {
                "type": "error",
                "code": "connection",
                "message": "Could not reach the Anthropic API. Check your network.",
            }
            yield {
                "type": "turn_end",
                "reason": "error",
                "final_content": final_assistant_content,
                "usage": cumulative_usage,
            }
            return

        # Accumulate token usage
        usage = final_message.usage
        cumulative_usage["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
        cumulative_usage["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        cumulative_usage["cache_creation_input_tokens"] += (
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        cumulative_usage["cache_read_input_tokens"] += (
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )
        yield {"type": "usage", "usage": dict(cumulative_usage)}

        # Convert the SDK's content (list of Pydantic models) to plain dicts.
        # CRITICAL: thinking blocks include `signature` fields that must be
        # preserved verbatim when we echo this back into the next request.
        assistant_blocks = _content_to_dict_list(final_message.content)
        final_assistant_content = assistant_blocks  # latest is what we persist

        stop_reason = final_message.stop_reason
        yield {
            "type": "iteration_end",
            "stop_reason": stop_reason,
            "iteration": iteration,
        }

        # Surface fully-parsed tool_use blocks for the UI (cleaner than the
        # streamed partial_json deltas)
        for block in assistant_blocks:
            if block.get("type") == "tool_use":
                yield {
                    "type": "tool_use_complete",
                    "id": block["id"],
                    "name": block["name"],
                    "input": block.get("input", {}),
                }

        # ---------- Branch on stop_reason ----------

        if stop_reason == "end_turn":
            yield {
                "type": "turn_end",
                "reason": "end_turn",
                "final_content": assistant_blocks,
                "usage": cumulative_usage,
            }
            return

        if stop_reason == "refusal":
            yield {
                "type": "turn_end",
                "reason": "refusal",
                "final_content": assistant_blocks,
                "usage": cumulative_usage,
            }
            return

        if stop_reason == "max_tokens":
            yield {
                "type": "turn_end",
                "reason": "max_tokens",
                "final_content": assistant_blocks,
                "usage": cumulative_usage,
            }
            return

        if stop_reason == "pause_turn":
            # Server-side tool hit its iteration limit. Re-send the entire
            # messages array with the partial assistant turn appended; the
            # model resumes where it left off. No tool_result needed —
            # server-side tools handle their own results.
            messages.append({"role": "assistant", "content": assistant_blocks})
            continue

        if stop_reason == "tool_use":
            # Append the assistant turn (with thinking blocks intact) and
            # the user-side tool_results for any client-side tools.
            messages.append({"role": "assistant", "content": assistant_blocks})

            tool_results: list[dict[str, Any]] = []
            for block in assistant_blocks:
                if block.get("type") != "tool_use":
                    continue
                if block.get("name") == "read_skill":
                    result = _execute_read_skill(block, skills)
                    tool_results.append(result)
                    yield {
                        "type": "tool_result",
                        "tool_use_id": result["tool_use_id"],
                        "content": result["content"],
                        "is_error": result.get("is_error", False),
                    }
                # web_search and code_execution are server-side; the API
                # surfaces their results in the next assistant turn
                # automatically. No client-side handling needed.

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            continue

        # Unknown stop_reason — log and exit gracefully.
        logger.warning("Unknown stop_reason: %r", stop_reason)
        yield {
            "type": "turn_end",
            "reason": stop_reason or "unknown",
            "final_content": assistant_blocks,
            "usage": cumulative_usage,
        }
        return

    # Iteration cap reached without end_turn
    yield {
        "type": "error",
        "code": "max_iterations",
        "message": f"Reached max iterations ({settings.max_iterations}) without end_turn.",
    }
    yield {
        "type": "turn_end",
        "reason": "max_iterations",
        "final_content": final_assistant_content,
        "usage": cumulative_usage,
    }
