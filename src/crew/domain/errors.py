"""Error hierarchy.

Every error raised deliberately by CREW derives from :class:`CrewError`. The CLI
translates these into a message plus a stable exit code; anything else is a bug
and is reported with a traceback hint.
"""


class CrewError(Exception):
    """Base class for expected, user-facing failures."""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class ConfigError(CrewError):
    exit_code = 2


class NotInitializedError(CrewError):
    exit_code = 2


class PreconditionError(CrewError):
    """The environment or repository is not in a state that allows the action."""

    exit_code = 3


class InvalidTransitionError(CrewError):
    exit_code = 4


class NotFoundError(CrewError):
    exit_code = 5


class RunLockedError(CrewError):
    exit_code = 6


class PolicyViolationError(CrewError):
    exit_code = 7


class GitError(CrewError):
    exit_code = 8


class ProviderError(CrewError):
    """A provider execution failed (process error, timeout, malformed output)."""

    exit_code = 9

    def __init__(self, message: str, *, kind: str = "error", hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.kind = kind


class PlanValidationError(CrewError):
    exit_code = 10


class BudgetExceededError(CrewError):
    """The run's spending limit (execution.max_run_budget_usd) is used up."""

    exit_code = 11


class MergeConflictError(CrewError):
    """A merge conflicted and could not be resolved (retrying would not help)."""

    exit_code = 12
