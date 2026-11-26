import itertools

import pytest

from crew.domain.enums import TERMINAL_STATES, RunState
from crew.domain.errors import InvalidTransitionError
from crew.orchestrator.state_machine import assert_transition, can_transition

S = RunState

HAPPY_PATH = [
    S.CREATED,
    S.ANALYZING,
    S.PLANNING,
    S.AWAITING_PLAN_APPROVAL,
    S.IMPLEMENTING,
    S.INTEGRATING,
    S.REVIEWING,
    S.EXTERNAL_EVALUATION,
    S.AWAITING_FINAL_APPROVAL,
    S.COMPLETED,
]


def test_happy_path_is_legal():
    for a, b in itertools.pairwise(HAPPY_PATH):
        assert_transition(a, b)


def test_correction_loop_is_legal():
    assert can_transition(S.REVIEWING, S.CHANGES_REQUESTED)
    assert can_transition(S.CHANGES_REQUESTED, S.IMPLEMENTING)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (S.PLANNING, S.IMPLEMENTING),  # skipping plan approval
        (S.AWAITING_PLAN_APPROVAL, S.REVIEWING),
        (S.IMPLEMENTING, S.REVIEWING),  # skipping integration
        (S.REVIEWING, S.AWAITING_FINAL_APPROVAL),  # skipping external evaluation
        (S.EXTERNAL_EVALUATION, S.COMPLETED),  # skipping final approval
        (S.CREATED, S.COMPLETED),
    ],
)
def test_gate_skipping_transitions_are_rejected(a, b):
    with pytest.raises(InvalidTransitionError):
        assert_transition(a, b)


@pytest.mark.parametrize("state", [s for s in RunState if s not in TERMINAL_STATES])
def test_any_active_state_can_fail_or_be_cancelled(state):
    assert can_transition(state, S.FAILED)
    assert can_transition(state, S.CANCELLED)


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_terminal_states_have_no_exits(state):
    for target in RunState:
        assert not can_transition(state, target)
