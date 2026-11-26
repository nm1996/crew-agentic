import pytest

from crew.domain.enums import RunState, TaskStatus
from crew.domain.errors import InvalidTransitionError
from crew.orchestrator.engine import Project
from crew.persistence.models import Task


def make_run(project: Project):
    return project.store.create_run(
        request="r", provider="fake", base_branch="main", base_commit=project.git.head()
    )


def test_transition_appends_event_atomically(project: Project):
    store = project.store
    run = make_run(project)
    store.transition(run.id, RunState.ANALYZING, message="go")
    events = [e for e in store.events(run.id) if e.type == "state_changed"]
    assert [(e.from_state, e.to_state) for e in events] == [("CREATED", "ANALYZING")]


def test_illegal_transition_is_not_persisted(project: Project):
    store = project.store
    run = make_run(project)
    with pytest.raises(InvalidTransitionError):
        store.transition(run.id, RunState.IMPLEMENTING, new_tasks=[_task("a")])
    assert store.get_run(run.id).state == RunState.CREATED
    assert store.tasks(run.id) == []
    assert not [e for e in store.events(run.id) if e.type == "state_changed"]


def _task(key: str) -> Task:
    return Task(key=key, kind="PLANNED", title=key, description=key, status=TaskStatus.PENDING)


def test_claim_task_prevents_double_dispatch(project: Project):
    store = project.store
    run = make_run(project)
    for state in (RunState.ANALYZING, RunState.PLANNING):
        store.transition(run.id, state)
    store.transition(run.id, RunState.AWAITING_PLAN_APPROVAL, new_tasks=[_task("a")])
    task = store.tasks(run.id)[0]
    first = store.claim_task(task, "exec-1")
    second = store.claim_task(task, "exec-2")
    assert first is not None and first.attempt == 1 and first.status == TaskStatus.RUNNING
    assert second is None


def test_timestamps_and_foreign_keys(project: Project):
    store = project.store
    run = make_run(project)
    with store.session() as s:
        fk = s.connection().exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert fk == 1
    assert run.created_at.tzinfo is not None
