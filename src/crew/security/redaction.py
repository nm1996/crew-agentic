"""Secret redaction applied to anything CREW logs, persists or reports.

This is defence in depth, not a guarantee: patterns catch common token formats
and ``KEY=value`` assignments whose key looks secret-bearing.
"""

from __future__ import annotations

import logging
import re

REDACTED = "[REDACTED]"

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),  # Anthropic
    re.compile(r"sk-[A-Za-z0-9]{20,}"),  # OpenAI-style
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),  # Slack
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),  # Google API key
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
]

# KEY=value / KEY: value where KEY suggests a secret.
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY|CREDENTIAL)[A-Z0-9_]*)"
    r"(\s*[=:]\s*)(['\"]?)([^\s'\"]{4,})\3"
)


def redact(text: str | None) -> str:
    if not text:
        return text or ""
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


def redact_args(args: list[str]) -> list[str]:
    return [redact(a) for a in args]


class RedactingFilter(logging.Filter):
    """Logging filter that redacts the rendered message before it is emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True
