import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.analyze_research_outcomes import AnalysisError, _source_snapshot, analyze_ledgers


def write_partition(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True) + "\n")


def file_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


class ResearchOutcomeAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.memory = self.root / "scanner-memory"
        self.output_root = self.root / "research-analysis"
        self.forecasts = []
        self.outcomes = []
        cases = [
            ("hit-40", 50.0, 2.0),
            ("hit-60", 60.0, 6.0),
            ("hit-100-late", 101.0, 6.01),
            ("early", 80.0, -0.01),
            ("negative", -15.0, 1.0),
            ("unavailable", None, 1.0),
            ("missing", None, None),
        ]
        for forecast_id, mark, lag in cases:
            self.forecasts.append({
                "schema": "crypto-prebreakout.forecast-ledger.v1",
                "forecast_id": forecast_id,
                "snapshot_utc": "2026-11-05T00:00:00Z",
                "feature_snapshot": {"price_usd": 1.0, "captured_at_detection": True},
                "raw_score": 70.0,
                "pipeline_version": "4.18.1",
            })
            if lag is None:
                continue
            observed_hour = 1.0 + lag
            observed_minute = int(round((observed_hour % 1) * 60))
            self.outcomes.append({
                "schema": "crypto-prebreakout.outcome-ledger.v1",
                "forecast_id": forecast_id,
                "horizon_hours": 1,
                "forecast_utc": "2026-11-05T00:00:00Z",
                "observed_utc": f"2026-11-05T{int(observed_hour):02d}:{observed_minute:02d}:00Z",
                "status": ("OBSERVED_MARK_NOT_EXECUTABLE_FILL" if mark is not None
                           else "UNAVAILABLE_NO_VALID_PRICE_MARK"),
                "mark_return_pct": mark,
                "feature_snapshot": {"must_not_be_used_as_forecast_feature": True},
            })
        write_partition(self.memory / "forecasts" / "2026-11-05.jsonl.gz", self.forecasts)
        write_partition(self.memory / "outcomes" / "2026-11-05.jsonl.gz", self.outcomes)

    def tearDown(self):
        self.temp.cleanup()

    def test_bounded_lag_endpoint_marks_and_censored_coverage(self):
        output = analyze_ledgers(self.memory, self.output_root)
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        metrics = report["horizon_metrics"]["1"]
        self.assertEqual(report["forecast_detection_count"], 7)
        self.assertEqual(metrics["forecast_detection_count"], 7)
        self.assertEqual(metrics["outcome_record_count"], 6)
        self.assertEqual(metrics["missing_outcome_record_count"], 1)
        self.assertEqual(metrics["unavailable_outcome_count"], 1)
        self.assertEqual(metrics["unlabeled_or_unavailable_forecast_count"], 2)
        self.assertEqual(metrics["outcome_status_counts"]["UNAVAILABLE_NO_VALID_PRICE_MARK"], 1)
        self.assertEqual(metrics["observed_mark_count"], 5)
        self.assertEqual(metrics["observed_marks_before_horizon"], 1)
        self.assertEqual(metrics["observed_marks_over_lag_limit"], 1)
        self.assertEqual(metrics["bounded_lag_mark_count"], 3)
        self.assertEqual(metrics["targets"]["+40%"]["observed_mark_attainments"], 2)
        self.assertEqual(metrics["targets"]["+60%"]["observed_mark_attainments"], 1)
        self.assertEqual(metrics["targets"]["+100%"]["observed_mark_attainments"], 0)
        self.assertEqual(metrics["adverse_endpoint_marks"]["below_base_mark_count"], 1)
        self.assertEqual(metrics["adverse_endpoint_marks"]["worst_observed_mark_return_pct"], -15.0)
        self.assertIn("time_to_target", report["unsupported_metrics"])
        self.assertEqual(report["feature_snapshot_count"], 7)

    def test_later_outcome_fields_do_not_replace_frozen_forecast_features(self):
        output = analyze_ledgers(self.memory, self.output_root)
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["feature_snapshot_count"], 7)
        self.assertNotIn("must_not_be_used_as_forecast_feature", json.dumps(report))
        self.assertIn("later outcomes are labels only", report["methodology"]["feature_policy"])

    def test_report_is_reproducible_and_source_ledgers_are_unchanged(self):
        before = file_hashes(self.memory)
        first = analyze_ledgers(self.memory, self.output_root)
        first_files = file_hashes(first)
        second = analyze_ledgers(self.memory, self.output_root)
        self.assertEqual(first, second)
        self.assertEqual(first_files, file_hashes(second))
        self.assertEqual(before, file_hashes(self.memory))
        manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_access"], "read-only")
        self.assertEqual(set(manifest["output_files_sha256"]), {"report.json", "report.md"})

    def test_existing_unmanaged_output_is_never_overwritten(self):
        _, _, _, snapshot_id = _source_snapshot(self.memory)
        self.output_root.mkdir(parents=True)
        unmanaged = self.output_root / f"outcomes-v1-1-{snapshot_id[:24]}"
        unmanaged.mkdir(parents=True)
        marker = unmanaged / "keep.txt"
        marker.write_text("unchanged", encoding="utf-8")
        with self.assertRaisesRegex(AnalysisError, "unmanaged research output|different research output"):
            analyze_ledgers(self.memory, self.output_root)
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")


if __name__ == "__main__":
    unittest.main()
