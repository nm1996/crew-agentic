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
    model: null                 # e.g. "opus", "sonnet"; null = CLI default
    max_budget_usd: null        # per agent execution, passed as --max-budget-usd
    restricted: true            # pass --restricted (confine file tools to the worktree)
    implementer_bash_rules: []  # e.g. ["Bash(uv run pytest:*)", "Bash(npm test:*)"]
    reviewer_bash_rules: []     # same syntax; lets the Reviewer run checks
execution:
  max_implementers: 2           # 1 … 2 (hard limit)
  max_correction_cycles: 2      # 0 … 5
  max_task_attempts: 2          # 1 … 3 (a failed task is retried in a fresh worktree)
  command_timeout_seconds: 600  # per verification command
  command_output_limit_bytes: 200000
  preserve_worktrees_on_failure: true
  verification_commands: []     # e.g. ["uv run pytest -q", "uv run ruff check ."]
git:
  branch_prefix: crew           # branches: crew/<run>/integration, crew/<run>/<task>-a<n>
  worktree_directory: .crew/worktrees
approvals:
  require_plan_approval: true   # cannot be disabled
  require_final_approval: true  # cannot be disabled
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
| `*_bash_rules` | each must be a scoped `Bash(...)` rule, not `Bash(*)` | No unrestricted shell. |
| `git.worktree_directory` | must be under `.crew/` | CREW only writes to paths it owns. |
| `execution.verification_commands` | no shells, no `rm`/`sudo`/network tools, no pipes or redirects | They run unattended, without a shell. |

## Verification commands

Commands in `execution.verification_commands` are executed **by CREW** (not by an agent)
in the integration worktree after each integration. They are split with `shlex`, run
without a shell, with a minimal environment (`PATH`, `HOME`, locale, `VIRTUAL_ENV`,
`CI=1`), a timeout and an output limit; output is redacted. Their results are the
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

## Project notes for the Director

Markdown files in `.crew/context/` are included in the Director's context. Use them for
conventions, architecture notes and off-limits areas. Do not put secrets there.

## Secret exclusions

CREW never lists or lets agents `Read` files matching built-in secret patterns
(`.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.kdbx`, `id_rsa*`, `id_ed25519*`,
`.netrc`, `.npmrc`, `.pypirc`, `credentials*`, `*secret*`), plus anything in
`context.exclude`. Agents work on worktrees created from commits, so untracked local files
(typically `.env`) are not present there in the first place.
