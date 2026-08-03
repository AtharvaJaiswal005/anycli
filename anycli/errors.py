"""Typed error hierarchy. All public errors raised by anycli come from here."""

from __future__ import annotations

from datetime import datetime, timezone


class AnycliError(Exception):
    """Base class for every error anycli raises."""


class AdapterError(AnycliError):
    """An adapter failed to launch, drive, or parse output from its agent CLI.

    Raised for subprocess failures, malformed event streams, and other
    CLI-side faults that are not auth or plan-limit problems.
    """


class TransientAdapterError(AdapterError):
    """A transient adapter failure that is safe to retry.

    Raised for provider throttling (HTTP 429) and overload (HTTP 529)
    signals. anycli retries these with exponential backoff and jitter.
    As a fallback for adapters that cannot classify a failure, an
    :class:`AdapterError` whose message carries a 429/529 status signal
    is also treated as transient. Everything else — including
    :class:`PlanLimitReached` — is never retried.
    """


class CLINotFound(AdapterError):
    """The agent CLI executable required by an adapter could not be found.

    Raised at launch time when the underlying CLI (or its SDK transport)
    is not installed or not on PATH.
    """


class AuthError(AnycliError):
    """The agent CLI is not authenticated or its credentials were rejected.

    Raised when a run fails for credential reasons; the fix is for the user
    to log in with their own agent CLI. anycli never manages credentials.
    """


class PlanLimitReached(AnycliError):
    """The user's plan limit was hit; the run was rejected, not throttled.

    Never retried by anycli: this is a hard rejection, not a transient
    error. ``resets_at`` carries the limit reset time when the agent CLI
    reported one; it is also appended to the message in human-readable
    form, so ``str(error)`` is directly presentable to users. Catch this
    separately from :class:`AdapterError` to surface the reset time
    instead of a generic failure.
    """

    def __init__(self, message: str, resets_at: datetime | None = None) -> None:
        if resets_at is not None:
            if resets_at.tzinfo is not None:
                stamp = resets_at.astimezone(timezone.utc)
                message = f"{message} (resets at {stamp:%Y-%m-%d %H:%M} UTC)"
            else:
                message = f"{message} (resets at {resets_at:%Y-%m-%d %H:%M})"
        super().__init__(message)
        self.resets_at = resets_at
