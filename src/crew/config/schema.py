"""Validated project configuration (``.crew/config.yaml``).

Hard safety limits live here, in code, so that editing YAML cannot weaken them:
approval gates cannot be disabled and implementer concurrency cannot exceed
``MAX_IMPLEMENTERS_HARD_LIMIT``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONFIG_VERSION = 1
MAX_IMPLEMENTERS_HARD_LIMIT = 2
MAX_CORRECTION_CYCLES_HARD_LIMIT = 5
SHELL_RULE_PREFIXES = ("Bash(", "PowerShell(")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectSettings(_Strict):
    name: str = Field(min_length=1)


Effort = Literal["low", "medium", "high", "xhigh", "max"]
RoleName = Literal["director", "implementer", "reviewer", "evaluator"]
CostProfile = Literal["default", "balanced", "economy"]


class RoleSettings(_Strict):
    """Per-role model and effort; ``None`` means "inherit"."""

    model: str | None = None  # alias such as "opus", "sonnet", "haiku", or a full model id
    effort: Effort | None = None  # passed as --effort


# Defaults each cost profile applies before explicit `roles` entries.
# Chosen from the measured reference run (docs/examples/spring-boot-crud.md):
# implementation dominates cost; review/evaluation read evidence and need less.
PROFILE_ROLES: dict[str, dict[str, RoleSettings]] = {
    "default": {},
    "balanced": {
        "director": RoleSettings(effort="medium"),
        "implementer": RoleSettings(effort="medium"),
        "reviewer": RoleSettings(model="sonnet", effort="medium"),
        "evaluator": RoleSettings(model="sonnet", effort="low"),
    },
    "economy": {
        "director": RoleSettings(model="sonnet", effort="medium"),
        "implementer": RoleSettings(model="sonnet", effort="medium"),
        "reviewer": RoleSettings(model="sonnet", effort="low"),
        "evaluator": RoleSettings(model="haiku", effort="low"),
    },
}


class ClaudeCodeSettings(_Strict):
    executable: str = "claude"
    timeout_seconds: int = Field(default=1800, ge=30, le=4 * 3600)
    model: str | None = None  # default model for every role
    # Cost profile: per-role model/effort defaults (see PROFILE_ROLES).
    profile: CostProfile = "default"
    # Explicit per-role overrides; win over the profile and over `model`.
    roles: dict[RoleName, RoleSettings] = Field(default_factory=dict)
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


def role_settings(settings: ClaudeCodeSettings, role: str) -> RoleSettings:
    """Effective model/effort for ``role``: explicit roles > profile > global model."""
    explicit = settings.roles.get(role) or RoleSettings()  # type: ignore[call-overload]
    profile = PROFILE_ROLES[settings.profile].get(role, RoleSettings())
    return RoleSettings(
        model=explicit.model or profile.model or settings.model,
        effort=explicit.effort or profile.effort,
    )


class ProviderSettings(_Strict):
    default: Literal["claude_code", "fake"] = "claude_code"
    claude_code: ClaudeCodeSettings = Field(default_factory=ClaudeCodeSettings)


class ExecutionSettings(_Strict):
    max_implementers: int = Field(default=2, ge=1, le=MAX_IMPLEMENTERS_HARD_LIMIT)
    max_correction_cycles: int = Field(default=2, ge=0, le=MAX_CORRECTION_CYCLES_HARD_LIMIT)
    max_task_attempts: int = Field(default=2, ge=1, le=3)
    # Total spend limit for one run (sum of provider-reported costs). None = no limit.
    max_run_budget_usd: float | None = Field(default=None, gt=0)
    command_timeout_seconds: int = Field(default=600, ge=5, le=3 * 3600)
    command_output_limit_bytes: int = Field(default=200_000, ge=1_000, le=5_000_000)
    preserve_worktrees_on_failure: bool = True
    # Commands CREW itself runs in the integration worktree as test evidence.
    # Parsed with shlex, executed without a shell.
    # Each entry is a string (split like a shell, never run by one) or an
    # explicit argv list such as ["python", "-m", "pytest"] (most portable).
    verification_commands: list[str | list[str]] = Field(default_factory=list)
    # How much of the integrated diff Reviewer and Evaluator get in their prompt:
    # "full" embeds the whole diff; "summary" embeds stats, the file list and the
    # first part of the patch, and they open files with their Read tool.
    review_context: Literal["full", "summary"] = "full"
    # Isolation for verification commands; see crew/security/sandbox.py.
    sandbox: Literal["none", "bwrap", "docker"] = "none"
    sandbox_image: str | None = None  # required for docker
    sandbox_network: bool = False
    sandbox_writable_paths: list[str] = Field(default_factory=list)  # e.g. ["~/.cache/uv"]

    @model_validator(mode="after")
    def _docker_needs_image(self) -> ExecutionSettings:
        if self.sandbox == "docker" and not self.sandbox_image:
            raise ValueError("execution.sandbox_image is required when sandbox is 'docker'")
        return self


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
