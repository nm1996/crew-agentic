"""Agent runtime: build a role request, invoke the provider, validate, record.

The runtime never advances workflow state. It returns a validated contract model
or raises :class:`ProviderError`; the orchestrator decides what that means.
"""

from __future__ import annotations

import asyncio
import uuid
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from crew.agents.contracts import CONTRACTS, output_schema
from crew.config.schema import CrewConfig
from crew.context.collector import SECRET_GLOBS
from crew.domain.enums import ExecutionStatus, Role
from crew.domain.errors import BudgetExceededError, ProviderError
from crew.persistence.models import JsonMixin, Task
from crew.persistence.store import Store
from crew.providers.base import AgentProvider, AgentRequest, AgentResult, Capabilities
from crew.security.redaction import redact, redact_data

M = TypeVar("M", bound=BaseModel)

# Below this, a Claude execution cannot do useful work; treat the budget as spent.
MIN_EXECUTION_BUDGET_USD = 0.01


@cache
def role_instructions(role: Role) -> str:
    prompts = files("crew.agents") / "prompts"
    shared = (prompts / "shared.md").read_text(encoding="utf-8")
    specific = (prompts / f"{role.value}.md").read_text(encoding="utf-8")
    return f"{specific}\n\n{shared}"


def capabilities_for(role: Role, config: CrewConfig) -> Capabilities:
    claude = config.provider.claude_code
    deny = tuple(SECRET_GLOBS) + tuple(config.context.exclude)
    if role is Role.IMPLEMENTER:
        return Capabilities(
            can_edit_files=True,
            bash_rules=tuple(claude.implementer_bash_rules),
            deny_read_globs=deny,
        )
    if role is Role.REVIEWER:
        return Capabilities(
            can_edit_files=False,
            bash_rules=tuple(claude.reviewer_bash_rules),
            deny_read_globs=deny,
        )
    return Capabilities(can_edit_files=False, deny_read_globs=deny)


def _usage_fields(result: AgentResult) -> dict[str, Any]:
    meta = result.metadata
    models = meta.get("models") or []
    return {
        "num_turns": meta.get("num_turns"),
        "model": ",".join(models)[:120] or None,
        "usage_json": JsonMixin.dumps(meta["usage"]) if meta.get("usage") else None,
    }


class AgentRuntime:
    def __init__(self, store: Store, provider: AgentProvider, config: CrewConfig) -> None:
        self.store = store
        self.provider = provider
        self.config = config

    def execution_budget(self, run_id: str, role: Role) -> float | None:
        """Spending cap for the next execution, or ``None`` when unlimited.

        Raises :class:`BudgetExceededError` once the run budget is used up. The
        remaining run budget is split between concurrent Implementers so that
        parallel executions cannot overspend it together.
        """
        per_call = self.config.provider.claude_code.max_budget_usd
        run_budget = self.config.execution.max_run_budget_usd
        if run_budget is None:
            return per_call
        spent = self.store.run_cost(run_id)
        remaining = run_budget - spent
        if remaining <= MIN_EXECUTION_BUDGET_USD:
            raise BudgetExceededError(
                f"run budget exhausted: ${spent:.2f} of ${run_budget:.2f} spent",
                hint="Raise execution.max_run_budget_usd, then `crew retry <run-id>`.",
            )
        if role is Role.IMPLEMENTER:
            remaining /= self.config.execution.max_implementers
        return min(per_call, remaining) if per_call else remaining

    async def invoke(
        self,
        role: Role,
        *,
        run_id: str,
        prompt: str,
        cwd: Path,
        cycle: int = 0,
        task: Task | None = None,
        execution_id: str | None = None,
    ) -> BaseModel:
        execution_id = execution_id or uuid.uuid4().hex[:16]
        budget = self.execution_budget(run_id, role)
        request = AgentRequest(
            role=role,
            instructions=role_instructions(role),
            prompt=prompt,
            cwd=cwd,
            capabilities=capabilities_for(role, self.config),
            output_schema=output_schema(role),
            run_id=run_id,
            execution_id=execution_id,
            task_key=task.key if task else None,
            max_budget_usd=budget,
        )
        self.store.start_execution(
            run_id=run_id,
            role=role,
            provider=self.provider.name,
            cwd=str(cwd),
            prompt=prompt,
            cycle=cycle,
            task=task,
            execution_id=execution_id,
        )

        try:
            result = await self.provider.execute(
                request,
                timeout_seconds=self.config.provider.claude_code.timeout_seconds,
                on_process_start=lambda pid, started: self.store.set_execution_pid(
                    execution_id, pid, started
                ),
            )
        except asyncio.CancelledError:
            self.store.finish_execution(execution_id, status=ExecutionStatus.CANCELLED)
            raise
        except ProviderError as exc:
            status = ExecutionStatus.TIMED_OUT if exc.kind == "timeout" else ExecutionStatus.FAILED
            self.store.finish_execution(execution_id, status=status, error=redact(exc.message))
            raise
        except Exception as exc:  # OSError from spawning, bugs in an adapter, ...
            self.store.finish_execution(
                execution_id, status=ExecutionStatus.FAILED, error=f"provider crashed: {exc!r}"
            )
            raise ProviderError(f"provider crashed: {exc!r}", kind="internal") from exc

        try:
            model = CONTRACTS[role].model_validate(result.output)
        except ValidationError as exc:
            message = f"{role.value} output failed schema validation: {exc.error_count()} error(s)"
            self.store.finish_execution(
                execution_id,
                status=ExecutionStatus.FAILED,
                error=f"{message}\n{exc}"[:20_000],
                raw_output=JsonMixin.dumps(redact_data(result.output))[:100_000],
                session_id=result.session_id,
                cost_usd=result.cost_usd,
                duration_seconds=result.duration_seconds,
                **_usage_fields(result),
            )
            raise ProviderError(message, kind="invalid_output") from exc

        self.store.finish_execution(
            execution_id,
            status=ExecutionStatus.SUCCEEDED,
            output_json=model.model_dump_json(),  # redacted structurally by the store
            raw_output=result.raw_text[:100_000],
            session_id=result.session_id,
            cost_usd=result.cost_usd,
            duration_seconds=result.duration_seconds,
            **_usage_fields(result),
        )
        return model
