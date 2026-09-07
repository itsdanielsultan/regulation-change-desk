from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from .agent_review import run_ollama_review
from .core import ChangeEvidence, Snapshot, SourceInfo, load_json, make_snapshot, compare_snapshots
from .packet import apply_human_decision, render_packet_html
from .safe_io import append_jsonl, atomic_write_json, project_path


def load_catalog() -> list[dict]:
    return load_json(project_path("data", "lesson_catalog.json"))


def prepare_fixture() -> tuple[ChangeEvidence, Snapshot]:
    source = SourceInfo(
        source_id="simulated-demo-source",
        title="SIMULATION ONLY - Demo Source Regulation",
        jurisdiction="Fictional demo",
        official_url="https://example.invalid/simulated-regulation",
        simulated=True,
    )
    before_path = project_path("fixtures", "demo_before.txt")
    after_path = project_path("fixtures", "demo_after.txt")
    old = make_snapshot(source, before_path.read_bytes(), "text/plain", retrieved_at="2026-09-04T12:00:00Z")
    new = make_snapshot(source, after_path.read_bytes(), "text/plain", retrieved_at="2026-09-04T12:05:00Z")
    evidence = compare_snapshots(old, new)
    state = project_path("state")
    atomic_write_json(state / "evidence" / f"{evidence.change_id}.json", evidence.to_dict())
    atomic_write_json(state / "candidates" / f"{evidence.change_id}.json", new.to_dict())
    append_jsonl(
        state / "audit.jsonl",
        {"event": "fixture_prepared", "change_id": evidence.change_id, "classification": evidence.classification},
    )
    return evidence, new


def command_prepare(_: argparse.Namespace) -> int:
    evidence, _ = prepare_fixture()
    print("SIMULATED CHANGE")
    print(f"change_id: {evidence.change_id}")
    print(f"classification: {evidence.classification}")
    print(f"added lines: {len(evidence.added_lines)}; removed lines: {len(evidence.removed_lines)}")
    print(f"evidence: {project_path('state', 'evidence', evidence.change_id + '.json')}")
    return 0


def command_review(args: argparse.Namespace) -> int:
    evidence, _ = prepare_fixture()
    packet = run_ollama_review(evidence, load_catalog(), model_id=args.model, host=args.host)
    packet_path = project_path("state", "packets", f"{packet['packet_id']}.json")
    html_path = project_path("state", "packets", f"{packet['packet_id']}.html")
    atomic_write_json(packet_path, packet)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_packet_html(packet), encoding="utf-8")
    append_jsonl(
        project_path("state", "audit.jsonl"),
        {
            "event": "agent_draft_created",
            "change_id": evidence.change_id,
            "packet_id": packet["packet_id"],
            "model": packet["model"],
            "tools_called": packet["tools_called"],
        },
    )
    print(f"draft packet: {packet_path}")
    print(f"local review page: {html_path}")
    print("Baseline unchanged. Use the decide command for a separate human decision.")
    return 0


def command_decide(args: argparse.Namespace) -> int:
    packet_path = project_path("state", "packets", f"{args.packet_id}.json")
    packet = load_json(packet_path)
    candidate_path = project_path("state", "candidates", f"{packet['change_id']}.json")
    candidate = Snapshot.from_dict(load_json(candidate_path))
    decided = apply_human_decision(
        packet,
        args.decision,
        candidate_snapshot=candidate,
        state_root=project_path("state"),
        confirmation=args.confirm,
    )
    append_jsonl(
        project_path("state", "audit.jsonl"),
        {"event": "human_decision", "packet_id": args.packet_id, "decision": args.decision},
    )
    print(f"packet status: {decided['status']}")
    if args.decision == "approve":
        print("Local baseline advanced. No course or external file was changed.")
    else:
        print("Local baseline unchanged.")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    failures = 0
    try:
        import strands  # noqa: F401
        import ollama  # noqa: F401

        print("Strands/Ollama Python dependencies: ready")
    except ImportError as exc:
        failures += 1
        print(f"Strands/Ollama Python dependencies: missing ({exc})")
    try:
        with urllib.request.urlopen(f"{args.host.rstrip('/')}/api/tags", timeout=3) as response:
            payload = json.load(response)
        model_names = {item.get("name") for item in payload.get("models", [])}
        if args.model in model_names or any(name and name.split(":")[0] == args.model for name in model_names):
            print(f"Ollama server and model: ready ({args.model})")
        else:
            failures += 1
            print(f"Ollama server: ready; model not listed ({args.model})")
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        failures += 1
        print(f"Ollama server: unavailable ({exc})")
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reg-change-desk",
        description="Prepare evidence-bound regulatory change reviews inside this project folder.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-fixture", help="Build deterministic evidence from fictional text")
    prepare.set_defaults(func=command_prepare)

    review = subparsers.add_parser("review-fixture", help="Run the Strands/Ollama reviewer on the fixture")
    review.add_argument("--model", default="gpt-oss:20b")
    review.add_argument("--host", default="http://localhost:11434")
    review.set_defaults(func=command_review)

    decide = subparsers.add_parser("decide", help="Record a separate human decision")
    decide.add_argument("packet_id")
    decide.add_argument("--decision", required=True, choices=["approve", "needs-work", "reject"])
    decide.add_argument("--confirm", help="Approval requires the exact value APPROVE_BASELINE")
    decide.set_defaults(func=command_decide)

    doctor = subparsers.add_parser("doctor", help="Check local model prerequisites")
    doctor.add_argument("--model", default="gpt-oss:20b")
    doctor.add_argument("--host", default="http://localhost:11434")
    doctor.set_defaults(func=command_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
