"""Tiny CLI harness for the agent loop.

Usage:
    python -m backend.agent_cli "Review this Python: def f(x): return x+1"

Prints raw events to stdout — useful for verifying the agentic loop and
inspecting cache_read_input_tokens across runs.
"""

from __future__ import annotations

import asyncio
import json
import sys

from . import agent
from .skills import load_skills
from .config import settings


async def main(prompt: str) -> None:
    skills = load_skills(settings.skills_dir)
    history = [{"role": "user", "content": prompt}]

    async for event in agent.run_turn(history, skills):
        # Render only the noteworthy bits to keep stdout readable
        et = event["type"]
        if et == "text_delta":
            sys.stdout.write(event["text"])
            sys.stdout.flush()
        elif et == "thinking_delta":
            sys.stdout.write(event["thinking"])
            sys.stdout.flush()
        elif et == "tool_use_start":
            print(f"\n[tool_use_start] {event['name']} (id={event['id']})")
        elif et == "tool_use_complete":
            print(
                f"[tool_use_complete] {event['name']} input={json.dumps(event['input'])[:120]}"
            )
        elif et == "tool_result":
            preview = event["content"][:200].replace("\n", " ")
            tag = "tool_result_error" if event.get("is_error") else "tool_result"
            print(f"\n[{tag}] {preview}…")
        elif et == "iteration_end":
            print(f"\n[iteration_end] stop_reason={event['stop_reason']}")
        elif et == "usage":
            u = event["usage"]
            total_input = u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]
            cache_pct = (u["cache_read_input_tokens"] / total_input * 100) if total_input else 0
            print(
                f"[usage] input={u['input_tokens']} output={u['output_tokens']} "
                f"cache_create={u['cache_creation_input_tokens']} "
                f"cache_read={u['cache_read_input_tokens']} ({cache_pct:.0f}% cache hit)"
            )
        elif et == "turn_end":
            print(f"\n[turn_end] reason={event['reason']}")
        elif et == "error":
            print(f"\n[error] {event['code']}: {event['message']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m backend.agent_cli <prompt>", file=sys.stderr)
        sys.exit(1)
    asyncio.run(main(" ".join(sys.argv[1:])))
