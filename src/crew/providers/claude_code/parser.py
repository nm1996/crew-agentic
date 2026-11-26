"""Defensive parsing of ``claude -p --output-format json`` output.

The CLI prints a single JSON result object. Observed fields (CLI 2.x):
``type``, ``subtype``, ``is_error``, ``result`` (final text), ``structured_output``
(present when ``--json-schema`` is used), ``session_id``, ``total_cost_usd``,
``duration_ms``, ``num_turns``, ``usage``. Any of them may be missing in other
versions, so every access is optional and failures map to ProviderError.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from crew.domain.errors import ProviderError

_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


@dataclass
class ParsedResult:
    output: dict[str, Any]
    session_id: str | None
    cost_usd: float | None
    num_turns: int | None
    result_text: str
    usage: dict[str, int]
    models: list[str]


USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _load_envelope(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise ProviderError("Claude CLI produced no output", kind="empty_output")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Some versions may emit progress lines before the result; take the last JSON line.
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        else:
            raise ProviderError("Claude CLI output is not JSON", kind="malformed_output") from None
    if isinstance(data, list):  # stream-json style array: take the result message
        results = [m for m in data if isinstance(m, dict) and m.get("type") == "result"]
        data = results[-1] if results else {}
    if not isinstance(data, dict):
        raise ProviderError("Claude CLI output is not a JSON object", kind="malformed_output")
    return data


_USAGE_LIMIT = re.compile(
    r"(hit your .{0,40}limit|usage limit|rate limit|rate_limit|too many requests|\b429\b"
    r"|overloaded)",
    re.IGNORECASE,
)


def is_usage_limit(text: str) -> bool:
    """True for subscription/usage limits and API rate limiting (not retryable now)."""
    return bool(_USAGE_LIMIT.search(text or ""))


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from free text (bare, fenced, or embedded)."""
    candidates = [text.strip()]
    if m := _FENCE.search(text):
        candidates.append(m.group(1))
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ProviderError("agent response does not contain a JSON object", kind="malformed_output")


def parse_cli_output(stdout: str) -> ParsedResult:
    envelope = _load_envelope(stdout)
    result_text = envelope.get("result") if isinstance(envelope.get("result"), str) else ""
    if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
        detail = result_text or envelope.get("subtype") or "unknown error"
        if is_usage_limit(detail):
            raise ProviderError(
                f"Claude usage limit reached: {detail}"[:2000],
                kind="rate_limited",
                hint="Wait until the limit resets, then continue with `crew retry <run-id>`.",
            )
        raise ProviderError(f"Claude CLI reported an error: {detail}"[:2000], kind="provider_error")

    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        output = structured
    elif isinstance(structured, str):
        output = extract_json_object(structured)
    else:
        output = extract_json_object(result_text)

    cost = envelope.get("total_cost_usd")
    turns = envelope.get("num_turns")
    session = envelope.get("session_id")
    raw_usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    usage = {k: int(raw_usage[k]) for k in USAGE_FIELDS if isinstance(raw_usage.get(k), int)}
    model_usage = envelope.get("modelUsage")
    models = sorted(model_usage) if isinstance(model_usage, dict) else []
    return ParsedResult(
        output=output,
        session_id=session if isinstance(session, str) else None,
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        num_turns=turns if isinstance(turns, int) else None,
        result_text=result_text,
        usage=usage,
        models=models,
    )
