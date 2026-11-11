import datetime as dt
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.analyze_candle_outcomes import AnalysisError, analyze_candles


UTC = dt.timezone.utc


def write_forecasts(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True) + "\n")


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


class CandleOutcomeAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.memory = self.root / "scanner-memory"
        self.observations = self.root / "observations"
        self.output = self.root / "research-analysis"
        self.snapshot = dt.datetime(2026, 11, 1, 0, 25, tzinfo=UTC)
        self.feature = {
            "chain": "solana",
            "market_type": "dex_pool",
            "contract_address": "Token-AbC",
            "pool_address": "Pool-XyZ",
            "quote_symbol": "SOL",
            "price_usd": 1.0,
            "retrieved_utc": iso(self.snapshot - dt.timedelta(seconds=5)),
        }
        self.forecast = {
            "schema": "crypto-prebreakout.forecast-ledger.v1",
            "forecast_id": "forecast-1",
            "identity": "dex:solana:token-abc",
            "snapshot_utc": iso(self.snapshot),
            "feature_snapshot": self.feature,
        }
        write_forecasts(self.memory / "forecasts" / "2026-11-01.jsonl.gz", [self.forecast])

    def tearDown(self):
        self.temp.cleanup()

    def expected_bars(self):
        end = self.snapshot + dt.timedelta(hours=24)
        opened = self.snapshot.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
        bars = []
        while opened + dt.timedelta(hours=1) <= end:
            high, low, close = 1.1, 0.9, 1.0
            if opened == bars_start(self.snapshot) + dt.timedelta(hours=5):
                high, low, close = 2.1, 0.7, 1.2
            elif opened == bars_start(self.snapshot) + dt.timedelta(hours=10):
                high, low, close = 1.5, 0.85, 1.1
            bars.append({
                "time_open": iso(opened),
                "time_close": iso(opened + dt.timedelta(hours=1)),
                "high": high, "low": low, "close": close,
            })
            opened += dt.timedelta(hours=1)
        return bars

    def observation(self, record_id="observation-1", bars=None, **changes):
        record = {
            "schema": "crypto-prebreakout.market-observation.v1",
            "record_id": record_id,
            "record_type": "watchlist_market_observation",
            "collection_run_id": "collection-1",
            "collected_utc": "2026-11-02T00:30:00Z",
            "source_received_utc": "2026-11-02T00:30:00Z",
            "source_status": "OBSERVED",
            "source": "GeckoTerminal exact-token USD 1h OHLCV",
            "source_url": "https://provider.example/pools/pool-xyz/ohlcv",
            "identity_fields": {
                "identity": "dex:solana:token-abc",
                "chain": "solana",
                "contract_address": "token-abc",
                "pool_address": "pool-xyz",
                "quote_symbol": "SOL",
                "pair_orientation": None,
                "quote_asset": None,
            },
            "source_evidence": {"resolution": "1h", "bars": bars or self.expected_bars()},
        }
        record.update(changes)
        return record

    def write_observations(self, records):
        run_dir = self.observations / "collection-test"
        path = run_dir / "observations.jsonl"
        run_dir.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in records),
                        encoding="utf-8")
        (run_dir / "manifest.json").write_text(json.dumps({
            "status": "COMPLETED",
            "collection_run_id": "collection-1",
            "record_count": len(records),
            "output_file": "observations.jsonl",
        }), encoding="utf-8")
        return path

    def run_analysis(self):
        return analyze_candles(self.memory, self.observations, self.output)

    @staticmethod
    def report(output):
        return json.loads((output / "report.json").read_text(encoding="utf-8"))

    def test_complete_exact_identity_hourly_window_deduplicates_polling(self):
        bars = self.expected_bars()
        # A repeated poll returns every same candle; the repeated timestamp is one bar.
        self.write_observations([
            self.observation(bars=bars),
            self.observation("observation-2", bars=bars),
        ])
        output = self.run_analysis()
        report = self.report(output)
        metrics = report["candle_level_metrics"]
        self.assertEqual(metrics["complete_coverage_denominator"], 1)
        self.assertEqual(metrics["target_results"]["+40%"]["candle_level_presence_count"], 1)
        self.assertEqual(metrics["target_results"]["+60%"]["candle_level_presence_count"], 1)
        self.assertEqual(metrics["target_results"]["+100%"]["candle_level_presence_count"], 1)
        self.assertEqual(
            metrics["target_results"]["+40%"]["same_bar_target_and_below_base_count"], 2
        )
        self.assertEqual(
            report["coverage_summary"]["duplicate_bar_records"], len(bars)
        )
        path = metrics["forecast_paths"][0]
        self.assertTrue(path["coverage_complete"])
        self.assertEqual(path["exact_join_identity"], {
            "chain": "solana", "token": "token-abc", "pool": "pool-xyz",
        })
        self.assertEqual(path["incomplete_reasons"], [])
        self.assertEqual(path["expected_full_hour_bars_in_24h_window"], len(bars))
        self.assertEqual(path["duplicate_bar_timestamps_excluded"], len(bars))
        self.assertAlmostEqual(path["hourly_adverse_movement"]["worst_bar_low_return_pct"], -30.0)
        self.assertTrue(path["targets"]["+40%"]["within_candle_order_unknown"])

    def test_unfinalized_collection_without_manifest_is_excluded(self):
        self.write_observations([self.observation()])
        partial_dir = self.observations / "collection-partial"
        partial_dir.mkdir()
        (partial_dir / "observations.jsonl").write_text(
            json.dumps(self.observation("partial-observation")) + "\n", encoding="utf-8"
        )

        output = self.run_analysis()
        report = self.report(output)

        self.assertEqual(report["record_counts"]["observations_loaded"], 1)
        self.assertEqual(report["record_counts"]["forecasts_joined_to_observations"], 1)
        source_paths = [item["path"] for item in report["source_files"]]
        self.assertFalse(any("collection-partial" in path for path in source_paths))

    def test_only_manifest_declared_records_with_matching_run_identity_are_loaded(self):
        first = self.observation()
        wrong_run = self.observation("wrong-run")
        wrong_run["collection_run_id"] = "different-run"
        beyond_manifest = self.observation("beyond-manifest")
        self.write_observations([first, wrong_run, beyond_manifest])
        manifest_path = self.observations / "collection-test" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["record_count"] = 2
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        report = self.report(self.run_analysis())

        self.assertEqual(report["record_counts"]["observation_records"], 3)
        self.assertEqual(report["record_counts"]["observations_loaded"], 1)
        self.assertEqual(report["coverage_summary"]["observation_collection_run_id_mismatch"], 1)
        self.assertEqual(report["coverage_summary"]["observation_records_beyond_manifest_count"], 1)

    def test_missing_manifest_declared_observation_file_is_reported(self):
        run_dir = self.observations / "collection-missing"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(json.dumps({
            "status": "INTERRUPTED",
            "collection_run_id": "collection-missing",
            "record_count": 2,
            "output_file": "observations.jsonl",
        }), encoding="utf-8")

        report = self.report(self.run_analysis())

        self.assertEqual(report["record_counts"]["observation_files_missing"], 1)
        self.assertEqual(report["record_counts"]["observation_records_declared"], 2)
        self.assertEqual(report["coverage_summary"]["observation_records_missing_from_manifest_count"], 2)
        self.assertEqual(report["record_counts"]["observations_loaded"], 0)

    def test_detection_and_horizon_boundary_bars_are_excluded(self):
        bars = self.expected_bars()
        overlap = {
            "time_open": iso(self.snapshot.replace(minute=0, second=0, microsecond=0)),
            "time_close": iso(self.snapshot.replace(minute=0, second=0, microsecond=0)
                              + dt.timedelta(hours=1)),
            "high": 100.0, "low": 0.01, "close": 1.0,
        }
        beyond = {
            "time_open": iso(self.snapshot.replace(minute=0, second=0, microsecond=0)
                              + dt.timedelta(hours=24)),
            "time_close": iso(self.snapshot.replace(minute=0, second=0, microsecond=0)
                              + dt.timedelta(hours=25)),
            "high": 100.0, "low": 0.01, "close": 1.0,
        }
        incomplete = {
            "time_open": iso(self.snapshot.replace(minute=0, second=0, microsecond=0)
                              + dt.timedelta(hours=25)),
            "time_close": iso(self.snapshot.replace(minute=0, second=0, microsecond=0)
                              + dt.timedelta(hours=26)),
            "high": 100.0, "low": 0.01, "close": 1.0,
        }
        self.write_observations([self.observation(
            bars=[overlap, *bars, beyond, incomplete], source_received_utc="2026-11-02T01:30:00Z",
            collected_utc="2026-11-02T01:30:00Z",
        )])
        report = self.report(self.run_analysis())
        self.assertEqual(report["coverage_summary"]["detection_overlapping_or_prior_bars_excluded"], 1)
        self.assertEqual(report["coverage_summary"]["bars_outside_forecast_window_excluded"], 1)
        self.assertEqual(report["coverage_summary"]["bar_excluded:bar_not_closed_when_observed"], 1)
        self.assertEqual(report["candle_level_metrics"]["target_results"]["+100%"]["candle_level_presence_count"], 1)

    def test_missing_hour_censors_full_window_target_labels(self):
        bars = self.expected_bars()
        bars.pop(4)
        self.write_observations([self.observation(bars=bars)])
        report = self.report(self.run_analysis())
        metrics = report["candle_level_metrics"]
        path = metrics["forecast_paths"][0]
        self.assertFalse(path["coverage_complete"])
        self.assertEqual(path["missing_elapsed_hours"], 1)
        self.assertEqual(metrics["complete_coverage_denominator"], 0)
        self.assertIsNone(metrics["target_results"]["+40%"]["candle_level_presence_rate"])
        self.assertEqual(report["incomplete_forecast_reasons"], {"missing_elapsed_hours": 1})

    def test_future_hours_are_censored_until_the_window_elapses(self):
        bars = self.expected_bars()[:5]
        self.write_observations([self.observation(
            bars=bars, source_received_utc="2026-11-01T06:00:00Z",
            collected_utc="2026-11-01T06:00:00Z",
        )])
        report = self.report(self.run_analysis())
        path = report["candle_level_metrics"]["forecast_paths"][0]
        self.assertFalse(path["coverage_complete"])
        self.assertEqual(path["elapsed_full_hour_bars_at_analysis_as_of"], 5)
        self.assertEqual(path["future_hours_censored"], 18)
        self.assertEqual(path["incomplete_reasons"], ["future_hours_censored"])
        self.assertIsNone(path["targets"]["+40%"]["candle_level_presence"])

    def test_exact_identity_and_price_units_are_required(self):
        wrong_pool = self.observation()
        wrong_pool["identity_fields"]["pool_address"] = "other-pool"
        wrong_chain = self.observation("observation-2")
        wrong_chain["identity_fields"]["chain"] = "ethereum"
        wrong_unit = self.observation("observation-3")
        wrong_unit["source"] = "existing CEX venue 1h OHLCV adapter"
        self.write_observations([wrong_pool, wrong_chain, wrong_unit])
        report = self.report(self.run_analysis())
        self.assertEqual(report["unjoinable_observation_record_count"], 3)
        self.assertEqual(
            report["unjoinable_observation_reasons"]["unjoinable_observations_no_exact_forecast_identity"],
            2,
        )
        self.assertEqual(
            report["unjoinable_observation_reasons"]["unjoinable_observations_unsupported_price_unit"],
            1,
        )
        self.assertEqual(
            report["excluded_forecast_reasons"]["unsupported_observation_price_source"], 1
        )
        self.assertIn(
            "no_valid_joined_hourly_observations",
            report["excluded_forecast_details"][0]["reasons"],
        )
        self.assertEqual(report["candle_level_metrics"]["forecast_paths"], [])

    def test_optional_quote_conflict_is_rejected(self):
        observation = self.observation()
        observation["identity_fields"]["quote_symbol"] = "USDC"
        self.write_observations([observation])
        report = self.report(self.run_analysis())
        self.assertEqual(report["unjoinable_observation_record_count"], 1)
        self.assertEqual(report["coverage_summary"]["identity_conflict:quote_symbol"], 1)
        self.assertEqual(
            report["excluded_forecast_reasons"]["optional_identity_conflict:quote_symbol"], 1
        )

    def test_malformed_and_duplicate_conflicting_bars_are_reported(self):
        bars = self.expected_bars()
        conflict = dict(bars[0], high=10.0)
        malformed = dict(bars[1], low=5.0)
        self.write_observations([
            self.observation(bars=[*bars, conflict, malformed]),
        ])
        report = self.report(self.run_analysis())
        path = report["candle_level_metrics"]["forecast_paths"][0]
        self.assertEqual(path["duplicate_bar_timestamps_excluded"], 1)
        self.assertEqual(path["missing_elapsed_hours"], 1)
        self.assertEqual(report["coverage_summary"]["conflicting_duplicate_bar_timestamps"], 1)
        self.assertEqual(report["coverage_summary"]["bar_excluded:invalid_ohlc_relationship"], 1)

    def test_report_is_reproducible_preserves_sources_and_legacy_artifact(self):
        self.write_observations([self.observation()])
        legacy = self.output / "outcomes-v1-1-old-phase3-report"
        legacy.mkdir(parents=True)
        marker = legacy / "report.md"
        marker.write_text("legacy artifact", encoding="utf-8")
        before_forecast = hashlib.sha256(
            (self.memory / "forecasts" / "2026-11-01.jsonl.gz").read_bytes()
        ).hexdigest()
        before_observations = hashlib.sha256(
            (self.observations / "collection-test" / "observations.jsonl").read_bytes()
        ).hexdigest()
        first = self.run_analysis()
        files_before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in first.iterdir() if path.is_file()}
        second = self.run_analysis()
        self.assertEqual(first, second)
        self.assertEqual(files_before, {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                        for path in second.iterdir() if path.is_file()})
        self.assertEqual(marker.read_text(encoding="utf-8"), "legacy artifact")
        self.assertEqual(before_forecast, hashlib.sha256(
            (self.memory / "forecasts" / "2026-11-01.jsonl.gz").read_bytes()
        ).hexdigest())
        self.assertEqual(before_observations, hashlib.sha256(
            (self.observations / "collection-test" / "observations.jsonl").read_bytes()
        ).hexdigest())
        manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["analysis_version"], "rocket-hunter-candle-outcomes.v4")

    def test_analysis_cannot_write_into_observation_source(self):
        self.write_observations([self.observation()])
        with self.assertRaisesRegex(AnalysisError, "separate"):
            analyze_candles(self.memory, self.observations, self.observations / "report")

    def test_empty_observation_history_produces_coverage_report(self):
        output = self.run_analysis()
        report = self.report(output)
        self.assertIsNone(report["analysis_as_of_utc"])
        self.assertEqual(report["record_counts"]["observation_records"], 0)
        self.assertEqual(report["record_counts"]["forecasts_joined_to_observations"], 0)
        self.assertEqual(report["candle_level_metrics"]["forecast_paths"], [])
        self.assertEqual(
            report["excluded_forecast_reasons"]["no_valid_joined_hourly_observations"], 1
        )

    def test_unobserved_records_set_analysis_as_of_without_joining(self):
        record = self.observation(source_status="PROVIDER_RATE_LIMITED", bars=[])
        self.write_observations([record])
        report = self.report(self.run_analysis())
        self.assertEqual(report["analysis_as_of_utc"], "2026-11-02T00:30:00Z")
        self.assertEqual(report["record_counts"]["observation_records"], 1)
        self.assertEqual(report["record_counts"]["observations_loaded"], 0)
        self.assertEqual(report["record_counts"]["forecasts_joined_to_observations"], 0)
        self.assertEqual(report["coverage_summary"]["observation_status:PROVIDER_RATE_LIMITED"], 1)


def bars_start(snapshot):
    return snapshot.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)


if __name__ == "__main__":
    unittest.main()
