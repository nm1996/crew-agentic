# CREW — Technical Specification

## 1. Runtime and dependencies

Target Python 3.12+. During setup, verify installed Python versions and dependency compatibility; do not assume a particular interpreter exists.

Use `uv` for project/dependency management.

Suggested dependencies:
- `typer` for CLI.
- `rich` for terminal output.
- `pydantic` for configuration and agent-output schemas.
- `sqlalchemy` for persistence.
- `alembic` for migrations.
- `pytest` and `pytest-asyncio` for tests.
- Optional: `structlog` or standard-library logging. Choose one; do not add both without a reason.

Use Python `asyncio` for bounded concurrency. Use the Git executable as the source of truth for Git operations. A GitPython dependency is optional; prefer a small, explicit subprocess wrapper if that keeps behavior clearer.

Do not add LangChain, CrewAI, or LangGraph as core dependencies. CREW owns its workflow and scheduling logic.

## 2. CLI contract

Initial commands:
- `crew --version`
- `crew init`
- `crew doctor`
- `crew configure`
- `crew run "<request>"`
- `crew plan <run-id>`
- `crew approve-plan <run-id>`
- `crew reject-plan <run-id>`
- `crew status [run-id]`
- `crew tasks <run-id>`
- `crew show <run-id>`
- `crew resume <run-id>`
- `crew stop <run-id>`
- `crew report <run-id>`

Prefer explicit approval commands for MVP. Interactive approval can be added later. Commands must return meaningful exit codes and avoid printing secrets.

## 3. Configuration

Store project configuration at `.crew/config.yaml`. Validate it with Pydantic.

Example shape (implementation may refine names, but document any changes):

```yaml
version: 1
project:
  name: my-project
provider:
  default: claude_code
  claude_code:
    executable: claude
    timeout_seconds: 1800
execution:
  max_implementers: 2
  max_correction_cycles: 2
  command_timeout_seconds: 600
  preserve_worktrees_on_failure: true
git:
  integration_branch_prefix: crew/integration
  worktree_directory: .crew/worktrees
approvals:
  require_plan_approval: true
  require_final_approval: true
```

Validate hard safety limits in code. Configuration must not allow disabling mandatory approval gates in MVP.

## 4. Provider interface

Define a provider-neutral interface, conceptually:

```python
class AgentProvider(Protocol):
    async def execute(
        self,
        request: AgentRequest,
        *,
        timeout_seconds: int,
    ) -> AgentResult: ...
```

`AgentRequest` should contain role, instructions, input payload, working directory, allowed capabilities, and correlation IDs. `AgentResult` should contain structured output, process metadata, usage metadata if available, and normalized error information.

Do not leak Claude-specific response objects into domain models.

## 5. Claude Code CLI adapter

- Detect executable with a safe `which`/path check.
- Invoke with a documented non-interactive mode and structured/stream output supported by the installed CLI version.
- Confirm exact flags against the installed CLI's `--help` during implementation; do not guess or hardcode undocumented flags.
- Use `asyncio.create_subprocess_exec`, not shell string interpolation.
- Set working directory to the assigned workspace.
- Enforce timeout and terminate process groups safely.
- Capture stdout/stderr with size limits and secret redaction.
- Parse structured output defensively; preserve raw output only when safe and useful.
- Return normalized provider errors.
- Do not use a blanket permission-bypass option.
- Do not assume the CLI is installed or authenticated; `crew doctor` must diagnose prerequisites.

Keep provider invocation isolated so a future Claude Agent SDK adapter or another provider can be added without rewriting orchestration.

## 6. Persistence

Use SQLite via SQLAlchemy. Suggested entities:
- `Run`
- `Task`
- `AgentExecution`
- `Approval`
- `WorkflowEvent`
- `CommandExecution`
- `Artifact`

Use Alembic migrations from the first persistent schema. Enable SQLite foreign keys. Use transactions for state changes and associated event writes.

Every state transition should atomically update the current state and append a workflow event. Store timestamps in UTC.

Do not store API keys, environment dumps, or raw secret-bearing process environments.

## 7. Git and worktrees

- Require a Git repository for implementation runs.
- Inspect and record initial branch, HEAD, and working-tree status.
- If the user's working tree is dirty, stop before implementation and explain options. Do not silently stash or commit user changes.
- Create one isolated worktree/branch per Implementer task.
- Keep all CREW-created worktrees under `.crew/` or another clearly owned directory.
- Integrate into a CREW-owned integration branch/worktree, never directly into the user's active branch.
- Detect overlapping file modifications and merge conflicts.
- Preserve failed/conflicted worktrees for inspection.
- Never run destructive cleanup commands against user-owned paths.
- Never push, merge to the user's branch, or deploy automatically.

The exact integration strategy (merge commits versus cherry-pick) must be selected and documented during implementation. Prefer the simplest strategy that preserves task provenance and handles conflicts safely.

## 8. Reports and artifacts

Store run artifacts under `.crew/runs/<run-id>/` or an equivalent consistent layout:
- `request.md`
- `plan.md`
- `implementation.md`
- `review.md`
- `external-evaluation.md`
- `test-results.md`
- `final-report.md`

The SQLite database is authoritative for workflow state. Markdown artifacts are human-readable evidence and exports, not a competing state store.

## 9. Testing strategy

- Unit tests for state transitions, task DAG validation, scheduler limits, configuration validation, output parsing, redaction, and Git command construction.
- Integration tests with a temporary Git repository and fake provider.
- End-to-end tests that run the complete workflow without network access.
- A small opt-in Claude smoke test, skipped unless explicitly enabled and credentials are available.
- Failure tests: provider timeout, malformed output, process termination, interrupted run, missing worktree, dirty repository, dependency failure, merge conflict, cancellation.
- Avoid tests that merely mirror implementation details. Test observable behavior and invariants.
