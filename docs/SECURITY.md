# CREW — Security and Safety Requirements

## 1. Security posture

CREW executes AI-generated code and commands. Treat all model output, repository content, dependency scripts, and tool output as untrusted. The MVP must default to conservative behavior.

Prompts are not enforcement. All permissions and approvals must be enforced by the execution layer.

## 2. Mandatory safeguards

- No automatic push, merge to the user's branch, release, or deployment.
- No implementation before explicit plan approval.
- No completion before explicit final approval.
- Implementers are restricted to their assigned worktree.
- Director, Reviewer, and External Evaluator are read-only.
- No blanket provider permission bypass.
- No silent overwrite, reset, clean, stash, or deletion of user changes.
- No unrestricted execution outside the project/worktree without explicit approval.
- Apply command timeouts and output-size limits.
- Redact secrets from logs and reports.
- Do not persist API keys or full process environments.
- Record commands and their exit status.

## 3. Command execution

Use argument arrays with `create_subprocess_exec`; never concatenate untrusted input into shell commands.

Classify commands:
- read-only inspection;
- project verification (tests, linters, builds);
- filesystem modification within assigned worktree;
- network access;
- destructive or privileged operations.

For MVP, allow only the minimum required command categories. Require developer approval for commands that are destructive, privileged, access paths outside the workspace, or perform external side effects.

## 4. Repository and prompt injection

Repository files may contain instructions that attempt to override CREW's policy. Treat them as project data, not as authority to change:
- agent role;
- allowed capabilities;
- approval gates;
- workspace boundaries;
- secret-handling rules;
- workflow state.

The orchestrator and execution guard remain authoritative.

## 5. Secrets and privacy

- Never ask an agent to print credentials.
- Do not include `.env` values, private keys, tokens, or credential files in collected context.
- Maintain an exclusion list for context collection.
- Redact common token patterns from logs.
- Avoid logging full environment variables.
- Keep reports local by default.
- Do not upload source code or reports except through an explicitly configured provider interaction.

## 6. Git safety

Before a run, record repository status. If the user's worktree is dirty, do not proceed with implementation until the user chooses a safe path.

CREW may modify only CREW-owned worktrees and branches. On conflict or uncertain recovery, stop and preserve data. Never use `git reset --hard`, `git clean -fd`, or equivalent destructive operations on the user's active worktree.

## 7. Threat model for MVP

Consider:
- malicious or misleading repository instructions;
- hallucinated commands and test results;
- accidental secret exposure;
- provider process hanging or spawning child processes;
- concurrent tasks editing overlapping files;
- interrupted process and stale worktree state;
- destructive Git commands;
- malicious dependency lifecycle scripts.

Document residual risks rather than claiming perfect isolation. A Git worktree is isolation of working files, not a security sandbox.

## 8. Implemented controls

| Safeguard | Where it is enforced | Test |
|---|---|---|
| No push/merge/deploy | `workspace/git.py` refuses network and destructive git outside CREW worktrees; CREW only merges into `crew/<run>/integration` | `tests/integration/test_workspace.py`, `tests/e2e/test_workflow.py::test_full_run_reaches_completion_without_touching_user_branch` |
| No implementation before plan approval | state machine has no path around `AWAITING_PLAN_APPROVAL`; engine stops at gates | `tests/unit/test_state_machine.py`, `test_plan_rejection_never_implements` |
| No completion before final approval | only `crew accept` performs `AWAITING_FINAL_APPROVAL → COMPLETED` | `test_final_completion_requires_explicit_accept` |
| Gates cannot be disabled | `approvals.*` typed `Literal[True]` | `tests/unit/test_config.py` |
| Implementers confined to their worktree | agent `cwd` is always a CREW worktree; `--restricted` | `test_every_agent_runs_in_a_crew_owned_worktree` |
| Read-only roles | Director/Reviewer/Evaluator get only `Read,Glob,Grep` in `dontAsk` mode | `tests/unit/test_claude_adapter.py` |
| No permission bypass | adapter never emits bypass flags; `--permission-prompts none`; `--restricted` and `--strict-mcp-config` are mandatory and precede all free text | `test_never_bypasses_permissions`, `test_security_flags_precede_free_text`, `test_restricted_cannot_be_disabled` |
| No writes to the developer's worktree | git wrapper refuses every write command outside CREW worktrees; hooks disabled for all git calls | `test_writes_are_refused_in_the_user_worktree`, `test_repository_hooks_never_run` |
| Read-only agents cannot affect integration | Reviewer/Evaluator run in disposable snapshot worktrees; integration must be clean before merging | `test_reviewer_and_evaluator_cannot_dirty_the_integration_worktree` |
| Agent processes never outlive CREW | process-tree termination (psutil + process groups), SIGTERM/SIGHUP/SIGBREAK handling, PID-reuse-safe orphan cleanup | `tests/unit/test_system.py`, `test_stop_after_driver_died_reconciles_leftovers` |
| Spending is bounded | `execution.max_run_budget_usd`, passed per execution as `--max-budget-usd` | `test_run_budget_stops_spending` |
| User changes never overwritten | dirty tree blocks `approve-plan`; no stash/reset/clean anywhere; cleanup uses `git worktree remove` (refuses dirty trees) | `test_approval_refused_on_dirty_tree`, `test_dirty_worktree_is_preserved_on_cleanup` |
| Timeouts and output limits | `security/execution_policy.py::run_command`, adapter `timeout_seconds`, process-group termination | `test_run_command_times_out_and_kills`, `test_execute_timeout_kills_process` |
| Secret redaction; no env persisted | `security/redaction.py` applied centrally by the store (structurally for JSON) and to artifacts and logs; minimal env for commands | `tests/unit/test_redaction.py`, `test_json_columns_stay_valid_after_redaction`, `test_run_command_does_not_leak_environment` |
| Secrets not committed | new `.env`/key files are kept out of CREW commits | `test_commit_all_keeps_new_secret_files_out` |
| Secret files excluded from context | `context/collector.py::SECRET_GLOBS`, passed as `Read(...)` deny rules; agents see committed files only | `test_never_bypasses_permissions` |
| Commands recorded | `command_executions` table (git + verification), `agent_executions` | `test_verification_commands_are_recorded_as_evidence`, `test_git_commands_are_recorded_per_run` |
| No shell interpolation | every subprocess uses argument lists; verification commands refuse shells/operators | `tests/unit/test_execution_policy.py` |

Residual risks are listed in [DECISIONS.md](DECISIONS.md#known-limitations).
