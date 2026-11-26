"""Claude Code CLI provider adapter.

Invocation (flags verified against ``claude --help`` of CLI 2.1.x):

    claude -p --restricted --strict-mcp-config
           --permission-prompts none --permission-mode <dontAsk|acceptEdits>
           --no-session-persistence --output-format json
           --tools <list> --allowedTools <list> [--disallowedTools <list>]
           [--model M] [--max-budget-usd N]
           --json-schema <schema> --append-system-prompt <role instructions>
    (prompt on stdin)

Permission enforcement:
* Read-only roles get only Read/Glob/Grep (plus scoped Bash rules for the
  Reviewer if configured) in ``dontAsk`` mode: anything not pre-approved is denied.
* Implementers additionally get Edit/Write in ``acceptEdits`` mode; Bash only via
  explicitly configured, scoped rules.
* ``--permission-prompts none`` means nobody can approve an escalation mid-run.
* ``--restricted`` (always on) confines file tools to the working directory,
  which is always a CREW-owned worktree, ignores user/project/local settings
  files (so repository hooks and permission rules cannot widen access) and
  refuses permission bypass. ``--strict-mcp-config`` keeps MCP servers out.
* ``--dangerously-skip-permissions`` / ``bypassPermissions`` are never used.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

from crew.config.schema import ClaudeCodeSettings
from crew.domain.errors import ProviderError
from crew.providers.base import AgentRequest, AgentResult, ProcessStarted
from crew.providers.claude_code.parser import parse_cli_output
from crew.security.redaction import redact
from crew.system.processes import process_started_at, spawn_kwargs, terminate_tree

READ_TOOLS = ("Read", "Glob", "Grep")
EDIT_TOOLS = ("Edit", "Write")
MAX_CAPTURE_BYTES = 5_000_000


def build_argv(
    settings: ClaudeCodeSettings, request: AgentRequest, command: str | list[str]
) -> list[str]:
    """Build the CLI argument list.

    Security-relevant flags come first and free text (JSON schema, role prompt)
    last, so no quoting accident in a long free-text argument can ever cause a
    later security flag to be dropped.
    """
    caps = request.capabilities
    tools = list(READ_TOOLS)
    allowed = list(READ_TOOLS)
    if caps.can_edit_files:
        tools += EDIT_TOOLS
        allowed += EDIT_TOOLS
    if caps.bash_rules:
        for tool in ("Bash", "PowerShell"):
            if any(rule.startswith(f"{tool}(") for rule in caps.bash_rules):
                tools.append(tool)
        allowed += caps.bash_rules
    argv = [
        *([command] if isinstance(command, str) else command),
        "-p",
        "--restricted",  # confine file tools, ignore user/project settings, refuse bypass
        "--strict-mcp-config",  # no MCP servers from the developer's configuration
        "--permission-prompts",
        "none",
        "--permission-mode",
        "acceptEdits" if caps.can_edit_files else "dontAsk",
        "--no-session-persistence",
        "--output-format",
        "json",
    ]
    # List options are joined into one value each and followed by another option,
    # so they cannot swallow other arguments.
    argv += ["--tools", ",".join(tools), "--allowedTools", ",".join(allowed)]
    if caps.deny_read_globs:
        argv += ["--disallowedTools", ",".join(f"Read({g})" for g in caps.deny_read_globs)]
    if settings.model:
        argv += ["--model", settings.model]
    budget = request.max_budget_usd or settings.max_budget_usd
    if budget:
        argv += ["--max-budget-usd", f"{budget:.2f}"]
    argv += [
        "--json-schema",
        json.dumps(request.output_schema, separators=(",", ":")),
        "--append-system-prompt",
        request.instructions,
    ]
    return argv


_NPM_CLI_JS = Path("node_modules") / "@anthropic-ai" / "claude-code" / "cli.js"


def resolve_cli_command(executable: str) -> list[str]:
    if not executable.casefold().endswith((".cmd", ".bat", ".ps1")):
        return [executable]
    cli_js = Path(executable).parent / _NPM_CLI_JS
    node = shutil.which("node")
    if cli_js.is_file() and node:
        return [node, str(cli_js)]
    raise ProviderError(
        f"{executable} is a script shim that cannot be invoked safely",
        kind="not_installed",
        hint="Install the native Claude Code build (claude.exe), or make sure `node` is on "
        "PATH next to the npm package, then run `crew doctor`.",
    )


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

    def resolve_command(self) -> list[str]:
        """The argv prefix that starts the CLI.

        On Windows an npm install provides ``claude.cmd``, which CreateProcess
        runs through cmd.exe. cmd.exe re-parses the command line: it stops at the
        first newline (our system prompt has many) and interprets ``& | ^ %``.
        So a batch shim is never executed directly; it is resolved to
        ``node <package>/cli.js``, or refused with a clear error.
        """
        return resolve_cli_command(self.resolve_executable())

    async def execute(
        self,
        request: AgentRequest,
        *,
        timeout_seconds: int,
        on_process_start: ProcessStarted | None = None,
    ) -> AgentResult:
        argv = build_argv(self.settings, request, self.resolve_command())
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(request.cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(),
            limit=MAX_CAPTURE_BYTES,
            **spawn_kwargs(),  # own process group so the whole agent tree can be stopped
        )
        if on_process_start:
            on_process_start(proc.pid, process_started_at(proc.pid))
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(request.prompt.encode("utf-8")), timeout=timeout_seconds
            )
        except TimeoutError:
            stopped = await terminate_tree(proc)
            raise ProviderError(
                f"Claude CLI timed out after {timeout_seconds}s"
                + ("" if stopped else " and could not be stopped"),
                kind="timeout",
            ) from None
        except asyncio.CancelledError:
            await terminate_tree(proc)
            raise
        await terminate_tree(proc)  # leftover tool subprocesses must not outlive the agent

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
