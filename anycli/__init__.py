"""anycli: one async Python interface for driving local agent CLIs.

Everything importable from this module is the public surface; anything else
is private and may change without notice.
"""

from anycli.adapters import BaseAgentAdapter
from anycli.adapters.claude_code import ClaudeCodeAdapter
from anycli.bridge import Bridge
from anycli.doctor import Finding, check_env
from anycli.errors import (
    AdapterError,
    AnycliError,
    AuthError,
    CLINotFound,
    PlanLimitReached,
    TransientAdapterError,
)
from anycli.types import (
    AuthStatus,
    Chunk,
    RawEvent,
    Result,
    TextDelta,
    ToolResult,
    ToolUse,
    Usage,
)

__version__ = "0.1.1"

__all__ = [
    "AdapterError",
    "AnycliError",
    "AuthError",
    "AuthStatus",
    "BaseAgentAdapter",
    "Bridge",
    "CLINotFound",
    "Chunk",
    "ClaudeCodeAdapter",
    "Finding",
    "PlanLimitReached",
    "RawEvent",
    "Result",
    "TextDelta",
    "ToolResult",
    "ToolUse",
    "TransientAdapterError",
    "Usage",
    "__version__",
    "check_env",
]
