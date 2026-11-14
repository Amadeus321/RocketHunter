import gzip
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from scripts.v418_pipeline import (
    age_classify, asset_age_hours, collect_rows, fingerprint_clusters, grade_outcomes,
    make_forecast_record, market_key, legacy_pipe_market_key, process_snapshot, read_jsonl_gz,
)
from scripts import v425_early_breakout


class V418QualityTests(unittest.TestCase):
    def test_forecast_captures_versioned_early_breakout_signal_and_provenance(self):
        row = {
            "identity": "dex:base:token:pool",
            "early_breakout_signal_version": v425_early_breakout.EARLY_BREAKOUT_SIGNAL_VERSION,
            "early_breakout_model_id": v425_early_breakout.EARLY_BREAKOUT_MODEL_ID,
            "early_breakout_score": 72.5,
            "early_breakout_tier": "ACCELERATING_WATCHLIST",
            "early_breakout_components": {
                "5m_impulse": 4.0,
                "1h_momentum": 8.0,
                "6h_trend": 3.0,
                "24h_context": 2.0,
                "momentum_acceleration": 5.0,
                "transaction_activity": 6.0,
                "volume_turnover": 7.0,
                "liquidity_depth": 8.0,
                "buy_pressure": 4.0,
                "intraday_volume_acceleration": 3.0,
                "pool_novelty": 6.0,
                "multi_window_alignment": 5.0,
            },
            "source": "dexpaprika_pool_momentum_1h",
            "source_url": "https://provider.invalid/pools/example",
            "retrieved_utc": "2026-11-09T12:00:00Z",
            "dexpaprika_provider_timestamp": "2026-11-09T11:59:30Z",
        }

        forecast = make_forecast_record(
            "dex|base|token|pool", row, {"universe"}, datetime(2026, 11, 9, 12, tzinfo=timezone.utc),
            "run-fixture", {"scanner_version": "4.28.0", "configuration_fingerprint": "cfg-fixture"},
        )
        signal = forecast["feature_snapshot"]["v425_early_breakout"]

        self.assertEqual(signal["signal_version"], "v4.25-early-breakout.v1")
        self.assertEqual(signal["model_id"], "v4.25-early-breakout")
        self.assertEqual(signal["score"], 72.5)
        self.assertEqual(signal["components"], row["early_breakout_components"])
        self.assertEqual(signal["source"], row["source"])
        self.assertEqual(signal["source_url"], row["source_url"])
        self.assertEqual(signal["retrieved_utc"], row["retrieved_utc"])
        self.assertEqual(signal["provider_timestamp"], row["dexpaprika_provider_timestamp"])
        self.assertEqual(forecast["run_id"], "run-fixture")
        self.assertEqual(forecast["configuration_fingerprint"], "cfg-fixture")
        self.assertEqual(forecast["scanner_version"], "4.28.0")

    def test_missing_early_breakout_signal_is_not_invented_and_missing_components_are_null(self):
        missing = make_forecast_record(
            "market-missing",
            {"early_breakout_score": 90.0, "early_breakout_components": {"1h_momentum": 8.0}},
            set(), datetime(2026, 11, 9, tzinfo=timezone.utc), "run-missing",
        )
        self.assertNotIn("v425_early_breakout", missing["feature_snapshot"])

        partial = make_forecast_record(
            "market-partial",
            {
                "early_breakout_signal_version": v425_early_breakout.EARLY_BREAKOUT_SIGNAL_VERSION,
                "early_breakout_model_id": v425_early_breakout.EARLY_BREAKOUT_MODEL_ID,
                "early_breakout_score": 42.0,
                "early_breakout_components": {"1h_momentum": 5.0},
            },
            set(), datetime(2026, 11, 9, tzinfo=timezone.utc), "run-partial",
        )
        signal = partial["feature_snapshot"]["v425_early_breakout"]
        self.assertEqual(signal["components"]["1h_momentum"], 5.0)
        self.assertIsNone(signal["components"]["5m_impulse"])
        self.assertIsNone(signal["components"]["buy_pressure"])
        self.assertIsNone(signal["source_url"])

    def test_malformed_early_breakout_values_are_stored_as_null_not_coerced(self):
        forecast = make_forecast_record(
            "market-malformed",
            {
                "early_breakout_signal_version": v425_early_breakout.EARLY_BREAKOUT_SIGNAL_VERSION,
                "early_breakout_model_id": v425_early_breakout.EARLY_BREAKOUT_MODEL_ID,
                "early_breakout_score": "72.5",
                "source_url": {"unexpected": "object"},
                "early_breakout_components": {
                    "5m_impulse": float("nan"),
                    "1h_momentum": True,
                    "6h_trend": -1.0,
                    "24h_context": 99.0,
                    "transaction_activity": 10 ** 1000,
                },
            },
            set(), datetime(2026, 11, 9, tzinfo=timezone.utc), "run-malformed",
        )
        signal = forecast["feature_snapshot"]["v425_early_breakout"]
        self.assertIsNone(signal["score"])
        self.assertIsNone(signal["components"]["5m_impulse"])
        self.assertIsNone(signal["components"]["1h_momentum"])
        self.assertIsNone(signal["components"]["6h_trend"])
        self.assertIsNone(signal["components"]["24h_context"])
        self.assertIsNone(signal["source_url"])
        self.assertIsNone(signal["components"]["transaction_activity"])

    def test_process_snapshot_persists_signal_with_available_run_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            memory = root / "memory"
            out.mkdir()
            main = {
                "created_utc": "2026-11-09T12:00:00Z",
                "dex_discovery_leads": [{
                    "identity": "dex:base:0xtoken:0xpool",
                    "market_type": "dex_pool",
                    "chain": "base",
                    "contract_address": "0xtoken",
                    "pool_address": "0xpool",
                    "source": "dexpaprika_pool_momentum_1h",
                    "retrieved_utc": "2026-11-09T11:59:30Z",
                    "early_breakout_signal_version": v425_early_breakout.EARLY_BREAKOUT_SIGNAL_VERSION,
                    "early_breakout_model_id": v425_early_breakout.EARLY_BREAKOUT_MODEL_ID,
                    "early_breakout_score": 61.25,
                    "early_breakout_components": {"1h_momentum": 5.0},
                }],
                "source_status": [],
            }
            (out / "rocket-hunt.json").write_text(json.dumps(main), encoding="utf-8")
            (out / "scan-status.json").write_text(json.dumps({
                "run_id": "run-persisted",
                "scanner_version": "4.28.0",
                "configuration_fingerprint": "cfg-persisted",
            }), encoding="utf-8")

            process_snapshot(out / "rocket-hunt.json", None, out, memory, "run-persisted")
            forecast = read_jsonl_gz(next((memory / "forecasts").glob("*.jsonl.gz")))[0]
            signal = forecast["feature_snapshot"]["v425_early_breakout"]

            self.assertEqual(signal["score"], 61.25)
            self.assertIsNone(signal["components"]["5m_impulse"])
            self.assertEqual(signal["components"]["1h_momentum"], 5.0)
            self.assertEqual(forecast["configuration_fingerprint"], "cfg-persisted")
            self.assertEqual(forecast["scanner_version"], "4.28.0")

    def test_under_24h_dex_mark_is_preserved_but_not_called_24h(self):
        row = {"market_type": "dex_pool", "age_hours": 3.5, "change_24h_pct": 1200.0, "risk_flags": []}
        status = age_classify(row, datetime(2026, 11, 5, tzinfo=timezone.utc))
        self.assertEqual(status, "INSUFFICIENT_24H_HISTORY")
        self.assertIsNone(row["change_24h_pct"])
        self.assertEqual(row["source_reported_change_24h_pct"], 1200.0)
        self.assertEqual(row["source_reported_change_during_available_pool_history_pct"], 1200.0)

    def test_unknown_dex_age_is_not_silently_comparable(self):
        row = {"market_type": "dex_pool", "change_24h_pct": 400, "risk_flags": []}
        status = age_classify(row, datetime(2026, 11, 5, tzinfo=timezone.utc))
        self.assertEqual(status, "UNKNOWN_POOL_AGE_NOT_COMPARABLE")
        self.assertIsNone(row["change_24h_pct"])
        self.assertEqual(row["source_reported_change_24h_pct"], 400)

    def test_24h_or_older_dex_is_comparable_source_reported(self):
        row = {"market_type": "dex_pool", "age_hours": 24.1, "change_24h_pct": 22.0}
        self.assertEqual(age_classify(row, datetime.now(timezone.utc)), "SOURCE_REPORTED_24H_POOL_AGE_GE_24H")
        self.assertEqual(row["change_24h_pct"], 22.0)

    def test_cex_24h_fields_are_not_pool_age_filtered(self):
        row = {"market_type": "cex_spot", "change_24h_pct": 10.0}
        self.assertEqual(age_classify(row, datetime.now(timezone.utc)), "EXCHANGE_OR_AGGREGATOR_24H_FIELD")
        self.assertEqual(row["change_24h_pct"], 10.0)

    def test_age_from_pool_created_at(self):
        row = {"market_type": "dex_pool", "pool_created_at": "2026-11-04T12:00:00Z"}
        self.assertAlmostEqual(asset_age_hours(row, datetime(2026, 11, 5, 0, 0, tzinfo=timezone.utc)), 12)

    def test_market_key_never_uses_ticker_alone(self):
        self.assertIsNone(market_key({"symbol": "ABC", "asset": "ABC"}))

    def test_market_key_supports_chain_contract_without_prebuilt_identity(self):
        row = {"chain": "base", "contract_address": "0xabc", "pool_address": "0xpool", "market_type": "dex_pool", "source": "gecko"}
        key = market_key(row)
        self.assertEqual(key, "dex:base:0xabc:0xpool")
        self.assertEqual(legacy_pipe_market_key(row), "dex|base|token:0xabc|pool:0xpool")

    def test_dex_market_key_requires_chain_token_and_exact_pool(self):
        self.assertIsNone(market_key({
            "chain": "base", "contract_address": "0xabc", "market_type": "dex_pool"
        }))
        self.assertIsNone(market_key({
            "chain": "base", "pool_address": "0xpool", "market_type": "dex_pool"
        }))

    def test_market_key_includes_pool_to_distinguish_markets(self):
        a = {"identity": "dex:base:0xabc", "contract_address": "0xabc",
             "pool_address": "0x1", "source": "gecko"}
        b = {"identity": "dex:base:0xabc", "contract_address": "0xabc",
             "pool_address": "0x2", "source": "gecko"}
        self.assertNotEqual(market_key(a), market_key(b))

    def test_market_key_preserves_case_sensitive_solana_mints_and_pools(self):
        a = {"chain": "solana", "market_type": "dex_pool",
             "contract_address": "AbCDef123", "pool_address": "PoolX"}
        b = {"chain": "solana", "market_type": "dex_pool",
             "contract_address": "abcdef123", "pool_address": "poolx"}
        self.assertNotEqual(market_key(a), market_key(b))

    def test_market_key_deduplicates_same_pool_across_providers(self):
        a = {"chain": "base", "market_type": "dex_pool", "contract_address": "0xABC",
             "pool_address": "0xDEF", "source": "gecko"}
        b = {**a, "source": "dexscreener"}
        self.assertEqual(market_key(a), market_key(b))

    def test_market_key_separates_chains_and_token_pool_identity(self):
        base = {"chain": "base", "market_type": "dex_pool",
                "contract_address": "0xabc", "pool_address": "0xdef"}
        eth = {**base, "chain": "ethereum"}
        other_pool = {**base, "pool_address": "0x987"}
        self.assertNotEqual(market_key(base), market_key(eth))
        self.assertNotEqual(market_key(base), market_key(other_pool))

    def test_fingerprint_group_is_flagged_but_not_removed(self):
        rows = []
        for i in range(3):
            rows.append({"identity": f"dex:base:0x{i}", "contract_address": f"0xtoken{i}", "pool_address": f"pool{i}", "market_type": "dex_pool", "chain": "base", "source": "gecko:base:new_pools", "price_usd": 0.020995 * (1 + i * 0.0005), "liquidity_usd": 31_500_000 * (1 + i * 0.0005)})
        clusters = fingerprint_clusters(rows)
        self.assertEqual(len(clusters), 1)
        self.assertTrue(all(row.get("fingerprint_cluster_id") for row in rows))
        self.assertEqual(len(rows), 3)

    def test_memory_snapshot_and_idempotent_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; memory = root / "memory"; out.mkdir()
            main = {
                "skill_version": "4.15.3", "created_utc": "2026-11-05T00:00:00Z",
                "top_24h_gainers": [
                    {"identity": "dex:base:0xaaa", "market_type": "dex_pool", "chain": "base", "contract_address": "0xaaa", "pool_address": "0xpool", "asset": "A", "age_hours": 12, "change_24h_pct": 900, "price_usd": 1.0, "retrieved_utc": "2026-11-05T00:00:00Z", "source": "gecko"},
                    {"identity": "dex:base:0xbbb", "market_type": "dex_pool", "chain": "base", "contract_address": "0xbbb", "pool_address": "0xpool2", "asset": "B", "age_hours": 48, "change_24h_pct": 50, "price_usd": 2.0, "retrieved_utc": "2026-11-05T00:00:00Z", "source": "gecko"},
                ],
                "new_pools_under_24h": [], "source_status": [],
            }
            forward = {"skill_version": "4.15.3", "candidates": []}
            (out / "rocket-hunt.json").write_text(json.dumps(main))
            (out / "forward-24h-rocket.json").write_text(json.dumps(forward))
            (out / "rocket-hunt.md").write_text("## Top 24h gainers\n")
            s1 = process_snapshot(out / "rocket-hunt.json", out / "forward-24h-rocket.json", out, memory, "run-1")
            self.assertEqual(s1["retained_report_rows_deduplicated"], 2)
            self.assertEqual(s1["comparable_24h_gainers"], 1)
            self.assertEqual(s1["new_launch_discovery_leads"], 1)
            self.assertEqual(s1["forecast_snapshots_new"], 2)
            s2 = process_snapshot(out / "rocket-hunt.json", out / "forward-24h-rocket.json", out, memory, "run-1")
            self.assertEqual(s2["forecast_snapshots_new"], 0)
            records = read_jsonl_gz(next((memory / "forecasts").glob("*.jsonl.gz")))
            self.assertEqual(len(records), 2)
            j = json.loads((out / "rocket-hunt.json").read_text())
            self.assertEqual(j["core_runtime_version"], "4.15.3")
            self.assertEqual(j["pipeline_version"], "4.18.1")
            self.assertEqual(len(j["reported_top_24h_gainers_raw"]), 2)
            self.assertEqual(len(j["new_launch_discovery_board_v418"]), 1)

    def test_outcome_grading_waits_for_observation_at_or_after_horizon(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; memory = root / "memory"; out.mkdir()
            main = {"skill_version": "4.15.3", "created_utc": "2026-11-05T00:00:00Z", "top_24h_gainers": [
                {"identity": "dex:base:0xaaa", "market_type": "dex_pool", "chain": "base", "contract_address": "0xaaa", "pool_address": "pool1", "asset": "A", "age_hours": 48, "price_usd": 1, "retrieved_utc": "2026-11-05T00:00:00Z", "source": "gecko"}
            ]}
            (out / "rocket-hunt.json").write_text(json.dumps(main)); (out / "rocket-hunt.md").write_text("")
            process_snapshot(out / "rocket-hunt.json", None, out, memory, "run-1")
            main2 = {"skill_version": "4.15.3", "created_utc": "2026-11-05T01:30:00Z", "top_24h_gainers": [
                {"identity": "dex:base:0xaaa", "market_type": "dex_pool", "chain": "base", "contract_address": "0xaaa", "pool_address": "pool1", "asset": "A", "age_hours": 48.5, "price_usd": 1.5, "retrieved_utc": "2026-11-05T00:30:00Z", "source": "gecko"}
            ]}
            (out / "rocket-hunt.json").write_text(json.dumps(main2))
            process_snapshot(out / "rocket-hunt.json", None, out, memory, "run-2")
            outcomes = []
            for p in (memory / "outcomes").glob("*.jsonl.gz"): outcomes.extend(read_jsonl_gz(p))
            self.assertFalse([o for o in outcomes if o["horizon_hours"] == 1])

    def test_outcome_grading_uses_same_market_and_marks_not_fills(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "out"; memory = root / "memory"; out.mkdir()
            main = {"skill_version": "4.15.3", "created_utc": "2026-11-05T00:00:00Z", "top_24h_gainers": [
                {"identity": "dex:base:0xaaa", "market_type": "dex_pool", "chain": "base", "contract_address": "0xaaa", "pool_address": "pool1", "asset": "A", "age_hours": 48, "change_24h_pct": 12, "price_usd": 1, "retrieved_utc": "2026-11-05T00:00:00Z", "source": "gecko"}
            ]}
            (out / "rocket-hunt.json").write_text(json.dumps(main)); (out / "rocket-hunt.md").write_text("")
            process_snapshot(out / "rocket-hunt.json", None, out, memory, "run-1")
            # Next observation is 5 hours later and has the exact same pool identity.
            main2 = {"skill_version": "4.15.3", "created_utc": "2026-11-05T05:00:00Z", "top_24h_gainers": [
                {"identity": "dex:base:0xaaa", "market_type": "dex_pool", "chain": "base", "contract_address": "0xaaa", "pool_address": "pool1", "asset": "A", "age_hours": 53, "change_24h_pct": 15, "price_usd": 1.2, "retrieved_utc": "2026-11-05T05:00:00Z", "source": "gecko"}
            ]}
            (out / "rocket-hunt.json").write_text(json.dumps(main2))
            summary = process_snapshot(out / "rocket-hunt.json", None, out, memory, "run-2")
            outcomes = []
            for p in (memory / "outcomes").glob("*.jsonl.gz"): outcomes.extend(read_jsonl_gz(p))
            one_hour = [o for o in outcomes if o["horizon_hours"] == 1]
            self.assertTrue(one_hour)
            self.assertEqual(one_hour[0]["status"], "OBSERVED_MARK_NOT_EXECUTABLE_FILL")
            self.assertAlmostEqual(one_hour[0]["mark_return_pct"], 20.0)
            self.assertEqual(one_hour[0]["fees_slippage_status"], "NOT_MODELED")

    def test_mature_outcomes_keep_missing_identity_and_mark_evidence_unavailable(self):
        key = "dex|base|token:0xaaa|pool:pool1"
        forecast = {
            "forecast_id": "forecast-mature",
            "market_key": key,
            "snapshot_utc": "2026-11-05T00:00:00Z",
            "feature_snapshot": {
                "market_type": "dex_pool",
                "chain": "base",
                "contract_address": "0xaaa",
                "pool_address": "pool1",
                "price_usd": 1.0,
                "retrieved_utc": "2026-11-05T00:00:00Z",
            },
        }
        now = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc)
        invalid_marks = (
            None,
            {"price_usd": 0.0, "retrieved_utc": "2026-11-13T08:00:00Z"},
            {"price_usd": 1.2, "retrieved_utc": None},
        )

        with tempfile.TemporaryDirectory() as tmp:
            for index, mark in enumerate(invalid_marks):
                with self.subTest(mark=mark):
                    memory = Path(tmp) / f"memory-{index}"
                    path = memory / "forecasts" / "2026-11-05.jsonl.gz"
                    path.parent.mkdir(parents=True)
                    with gzip.open(path, "wt", encoding="utf-8") as handle:
                        handle.write(json.dumps(forecast) + "\n")

                    rows = {} if mark is None else {key: mark}
                    result = grade_outcomes(memory, now, rows, "run-test")
                    outcomes = [
                        row
                        for partition in (memory / "outcomes").glob("*.jsonl.gz")
                        for row in read_jsonl_gz(partition)
                    ]

                    self.assertEqual(result["new_outcomes"], 5)
                    self.assertEqual(result["new_unavailable_outcomes"], 5)
                    self.assertEqual(len(outcomes), 5)
                    self.assertTrue(all(row["status"].startswith("UNAVAILABLE_") for row in outcomes))
                    self.assertTrue(all(row["mark_return_pct"] is None for row in outcomes))
                    self.assertTrue(all(row["observed_price_usd"] is None for row in outcomes))

    def test_later_marks_do_not_rewrite_frozen_forecasts_or_claim_weight_adaptation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            memory = root / "memory"
            out.mkdir()
            main = {
                "created_utc": "2026-11-05T00:00:00Z",
                "dex_discovery_leads": [{
                    "identity": "dex:base:0xaaa",
                    "market_type": "dex_pool",
                    "chain": "base",
                    "contract_address": "0xaaa",
                    "pool_address": "pool1",
                    "price_usd": 1.0,
                    "retrieved_utc": "2026-11-05T00:00:00Z",
                    "forecast_score": 72.5,
                }],
            }
            report = out / "rocket-hunt.json"
            report.write_text(json.dumps(main), encoding="utf-8")
            process_snapshot(report, None, out, memory, "run-first")
            first_forecast = read_jsonl_gz(next((memory / "forecasts").glob("*.jsonl.gz")))[0]

            main["created_utc"] = "2026-11-05T05:00:00Z"
            main["dex_discovery_leads"][0]["price_usd"] = 1.5
            main["dex_discovery_leads"][0]["retrieved_utc"] = "2026-11-05T05:00:00Z"
            main["dex_discovery_leads"][0]["forecast_score"] = 90.0
            report.write_text(json.dumps(main), encoding="utf-8")
            process_snapshot(report, None, out, memory, "run-later")

            forecasts = [
                row
                for partition in (memory / "forecasts").glob("*.jsonl.gz")
                for row in read_jsonl_gz(partition)
            ]
            original = next(row for row in forecasts if row["run_id"] == "run-first")
            saved_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(len(forecasts), 2)
            self.assertEqual(original, first_forecast)
            self.assertEqual(original["feature_snapshot"]["price_usd"], 1.0)
            self.assertEqual(original["raw_score"], 72.5)
            self.assertFalse(original["score_is_probability"])
            self.assertIn(
                "NO AUTOMATIC MODEL WEIGHT CHANGES",
                saved_report["v418_quality_audit"]["learning_policy"],
            )

    def test_outcome_grading_requires_a_timestamped_non_future_price_mark(self):
        key = "dex|base|token:0xaaa|pool:pool1"
        forecast = {
            "forecast_id": "forecast-1",
            "market_key": key,
            "snapshot_utc": "2026-11-05T00:00:00Z",
            "feature_snapshot": {"price_usd": 1.0, "retrieved_utc": "2026-11-05T00:00:00Z"},
        }
        now = datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            for retrieved_utc in (None, "2026-11-05T02:01:00Z"):
               memory = Path(tmp) / ("memory-missing" if retrieved_utc is None else "memory-future")
               path = memory / "forecasts" / "2026-11-05.jsonl.gz"
               path.parent.mkdir(parents=True)
               with gzip.open(path, "wt", encoding="utf-8") as handle:
                   handle.write(json.dumps(forecast) + "\n")

               result = grade_outcomes(memory, now, {
                   key: {"price_usd": 1.2, "retrieved_utc": retrieved_utc}
               }, "run-test")

               self.assertEqual(result["new_outcomes"], 0)
               self.assertEqual(result["pending_horizon_checks"], 5)
               self.assertEqual(list((memory / "outcomes").glob("*.jsonl.gz")) if
                                (memory / "outcomes").exists() else [], [])


if __name__ == "__main__":
    unittest.main()
