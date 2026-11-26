# Implementation decisions

Decisions the specifications left open, recorded in short ADR form. Each entry says what
was chosen and why. Change an entry when the implementation changes.

## D1. Python 3.12 baseline, uv, hatchling

- `requires-python = ">=3.12"`, `.python-version` pins 3.12 for development (the oldest
  supported version, so CI catches use of newer features). Verified on 3.12.3.
- `uv` manages the environment and lockfile (`uv.lock`); `hatchling` builds the wheel.
- Runtime dependencies: typer, rich, pydantic, sqlalchemy, alembic, pyyaml (YAML config),
  psutil (portable process trees and identity, added in 0.2.0).
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

- The Director runs in a detached worktree at the base commit, Implementers in their task
  worktrees, and Reviewer and Evaluator in disposable detached *snapshot* worktrees at the
  integration tip (`review-<sha>`, `evaluate-<sha>`). Nothing a read-only agent or a tool
  it runs writes can leak into the next integration merge; the integration worktree must
  be clean before every merge. Combined with `--restricted`, file tools are confined to
  CREW-owned directories.
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
| Implementer | adds `Edit,Write` and `--permission-mode acceptEdits`; `Bash`/`PowerShell` only with configured scoped rules |
| Confinement | `--restricted` — **mandatory** since 0.2.0 (`restricted: Literal[True]`): confines file tools, ignores user/project/local settings files (so repository hooks and permission rules cannot widen access), refuses bypass |
| No foreign MCP servers | `--strict-mcp-config` (no `--mcp-config` given, so no MCP servers at all) |
| Secrets | `--disallowedTools Read(**/.env),…` |
| Spend | `--max-budget-usd` = min(per-call setting, remaining run budget) |
| Hygiene | `--no-session-persistence`, role prompt via `--append-system-prompt` |

**Argument order:** security flags come first and free text (`--json-schema`,
`--append-system-prompt`) last. If a free-text argument were ever mangled by a platform
quoting layer, nothing after it can be a security flag.

**Windows shims:** npm installs `claude.cmd`. Windows runs batch files through cmd.exe,
which stops at the first newline in the command line (the role prompt has many) and
interprets `& | ^ %`. CREW therefore never executes a `.cmd`/`.bat`/`.ps1` shim: it
resolves it to `node <prefix>/node_modules/@anthropic-ai/claude-code/cli.js`, or refuses
with a hint to install the native `claude.exe`.

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
- `FAILED` is terminal for the engine, but `crew retry <id>` (0.2.0) moves the run back to
  the stage recorded in `runs.failed_state`, giving failed/blocked/cancelled tasks of the
  current cycle fresh attempts in new worktrees. The retry is recorded as a `RETRY`
  approval. FAILED may only exit into working stages, never past a developer gate.
- Unexpected exceptions inside one task fail that task only; unexpected provider errors
  are recorded and normalised to `ProviderError`; a verification command that cannot be
  spawned is recorded as exit 127 evidence.
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
- Redaction is applied centrally by the store on every write: `*_json` columns are
  redacted **structurally** (string leaves) so they stay valid JSON — regex redaction of
  serialised JSON could eat escape characters and corrupt it — and free-text columns as
  text. Markdown artifacts are redacted before writing. Process environments are never
  stored.

## D12. Process environment

- Verification commands get a minimal allow-listed environment (plus the Windows system
  variables such as `SYSTEMROOT`, `PATHEXT`, `TEMP` that programs need to start at all).
- The Claude CLI gets the parent environment minus well-known unrelated credentials
  (`AWS_SECRET*`, `GITHUB_TOKEN`, `GH_TOKEN`, `NPM_TOKEN`, `PYPI_*`), because it needs its
  own auth/config variables.

## D13. Platform layer (`crew.system`)

All OS-specific code lives in `src/crew/system/`:

- **Process trees:** children start in their own session (POSIX) or with
  `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` (Windows). Termination walks the tree with
  psutil (terminate → wait → kill) and on POSIX also signals the process group. It also
  runs after a *normal* exit, so background grandchildren an agent started do not outlive
  it.
- **Identity:** the provider process creation time is stored
  (`agent_executions.process_started_at`, migration 0002); recovery only signals a PID
  whose creation time and command line still match, never a reused PID.
- **Locking:** `fcntl.flock` on POSIX, `msvcrt.locking` on Windows; both are released by
  the OS when the holder dies.
- **Termination signals:** SIGTERM and SIGHUP (POSIX) and SIGBREAK (Windows) are handled
  like Ctrl-C, so asyncio cancellation stops agent trees before CREW exits.
- psutil is the only new dependency; Job Objects (ctypes) were left out for now (see
  known limitations).

## D14. Git policy

- Every git call disables hooks (`core.hooksPath=<devnull>`), fsmonitor and auto-gc, and
  on Windows enables `core.longpaths`. Neither the developer's nor an agent's hooks run as
  a side effect of CREW's git calls.
- Network and destructive commands are always refused. *Write* commands (commit, merge,
  add, rm, …) are refused outside CREW-owned worktrees; from the developer's root CREW may
  only read and run `git worktree add/remove/list`.
- Output is decoded as UTF-8 with replacement; paths are parsed from `-z` output.
- `commit_all` keeps *newly created* files matching a narrow list of key/credential
  patterns (`.env`, `*.pem`, `*.key`, `id_rsa`, …; templates like `.env.example` are
  allowed) or larger than 10 MB out of commits and reports them as an event.
- All git commands made for a run are recorded in `command_executions` (`purpose=git`),
  attributed through a context variable set while a run is locked.
- Branch deletion exists only as a prefix-checked primitive used by `crew cleanup`.

## D15. Cancellation

Cancellation requests are checked before the gate test (so a stop during planning cannot
survive the gate and fire after a later approval), polled every 0.5 s during *every*
agent execution and verification command, and an immediate `crew stop` (no live driver)
reconciles leftover agent processes and tasks first.

## D16. Run budget

`execution.max_run_budget_usd` caps the sum of provider-reported costs of a run. Each
execution receives the remaining budget as `--max-budget-usd`; for Implementers it is
divided by `max_implementers`, so parallel executions cannot overspend together. Once
exhausted (≤ $0.01 left) no agent starts, budget errors are not retried, and the run fails
with a clear reason; raise the budget and `crew retry`. Costs are as reported by the
provider; the fake provider reports 0 unless configured.

## D17. Cleanup rules

`crew cleanup <id>|--all` only acts on finished runs and only removes worktrees without
uncommitted work. With `--branches`: task branches are deleted once contained in the
integration branch (or when the run was rejected/cancelled); the integration branch of a
COMPLETED run only after it was merged into the base branch; FAILED runs keep all
branches so `crew retry` keeps working.

## D18. Reserved task ids

Correction tasks are named `crew-fix-<cycle>`; plans may not use the `crew-` prefix, so a
Director task can never collide with a CREW-generated one.

## Known limitations

- **Verification commands execute agent-written code.** `execution.verification_commands`
  run in the integration worktree with your user's privileges, and Implementers can edit
  what those commands execute (`conftest.py`, `package.json` scripts, `Makefile`, …). Even
  with no Bash rules, configuring verification therefore grants agent-authored code
  execution outside Claude's permission system. Review the diff of test/build
  configuration when in doubt; a sandboxed runner is on the roadmap.
- A Git worktree is isolation of files, not a security sandbox. Agents with Bash rules
  can run whatever those rules match, with your user's privileges. Grant narrow rules.
- Claude Code discovers `CLAUDE.md` files upward from its working directory; worktrees
  live inside your repository, so your root `CLAUDE.md` is visible to agents (usually
  desirable). Treat it like any other repository content: data, not authority.
- On Windows, grandchildren whose parent already exited cannot be found by tree walking;
  a Job Object based kill is planned.
- Secret redaction is pattern-based and can miss unusual formats.
- Claude authentication cannot be verified without an API call; `doctor` only detects
  credentials heuristically.
- `PowerShell(...)` rules are passed through to Claude Code but have not been verified
  against a Windows Claude Code build yet.
- One real provider (Claude Code CLI). The provider interface is ready for others.
