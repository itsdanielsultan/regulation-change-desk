from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher, unified_diff
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


MAX_EVIDENCE_LINES = 24
MAX_EVIDENCE_LINE_LENGTH = 280
MAX_EVIDENCE_DIFF_LENGTH = 12000


class _VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._hidden_depth += 1
        elif self._hidden_depth == 0 and tag.lower() in {
            "p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "h5", "br"
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._hidden_depth = max(0, self._hidden_depth - 1)
        elif self._hidden_depth == 0 and tag.lower() in {
            "p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "h5"
        }:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._hidden_depth == 0:
            self.parts.append(data)


@dataclass(frozen=True)
class SourceInfo:
    source_id: str
    title: str
    jurisdiction: str
    official_url: str
    simulated: bool = False


@dataclass(frozen=True)
class Snapshot:
    source: SourceInfo
    retrieved_at: str
    content_type: str
    raw_sha256: str
    canonical_sha256: str
    canonical_text: str
    http_status: int | None = None
    etag: str | None = None
    last_modified: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Snapshot":
        return cls(source=SourceInfo(**value["source"]), **{k: v for k, v in value.items() if k != "source"})


@dataclass(frozen=True)
class ChangeEvidence:
    change_id: str
    source: SourceInfo
    classification: str
    retrieved_at: str
    old_raw_sha256: str
    new_raw_sha256: str
    old_canonical_sha256: str
    new_canonical_sha256: str
    added_lines: tuple[str, ...]
    removed_lines: tuple[str, ...]
    changed_sections: tuple[str, ...]
    unified_diff: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ChangeEvidence":
        return cls(
            source=SourceInfo(**value["source"]),
            added_lines=tuple(value["added_lines"]),
            removed_lines=tuple(value["removed_lines"]),
            changed_sections=tuple(value["changed_sections"]),
            **{
                k: v
                for k, v in value.items()
                if k not in {"source", "added_lines", "removed_lines", "changed_sections"}
            },
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def extract_text(raw: bytes, content_type: str) -> str:
    decoded = raw.decode("utf-8-sig", errors="replace")
    lowered = content_type.lower()
    if "xml" in lowered:
        try:
            root = ElementTree.fromstring(decoded)
            return "\n".join(part for part in root.itertext())
        except ElementTree.ParseError as exc:
            raise ValueError(f"Invalid XML source: {exc}") from exc
    if "html" in lowered:
        parser = _VisibleHTML()
        parser.feed(decoded)
        return "".join(parser.parts)
    return decoded


def canonicalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    lines: list[str] = []
    for raw_line in normalized.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"[\t ]+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def make_snapshot(
    source: SourceInfo,
    raw: bytes,
    content_type: str,
    *,
    retrieved_at: str | None = None,
    http_status: int | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
) -> Snapshot:
    canonical_text = canonicalize_text(extract_text(raw, content_type))
    return Snapshot(
        source=source,
        retrieved_at=retrieved_at or utc_now(),
        content_type=content_type,
        raw_sha256=sha256_bytes(raw),
        canonical_sha256=sha256_bytes(canonical_text.encode("utf-8")),
        canonical_text=canonical_text,
        http_status=http_status,
        etag=etag,
        last_modified=last_modified,
    )


_HEADING = re.compile(r"^(?:section|part|division|schedule|article|chapter)\b", re.IGNORECASE)


def _nearest_heading(lines: list[str], index: int) -> str:
    for candidate in reversed(lines[: index + 1]):
        if _HEADING.match(candidate):
            return candidate
    return "Unlabelled passage"


def _check_evidence_limits(added: list[str], removed: list[str], sections: list[str]) -> None:
    # Count occurrences across both directions: repeated or replaced lines still
    # represent changes the reviewer must see, not duplicates to discard.
    changed_count = len(added) + len(removed)
    if changed_count > MAX_EVIDENCE_LINES:
        raise ValueError(
            f"Evidence limit exceeded: {changed_count} changed lines exceed "
            f"the {MAX_EVIDENCE_LINES}-line limit; complete manual review is required"
        )
    for label, values in (("added line", added), ("removed line", removed), ("section heading", sections)):
        if any(len(value) > MAX_EVIDENCE_LINE_LENGTH for value in values):
            raise ValueError(
                f"Evidence limit exceeded: {label} exceeds "
                f"{MAX_EVIDENCE_LINE_LENGTH} characters; complete manual review is required"
            )


def compare_snapshots(old: Snapshot, new: Snapshot) -> ChangeEvidence:
    if old.source.source_id != new.source.source_id:
        raise ValueError("Snapshots belong to different sources")
    if old.raw_sha256 == new.raw_sha256:
        classification = "unchanged"
    elif old.canonical_sha256 == new.canonical_sha256:
        classification = "formatting_only"
    else:
        classification = "substantive"

    old_lines = old.canonical_text.splitlines()
    new_lines = new.canonical_text.splitlines()
    added: list[str] = []
    removed: list[str] = []
    sections: list[str] = []
    if classification == "substantive":
        matcher = SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            removed.extend(old_lines[i1:i2])
            added.extend(new_lines[j1:j2])
            if i1 < len(old_lines):
                sections.append(_nearest_heading(old_lines, i1))
            if j1 < len(new_lines):
                sections.append(_nearest_heading(new_lines, j1))
        _check_evidence_limits(added, removed, sections)

    diff_text = "\n".join(
        unified_diff(old_lines, new_lines, fromfile="before", tofile="after", lineterm="")
    )
    if len(diff_text) > MAX_EVIDENCE_DIFF_LENGTH:
        raise ValueError(
            f"Evidence limit exceeded: unified diff exceeds {MAX_EVIDENCE_DIFF_LENGTH} "
            "characters; complete manual review is required"
        )
    change_seed = f"{old.source.source_id}\0{old.canonical_sha256}\0{new.canonical_sha256}".encode()
    change_id = hashlib.sha256(change_seed).hexdigest()[:20]
    return ChangeEvidence(
        change_id=change_id,
        source=new.source,
        classification=classification,
        retrieved_at=new.retrieved_at,
        old_raw_sha256=old.raw_sha256,
        new_raw_sha256=new.raw_sha256,
        old_canonical_sha256=old.canonical_sha256,
        new_canonical_sha256=new.canonical_sha256,
        added_lines=tuple(added),
        removed_lines=tuple(removed),
        changed_sections=tuple(dict.fromkeys(sections)),
        unified_diff=diff_text,
    )


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
