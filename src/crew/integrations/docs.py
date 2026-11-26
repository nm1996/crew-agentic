"""Documentation adapters: Confluence Cloud (REST v2) and a local Markdown folder."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from crew.config.schema import ConfluenceSettings, FolderDocsSettings
from crew.integrations.base import DocPage
from crew.integrations.http import HttpClient, IntegrationError, Transport, basic_auth
from crew.integrations.jira import require_env
from crew.integrations.markup import markdown_to_storage, storage_to_text

_PAGE_ID_IN_URL = re.compile(r"/pages/(\d+)")


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    return slug[:80] or "page"


class ConfluenceDocs:
    name = "confluence"

    def __init__(self, settings: ConfluenceSettings, *, transport: Transport | None = None) -> None:
        self.settings = settings
        email = require_env(settings.email_env, "Confluence")
        token = require_env(settings.token_env, "Confluence")
        self.http = HttpClient(settings.base_url, basic_auth(email, token), transport=transport)

    def _page_id(self, ref: str) -> str:
        ref = ref.strip()
        if ref.isdigit():
            return ref
        if m := _PAGE_ID_IN_URL.search(ref):
            return m.group(1)
        raise IntegrationError(f"cannot find a Confluence page id in {ref!r}")

    def _url(self, links: dict) -> str:
        webui = links.get("webui", "")
        return f"{self.settings.base_url.rstrip('/')}/wiki{webui}" if webui else ""

    def fetch_page(self, ref: str) -> DocPage:
        page_id = self._page_id(ref)
        data = self.http.get(f"/wiki/api/v2/pages/{quote(page_id)}?body-format=storage")
        storage = ((data.get("body") or {}).get("storage") or {}).get("value", "")
        return DocPage(
            ref=ref,
            title=str(data.get("title") or page_id),
            body=storage_to_text(storage),
            url=self._url(data.get("_links") or {}),
        )

    def publish_page(self, title: str, markdown: str) -> str:
        if not self.settings.space_id:
            raise IntegrationError(
                "publishing to Confluence needs integrations.docs.space_id",
                hint="Find it in the space settings or via /wiki/api/v2/spaces.",
            )
        payload = {
            "spaceId": self.settings.space_id,
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": markdown_to_storage(markdown)},
        }
        if self.settings.parent_id:
            payload["parentId"] = self.settings.parent_id
        data = self.http.post("/wiki/api/v2/pages", payload)
        return self._url(data.get("_links") or {}) or str(data.get("id", ""))


class FolderDocs:
    """Read and write Markdown pages in a folder inside the repository.

    Publishing creates a *new* file only; it never overwrites an existing one.
    """

    name = "folder"

    def __init__(self, settings: FolderDocsSettings, project_root: Path) -> None:
        self.root = project_root.resolve()
        self.folder = (self.root / settings.path).resolve()
        if self.root not in (self.folder, *self.folder.parents):
            raise IntegrationError(f"docs folder {settings.path!r} must be inside the repository")

    def fetch_page(self, ref: str) -> DocPage:
        path = (self.root / ref).resolve()
        if self.root not in path.parents or not path.is_file():
            raise IntegrationError(f"documentation file {ref!r} not found inside the repository")
        text = path.read_text(encoding="utf-8", errors="replace")
        heading = next(
            (ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), path.stem
        )
        return DocPage(ref=ref, title=heading, body=text, url=str(path.relative_to(self.root)))

    def publish_page(self, title: str, markdown: str) -> str:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{slugify(title)}.md"
        if path.exists():
            raise IntegrationError(f"{path.relative_to(self.root)} already exists; not overwriting")
        path.write_text(markdown, encoding="utf-8")
        return str(path.relative_to(self.root))
