"""Data-access layer over the ORM models.

Every workflow state change goes through :meth:`Store.transition`, which updates
``runs.state`` and appends a :class:`WorkflowEvent` in one transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from crew.domain.enums import ExecutionStatus, RunState, TaskStatus
from crew.domain.errors import NotFoundError
from crew.orchestrator.state_machine import assert_transition
from crew.persistence.database import make_engine, make_session_factory
from crew.persistence.models import (
    AgentExecution,
    Approval,
    Artifact,
    CommandExecution,
    JsonMixin,
    Run,
    Task,
    WorkflowEvent,
    utcnow,
)
from crew.security.redaction import redact, redact_json_text

# Free-text columns that may contain agent or tool output.
_TEXT_FIELDS = frozenset({"failure_reason", "error", "raw_output", "message", "note"})


def _sanitize(fields: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets centrally before anything is written to SQLite.

    ``*_json`` columns are redacted structurally so they stay valid JSON.
    """
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, str) and key.endswith("_json"):
            clean[key] = redact_json_text(value)
        elif isinstance(value, str) and key in _TEXT_FIELDS:
            clean[key] = redact(value)
        else:
            clean[key] = value
    return clean


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


class Store:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def open(cls, db_file: Path) -> Store:
        return cls(make_session_factory(make_engine(db_file)))

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._sessions.begin() as s:
            yield s

    # ---- runs -------------------------------------------------------------

    def create_run(
        self, *, request: str, provider: str, base_branch: str | None, base_commit: str
    ) -> Run:
        run = Run(
            id=new_run_id(),
            request=request,
            state=RunState.CREATED,
            provider=provider,
            base_branch=base_branch,
            base_commit=base_commit,
        )
        with self.session() as s:
            s.add(run)
            s.add(WorkflowEvent(run_id=run.id, type="run_created", to_state=RunState.CREATED))
        return run

    def get_run(self, run_id: str) -> Run:
        with self.session() as s:
            run = s.get(Run, run_id)
            if run is None:
                raise NotFoundError(f"Run {run_id} not found", hint="See `crew history`.")
            return run

    def latest_run(self) -> Run | None:
        with self.session() as s:
            return s.scalars(select(Run).order_by(Run.created_at.desc()).limit(1)).first()

    def list_runs(self, limit: int = 20) -> list[Run]:
        with self.session() as s:
            return list(s.scalars(select(Run).order_by(Run.created_at.desc()).limit(limit)))

    def update_run(self, run_id: str, **fields: Any) -> Run:
        with self.session() as s:
            run = s.get(Run, run_id)
            if run is None:
                raise NotFoundError(f"Run {run_id} not found")
            for key, value in _sanitize(fields).items():
                setattr(run, key, value)
            return run

    def transition(
        self,
        run_id: str,
        to_state: RunState,
        *,
        message: str = "",
        data: dict[str, Any] | None = None,
        new_tasks: list[Task] | None = None,
        **run_fields: Any,
    ) -> Run:
        """Validate and apply a state transition atomically with its event.

        ``new_tasks`` are inserted in the same transaction (plan persistence and
        correction-task creation), so a crash can never leave tasks without the
        state that owns them, or vice versa.
        """
        with self.session() as s:
            run = s.get(Run, run_id)
            if run is None:
                raise NotFoundError(f"Run {run_id} not found")
            from_state = RunState(run.state)
            assert_transition(from_state, to_state)
            run.state = to_state
            for key, value in _sanitize(run_fields).items():
                setattr(run, key, value)
            for task in new_tasks or []:
                task.run_id = run_id
                s.add(task)
                s.add(
                    WorkflowEvent(
                        run_id=run_id, type="task_created", task_key=task.key, message=task.title
                    )
                )
            s.add(
                WorkflowEvent(
                    run_id=run_id,
                    type="state_changed",
                    from_state=from_state,
                    to_state=to_state,
                    message=redact(message),
                    data_json=JsonMixin.dumps(data) if data else None,
                )
            )
            return run

    def request_cancel(self, run_id: str) -> None:
        self.update_run(run_id, cancel_requested=True)
        self.add_event(run_id, "cancel_requested", "Cancellation requested by developer")

    def is_cancel_requested(self, run_id: str) -> bool:
        with self.session() as s:
            return bool(s.scalar(select(Run.cancel_requested).where(Run.id == run_id)))

    # ---- events -----------------------------------------------------------

    def add_event(
        self,
        run_id: str,
        type_: str,
        message: str = "",
        *,
        task_key: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self.session() as s:
            s.add(
                WorkflowEvent(
                    run_id=run_id,
                    type=type_,
                    task_key=task_key,
                    message=redact(message),
                    data_json=JsonMixin.dumps(data) if data else None,
                )
            )

    def events(self, run_id: str) -> list[WorkflowEvent]:
        with self.session() as s:
            return list(
                s.scalars(
                    select(WorkflowEvent)
                    .where(WorkflowEvent.run_id == run_id)
                    .order_by(WorkflowEvent.id)
                )
            )

    # ---- tasks ------------------------------------------------------------

    def tasks(self, run_id: str, *, cycle: int | None = None) -> list[Task]:
        with self.session() as s:
            q = select(Task).where(Task.run_id == run_id)
            if cycle is not None:
                q = q.where(Task.cycle == cycle)
            return list(s.scalars(q.order_by(Task.cycle, Task.seq, Task.pk)))

    def get_task(self, run_id: str, key: str) -> Task:
        with self.session() as s:
            task = s.scalars(select(Task).where(Task.run_id == run_id, Task.key == key)).first()
            if task is None:
                raise NotFoundError(f"Task {key} not found in run {run_id}")
            return task

    def update_task(self, pk: int, **fields: Any) -> Task:
        with self.session() as s:
            task = s.get(Task, pk)
            if task is None:
                raise NotFoundError(f"Task #{pk} not found")
            for key, value in _sanitize(fields).items():
                setattr(task, key, value)
            return task

    def set_task_status(
        self, task: Task, status: TaskStatus, message: str = "", **fields: Any
    ) -> Task:
        with self.session() as s:
            row = s.get(Task, task.pk)
            assert row is not None
            previous = row.status
            row.status = status
            for key, value in _sanitize(fields).items():
                setattr(row, key, value)
            s.add(
                WorkflowEvent(
                    run_id=row.run_id,
                    type="task_status",
                    task_key=row.key,
                    from_state=previous,
                    to_state=status,
                    message=redact(message),
                )
            )
            return row

    def claim_task(self, task: Task, execution_id: str) -> Task | None:
        """Atomically move a task from PENDING/READY to RUNNING with a new attempt.

        Compare-and-set prevents double dispatch even if two drivers raced.
        """
        with self.session() as s:
            result = s.execute(
                update(Task)
                .where(
                    Task.pk == task.pk,
                    Task.status.in_([TaskStatus.PENDING, TaskStatus.READY]),
                )
                .values(
                    status=TaskStatus.RUNNING,
                    attempt=Task.attempt + 1,
                    execution_id=execution_id,
                    started_at=utcnow(),
                    failure_reason=None,
                )
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                return None
            row = s.get(Task, task.pk)
            assert row is not None
            s.refresh(row)
            s.add(
                WorkflowEvent(
                    run_id=row.run_id,
                    type="task_status",
                    task_key=row.key,
                    from_state=task.status,
                    to_state=TaskStatus.RUNNING,
                    message=f"dispatched attempt {row.attempt}",
                )
            )
            return row

    # ---- agent executions -------------------------------------------------

    def start_execution(
        self,
        *,
        run_id: str,
        role: str,
        provider: str,
        cwd: str,
        prompt: str,
        cycle: int,
        task: Task | None = None,
        execution_id: str | None = None,
    ) -> AgentExecution:
        execution = AgentExecution(
            id=execution_id or uuid.uuid4().hex[:16],
            run_id=run_id,
            task_pk=task.pk if task else None,
            attempt=task.attempt if task else None,
            role=role,
            provider=provider,
            status=ExecutionStatus.RUNNING,
            cycle=cycle,
            cwd=cwd,
            prompt=redact(prompt)[:200_000],
        )
        with self.session() as s:
            s.add(execution)
        return execution

    def set_execution_pid(self, execution_id: str, pid: int, started_at: float | None) -> None:
        with self.session() as s:
            row = s.get(AgentExecution, execution_id)
            if row is not None:
                row.pid = pid
                row.process_started_at = started_at

    def finish_execution(self, execution_id: str, **fields: Any) -> None:
        with self.session() as s:
            row = s.get(AgentExecution, execution_id)
            assert row is not None
            row.finished_at = utcnow()
            for key, value in _sanitize(fields).items():
                setattr(row, key, value)

    def executions(
        self, run_id: str, *, role: str | None = None, status: str | None = None
    ) -> list[AgentExecution]:
        with self.session() as s:
            q = select(AgentExecution).where(AgentExecution.run_id == run_id)
            if role:
                q = q.where(AgentExecution.role == role)
            if status:
                q = q.where(AgentExecution.status == status)
            return list(s.scalars(q.order_by(AgentExecution.started_at)))

    def latest_output(self, run_id: str, role: str, cycle: int | None = None) -> dict | None:
        rows = [
            e
            for e in self.executions(run_id, role=role, status=ExecutionStatus.SUCCEEDED)
            if cycle is None or e.cycle == cycle
        ]
        return rows[-1].output if rows else None

    # ---- approvals, commands, artifacts ----------------------------------

    def add_approval(self, run_id: str, kind: str, decision: str, note: str | None) -> None:
        with self.session() as s:
            s.add(
                Approval(
                    run_id=run_id, kind=kind, decision=decision, note=redact(note or "") or None
                )
            )

    def approvals(self, run_id: str) -> list[Approval]:
        with self.session() as s:
            return list(
                s.scalars(select(Approval).where(Approval.run_id == run_id).order_by(Approval.id))
            )

    def record_command(self, **fields: Any) -> None:
        with self.session() as s:
            s.add(CommandExecution(**_sanitize(fields)))

    def commands(self, run_id: str, *, purpose: str | None = None) -> list[CommandExecution]:
        with self.session() as s:
            q = select(CommandExecution).where(CommandExecution.run_id == run_id)
            if purpose:
                q = q.where(CommandExecution.purpose == purpose)
            return list(s.scalars(q.order_by(CommandExecution.id)))

    def upsert_artifact(self, run_id: str, name: str, path: Path) -> None:
        with self.session() as s:
            row = s.scalars(
                select(Artifact).where(Artifact.run_id == run_id, Artifact.name == name)
            ).first()
            if row is None:
                s.add(Artifact(run_id=run_id, name=name, path=str(path)))
            else:
                row.path = str(path)
                row.updated_at = utcnow()

    def artifacts(self, run_id: str) -> list[Artifact]:
        with self.session() as s:
            return list(
                s.scalars(select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.id))
            )
