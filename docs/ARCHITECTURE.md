# CREW — Architecture

## 1. Architectural approach

Use a **modular monolith**. CREW is one Python application with explicit internal boundaries. Do not split it into services.

The CLI is an interface to an application layer. The workflow engine owns state transitions and scheduling. Agents are role definitions plus provider-backed executions. Provider adapters isolate Claude-specific invocation details. Persistence records durable state and events. Git/worktree management isolates source changes.

## 2. High-level components

```text
CLI (Typer / Rich)
       |
Application services
       |
Workflow Orchestrator ---- Approval / Policy
       |                         |
Task Scheduler              Execution Guard
       |                         |
Agent Runtime ------------- Provider Interface
       |                         |
Role Contracts             Claude Code CLI adapter
       |
Workspace / Git Manager
       |
Persistence (SQLite) + Event History
       |
Reports / Context
```

## 3. Responsibility boundaries

### CLI
- Parse arguments and render output.
- Call application services.
- Never implement workflow rules.
- Never call provider processes directly.

### Application layer
- Implements use cases such as initialize, start run, approve plan, resume, stop, and report.
- Coordinates repositories and domain services.
- Keeps command handlers thin.

### Orchestrator
- Owns workflow state.
- Validates every transition.
- Starts stages only when preconditions are satisfied.
- Persists state before and after meaningful operations.
- Handles cancellation, retry policy, and recovery.

### Scheduler
- Selects tasks whose dependencies are complete.
- Enforces concurrency limit (default 2).
- Prevents duplicate dispatch.
- Persists dispatch/start/finish events.
- Does not decide architecture or rewrite tasks.

### Agent runtime
- Builds role-specific instructions and bounded task context.
- Invokes a provider adapter.
- Parses and validates structured results.
- Records inputs/outputs with secret redaction.
- Does not independently advance workflow state.

### Provider adapter
- Defines a provider-neutral execution contract.
- Handles Claude CLI invocation, process lifecycle, output parsing, timeouts, and errors.
- Does not own task scheduling, Git integration, or approval policy.

### Workspace/Git manager
- Inspects repository state.
- Creates and removes CREW-owned worktrees.
- Captures diffs and commit references.
- Integrates work only in a CREW-owned integration workspace.
- Never resets, cleans, or checks out over the user's active worktree.

### Persistence
- Stores runs, tasks, transitions, approvals, agent executions, command records, and report metadata.
- Uses SQLite for MVP.
- Uses migrations for schema changes.
- Event history is append-only.

## 4. Suggested package layout

```text
src/crew/
  __init__.py
  cli/
    __init__.py
    app.py
    commands/
  application/
    services/
    dto/
  domain/
    models/
    enums.py
    errors.py
    policies.py
  orchestrator/
    engine.py
    state_machine.py
    scheduler.py
    recovery.py
  agents/
    runtime.py
    contracts.py
    prompts/
    roles/
  providers/
    base.py
    claude_code/
      adapter.py
      process.py
      parser.py
  workspace/
    repository.py
    worktrees.py
    integration.py
    diff.py
  persistence/
    database.py
    models.py
    repositories/
    migrations/
  context/
    collector.py
    project_files.py
  reporting/
    renderer.py
    templates/
  config/
    schema.py
    loader.py
    defaults.py
  security/
    approvals.py
    execution_policy.py
    redaction.py
  utils/
tests/
  unit/
  integration/
  e2e/
```

Adjust names only when implementation reveals a concrete reason. Avoid creating empty abstraction layers.

## 5. Data flow

1. CLI submits a user request to an application service.
2. Application service creates a Run and persists it.
3. Context collector gathers bounded repository context.
4. Director executes in read-only mode and returns a validated plan.
5. Orchestrator persists plan and transitions to approval state.
6. After approval, scheduler dispatches tasks.
7. Each Implementer works in a dedicated worktree.
8. Integration manager combines completed task commits in a CREW-owned integration worktree.
9. Reviewer evaluates integrated diff and test evidence.
10. Corrections return to scheduler with explicit task links.
11. External Evaluator independently evaluates the evidence.
12. Orchestrator waits for final developer approval and writes final report.

## 6. Design rules

- Domain and workflow logic must not depend on Typer or Rich.
- Domain and application layers must not depend on Claude-specific types.
- Provider output is untrusted input and must be validated.
- Agent prompts are not a security boundary.
- Every state transition must be validated and persisted.
- Every external process invocation must be recorded with redacted arguments/output metadata.
- Avoid global mutable state.
- Prefer straightforward functions and dataclasses/Pydantic models over speculative frameworks.
- Do not add a message broker or distributed task queue.
