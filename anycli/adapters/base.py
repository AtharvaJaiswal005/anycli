"""Abstract adapter contract.

An adapter knows exactly five things about its agent CLI: how to launch it,
send a turn, stream raw events, capture/resume a session id, and report auth
and rate-limit state in normalized form. Everything else lives in middleware,
which talks only to this contract.

RawEvent kinds contract
-----------------------

The middleware streaming normalizer (``anycli.middleware.streaming.normalize``)
is adapter-agnostic: it interprets only the :class:`~anycli.types.RawEvent`
``kind`` values below. Every adapter must map its agent CLI's native events
onto these kinds, with ``data`` as a plain dict shaped exactly as documented.

- ``"init"`` — emitted once, as early as possible, when the agent CLI reports
  its session id. ``data = {"session_id": str}``.
- ``"text_delta"`` — a fragment of assistant text. ``data = {"text": str}``.
- ``"tool_use"`` — the agent invoked a tool.
  ``data = {"tool_name": str, "tool_input": dict, "tool_use_id": str}``.
- ``"tool_result"`` — outcome of a prior tool use.
  ``data = {"tool_use_id": str, "content": Any, "is_error": bool}``.
- ``"usage"`` — interim per-message token usage.
  ``data = {"message_id": str | None, "usage": {"input_tokens": int,
  "output_tokens": int, "cache_read_tokens": int,
  "cache_creation_tokens": int}}``. The normalizer deduplicates by
  ``message_id``: duplicate usage reported for the same message id (as
  happens with parallel tool calls) is counted once.
- ``"result"`` — terminal event, exactly one per run, always last.
  ``data = {"text": str, "session_id": str, "is_error": bool,
  "num_turns": int, "usage": dict | None (same keys as above),
  "total_cost_usd": float | None, "raw": dict | None}``. When ``usage`` is
  None the normalizer falls back to summing deduplicated ``"usage"`` events.
- Any other ``kind`` is adapter-specific diagnostics; the normalizer ignores
  it and produces no public chunk for it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from anycli.types import AuthStatus, RawEvent


class BaseAgentAdapter(ABC):
    """Contract every agent CLI adapter implements.

    Adapters emit :class:`~anycli.types.RawEvent` envelopes; the middleware
    normalizes them into public chunk types. Adapters must not implement
    concurrency control, retries, or session bookkeeping — that is
    middleware's job.
    """

    #: Short identifier for the agent CLI this adapter drives, e.g. "claude-code".
    name: str

    @abstractmethod
    async def check_auth(self) -> AuthStatus:
        """Report whether the underlying agent CLI is authenticated.

        Must not prompt for or transport credentials; it only inspects and
        reports the local auth state in normalized form.
        """

    @abstractmethod
    def run_once(
        self,
        prompt: str,
        cwd: str,
        *,
        allowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        max_turns: int | None = None,
        extra_options: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawEvent]:
        """One-shot turn: launch, send ``prompt``, stream raw events, exit.

        An async generator. The subprocess must be terminated when the
        iterator is exhausted or closed, including on cancellation.
        ``extra_options`` passes adapter-specific options through untouched.
        """

    # open_session() / AdapterSession (persistent multi-turn sessions) arrive
    # in v0.2; v0.1 is one-shot only.
