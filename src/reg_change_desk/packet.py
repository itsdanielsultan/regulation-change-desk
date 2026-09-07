from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import ChangeEvidence, Snapshot
from .safe_io import atomic_write_json, within


VALID_CONFIDENCE = {"high", "medium", "low"}
VALID_DECISIONS = {"approve", "needs-work", "reject"}


@dataclass(frozen=True)
class PacketValidationResult:
    packet: dict[str, Any]


def validate_assessment(
    assessment: dict[str, Any], evidence: ChangeEvidence, catalog: list[dict[str, Any]]
) -> PacketValidationResult:
    if evidence.classification != "substantive":
        raise ValueError("An agent packet is allowed only for substantive changes")
    allowed_ids = {entry["id"] for entry in catalog}
    evidence_lines = set(evidence.added_lines) | set(evidence.removed_lines)
    summary = str(assessment.get("summary", "")).strip()
    uncertainty = str(assessment.get("uncertainty", "")).strip()
    if not summary or len(summary) > 500:
        raise ValueError("Summary must contain 1 to 500 characters")
    if not uncertainty or len(uncertainty) > 500:
        raise ValueError("Uncertainty must contain 1 to 500 characters")

    validated_matches: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_match in assessment.get("affected_lessons", []):
        lesson_id = str(raw_match.get("lesson_id", ""))
        excerpt = str(raw_match.get("supporting_excerpt", ""))
        reason = str(raw_match.get("reason", "")).strip()
        confidence = str(raw_match.get("confidence", "")).lower()
        if lesson_id not in allowed_ids:
            raise ValueError(f"Unknown lesson ID: {lesson_id}")
        if lesson_id in seen_ids:
            raise ValueError(f"Duplicate lesson ID: {lesson_id}")
        if excerpt not in evidence_lines:
            raise ValueError("Every supporting excerpt must exactly match a changed evidence line")
        if not reason or len(reason) > 350:
            raise ValueError("Each mapping reason must contain 1 to 350 characters")
        if confidence not in VALID_CONFIDENCE:
            raise ValueError(f"Invalid confidence: {confidence}")
        seen_ids.add(lesson_id)
        validated_matches.append(
            {
                "lesson_id": lesson_id,
                "reason": reason,
                "supporting_excerpt": excerpt,
                "confidence": confidence,
            }
        )

    base = {
        "schema_version": 2,
        "change_id": evidence.change_id,
        "status": "draft",
        "summary": summary,
        "affected_lessons": validated_matches,
        "uncertainty": uncertainty,
        "legal_review_needed": bool(assessment.get("legal_review_needed", True)),
        "source": evidence.to_dict()["source"],
        "retrieved_at": evidence.retrieved_at,
        "classification": evidence.classification,
        "hashes": {
            "old_raw_sha256": evidence.old_raw_sha256,
            "new_raw_sha256": evidence.new_raw_sha256,
            "old_canonical_sha256": evidence.old_canonical_sha256,
            "new_canonical_sha256": evidence.new_canonical_sha256,
        },
        "changed_sections": list(evidence.changed_sections),
        "added_lines": list(evidence.added_lines),
        "removed_lines": list(evidence.removed_lines),
    }
    packet_seed = json.dumps(base, sort_keys=True, ensure_ascii=False).encode("utf-8")
    base["packet_id"] = hashlib.sha256(packet_seed).hexdigest()[:20]
    return PacketValidationResult(packet=base)


def apply_human_decision(
    packet: dict[str, Any],
    decision: str,
    *,
    candidate_snapshot: Snapshot,
    state_root: Path,
    confirmation: str | None = None,
    actor: str = "local-reviewer",
) -> dict[str, Any]:
    if decision not in VALID_DECISIONS:
        raise ValueError(f"Invalid decision: {decision}")
    if packet.get("status") != "draft":
        raise ValueError("Only a draft packet can receive a decision")
    if packet.get("change_id") is None:
        raise ValueError("Packet has no change ID")
    if decision == "approve" and confirmation != "APPROVE_BASELINE":
        raise ValueError("Approval requires the exact confirmation APPROVE_BASELINE")
    if decision == "approve" and packet.get("schema_version") != 2:
        raise ValueError("Legacy packet: regenerate a complete-evidence review before approval")

    packet_id = str(packet.get("packet_id", ""))
    source_id = candidate_snapshot.source.source_id
    if not re.fullmatch(r"[0-9a-f]{20}", packet_id):
        raise ValueError("Invalid packet ID")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", source_id):
        raise ValueError("Invalid source ID")
    hashes = packet.get("hashes", {})
    actual_canonical_hash = hashlib.sha256(candidate_snapshot.canonical_text.encode("utf-8")).hexdigest()
    if (
        candidate_snapshot.to_dict()["source"] != packet.get("source")
        or candidate_snapshot.raw_sha256 != hashes.get("new_raw_sha256")
        or candidate_snapshot.canonical_sha256 != hashes.get("new_canonical_sha256")
        or actual_canonical_hash != candidate_snapshot.canonical_sha256
        or candidate_snapshot.retrieved_at != packet.get("retrieved_at")
    ):
        raise ValueError("Candidate snapshot does not match the reviewed packet")

    decided = dict(packet)
    decided["status"] = {"approve": "approved", "needs-work": "needs-work", "reject": "rejected"}[decision]
    decided["decision"] = {
        "actor": actor,
        "decision": decision,
        "decided_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    decision_path = within(state_root, state_root / "decisions" / f"{packet_id}.json")
    baseline_path = within(state_root, state_root / "baselines" / f"{source_id}.json")
    lock_path = within(state_root, state_root / "locks" / f"{source_id}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = lock_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ValueError("Another decision for this source is in progress; review its state first") from exc
    try:
        with lock:
            if decision_path.exists():
                raise ValueError("This packet already has a recorded decision; create a new review")
            if decision == "approve" and baseline_path.exists():
                previous = Snapshot.from_dict(json.loads(baseline_path.read_text(encoding="utf-8")))
                previous_hash = hashlib.sha256(previous.canonical_text.encode("utf-8")).hexdigest()
                if (
                    previous.source != candidate_snapshot.source
                    or previous.raw_sha256 != hashes.get("old_raw_sha256")
                    or previous.canonical_sha256 != hashes.get("old_canonical_sha256")
                    or previous_hash != previous.canonical_sha256
                ):
                    raise ValueError("Baseline changed since this review; create a new review")
            atomic_write_json(decision_path, decided, root=state_root)
            if decision == "approve":
                atomic_write_json(baseline_path, candidate_snapshot.to_dict(), root=state_root)
    finally:
        lock_path.unlink(missing_ok=True)
    return decided


def render_packet_html(packet: dict[str, Any]) -> str:
    esc = lambda value: html.escape(str(value), quote=True)
    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            esc(match["lesson_id"]), esc(match["reason"]), esc(match["confidence"])
        )
        for match in packet.get("affected_lessons", [])
    )
    added = "".join(f"<li>{esc(line)}</li>" for line in packet.get("added_lines", []))
    removed = "".join(f"<li>{esc(line)}</li>" for line in packet.get("removed_lines", []))
    simulated = "<strong>SIMULATED CHANGE</strong>" if packet.get("source", {}).get("simulated") else ""
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Review packet {esc(packet.get('packet_id', ''))}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 20px;color:#17202a}}h1,h2{{line-height:1.2}}.note{{background:#fff4cc;padding:12px}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #ccd3da;text-align:left;padding:10px;vertical-align:top}}code{{overflow-wrap:anywhere}}</style>
<main><p class="note">{simulated} Review aid only. Human approval is required. This is not legal advice.</p>
<h1>{esc(packet.get('summary', ''))}</h1>
<p><strong>Source:</strong> <a href="{esc(packet.get('source', {}).get('official_url', ''))}">{esc(packet.get('source', {}).get('title', ''))}</a></p>
<p><strong>Status:</strong> {esc(packet.get('status', ''))} &nbsp; <strong>Change:</strong> <code>{esc(packet.get('change_id', ''))}</code></p>
<h2>Changed evidence</h2><h3>Added</h3><ul>{added}</ul><h3>Removed</h3><ul>{removed}</ul>
<h2>Possible lesson impact</h2><table><thead><tr><th>Lesson</th><th>Reason</th><th>Confidence</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Uncertainty</h2><p>{esc(packet.get('uncertainty', ''))}</p></main></html>"""
