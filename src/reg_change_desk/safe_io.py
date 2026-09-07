from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class ProjectBoundaryError(ValueError):
    pass


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def within(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ProjectBoundaryError(f"Path leaves project boundary: {candidate}") from exc
    return resolved_candidate


def project_path(*parts: str) -> Path:
    return within(PROJECT_ROOT, PROJECT_ROOT.joinpath(*parts))


def atomic_write_json(path: Path, value: Any, *, root: Path = PROJECT_ROOT) -> None:
    target = within(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent, text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        Path(temp_name).replace(target)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def append_jsonl(path: Path, value: Any, *, root: Path = PROJECT_ROOT) -> None:
    target = within(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")

