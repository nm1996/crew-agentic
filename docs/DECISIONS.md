# Implementation decisions

Decisions the specifications left open, recorded in short ADR form. Each entry says what
was chosen and why. Change an entry when the implementation changes.

## D1. Python 3.12 baseline, uv, hatchling

- `requires-python = ">=3.12"`, `.python-version` pins 3.12 for development (the oldest
  supported version, so CI catches use of newer features). Verified on 3.12.3.
- `uv` manages the environment and lockfile (`uv.lock`); `hatchling` builds the wheel.
- Runtime dependencies: typer, rich, pydantic, sqlalchemy, alembic, pyyaml (YAML config).
  Logging uses the standard library only (no structlog).

## D2. Rejections are explicit terminal states

- A rejected plan ends in `PLAN_REJECTED`; a rejected final result ends in `REJECTED`.
  `CANCELLED` is reserved for `crew stop`. This keeps history and reports unambiguous.

## D3. Final-approval commands are `accept` / `reject`

The spec named only plan-approval commands. The final gate uses `crew accept <id>` and
`crew reject <id>`, symmetrical with `approve-plan` / `reject-plan`. Approval is
command-based (no interactive prompts) so it is scriptable and works after a restart.

## D4. Integration strategy: merge commits into a CREW integration branch

- Each run gets `crew/<run>/integration`, created from the base commit.
- Each task attempt gets its own branch `crew/<run>/<task>-a<attempt>` and worktree,
  started from the integration tip. Dependencies' branches are merged into the task
  worktree first, so dependent tasks build on their prerequisites.
- After implementation, every completed task branch is merged into the integration branch
  with `git merge --no-ff` in topological order. One merge commit per task preserves
  provenance, and Git does conflict detection. On conflict the merge is aborted (in the
  CREW-owned worktree only), the run fails, and every worktree is preserved.
- Cherry-picking was rejected: it rewrites commit identity and makes provenance harder to
  follow. Rebasing was rejected for the same reason.
- CREW commits on behalf of the Implementer (`git add --all && git commit` in the task
  worktree, author `CREW <crew@localhost>`, `--no-verify`). Implementers are told not to
  commit; if they do anyway, the change is still captured because diffs are computed
  from the task's start commit.

## D5. Agents never run in the developer's worktree

- The Director runs in a detached worktree at the base commit, Reviewer and Evaluator in
  the integration worktree, Implementers in their task worktrees. Combined with
  `--restricted`, file tools are confined to CREW-owned directories.
- Planning context comes from the base commit, so uncommitted local files (including
  `.env`) are invisible to agents.
- A dirty developer tree is allowed for planning (with a warning) but blocks
  `approve-plan`.

## D6. Claude Code CLI invocation

Flags verified against `claude --help` (CLI 2.1.319):

| Concern | Flags |
|---|---|
| Non-interactive structured output | `-p --output-format json --json-schema <schema>` (prompt on stdin) |
| No escalation mid-run | `--permission-prompts none` |
| Read-only roles | `--tools Read,Glob,Grep --allowedTools Read,Glob,Grep --permission-mode dontAsk` |
| Implementer | adds `Edit,Write` and `--permission-mode acceptEdits`; `Bash` only with configured scoped rules |
| Confinement | `--restricted` (default on) |
| Secrets | `--disallowedTools Read(**/.env),…` |
| Hygiene | `--no-session-persistence`, role prompt via `--append-system-prompt` |

Never used: `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`,
`--permission-mode bypassPermissions`.

Output parsing prefers `structured_output`, then falls back to a JSON object in `result`
(bare or fenced). Any non-success envelope, non-zero exit without output, timeout, or
schema mismatch becomes a normalised `ProviderError`.

## D7. Verification evidence is produced by CREW, not agents

Agents' claims about tests are recorded and labelled as *claims*. The trustworthy evidence
is `execution.verification_commands`, which CREW runs itself after each integration. This
is what makes "do not rely solely on Implementer claims" enforceable.

## D8. Failure handling

- A failed task attempt (provider error, timeout, invalid output) is retried in a fresh
  worktree until `execution.max_task_attempts` is reached; then it is `FAILED`,
  dependents become `BLOCKED`, independent tasks still finish, and the run ends `FAILED`.
- `FAILED` runs are terminal; start a new run after fixing the cause. Worktrees are kept.
- Director plans that fail graph validation (bad ids, unknown dependencies, cycles) are
  sent back once with the validation errors; a second failure fails the run.
- Unexpected exceptions (bugs) fail the run with the error recorded and logged with a
  traceback in `.crew/logs/crew.log`.

## D9. Correction-limit behaviour

When blocking findings remain after `max_correction_cycles`, CREW does not loop or fail.
It records `correction_limit_reached`, still runs the External Evaluator, and stops at the
final gate with the unresolved findings highlighted in the report: the developer decides.
`NEEDS_CHANGES` or `REJECT` from the evaluator likewise stop at the final gate (advisory).

## D10. Concurrency model and locking

- The workflow engine is synchronous; only stages that call providers use `asyncio`.
  Implementation runs at most `max_implementers` provider processes concurrently, under a
  polling scheduler (0.5 s) that also observes cancellation requests.
- Exactly one process may drive a run: an `flock` on `.crew/runs/<id>/.lock`. The kernel
  releases it when the process dies, so crashes never leave stale locks.
- Duplicate dispatch is prevented by a compare-and-set update (`PENDING|READY → RUNNING`)
  and a unique `(task, attempt)` constraint on agent executions.

## D11. Persistence details

- SQLite in WAL mode with `foreign_keys=ON`; one Alembic migration per schema change,
  applied automatically when any command opens the project.
- State transitions and their workflow events (and any tasks created by the transition)
  are written in a single transaction. Events are append-only.
- Prompts, outputs, command arguments and output are redacted before being stored.
  Process environments are never stored.

## D12. Process environment

- Verification commands get a minimal allow-listed environment.
- The Claude CLI gets the parent environment minus well-known unrelated credentials
  (`AWS_SECRET*`, `GITHUB_TOKEN`, `GH_TOKEN`, `NPM_TOKEN`, `PYPI_*`), because it needs its
  own auth/config variables.

## Known limitations

- A Git worktree is isolation of files, not a security sandbox. Agents with Bash rules
  can run whatever those rules match, with your user's privileges. Grant narrow rules.
- Secret redaction is pattern-based and can miss unusual formats.
- Claude authentication cannot be verified without an API call; `doctor` only detects
  credentials heuristically.
- No automatic retry of a `FAILED` run; resume works for interrupted active runs only.
- Linux-first. Orphan detection on resume reads `/proc`; on other platforms orphaned
  processes are only marked interrupted.
- One real provider (Claude Code CLI). The provider interface is ready for others.
