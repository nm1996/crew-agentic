import logging

import pytest

from crew.security.redaction import REDACTED, RedactingFilter, redact


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAABCDEFGHIJKLMNOP",
        "xoxb-1234567890-abcdefghij",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ],
)
def test_token_patterns_are_redacted(secret):
    text = f"value={secret} trailing"
    assert secret not in redact(text)
    assert REDACTED in redact(text)


def test_secret_assignments_are_redacted():
    text = "DATABASE_PASSWORD=hunter2hunter2 API_KEY: 'abcd1234efgh' name=alice"
    out = redact(text)
    assert "hunter2hunter2" not in out
    assert "abcd1234efgh" not in out
    assert "name=alice" in out


def test_private_keys_are_redacted():
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
    assert "MIIEow" not in redact(f"before\n{key}\nafter")


def test_plain_text_is_unchanged():
    assert redact("all tests passed in 0.3s") == "all tests passed in 0.3s"


def test_logging_filter_redacts_messages():
    record = logging.LogRecord("x", logging.INFO, "f", 1, "token %s", ("ghp_" + "a" * 30,), None)
    RedactingFilter().filter(record)
    assert "ghp_" not in record.getMessage()


def test_structured_redaction_keeps_json_valid():
    import json

    from crew.security.redaction import redact_data, redact_json_text

    payload = {"summary": 'set PASSWORD=hunter2\\" then run', "nested": [{"t": "TOKEN=abcd\\\\x"}]}
    text = json.dumps(payload)
    out = json.loads(redact_json_text(text))  # must still parse
    assert "hunter2" not in out["summary"]
    assert "abcd" not in out["nested"][0]["t"]
    assert redact_data(5) == 5


def test_text_redaction_of_json_would_corrupt_it():
    """Documents why structured redaction exists (regression guard for H1)."""
    import json

    text = json.dumps({"s": 'PASSWORD=hunter2" x'})
    with pytest.raises(json.JSONDecodeError):
        json.loads(redact(text))
