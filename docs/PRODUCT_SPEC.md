# CREW — Product Specification

## 1. Purpose

CREW coordinates a controlled software-development workflow using multiple AI roles. It turns a developer's request into an approved plan, delegates bounded implementation tasks, obtains a code review, and requests an independent evaluation before presenting the result.

CREW must make the work observable. The developer should be able to inspect what each agent was asked, what it returned, what commands ran, what changed, and why the workflow advanced or stopped.

## 2. Target user

A software developer working locally in an existing Git repository who wants AI assistance with meaningful engineering tasks while retaining control over architecture, source changes, and final acceptance.

## 3. Core use case

1. Developer runs `crew run "Add health checks to the API"`.
2. CREW inspects repository metadata and gathers relevant project context.
3. Director proposes an implementation plan and task breakdown.
4. Developer reviews and approves or rejects the plan.
5. CREW dispatches eligible tasks to one or two Implementers in isolated worktrees.
6. CREW integrates completed task branches in a controlled integration workspace.
7. Reviewer inspects the integrated diff and available test evidence.
8. If changes are requested, CREW creates bounded follow-up tasks and repeats implementation/review.
9. External Evaluator independently compares the result against the original request, approved plan, diff, tests, and review findings.
10. Developer reviews the final report and explicitly accepts or rejects completion.

## 4. Agent roles

- **Director**: repository analysis, architecture proposal, task decomposition, dependencies, acceptance criteria. Read-only with respect to project source.
- **Implementer**: implements one assigned task in its own worktree, adds or updates focused tests, reports changed files and verification.
- **Reviewer**: read-only review of the integrated change. Finds concrete defects and risks; does not silently modify code.
- **External Evaluator**: independent assessment against original requirements and evidence. Returns ACCEPT, REJECT, or NEEDS_CHANGES with reasons. It is advisory; the developer remains the authority.

See `AGENT_ROLES.md` for full contracts.

## 5. Functional requirements

### Project setup
- `crew init` creates `.crew/`, default configuration, context templates, and local database.
- Never overwrite existing project files without asking.
- `crew doctor` checks Python/runtime, Git, Claude Code CLI, authentication availability where detectable, repository status, and writable paths.
- `crew configure` supports editing provider and execution settings.

### Workflow
- `crew run "<request>"` creates and executes a run.
- Planning must stop at a plan-approval gate.
- Implementation must not start before plan approval.
- Maximum concurrent Implementers defaults to 2 and may not exceed the configured hard limit.
- Tasks with unmet dependencies must not be dispatched.
- Review findings must be actionable and linked to files/lines where possible.
- External evaluation must be independent of the Implementer's self-assessment.
- Final completion requires explicit developer approval.

### Inspection and recovery
- `crew status` displays current run and state.
- `crew tasks` lists tasks and their status.
- `crew history` lists previous runs.
- `crew show <run-id>` displays run details.
- `crew resume <run-id>` resumes a recoverable run after validating repository/worktree state.
- `crew stop <run-id>` requests graceful cancellation.
- `crew report <run-id>` renders or opens the Markdown report.

### Reports
Each run should produce a concise final report and retain stage-specific artifacts:
- request and run metadata;
- approved plan;
- task outcomes;
- changed files and commit references;
- commands and test results;
- review findings and resolutions;
- external evaluation;
- unresolved risks and final developer decision.

## 6. Non-functional requirements

- Python 3.12+ baseline; verify actual dependency support during implementation.
- Linux first, but avoid unnecessary platform-specific assumptions.
- Deterministic orchestration decisions where possible.
- Structured, validated agent outputs.
- Durable state transitions and append-only event history.
- Clear error messages and actionable diagnostics.
- No secrets in logs, prompts, reports, or SQLite.
- Unit tests must run without Claude credentials or network access.
- No silent destructive Git operations.
- Keep modules small and interfaces explicit.

## 7. MVP acceptance criteria

The MVP is not complete until all of the following work:

1. `crew init` creates a usable project configuration and database.
2. `crew doctor` detects missing Git/Claude CLI prerequisites and explains how to fix them.
3. A fake-provider end-to-end run can pass through all workflow stages.
4. A real Claude Code CLI adapter can execute at least one task in a controlled worktree.
5. Director output is schema-validated and cannot directly edit source files.
6. Plan approval blocks implementation until explicitly granted.
7. Two independent tasks can run concurrently; a third waits.
8. Task dependencies are respected.
9. Reviewer findings can trigger a correction cycle.
10. External Evaluator receives original requirements and evidence, not merely the Implementer's summary.
11. Killing the CREW process does not corrupt the run; a recoverable run can be resumed.
12. Git conflicts are detected and surfaced; user changes are never overwritten.
13. No automatic push, merge to the user's branch, release, or deployment occurs.
14. Final report contains enough evidence for a developer to judge the result.
