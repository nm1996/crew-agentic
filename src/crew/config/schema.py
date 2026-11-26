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
SHELL_RULE_PREFIXES = ("Bash(", "PowerShell(")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectSettings(_Strict):
    name: str = Field(min_length=1)


class ClaudeCodeSettings(_Strict):
    executable: str = "claude"
    timeout_seconds: int = Field(default=1800, ge=30, le=4 * 3600)
    model: str | None = None
    max_budget_usd: float | None = Field(default=None, gt=0)
    # Always passed as --restricted. Kept as a key only so older configs that set
    # it still load; it cannot be turned off.
    restricted: Literal[True] = True
    # Bash permission rules granted to Implementers in addition to file edits,
    # in Claude Code permission-rule syntax, e.g. "Bash(uv run pytest:*)".
    implementer_bash_rules: list[str] = Field(default_factory=list)
    reviewer_bash_rules: list[str] = Field(default_factory=list)

    @field_validator("implementer_bash_rules", "reviewer_bash_rules")
    @classmethod
    def _only_scoped_bash(cls, rules: list[str]) -> list[str]:
        for rule in rules:
            scoped = rule.startswith(SHELL_RULE_PREFIXES) and rule.endswith(")")
            inner = rule[rule.find("(") + 1 : -1].strip()
            if not scoped or inner in {"", "*", ":*"}:
                raise ValueError(
                    f"invalid shell rule {rule!r}: use a scoped rule such as "
                    "'Bash(uv run pytest:*)' (or 'PowerShell(...)' on Windows)"
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
    # Each entry is a string (split like a shell, never run by one) or an
    # explicit argv list such as ["python", "-m", "pytest"] (most portable).
    verification_commands: list[str | list[str]] = Field(default_factory=list)


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
