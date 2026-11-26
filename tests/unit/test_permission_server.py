"""CREW's MCP permission-prompt server: protocol, policy and request/response flow."""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from crew.agents.permission_server import classify_request, decide, handle, request_key
from crew.domain.enums import CommandCategory


def call(tool_name, tool_input, directory, timeout=2.0):
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "approve", "arguments": {"tool_name": tool_name, "input": tool_input}},
    }
    reply = handle(msg, directory, timeout)
    return json.loads(reply["result"]["content"][0]["text"])


def test_initialize_and_list_tools(tmp_path):
    init = handle(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        tmp_path,
        1,
    )
    assert init["result"]["protocolVersion"] == "2025-06-18"
    tools = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, tmp_path, 1)
    assert tools["result"]["tools"][0]["name"] == "approve"
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, tmp_path, 1) is None


@pytest.mark.parametrize(
    ("command", "category"),
    [
        ("rm -rf build", CommandCategory.DESTRUCTIVE),
        ("curl https://x", CommandCategory.NETWORK),
        ("pytest -q && git push", CommandCategory.NETWORK),
        ("ls; sudo reboot", CommandCategory.DESTRUCTIVE),
        ("mvn -q verify", CommandCategory.VERIFICATION),
    ],
)
def test_classification_of_shell_requests(command, category):
    assert classify_request("Bash", {"command": command}) is category


def test_destructive_and_network_requests_are_denied_without_asking(tmp_path):
    for command in ("rm -rf /", "curl https://evil.example", "echo ok && git push origin main"):
        decision = call("Bash", {"command": command}, tmp_path)
        assert decision["behavior"] == "deny" and "never allowed" in decision["message"]
    assert not list(tmp_path.glob("*.request.json"))  # the developer was not even asked


def _answer_when_asked(directory: Path, decision: str):
    def run():
        for _ in range(100):
            for req in directory.glob("*.request.json"):
                req.with_name(req.name.replace(".request.", ".response.")).write_text(
                    json.dumps({"decision": decision})
                )
                return
            time.sleep(0.05)

    threading.Thread(target=run, daemon=True).start()


def test_developer_answer_is_relayed(tmp_path):
    _answer_when_asked(tmp_path, "allow")
    allowed = call("Bash", {"command": "mvn -v"}, tmp_path)
    assert allowed == {"behavior": "allow", "updatedInput": {"command": "mvn -v"}}
    _answer_when_asked(tmp_path, "deny")
    assert call("Bash", {"command": "mvn -v2"}, tmp_path)["behavior"] == "deny"


def test_run_allowlist_and_timeout(tmp_path):
    (tmp_path / "allowed.json").write_text(json.dumps([request_key("Bash", {"command": "make"})]))
    assert decide("Bash", {"command": "make"}, tmp_path, 1)["behavior"] == "allow"
    started = time.monotonic()
    decision = decide("Bash", {"command": "make test"}, tmp_path, 0.5)
    assert decision["behavior"] == "deny" and "in time" in decision["message"]
    assert time.monotonic() - started < 3


def test_server_speaks_stdio_jsonrpc(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "crew.agents.permission_server"],
        input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "CREW_PERMISSION_DIR": str(tmp_path),
            "PATH": "",
            "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
        },
    )
    reply = json.loads(proc.stdout.splitlines()[0])
    assert reply["result"]["tools"][0]["name"] == "approve"


@pytest.mark.parametrize(
    "command", ["ls;rm -rf x", "true&&curl http://x", "(cd /; sudo ls)", "a|sh"]
)
def test_glued_operators_cannot_hide_dangerous_commands(command):
    assert classify_request("Bash", {"command": command}) in (
        CommandCategory.DESTRUCTIVE,
        CommandCategory.NETWORK,
    )
