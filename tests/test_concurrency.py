"""Concurrency invariants: semaphore cap, generator reaping, max-lifetime kill."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from anycli import AdapterError, Bridge, Result
from anycli.middleware import concurrency
from anycli.middleware.concurrency import ConcurrencyGovernor
from anycli.types import AuthStatus, RawEvent

from .fake_adapter import FakeAdapter, make_script


def make_bridge(adapter: FakeAdapter, **kwargs: object) -> Bridge:
    kwargs.setdefault("default_cwd", "/tmp/fake-cwd")
    kwargs.setdefault("warn_on_auth_conflict", False)
    return Bridge(adapter, **kwargs)  # type: ignore[arg-type]


async def test_semaphore_caps_concurrent_runs() -> None:
    adapter = FakeAdapter(delay=0.02)  # slow enough that runs overlap
    bridge = make_bridge(adapter, max_concurrency=3)

    results = await asyncio.gather(*(bridge.run(f"prompt-{i}") for i in range(10)))

    assert adapter.attempts == 10
    assert adapter.max_active <= 3, f"peak active {adapter.max_active} exceeded cap 3"
    assert adapter.max_active > 1, "runs never overlapped; test proves nothing"
    assert all(isinstance(r, Result) for r in results)
    assert bridge.health() == {"active_runs": 0, "max_concurrency": 3, "queued": 0}


async def test_abandoned_stream_closes_adapter_generator() -> None:
    adapter = FakeAdapter(make_script(text_parts=("a", "b", "c", "d")))
    bridge = make_bridge(adapter)

    stream = await bridge.run("hi", stream=True)
    first = await anext(stream)
    assert first is not None
    # Consumer walks away mid-iteration.
    await stream.aclose()

    assert adapter.closed_early, "adapter generator was not closed on abandonment"
    assert adapter.cleanup_count == 1
    assert adapter.active == 0
    assert bridge.health()["active_runs"] == 0, "concurrency slot leaked"


async def test_max_lifetime_kills_hung_run() -> None:
    adapter = FakeAdapter(hang=True)
    bridge = make_bridge(adapter, max_turn_seconds=0.05)

    with pytest.raises(AdapterError, match="max lifetime"):
        await bridge.run("hi")

    assert adapter.closed_early, "hung adapter generator was not torn down"
    assert adapter.active == 0
    assert bridge.health()["active_runs"] == 0


async def test_max_lifetime_covers_whole_turn_not_per_event() -> None:
    # Each event arrives quickly, but the total turn exceeds the deadline.
    adapter = FakeAdapter(make_script(text_parts=("a",) * 20), delay=0.02)
    bridge = make_bridge(adapter, max_turn_seconds=0.05)

    with pytest.raises(AdapterError, match="max lifetime"):
        await bridge.run("hi")

    assert adapter.active == 0


async def test_governor_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        ConcurrencyGovernor(max_concurrency=0)
    with pytest.raises(ValueError):
        ConcurrencyGovernor(max_turn_seconds=0)


async def test_watchdog_reaps_abandoned_unclosed_stream() -> None:
    # The consumer stops iterating but keeps a live reference and never
    # calls aclose(): the deadline must still fire, the adapter generator
    # must still be closed, and the concurrency slot must still be freed.
    adapter = FakeAdapter(make_script(text_parts=("a", "b", "c", "d")))
    bridge = make_bridge(adapter, max_concurrency=1, max_turn_seconds=0.05)

    stream = await bridge.run("hi", stream=True)
    first = await anext(stream)
    assert first is not None
    # Abandon without closing; the reference stays alive for the whole test.

    await asyncio.sleep(0.3)

    assert adapter.closed_early, "abandoned adapter generator was not reaped"
    assert adapter.active == 0
    assert bridge.health()["active_runs"] == 0, "concurrency slot leaked"

    # The freed slot must be usable by a fresh run.
    result = await bridge.run("second")
    assert isinstance(result, Result)
    del stream


class StuckCleanupAdapter(FakeAdapter):
    """Adapter whose generator cleanup blocks far longer than the grace."""

    def __init__(self, cleanup_seconds: float) -> None:
        super().__init__()
        self._cleanup_seconds = cleanup_seconds
        self.cleanup_finished = False

    async def check_auth(self) -> AuthStatus:
        return AuthStatus(ok=True, method="fake", detail=None)

    async def run_once(  # type: ignore[override]
        self,
        prompt: str,
        cwd: str,
        **kwargs: Any,
    ) -> AsyncIterator[RawEvent]:
        try:
            while True:
                await asyncio.sleep(3600)
                yield RawEvent(kind="never", data=None)  # pragma: no cover
        finally:
            await asyncio.shield(asyncio.sleep(self._cleanup_seconds))
            self.cleanup_finished = True


async def test_max_lifetime_is_bounded_even_when_cleanup_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hung turn whose adapter cleanup itself blocks must not pin
    # bridge.run() (or the slot) past deadline + grace: the stuck cleanup
    # is left to finish in a background task.
    monkeypatch.setattr(concurrency, "CLEANUP_GRACE_SECONDS", 0.05)
    adapter = StuckCleanupAdapter(cleanup_seconds=0.5)
    bridge = make_bridge(adapter, max_turn_seconds=0.05)
    loop = asyncio.get_running_loop()

    start = loop.time()
    with pytest.raises(AdapterError, match="max lifetime"):
        await bridge.run("hi")
    elapsed = loop.time() - start

    assert elapsed < 0.4, f"run was pinned by stuck adapter cleanup ({elapsed:.2f}s)"
    assert bridge.health()["active_runs"] == 0
    assert not adapter.cleanup_finished, "cleanup finished early; test proves nothing"

    # Let the background cleanup finish so the loop closes cleanly.
    await asyncio.sleep(0.6)
    assert adapter.cleanup_finished
