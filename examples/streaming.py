"""Streaming run: render typed chunks as they arrive.

With ``stream=True``, ``Bridge.run()`` returns an async iterator of typed
chunks — never raw strings. This example renders each chunk type
distinctly:

- ``TextDelta``  — assistant text, printed as it streams.
- ``ToolUse``    — the agent invoked a tool.
- ``ToolResult`` — the outcome of that tool call, matched by id.
- ``Result``     — the terminal chunk with session and cost metadata.

This drives a real agent CLI on your own auth; the prompt is small, and
``max_turns=3`` leaves room for the tool round (one turn is not enough
for tool use plus the final reply — the run would end in an
``error_max_turns`` result).

Usage:
    uv run python examples/streaming.py [cwd]
"""

from __future__ import annotations

import asyncio
import sys

from anycli import (
    Bridge,
    ClaudeCodeAdapter,
    PlanLimitReached,
    Result,
    TextDelta,
    ToolResult,
    ToolUse,
)


async def main() -> None:
    cwd = sys.argv[1] if len(sys.argv) > 1 else "."

    bridge = Bridge(ClaudeCodeAdapter(), default_cwd=cwd)

    try:
        stream = await bridge.run(
            "List the files in the current directory, then say done.",
            # A tool round takes more than one turn; 3 covers
            # tool use -> tool result -> final reply.
            max_turns=3,
            stream=True,
        )
        async for chunk in stream:
            if isinstance(chunk, TextDelta):
                # Assistant text streams in fragments; print without newlines.
                print(chunk.text, end="", flush=True)
            elif isinstance(chunk, ToolUse):
                print(f"\n[tool use]    {chunk.tool_name} {chunk.tool_input}")
            elif isinstance(chunk, ToolResult):
                status = "error" if chunk.is_error else "ok"
                print(f"[tool result] ({status}) id={chunk.tool_use_id}")
            elif isinstance(chunk, Result):
                print("\n--- Result ---")
                print(f"session_id={chunk.session_id} cwd={chunk.cwd}")
                print(f"turns={chunk.num_turns} cost_usd={chunk.total_cost_usd}")
    except PlanLimitReached as error:
        # Hard rejection, never retried: tell the user when it resets.
        when = error.resets_at.isoformat() if error.resets_at else "unknown"
        print(f"\nPlan limit reached (resets at {when}): {error}")


if __name__ == "__main__":
    asyncio.run(main())
