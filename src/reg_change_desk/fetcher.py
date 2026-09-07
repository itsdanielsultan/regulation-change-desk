from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .core import SourceInfo, Snapshot, make_snapshot


MAX_DOWNLOAD_BYTES = 3_000_000
USER_AGENT = "RegulationChangeDesk/0.1 (local review prototype; no bulk crawling)"


@dataclass(frozen=True)
class SourceDefinition:
    id: str
    title: str
    publisher: str
    jurisdiction: str
    url: str
    allowed_host: str
    allowed_path_prefix: str
    content_format: str
    attribution: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceDefinition":
        allowed = {field for field in cls.__dataclass_fields__}
        return cls(**{key: value[key] for key in allowed})


def load_manifest(path: Path) -> dict[str, SourceDefinition]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    definitions = [SourceDefinition.from_dict(entry) for entry in entries]
    return {definition.id: definition for definition in definitions}


def validate_source_url(definition: SourceDefinition, value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise ValueError("Only HTTPS sources are allowed")
    if parsed.hostname != definition.allowed_host:
        raise ValueError("Source host is outside the manifest allowlist")
    if parsed.port not in {None, 443}:
        raise ValueError("Non-standard ports are not allowed")
    # Retain the manifest field name for compatibility; its value is an exact
    # approved document path, not a directory or string-prefix permission.
    if parsed.path != definition.allowed_path_prefix or parsed.params:
        raise ValueError("Source path is outside the manifest allowlist")
    approved = urlparse(definition.url)
    if parsed.query != approved.query or parsed.fragment != approved.fragment:
        raise ValueError("Source query or fragment is outside the manifest allowlist")
    if parsed.username or parsed.password:
        raise ValueError("Credentials are not allowed in source URLs")


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, definition: SourceDefinition) -> None:
        super().__init__()
        self.definition = definition

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib calls this before opening the redirect target. A final-response
        # URL check alone would reject the result only after contacting it.
        try:
            validate_source_url(self.definition, newurl)
        except ValueError:
            fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_snapshot(
    definition: SourceDefinition,
    *,
    opener: Callable[..., Any] | None = None,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> Snapshot:
    """Fetch an approved source with guarded redirects.

    A custom opener remains available for trusted transports and deterministic
    tests. It must not follow redirects itself without the same validation.
    """
    validate_source_url(definition, definition.url)
    if opener is None or opener is urllib.request.urlopen:
        opener = urllib.request.build_opener(_AllowlistedRedirectHandler(definition)).open
    request = urllib.request.Request(
        definition.url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,text/html;q=0.9"},
        method="GET",
    )
    with opener(request, timeout=20) as response:
        final_url = response.geturl()
        validate_source_url(definition, final_url)
        status = getattr(response, "status", None)
        if status is not None and status != 200:
            raise RuntimeError(f"Official source returned HTTP {status}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(65_536, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"Source exceeded {max_bytes} byte cap")
            chunks.append(chunk)
        headers = response.headers
        content_type = headers.get("Content-Type", definition.content_format)
        return make_snapshot(
            SourceInfo(
                source_id=definition.id,
                title=definition.title,
                jurisdiction=definition.jurisdiction,
                official_url=definition.url,
            ),
            b"".join(chunks),
            content_type,
            http_status=status,
            etag=headers.get("ETag"),
            last_modified=headers.get("Last-Modified"),
        )
