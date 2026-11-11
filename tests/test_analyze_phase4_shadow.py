import gzip
import json
import tempfile
import unittest
import datetime as dt
from pathlib import Path
from unittest.mock import patch

from scripts import analyze_phase4_shadow as shadow


class Phase4ShadowAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.memory = self.root / "memory"
        self.forecast_dir = self.memory / "forecasts"
        self.outcome_dir = self.memory / "outcomes"
        self.forecast_dir.mkdir(parents=True)
        self.outcome_dir.mkdir(parents=True)
        self.output = self.root / "analysis"
        self.forecasts = [
            self.forecast("train-low", "2026-11-05T10:00:00Z", -2.0),
            self.forecast("train-high", "2026-11-05T11:00:00Z", 2.0),
            self.forecast("holdout-low", "2026-11-06T01:00:00Z", -3.0),
            self.forecast("holdout-high", "2026-11-06T02:00:00Z", 3.0),
            self.forecast("holdout-no-signal", "2026-11-06T03:00:00Z", None),
        ]
        self.outcomes = [
            self.outcome(self.forecasts[0], -10.0),
            self.outcome(self.forecasts[1], 10.0),
            self.outcome(self.forecasts[2], -20.0),
            self.outcome(self.forecasts[3], 20.0),
            self.outcome(self.forecasts[4], -5.0),
        ]
        self.write_jsonl_gz(self.forecast_dir / "2026-11-05.jsonl.gz", self.forecasts)
        self.write_jsonl_gz(self.outcome_dir / "2026-11-09.jsonl.gz", self.outcomes)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def write_jsonl_gz(path, records):
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")

    @staticmethod
    def forecast(forecast_id, snapshot_utc, signal):
        feature_snapshot = {
            "source": "dexscreener_search",
            "source_url": "https://provider.invalid/fixture",
            "retrieved_utc": snapshot_utc,
            "market_type": "dex_pool",
            "chain": "bsc",
            "contract_address": "0xtoken",
            "pool_address": "0xpool",
        }
        if signal is not None:
            feature_snapshot["change_1h_pct"] = signal
        return {
            "forecast_id": forecast_id,
            "snapshot_utc": snapshot_utc,
            "identity": "dex:bsc:token:pool",
            "market_key": "dex:bsc:token:pool",
            "model_id": "v4.19.0-eb-beta-binomial",
            "feature_snapshot": feature_snapshot,
        }

    @staticmethod
    def outcome(forecast, mark_return):
        return {
            "forecast_id": forecast["forecast_id"],
            "forecast_utc": forecast["snapshot_utc"],
            "market_key": forecast["market_key"],
            "horizon_hours": 24,
            "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL",
            "observed_utc": (
                dt.datetime.fromisoformat(
                    forecast["snapshot_utc"].replace("Z", "+00:00")
                ) + dt.timedelta(hours=24, minutes=30)
            ).isoformat().replace("+00:00", "Z"),
            "base_price_usd": 100.0,
            "observed_price_usd": 100.0 * (1.0 + mark_return / 100.0),
            "mark_return_pct": round(mark_return, 6),
        }

    def test_analysis_uses_training_threshold_and_reports_holdout_mark_groups(self):
        report = shadow.analyze_ledgers(self.memory)

        self.assertEqual(report["scope"]["threshold"]["training_feature_median_pct"], 0.0)
        self.assertTrue(report["scope"]["threshold"]["computed_from_training_feature_values_without_outcome_labels"])
        self.assertEqual(report["time_split"]["training"]["forecast_count"], 2)
        self.assertEqual(report["time_split"]["holdout"]["forecast_count"], 3)
        self.assertEqual(report["time_split"]["holdout"]["signal_missing_or_unusable_count"], 1)
        self.assertEqual(report["time_split"]["holdout"]["eligible_endpoint_marks_without_signal"], 1)
        groups = report["endpoint_return_distributions_by_signal_group"]["holdout"]
        self.assertEqual(groups["signal_at_or_below_training_median"]["median"], -20.0)
        self.assertEqual(groups["signal_above_training_median"]["median"], 20.0)
        self.assertIn("exploratory endpoint-mark associations only", report["interpretation"].lower())

    def test_artifacts_are_versioned_idempotent_and_leave_memory_unchanged(self):
        before = {
            path.relative_to(self.memory): path.read_bytes()
            for path in self.memory.rglob("*") if path.is_file()
        }

        first = shadow.write_report(self.memory, self.output)
        second = shadow.write_report(self.memory, self.output)

        self.assertEqual(first, second)
        self.assertTrue(first.name.startswith("phase4-shadow-v1-"))
        self.assertTrue((first / "manifest.json").is_file())
        self.assertEqual(
            before,
            {
                path.relative_to(self.memory): path.read_bytes()
                for path in self.memory.rglob("*") if path.is_file()
            },
        )

    def test_rejects_mismatched_outcome_market_identity(self):
        self.outcomes[0]["market_key"] = "dex:bsc:other:pool"
        self.write_jsonl_gz(self.outcome_dir / "2026-11-09.jsonl.gz", self.outcomes)

        report = shadow.analyze_ledgers(self.memory)

        self.assertEqual(
            report["time_split"]["training"]["outcome_state_counts"]["MARKET_KEY_MISMATCH"],
            1,
        )
        self.assertEqual(
            report["time_split"]["training"]["eligible_endpoint_marks_with_signal"],
            1,
        )

    def test_accepts_exact_dex_identity_with_token_mint(self):
        features = self.forecasts[0]["feature_snapshot"]
        features["chain"] = "solana"
        features["token_mint"] = features.pop("contract_address")
        self.write_jsonl_gz(self.forecast_dir / "2026-11-05.jsonl.gz", self.forecasts)

        report = shadow.analyze_ledgers(self.memory)

        self.assertEqual(report["time_split"]["training"]["forecast_count"], 2)

    def test_refuses_to_publish_if_source_snapshot_changes_before_analysis(self):
        with patch.object(
            shadow,
            "analyze_ledgers",
            return_value={"source_snapshot_id": "changed"},
        ):
            with self.assertRaisesRegex(shadow.AnalysisError, "changed between snapshot and analysis"):
                shadow.write_report(self.memory, self.output)

        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
