# CREW — Workflow and State Machine

## 1. Standard workflow

```text
CREATED
  -> ANALYZING
  -> PLANNING
  -> AWAITING_PLAN_APPROVAL
  -> IMPLEMENTING
  -> INTEGRATING
  -> REVIEWING
       |-> CHANGES_REQUESTED -> IMPLEMENTING
       |-> EXTERNAL_EVALUATION
  -> AWAITING_FINAL_APPROVAL
  -> COMPLETED
```

Any active state may transition to FAILED or CANCELLED through controlled error/cancellation handling. A rejected plan transitions to CANCELLED or a dedicated PLAN_REJECTED state; choose one representation and document it consistently.

## 2. State definitions

- `CREATED`: run persisted, not yet started.
- `ANALYZING`: repository and request context are being collected.
- `PLANNING`: Director is producing a plan and task graph.
- `AWAITING_PLAN_APPROVAL`: plan is persisted; no implementation may start.
- `IMPLEMENTING`: eligible tasks are executing.
- `INTEGRATING`: completed task work is being assembled in an isolated integration workspace.
- `REVIEWING`: Reviewer evaluates integrated changes.
- `CHANGES_REQUESTED`: review has actionable blocking findings.
- `EXTERNAL_EVALUATION`: independent evaluator checks requirements and evidence.
- `AWAITING_FINAL_APPROVAL`: all automated stages finished; developer decision required.
- `COMPLETED`: developer accepted the result.
- `FAILED`: unrecoverable execution or internal error.
- `CANCELLED`: developer cancelled the run.
- `PLAN_REJECTED`: developer rejected the proposed plan.

## 3. Transition invariants

- Implementation cannot begin before plan approval.
- A task cannot start until all dependencies are completed.
- A task cannot be dispatched more than once concurrently.
- Active Implementer count cannot exceed configured concurrency (maximum 2 for MVP).
- Integration cannot start while required tasks are incomplete.
- Review must inspect the integrated diff, not only agent summaries.
- External evaluation must receive the original request and approved plan.
- Completion requires explicit final developer approval.
- A failed or cancelled run must retain its evidence and history.
- Resume must revalidate repository, worktree, and process state before continuing.

## 4. Task model

Each task should include:
- stable ID;
- title and clear description;
- rationale;
- dependencies (task IDs);
- acceptance criteria;
- expected areas/files, if known;
- status;
- assigned worker/execution ID;
- worktree and branch references;
- changed files and commit reference;
- verification commands and results;
- timestamps and failure details.

Task statuses:
`PENDING`, `READY`, `RUNNING`, `COMPLETED`, `FAILED`, `BLOCKED`, `CANCELLED`.

A task is `READY` only when all dependencies are `COMPLETED`.

## 5. Review and correction loop

Reviewer findings should have:
- severity: `BLOCKER`, `HIGH`, `MEDIUM`, `LOW`;
- category: correctness, security, data integrity, concurrency, performance, architecture, maintainability, tests, documentation;
- location: file and line/range when available;
- concrete explanation;
- reproduction or evidence;
- recommended correction;
- blocking flag.

Only blocking findings should automatically return the run to implementation. Non-blocking observations remain in the report unless the developer chooses to address them.

Limit automatic correction cycles with a configurable small default (for example, 2). When the limit is reached, stop and request developer input. Never loop indefinitely.

## 6. Failure and recovery

Persist before and after each meaningful stage and task dispatch. On startup/resume:
- identify runs left in active states;
- mark orphaned process executions as interrupted;
- inspect worktree paths and Git branches;
- reconcile task state conservatively;
- do not assume a process completed just because a worktree contains changes;
- ask for developer intervention if safe recovery cannot be established.

Use idempotency keys or unique constraints to prevent duplicate task dispatch after a crash.

## 7. Cancellation

Cancellation is cooperative:
- stop dispatching new tasks;
- request active provider processes to terminate;
- wait for a bounded grace period;
- record processes that could not be stopped;
- preserve worktrees and diffs for inspection;
- never delete uncommitted work automatically.
