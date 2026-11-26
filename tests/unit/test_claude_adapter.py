"""Claude Code adapter: argument construction, output parsing and process handling.

Process tests use a stand-in executable, so no real CLI or credentials are needed.
"""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from crew.agents.contracts import output_schema
from crew.agents.runtime import capabilities_for, role_instructions
from crew.config.loader import parse_config
from crew.config.schema import ClaudeCodeSettings
from crew.domain.enums import Role
from crew.domain.errors import ProviderError
from crew.providers.base import AgentRequest
from crew.providers.claude_code.adapter import ClaudeCodeProvider, build_argv
from crew.providers.claude_code.parser import extract_json_object, parse_cli_output

CONFIG = parse_config(
    {
        "version": 1,
        "project": {"name": "t"},
        "provider": {"claude_code": {"implementer_bash_rules": ["Bash(uv run pytest:*)"]}},
    }
)


def make_request(role: Role, cwd: Path) -> AgentRequest:
    return AgentRequest(
        role=role,
        instructions=role_instructions(role),
        prompt="do the thing",
        cwd=cwd,
        capabilities=capabilities_for(role, CONFIG),
        output_schema=output_schema(role),
        run_id="r1",
        execution_id="e1",
    )


def _value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


@pytest.mark.parametrize("role", [Role.DIRECTOR, Role.REVIEWER, Role.EVALUATOR])
def test_read_only_roles_cannot_edit(role, tmp_path):
    argv = build_argv(CONFIG.provider.claude_code, make_request(role, tmp_path), "claude")
    tools = _value(argv, "--tools").split(",")
    assert "Edit" not in tools and "Write" not in tools and "Bash" not in tools
    assert _value(argv, "--permission-mode") == "dontAsk"


def test_implementer_gets_edits_and_only_scoped_bash(tmp_path):
    argv = build_argv(
        CONFIG.provider.claude_code, make_request(Role.IMPLEMENTER, tmp_path), "claude"
    )
    assert {"Edit", "Write", "Bash"} <= set(_value(argv, "--tools").split(","))
    allowed = _value(argv, "--allowedTools").split(",")
    assert "Bash(uv run pytest:*)" in allowed
    assert "Bash" not in allowed  # never unscoped
    assert _value(argv, "--permission-mode") == "acceptEdits"


@pytest.mark.parametrize("role", list(Role))
def test_never_bypasses_permissions(role, tmp_path):
    argv = build_argv(CONFIG.provider.claude_code, make_request(role, tmp_path), "claude")
    joined = " ".join(argv)
    assert "dangerously" not in joined
    assert "bypassPermissions" not in joined
    assert "--restricted" in argv
    assert _value(argv, "--permission-prompts") == "none"
    assert "Read(**/.env)" in _value(argv, "--disallowedTools")
    json.loads(_value(argv, "--json-schema"))


def test_parse_structured_output():
    out = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done",
            "structured_output": {"a": 1},
            "session_id": "s",
            "total_cost_usd": 0.12,
        }
    )
    parsed = parse_cli_output(out)
    assert parsed.output == {"a": 1}
    assert parsed.cost_usd == pytest.approx(0.12)


def test_parse_falls_back_to_fenced_json_in_result():
    out = json.dumps({"subtype": "success", "result": 'Here:\n```json\n{"b": 2}\n```'})
    assert parse_cli_output(out).output == {"b": 2}


@pytest.mark.parametrize(
    ("stdout", "kind"),
    [
        ("", "empty_output"),
        ("not json at all", "malformed_output"),
        (json.dumps({"is_error": True, "subtype": "error_max_turns"}), "provider_error"),
        (json.dumps({"subtype": "success", "result": "no json here"}), "malformed_output"),
    ],
)
def test_parse_errors_are_normalised(stdout, kind):
    with pytest.raises(ProviderError) as info:
        parse_cli_output(stdout)
    assert info.value.kind == kind


def test_extract_json_object_from_prose():
    assert extract_json_object('blah {"x": [1, 2]} blah') == {"x": [1, 2]}


def _stub_cli(tmp_path: Path, body: str) -> str:
    script = tmp_path / "claude-stub"
    script.write_text(f"#!{sys.executable}\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


async def test_execute_feeds_prompt_on_stdin_and_parses(tmp_path):
    exe = _stub_cli(
        tmp_path,
        "import sys, json\n"
        "prompt = sys.stdin.read()\n"
        "print(json.dumps({'subtype': 'success', 'result': '',"
        " 'structured_output': {'echo': prompt, 'argc': len(sys.argv)}}))",
    )
    provider = ClaudeCodeProvider(ClaudeCodeSettings(executable=exe))
    pids = []
    result = await provider.execute(
        make_request(Role.DIRECTOR, tmp_path), timeout_seconds=30, on_process_start=pids.append
    )
    assert result.output["echo"] == "do the thing"
    assert pids and pids[0] > 0


async def test_execute_timeout_kills_process(tmp_path):
    exe = _stub_cli(tmp_path, "import time\ntime.sleep(60)")
    provider = ClaudeCodeProvider(ClaudeCodeSettings(executable=exe))
    pids = []
    with pytest.raises(ProviderError) as info:
        await provider.execute(
            make_request(Role.DIRECTOR, tmp_path), timeout_seconds=1, on_process_start=pids.append
        )
    assert info.value.kind == "timeout"
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


async def test_execute_nonzero_exit_without_output(tmp_path):
    exe = _stub_cli(tmp_path, "import sys\nsys.stderr.write('auth required')\nsys.exit(1)")
    provider = ClaudeCodeProvider(ClaudeCodeSettings(executable=exe))
    with pytest.raises(ProviderError, match="auth required") as info:
        await provider.execute(make_request(Role.DIRECTOR, tmp_path), timeout_seconds=10)
    assert info.value.kind == "process_error"


def test_missing_executable_has_actionable_error(tmp_path):
    provider = ClaudeCodeProvider(ClaudeCodeSettings(executable="definitely-not-claude-xyz"))
    with pytest.raises(ProviderError) as info:
        provider.resolve_executable()
    assert info.value.kind == "not_installed"
    assert info.value.hint
