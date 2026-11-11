import gzip
import json
import tempfile
import unittest
import datetime as dt
from pathlib import Path

from scripts import analyze_v425_shadow as shadow


class V425ShadowAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.memory = self.root / "memory"
        (self.memory / "forecasts").mkdir(parents=True)
        (self.memory / "outcomes").mkdir()
        self.output = self.root / "analysis"
        self.forecasts = [
            self.forecast("train-low", "2026-11-05T10:00:00Z", 20),
            self.forecast("train-high", "2026-11-05T11:00:00Z", 80),
            self.forecast("holdout-low", "2026-11-06T01:00:00Z", 30),
            self.forecast("holdout-high", "2026-11-06T02:00:00Z", 90),
            self.forecast("holdout-no-score", "2026-11-06T03:00:00Z", None),
        ]
        self.outcomes = [
            self.outcome(self.forecasts[0], -10),
            self.outcome(self.forecasts[1], 10),
            self.outcome(self.forecasts[2], -20),
            self.outcome(self.forecasts[3], 20),
            self.outcome(self.forecasts[4], -5),
        ]
        self._write(self.memory / "forecasts" / "2026-11-05.jsonl.gz", self.forecasts)
        self._write(self.memory / "outcomes" / "2026-11-09.jsonl.gz", self.outcomes)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _write(path, records):
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")

    @staticmethod
    def forecast(forecast_id, snapshot_utc, score):
        signal = {
            "signal_version": shadow.SIGNAL_VERSION,
            "model_id": shadow.MODEL_ID,
            "score": score,
            "components": {"1h_momentum": 5.0},
            "source": "fixture",
            "source_url": "https://provider.invalid/fixture",
            "retrieved_utc": snapshot_utc,
        }
        return {
            "forecast_id": forecast_id,
            "snapshot_utc": snapshot_utc,
            "identity": "dex:bsc:token:pool",
            "market_key": "dex:bsc:token:pool",
            "feature_snapshot": {
                "market_type": "dex_pool",
                "chain": "bsc",
                "contract_address": "0xtoken",
                "pool_address": "0xpool",
                "v425_early_breakout": signal,
            },
        }

    @staticmethod
    def outcome(forecast, mark_return):
        observed = (
            dt.datetime.fromisoformat(forecast["snapshot_utc"].replace("Z", "+00:00"))
            + dt.timedelta(hours=24, minutes=30)
        ).isoformat().replace("+00:00", "Z")
        return {
            "forecast_id": forecast["forecast_id"],
            "forecast_utc": forecast["snapshot_utc"],
            "market_key": forecast["market_key"],
            "horizon_hours": 24,
            "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL",
            "observed_utc": observed,
            "base_price_usd": 100,
            "observed_price_usd": 100 * (1 + mark_return / 100),
            "mark_return_pct": round(mark_return, 6),
        }

    def test_requires_explicit_time_split_and_uses_training_score_median(self):
        with self.assertRaisesRegex(ValueError, "explicit split_cutoff_utc"):
            shadow.analyze_ledgers(self.memory)

        report = shadow.analyze_ledgers(self.memory, "2026-11-06T00:00:00Z")
        self.assertEqual(report["scope"]["training_score_median"], 50)
        self.assertEqual(report["time_split"]["training"]["forecast_count"], 2)
        self.assertEqual(report["time_split"]["holdout"]["forecast_count"], 3)
        self.assertEqual(report["time_split"]["holdout"]["score_missing_or_unusable_count"], 1)
        groups = report["endpoint_return_distributions_by_score_group"]["holdout"]
        self.assertEqual(groups["score_at_or_below_training_median"]["median"], -20)
        self.assertEqual(groups["score_above_training_median"]["median"], 20)
        self.assertIn("exploratory prospective endpoint-mark associations", report["interpretation"].lower())

    def test_rejects_non_unique_or_mismatched_outcome_joins(self):
        valid_training = self.forecast("train-valid", "2026-11-05T12:00:00Z", 50)
        self.forecasts.append(valid_training)
        self.outcomes[0]["market_key"] = "dex:bsc:other:pool"
        self.outcomes.append(dict(self.outcomes[1]))
        self.outcomes.append(self.outcome(valid_training, 5))
        self._write(self.memory / "forecasts" / "2026-11-05.jsonl.gz", self.forecasts)
        self._write(self.memory / "outcomes" / "2026-11-09.jsonl.gz", self.outcomes)

        report = shadow.analyze_ledgers(self.memory, "2026-11-06T00:00:00Z")
        states = report["time_split"]["training"]["outcome_state_counts"]
        self.assertEqual(states["MARKET_KEY_MISMATCH"], 1)
        self.assertEqual(states["DUPLICATE_OUTCOME_ROWS"], 1)
        self.assertEqual(report["time_split"]["training"]["eligible_endpoint_marks_with_score"], 1)

    def test_excludes_duplicate_ids_even_when_duplicate_row_has_no_v425_snapshot(self):
        baseline_duplicate = dict(self.forecasts[0])
        baseline_duplicate["feature_snapshot"] = {"market_type": "dex_pool"}
        self.forecasts.append(baseline_duplicate)
        self._write(self.memory / "forecasts" / "2026-11-05.jsonl.gz", self.forecasts)

        report = shadow.analyze_ledgers(self.memory, "2026-11-06T00:00:00Z")

        self.assertEqual(report["record_counts"]["duplicate_forecast_ids"], 1)
        self.assertEqual(report["record_counts"]["excluded_duplicate_forecast_ids"], 1)
        self.assertEqual(report["time_split"]["training"]["forecast_count"], 1)
        self.assertEqual(report["time_split"]["training"]["eligible_endpoint_marks_with_score"], 1)

    def test_excludes_stale_score_provenance_from_training_threshold(self):
        self.forecasts[0]["feature_snapshot"]["v425_early_breakout"]["retrieved_utc"] = (
            "2026-11-05T09:29:00Z"
        )
        self._write(self.memory / "forecasts" / "2026-11-05.jsonl.gz", self.forecasts)

        report = shadow.analyze_ledgers(self.memory, "2026-11-06T00:00:00Z")
        training = report["time_split"]["training"]
        self.assertEqual(training["score_present_count"], 1)
        self.assertEqual(training["score_missing_or_unusable_count"], 1)
        self.assertEqual(
            report["record_counts"]["signal:stale_or_future_signal_timestamp"], 1
        )

    def test_excludes_score_missing_source_label_and_reports_run_provenance(self):
        self.forecasts[0]["feature_snapshot"]["v425_early_breakout"]["source"] = None
        self._write(self.memory / "forecasts" / "2026-11-05.jsonl.gz", self.forecasts)

        report = shadow.analyze_ledgers(self.memory, "2026-11-06T00:00:00Z")

        self.assertEqual(report["record_counts"]["signal:missing_source_label"], 1)
        self.assertEqual(report["forecast_provenance"]["missing_scanner_version_count"], 5)
        self.assertEqual(
            report["forecast_provenance"]["missing_configuration_fingerprint_count"], 5
        )

    def test_accepts_exact_dex_identity_with_token_mint(self):
        features = self.forecasts[0]["feature_snapshot"]
        features["chain"] = "solana"
        features["token_mint"] = features.pop("contract_address")
        self._write(self.memory / "forecasts" / "2026-11-05.jsonl.gz", self.forecasts)

        report = shadow.analyze_ledgers(self.memory, "2026-11-06T00:00:00Z")

        self.assertEqual(report["time_split"]["training"]["forecast_count"], 2)

    def test_refuses_analysis_until_both_time_splits_have_matched_prospective_outcomes(self):
        training_outcomes = [
            outcome for outcome in self.outcomes
            if outcome["forecast_id"].startswith("train-")
        ]
        self._write(self.memory / "outcomes" / "2026-11-09.jsonl.gz", training_outcomes)

        with self.assertRaisesRegex(
            shadow.common.AnalysisError,
            "no prospective v4.25 score has a unique eligible 24-hour outcome in the holdout split",
        ):
            shadow.analyze_ledgers(self.memory, "2026-11-06T00:00:00Z")

    def test_versioned_output_is_idempotent_and_does_not_change_memory(self):
        before = {
            path.relative_to(self.memory): path.read_bytes()
            for path in self.memory.rglob("*") if path.is_file()
        }
        first = shadow.write_report(
            self.memory, self.output, "2026-11-06T00:00:00Z"
        )
        first_bytes = (first / "report.json").read_bytes()
        second = shadow.write_report(
            self.memory, self.output, "2026-11-06T00:00:00Z"
        )
        self.assertEqual(first, second)
        self.assertEqual(first_bytes, (second / "report.json").read_bytes())
        manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["analysis_version"], shadow.ANALYSIS_VERSION)
        self.assertEqual(
            before,
            {
                path.relative_to(self.memory): path.read_bytes()
                for path in self.memory.rglob("*") if path.is_file()
            },
        )

    def test_refuses_to_reuse_tampered_content_addressed_output(self):
        output = shadow.write_report(
            self.memory, self.output, "2026-11-06T00:00:00Z"
        )
        (output / "report.json").write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(shadow.common.AnalysisError, "checksum validation"):
            shadow.write_report(
                self.memory, self.output, "2026-11-06T00:00:00Z"
            )


if __name__ == "__main__":
    unittest.main()
