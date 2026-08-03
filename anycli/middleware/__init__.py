"""CLI-agnostic engine: concurrency, lifecycle, and streaming normalization.

Middleware never imports an SDK and never touches a subprocess; it talks
only to the :class:`~anycli.adapters.base.BaseAgentAdapter` contract.
"""

from anycli.middleware.concurrency import ConcurrencyGovernor
from anycli.middleware.streaming import normalize

__all__ = ["ConcurrencyGovernor", "normalize"]
