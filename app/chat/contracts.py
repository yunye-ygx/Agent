"""Stable chat execution states, event names and domain errors."""

from dataclasses import dataclass
from enum import StrEnum


class TurnStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REQUIRES_ATTENTION = "requires_attention"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RECOVERING = "recovering"
    REQUIRES_ATTENTION = "requires_attention"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
)

# A requires_attention run is not recoverable automatically, but it is not a
# successful terminal state either.
SETTLED_RUN_STATUSES = TERMINAL_RUN_STATUSES | {RunStatus.REQUIRES_ATTENTION}

TERMINAL_EVENT_TYPES = frozenset(
    {
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.requires_attention",
    }
)

TRIGGER_REASONS = frozenset(
    {"initial", "failure_retry", "cancel_retry", "regenerate", "recovery"}
)

# Bump when the graph state shape changes. A Run recorded with another version
# must not be resumed by the current executor.
CHECKPOINT_SCHEMA_VERSION = "v1"


@dataclass(frozen=True)
class RunLease:
    """Fencing identity of one claimed execution attempt."""

    run_id: str
    turn_id: str
    worker_id: str
    lease_generation: int


class ExecutionLost(RuntimeError):
    """The worker no longer owns the run's current fencing generation."""

    code = "CHAT_EXECUTION_LOST"


class RunCancelled(Exception):
    """The run entered cancelling and reached a safe stop boundary."""

    code = "CHAT_RUN_CANCELLED"


class ToolRequiresAttention(Exception):
    """A side effect may have happened and cannot be reconciled automatically."""

    code = "CHAT_TOOL_REQUIRES_ATTENTION"