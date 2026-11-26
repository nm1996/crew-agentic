"""SQLAlchemy ORM models — the authoritative workflow state.

Any change here needs an Alembic migration in ``persistence/migrations/versions``.
JSON-ish fields are stored as TEXT holding JSON to stay portable and inspectable
with the ``sqlite3`` shell. All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class JsonMixin:
    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def loads(value: str | None, default: Any = None) -> Any:
        return json.loads(value) if value else default


class Run(Base, JsonMixin):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    request: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    base_branch: Mapped[str | None] = mapped_column(String(255))
    base_commit: Mapped[str] = mapped_column(String(64))
    integration_branch: Mapped[str | None] = mapped_column(String(255))
    plan_json: Mapped[str | None] = mapped_column(Text)
    correction_cycle: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # Stage the run was in when it failed; `crew retry` resumes from there.
    failed_state: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    @property
    def plan(self) -> dict[str, Any] | None:
        return self.loads(self.plan_json)


class Task(Base, JsonMixin):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("run_id", "key", name="uq_task_run_key"),)

    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    cycle: Mapped[int] = mapped_column(Integer, default=0)
    seq: Mapped[int] = mapped_column(Integer, default=0)  # stable display/integration order
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, default="")
    dependencies_json: Mapped[str] = mapped_column(Text, default="[]")
    acceptance_json: Mapped[str] = mapped_column(Text, default="[]")
    expected_files_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(16), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    execution_id: Mapped[str | None] = mapped_column(String(40))
    worktree_path: Mapped[str | None] = mapped_column(Text)
    branch: Mapped[str | None] = mapped_column(String(255))
    start_commit: Mapped[str | None] = mapped_column(String(64))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    integrated: Mapped[bool] = mapped_column(Boolean, default=False)
    changed_files_json: Mapped[str] = mapped_column(Text, default="[]")
    report_json: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def dependencies(self) -> list[str]:
        return self.loads(self.dependencies_json, [])

    @property
    def acceptance_criteria(self) -> list[str]:
        return self.loads(self.acceptance_json, [])

    @property
    def expected_files(self) -> list[str]:
        return self.loads(self.expected_files_json, [])

    @property
    def changed_files(self) -> list[str]:
        return self.loads(self.changed_files_json, [])

    @property
    def report(self) -> dict[str, Any] | None:
        return self.loads(self.report_json)


class AgentExecution(Base, JsonMixin):
    __tablename__ = "agent_executions"
    # One execution per task attempt: a crash cannot lead to a duplicate dispatch.
    __table_args__ = (UniqueConstraint("task_pk", "attempt", name="uq_execution_task_attempt"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    task_pk: Mapped[int | None] = mapped_column(ForeignKey("tasks.pk", ondelete="CASCADE"))
    attempt: Mapped[int | None] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), index=True)
    cycle: Mapped[int] = mapped_column(Integer, default=0)
    cwd: Mapped[str] = mapped_column(Text)
    pid: Mapped[int | None] = mapped_column(Integer)
    # Process creation time, so recovery never signals a reused PID.
    process_started_at: Mapped[float | None] = mapped_column(Float)
    prompt: Mapped[str] = mapped_column(Text, default="")  # redacted
    output_json: Mapped[str | None] = mapped_column(Text)  # validated structured output
    raw_output: Mapped[str | None] = mapped_column(Text)  # redacted, truncated
    error: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(String(80))
    cost_usd: Mapped[float | None] = mapped_column(Float)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    num_turns: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(String(120))
    # Provider-reported token counts, e.g. {"input_tokens": 12, "output_tokens": 34, ...}
    usage_json: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def usage(self) -> dict[str, int]:
        return self.loads(self.usage_json, {})

    @property
    def output(self) -> dict[str, Any] | None:
        return self.loads(self.output_json)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    decision: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkflowEvent(Base, JsonMixin):
    """Append-only history. Never updated or deleted by CREW."""

    __tablename__ = "workflow_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    type: Mapped[str] = mapped_column(String(48))
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str | None] = mapped_column(String(32))
    task_key: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, default="")
    data_json: Mapped[str | None] = mapped_column(Text)


class CommandExecution(Base):
    __tablename__ = "command_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    task_pk: Mapped[int | None] = mapped_column(ForeignKey("tasks.pk", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(32))  # git | verification
    category: Mapped[str] = mapped_column(String(32))
    argv_json: Mapped[str] = mapped_column(Text)  # redacted
    cwd: Mapped[str] = mapped_column(Text)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    stdout: Mapped[str | None] = mapped_column(Text)  # redacted, truncated
    stderr: Mapped[str | None] = mapped_column(Text)  # redacted, truncated
    cycle: Mapped[int] = mapped_column(Integer, default=0)
    sandbox: Mapped[str | None] = mapped_column(String(16))  # none | bwrap | docker
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("run_id", "name", name="uq_artifact_run_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
