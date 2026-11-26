"""Claude Code CLI provider adapter.

Invocation (flags verified against ``claude --help`` of CLI 2.1.x):

    claude -p --output-format json --no-session-persistence
           --permission-prompts none --permission-mode <dontAsk|acceptEdits>
           --tools <list> --allowedTools <list> [--disallowedTools <list>]
           [--restricted] [--model M] [--max-budget-usd N]
           --json-schema <schema> --append-system-prompt <role instructions>
    (prompt on stdin)

Permission enforcement:
* Read-only roles get only Read/Glob/Grep (plus scoped Bash rules for the
  Reviewer if configured) in ``dontAsk`` mode: anything not pre-approved is denied.
* Implementers additionally get Edit/Write in ``acceptEdits`` mode; Bash only via
  explicitly configured, scoped rules.
* ``--permission-prompts none`` means nobody can approve an escalation mid-run.
* ``--restricted`` (default on) confines file tools to the working directory,
  which is always a CREW-owned worktree, and refuses permission bypass.
* ``--dangerously-skip-permissions`` / ``bypassPermissions`` are never used.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time

from crew.config.schema import ClaudeCodeSettings
from crew.domain.errors import ProviderError
from crew.providers.base import AgentRequest, AgentResult, ProcessStarted
from crew.providers.claude_code.parser import parse_cli_output
from crew.security.execution_policy import terminate_process_group
from crew.security.redaction import redact

READ_TOOLS = ("Read", "Glob", "Grep")
EDIT_TOOLS = ("Edit", "Write")
MAX_CAPTURE_BYTES = 5_000_000


def build_argv(settings: ClaudeCodeSettings, request: AgentRequest, executable: str) -> list[str]:
    caps = request.capabilities
    tools = list(READ_TOOLS)
    allowed = list(READ_TOOLS)
    if caps.can_edit_files:
        tools += EDIT_TOOLS
        allowed += EDIT_TOOLS
    if caps.bash_rules:
        tools.append("Bash")
        allowed += caps.bash_rules
    argv = [
        executable,
        "-p",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--permission-prompts",
        "none",
        "--permission-mode",
        "acceptEdits" if caps.can_edit_files else "dontAsk",
        "--json-schema",
        json.dumps(request.output_schema, separators=(",", ":")),
        "--append-system-prompt",
        request.instructions,
    ]
    if settings.restricted:
        argv.append("--restricted")
    if settings.model:
        argv += ["--model", settings.model]
    if settings.max_budget_usd:
        argv += ["--max-budget-usd", f"{settings.max_budget_usd:.2f}"]
    # Variadic list options go last so they cannot swallow other arguments.
    argv += ["--tools", ",".join(tools), "--allowedTools", ",".join(allowed)]
    if caps.deny_read_globs:
        argv += ["--disallowedTools", ",".join(f"Read({g})" for g in caps.deny_read_globs)]
    return argv


def _child_env() -> dict[str, str]:
    # Claude Code needs its own auth/config variables; drop obvious unrelated secrets.
    drop_prefixes = ("AWS_SECRET", "GITHUB_TOKEN", "GH_TOKEN", "NPM_TOKEN", "PYPI_")
    return {k: v for k, v in os.environ.items() if not k.startswith(drop_prefixes)}


class ClaudeCodeProvider:
    name = "claude_code"

    def __init__(self, settings: ClaudeCodeSettings) -> None:
        self.settings = settings

    def resolve_executable(self) -> str:
        path = shutil.which(self.settings.executable)
        if not path:
            raise ProviderError(
                f"Claude Code CLI '{self.settings.executable}' not found on PATH",
                kind="not_installed",
                hint="Install Claude Code (https://docs.claude.com/claude-code) and run "
                "`claude` once to authenticate, then `crew doctor`.",
            )
        return path

    async def execute(
        self,
        request: AgentRequest,
        *,
        timeout_seconds: int,
        on_process_start: ProcessStarted | None = None,
    ) -> AgentResult:
        argv = build_argv(self.settings, request, self.resolve_executable())
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(request.cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(),
            start_new_session=True,  # own process group so timeouts kill children too
            limit=MAX_CAPTURE_BYTES,
        )
        if on_process_start:
            on_process_start(proc.pid)
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(request.prompt.encode("utf-8")), timeout=timeout_seconds
            )
        except TimeoutError:
            stopped = await terminate_process_group(proc)
            raise ProviderError(
                f"Claude CLI timed out after {timeout_seconds}s"
                + ("" if stopped else " and could not be stopped"),
                kind="timeout",
            ) from None
        except asyncio.CancelledError:
            await terminate_process_group(proc)
            raise

        out_text = stdout[:MAX_CAPTURE_BYTES].decode("utf-8", errors="replace")
        err_text = redact(stderr[-4000:].decode("utf-8", errors="replace")).strip()
        if proc.returncode != 0 and not out_text.strip():
            raise ProviderError(
                f"Claude CLI exited with code {proc.returncode}: {err_text or 'no stderr'}",
                kind="process_error",
            )
        parsed = parse_cli_output(out_text)
        return AgentResult(
            output=parsed.output,
            raw_text=redact(parsed.result_text)[:100_000],
            session_id=parsed.session_id,
            cost_usd=parsed.cost_usd,
            duration_seconds=round(time.monotonic() - started, 3),
            metadata={"num_turns": parsed.num_turns, "exit_code": proc.returncode},
        )
