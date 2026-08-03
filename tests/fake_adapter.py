"""Deterministic in-memory adapter for testing the middleware.

FakeAdapter implements the BaseAgentAdapter contract with no subprocesses,
no auth, and no network. It plays back a scripted RawEvent sequence with an
optional per-event delay, can inject a configured error at a given position,
and records concurrency and cleanup state so tests can assert on semaphore
saturation and generator reaping.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from anycli.adapters.base import BaseAgentAdapter
from anycli.types import AuthStatus, RawEvent

DEFAULT_USAGE: dict[str, int] = {
    "input_tokens": 10,
    "output_tokens": 5,
    "cache_read_tokens": 2,
    "cache_creation_tokens": 1,
}


def make_script(
    *,
    text_parts: Sequence[str] = ("hel", "lo"),
    session_id: str = "sess-1",
    result_text: str = "",
    usage: dict[str, int] | None = None,
) -> list[RawEvent]:
    """A well-formed event sequence matching the BaseAgentAdapter contract."""
    events: list[RawEvent] = [RawEvent(kind="init", data={"session_id": session_id})]
    events.extend(RawEvent(kind="text_delta", data={"text": part}) for part in text_parts)
    events.append(
        RawEvent(
            kind="result",
            data={
                "text": result_text,
                "session_id": session_id,
                "is_error": False,
                "num_turns": 1,
                "usage": dict(DEFAULT_USAGE) if usage is None else usage,
                "total_cost_usd": 0.001,
                "raw": None,
            },
        )
    )
    return events


class FakeAdapter(BaseAgentAdapter):
    """Scripted BaseAgentAdapter: deterministic, no subprocesses, no auth.

    Args:
        script: RawEvents to yield, in order. Defaults to ``make_script()``.
        delay: ``asyncio.sleep`` before each yielded event (keeps runs "live"
            long enough for concurrency assertions).
        error: exception instance to raise while streaming.
        error_at: event index at which ``error`` is raised (events before it
            are yielded first; an index past the end raises after the script).
        fail_times: how many runs (1-based, counted from the first) raise
            ``error``; ``None`` means every run raises it.
        hang: if True, never finish — sleep indefinitely instead of yielding
            the script (for max-lifetime kill tests).
    """

    name = "fake"

    def __init__(
        self,
        script: Sequence[RawEvent] | None = None,
        *,
        delay: float = 0.0,
        error: Exception | None = None,
        error_at: int = 0,
        fail_times: int | None = None,
        hang: bool = False,
    ) -> None:
        self._script: list[RawEvent] = list(make_script() if script is None else script)
        self._delay = delay
        self._error = error
        self._error_at = error_at
        self._fail_times = fail_times
        self._hang = hang

        # Instrumentation, readable by tests.
        self.attempts = 0  # how many times run_once was started
        self.active = 0  # generators currently executing
        self.max_active = 0  # peak concurrent generators (semaphore assertions)
        self.cleanup_count = 0  # how many generators ran their finally block
        self.closed_early = False  # a generator was torn down before finishing
        self.calls: list[dict[str, Any]] = []  # kwargs seen by each run

    async def check_auth(self) -> AuthStatus:
        return AuthStatus(ok=True, method="fake", detail=None)

    async def run_once(
        self,
        prompt: str,
        cwd: str,
        *,
        allowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        max_turns: int | None = None,
        extra_options: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawEvent]:
        self.attempts += 1
        run_number = self.attempts
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": cwd,
                "allowed_tools": allowed_tools,
                "permission_mode": permission_mode,
                "max_turns": max_turns,
                "extra_options": extra_options,
            }
        )
        error = self._error
        if self._fail_times is not None and run_number > self._fail_times:
            error = None
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        finished = False
        try:
            if self._hang:
                while True:  # pragma: no branch
                    await asyncio.sleep(3600)
            for index, event in enumerate(self._script):
                if error is not None and index == self._error_at:
                    raise error
                if self._delay:
                    await asyncio.sleep(self._delay)
                yield event
            if error is not None and self._error_at >= len(self._script):
                raise error
            finished = True
        finally:
            self.active -= 1
            self.cleanup_count += 1
            if not finished:
                self.closed_early = True
