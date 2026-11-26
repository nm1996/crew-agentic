# Usage

## Walkthrough

```bash
crew run "Add a /health endpoint that returns build info, with tests"
```

CREW records the base branch and commit, collects bounded context from the **last commit**
(tracked files only), and asks the Director for a plan. The run stops at
`AWAITING_PLAN_APPROVAL`:

```bash
crew plan 20261126-113753-5b15          # tasks, dependencies, acceptance criteria, risks
crew approve-plan 20261126-113753-5b15  # or: crew reject-plan <id> -m "reason"
```

Approval requires a clean working tree. CREW then, in the foreground:

1. creates an integration branch `crew/<run>/integration` from the base commit;
2. runs up to two Implementers at a time, each in its own worktree and branch
   (`crew/<run>/<task>-a<attempt>`), starting only tasks whose dependencies are complete;
3. commits each task's changes in its worktree and merges every task branch into the
   integration branch (`git merge --no-ff`), stopping safely on conflicts;
4. runs your `execution.verification_commands` in the integration worktree and records
   the results as evidence;
5. asks the Reviewer for structured findings; blocking findings create a correction task
   (bounded by `execution.max_correction_cycles`);
6. asks the External Evaluator to assess the result against the original request;
7. stops at `AWAITING_FINAL_APPROVAL`.

```bash
crew report <id>                       # final-report.md: tasks, commits, tests, findings, evaluation
git diff <base>...crew/<id>/integration
crew accept <id> -m "LGTM"             # or: crew reject <id> -m "reason"
git merge --no-ff crew/<id>/integration   # CREW never merges for you
```

## Commands

| Command | Purpose |
|---|---|
| `crew --version` | Print version. |
| `crew init [--name N] [--provider claude_code\|fake]` | Create `.crew/` in the current repository. Idempotent, never overwrites. |
| `crew doctor` | Diagnose Python, Git, repository, config, database and Claude CLI. Exit 1 on any FAIL. |
| `crew configure [key=value ...]` | Show config, or set dotted keys (validated before writing). Values are YAML: `2`, `true`, `["a", "b"]`. |
| `crew run ["<request>"] [--issue KEY] [--doc REF]… [--pr-comments N]` | Create a run (from text, an issue or PR review comments, with linked docs), analyse, plan, stop at the plan gate. |
| `crew publish <id> [--pr] [--draft] [--review] [--docs] [--no-issue] [--yes]` | Share a COMPLETED run: PR, review, issue comment/transition, report page. See [INTEGRATIONS.md](INTEGRATIONS.md). |
| `crew plan <id>` | Show the plan. |
| `crew approve-plan <id> [-m note] [--no-continue]` | Approve and execute through to the final gate (`--no-continue`: record approval only, continue with `resume`). |
| `crew reject-plan <id> [-m note]` | End the run as `PLAN_REJECTED`. |
| `crew accept <id> [-m note]` | Final approval → `COMPLETED`. |
| `crew reject <id> [-m note]` | Final rejection → `REJECTED`. |
| `crew status [id]` | State and cost of a run (default: latest). |
| `crew tasks <id>` | Task table: status, dependencies, attempts, commits. |
| `crew history [--limit N]` | Previous runs. |
| `crew show <id> [--json]` | Events, agent executions, artifacts. `--json` for scripting. |
| `crew resume <id>` | Reconcile and continue an interrupted run. |
| `crew retry <id> [-m note]` | Retry a `FAILED` run from the stage it failed in; failed tasks get fresh attempts. |
| `crew stop <id>` | Cancel: immediately if no process is driving the run, otherwise cooperatively. |
| `crew cleanup <id>\|--all [--branches]` | Remove finished runs' clean worktrees; `--branches` also deletes merged CREW branches. |
| `crew report <id> [--path]` | Regenerate and display `final-report.md`. |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (including stopping at a gate). |
| 1 | Generic error, run ended `FAILED`, or `doctor` found a failure. |
| 2 | Not initialised / invalid configuration. |
| 3 | Precondition failed (dirty tree, no commits, not a Git repo). |
| 4 | Invalid workflow transition (e.g. `accept` before the final gate). |
| 5 | Run or task not found. |
| 6 | Run locked by another CREW process. |
| 7 | Execution-policy violation. |
| 8 | Git error. |
| 9 | Provider error. |
| 10 | Invalid plan. |
| 11 | Run budget exhausted (`execution.max_run_budget_usd`). |
| 12 | Unresolvable merge conflict. |
| 13 | Integration error (missing credentials, HTTP error from Jira/GitHub/Confluence). |
| 130 | Interrupted (Ctrl-C); state preserved for `crew resume`. |

## Run states

```text
CREATED → ANALYZING → PLANNING → AWAITING_PLAN_APPROVAL ─┬→ IMPLEMENTING → INTEGRATING → REVIEWING ─┬→ EXTERNAL_EVALUATION → AWAITING_FINAL_APPROVAL ─┬→ COMPLETED
                                                         └→ PLAN_REJECTED              ↑               │                                                  └→ REJECTED
                                                                                       └ CHANGES_REQUESTED ←┘
any active state → FAILED | CANCELLED
```

Details: [WORKFLOW.md](WORKFLOW.md).

## Run artifacts

`.crew/runs/<run-id>/` holds human-readable evidence (the database is authoritative):

| File | Contents |
|---|---|
| `request.md` | Request, base branch/commit, provider. |
| `context.md` | Context given to the Director. |
| `plan.md` | The Director's plan. |
| `implementation.md` | Per-task branch, commit, files and the Implementer's *claims*. |
| `test-results.md` | Verification commands CREW itself ran, with output. |
| `review.md` | Findings per review cycle. |
| `external-evaluation.md` | Requirement-by-requirement assessment and recommendation. |
| `final-report.md` | Everything above summarised, plus decisions and next steps. |

## Interruption and recovery

Killing CREW (Ctrl-C, `kill`, closed terminal, reboot) never corrupts a run: state changes
are transactional and SQLite runs in WAL mode. To continue:

```bash
crew status            # see where it stopped
crew resume <run-id>
```

Resume first reconciles: agent executions left running are marked `INTERRUPTED` (an orphaned
`claude` process is terminated), interrupted tasks are reset and re-run in a **fresh**
worktree (the old one is kept for inspection), completed tasks must still have their
commits, and a half-finished merge in the integration worktree is aborted. If safe
recovery cannot be established, CREW stops and tells you why.

## Retrying a failed run

A run ends `FAILED` when, for example, an agent keeps timing out, returns invalid output,
the budget or your Claude usage limit runs out, or a merge conflict cannot be resolved. Its worktrees and branches are preserved.
After fixing the cause (raise the timeout or budget, fix credentials, …):

```bash
crew retry <run-id> -m "raised timeout"
```

CREW records the retry as a developer decision and continues from the stage it failed in.
Completed tasks are kept; failed, blocked and cancelled tasks of the current cycle get a
new attempt in a fresh worktree. A retry never skips a gate. Retrying a merge conflict
fails again unless something changed: start a new run with a plan that avoids overlapping
tasks.

## Cancellation

`crew stop <run-id>` cancels immediately when no CREW process is driving the run. If one
is (e.g. another terminal is running the workflow), it sets a flag; within about half a
second the driver stops whatever is running — Director, Implementers, Reviewer,
Evaluator or a verification command — terminates the agent process trees and ends the run
as `CANCELLED`. Worktrees and branches are preserved.

CREW also stops its agents when it is terminated itself (Ctrl-C, SIGTERM, closed terminal
on POSIX, Ctrl-Break on Windows). If it is killed hard (SIGKILL, power loss), leftover agent
processes are found and stopped the next time you `resume`, `retry` or `stop` the run.

## Cleanup

After `accept`, `reject` or `reject-plan`, CREW removes its own worktrees that contain no
uncommitted work; branches are kept. Failed or cancelled runs keep everything (unless
`execution.preserve_worktrees_on_failure: false`, which still only removes clean ones).

```bash
crew cleanup <run-id>              # remove clean worktrees of a finished run
crew cleanup --all --branches      # every finished run, plus merged CREW branches
```

Branch rules with `--branches`: task branches are deleted once their work is in the
integration branch; the integration branch of a completed run only after you merged it;
FAILED runs keep all branches so they can still be retried. Worktrees with uncommitted
changes are never removed.
