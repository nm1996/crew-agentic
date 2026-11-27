# Configuration reference

Project configuration lives in `.crew/config.yaml` and is validated with Pydantic
(`src/crew/config/schema.py`) every time CREW starts. Unknown keys are errors. Change
values with `crew configure key=value` (validated before writing) or edit the file.

```yaml
version: 1
project:
  name: my-project
provider:
  default: claude_code          # claude_code | fake (offline, deterministic)
  claude_code:
    executable: claude          # name on PATH or absolute path
    timeout_seconds: 1800       # per agent execution (30 … 14400)
    model: null                 # default model for every role; null = CLI default
    profile: default            # default | balanced | economy (per-role model/effort)
    roles: {}                   # per-role overrides, e.g. {evaluator: {model: haiku, effort: low}}
    max_budget_usd: null        # per agent execution cap, passed as --max-budget-usd
    restricted: true            # always on: --restricted --strict-mcp-config (cannot be disabled)
    implementer_bash_rules: []  # e.g. ["Bash(uv run pytest:*)"]; on Windows also "PowerShell(...)"
    reviewer_bash_rules: []     # same syntax; lets the Reviewer run checks
execution:
  max_implementers: 2           # 1 … 2 (hard limit)
  max_correction_cycles: 2      # 0 … 5
  max_task_attempts: 2          # 1 … 3 (a failed task is retried in a fresh worktree)
  max_run_budget_usd: null      # total spend limit per run (USD); null = unlimited
  command_timeout_seconds: 600  # per verification command
  command_output_limit_bytes: 200000
  preserve_worktrees_on_failure: true
  verification_commands: []     # strings or argv lists, e.g. ["uv run pytest -q", ["npm", "test"]]
  review_context: full          # full | summary — how much diff Reviewer/Evaluator get inline
  conflict_resolution: agent    # agent | fail — resolve merge conflicts with an Implementer
  serialize_shared_files: true  # tasks sharing an expected file never run in parallel (D25)
  sandbox: none                 # none | bwrap (Linux) | docker — isolation for verification
  sandbox_image: null           # docker only, e.g. python:3.12-slim
  sandbox_network: false        # allow network inside the sandbox
  sandbox_writable_paths: []    # extra writable paths for bwrap, e.g. ["~/.cache/uv"]
git:
  branch_prefix: crew           # branches: crew/<run>/integration, crew/<run>/<task>-a<n>
  worktree_directory: .crew/worktrees
approvals:
  require_plan_approval: true   # cannot be disabled
  require_final_approval: true  # cannot be disabled
  permission_prompts: off       # off | ask — ask the developer when an agent needs more
  permission_timeout_seconds: 300
integrations: {}                # Jira, GitHub, Confluence / folder — see INTEGRATIONS.md
context:
  max_files_listed: 400         # file list size given to the Director
  exclude: []                   # extra globs never listed and never readable by agents
```

## Hard limits enforced in code

These are not just defaults; values outside them are rejected:

| Setting | Constraint | Why |
|---|---|---|
| `approvals.*` | must be `true` | Mandatory human gates (SECURITY.md §2). |
| `execution.max_implementers` | 1–2 | MVP concurrency limit. |
| `execution.max_correction_cycles` | 0–5 | Never loop indefinitely. |
| `provider.claude_code.restricted` | must be `true` | Agents always run with `--restricted --strict-mcp-config`. |
| `*_bash_rules` | each must be a scoped `Bash(...)` / `PowerShell(...)` rule, not `Bash(*)` | No unrestricted shell. |
| `git.worktree_directory` | must be under `.crew/` | CREW only writes to paths it owns. |
| `execution.verification_commands` | no shells (`sh`, `bash`, `cmd`, `powershell`), no `rm`/`sudo`/`del`/network tools, no pipes or redirects; Windows batch files (`npm.cmd`) without cmd.exe metacharacters | They run unattended, without a shell. |

## Verification commands

Commands in `execution.verification_commands` are executed **by CREW** (not by an agent)
in the integration worktree after each integration. Each entry is either a string, split
like a shell would (but never run by one), or an explicit argument list — the most
portable form, recommended on Windows:

```yaml
verification_commands:
  - uv run pytest -q
  - ["npm", "test"]
  - ["python", "-m", "mypy", "src"]
```

The program is resolved on `PATH` (with `PATHEXT` on Windows). Commands run with a
minimal environment (`PATH`, `HOME`, locale, `VIRTUAL_ENV`, `CI=1`, plus required Windows
system variables), a timeout and an output limit; output is redacted. A program that
cannot be started is recorded as exit code 127.

> **Security note:** verification commands run code that agents may have written or
> changed (test files, `conftest.py`, `package.json` scripts, `Makefile`). Without a
> sandbox, that code runs as your user. Enable one:

### Sandboxing verification

```bash
crew configure execution.sandbox=bwrap                       # Linux, needs bubblewrap
crew configure execution.sandbox=docker execution.sandbox_image=python:3.12-slim
```

| | `bwrap` | `docker` |
|---|---|---|
| Writable | the worktree, a private `/tmp`, `sandbox_writable_paths` | `/workspace` (the worktree), `/tmp` |
| Network | off unless `sandbox_network: true` | `--network none` unless `sandbox_network: true` |
| Your home | readable, credential dirs (`.ssh`, `.aws`, `.config/gh`, `.claude`, …) hidden | not mounted |
| Toolchain | your host tools | must be inside the image |

If the sandbox is not available when a run reaches verification, the run **fails** (it is
never silently run unsandboxed); fix it and `crew retry`. `crew doctor` checks it.

Tips:

- With the network off, tools cannot download dependencies. Either set
  `sandbox_network: true`, or (bwrap) make your package cache writable, e.g.
  `sandbox_writable_paths: ["~/.cache/uv"]`, and run offline (`UV_OFFLINE=1` is not set
  for you; use a command such as `["uv", "run", "--offline", "pytest"]`).
- Ubuntu 24.04+ restricts unprivileged user namespaces; if `doctor` reports that bwrap
  cannot create a sandbox, allow them (`sysctl kernel.apparmor_restrict_unprivileged_userns=0`)
  or use Docker. Their results are the
trustworthy test evidence given to the Reviewer and the External Evaluator and shown in
reports. A failing command does not stop the run; it becomes evidence.

Tip: if a tool needs its environment set up first (e.g. `uv sync`), make that a separate
command earlier in the list.

## Bash rules for agents

By default Implementers can read and edit files only, and read-only roles can only read.
To let an Implementer run tests itself, grant narrowly scoped rules in Claude Code
permission-rule syntax:

```bash
crew configure 'provider.claude_code.implementer_bash_rules=["Bash(uv run pytest:*)"]'
```

Anything not allowed is denied automatically (`--permission-prompts none`); no one can
approve an escalation in the middle of a run.

## Cost: profiles, per-role models and review context

Most of a run's cost is model choice and how much text each agent reads. Three settings
control it (measured effect: [examples/spring-boot-crud.md](examples/spring-boot-crud.md)):

**`provider.claude_code.profile`** sets per-role defaults:

| Role | `default` | `balanced` | `economy` |
|---|---|---|---|
| Director | CLI default | CLI default, effort medium | sonnet, effort medium |
| Implementer | CLI default | CLI default, effort medium | sonnet, effort medium |
| Reviewer | CLI default | sonnet, effort medium | sonnet, effort low |
| Evaluator | CLI default | sonnet, effort low | haiku, effort low |

**`provider.claude_code.roles`** overrides single roles and wins over the profile and the
global `model`:

```yaml
provider:
  claude_code:
    profile: balanced
    roles:
      implementer: {model: opus, effort: high}   # keep the strongest model where it matters
```

Model values are Claude Code aliases (`opus`, `sonnet`, `haiku`, …) or full model ids;
effort is one of `low`, `medium`, `high`, `xhigh`, `max` (`--effort`).

**`execution.review_context: summary`** gives the Reviewer and Evaluator the diff stat,
the list of changed files and the first 12 kB of the patch instead of the whole diff; they
open the files they need with their Read tool. On large changes this removes tens of
thousands of prompt tokens per agent.

Measured on the reference run ([examples/spring-boot-crud.md](examples/spring-boot-crud.md#cost-profiles-compared)):
`default` $3.34, `balanced` $2.87, `economy` $1.28 — all three produced a working service;
the cheaper profiles wrote fewer tests and reviewed less deeply. The Implementer model is
the main cost lever.

## Run budget

`execution.max_run_budget_usd` limits the total provider-reported cost of one run. Each
agent execution gets the remaining budget as its `--max-budget-usd` (split between
concurrent Implementers). When it is used up, no further agent starts and the run fails
with exit code 11; raise the limit and `crew retry <run-id>`. `crew status` shows the cost
so far.

## Asking for permissions

By default an agent that tries something outside its rules is simply denied and has to
work around it. With

```bash
crew configure approvals.permission_prompts=ask
```

Implementers and the Reviewer get the shell tool *available but not pre-approved*. Each
command outside their rules is shown to you in the terminal running CREW:

```text
Permission request from the implementer (verification):
  Bash: mvn -q dependency:tree
  Allow? [y]es once / [a]lways in this run / [N]o:
```

- Destructive and network commands (`rm`, `sudo`, `curl`, `git push`, …, also inside
  compound commands) are denied without asking.
- "always in this run" allows that exact command again for the rest of the run.
- Without a terminal (CI, background) or after `permission_timeout_seconds`, the answer
  is no.
- Every answer is recorded as a `PERMISSION` approval in the run's history and report.

Under the hood this is CREW's own MCP tool passed to Claude Code as its permission-prompt
tool; it is the only MCP server agents get.

## Project notes for the Director

Markdown files in `.crew/context/` are included in the Director's context. Use them for
conventions, architecture notes and off-limits areas. Do not put secrets there.

## Secret exclusions

CREW never lists or lets agents `Read` files matching built-in secret patterns
(`.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.kdbx`, `id_rsa*`, `id_ed25519*`,
`.netrc`, `.npmrc`, `.pypirc`, `credentials*`, `*secret*`), plus anything in
`context.exclude`. Agents work on worktrees created from commits, so untracked local files
(typically `.env`) are not present there in the first place.
