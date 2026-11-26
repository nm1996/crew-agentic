"""Build configured adapters. Raises IntegrationError when one is not configured."""

from __future__ import annotations

from pathlib import Path

from crew.config.schema import CrewConfig, FolderDocsSettings
from crew.integrations.base import CodeHost, DocsTarget, IssueTracker
from crew.integrations.docs import ConfluenceDocs, FolderDocs
from crew.integrations.github import GitHubHost
from crew.integrations.http import IntegrationError, Transport
from crew.integrations.jira import JiraTracker


def issue_tracker(config: CrewConfig, *, transport: Transport | None = None) -> IssueTracker:
    settings = config.integrations.issue_tracker
    if settings is None:
        raise IntegrationError(
            "no issue tracker configured",
            hint="Add integrations.issue_tracker (kind: jira, base_url: …) to .crew/config.yaml.",
        )
    return JiraTracker(settings, transport=transport)


def code_host(config: CrewConfig, *, transport: Transport | None = None) -> CodeHost:
    settings = config.integrations.code_host
    if settings is None:
        raise IntegrationError(
            "no code host configured",
            hint="Add integrations.code_host (kind: github, repo: owner/name) to "
            ".crew/config.yaml.",
        )
    return GitHubHost(settings, transport=transport)


def docs_target(
    config: CrewConfig, project_root: Path, *, transport: Transport | None = None
) -> DocsTarget:
    settings = config.integrations.docs
    if settings is None:
        raise IntegrationError(
            "no documentation target configured",
            hint="Add integrations.docs (kind: confluence … or kind: folder) to .crew/config.yaml.",
        )
    if isinstance(settings, FolderDocsSettings):
        return FolderDocs(settings, project_root)
    return ConfluenceDocs(settings, transport=transport)
