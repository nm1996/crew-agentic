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
- **Windows Job Objects** (0.3.0, `crew/system/winjob.py`, ctypes): every agent and
  verification process is assigned to its own `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` job.
  Terminating the job ends grandchildren whose parent already exited, and Windows kills
  the tree if CREW dies. Processes a child spawns in the few milliseconds before it is
  assigned are still covered by psutil tree termination.
- psutil is the only new dependency.

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

## D19. Verification sandbox

`execution.sandbox` isolates the commands CREW runs as evidence, because they execute
code agents may have written:

| Mode | Isolation | Notes |
|---|---|---|
| `none` (default) | none | `doctor` warns when verification commands are configured. |
| `bwrap` (Linux) | read-only filesystem except the worktree, a private `/tmp` and `sandbox_writable_paths`; credential directories in `$HOME` (`.ssh`, `.aws`, `.config/gh`, `.claude`, …) hidden; all namespaces unshared, so **no network** unless `sandbox_network` | Read access to the rest of the filesystem remains; without network it cannot be exfiltrated. |
| `docker` | worktree at `/workspace` in `sandbox_image`, your UID, `--cap-drop ALL`, `no-new-privileges`, read-only root, `--network none` by default | The toolchain must be in the image; programs are not resolved on the host. |

Design choices:

- **Fail closed:** if the configured sandbox is unavailable, the run fails before any
  command runs (retryable), instead of silently running unsandboxed.
- Network is off by default; dependency installation inside verification therefore needs
  `sandbox_network: true` or a pre-populated cache bound via `sandbox_writable_paths`.
- The sandbox used is recorded per command (`command_executions.sandbox`, migration 0004)
  and shown in evidence and reports.
- Agents themselves are not sandboxed by CREW; they are confined by Claude Code's
  permission system (D6).

## D20. Real-CLI verification

The opt-in smoke tests (`tests/e2e/test_claude_smoke.py`) run planning, a full workflow
and a scoped-shell-rule workflow against the real Claude Code CLI. The
`Claude smoke` workflow (manual dispatch, needs the `ANTHROPIC_API_KEY` secret) runs them
on Linux, macOS and Windows with the native Claude Code build.

Observed on Linux (CLI 2.1.319): a scoped rule containing spaces
(`Bash(python3 --version:*)`) reaches the CLI intact and is enforced; a compound command
outside it was blocked. Claude Code itself auto-allows some read-only commands such as
`git status` without a rule.

## D21. Cost controls

The reference run showed every role on the most expensive model, Reviewer and Evaluator
spending a third of the cost on embedding the full diff, and Implementers using ~33 turns
per task. Controls added in 0.4.0:

- `provider.claude_code.profile` (`default` | `balanced` | `economy`) maps each role to a
  model alias and an `--effort` level; `provider.claude_code.roles` overrides per role.
  Precedence: explicit role > profile > global `model` > CLI default. `default` keeps the
  previous behaviour (no `--model`, no `--effort`).
- `execution.review_context: summary` replaces the embedded diff with stat, file list and
  the first 12 kB of the patch; read-only agents already run in a snapshot of the
  integration result and can open files themselves.
- The Implementer prompt asks to batch edits before building.
- Profiles change *which* model does the work, not the workflow or the safety gates.
  Quality differences are measured in the reference example, not assumed.

## D22. Merge conflicts and usage limits

- **Conflicts** while merging dependency branches into a task worktree, or task branches
  into the integration branch, are left in progress; with
  `execution.conflict_resolution: agent` (default) an Implementer edits the conflicted
  files with both sides' task reports as context. CREW — not the agent — verifies that no
  conflict markers remain and creates the merge commit; otherwise it aborts the merge and
  fails with `MergeConflictError` (not retried: the conflict is deterministic). `fail`
  restores the earlier stop-on-conflict behaviour.
- Changed files outside a task's `expected_files` are recorded (`scope_expanded`) and
  listed in `implementation.md`; they are the usual cause of conflicts between parallel
  tasks.
- **Usage and rate limits** (subscription windows, HTTP 429, overload) are classified as
  `rate_limited`: no retry, no new dispatches, completed tasks kept, and the run fails with
  a pointer to `crew retry` after the reset.
- Reported costs are API-list equivalents; on a subscription they are informational.

## D23. Integration adapters

- Three small protocols (`IssueTracker`, `CodeHost`, `DocsTarget`) next to
  `AgentProvider`; first adapters: Jira Cloud, GitHub, Confluence Cloud, and a Markdown
  folder that needs no credentials. A standard-library HTTP client with an injectable
  transport keeps adapters dependency-free and testable without network.
- Reads are explicit `crew run` options; writes happen only in `crew publish` for
  COMPLETED runs, after confirmation, and are recorded as events plus a `PUBLISH`
  approval. "Never push automatically" still holds: the only push is the developer's
  `crew publish --pr`, limited to the run's integration branch and never forced.
- Credentials are environment variables named in the config (GitHub falls back to
  `gh auth token`); they are never persisted. Integration URLs must be HTTPS.
- Rich-text formats are converted with small purpose-built converters (ADF for Jira,
  storage XHTML for Confluence) rather than new dependencies.
- Not yet: GitLab/Bitbucket, Jira Server/Data Center, inline PR review comments (the
  review is posted as one body to avoid position errors). Verified with fakes only; the
  real services have not been called because no credentials were available.

## D24. Permission prompts

- `approvals.permission_prompts: ask` passes CREW's MCP server to Claude Code as
  `--permission-prompt-tool mcp__crew__approve` (with `--permission-prompts host` and
  `--mcp-config`; `--strict-mcp-config` keeps every other server out). The flag is not
  listed in `claude --help` but is referenced by `--permission-prompts`; it was verified
  against CLI 2.1.319 by an opt-in smoke test.
- The server classifies shell requests with the execution policy (operators split even
  when glued to words) and denies DESTRUCTIVE/NETWORK without asking. Other requests are
  written to `.crew/runs/<id>/permissions/` and answered by the driver process, which asks
  the developer in a daemon thread (an unanswered prompt cannot block CREW from exiting).
- In ask mode Implementers and the Reviewer get the shell tool available but not
  pre-approved; Director and Evaluator stay read-only.
- Default `off`: unchanged behaviour (deny silently), suitable for unattended runs.

## D25. Fewer conflicts between parallel tasks

- **Shared files are ordered at plan time.** When two planned tasks list overlapping
  `expected_files` (same file, or one inside a listed directory) and neither already
  depends on the other, the later task in topological order gets a dependency on the
  nearest earlier one, so they form a chain instead of running in parallel. Doing it in
  the plan, before approval, keeps it visible (`plan.md`, `plan_serialized` events) and
  reuses the normal dependency semantics: the later task merges the earlier branch and is
  BLOCKED if it fails. Edges only point backwards in topological order, so no cycle can
  appear. `execution.serialize_shared_files: false` turns it off. The Director prompt
  asks for shared files (build manifests, routers, changelogs) to be listed, because an
  unlisted shared file cannot be ordered.
- **A conflict is resolved once.** When a task branch conflicts at integration but is
  already contained in a later COMPLETED task of the cycle (a diamond: c merged a and b
  and resolved them in its worktree), the merge is aborted and the branch arrives through
  that task. Otherwise a second, possibly different, resolution would happen and could
  conflict with the first. Such tasks are recorded as `integration_deferred` and marked
  integrated once their commit is in the integration branch; they have no merge commit
  of their own.
- **Resolution checks.** A bare `=======` line counts as a leftover marker only when the
  file has more of them than either side of the merge (it is also a Markdown/reST
  heading underline); start, end and diff3 base markers always count. Changes the resolver
  makes outside the conflicted files fail the resolution: CREW commits only the
  conflicted files, so those changes would otherwise stay uncommitted in the worktree
  that later merges and verification commands run in. The worktree is preserved.

## D26. Throughput and cost of parallel runs

- **Git off the event loop.** Task worktree creation, dependency merges, commits and
  conflict checks run in a thread (`asyncio.to_thread`) so the other Implementer's agent
  process keeps streaming meanwhile. They are serialized by one lock: concurrent git
  commands in one repository can fail on shared ref/worktree lock files. A cancelled task
  waits for its running git command before the lock is released.
- **Optional evaluation skip.** `execution.skip_evaluation_max_lines` skips the External
  Evaluator only when the integrated change is at most that many added+removed lines, at
  least one verification command ran in the cycle and all passed, and no
  `correction_limit_reached` event exists. The run still passes through
  `EXTERNAL_EVALUATION` (no state-machine change), the skip is an event, and both
  `external-evaluation.md` and the final report say why. Off by default: the Evaluator is
  advisory, the developer's final gate is unchanged.

## Releases

Version tags `vX.Y.Z` trigger `.github/workflows/release.yml`: the tag must equal the
package version, tests run, and a GitHub release is published with the wheel, the sdist
and the matching `CHANGELOG.md` section.

## Known limitations

- **Verification commands execute agent-written code.** Implementers can edit what those
  commands run (`conftest.py`, `package.json` scripts, `Makefile`, …). With
  `execution.sandbox: none` (the default) that code runs with your user's privileges.
  Use `bwrap` or `docker` (D19). Even in `bwrap`, the code can *read* files outside the
  hidden credential directories; it cannot write them or reach the network.
- A Git worktree is isolation of files, not a security sandbox. Agents with Bash rules
  can run whatever those rules match, with your user's privileges. Grant narrow rules.
- Claude Code discovers `CLAUDE.md` files upward from its working directory; worktrees
  live inside your repository, so your root `CLAUDE.md` is visible to agents (usually
  desirable). Treat it like any other repository content: data, not authority.
- Secret redaction is pattern-based and can miss unusual formats.
- Claude authentication cannot be verified without an API call; `doctor` only detects
  credentials heuristically.
- Real-CLI behaviour on Windows and macOS (including `PowerShell(...)` rules) is covered by
  the `Claude smoke` workflow, which only runs when you provide an API key; it has been
  verified on Linux.
- One real provider (Claude Code CLI). The provider interface is ready for others.
- Integration adapters are tested against fake services only (D23).
