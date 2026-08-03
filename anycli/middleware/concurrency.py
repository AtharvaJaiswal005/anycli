"""Global concurrency control for live agent CLI runs.

The governor enforces two invariants: a global semaphore caps how many
runs (and therefore agent CLI subprocesses) are live at once, and a hard
max-lifetime watchdog bounds every turn. Each guarded run is driven by a
dedicated task that owns its concurrency slot and its deadline, so both
are enforced even when the consumer stops iterating the stream without
closing it. The governor never imports an SDK and never touches a
subprocess — it only gates and wraps event streams produced by a
:class:`~anycli.adapters.base.BaseAgentAdapter`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, Final

from anycli.errors import AdapterError
from anycli.types import RawEvent

DEFAULT_MAX_CONCURRENCY = 3
DEFAULT_MAX_TURN_SECONDS = 1800.0

#: Grace given to adapter cleanup (generator unwinding, subprocess teardown)
#: at each step before the rest is left to a background task. This bounds how
#: long a turn can outlive its deadline: a stuck adapter cleanup keeps running
#: in the background instead of pinning the turn and its concurrency slot.
CLEANUP_GRACE_SECONDS: Final = 10.0

_END_OF_STREAM: Final = object()

# Strong references to background cleanup tasks so they are not garbage
# collected before finishing (the event loop keeps only weak references).
_background_tasks: set[asyncio.Task[None]] = set()


async def _aclose_quietly(events: AsyncIterator[RawEvent]) -> None:
    """Close ``events`` if closable; a cleanup failure must not mask outcomes."""
    aclose = getattr(events, "aclose", None)
    if aclose is None:
        return
    # Deliberately broad: this is last-resort cleanup and the run's outcome
    # was already decided before it started.
    with suppress(Exception):
        await aclose()


def _close_in_background(events: AsyncIterator[RawEvent]) -> asyncio.Task[None]:
    """Spawn a task closing ``events`` and keep it referenced until done."""
    task = asyncio.get_running_loop().create_task(_aclose_quietly(events))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


class ConcurrencyGovernor:
    """Caps simultaneous runs and enforces a hard per-turn lifetime.

    One slot in the governor corresponds to one live agent CLI subprocess,
    so the cap should be sized to host memory. The default is deliberately
    conservative.
    """

    def __init__(
        self,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_turn_seconds: float = DEFAULT_MAX_TURN_SECONDS,
    ) -> None:
        """Validate and store limits; raises ``ValueError`` on nonsensical values."""
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency must be >= 1, got {max_concurrency}")
        if max_turn_seconds <= 0:
            raise ValueError(f"max_turn_seconds must be > 0, got {max_turn_seconds}")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_concurrency = max_concurrency
        self._max_turn_seconds = max_turn_seconds
        self._active = 0
        self._queued = 0

    @property
    def max_concurrency(self) -> int:
        """Configured cap on simultaneous runs."""
        return self._max_concurrency

    @property
    def max_turn_seconds(self) -> float:
        """Hard wall-clock lifetime allowed for a single turn."""
        return self._max_turn_seconds

    @property
    def active_runs(self) -> int:
        """Number of runs currently holding a slot."""
        return self._active

    @property
    def queued(self) -> int:
        """Number of runs currently waiting for a slot."""
        return self._queued

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """Hold one concurrency slot for the duration of the ``with`` body."""
        self._queued += 1
        try:
            await self._semaphore.acquire()
        finally:
            self._queued -= 1
        self._active += 1
        try:
            yield
        finally:
            self._active -= 1
            self._semaphore.release()

    async def guard(self, events: AsyncIterator[RawEvent]) -> AsyncIterator[RawEvent]:
        """Run ``events`` under the concurrency cap and max-lifetime deadline.

        A dedicated driver task pulls events from the adapter and owns both
        the concurrency slot and the deadline, so a consumer that stops
        iterating — with or without closing this generator — cannot stall
        enforcement: the watchdog still fires at the deadline, the adapter
        generator is still closed (which obliges the adapter to terminate
        its subprocess), and the slot is still released. Adapter cleanup is
        given :data:`CLEANUP_GRACE_SECONDS` per step to finish before being
        left to complete in a background task.

        On deadline expiry :class:`AdapterError` is raised to the consumer.
        Closing this generator cancels the driver, which performs the same
        bounded cleanup.
        """
        queue: asyncio.Queue[Any] = asyncio.Queue()
        pace = asyncio.Semaphore(1)
        driver = asyncio.get_running_loop().create_task(self._drive(events, queue, pace))
        try:
            while True:
                item = await queue.get()
                if item is _END_OF_STREAM:
                    return
                if isinstance(item, BaseException):
                    raise item
                try:
                    yield item
                finally:
                    # Let the driver pull the next event only once the
                    # consumer has taken this one, so the adapter cannot
                    # run arbitrarily far ahead of the consumer.
                    pace.release()
        finally:
            if not driver.done():
                driver.cancel()
            # The driver's own cleanup is bounded, so this cannot hang.
            await asyncio.wait({driver})

    async def _drive(
        self,
        events: AsyncIterator[RawEvent],
        queue: asyncio.Queue[Any],
        pace: asyncio.Semaphore,
    ) -> None:
        """Driver task: slot -> pump under deadline -> bounded cleanup -> signal."""
        outcome: Any = _END_OF_STREAM
        signal = True
        try:
            async with self.acquire():
                pump = asyncio.get_running_loop().create_task(self._pump(events, queue, pace))
                try:
                    done, _pending = await asyncio.wait({pump}, timeout=self._max_turn_seconds)
                    if pump in done:
                        error = pump.exception()
                        if error is not None:
                            outcome = error
                    else:
                        outcome = AdapterError(
                            f"turn exceeded max lifetime of {self._max_turn_seconds:g}s"
                        )
                except asyncio.CancelledError:
                    signal = False  # the consumer is gone; nobody is listening
                    raise
                finally:
                    pump.cancel()
                    await self._reap(pump, events)
        except asyncio.CancelledError:
            signal = False
            raise
        except BaseException as error:  # noqa: BLE001 - forwarded to the consumer
            # Anything the pipeline raised (slot acquisition failure, reap
            # failure) is relayed to the consumer, not swallowed here.
            outcome = error
        finally:
            if signal:
                queue.put_nowait(outcome)

    @staticmethod
    async def _pump(
        events: AsyncIterator[RawEvent],
        queue: asyncio.Queue[Any],
        pace: asyncio.Semaphore,
    ) -> None:
        """Forward adapter events into the queue, paced by consumer demand."""
        async for event in events:
            await pace.acquire()
            queue.put_nowait(event)

    @staticmethod
    async def _reap(pump: asyncio.Task[None], events: AsyncIterator[RawEvent]) -> None:
        """Bounded cleanup of the pump task and the adapter generator.

        Waits up to :data:`CLEANUP_GRACE_SECONDS` at each step; cleanup that
        takes longer keeps running in a background task so a stuck adapter
        teardown cannot pin this turn (or its concurrency slot) forever.
        """

        def _settle_pump(task: asyncio.Task[None]) -> None:
            # Retrieve the exception (if any) so the loop does not log an
            # "exception was never retrieved" warning for a pump that failed
            # in the same tick the deadline fired.
            if not task.cancelled():
                task.exception()

        await asyncio.wait({pump}, timeout=CLEANUP_GRACE_SECONDS)
        if not pump.done():
            # The adapter generator is still unwinding; it cannot be closed
            # while running, so close it in the background once it settles.
            def _close_when_settled(task: asyncio.Task[None]) -> None:
                _settle_pump(task)
                _close_in_background(events)

            pump.add_done_callback(_close_when_settled)
            return
        _settle_pump(pump)
        close_task = _close_in_background(events)
        await asyncio.wait({close_task}, timeout=CLEANUP_GRACE_SECONDS)
