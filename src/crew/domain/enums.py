"""Enumerations shared across CREW layers.

Values are persisted in SQLite, so renaming a member is a schema change.
"""

from enum import StrEnum


class RunState(StrEnum):
    CREATED = "CREATED"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    IMPLEMENTING = "IMPLEMENTING"
    INTEGRATING = "INTEGRATING"
    REVIEWING = "REVIEWING"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    EXTERNAL_EVALUATION = "EXTERNAL_EVALUATION"
    AWAITING_FINAL_APPROVAL = "AWAITING_FINAL_APPROVAL"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    PLAN_REJECTED = "PLAN_REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = frozenset(
    {
        RunState.COMPLETED,
        RunState.REJECTED,
        RunState.PLAN_REJECTED,
        RunState.FAILED,
        RunState.CANCELLED,
    }
)

# States in which the engine stops and waits for an explicit developer command.
GATE_STATES = frozenset({RunState.AWAITING_PLAN_APPROVAL, RunState.AWAITING_FINAL_APPROVAL})


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class TaskKind(StrEnum):
    PLANNED = "PLANNED"
    CORRECTION = "CORRECTION"


class Role(StrEnum):
    DIRECTOR = "director"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    EVALUATOR = "evaluator"


READ_ONLY_ROLES = frozenset({Role.DIRECTOR, Role.REVIEWER, Role.EVALUATOR})


class ExecutionStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class ApprovalKind(StrEnum):
    PLAN = "PLAN"
    FINAL = "FINAL"


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Severity(StrEnum):
    BLOCKER = "BLOCKER"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class FindingCategory(StrEnum):
    CORRECTNESS = "correctness"
    SECURITY = "security"
    DATA_INTEGRITY = "data integrity"
    CONCURRENCY = "concurrency"
    PERFORMANCE = "performance"
    ARCHITECTURE = "architecture"
    MAINTAINABILITY = "maintainability"
    TESTS = "tests"
    DOCUMENTATION = "documentation"


class Recommendation(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    NEEDS_CHANGES = "NEEDS_CHANGES"


class CommandCategory(StrEnum):
    """Execution-policy classes from SECURITY.md §3."""

    READ_ONLY = "read_only"
    VERIFICATION = "verification"
    WORKTREE_WRITE = "worktree_write"
    NETWORK = "network"
    DESTRUCTIVE = "destructive"
