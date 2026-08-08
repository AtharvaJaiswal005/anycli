"""Concurrent runs through one Bridge: the semaphore in action.

One ``Bridge`` holds a global semaphore capping live agent CLI
subprocesses (one run = one subprocess). This example fires several tiny
prompts at once with ``max_concurrency=2``, so at most two subprocesses
are ever alive while the rest queue. A watcher task prints ``health()``
while the runs are in flight so you can see ``active_runs`` respect the
cap and ``queued`` drain.

Every run here spends tokens from your own plan; the prompts are tiny and
capped at one turn, but this example still performs several real runs —
throughput is capped by your plan, not by anycli.

Usage:
    uv run python examples/concurrent.py [cwd]
"""

from __future__ import annotations

import asyncio
import sys

from anycli import Bridge, ClaudeCodeAdapter, PlanLimitReached

PROMPTS = [
    "Reply with exactly the word: one",
    "Reply with exactly the word: two",
    "Reply with exactly the word: three",
    "Reply with exactly the word: four",
]


async def run_one(bridge: Bridge, index: int, prompt: str) -> None:
    try:
        result = await bridge.run(prompt, max_turns=1)
        print(f"[run {index}] done: {result.text!r} (session {result.session_id})")
    except PlanLimitReached as error:
        when = error.resets_at.isoformat() if error.resets_at else "unknown"
        print(f"[run {index}] plan limit reached, resets at {when}")


async def watch_health(bridge: Bridge, stop: asyncio.Event) -> None:
    while not stop.is_set():
        print(f"[health] {bridge.health()}")
        try:
            # asyncio.TimeoutError, not the builtin, for Python 3.10 compat.
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            continue


async def main() -> None:
    cwd = sys.argv[1] if len(sys.argv) > 1 else "."

    # Cap live subprocesses at 2: with four prompts, two run and two queue.
    bridge = Bridge(ClaudeCodeAdapter(), default_cwd=cwd, max_concurrency=2)

    stop = asyncio.Event()
    watcher = asyncio.create_task(watch_health(bridge, stop))
    try:
        await asyncio.gather(*(run_one(bridge, i, prompt) for i, prompt in enumerate(PROMPTS)))
    finally:
        stop.set()
        await watcher

    print(f"[health] final: {bridge.health()}")


if __name__ == "__main__":
    asyncio.run(main())
