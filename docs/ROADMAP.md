# CREW — MVP Roadmap

Build in vertical slices. Keep the application runnable at every milestone.

## Status

| Milestone | Status | Notes |
|---|---|---|
| M0 Foundation | Done | Package, CLI, config, `init`, `doctor`, logging, ruff, pytest. |
| M1 Domain and persistence | Done | ORM models, Alembic `0001`, validated state machine, atomic transitions + events. |
| M2 Provider and agent contracts | Done | Provider protocol, fake provider, Claude Code CLI adapter (flags verified), role contracts. |
| M3 Planning and approvals | Done | Context collection with exclusions, DAG validation with one corrective retry, plan gate. |
| M4 Worktrees and implementation | Done | Clean-tree precondition, per-task worktrees, dependency-aware scheduler (≤2), merge integration, conflict detection. |
| M5 Review and evaluation | Done | Structured findings, bounded corrections, independent evaluator, final gate, reports. |
| M6 Recovery and hardening | Done (0.1.0) | Resume/reconcile, cancellation, orphan handling, redaction tests, e2e tests, docs. |
| M7 Hardening and cross-platform | Done (0.2.0) | See below. |

## M7 — Hardening and cross-platform (0.2.0)

Driven by an independent review of 0.1.0. Each item links to its decision record.

| Item | Status | Decision |
|---|---|---|
| Structured, central secret redaction (JSON stays valid) | Done | D11 |
| Cancellation before gates, in every stage, and on immediate stop | Done | D15 |
| Clean failure of tasks/executions on unexpected errors | Done | D8 |
| Git policy: UTF-8, `-z`, writes only in CREW worktrees, hooks off, commit filter, recorded commands | Done | D14 |
| Claude isolation: mandatory `--restricted`, `--strict-mcp-config`, security flags first | Done | D6 |
| Reserved `crew-` task ids; snapshot worktrees for Reviewer/Evaluator | Done | D5, D18 |
| Platform layer: process trees, PID identity, portable lock, signal handling | Done | D13 |
| Windows conventions: `claude.cmd` shim, PATHEXT, argv-list verification commands, env | Done | D6, D12 |
| CI on Linux, macOS and Windows × Python 3.12/3.13 (incl. autocrlf) | Done | – |
| Per-run budget | Done | D16 |
| `crew retry` for FAILED runs | Done | D8 |
| `crew cleanup` | Done | D17 |

**Exit condition:** full suite green on all three operating systems; real Claude Code smoke
run passes on Linux.

## M8 — Sandboxing, Windows trees, real-CLI checks, releases (0.3.0)

| Item | Status | Decision |
|---|---|---|
| Sandboxed verification (`execution.sandbox: none\|bwrap\|docker`), fail-closed | Done | D19 |
| Windows Job Objects (kill-on-close agent trees) | Done | D13 |
| Scoped shell rules verified against the real CLI; opt-in smoke workflow on all OSes | Done (Linux verified; Windows/macOS need the API-key secret) | D20 |
| Tag-driven GitHub releases | Done | Releases |

## M9 — Interactive permissions and integration adapters

Goal: CREW works with the systems around the code (issue tracker, code host, review,
documentation) through small, explicit adapters, and an agent that lacks a permission
can ask for it instead of failing.

### 1. Ask for a missing permission

Today agents run with `--permission-prompts none`: anything outside the configured rules
is denied and the agent must work around it. Instead:

- CREW serves a local permission-prompt tool (`--permission-prompt-tool`, an MCP tool in
  a CREW-owned helper process) that receives each denied request (tool + arguments).
- The request is classified with the existing execution policy. DESTRUCTIVE and NETWORK
  requests stay denied; everything else is shown to the developer: *allow once*,
  *allow for this run* (added as a scoped rule), or *deny*.
- Decisions are recorded as approvals (kind `PERMISSION`) and in the report. Without an
  interactive terminal (CI, background runs) the behaviour stays "deny".
- Concurrency: requests from parallel Implementers are queued; the agent waits (bounded
  by its timeout).

### 2. Integration adapters

A small adapter interface per integration kind, next to `AgentProvider`, configured in
`.crew/config.yaml` with credentials taken from environment variables (never stored):

| Kind | Interface (sketch) | First adapters |
|---|---|---|
| Issue tracker | `fetch_issue(key) -> Issue`, `comment(key, text)`, `transition(key, state)` | Jira (REST v3) |
| Code host | `push_branch(branch)`, `open_pull_request(branch, title, body) -> url` | GitHub (`gh`), GitLab |
| Code review | `fetch_review_comments(pr) -> [Comment]`, `post_review(pr, findings)` | GitHub reviews |
| Documentation | `fetch_page(ref) -> Markdown`, `publish_page(space, title, markdown)` | Confluence (REST), local Markdown folder |

How they fit the workflow:

- `crew run --issue PROJ-123` loads the issue (title, description, acceptance criteria)
  as the request; linked Confluence pages are added to the Director's context through the
  existing bounded context collector.
- `crew publish <id> [--pr]` (explicit command, COMPLETED runs only) pushes the
  integration branch and opens a PR whose body is the final report; the Reviewer's
  findings can be posted as review comments.
- On completion, an optional comment/transition is sent to the issue and the report can
  be published as a documentation page.
- Every outbound call is NETWORK in the execution policy: it happens only through these
  adapters, only on explicit commands or configured completion hooks, and is recorded as
  an event. "Never push automatically" stays true.

### 3. Measured reference example — done

[Spring Boot CRUD](examples/spring-boot-crud.md): COMPLETED in 14 min for $3.34, 69
integration tests, 25/25 independent API checks. Findings that feed back into CREW:

- **Per-role models and cost profiles** — done in 0.4.0 (D21): economy $1.28, balanced
  $2.87 for the same, equally working service.
- **Compact review context** — done in 0.4.0 (`execution.review_context: summary`).
- **Merge-conflict resolution and usage-limit handling** — done in 0.4.0 (D22), found by
  the balanced runs.
- **Optional runtime check:** a configurable "start and probe" verification step (e.g.
  start the service, wait for a port, run a probe command), so the Evaluator does not
  have to mark startup as unverifiable.
- Fixed during the run: run ids were truncated in `crew history` (0.3.1).

## M10 — Candidates

Ordered by value; none started.

1. **Claude Agent SDK provider** behind `AgentProvider`, with programmatic `can_use_tool`
   permission decisions and streaming progress.
2. **Observability:** `stream-json` tool-use events per execution, `crew logs -f`.
3. **Interactive approvals** (`crew run --interactive`), later a TUI.
4. **PyPI release** with trusted publishing on tags.

## M0 — Foundation

- Create Python package with `uv`.
- Add Typer CLI and `crew --version`.
- Add configuration schema and loader.
- Implement `crew init` without overwriting existing files.
- Implement `crew doctor` for Python, Git, Claude CLI, and project paths.
- Add logging, error model, formatting/linting, and pytest setup.
- Add `CLAUDE.md` and initial docs.

**Exit condition:** installable local CLI; init and doctor work; tests run offline.

## M1 — Domain and persistence

- Define Run, Task, Approval, AgentExecution, WorkflowEvent models.
- Add SQLite and Alembic.
- Implement validated state machine.
- Implement append-only workflow events.
- Add repository/service interfaces.

**Exit condition:** create/read runs and tasks; reject illegal transitions; state and event writes are atomic.

## M2 — Provider and agent contracts

- Define provider-neutral request/result contracts.
- Implement fake provider.
- Implement Claude Code CLI adapter after verifying installed CLI flags and output format.
- Add structured output validation and process timeout handling.
- Add role prompt templates and output schemas.

**Exit condition:** each role can be invoked through the same provider interface; tests use fake provider by default.

## M3 — Planning and approvals

- Implement context collection with exclusions.
- Implement Director analysis and task graph generation.
- Validate task IDs, dependencies, cycles, and acceptance criteria.
- Persist plan artifacts.
- Add plan approval/rejection commands.

**Exit condition:** planning produces an inspectable plan and implementation remains blocked until approval.

## M4 — Worktrees and controlled implementation

- Implement clean-worktree precondition.
- Create isolated worktree per task.
- Implement dependency-aware scheduler.
- Enforce maximum two active Implementers.
- Record task outcomes and commits.
- Implement CREW-owned integration workspace and conflict detection.

**Exit condition:** independent tasks run concurrently in isolation; conflicts stop safely; user branch remains untouched.

## M5 — Review, correction, and external evaluation

- Implement Reviewer contract and structured findings.
- Add bounded correction cycles.
- Implement independent External Evaluator.
- Add final approval gate.
- Generate complete Markdown report.

**Exit condition:** complete fake-provider workflow passes with review corrections and external assessment.

## M6 — Recovery, hardening, and release

- Add process interruption and resume handling.
- Add cancellation and orphan-process handling.
- Add security/redaction tests.
- Add end-to-end temporary-repository tests.
- Verify Linux behavior and avoid platform assumptions where practical.
- Write installation and usage docs.
- Package and tag an MVP release.

**Exit condition:** acceptance criteria in `PRODUCT_SPEC.md` pass.

## Development rules

- Do not start later milestones while earlier invariants are broken.
- Do not add features because a framework makes them easy.
- Prefer a working vertical slice over broad scaffolding.
- Keep the first provider Claude Code CLI only.
- Keep fake-provider tests fast and deterministic.
- Revisit architecture only when implementation evidence requires it.
