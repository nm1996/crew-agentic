"""Validated project configuration (``.crew/config.yaml``).

Hard safety limits live here, in code, so that editing YAML cannot weaken them:
approval gates cannot be disabled and implementer concurrency cannot exceed
``MAX_IMPLEMENTERS_HARD_LIMIT``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONFIG_VERSION = 1
MAX_IMPLEMENTERS_HARD_LIMIT = 2
MAX_CORRECTION_CYCLES_HARD_LIMIT = 5


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectSettings(_Strict):
    name: str = Field(min_length=1)


class ClaudeCodeSettings(_Strict):
    executable: str = "claude"
    timeout_seconds: int = Field(default=1800, ge=30, le=4 * 3600)
    model: str | None = None
    max_budget_usd: float | None = Field(default=None, gt=0)
    # --restricted confines file tools to the working directory and refuses
    # permission bypass. Keep it on unless your CLI version does not support it.
    restricted: bool = True
    # Bash permission rules granted to Implementers in addition to file edits,
    # in Claude Code permission-rule syntax, e.g. "Bash(uv run pytest:*)".
    implementer_bash_rules: list[str] = Field(default_factory=list)
    reviewer_bash_rules: list[str] = Field(default_factory=list)

    @field_validator("implementer_bash_rules", "reviewer_bash_rules")
    @classmethod
    def _only_scoped_bash(cls, rules: list[str]) -> list[str]:
        for rule in rules:
            if not (rule.startswith("Bash(") and rule.endswith(")")) or rule in {
                "Bash(*)",
                "Bash(:*)",
            }:
                raise ValueError(
                    f"invalid bash rule {rule!r}: use a scoped rule such as 'Bash(uv run pytest:*)'"
                )
        return rules


class ProviderSettings(_Strict):
    default: Literal["claude_code", "fake"] = "claude_code"
    claude_code: ClaudeCodeSettings = Field(default_factory=ClaudeCodeSettings)


class ExecutionSettings(_Strict):
    max_implementers: int = Field(default=2, ge=1, le=MAX_IMPLEMENTERS_HARD_LIMIT)
    max_correction_cycles: int = Field(default=2, ge=0, le=MAX_CORRECTION_CYCLES_HARD_LIMIT)
    max_task_attempts: int = Field(default=2, ge=1, le=3)
    command_timeout_seconds: int = Field(default=600, ge=5, le=3 * 3600)
    command_output_limit_bytes: int = Field(default=200_000, ge=1_000, le=5_000_000)
    preserve_worktrees_on_failure: bool = True
    # Commands CREW itself runs in the integration worktree as test evidence.
    # Parsed with shlex, executed without a shell.
    verification_commands: list[str] = Field(default_factory=list)


class GitSettings(_Strict):
    branch_prefix: str = "crew"
    worktree_directory: str = ".crew/worktrees"

    @field_validator("branch_prefix")
    @classmethod
    def _safe_prefix(cls, value: str) -> str:
        if not value or value.startswith(("-", "/")) or ".." in value or " " in value:
            raise ValueError("branch_prefix must be a simple ref component such as 'crew'")
        return value.rstrip("/")

    @field_validator("worktree_directory")
    @classmethod
    def _inside_crew(cls, value: str) -> str:
        if not value.startswith(".crew/") or ".." in value:
            raise ValueError("worktree_directory must live under .crew/ (CREW-owned paths only)")
        return value.rstrip("/")


class ApprovalSettings(_Strict):
    # Literal[True]: the MVP does not allow disabling mandatory approval gates.
    require_plan_approval: Literal[True] = True
    require_final_approval: Literal[True] = True


class ContextSettings(_Strict):
    max_files_listed: int = Field(default=400, ge=10, le=5000)
    # Glob patterns never included in collected context (in addition to built-ins).
    exclude: list[str] = Field(default_factory=list)


class CrewConfig(_Strict):
    version: Literal[1] = CONFIG_VERSION
    project: ProjectSettings
    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    git: GitSettings = Field(default_factory=GitSettings)
    approvals: ApprovalSettings = Field(default_factory=ApprovalSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
