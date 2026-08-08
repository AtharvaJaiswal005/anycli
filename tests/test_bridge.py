"""Bridge.run(): typed results, typed streaming, and the retry policy."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

import pytest

import anycli.bridge
from anycli import (
    AdapterError,
    Bridge,
    Chunk,
    PlanLimitReached,
    Result,
    TextDelta,
    TransientAdapterError,
    Usage,
)

from .fake_adapter import DEFAULT_USAGE, FakeAdapter, make_script

CHUNK_TYPES = get_args(Chunk)


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink retry backoff so retry tests finish in milliseconds."""
    monkeypatch.setattr(anycli.bridge, "_BACKOFF_BASE_SECONDS", 0.001)


def make_bridge(adapter: FakeAdapter, **kwargs: object) -> Bridge:
    kwargs.setdefault("default_cwd", "/tmp/fake-cwd")
    kwargs.setdefault("warn_on_auth_conflict", False)
    return Bridge(adapter, **kwargs)  # type: ignore[arg-type]


async def test_run_returns_typed_result_with_aggregated_text() -> None:
    adapter = FakeAdapter(make_script(text_parts=("hel", "lo"), result_text=""))
    bridge = make_bridge(adapter)

    result = await bridge.run("hi")

    assert isinstance(result, Result)
    assert result.text == "hello"  # aggregated from text deltas
    assert result.session_id == "sess-1"
    # The pair travels with the session id, resolved to an absolute path.
    assert result.cwd == str(Path("/tmp/fake-cwd").resolve())
    assert result.is_error is False
    assert result.num_turns == 1
    assert result.usage == Usage(**DEFAULT_USAGE)
    assert result.total_cost_usd == 0.001
    assert adapter.attempts == 1
    assert adapter.calls[0]["prompt"] == "hi"
    assert adapter.calls[0]["cwd"] == str(Path("/tmp/fake-cwd").resolve())


async def test_run_passes_options_through_to_adapter() -> None:
    adapter = FakeAdapter()
    bridge = make_bridge(adapter)

    await bridge.run(
        "hi",
        cwd="/somewhere/else",
        allowed_tools=["Read"],
        permission_mode="plan",
        max_turns=2,
        extra_options={"model": "x"},
    )

    call = adapter.calls[0]
    assert call["cwd"] == str(Path("/somewhere/else").resolve())
    assert call["allowed_tools"] == ["Read"]
    assert call["permission_mode"] == "plan"
    assert call["max_turns"] == 2
    assert call["extra_options"] == {"model": "x"}


async def test_run_requires_cwd_when_no_default() -> None:
    bridge = Bridge(FakeAdapter(), warn_on_auth_conflict=False)
    with pytest.raises(ValueError, match="cwd"):
        await bridge.run("hi")


async def test_run_resolves_cwd_to_absolute_path() -> None:
    """A relative cwd like "." must never reach the adapter or the Result.

    Agent CLIs key on-disk session state by absolute working directory;
    (session_id, cwd) is only resumable if the stored cwd is absolute.
    """
    adapter = FakeAdapter()
    bridge = Bridge(adapter, warn_on_auth_conflict=False)

    result = await bridge.run("hi", cwd=".")

    expected = str(Path.cwd())
    assert adapter.calls[0]["cwd"] == expected
    assert result.cwd == expected


async def test_stream_yields_only_typed_chunks() -> None:
    adapter = FakeAdapter(make_script(text_parts=("a", "b", "c")))
    bridge = make_bridge(adapter)

    stream = await bridge.run("hi", stream=True)
    chunks = [chunk async for chunk in stream]

    assert chunks, "stream yielded nothing"
    for chunk in chunks:
        assert isinstance(chunk, CHUNK_TYPES), f"non-typed chunk leaked: {chunk!r}"
        assert not isinstance(chunk, str)
    assert [type(c) for c in chunks[:3]] == [TextDelta, TextDelta, TextDelta]
    assert isinstance(chunks[-1], Result)
    assert chunks[-1].text == "abc"


async def test_plan_limit_reached_is_not_retried() -> None:
    resets_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    adapter = FakeAdapter(error=PlanLimitReached("usage limit reached", resets_at=resets_at))
    bridge = make_bridge(adapter)

    with pytest.raises(PlanLimitReached) as excinfo:
        await bridge.run("hi")

    assert adapter.attempts == 1
    assert excinfo.value.resets_at == resets_at


async def test_hard_adapter_error_is_not_retried() -> None:
    adapter = FakeAdapter(error=AdapterError("subprocess exploded"))
    bridge = make_bridge(adapter)

    with pytest.raises(AdapterError):
        await bridge.run("hi")

    assert adapter.attempts == 1


async def test_transient_error_is_retried_then_succeeds(fast_backoff: None) -> None:
    adapter = FakeAdapter(error=TransientAdapterError("throttled"), fail_times=2)
    bridge = make_bridge(adapter)

    result = await bridge.run("hi")

    assert adapter.attempts == 3  # two failures, then success
    assert isinstance(result, Result)
    assert result.session_id == "sess-1"


async def test_transient_error_gives_up_after_max_attempts(fast_backoff: None) -> None:
    adapter = FakeAdapter(error=TransientAdapterError("still throttled"))
    bridge = make_bridge(adapter)

    with pytest.raises(TransientAdapterError):
        await bridge.run("hi")

    assert adapter.attempts == 3


async def test_429_in_message_is_treated_as_transient(fast_backoff: None) -> None:
    adapter = FakeAdapter(error=AdapterError("HTTP 429 rate limited"), fail_times=1)
    bridge = make_bridge(adapter)

    result = await bridge.run("hi")

    assert adapter.attempts == 2
    assert isinstance(result, Result)


async def test_stream_retries_only_before_first_chunk(fast_backoff: None) -> None:
    # Failure at position 0: nothing yielded yet, so the stream retries.
    adapter = FakeAdapter(error=TransientAdapterError("throttled"), error_at=0, fail_times=1)
    bridge = make_bridge(adapter)

    stream = await bridge.run("hi", stream=True)
    chunks = [chunk async for chunk in stream]

    assert adapter.attempts == 2
    assert isinstance(chunks[-1], Result)


async def test_stream_does_not_retry_after_first_chunk(fast_backoff: None) -> None:
    # Failure mid-stream: chunks already yielded, so the error propagates.
    adapter = FakeAdapter(error=TransientAdapterError("throttled"), error_at=2, fail_times=1)
    bridge = make_bridge(adapter)

    stream = await bridge.run("hi", stream=True)
    with pytest.raises(TransientAdapterError):
        async for _ in stream:
            pass

    assert adapter.attempts == 1
