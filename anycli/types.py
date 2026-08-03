"""Typed chunks and data shapes shared across the public API.

Every public streaming surface yields these types, never raw strings or
adapter-specific objects. Adapters emit :class:`RawEvent` envelopes; the
middleware normalizes those into the public chunk types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TextDelta:
    """A fragment of assistant text, streamed as it is produced."""

    text: str


@dataclass(frozen=True)
class ToolUse:
    """The agent invoked a tool."""

    tool_name: str
    tool_input: dict
    tool_use_id: str


@dataclass(frozen=True)
class ToolResult:
    """The outcome of a prior :class:`ToolUse`, matched by ``tool_use_id``."""

    tool_use_id: str
    content: Any
    is_error: bool


@dataclass(frozen=True)
class Usage:
    """Token accounting for a completed run, as reported by the agent CLI."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


@dataclass(frozen=True)
class Result:
    """Terminal chunk of a run: final text plus session and cost metadata.

    ``session_id`` and ``cwd`` — the working directory the session was
    created under — travel together on this object and must be persisted
    together; one without the other cannot resume the session.
    """

    text: str
    session_id: str
    cwd: str
    is_error: bool
    num_turns: int
    usage: Usage | None
    total_cost_usd: float | None
    raw: dict | None


@dataclass(frozen=True)
class RawEvent:
    """Thin envelope adapters emit for every event from their agent CLI.

    ``kind`` is an adapter-defined discriminator; ``data`` is the untouched
    underlying payload. The middleware normalizes these into public chunks.
    """

    kind: str
    data: Any


@dataclass(frozen=True)
class AuthStatus:
    """Normalized auth state reported by an adapter's ``check_auth()``."""

    ok: bool
    method: str | None
    detail: str | None


Chunk = TextDelta | ToolUse | ToolResult | Result
"""Union of every chunk type a public streaming surface may yield."""
