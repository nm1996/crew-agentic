"""Deterministic offline provider for tests and demos (``provider.default: fake``).

Each role is handled by a replaceable function, so tests can script plans,
review findings, failures or slow executions without any network access.
The default Implementer writes ``crew_fake/<task>.md`` in its worktree so every
task produces a real, reviewable diff.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from crew.domain.enums import Role
from crew.providers.base import AgentRequest, AgentResult, ProcessStarted

Handler = Callable[[AgentRequest], dict[str, Any] | Awaitable[dict[str, Any]]]


def default_director(request: AgentRequest) -> dict[str, Any]:
    def task(task_id: str, title: str, deps: list[str]) -> dict[str, Any]:
        return {
            "id": task_id,
            "title": title,
            "description": f"{title} (fake provider task)",
            "rationale": "Demonstrates scheduling",
            "dependencies": deps,
            "acceptance_criteria": [f"crew_fake/{task_id}.md exists"],
            "expected_files": [f"crew_fake/{task_id}.md"],
        }

    return {
        "understanding": "Fake plan generated offline for the given request.",
        "assumptions": ["The fake provider is used; no real code changes are planned."],
        "open_questions": [],
        "approach": "Three small tasks: two independent, one depending on the first.",
        "scope": ["Fake files under crew_fake/"],
        "non_goals": ["Real implementation"],
        "tasks": [
            task("part-a", "Create part A", []),
            task("part-b", "Create part B", []),
            task("part-c", "Create part C on top of A", ["part-a"]),
        ],
        "risks": [],
        "verification_strategy": "Check that each file exists.",
    }


def default_implementer(request: AgentRequest) -> dict[str, Any]:
    key = request.task_key or "task"
    target = request.cwd / "crew_fake" / f"{key}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"# {key}\n\nWritten by the fake implementer.\n", encoding="utf-8")
    return {
        "summary": f"Created {target.relative_to(request.cwd)}",
        "changed_files": [str(target.relative_to(request.cwd))],
        "tests_changed": [],
        "verification": [],
        "limitations": ["Fake provider: no tests were run by the agent."],
    }


def default_reviewer(request: AgentRequest) -> dict[str, Any]:
    return {
        "assessment": "No defects found by the fake reviewer.",
        "findings": [],
        "checks_run": [],
    }


def default_evaluator(request: AgentRequest) -> dict[str, Any]:
    return {
        "recommendation": "ACCEPT",
        "requirements": [
            {"requirement": "Original request", "status": "met", "evidence": "Fake evaluation."}
        ],
        "unresolved_risks": [],
        "discrepancies": [],
        "explanation": "Fake evaluator accepts by default.",
    }


@dataclass
class FakeProvider:
    handlers: dict[Role, Handler] = field(
        default_factory=lambda: {
            Role.DIRECTOR: default_director,
            Role.IMPLEMENTER: default_implementer,
            Role.REVIEWER: default_reviewer,
            Role.EVALUATOR: default_evaluator,
        }
    )
    delay_seconds: float = 0.0
    name: str = "fake"
    calls: list[AgentRequest] = field(default_factory=list)
    active: int = 0
    max_active: dict[Role, int] = field(default_factory=dict)

    def on(self, role: Role, handler: Handler) -> FakeProvider:
        self.handlers[role] = handler
        return self

    async def execute(
        self,
        request: AgentRequest,
        *,
        timeout_seconds: int,
        on_process_start: ProcessStarted | None = None,
    ) -> AgentResult:
        self.calls.append(request)
        self.active += 1
        self.max_active[request.role] = max(self.max_active.get(request.role, 0), self.active)
        try:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            result = self.handlers[request.role](request)
            if asyncio.iscoroutine(result):
                result = await result
            return AgentResult(output=result, raw_text="(fake)", cost_usd=0.0)
        finally:
            self.active -= 1
