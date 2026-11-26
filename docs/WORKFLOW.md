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
       |-> COMPLETED
       |-> REJECTED

AWAITING_PLAN_APPROVAL -> PLAN_REJECTED
```

Any active (non-terminal) state may transition to FAILED or CANCELLED through controlled error/cancellation handling.

**Implemented representation** (see `src/crew/orchestrator/state_machine.py` and [DECISIONS.md](DECISIONS.md) D2): a rejected plan ends in the terminal state `PLAN_REJECTED`; a rejected final result ends in the terminal state `REJECTED`; `CANCELLED` is used only for `crew stop`. Terminal states: `COMPLETED`, `REJECTED`, `PLAN_REJECTED`, `FAILED`, `CANCELLED`. Gate states, where the engine stops and waits for a developer command: `AWAITING_PLAN_APPROVAL`, `AWAITING_FINAL_APPROVAL`.

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
- `REJECTED`: developer rejected the final result.

`FAILED` is terminal for the engine; the developer may retry it with `crew retry`, which transitions `FAILED → <stage it failed in>` (one of ANALYZING, PLANNING, IMPLEMENTING, INTEGRATING, REVIEWING, CHANGES_REQUESTED, EXTERNAL_EVALUATION; never a gate). The stage is stored in `runs.failed_state`.

Developer commands per gate: `crew approve-plan` / `crew reject-plan` at `AWAITING_PLAN_APPROVAL`; `crew accept` / `crew reject` at `AWAITING_FINAL_APPROVAL`.

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

**Implemented:** each `CHANGES_REQUESTED` creates one correction task `crew-fix-<cycle>` (the `crew-` prefix is reserved and rejected in plans) containing all blocking findings, started from the current integration tip, and increments `runs.correction_cycle` (default limit `execution.max_correction_cycles: 2`). When the limit is reached with blocking findings left, the run proceeds to `EXTERNAL_EVALUATION` and then stops at `AWAITING_FINAL_APPROVAL` with a `correction_limit_reached` event and the unresolved findings highlighted in the report: that gate is the "request developer input". The evaluator's recommendation is advisory and never changes state by itself.

## 6. Failure and recovery

Persist before and after each meaningful stage and task dispatch. On startup/resume:
- identify runs left in active states;
- mark orphaned process executions as interrupted;
- inspect worktree paths and Git branches;
- reconcile task state conservatively;
- do not assume a process completed just because a worktree contains changes;
- ask for developer intervention if safe recovery cannot be established.

Use idempotency keys or unique constraints to prevent duplicate task dispatch after a crash.

**Implemented** (`src/crew/orchestrator/recovery.py`): executions left `RUNNING` become `INTERRUPTED` and a still-alive orphaned agent process (verified via `/proc/<pid>/cmdline`) is terminated; tasks left `RUNNING` return to `PENDING` and get a new attempt in a fresh worktree/branch while the old worktree is preserved; completed tasks must still have their commits; a half-finished merge in the CREW integration worktree is aborted; a missing integration branch after implementation stops recovery with an explanation. Dispatch uses a compare-and-set update plus a unique `(task_pk, attempt)` constraint. One driver per run is enforced with an `flock`.

Task attempts: a failed attempt is retried until `execution.max_task_attempts` (default 2); then the task is `FAILED`, dependents become `BLOCKED`, independent tasks still finish, and the run ends `FAILED`.

## 7. Cancellation

Cancellation is cooperative:
- stop dispatching new tasks;
- request active provider processes to terminate;
- wait for a bounded grace period;
- record processes that could not be stopped;
- preserve worktrees and diffs for inspection;
- never delete uncommitted work automatically.

**Implemented:** `crew stop` cancels immediately when no process holds the run lock (after reconciling leftover processes and tasks); otherwise it sets `runs.cancel_requested`, which the driving process polls every 0.5 s during every agent execution and verification command, and before checking gates. Active provider process trees are terminated (terminate, then kill after a 5 s grace period). Interrupting or terminating CREW itself (Ctrl-C, SIGTERM, SIGHUP, Ctrl-Break) is *not* a cancellation: agent trees are stopped and tasks are left resumable.
