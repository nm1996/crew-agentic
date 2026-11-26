"""Structured output contracts for each agent role (docs/AGENT_ROLES.md).

Provider output is untrusted: it is validated against these models before CREW
acts on it. The JSON Schema of each model is also handed to the provider so it
can constrain generation (Claude Code ``--json-schema``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from crew.domain.enums import FindingCategory, Recommendation, Role, Severity


class _Contract(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


# ---- Director ---------------------------------------------------------------


class PlannedTask(_Contract):
    id: str = Field(description="Short lowercase ref-safe id, e.g. 'add-health-endpoint'")
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=10)
    rationale: str = ""
    dependencies: list[str] = Field(default_factory=list, description="ids of prerequisite tasks")
    acceptance_criteria: list[str] = Field(min_length=1)
    expected_files: list[str] = Field(default_factory=list)


class DirectorPlan(_Contract):
    understanding: str = Field(min_length=10, description="Requested outcome in your own words")
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    approach: str = Field(min_length=10, description="Architecture / design approach")
    scope: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    tasks: list[PlannedTask] = Field(min_length=1, max_length=12)
    risks: list[str] = Field(default_factory=list)
    verification_strategy: str = ""
    blocking_ambiguity: str | None = Field(
        default=None,
        description="Set only if the request is too ambiguous to plan safely; explain why.",
    )


# ---- Implementer ------------------------------------------------------------


class VerificationRecord(_Contract):
    command: str
    exit_code: int | None = None
    summary: str = Field(description="What you observed. Never claim a run you did not perform.")


class ImplementationReport(_Contract):
    summary: str = Field(min_length=5)
    changed_files: list[str] = Field(default_factory=list)
    tests_changed: list[str] = Field(default_factory=list)
    verification: list[VerificationRecord] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    scope_notes: str = Field(default="", description="Why scope changed, if it did")


# ---- Reviewer ---------------------------------------------------------------


class Finding(_Contract):
    severity: Severity
    category: FindingCategory
    title: str = Field(min_length=3)
    file: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    explanation: str = Field(min_length=5)
    evidence: str = ""
    recommendation: str = ""
    blocking: bool

    @property
    def location(self) -> str:
        if not self.file:
            return "-"
        if self.line_start and self.line_end and self.line_end != self.line_start:
            return f"{self.file}:{self.line_start}-{self.line_end}"
        if self.line_start:
            return f"{self.file}:{self.line_start}"
        return self.file


class ReviewReport(_Contract):
    assessment: str = Field(min_length=5)
    findings: list[Finding] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)

    @property
    def blocking_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]


# ---- External Evaluator -----------------------------------------------------


class RequirementAssessment(_Contract):
    requirement: str
    status: Literal["met", "partially_met", "not_met", "unverifiable"]
    evidence: str = ""


class EvaluationReport(_Contract):
    recommendation: Recommendation
    requirements: list[RequirementAssessment] = Field(min_length=1)
    unresolved_risks: list[str] = Field(default_factory=list)
    discrepancies: list[str] = Field(default_factory=list)
    explanation: str = Field(min_length=5)


CONTRACTS: dict[Role, type[_Contract]] = {
    Role.DIRECTOR: DirectorPlan,
    Role.IMPLEMENTER: ImplementationReport,
    Role.REVIEWER: ReviewReport,
    Role.EVALUATOR: EvaluationReport,
}


def output_schema(role: Role) -> dict:
    return CONTRACTS[role].model_json_schema()
