"""CREW permission-prompt tool: a minimal MCP server over stdio.

Claude Code runs it (``--mcp-config``) and calls its ``approve`` tool
(``--permission-prompt-tool mcp__crew__approve``) whenever an agent asks for an
action its permission rules do not allow. The server:

1. denies DESTRUCTIVE and NETWORK commands outright (execution policy);
2. allows requests the developer already allowed *for this run*;
3. otherwise writes ``<id>.request.json`` into ``CREW_PERMISSION_DIR`` and waits
   for the CREW driver to write ``<id>.response.json`` (the developer's answer),
   denying after ``CREW_PERMISSION_TIMEOUT`` seconds.

It speaks newline-delimited JSON-RPC 2.0 (MCP stdio transport) and depends only
on the standard library and CREW's execution policy.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from crew.domain.enums import CommandCategory
from crew.domain.errors import PolicyViolationError
from crew.security.execution_policy import classify_command

TOOL_NAME = "approve"
SHELL_TOOLS = ("Bash", "PowerShell")
POLL_SECONDS = 0.2
ALLOWLIST_FILE = "allowed.json"


def request_key(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Identity of a request for "allow for this run": tool + exact command/path."""
    detail = (
        tool_input.get("command")
        or tool_input.get("file_path")
        or json.dumps(tool_input, sort_keys=True)
    )
    return f"{tool_name}:{detail}"


def classify_request(tool_name: str, tool_input: dict[str, Any]) -> CommandCategory | None:
    if tool_name not in SHELL_TOOLS:
        return None
    command = str(tool_input.get("command", ""))
    try:
        # punctuation_chars splits operators even when glued to words ("ls;rm").
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        argv = list(lexer)
    except ValueError:
        return CommandCategory.DESTRUCTIVE  # unparseable shell: treat as unsafe
    if not argv:
        return None
    # Compound commands are judged by their most dangerous part.
    parts: list[list[str]] = [[]]
    for token in argv:
        if token and set(token) <= set("&|;<>()"):
            parts.append([])
        else:
            parts[-1].append(token)
    categories = []
    for part in parts:
        if part:
            try:
                categories.append(classify_command(part))
            except PolicyViolationError:
                categories.append(CommandCategory.DESTRUCTIVE)
    for worst in (CommandCategory.DESTRUCTIVE, CommandCategory.NETWORK):
        if worst in categories:
            return worst
    return categories[0] if categories else None


def _deny(message: str) -> dict[str, Any]:
    return {"behavior": "deny", "message": message}


def _allow(tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"behavior": "allow", "updatedInput": tool_input}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def decide(tool_name: str, tool_input: dict[str, Any], directory: Path, timeout: float) -> dict:
    category = classify_request(tool_name, tool_input)
    if category in (CommandCategory.DESTRUCTIVE, CommandCategory.NETWORK):
        return _deny(f"CREW policy: {category.value} commands are never allowed for agents.")
    key = request_key(tool_name, tool_input)
    if key in (_read_json(directory / ALLOWLIST_FILE) or []):
        return _allow(tool_input)

    request_id = uuid.uuid4().hex[:12]
    request = {
        "id": request_id,
        "run_id": os.environ.get("CREW_RUN_ID"),
        "execution_id": os.environ.get("CREW_EXECUTION_ID"),
        "role": os.environ.get("CREW_ROLE"),
        "tool_name": tool_name,
        "input": tool_input,
        "key": key,
        "category": category.value if category else None,
        "created_at": time.time(),
    }
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"{request_id}.request.tmp"
    tmp.write_text(json.dumps(request), encoding="utf-8")
    tmp.replace(directory / f"{request_id}.request.json")

    response_path = directory / f"{request_id}.response.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = _read_json(response_path)
        if isinstance(response, dict):
            if response.get("decision") in ("allow", "allow_run"):
                return _allow(tool_input)
            return _deny(response.get("message") or "Denied by the developer.")
        time.sleep(POLL_SECONDS)
    return _deny("No answer from the developer in time; denied.")


TOOL_SPEC = {
    "name": TOOL_NAME,
    "description": "CREW permission check for actions outside the agent's permission rules.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "input": {"type": "object"},
            "tool_use_id": {"type": "string"},
        },
        "required": ["tool_name", "input"],
    },
}


def handle(message: dict[str, Any], directory: Path, timeout: float) -> dict[str, Any] | None:
    """Handle one JSON-RPC message; ``None`` for notifications."""
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return None  # notification (e.g. notifications/initialized)
    if method == "initialize":
        params = message.get("params") or {}
        result: Any = {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "crew", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [TOOL_SPEC]}
    elif method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != TOOL_NAME:
            return _error(msg_id, -32602, f"unknown tool {params.get('name')!r}")
        args = params.get("arguments") or {}
        tool_input = args.get("input") if isinstance(args.get("input"), dict) else {}
        decision = decide(str(args.get("tool_name", "")), tool_input, directory, timeout)
        result = {"content": [{"type": "text", "text": json.dumps(decision)}]}
    elif method == "ping":
        result = {}
    else:
        return _error(msg_id, -32601, f"method {method!r} not supported")
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text}}


def main() -> None:
    directory = Path(os.environ["CREW_PERMISSION_DIR"])
    timeout = float(os.environ.get("CREW_PERMISSION_TIMEOUT", "300"))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle(message, directory, timeout)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
