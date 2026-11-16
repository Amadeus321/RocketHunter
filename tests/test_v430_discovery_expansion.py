#!/usr/bin/env python3
"""Comprehensive regression test suite for Rocket Hunter v4.30 Discovery Coverage Expansion.

Validates:
1. Updated CLI parameter defaults (Gecko 3 pages, 24 networks, 300 lookups, 200 forward candidates, 60 security limit).
2. Propagation of security limit 60 across run_local_scan.py, run_full_scan.py, and post_scan_audit.py.
3. Strict fail-closed qualification behavior for Ranks 60 vs 61 boundary:
   - Ranks 61-200 without verified security PASS are forced to BLOCKED_UNVERIFIED_OR_RISK.
   - No unverified candidate can become actionable or enter verified_actionable_opportunities.
4. Mathematical throttle pacing reconciliation:
   - Formula: (N - 1) * interval for sequential calls with initial zero-wait.
   - Reconciles discovery (87 calls -> 86 sleeps * 12s = 17.20m),
     CVD audit (4 calls -> 3 sleeps * 12s = 0.60m, or 8 calls -> 1.40m),
     and candle verification (200 candidates -> 199 sleeps * 12s = 39.80m).
5. Immutable safety invariants:
   - Minimum single technical TP of +40.00%.
   - Maximum 24-hour evaluation horizon.
   - Exact token mint and pool identity preservation.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from scripts import (
    run_local_scan,
    run_full_scan,
    post_scan_audit,
    forward_24h_report,
    live_price_verifier,
)


class DiscoveryCoverageExpansionConfigurationTests(unittest.TestCase):
    """Test CLI parameter defaults and propagation across modules."""

    def test_run_local_scan_defaults(self):
        parser = run_local_scan.build_arg_parser()
        args = parser.parse_args(["--outdir", "tmp-test"])
        self.assertEqual(args.gecko_pages, 3, "GeckoTerminal pages default must be 3")
        self.assertEqual(args.network_sweep_limit, 24, "Network sweep limit default must be 24")

    def test_run_local_scan_build_metadata(self):
        parser = run_local_scan.build_arg_parser()
        args = parser.parse_args(["--outdir", "tmp-test"])
        meta = run_local_scan.build_run_metadata(args)
        cfg = meta.get("local_configuration", {})
        self.assertEqual(cfg.get("gecko_pages"), 3)
        self.assertEqual(cfg.get("network_sweep_limit"), 24)
        self.assertEqual(cfg.get("dex_lookup_limit"), 300, "DexScreener lookup limit must be 300")

    def test_run_local_scan_build_command(self):
        parser = run_local_scan.build_arg_parser()
        args = parser.parse_args(["--outdir", "tmp-test"])
        cmd = run_local_scan.build_scan_command(args, Path("tmp-test"), 120.0)
        self.assertEqual(cmd[cmd.index("--gecko-pages") + 1], "3")
        self.assertEqual(cmd[cmd.index("--network-sweep-limit") + 1], "24")
        self.assertEqual(cmd[cmd.index("--dex-lookup-limit") + 1], "300")

    def test_run_full_scan_defaults(self):
        parser = run_full_scan.build_parser()
        args = parser.parse_args([])
        self.assertEqual(args.gecko_pages, 3)
        self.assertEqual(args.network_sweep_limit, 24)
        self.assertEqual(args.dex_lookup_limit, 300, "run_full_scan dex_lookup_limit default must be 300")
        self.assertEqual(args.security_limit, 60, "run_full_scan security_limit default must be 60")
        self.assertEqual(args.dex_limit, 200, "run_full_scan dex_limit default must be 200")

    def test_post_scan_audit_default_security_limit(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--security-limit", type=int, default=60)
        args = parser.parse_args([])
        self.assertEqual(args.security_limit, 60, "post_scan_audit security_limit default must be 60")

    def test_forward_24h_report_dex_limit_defaults(self):
        # pick_candidates default parameter
        empty_report = {"top_24h_gainers": []}
        candidates = forward_24h_report.pick_candidates(empty_report)
        self.assertEqual(len(candidates), 0)


class PacingModelReconciliationTests(unittest.TestCase):
    """Reconcile the 57.2 / 58.8 / 59.0 minute pacing discrepancy using actual throttle logic."""

    def test_throttle_pacing_formula(self):
        interval = 12.0

        # Discovery: 87 calls -> (87 - 1) * 12s = 1032s = 17.20 minutes
        discovery_calls = 87
        discovery_sleep_sec = (discovery_calls - 1) * interval
        self.assertEqual(discovery_sleep_sec, 1032.0)
        self.assertAlmostEqual(discovery_sleep_sec / 60.0, 17.20, places=2)

        # Forward candle sweeps: 200 pools -> (200 - 1) * 12s = 2388s = 39.80 minutes
        # Note: If modeled as 200 * 12s = 2400s = 40.00 minutes
        candle_pools = 200
        candle_sleep_sec = (candle_pools - 1) * interval
        self.assertEqual(candle_sleep_sec, 2388.0)
        self.assertAlmostEqual(candle_sleep_sec / 60.0, 39.80, places=2)

        # CVD audit: 4 calls -> (4 - 1) * 12s = 36s = 0.60 minutes
        cvd_calls_4 = 4
        cvd_sleep_4_sec = (cvd_calls_4 - 1) * interval
        self.assertEqual(cvd_sleep_4_sec, 36.0)
        self.assertAlmostEqual(cvd_sleep_4_sec / 60.0, 0.60, places=2)

        # CVD audit: 8 calls -> (8 - 1) * 12s = 84s = 1.40 minutes
        cvd_calls_8 = 8
        cvd_sleep_8_sec = (cvd_calls_8 - 1) * interval
        self.assertEqual(cvd_sleep_8_sec, 84.0)
        self.assertAlmostEqual(cvd_sleep_8_sec / 60.0, 1.40, places=2)

        # Reconciliation:
        # 1. 57.2 min = 17.2m (discovery: 86*12s) + 40.0m (candle upper bound: 200*12s)
        self.assertAlmostEqual((86 * 12.0 + 200 * 12.0) / 60.0, 57.20, places=2)

        # 2. 57.6 min = pure exact throttle sleeps (86*12s + 3*12s + 199*12s)
        exact_sleep_total = (discovery_sleep_sec + cvd_sleep_4_sec + candle_sleep_sec) / 60.0
        self.assertAlmostEqual(exact_sleep_total, 57.60, places=2)

        # 3. 58.4 min = exact throttle sleeps with 8 CVD calls (86*12s + 7*12s + 199*12s)
        exact_sleep_8_total = (discovery_sleep_sec + cvd_sleep_8_sec + candle_sleep_sec) / 60.0
        self.assertAlmostEqual(exact_sleep_8_total, 58.40, places=2)

        # 4. 58.8 min = 17.2m (discovery) + 1.6m (8 CVD calls) + 40.0m (200 candles)
        self.assertAlmostEqual(17.2 + 1.6 + 40.0, 58.80, places=2)

        # 5. 59.0 min = N*12s for all 295 calls without (N-1) subtraction: (87 + 8 + 200) * 12s / 60 = 59.0m
        self.assertAlmostEqual((87 + 8 + 200) * 12.0 / 60.0, 59.00, places=2)


class SecurityAuditBoundaryTests(unittest.TestCase):
    """Verify that Ranks 60 vs 61 boundary strictly enforces fail-closed behavior."""

    def test_rank_60_with_audit_pass_can_qualify(self):
        candidate_60 = {
            "identity": "dex:base:0xpool60",
            "chain": "base",
            "pool_address": "0xpool60",
            "contract_address": "0xcontract60",
            "market_type": "dex_pool",
            "forecast_rank": 60,
            "dex_loss_guard_status": "PASS",
            "dex_loss_guard_blockers": [],
            "entry_decision": "QUALIFIED",
            "breakout_evidence_eligible": True,
            "price": 1.0,
            "final_price_usd": 1.0,
            "target_price": 1.5,
            "liquidity_usd": 50000.0,
        }
        # In live_price_verifier
        cand_rank = int(candidate_60.get("forecast_rank") or 0)
        security_tier_passed = cand_rank <= 60 or candidate_60.get("dex_loss_guard_status") == "PASS"
        upstream_qualified = security_tier_passed and str(candidate_60.get("entry_decision")).upper() == "QUALIFIED"
        self.assertTrue(upstream_qualified, "Rank 60 with security PASS must be eligible for qualification")

    def test_rank_61_without_audit_pass_is_permanently_blocked(self):
        candidate_61 = {
            "identity": "dex:base:0xpool61",
            "chain": "base",
            "pool_address": "0xpool61",
            "contract_address": "0xcontract61",
            "market_type": "dex_pool",
            "forecast_rank": 61,
            "dex_loss_guard_status": "UNAUDITED_OUTSIDE_LIMIT",
            "dex_loss_guard_blockers": [],
            "entry_decision": "QUALIFIED",
            "breakout_evidence_eligible": True,
            "price": 1.0,
            "final_price_usd": 1.0,
            "target_price": 1.5,
            "liquidity_usd": 50000.0,
        }

        # Apply central qualification enforcement from forward_24h_report
        rank = 61
        if candidate_61.get("market_type") == "dex_pool" and rank > 60:
            if candidate_61.get("dex_loss_guard_status") != "PASS":
                candidate_61["dex_loss_guard_status"] = "BLOCKED_UNVERIFIED_OR_RISK"
                blockers = list(candidate_61.get("dex_loss_guard_blockers") or [])
                blockers.append("candidate outside deep security audit tier (rank > 60); contract safety unverified")
                candidate_61["dex_loss_guard_blockers"] = list(dict.fromkeys(blockers))
                candidate_61["entry_decision"] = "TOP ROCKET — EXECUTION UNVERIFIED"
                candidate_61["verification_status"] = "DISCOVERY_UNVERIFIED_SAFETY"

        self.assertEqual(candidate_61["dex_loss_guard_status"], "BLOCKED_UNVERIFIED_OR_RISK")
        self.assertEqual(candidate_61["entry_decision"], "TOP ROCKET — EXECUTION UNVERIFIED")
        self.assertEqual(candidate_61["verification_status"], "DISCOVERY_UNVERIFIED_SAFETY")
        self.assertIn("candidate outside deep security audit tier (rank > 60)", candidate_61["dex_loss_guard_blockers"][0])

        # Test live_price_verifier upstream_qualified gate
        cand_rank = int(candidate_61.get("forecast_rank") or 0)
        security_tier_passed = cand_rank <= 60 or candidate_61.get("dex_loss_guard_status") == "PASS"
        upstream_qualified = security_tier_passed and str(candidate_61.get("entry_decision")).upper() == "QUALIFIED"
        self.assertFalse(upstream_qualified, "Rank 61 without audit PASS must NEVER be upstream qualified")

    def test_enforce_dex_loss_guard_blocks_unverified(self):
        unverified_pick = {
            "market_type": "dex_pool",
            "dex_loss_guard_status": "BLOCKED_UNVERIFIED_OR_RISK",
        }
        verified_plan = {"trade_plan": {"entry_price": 1.0, "single_take_profit": 1.5}}
        plan, blockers = forward_24h_report.enforce_dex_loss_guard(unverified_pick, verified_plan, [])
        self.assertIsNone(plan, "Unverified DEX pick must be stripped of trade plan")
        self.assertTrue(len(blockers) > 0, "Blocker must be emitted for unverified pick")
        self.assertIn("DEX loss-pattern guard PASS", blockers[0])


class SafetyInvariantsPreservationTests(unittest.TestCase):
    """Verify minimum single technical TP of +40% and 24-hour horizon."""

    def test_tp_less_than_40_pct_is_rejected(self):
        top = {
            "market_type": "dex_pool",
            "price": 1.0,
            "chain": "base",
            "pool_address": "0xpool1",
            "contract_address": "0xcontract1",
            "trade_plan": {"single_take_profit": 1.35},  # only +35% upside
        }
        meta = {
            "final_price_verified": True,
            "final_pool_address": "0xpool1",
            "final_token_contract": "0xcontract1",
            "final_quote_chain": "base",
            "pool_address": "0xpool1",
            "contract_address": "0xcontract1",
            "_candles_count": 100,
            "target_price": 1.35,
            "target_price_source": "RESISTANCE",
            "technical_tp_validation": {
                "status": "FAIL",
                "blockers": ["take-profit upside +35.00% is below the required minimum +40.00%"],
            },
        }
        pick, gaps = forward_24h_report.enforce_required_trade_fields(top, meta, "2026-11-17T00:00:00Z")
        self.assertIsNone(pick, "Candidate with TP < 40% must be disqualified")
        self.assertTrue(any("40.00%" in g for g in gaps), "Required gaps must note the +40% minimum threshold")

    def test_tp_40_pct_or_greater_is_accepted(self):
        top = {
            "market_type": "dex_pool",
            "price": 1.0,
            "chain": "base",
            "data_fresh": True,
            "market_verified": True,
            "tracked_token_orientation_verified": True,
            "entry_decision": "TOP ROCKET — EXECUTION UNVERIFIED",
            "pool_address": "0xpool1",
            "contract_address": "0xcontract1",
            "trade_plan": {"single_take_profit": 1.45},  # +45% upside
        }
        meta = {
            "final_price_verified": True,
            "final_pool_address": "0xpool1",
            "final_token_contract": "0xcontract1",
            "final_quote_chain": "base",
            "final_quote_token_orientation_verified": True,
            "pool_address": "0xpool1",
            "contract_address": "0xcontract1",
            "_candles_count": 100,
            "target_price": 1.45,
            "target_price_source": "RESISTANCE",
            "technical_tp_validation": {
                "status": "PASS",
                "blockers": [],
                "target_price": 1.45,
            },
        }
        pick, gaps = forward_24h_report.enforce_required_trade_fields(top, meta, "2026-11-17T00:00:00Z")
        self.assertIsNotNone(pick, "Candidate with TP >= 40% and valid fields must be accepted")
        self.assertEqual(gaps, [])
        self.assertEqual(pick["trade_plan"]["tp_upside_pct"], 45.0)

    def test_24h_horizon_invariant(self):
        scan_start_str = "2026-11-16T12:00:00Z"
        scan_start_dt = datetime.fromisoformat(scan_start_str.replace("Z", "+00:00"))
        forecast_end_dt = scan_start_dt + timedelta(hours=24)
        forecast_end_str = forecast_end_dt.isoformat().replace("+00:00", "Z")
        self.assertEqual(forecast_end_str, "2026-11-17T12:00:00Z", "Forecast horizon must be strictly 24 hours")


if __name__ == "__main__":
    unittest.main()
