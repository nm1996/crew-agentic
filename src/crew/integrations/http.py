"""Minimal JSON-over-HTTPS client (standard library only).

Adapters use :class:`HttpClient` so tests can inject a fake transport and no
network is needed. Errors are normalised to :class:`IntegrationError` with the
response body redacted and truncated.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from crew import __version__
from crew.domain.errors import CrewError
from crew.security.redaction import redact

DEFAULT_TIMEOUT_SECONDS = 30


class IntegrationError(CrewError):
    exit_code = 13


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None


# (method, url, headers, body) -> response; replaced by a fake in tests.
Transport = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> HttpResponse:
    if not url.startswith("https://") and not url.startswith("http://localhost"):
        raise IntegrationError(f"refusing non-HTTPS integration URL: {url}")
    request = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as resp:  # noqa: S310
            return HttpResponse(resp.status, resp.read())
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read() or b"")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise IntegrationError(f"{method} {url} failed: {exc}") from exc


def basic_auth(user: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()


class HttpClient:
    def __init__(
        self, base_url: str, auth_header: str, *, transport: Transport | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth = auth_header
        self._transport = transport or urllib_transport

    def request(self, method: str, path: str, payload: Any = None) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        headers = {
            "Authorization": self._auth,
            "Accept": "application/json",
            "User-Agent": f"crew-cli/{__version__}",
        }
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        response = self._transport(method, url, headers, body)
        if response.status >= 400:
            detail = redact(response.body.decode("utf-8", errors="replace"))[:500]
            raise IntegrationError(f"{method} {url} returned HTTP {response.status}: {detail}")
        return response.json()

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, payload: Any) -> Any:
        return self.request("POST", path, payload)

    def put(self, path: str, payload: Any) -> Any:
        return self.request("PUT", path, payload)
