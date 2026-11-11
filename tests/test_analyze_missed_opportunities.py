#!/usr/bin/env python3
"""Unit and regression tests for retrospective missed-opportunity analysis."""
from __future__ import annotations

import datetime as dt
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from scripts import analyze_missed_opportunities as amo


def _write_jsonl_gz(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


class TestAnalyzeMissedOpportunities(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.memory_dir = self.root / "scanner-memory"
        self.output_root = self.root / "research-analysis"
        self.memory_dir.mkdir(parents=True)
        self.output_root.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_classifies_gainers_across_tiers_and_reasons(self):
        forecast_ts = "2026-11-06T10:00:00Z"
        observed_ts = "2026-11-07T11:00:00Z"  # 25h elapsed -> 1h lag (bounded <= 6h)

        forecasts = [
            # 1. CEX spot gainer (+150%)
            {
                "forecast_id": "f_cex",
                "snapshot_utc": forecast_ts,
                "identity": "cex:TOKEN1",
                "market_key": "cex:token1||binance|",
                "raw_score": 25.0,
                "model_id": "v4.19.0-eb-beta-binomial",
                "discovery_lanes": ["top_24h_gainers"],
                "feature_snapshot": {
                    "asset": "TOKEN1",
                    "venue": "binance",
                    "market_type": "cex_spot",
                },
            },
            # 2. DEX gainer with low score (+80%)
            {
                "forecast_id": "f_low_score",
                "snapshot_utc": forecast_ts,
                "identity": "dex:solana:mint2",
                "market_key": "dex:solana:pool2",
                "raw_score": 15.0,
                "model_id": "v4.19.0-eb-beta-binomial",
                "discovery_lanes": ["discovery_rows_v421"],
                "feature_snapshot": {
                    "chain": "solana",
                    "pool_address": "pool2",
                    "contract_address": "mint2",
                    "market_type": "dex_pool",
                },
            },
            # 3. DEX gainer with high score filtered before qualification (+350%)
            {
                "forecast_id": "f_filtered",
                "snapshot_utc": forecast_ts,
                "identity": "dex:ethereum:mint3",
                "market_key": "dex:ethereum:pool3",
                "raw_score": 75.0,
                "model_id": "v4.19.0-eb-beta-binomial",
                "discovery_lanes": ["early_breakout_leaders"],
                "feature_snapshot": {
                    "chain": "ethereum",
                    "pool_address": "pool3",
                    "contract_address": "mint3",
                    "market_type": "dex_pool",
                },
            },
            # 4. Gainer with incomplete identity (+50%)
            {
                "forecast_id": "f_no_id",
                "snapshot_utc": forecast_ts,
                "identity": "dex:unknown:mint4",
                "market_key": "dex:unknown:pool4",
                "raw_score": 50.0,
                "model_id": "v4.19.0-eb-beta-binomial",
                "discovery_lanes": ["universe_rows_v419"],
                "feature_snapshot": {
                    "market_type": "dex_pool",
                    # pool_address and contract_address missing!
                },
            },
            # 5. Non-gainer (+10%, should not be counted as big mover)
            {
                "forecast_id": "f_small",
                "snapshot_utc": forecast_ts,
                "identity": "dex:solana:mint5",
                "market_key": "dex:solana:pool5",
                "raw_score": 60.0,
                "model_id": "v4.19.0-eb-beta-binomial",
            },
        ]

        outcomes = [
            {
                "forecast_id": "f_cex",
                "market_key": "cex:token1||binance|",
                "horizon_hours": 24,
                "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL",
                "observed_utc": observed_ts,
                "base_price_usd": 1.0,
                "observed_price_usd": 2.5,
                "mark_return_pct": 150.0,
            },
            {
                "forecast_id": "f_low_score",
                "market_key": "dex:solana:pool2",
                "horizon_hours": 24,
                "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL",
                "observed_utc": observed_ts,
                "base_price_usd": 0.1,
                "observed_price_usd": 0.18,
                "mark_return_pct": 80.0,
            },
            {
                "forecast_id": "f_filtered",
                "market_key": "dex:ethereum:pool3",
                "horizon_hours": 24,
                "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL",
                "observed_utc": observed_ts,
                "base_price_usd": 0.01,
                "observed_price_usd": 0.045,
                "mark_return_pct": 350.0,
            },
            {
                "forecast_id": "f_no_id",
                "market_key": "dex:unknown:pool4",
                "horizon_hours": 24,
                "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL",
                "observed_utc": observed_ts,
                "base_price_usd": 10.0,
                "observed_price_usd": 15.0,
                "mark_return_pct": 50.0,
            },
            {
                "forecast_id": "f_small",
                "market_key": "dex:solana:pool5",
                "horizon_hours": 24,
                "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL",
                "observed_utc": observed_ts,
                "base_price_usd": 1.0,
                "observed_price_usd": 1.1,
                "mark_return_pct": 10.0,
            },
        ]

        _write_jsonl_gz(self.memory_dir / "forecasts" / "2026-11-06.jsonl.gz", forecasts)
        _write_jsonl_gz(self.memory_dir / "outcomes" / "2026-11-06.jsonl.gz", outcomes)

        report = amo.analyze_missed_opportunities(self.memory_dir, self.output_root)

        self.assertEqual(report["total_movers_40pct_plus"], 4)
        self.assertEqual(report["missed_count"], 4)
        self.assertEqual(report["tier_summary"]["+40%"]["total_movers"], 4)
        self.assertEqual(report["tier_summary"]["+60%"]["total_movers"], 3)
        self.assertEqual(report["tier_summary"]["+100%"]["total_movers"], 2)
        self.assertEqual(report["tier_summary"]["+300%"]["total_movers"], 1)

        breakdown = report["miss_category_breakdown"]
        self.assertEqual(breakdown["CEX_SPOT_RESEARCH_ONLY"]["count"], 1)
        self.assertEqual(breakdown["LOW_HEURISTIC_SCORE"]["count"], 1)
        self.assertEqual(breakdown["DISCOVERY_FILTERED_BEFORE_QUALIFICATION"]["count"], 1)
        self.assertEqual(breakdown["INSUFFICIENT_EXACT_DEX_IDENTITY"]["count"], 1)

        # Verify output directory and artifacts
        output_dirs = list(self.output_root.glob("missed-opportunities-v1-*"))
        self.assertEqual(len(output_dirs), 1)
        out_dir = output_dirs[0]
        self.assertTrue((out_dir / "report.json").is_file())
        self.assertTrue((out_dir / "report.md").is_file())
        self.assertTrue((out_dir / "manifest.json").is_file())

    def test_excludes_outcomes_beyond_max_observation_lag(self):
        forecast_ts = "2026-11-06T10:00:00Z"
        # 32h elapsed -> 8h lag (> 6h threshold)
        late_ts = "2026-11-07T18:00:00Z"

        forecasts = [{
            "forecast_id": "f_late",
            "snapshot_utc": forecast_ts,
            "identity": "dex:solana:mint_late",
            "raw_score": 50.0,
        }]
        outcomes = [{
            "forecast_id": "f_late",
            "horizon_hours": 24,
            "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL",
            "observed_utc": late_ts,
            "base_price_usd": 1.0,
            "observed_price_usd": 2.0,
            "mark_return_pct": 100.0,
        }]

        _write_jsonl_gz(self.memory_dir / "forecasts" / "2026-11-06.jsonl.gz", forecasts)
        _write_jsonl_gz(self.memory_dir / "outcomes" / "2026-11-06.jsonl.gz", outcomes)

        report = amo.analyze_missed_opportunities(self.memory_dir, self.output_root)
        self.assertEqual(report["total_movers_40pct_plus"], 0)
        self.assertEqual(report["bounded_lag_24h_outcomes"], 0)


if __name__ == "__main__":
    unittest.main()
