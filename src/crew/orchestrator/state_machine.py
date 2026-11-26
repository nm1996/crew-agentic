"""Run state machine (docs/WORKFLOW.md §1).

Pure data and functions: no I/O. :func:`assert_transition` is called inside the
same transaction that writes the new state, so illegal transitions never persist.

Representation choices (documented in docs/WORKFLOW.md):
* A rejected plan ends in ``PLAN_REJECTED`` (terminal), not ``CANCELLED``.
* A rejected final result ends in ``REJECTED`` (terminal).
* ``FAILED`` is terminal for the engine, but the developer may retry it from
  the stage it failed in (``crew retry``).
"""

from __future__ import annotations

from crew.domain.enums import TERMINAL_STATES, RunState
from crew.domain.errors import InvalidTransitionError

S = RunState

_FORWARD: dict[RunState, frozenset[RunState]] = {
    S.CREATED: frozenset({S.ANALYZING}),
    S.ANALYZING: frozenset({S.PLANNING}),
    S.PLANNING: frozenset({S.AWAITING_PLAN_APPROVAL}),
    S.AWAITING_PLAN_APPROVAL: frozenset({S.IMPLEMENTING, S.PLAN_REJECTED}),
    S.IMPLEMENTING: frozenset({S.INTEGRATING}),
    S.INTEGRATING: frozenset({S.REVIEWING}),
    S.REVIEWING: frozenset({S.CHANGES_REQUESTED, S.EXTERNAL_EVALUATION}),
    S.CHANGES_REQUESTED: frozenset({S.IMPLEMENTING}),
    S.EXTERNAL_EVALUATION: frozenset({S.AWAITING_FINAL_APPROVAL}),
    S.AWAITING_FINAL_APPROVAL: frozenset({S.COMPLETED, S.REJECTED}),
}

# Stages a FAILED run may be retried from (only via `crew retry`, recorded as an
# approval). FAILED is still terminal for the engine: it never leaves it alone.
RETRY_TARGETS: frozenset[RunState] = frozenset(
    {
        S.ANALYZING,
        S.PLANNING,
        S.IMPLEMENTING,
        S.INTEGRATING,
        S.REVIEWING,
        S.CHANGES_REQUESTED,
        S.EXTERNAL_EVALUATION,
    }
)

# Any non-terminal state may fail or be cancelled.
TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    state: (_FORWARD.get(state, frozenset()) | {S.FAILED, S.CANCELLED})
    if state not in TERMINAL_STATES
    else (RETRY_TARGETS if state is S.FAILED else frozenset())
    for state in RunState
}


def retry_target(failed_state: RunState | None) -> RunState | None:
    """Stage to retry a run from, given the stage it failed in."""
    if failed_state is S.CREATED:
        return S.ANALYZING
    return failed_state if failed_state in RETRY_TARGETS else None


def can_transition(from_state: RunState, to_state: RunState) -> bool:
    return to_state in TRANSITIONS[from_state]


def assert_transition(from_state: RunState, to_state: RunState) -> None:
    if not can_transition(from_state, to_state):
        raise InvalidTransitionError(
            f"Illegal workflow transition {from_state} -> {to_state}",
        )


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_STATES
