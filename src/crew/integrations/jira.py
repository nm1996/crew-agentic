"""Jira Cloud (REST API v3) issue-tracker adapter.

Credentials: an Atlassian account e-mail and API token from the environment
variables named in ``integrations.issue_tracker`` (default ``JIRA_EMAIL`` /
``JIRA_API_TOKEN``), sent as HTTP basic auth.
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote

from crew.config.schema import JiraSettings
from crew.integrations.adf import adf_to_text, text_to_adf
from crew.integrations.base import Issue
from crew.integrations.http import HttpClient, IntegrationError, Transport, basic_auth

ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


def require_env(name: str, purpose: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise IntegrationError(
            f"{purpose} needs the environment variable {name}",
            hint=f"export {name}=… (CREW never stores credentials in its config or database)",
        )
    return value


class JiraTracker:
    name = "jira"

    def __init__(self, settings: JiraSettings, *, transport: Transport | None = None) -> None:
        self.settings = settings
        email = require_env(settings.email_env, "Jira")
        token = require_env(settings.token_env, "Jira")
        self.http = HttpClient(settings.base_url, basic_auth(email, token), transport=transport)

    def _key(self, key: str) -> str:
        key = key.strip().upper()
        if not ISSUE_KEY_RE.match(key):
            raise IntegrationError(f"{key!r} is not a Jira issue key (e.g. PROJ-123)")
        return key

    def fetch_issue(self, key: str) -> Issue:
        key = self._key(key)
        data = self.http.get(
            f"/rest/api/3/issue/{quote(key)}?fields=summary,description,status,labels"
        )
        fields = data.get("fields", {}) if isinstance(data, dict) else {}
        return Issue(
            key=key,
            title=str(fields.get("summary") or ""),
            description=adf_to_text(fields.get("description")),
            url=f"{self.settings.base_url.rstrip('/')}/browse/{key}",
            status=(fields.get("status") or {}).get("name"),
            labels=tuple(fields.get("labels") or ()),
        )

    def comment(self, key: str, text: str) -> None:
        self.http.post(
            f"/rest/api/3/issue/{quote(self._key(key))}/comment", {"body": text_to_adf(text)}
        )

    def transition(self, key: str, state: str) -> None:
        key = self._key(key)
        data = self.http.get(f"/rest/api/3/issue/{quote(key)}/transitions")
        transitions = data.get("transitions", []) if isinstance(data, dict) else []
        wanted = state.casefold()
        for t in transitions:
            target = (t.get("to") or {}).get("name", "")
            if wanted in (str(t.get("name", "")).casefold(), str(target).casefold()):
                self.http.post(
                    f"/rest/api/3/issue/{quote(key)}/transitions", {"transition": {"id": t["id"]}}
                )
                return
        names = ", ".join(str(t.get("name")) for t in transitions) or "none"
        raise IntegrationError(f"Jira issue {key} has no transition {state!r} (available: {names})")
