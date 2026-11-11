#!/usr/bin/env python3
"""Unit tests for the v4.19.0 Collector, Network Rotation, Full-Universe Memory & Probability Calibration Upgrade."""
from __future__ import annotations

import json, sys, tempfile, unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.v419_upgrade import (
    CALIBRATION_MAX_OBSERVATION_LAG_HOURS, CORE_ANCHOR_NETWORKS, UPGRADE_VERSION, _read_jsonl_gz, apply_v419_post_scan,
    build_calibration_model, calibrate_candidate_row, install_Collector_hooks_into_rocket_hunt,
    install_runtime_patches, select_rotating_gecko_networks, update_network_rotation_ledger,
    _merge_universe_market_row,
)


class V419UpgradeTests(unittest.TestCase):
    def test_rotating_223_gecko_networks_covers_entire_registry_across_slots(self):
        registry = list(CORE_ANCHOR_NETWORKS) + [f"frontier_chain_{i}" for i in range(1, 212)] + ["eth_sepolia_testnet"]
        swept_all: set[str] = set()
        for slot in range(12):
            selected, meta = select_rotating_gecko_networks(registry, list(CORE_ANCHOR_NETWORKS), limit=32, rotation_slot=slot, anchor_count=12)
            self.assertEqual(len(selected), 32)
            self.assertEqual(meta["supported_network_count"], 223)
            self.assertNotIn("eth_sepolia_testnet", selected)
            for anchor in CORE_ANCHOR_NETWORKS:
                self.assertIn(anchor, selected)
            swept_all.update(selected)
        self.assertEqual(len(swept_all), 223)

    def test_network_rotation_ledger_accumulates_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp)
            s1 = update_network_rotation_ledger(mem, list(CORE_ANCHOR_NETWORKS) + [f"net_{i}" for i in range(20)], 223, {"rotation_slot": 0, "rotation_offset": 0, "registry_complete": True}, "run-1", "2026-11-05T12:00:00Z")
            self.assertEqual(s1["cumulative_unique_networks_swept_count"], 32)
            s2 = update_network_rotation_ledger(mem, list(CORE_ANCHOR_NETWORKS) + [f"net_{i}" for i in range(20, 40)], 223, {"rotation_slot": 1, "rotation_offset": 20, "registry_complete": True}, "run-2", "2026-11-05T16:00:00Z")
            self.assertEqual(s2["cumulative_unique_networks_swept_count"], 52)
            self.assertGreater(s2["cumulative_network_coverage_pct"], s1["cumulative_network_coverage_pct"])

    def test_collector_hook_exports_full_universe_and_version_4190(self):
        class DummyHunter:
            def __init__(self):
                self.candidates = [
                    {"identity": f"dex:base:0x{i:04x}", "symbol": f"TOK{i}", "asset": f"TOK{i}", "chain": "base", "market_type": "dex_pool", "pool_address": f"0xpool{i:04x}", "price_usd": 1.25, "change_24h_pct": 15.0 + (i % 40), "liquidity_usd": 150000.0, "volume_24h_usd": 300000.0, "age_hours": 48.0, "rank_score": 62.0, "source": "geckoterminal"}
                    for i in range(600)
                ]
            def discover_gecko_networks(self, preferred_networks=None, limit=24):
                return [], {}
            def build_output(self):
                return {"skill_version": "4.15.3", "coverage": {"unique_asset_rows": len(self.candidates)}, "top_24h_gainers": self.candidates[:100]}

        class DummyModule:
            VERSION = "4.15.3"
            GECKO_NETWORK_REGISTRY_PAGES = 3
            RocketHunter = DummyHunter

        original = DummyHunter()
        original.candidates.append({"identity": "dex:base:unpriced", "asset": "UNPRICED", "symbol": "UP", "chain": "base", "market_type": "dex_pool", "pool_address": "0xunpriced", "price_usd": None, "change_24h_pct": None, "source": "geckoterminal"})
        install_Collector_hooks_into_rocket_hunt(DummyModule)
        self.assertEqual(DummyModule.VERSION, UPGRADE_VERSION)
        self.assertEqual(DummyModule.GECKO_NETWORK_REGISTRY_PAGES, 12)
        out = original.build_output()
        self.assertEqual(out["skill_version"], UPGRADE_VERSION)
        self.assertEqual(len(out["universe_rows_v419"]), 600)
        self.assertEqual(len(out["discovery_rows_v421"]), 601)
        unpriced = next(r for r in out["discovery_rows_v421"] if r["identity"] == "dex:base:unpriced")
        self.assertEqual(unpriced["discovery_status"], "DISCOVERY_ONLY_INCOMPLETE_MARKET_DATA")
        self.assertIn("price_usd", unpriced["discovery_missing_fields"])
        self.assertIn("change_24h_pct", unpriced["discovery_missing_fields"])

    def test_discover_registry_stops_after_short_final_page(self):
        class DummyHunter:
            def __init__(self):
                self.requested_pages = []
            def build_output(self):
                return {"coverage": {}}
            def _fetch_gecko_json(self, source, url):
                page = int(url.rsplit("=", 1)[1])
                self.requested_pages.append(page)
                count = 100 if page < 3 else 24
                return {"data": [{"id": f"network_{page}_{i}"} for i in range(count)]}
        class DummyModule:
            VERSION = "4.15.3"
            GECKO_NETWORK_REGISTRY_PAGES = 12
            GECKO_NETWORK_ALIASES = {}
            DEFAULT_NETWORKS = CORE_ANCHOR_NETWORKS
            GECKO_ROOT = "https://api.example/v2"
            _items = staticmethod(lambda payload: payload.get("data", []))
            RocketHunter = DummyHunter
        install_Collector_hooks_into_rocket_hunt(DummyModule)
        hunter = DummyHunter()
        selected, meta = hunter.discover_gecko_networks(limit=32)
        self.assertEqual(hunter.requested_pages, [1, 2, 3])
        self.assertEqual(meta["registry_pages_ok"], 3)
        self.assertTrue(meta["registry_exhausted"])
        self.assertTrue(meta["registry_complete"])
        self.assertFalse(meta["registry_page_cap_reached"])
        self.assertEqual(meta["supported_network_count"], 224)
        self.assertEqual(len(selected), 32)

    def test_calibration_model_and_under_24h_exclusion(self):
        forecasts = [{"forecast_id": f"fc-{i}", "snapshot_utc": "2026-11-05T00:00:00Z", "raw_score": 70.0, "feature_snapshot": {"forecast_score": 70.0, "liquidity_usd": 300000.0, "volume_24h_usd": 600000.0, "age_hours_v418": 60.0, "price_usd": 1.0, "retrieved_utc": "2026-11-05T00:00:00Z"}} for i in range(30)]
        outcomes = [{"forecast_id": f"fc-{i}", "horizon_hours": 24, "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL", "observed_utc": "2026-11-06T00:00:00Z", "mark_return_pct": 18.0 if i < 12 else -4.0} for i in range(30)]
        cal = build_calibration_model(forecasts, outcomes, as_of_utc="2026-11-06T02:00:00Z")
        self.assertEqual(cal["calibration_mode"], "EMPIRICAL_POSTERIOR_CALIBRATED")
        self.assertEqual(cal["resolved_24h_outcomes_used"], 30)
        mature = {"asset": "MATURE", "market_type": "dex_pool", "age_hours_v418": 48.0, "return_window_status": "SOURCE_REPORTED_24H_POOL_AGE_GE_24H", "forecast_score": 70.0, "liquidity_usd": 300000.0, "volume_24h_usd": 600000.0}
        calibrate_candidate_row(mature, cal)
        self.assertTrue(mature["score_is_probability"])
        self.assertIsNotNone(mature["calibrated_prob_24h"])
        young = {"asset": "YOUNG", "market_type": "dex_pool", "age_hours_v418": 8.5, "return_window_status": "INSUFFICIENT_24H_HISTORY", "forecast_score": 95.0}
        calibrate_candidate_row(young, cal)
        self.assertFalse(young["score_is_probability"])
        self.assertIsNone(young["calibrated_prob_24h"])

    def test_as_of_calibration_excludes_missing_and_future_timestamps(self):
        forecasts = [
            {"forecast_id": "valid", "snapshot_utc": "2026-11-05T08:00:00Z", "raw_score": 70.0, "feature_snapshot": {"price_usd": 1.0, "retrieved_utc": "2026-11-05T08:00:00Z"}},
            {"forecast_id": "future-forecast", "snapshot_utc": "2026-11-06T13:00:00Z", "raw_score": 70.0, "feature_snapshot": {}},
            {"forecast_id": "missing-forecast-time", "raw_score": 70.0, "feature_snapshot": {}},
        ]
        outcomes = [
            {"forecast_id": "valid", "horizon_hours": 24, "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL", "observed_utc": "2026-11-06T08:00:00Z", "mark_return_pct": 15.0},
            {"forecast_id": "valid", "horizon_hours": 24, "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL", "mark_return_pct": 15.0},
            {"forecast_id": "future-forecast", "horizon_hours": 24, "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL", "observed_utc": "2026-11-07T13:00:00Z", "mark_return_pct": 15.0},
            {"forecast_id": "missing-forecast-time", "horizon_hours": 24, "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL", "observed_utc": "2026-11-06T08:00:00Z", "mark_return_pct": 15.0},
            {"forecast_id": "valid", "horizon_hours": 24, "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL", "observed_utc": "2026-11-06T13:00:00Z", "mark_return_pct": 15.0},
        ]
        cal = build_calibration_model(forecasts, outcomes, as_of_utc="2026-11-06T12:00:00Z")
        self.assertEqual(cal["resolved_24h_outcomes_used"], 1)
        self.assertEqual(cal["time_boundary_policy"], "24H_OUTCOME_OBSERVED_WITHIN_6H_LAG; PRICE_AND_FORECAST_PROVENANCE_REQUIRED; NO_FUTURE_DATA")
        self.assertEqual(cal["as_of_utc"], "2026-11-06T12:00:00Z")

    def test_calibration_rejects_missing_stale_and_future_price_provenance(self):
        forecasts = [
            {"forecast_id": "fresh", "snapshot_utc": "2026-11-05T08:00:00Z", "raw_score": 70,
             "feature_snapshot": {"price_usd": 1.0, "retrieved_utc": "2026-11-05T07:45:00Z"}},
            {"forecast_id": "missing", "snapshot_utc": "2026-11-05T08:00:00Z", "raw_score": 70,
             "feature_snapshot": {"price_usd": 1.0}},
            {"forecast_id": "stale", "snapshot_utc": "2026-11-05T08:00:00Z", "raw_score": 70,
             "feature_snapshot": {"price_usd": 1.0, "retrieved_utc": "2026-11-05T07:29:00Z"}},
            {"forecast_id": "future", "snapshot_utc": "2026-11-05T08:00:00Z", "raw_score": 70,
             "feature_snapshot": {"price_usd": 1.0, "retrieved_utc": "2026-11-05T08:02:00Z"}},
        ]
        outcomes = [
            {"forecast_id": row["forecast_id"], "horizon_hours": 24,
             "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL", "observed_utc": "2026-11-06T08:00:00Z",
             "mark_return_pct": 12.0}
            for row in forecasts
        ]
        cal = build_calibration_model(forecasts, outcomes, as_of_utc="2026-11-06T09:00:00Z")
        self.assertEqual(cal["resolved_24h_outcomes_used"], 1)

    def test_calibration_requires_24h_mark_with_bounded_observation_lag(self):
        forecasts = [
            {"forecast_id": name, "snapshot_utc": "2026-11-05T00:00:00Z", "raw_score": 70,
             "feature_snapshot": {"price_usd": 1.0, "retrieved_utc": "2026-11-05T00:00:00Z"}}
            for name in ("early", "within-lag", "at-limit", "late")
        ]
        outcomes = [
            {"forecast_id": "early", "horizon_hours": 24, "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL",
             "observed_utc": "2026-11-05T23:59:59Z", "mark_return_pct": 12.0},
            {"forecast_id": "within-lag", "horizon_hours": 24, "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL",
             "observed_utc": "2026-11-06T01:00:00Z", "mark_return_pct": 12.0},
            {"forecast_id": "at-limit", "horizon_hours": 24, "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL",
             "observed_utc": "2026-11-06T06:00:00Z", "mark_return_pct": 12.0},
            {"forecast_id": "late", "horizon_hours": 24, "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL",
             "observed_utc": "2026-11-06T06:00:01Z", "mark_return_pct": 12.0},
        ]
        cal = build_calibration_model(forecasts, outcomes, as_of_utc="2026-11-07T00:00:00Z")
        self.assertEqual(CALIBRATION_MAX_OBSERVATION_LAG_HOURS, 6.0)
        self.assertEqual(cal["resolved_24h_outcomes_used"], 2)

    def test_calibration_excludes_immature_and_unknown_age_dex_forecasts(self):
        forecasts = [
            {
                "forecast_id": "mature", "snapshot_utc": "2026-11-04T00:00:00Z", "raw_score": 70.0,
                "feature_snapshot": {"market_type": "dex_pool", "age_hours_v418": 36.0, "return_window_status": "SOURCE_REPORTED_24H_POOL_AGE_GE_24H", "chain": "base", "contract_address": "0xabc", "pool_address": "0xdef", "price_usd": 1.0, "retrieved_utc": "2026-11-04T00:00:00Z"},
            },
            {
                "forecast_id": "young", "snapshot_utc": "2026-11-04T00:00:00Z", "raw_score": 70.0,
                "feature_snapshot": {"market_type": "dex_pool", "age_hours_v418": 8.0, "return_window_status": "INSUFFICIENT_24H_HISTORY", "chain": "base", "contract_address": "0xaaa", "pool_address": "0xbbb", "price_usd": 1.0, "retrieved_utc": "2026-11-04T00:00:00Z"},
            },
            {
                "forecast_id": "unknown", "snapshot_utc": "2026-11-04T00:00:00Z", "raw_score": 70.0,
                "feature_snapshot": {"market_type": "dex_pool", "age_hours_v418": None, "return_window_status": "UNKNOWN_POOL_AGE_NOT_COMPARABLE", "chain": "base", "contract_address": "0x111", "pool_address": "0x222", "price_usd": 1.0, "retrieved_utc": "2026-11-04T00:00:00Z"},
            },
        ]
        outcomes = [
            {"forecast_id": name, "horizon_hours": 24, "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL", "observed_utc": "2026-11-05T00:00:00Z", "mark_return_pct": 15.0}
            for name in ("mature", "young", "unknown")
        ]
        cal = build_calibration_model(forecasts, outcomes, as_of_utc="2026-11-05T12:00:00Z")
        self.assertEqual(cal["resolved_24h_outcomes_used"], 1)
        self.assertEqual(cal["buckets"]["bucket_65_80"]["n_resolved_24h"], 1)

    def test_low_sample_bucket_does_not_emit_probability(self):
        forecasts = [
            {"forecast_id": f"small-{i}", "snapshot_utc": "2026-11-05T00:00:00Z", "raw_score": 70.0, "feature_snapshot": {"price_usd": 1.0, "retrieved_utc": "2026-11-05T00:00:00Z"}}
            for i in range(9)
        ]
        outcomes = [
            {"forecast_id": f"small-{i}", "horizon_hours": 24, "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL", "observed_utc": "2026-11-06T00:00:00Z", "mark_return_pct": 12.0 if i < 4 else -2.0}
            for i in range(9)
        ]
        cal = build_calibration_model(forecasts, outcomes, as_of_utc="2026-11-06T02:00:00Z")
        row = {"asset": "MATURE", "market_type": "dex_pool", "age_hours_v418": 48.0, "return_window_status": "SOURCE_REPORTED_24H_POOL_AGE_GE_24H", "forecast_score": 70.0}
        calibrate_candidate_row(row, cal)
        self.assertFalse(row["score_is_probability"])
        self.assertIsNone(row["calibrated_prob_24h"])
        self.assertIsNotNone(row["prior_breakout_estimate_24h"])

    def test_runtime_installer_widens_wrapper_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            (root / "VERSION").write_text("4.18.1\n", encoding="utf-8")
            (scripts / "rocket_hunt.py").write_text(
                "networks = default_networks[:a.network_sweep_limit] + extra_networks\n"
                "network_sweep_count = len(networks)\n"
                "        if network_names and per_network > 0:\n"
                "            for network in network_names:\n"
                "                for endpoint in (\"trending_pools\", \"new_pools\", \"pools\"):\n"
                "                    query = {\"page\": 1, \"include\": \"base_token,quote_token,dex\"}\n"
                "                    if endpoint == \"trending_pools\":\n"
                "                        query[\"duration\"] = \"24h\"\n"
                "                    elif endpoint == \"pools\":\n"
                "                        query[\"sort\"] = \"h24_volume_usd_desc\"\n"
                "                    routes.append((\"gecko:%s:%s\" % (network, endpoint),\n"
                "                                   \"%s/networks/%s/%s?%s\" % (GECKO_ROOT, quote(network, safe=\"\"), endpoint, urlencode(query))))\n"
                "        gecko_network_calls = len(selected_networks) * 3 if include_gecko and gecko_network_sweep else 0\n"
                "        routes = routes[:max(4, len(base_routes) + (len(network_names or []) * 3 if per_network else 0))]\n"
                "    p.add_argument(\"--gecko-pages\", type=int, default=3, help=\"global GeckoTerminal pages per timeframe/list (1-10)\")\n"
                "if __name__ == \"__main__\":\n    pass\n",
                encoding="utf-8",
            )
            (scripts / "run_full_scan.py").write_text(
                "p.add_argument(\"--network-sweep-limit\", type=int, default=24)\n"
                "if not 0 <= args.network_sweep_limit <= 24:\n"
                "    p.error(\"--network-sweep-limit must be between 0 and 24\")\n",
                encoding="utf-8",
            )
            first = install_runtime_patches(root)
            second = install_runtime_patches(root)
            rocket = (scripts / "rocket_hunt.py").read_text(encoding="utf-8")
            wrapper = (scripts / "run_full_scan.py").read_text(encoding="utf-8")
            self.assertEqual(first["installed_version"], UPGRADE_VERSION)
            self.assertEqual(second["installed_version"], UPGRADE_VERSION)
            self.assertIn("network_sweep_count = (max(0, a.network_sweep_limit)", rocket)
            self.assertIn("network_pages = max(1, min(10, int(pages)))", rocket)
            self.assertIn("gecko_network_calls = len(selected_networks) * 3 * max(1, min(10, gecko_pages))", rocket)
            self.assertIn("len(network_names or []) * 3 * max(1, min(10, int(pages)))", rocket)
            self.assertIn("gecko:%s:%s:page:%s", rocket)
            self.assertIn("GeckoTerminal pages per global list and per-network pool family", rocket)
            self.assertIn("install_Collector_hooks_into_rocket_hunt", rocket)
            self.assertEqual(rocket.count("_iv419(_s.modules[__name__])"), 1)
            self.assertIn("default=32", wrapper)
            self.assertIn("<= 64", wrapper)
            self.assertEqual((root / "VERSION").read_text(encoding="utf-8").strip(), UPGRADE_VERSION)

    def test_partial_registry_does_not_claim_coverage_percentage(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = update_network_rotation_ledger(
                Path(tmp), list(CORE_ANCHOR_NETWORKS), 223,
                {"rotation_slot": 0, "rotation_offset": 0, "registry_complete": False},
                "partial-run", "2026-11-05T12:00:00Z",
            )
            self.assertIsNone(state["cumulative_network_coverage_pct"])
            self.assertFalse(state["registry_size_known"])
            self.assertFalse(state["registry_complete"])

    def test_unknown_registry_size_does_not_claim_coverage_percentage(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = update_network_rotation_ledger(
                Path(tmp), list(CORE_ANCHOR_NETWORKS), 0,
                {"rotation_slot": 0, "rotation_offset": 0, "registry_complete": False},
                "fallback-run", "2026-11-05T12:00:00Z",
            )
            self.assertIsNone(state["cumulative_network_coverage_pct"])
            self.assertFalse(state["registry_size_known"])
            self.assertFalse(state["registry_complete"])

    def test_duplicate_market_merge_keeps_youngest_age_and_suppresses_24h_move(self):
        mature = {
            "identity": "dex:base:0xpool", "market_type": "dex_pool", "pool_address": "0xpool",
            "age_hours": 48.0, "age_hours_v418": 48.0, "change_24h_pct": 35.0,
        }
        young = {
            "identity": "dex:base:0xpool", "market_type": "dex_pool", "pool_address": "0xpool",
            "age_hours": 7.0, "age_hours_v418": 7.0, "change_24h_pct": 80.0,
        }
        merged = _merge_universe_market_row(mature, young)
        self.assertEqual(merged["age_hours_v418"], 7.0)
        self.assertIsNone(merged["change_24h_pct"])
        self.assertEqual(merged["source_reported_change_24h_pct"], 35.0)
        self.assertEqual(merged["return_window_status"], "INSUFFICIENT_24H_HISTORY")

    def test_post_scan_ingests_full_universe_and_updates_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); outdir = root / "out"; memdir = root / "mem"; outdir.mkdir(); memdir.mkdir()
            rep = [{"identity": f"dex:base:0x{i:04x}", "contract_address": f"0xtoken{i:04x}", "asset": f"REP{i}", "symbol": f"REP{i}", "chain": "base", "market_type": "dex_pool", "pool_address": f"0xpool{i:04x}", "price_usd": 2.0, "retrieved_utc": "2026-11-05T12:00:00Z", "change_24h_pct": 25.0, "liquidity_usd": 200000.0, "volume_24h_usd": 500000.0, "age_hours": 36.0, "age_hours_v418": 36.0, "return_window_status": "SOURCE_REPORTED_24H_POOL_AGE_GE_24H", "rank_score": 68.0} for i in range(2)]
            uni = rep + [{"identity": f"dex:solana:0x{i:04x}", "contract_address": f"SolToken{i:04x}", "asset": f"UNI{i}", "symbol": f"UNI{i}", "chain": "solana", "market_type": "dex_pool", "pool_address": f"SolPool{i:04x}", "price_usd": 0.5, "retrieved_utc": "2026-11-05T12:00:00Z", "change_24h_pct": 5.0, "liquidity_usd": 80000.0, "volume_24h_usd": 120000.0, "age_hours": 72.0, "age_hours_v418": 72.0, "return_window_status": "SOURCE_REPORTED_24H_POOL_AGE_GE_24H", "rank_score": 52.0} for i in range(2, 10)]
            uni[2].update({
                "early_breakout_signal_version": "v4.25-early-breakout.v1",
                "early_breakout_model_id": "v4.25-early-breakout",
                "early_breakout_score": 67.5,
                "early_breakout_components": {"1h_momentum": 6.0},
            })
            discovery = [{"identity": "dex:base:0xmissing", "asset": "MISSING", "symbol": "MISS", "chain": "base", "market_type": "dex_pool", "pool_address": "0xmissing", "discovery_status": "DISCOVERY_ONLY_INCOMPLETE_MARKET_DATA", "discovery_missing_fields": ["price_usd", "change_24h_pct"]}]
            (outdir / "rocket-hunt.json").write_text(json.dumps({"skill_version": "4.15.3", "created_utc": "2026-11-05T12:00:00Z", "coverage": {"unique_asset_rows": 10}, "top_24h_gainers": rep, "comparable_24h_gainers_v418": rep, "universe_rows_v419": uni, "discovery_rows_v421": discovery}))
            (outdir / "forward-24h-rocket.json").write_text(json.dumps({"skill_version": "4.15.3", "candidates": [dict(rep[0], forecast_score=74.0)], "forecast_leader": dict(rep[0], forecast_score=74.0)}))
            (outdir / "v418-quality-audit.json").write_text(json.dumps({"learning": {}}))
            (outdir / "scan-status.json").write_text(json.dumps({
                "run_id": "run-v419-test",
                "scanner_version": "4.28.0",
                "configuration_fingerprint": "cfg-v419-test",
            }))
            (outdir / "rocket-hunt.md").write_text("# Report\n")
            s = apply_v419_post_scan(outdir, memdir, "run-v419-test", now_override=datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc))
            self.assertEqual(s["full_universe_assets_snapshotted"], 10)
            forecasts = _read_jsonl_gz(memdir / "forecasts" / "2026-11-05.jsonl.gz")
            self.assertEqual(len(forecasts), 10)
            captured = next(row for row in forecasts
                            if row["feature_snapshot"].get("v425_early_breakout"))
            signal = captured["feature_snapshot"]["v425_early_breakout"]
            self.assertEqual(signal["score"], 67.5)
            self.assertEqual(signal["components"]["1h_momentum"], 6.0)
            self.assertIsNone(signal["components"]["5m_impulse"])
            self.assertEqual(captured["configuration_fingerprint"], "cfg-v419-test")
            manifest = json.loads((memdir / "memory-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["pipeline_version"], UPGRADE_VERSION)
            self.assertEqual(manifest["core_runtime_version"], "4.15.3")
            self.assertEqual(manifest["full_universe_market_rows_snapshotted_v419"], 10)
            self.assertEqual(manifest["discovery_rows_this_run"], 1)
            self.assertEqual(manifest["discovery_architecture_version"], "4.22.0")
            discovery_records = _read_jsonl_gz(memdir / "discovery" / "2026-11-05.jsonl.gz")
            self.assertEqual(len(discovery_records), 1)
            self.assertEqual(discovery_records[0]["discovery_snapshot"]["discovery_status"], "DISCOVERY_ONLY_INCOMPLETE_MARKET_DATA")


if __name__ == "__main__":
    unittest.main()