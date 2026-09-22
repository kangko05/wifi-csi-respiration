"""Post-hoc manual evaluation: nominal 120 s denominator, empty rooms, missing counts."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import posthoc_manual_eval as ev  # noqa: E402


def ctx(sid, occupancy, distance=None, group=None):
    return dict(session_id=sid, occupancy=occupancy, distance_cm=distance,
                group=group or ("empty_complete" if occupancy == "0" else f"{int(distance)}cm"))


def pred(sid, method, status, diag, cand, duration="117.9", res="0.5085"):
    return dict(session_id=sid, method=method, status=status, diagnostic_bpm=diag,
                candidate_bpm=cand, resolution_bpm=res, analysis_duration_s=duration, reasons="")


class ReferenceMappingTest(unittest.TestCase):
    def test_real_source_maps_nineteen_values_and_leaves_last_two_unavailable(self):
        contexts = [ctx("s00", "0")] + [ctx(f"s{i:02d}", "1", 100.0) for i in range(1, 19)] \
            + [ctx("s19", "0"), ctx("s20", "0", group="empty_operator_interrupted")]
        rows = ev.build_reference_rows(contexts, ev.SOURCE_TEXT)
        self.assertEqual(len(ev.SOURCE_TEXT.split()), 19)
        self.assertEqual([r["count_status"] for r in rows].count("user_reported"), 19)
        self.assertEqual(sum(r["reference_role"] == "occupied_rate" for r in rows), 18)
        self.assertEqual(rows[0]["reference_role"], "occupancy_absence")
        self.assertEqual(rows[0]["reference_bpm"], "")
        self.assertEqual(rows[1]["reference_bpm"], 12.0)  # 24 / 2
        self.assertEqual([r["count_status"] for r in rows[19:]], ["not_supplied"] * 2)
        self.assertEqual([r["count"] for r in rows[19:]], ["", ""])

    def test_inconsistent_occupancy_or_extra_counts_are_refused(self):
        with self.assertRaises(ValueError):
            ev.build_reference_rows([ctx("a", "1", 30.0)], "0")
        with self.assertRaises(ValueError):
            ev.build_reference_rows([ctx("a", "0")], "5")
        with self.assertRaises(ValueError):
            ev.build_reference_rows([ctx("a", "0")], "0 20")


class EvaluationTest(unittest.TestCase):
    def test_target_uses_nominal_interval_not_cropped_analysis_span(self):
        refs = {r["session_id"]: r for r in
                ev.build_reference_rows([ctx("occ", "1", 500.0)], "30")}
        rows = ev.evaluate_rows([pred("occ", "amplitude", "withheld", "15.2", "", duration="117.9")], refs)
        self.assertEqual(rows[0]["reference_bpm"], 15.0)  # not 30*60/117.9
        self.assertAlmostEqual(rows[0]["diagnostic_error_bpm"], 0.2)
        self.assertIsNone(rows[0]["accepted_error_bpm"])

    def test_empty_rooms_are_candidates_not_zero_bpm_errors(self):
        contexts = [ctx("e0", "0"), ctx("e1", "0")]
        refs = {r["session_id"]: r for r in ev.build_reference_rows(contexts, "0")}
        rows = ev.evaluate_rows([pred("e0", "cir", "accepted", "39.9", "39.9"),
                                 pred("e1", "cir", "withheld", "23.4", "")], refs)
        self.assertTrue(all(r["reference_bpm"] is None for r in rows))
        self.assertTrue(all(r["diagnostic_error_bpm"] is None for r in rows))
        self.assertEqual([r["empty_room_accepted_candidate"] for r in rows], [True, False])
        summary = ev.summarize(rows)
        cir = next(e for e in summary if e["group"] == "empty_complete" and e["method"] == "cir")
        self.assertNotIn("diagnostic", cir)
        self.assertEqual(cir["empty_room_accepted_candidates"], 1)

    def test_diagnostic_and_accepted_denominators_stay_separate(self):
        refs = {r["session_id"]: r for r in ev.build_reference_rows(
            [ctx("a", "1", 100.0), ctx("b", "1", 100.0)], "36 30")}
        rows = ev.evaluate_rows([pred("a", "phase", "accepted", "16.2", "16.2", res="0.4999"),
                                 pred("b", "phase", "withheld", "17.5", "")], refs)
        entry = next(e for e in ev.summarize(rows) if e["group"] == "100cm" and e["method"] == "phase")
        self.assertEqual(entry["diagnostic"]["n"], 2)
        self.assertEqual(entry["accepted"]["n"], 1)
        self.assertEqual(entry["accepted"]["within_1_1"], 0)   # |16.2-18| = 1.8
        self.assertEqual(entry["accepted"]["within_2_2"], 1)
        self.assertEqual(entry["diagnostic"]["within_1_1"], 0)  # 1.8 and 2.5
        self.assertEqual(entry["diagnostic"]["within_resolution"], 0)


if __name__ == "__main__":
    unittest.main()
