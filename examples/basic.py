"""One-shot run: the smallest useful anycli program.

Runs a single tiny prompt through ``Bridge.run()`` and prints every field
of the final ``Result``. This drives a real agent CLI on your own auth and
spends tokens from your own plan, so the prompt is deliberately small and
capped at one turn.

Usage:
    uv run python examples/basic.py [cwd]
"""

from __future__ import annotations

import asyncio
import sys

from anycli import Bridge, ClaudeCodeAdapter, PlanLimitReached


async def main() -> None:
    cwd = sys.argv[1] if len(sys.argv) > 1 else "."

    bridge = Bridge(ClaudeCodeAdapter(), default_cwd=cwd)

    try:
        result = await bridge.run(
            "Reply with exactly one short sentence: what directory are you in?",
            max_turns=1,
        )
    except PlanLimitReached as error:
        # A hard plan-limit rejection is never retried; surface the reset
        # time instead of treating it as a generic failure.
        when = error.resets_at.isoformat() if error.resets_at else "unknown"
        print(f"Plan limit reached (resets at {when}): {error}")
        return

    print("--- Result ---")
    print(f"text:           {result.text!r}")
    # session_id and cwd travel together: persist them as a pair, because
    # one without the other cannot resume the session.
    print(f"session_id:     {result.session_id}")
    print(f"cwd:            {result.cwd}")
    print(f"is_error:       {result.is_error}")
    print(f"num_turns:      {result.num_turns}")
    print(f"total_cost_usd: {result.total_cost_usd}")
    if result.usage is not None:
        print(
            "usage:          "
            f"in={result.usage.input_tokens} "
            f"out={result.usage.output_tokens} "
            f"cache_read={result.usage.cache_read_tokens} "
            f"cache_creation={result.usage.cache_creation_tokens}"
        )
    else:
        print("usage:          (not reported)")


if __name__ == "__main__":
    asyncio.run(main())
