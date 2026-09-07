from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from reg_change_desk.core import (  # noqa: E402
    MAX_EVIDENCE_DIFF_LENGTH,
    MAX_EVIDENCE_LINE_LENGTH,
    MAX_EVIDENCE_LINES,
    SourceInfo,
    compare_snapshots,
    make_snapshot,
)


SOURCE = SourceInfo("limit-fixture", "SIMULATION ONLY", "Fictional", "https://example.invalid/limits", True)


def snapshot(text: str):
    return make_snapshot(SOURCE, text.encode(), "text/plain", retrieved_at="2026-09-07T00:00:00Z")


class EvidenceLimitTests(unittest.TestCase):
    def test_exact_line_length_is_preserved_in_both_directions(self) -> None:
        line = "x" * MAX_EVIDENCE_LINE_LENGTH
        for old, new, field in (("", line, "added_lines"), (line, "", "removed_lines")):
            with self.subTest(field=field):
                evidence = compare_snapshots(snapshot(old), snapshot(new))
                self.assertEqual(getattr(evidence, field), (line,))
                self.assertIn(line, evidence.unified_diff)

    def test_oversized_changed_line_is_rejected_in_both_directions(self) -> None:
        line = "x" * (MAX_EVIDENCE_LINE_LENGTH + 1)
        for old, new, direction in (("", line, "added"), (line, "", "removed")):
            with self.subTest(direction=direction):
                with self.assertRaisesRegex(ValueError, direction + r" line exceeds 280 characters"):
                    compare_snapshots(snapshot(old), snapshot(new))

    def test_change_after_old_clipping_boundary_is_not_hidden(self) -> None:
        prefix = "x" * MAX_EVIDENCE_LINE_LENGTH
        with self.assertRaisesRegex(ValueError, "Evidence limit exceeded"):
            compare_snapshots(snapshot(prefix + "Allowed"), snapshot(prefix + "Prohibited"))

    def test_exact_total_line_count_preserves_both_sides(self) -> None:
        old = [f"Old rule {i}" for i in range(MAX_EVIDENCE_LINES // 2)]
        new = [f"New rule {i}" for i in range(MAX_EVIDENCE_LINES // 2)]
        evidence = compare_snapshots(snapshot("\n".join(old)), snapshot("\n".join(new)))
        self.assertEqual(evidence.removed_lines, tuple(old))
        self.assertEqual(evidence.added_lines, tuple(new))

    def test_limit_counts_added_and_removed_lines_together(self) -> None:
        old = "\n".join(f"Old rule {i}" for i in range(12))
        new = "\n".join(f"New rule {i}" for i in range(13))
        with self.assertRaisesRegex(ValueError, "25 changed lines exceed the 24-line limit"):
            compare_snapshots(snapshot(old), snapshot(new))

    def test_single_direction_overflow_is_rejected(self) -> None:
        text = "\n".join(f"Rule {i}" for i in range(MAX_EVIDENCE_LINES + 1))
        for old, new in (("", text), (text, "")):
            with self.subTest(removal=bool(old)):
                with self.assertRaisesRegex(ValueError, "25 changed lines exceed the 24-line limit"):
                    compare_snapshots(snapshot(old), snapshot(new))

    def test_repeated_changed_lines_remain_visible(self) -> None:
        repeated = ("Repeated rule",) * MAX_EVIDENCE_LINES
        for old, new, field in (("", "\n".join(repeated), "added_lines"), ("\n".join(repeated), "", "removed_lines")):
            with self.subTest(field=field):
                evidence = compare_snapshots(snapshot(old), snapshot(new))
                self.assertEqual(getattr(evidence, field), repeated)

    def test_repeated_lines_cannot_bypass_total_limit(self) -> None:
        text = "\n".join(["Repeated rule"] * (MAX_EVIDENCE_LINES + 1))
        with self.assertRaisesRegex(ValueError, "25 changed lines exceed the 24-line limit"):
            compare_snapshots(snapshot(""), snapshot(text))

    def test_changed_section_heading_is_never_clipped(self) -> None:
        heading = "Section " + "x" * MAX_EVIDENCE_LINE_LENGTH
        with self.assertRaisesRegex(ValueError, "section heading exceeds 280 characters"):
            compare_snapshots(snapshot(heading + "\nOld rule"), snapshot(heading + "\nNew rule"))

    def test_oversized_diff_context_is_rejected_not_sliced(self) -> None:
        context = "x" * MAX_EVIDENCE_DIFF_LENGTH
        with self.assertRaisesRegex(ValueError, "unified diff exceeds 12000 characters"):
            compare_snapshots(snapshot(context + "\nOld rule"), snapshot(context + "\nNew rule"))

    def test_unchanged_long_text_remains_quiet(self) -> None:
        text = "x" * (MAX_EVIDENCE_LINE_LENGTH + 1)
        evidence = compare_snapshots(snapshot(text), snapshot(text))
        self.assertEqual(evidence.classification, "unchanged")
        self.assertEqual(evidence.added_lines, ())
        self.assertEqual(evidence.unified_diff, "")

    def test_formatting_only_long_text_remains_quiet(self) -> None:
        text = "x" * (MAX_EVIDENCE_LINE_LENGTH + 1)
        evidence = compare_snapshots(snapshot(text), snapshot("  " + text + " \r\n"))
        self.assertEqual(evidence.classification, "formatting_only")
        self.assertEqual(evidence.added_lines, ())
        self.assertEqual(evidence.unified_diff, "")


if __name__ == "__main__":
    unittest.main()
