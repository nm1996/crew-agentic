# CREW

**CREW is a local-first AI development orchestration CLI.** It coordinates specialized AI agents to plan, implement, review, and independently evaluate software changes inside a Git repository.

The MVP is a Python CLI, Claude-first, provider-extensible, and designed for one developer working locally. CREW is an orchestrator, not an autonomous replacement for the developer: important decisions and risky actions require explicit approval.

## Product principles

- Local-first: project state and history live in the target repository under `.crew/`.
- Human-controlled: the developer approves plans and final changes.
- Explicit roles: Director, Implementer, Reviewer, and External Evaluator have distinct responsibilities.
- Safe execution: permissions are enforced by code, not only by agent prompts.
- Recoverable: workflow state survives process termination.
- Small MVP: SQLite, local processes, Git worktrees, and clear interfaces. No distributed infrastructure.

## MVP capabilities

- Initialize a project with `crew init`.
- Diagnose local prerequisites with `crew doctor`.
- Run a request through analysis, planning, implementation, review, and independent evaluation.
- Require approval before implementation and before final completion.
- Run at most two implementation workers concurrently.
- Isolate implementation tasks in Git worktrees.
- Persist tasks, workflow state, events, and reports in SQLite.
- Resume interrupted runs.
- Generate Markdown reports.
- Use Claude Code CLI as the first provider backend.
- Use a fake provider in tests so the suite does not require API access.

## Non-goals for MVP

- Cloud service or hosted dashboard.
- Multi-user collaboration.
- Kubernetes, Redis, Kafka, NATS, or microservices.
- Automatic push, merge, release, or deployment.
- Unrestricted shell access.
- Supporting every AI provider from day one.
- A sophisticated terminal UI.

## Documentation map

- `PRODUCT_SPEC.md` — product requirements and acceptance criteria.
- `ARCHITECTURE.md` — system boundaries and module design.
- `WORKFLOW.md` — workflow state machine and transitions.
- `AGENT_ROLES.md` — agent responsibilities and contracts.
- `TECHNICAL_SPEC.md` — Python stack, interfaces, persistence, and Git strategy.
- `SECURITY.md` — execution and approval safety requirements.
- `ROADMAP.md` — implementation milestones and delivery order.
- `CLAUDE.md` — instructions for Claude Code working in this repository.
- `CONTRIBUTING.md` — engineering conventions.

## Proposed technology baseline

Use Python **3.12 or newer**, subject to dependency compatibility verified during project setup. Use `uv` for environment and dependency management.

Core stack:
- Typer — CLI.
- Rich — terminal output.
- Pydantic — configuration and structured agent output validation.
- SQLAlchemy + SQLite — persistence.
- Alembic — schema migrations.
- pytest — tests.
- asyncio — bounded concurrent orchestration.
- Git CLI — authoritative Git operations; use a thin wrapper rather than hiding Git semantics.

Claude Code CLI is an external executable and must be installed and authenticated separately. CREW must diagnose its availability and report actionable errors.
