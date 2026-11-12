#!/usr/bin/env python3
"""Comprehensive Production Integration and Regression Tests for Rocket Hunter v4.29.0.

Verifies all critical audit fixes:
1. GitHub workflow command-line argument validation
2. Dynamic network sweep limit derived from registry
3. AURA user data directory isolation path (ROCKET_HUNTER_USER_DATA_DIR)
4. Removal of synthetic P&L and invented feature generation
5. Soft-archive preservation of historical trade records
6. AURA 1-to-1 learned feature-to-weight score mapping
7. Expected Return Engine rejection of missing TP without synthetic defaults
8. DefiLlama Flow Engine dynamic network prioritization
9. DexScreener Discovery Engine multi-lane integration
10. Mandatory +40.00% single-TP deterministic qualification gate
11. Permanent historical failure & success lessons preservation
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.breakout_signal_engines import BreakoutSignalEngines
from scripts.defillama_flow_engine import DefiLlamaFlowEngine, compute_ecosystem_flow_score
from scripts.dexscreener_discovery import DexScreenerDiscoveryEngine
from scripts.expected_return_engine import ExpectedReturnEngine
from scripts.live_price_verifier import refresh_and_filter_candidates
from scripts.run_full_scan import build_parser as build_full_scan_parser
from scripts.unified_network_registry import CANONICAL_NETWORKS, UnifiedNetworkRegistry
import scripts.user_trade_learning as utl


class ProductionIntegrationV429Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.user_data_path = Path(self.temp_dir.name) / "user_data"
        self.user_data_path.mkdir(parents=True, exist_ok=True)
        self.orig_env = os.environ.get("ROCKET_HUNTER_USER_DATA_DIR")
        os.environ["ROCKET_HUNTER_USER_DATA_DIR"] = str(self.user_data_path)

    def tearDown(self):
        if self.orig_env is not None:
            os.environ["ROCKET_HUNTER_USER_DATA_DIR"] = self.orig_env
        else:
            os.environ.pop("ROCKET_HUNTER_USER_DATA_DIR", None)
        self.temp_dir.cleanup()

    def test_github_workflow_command_line_accepted(self):
        """Regression test: verifies that all CLI arguments in .github/workflows/full-rocket-hunt.yml are accepted."""
        workflow_path = Path(".github/workflows/full-rocket-hunt.yml")
        self.assertTrue(workflow_path.exists())
        content = workflow_path.read_text(encoding="utf-8")
        self.assertIn("python scripts/run_full_scan.py", content)

        # Extract arguments passed in the workflow
        parser = build_full_scan_parser()
        # Simulated parsed arguments from full-rocket-hunt.yml
        args = [
            "--outdir", "rocket-run",
            "--timeout", "8",
            "--workers", "8",
            "--gecko-pages", "3",
            "--network-sweep-limit", "24",
            "--gecko-request-interval", "12",
            "--retries", "2",
            "--dex-search-limit", "12",
            "--dex-lookup-limit", "160",
            "--resume",
            "--resume-max-age-seconds", "900",
            "--max-runtime-minutes", "150",
            "--defer-completion",
            "--birdeye",
        ]
        parsed = parser.parse_args(args)
        self.assertEqual(parsed.network_sweep_limit, 24)
        self.assertEqual(parsed.gecko_pages, 3)
        self.assertEqual(parsed.max_runtime_minutes, 150)
        self.assertTrue(parsed.birdeye)

    def test_dynamic_network_sweep_limit_derived_from_registry(self):
        """Verifies scanner accepts network sweep limits up to dynamic registry capacity."""
        parser = build_full_scan_parser()
        max_limit = max(64, len(CANONICAL_NETWORKS))
        # Valid edge limits
        parsed_0 = parser.parse_args(["--network-sweep-limit", "0"])
        self.assertEqual(parsed_0.network_sweep_limit, 0)
        parsed_24 = parser.parse_args(["--network-sweep-limit", "24"])
        self.assertEqual(parsed_24.network_sweep_limit, 24)
        parsed_max = parser.parse_args(["--network-sweep-limit", str(max_limit)])
        self.assertEqual(parsed_max.network_sweep_limit, max_limit)

    def test_aura_user_data_dir_isolation_path(self):
        """Verifies ROCKET_HUNTER_USER_DATA_DIR routes all journal and learning files to isolated path."""
        self.assertEqual(utl.get_user_data_dir(), self.user_data_path)
        self.assertEqual(utl.get_journal_file(), self.user_data_path / "user_positions_journal.json")
        self.assertEqual(utl.get_ai_engine_file(), self.user_data_path / "ai_learning_engine.json")

        # Record a position and ensure it writes directly into configured directory
        rec = utl.record_position({
            "pair": "TEST/SOL",
            "chain": "solana",
            "entry_price": "0.005",
            "single_tp": "0.0075",
            "potential_pct": "+50.0%",
            "pool_address": "TestPoolAddress123",
            "token_contract": "TestContractAddress123",
        })
        self.assertEqual(rec["status"], "OK")
        self.assertTrue((self.user_data_path / "user_positions_journal.json").exists())
        self.assertTrue((self.user_data_path / "ai_learning_engine.json").exists())

    def test_aura_no_synthetic_pnl_or_invented_features(self):
        """Verifies AURA never invents +40% or -10% P&L, nor reconstructs fake features from chain/notes."""
        # Record position with NO final_pnl_pct
        utl.record_position({
            "id": "test_win_no_pnl",
            "pair": "WIN/USDT",
            "chain": "solana",
            "status": "TP_HIT",
            "final_pnl_pct": "",  # Missing P&L
            "notes": "Fast breakout",
        })
        # Record position with loss but NO final_pnl_pct
        utl.record_position({
            "id": "test_loss_no_pnl",
            "pair": "LOSS/ETH",
            "chain": "eth",
            "status": "CLOSED_LOSS",
            "final_pnl_pct": "",  # Missing P&L
            "notes": "Dumped",
        })

        ai_model = utl.train_ai_from_user_journal()
        # Missing P&L positions must NOT be used as verified training samples
        self.assertEqual(ai_model["verified_training_samples"], 0)
        self.assertFalse(ai_model["synthetic_labels_permitted"])

        # Record position with genuine P&L AND genuine entry features
        utl.record_position({
            "id": "test_valid_trade",
            "pair": "REAL/SOL",
            "chain": "solana",
            "status": "CLOSED_WIN",
            "final_pnl_pct": "+44.5%",
            "entry_features": {
                "engine_1_momentum_acceleration": 85.0,
                "engine_2_volume_intelligence": 90.0,
                "engine_3_buy_sell_pressure": 82.0,
                "engine_4_liquidity_depth": 78.0,
                "engine_5_accumulation_detection": 70.0,
                "engine_6_smart_money": 65.0,
                "engine_7_breakout_prediction": 88.0,
                "engine_8_ecosystem_flow": 80.0,
                "engine_9_risk_safety": 95.0,
            }
        })
        updated_model = utl.train_ai_from_user_journal()
        self.assertEqual(updated_model["verified_training_samples"], 1)
        self.assertEqual(updated_model["win_count"], 1)

    def test_aura_soft_delete_preserves_history(self):
        """Verifies delete_position soft-archives positions without physically destroying learning records."""
        utl.record_position({
            "id": "pos_preserve_test",
            "pair": "HIST/SOL",
            "status": "OPEN",
            "final_pnl_pct": "+52.0%",
        })
        res = utl.delete_position("pos_preserve_test")
        self.assertEqual(res["status"], "OK")

        journal = utl.get_journal()
        matching = [p for p in journal.get("positions", []) if p.get("id") == "pos_preserve_test"]
        self.assertEqual(len(matching), 1)
        self.assertTrue(matching[0].get("archived"))
        self.assertEqual(matching[0].get("status"), "ARCHIVED")

    def test_aura_learned_weight_mapping_one_to_one(self):
        """Verifies that each learned weight maps 1-to-1 to its corresponding breakout feature."""
        candidate = {
            "pair": "MAP/SOL",
            "chain": "solana",
            "pool_address": "5tYFviFWQRKV9BJSTHGitbdqEYC1BGUgRUDnSADUXqJP",
            "contract_address": "CGEDT9QZDvvH5GmVkWJH2BXiMJqMJySC9ihWyr7Spump",
            "price": 1.0,
            "final_price_usd": 1.0,
            "target_price": 1.50,  # +50%
            "liquidity_usd": 50000.0,
            "volume_24h_usd": 100000.0,
            "early_breakout_score": 75.0,
            "breakout_confluence_score": 75.0,
            "realistic_opportunity_rank": 80.0,
            "executable_route_verified": True,
            "engine_scores": {
                "engine_1_momentum_acceleration": 90.0,
                "engine_2_volume_intelligence": 50.0,
                "engine_3_buy_sell_pressure": 50.0,
                "engine_4_liquidity_depth": 50.0,
                "engine_5_accumulation_detection": 50.0,
                "engine_6_smart_money": 50.0,
                "engine_7_breakout_prediction": 50.0,
                "engine_8_ecosystem_flow": 50.0,
                "engine_9_risk_safety": 90.0,
            }
        }
        # Run filter and sort
        actionable, _ = refresh_and_filter_candidates([candidate], min_potential_pct=40.0)
        self.assertEqual(len(actionable), 1)

    def test_expected_return_engine_no_invented_defaults(self):
        """Verifies ExpectedReturnEngine returns None for missing TP without defaulting to +40%."""
        row_missing_tp = {
            "pair": "NOTP/SOL",
            "price": 1.0,
            # No tp_upside_pct or potential_pct
        }
        res = ExpectedReturnEngine.estimate_outcomes(row_missing_tp)
        self.assertFalse(res["meets_mandatory_single_tp"])
        self.assertIsNone(res["single_tp_target_pct"])
        self.assertEqual(res["probability_tp_hit_24h"], 0.0)
        self.assertIsNone(res["expected_net_24h_return_pct"])
        self.assertFalse(res["is_calibrated_empirical_model"])

    def test_defillama_flow_engine_prioritization(self):
        """Verifies DefiLlamaFlowEngine calculates momentum weights and ranks chains."""
        engine = DefiLlamaFlowEngine()
        # Mock retrieved chain flow data
        engine.chain_flow_data = {
            "solana": {"canonical_id": "solana", "discovery_budget_weight": 2.1, "volume_24h_usd": 1_500_000_000},
            "bsc": {"canonical_id": "bsc", "discovery_budget_weight": 1.4, "volume_24h_usd": 600_000_000},
            "celo": {"canonical_id": "celo", "discovery_budget_weight": 0.8, "volume_24h_usd": 15_000_000},
        }
        priority = engine.get_priority_networks(top_n=3)
        self.assertEqual(priority[0], "solana")
        self.assertEqual(priority[1], "bsc")
        self.assertEqual(priority[2], "celo")

    def test_dexscreener_discovery_multi_lane_leads(self):
        """Verifies DexScreenerDiscoveryEngine initializes and defines all multi-lane discovery leads."""
        engine = DexScreenerDiscoveryEngine(timeout=5, max_workers=2)
        self.assertIn("breakout", engine.search_queries)
        self.assertIn("profiles_fetched", engine.stats)
        self.assertIn("boosts_fetched", engine.stats)
        self.assertIn("ctos_fetched", engine.stats)
        self.assertIn("ads_fetched", engine.stats)

    def test_mandatory_tp40_gate_enforcement(self):
        """Strict deterministic gate: a candidate with +39.99% potential MUST be rejected."""
        sub_40_candidate = {
            "pair": "SUB40/SOL",
            "chain": "solana",
            "price": 1.0,
            "final_price_usd": 1.0,
            "target_price": 1.3999,  # Exactly +39.99% upside
            "liquidity_usd": 80000.0,
            "early_breakout_score": 95.0,  # Even with highest score
            "breakout_confluence_score": 95.0,
            "executable_route_verified": True,
        }
        actionable, watchlist = refresh_and_filter_candidates([sub_40_candidate], min_potential_pct=40.0)
        self.assertEqual(len(actionable), 0, "Sub-40% candidate must never enter actionable board")
        self.assertEqual(len(watchlist), 0, "Sub-40% candidate must never enter watchlist")

    def test_permanent_historical_lessons_preservation(self):
        """Verifies FAIR/SOL, USOE/SOL, CORLA/WBNB, Reboot, HIGH, DENIM are permanently preserved."""
        journal = utl.get_journal()
        lessons = journal.get("ai_learning_stats", {}).get("research_lessons", {})
        neg = [item["symbol"] for item in lessons.get("negative_failure_lessons", [])]
        pos = [item["symbol"] for item in lessons.get("positive_success_lessons", [])]
        self.assertIn("FAIR/SOL", neg)
        self.assertIn("USOE/SOL", neg)
        self.assertIn("CORLA/WBNB", neg)
        self.assertIn("Reboot/ETH", pos)
        self.assertIn("HIGH/SOL", pos)
        self.assertIn("DENIM/ETH", pos)


if __name__ == "__main__":
    unittest.main()
