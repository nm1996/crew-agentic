"""Provider-neutral execution contract (docs/TECHNICAL_SPEC.md §4).

Orchestration code depends only on these types. Provider adapters translate
them to a concrete backend and must never leak backend-specific objects.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from crew.domain.enums import Role


@dataclass(frozen=True)
class Capabilities:
    """What the agent may do. Enforced by the adapter's backend flags, not prompts."""

    can_edit_files: bool
    bash_rules: tuple[str, ...] = ()
    deny_read_globs: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentRequest:
    role: Role
    instructions: str  # role system prompt
    prompt: str  # task-specific input payload
    cwd: Path  # always a CREW-owned worktree
    capabilities: Capabilities
    output_schema: dict[str, Any]
    run_id: str
    execution_id: str
    task_key: str | None = None


@dataclass
class AgentResult:
    output: dict[str, Any]  # parsed JSON, validated by the runtime against the role contract
    raw_text: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    duration_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


ProcessStarted = Callable[[int], None]


class AgentProvider(Protocol):
    name: str

    async def execute(
        self,
        request: AgentRequest,
        *,
        timeout_seconds: int,
        on_process_start: ProcessStarted | None = None,
    ) -> AgentResult:
        """Run one agent turn. Raise :class:`crew.domain.errors.ProviderError` on failure."""
        ...
