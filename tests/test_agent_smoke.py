from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from reg_change_desk.agent_review import run_ollama_review  # noqa: E402
from reg_change_desk.core import SourceInfo, compare_snapshots, make_snapshot  # noqa: E402


@unittest.skipUnless(os.environ.get("RUN_OLLAMA_SMOKE") == "1", "set RUN_OLLAMA_SMOKE=1 for local model test")
class OllamaSmokeTest(unittest.TestCase):
    def test_agent_uses_required_tools_and_returns_grounded_packet(self) -> None:
        source = SourceInfo(
            "simulated-demo-source",
            "SIMULATION ONLY - Demo Source Regulation",
            "Fictional demo",
            "https://example.invalid/simulated-regulation",
            True,
        )
        old = make_snapshot(source, (PROJECT / "fixtures" / "demo_before.txt").read_bytes(), "text/plain")
        new = make_snapshot(source, (PROJECT / "fixtures" / "demo_after.txt").read_bytes(), "text/plain")
        evidence = compare_snapshots(old, new)
        catalog = json.loads((PROJECT / "data" / "lesson_catalog.json").read_text(encoding="utf-8"))
        packet = run_ollama_review(evidence, catalog, model_id="gpt-oss:20b")
        self.assertEqual(packet["status"], "draft")
        self.assertTrue(
            {"read_change_evidence", "lookup_lessons", "record_lesson_impact", "finalize_review"}.issubset(
                packet["tools_called"]
            )
        )
        self.assertGreaterEqual(len(packet["affected_lessons"]), 1)
        changed_lines = set(evidence.added_lines) | set(evidence.removed_lines)
        for match in packet["affected_lessons"]:
            self.assertIn(match["supporting_excerpt"], changed_lines)


if __name__ == "__main__":
    unittest.main()

