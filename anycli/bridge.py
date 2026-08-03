"""Public API: Bridge.run() one-shot over any agent CLI adapter.

The bridge composes the middleware around a
:class:`~anycli.adapters.base.BaseAgentAdapter`: a global concurrency
governor with a hard per-turn lifetime, the streaming normalizer, and a
retry policy that retries only transient failures. It never imports an SDK
and never touches a subprocess.
"""

from __future__ import annotations

import asyncio
import random
import re
import warnings
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, Literal, overload

from anycli.adapters.base import BaseAgentAdapter
from anycli.doctor import check_env
from anycli.errors import AnycliError, PlanLimitReached, TransientAdapterError
from anycli.middleware.concurrency import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_TURN_SECONDS,
    ConcurrencyGovernor,
)
from anycli.middleware.streaming import normalize
from anycli.types import Chunk, Result

_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.5
_TRANSIENT_STATUS_PATTERN = re.compile(r"\b(429|529)\b")


def _is_transient(error: AnycliError) -> bool:
    """True only for failures that are safe to retry.

    :class:`PlanLimitReached` is a hard rejection and is never transient.
    """
    if isinstance(error, PlanLimitReached):
        return False
    if isinstance(error, TransientAdapterError):
        return True
    return bool(_TRANSIENT_STATUS_PATTERN.search(str(error)))


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with jitter for retry ``attempt`` (0-based)."""
    delay = _BACKOFF_BASE_SECONDS * (2**attempt)
    return delay + random.uniform(0, delay / 2)


async def _aclose(iterator: AsyncIterator[Any]) -> None:
    """Close an async iterator if it supports closure (async generators do)."""
    aclose = getattr(iterator, "aclose", None)
    if aclose is not None:
        await aclose()


class Bridge:
    """One async surface for driving a local agent CLI.

    Wraps an adapter with the CLI-agnostic middleware: a global semaphore
    caps live runs (one run = one subprocess), a hard max-lifetime kills
    hung turns, streams are normalized into typed chunks, and only
    transient failures are retried. Throughput is ultimately capped by the
    user's own plan; the bridge cannot raise that ceiling.
    """

    def __init__(
        self,
        adapter: BaseAgentAdapter,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        default_cwd: str | None = None,
        max_turn_seconds: float = DEFAULT_MAX_TURN_SECONDS,
        warn_on_auth_conflict: bool = True,
    ) -> None:
        """Compose the middleware around ``adapter``.

        When ``warn_on_auth_conflict`` is true (the default), construction
        runs :func:`anycli.doctor.check_env`, which performs a quick local
        filesystem stat of the agent CLI's credentials directory. That is
        a synchronous call: on a pathologically slow home directory it
        briefly blocks the running event loop; pass
        ``warn_on_auth_conflict=False`` to skip it.
        """
        self._adapter = adapter
        self._default_cwd = default_cwd
        self._governor = ConcurrencyGovernor(
            max_concurrency=max_concurrency,
            max_turn_seconds=max_turn_seconds,
        )
        if warn_on_auth_conflict:
            for finding in check_env():
                if finding.level == "warning":
                    warnings.warn(finding.message, UserWarning, stacklevel=2)

    @overload
    async def run(
        self,
        prompt: str,
        cwd: str | None = None,
        *,
        allowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        max_turns: int | None = None,
        stream: Literal[False] = False,
        extra_options: dict[str, Any] | None = None,
    ) -> Result: ...

    @overload
    async def run(
        self,
        prompt: str,
        cwd: str | None = None,
        *,
        allowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        max_turns: int | None = None,
        stream: Literal[True],
        extra_options: dict[str, Any] | None = None,
    ) -> AsyncGenerator[Chunk, None]: ...

    async def run(
        self,
        prompt: str,
        cwd: str | None = None,
        *,
        allowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        max_turns: int | None = None,
        stream: bool = False,
        extra_options: dict[str, Any] | None = None,
    ) -> Result | AsyncGenerator[Chunk, None]:
        """Run one one-shot turn against the agent CLI.

        With ``stream=False`` (default), consumes the whole run and returns
        the final :class:`Result`. With ``stream=True``, returns an async
        iterator of typed :class:`Chunk` objects ending with a
        :class:`Result`; iterate it to completion or close it promptly. An
        abandoned iterator holds its concurrency slot until the
        max-lifetime watchdog reaps the run (or garbage collection closes
        the iterator, whichever comes first).

        Transient failures (:class:`TransientAdapterError`, or 429/529
        signals in an adapter error) are retried up to three attempts with
        exponential backoff and jitter. :class:`PlanLimitReached` is never
        retried. A streaming run is only retried if it fails before the
        first chunk was yielded; after that, errors propagate.

        Raises ``ValueError`` immediately (a usage error, not an
        :class:`~anycli.errors.AnycliError`) when no ``cwd`` is given and
        the bridge has no ``default_cwd``. Run failures raise the typed
        hierarchy from :mod:`anycli.errors`.
        """
        resolved_cwd = cwd or self._default_cwd
        if resolved_cwd is None:
            raise ValueError("cwd is required: pass cwd= or set default_cwd on the Bridge")

        if stream:
            return self._run_stream(
                prompt,
                resolved_cwd,
                allowed_tools=allowed_tools,
                permission_mode=permission_mode,
                max_turns=max_turns,
                extra_options=extra_options,
            )

        for attempt in range(_MAX_ATTEMPTS):
            try:
                result: Result | None = None
                async for chunk in self._execute(
                    prompt,
                    resolved_cwd,
                    allowed_tools=allowed_tools,
                    permission_mode=permission_mode,
                    max_turns=max_turns,
                    extra_options=extra_options,
                ):
                    if isinstance(chunk, Result):
                        result = chunk
                assert result is not None  # normalize() guarantees a Result or raises
                return result
            except PlanLimitReached:
                raise
            except AnycliError as error:
                if attempt == _MAX_ATTEMPTS - 1 or not _is_transient(error):
                    raise
                await asyncio.sleep(_backoff_seconds(attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    async def _run_stream(
        self,
        prompt: str,
        cwd: str,
        *,
        allowed_tools: list[str] | None,
        permission_mode: str | None,
        max_turns: int | None,
        extra_options: dict[str, Any] | None,
    ) -> AsyncGenerator[Chunk, None]:
        """Streaming path: retry only before the first chunk is yielded."""
        for attempt in range(_MAX_ATTEMPTS):
            yielded = False
            executor = self._execute(
                prompt,
                cwd,
                allowed_tools=allowed_tools,
                permission_mode=permission_mode,
                max_turns=max_turns,
                extra_options=extra_options,
            )
            try:
                async for chunk in executor:
                    yielded = True
                    yield chunk
                return
            except PlanLimitReached:
                raise
            except AnycliError as error:
                if yielded or attempt == _MAX_ATTEMPTS - 1 or not _is_transient(error):
                    raise
                await asyncio.sleep(_backoff_seconds(attempt))
            finally:
                # Closing this generator must cascade to the attempt's
                # pipeline so the adapter subprocess is reaped promptly.
                await _aclose(executor)

    async def _execute(
        self,
        prompt: str,
        cwd: str,
        *,
        allowed_tools: list[str] | None,
        permission_mode: str | None,
        max_turns: int | None,
        extra_options: dict[str, Any] | None,
    ) -> AsyncIterator[Chunk]:
        """One attempt: adapter -> governor (slot + watchdog) -> normalizer.

        The governor's guard drives the adapter from a dedicated task that
        owns the concurrency slot and the max-lifetime deadline, so both
        are enforced even if this generator is abandoned mid-stream.
        """
        raw_events = self._adapter.run_once(
            prompt,
            cwd,
            allowed_tools=allowed_tools,
            permission_mode=permission_mode,
            max_turns=max_turns,
            extra_options=extra_options,
        )
        chunks = normalize(self._governor.guard(raw_events), cwd=cwd)
        try:
            async for chunk in chunks:
                yield chunk
        finally:
            # Cascade closure: normalize closes the guard, the guard
            # cancels its driver, the driver closes the adapter generator,
            # the adapter reaps its subprocess.
            await _aclose(chunks)

    def health(self) -> dict[str, int]:
        """Current concurrency state: active runs, cap, and queued waiters."""
        return {
            "active_runs": self._governor.active_runs,
            "max_concurrency": self._governor.max_concurrency,
            "queued": self._governor.queued,
        }
