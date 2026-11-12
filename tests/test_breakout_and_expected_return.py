#!/usr/bin/env python3
"""Tests for 9-Engine Imminent Breakout Detection and 24h Probabilistic Expected Return Engine."""
from __future__ import annotations

import unittest
from scripts.breakout_signal_engines import BreakoutSignalEngines
from scripts.expected_return_engine import ExpectedReturnEngine, MANDATORY_MIN_TP_PCT


class TestBreakoutSignalEngines(unittest.TestCase):
    """Test the 9 independent signal engines."""

    def test_engine_1_momentum_acceleration(self):
        row = {"change_5m_pct": 8.0, "change_1h_pct": 20.0, "change_6h_pct": 30.0}
        score, details = BreakoutSignalEngines.engine_1_momentum_acceleration(row)
        self.assertGreater(score, 0.0)
        self.assertLessEqual(score, 100.0)
        self.assertIn("acceleration_delta", details)
        self.assertGreater(details["v_short_annualized"], details["v_1h"])

    def test_engine_2_volume_intelligence(self):
        # 5m volume annualized vs 1h volume
        row = {
            "volume_5m": 25_000.0,
            "volume_1h": 50_000.0,  # 25k * 12 / 50k = 6.0 burst ratio
            "volume_24h_usd": 300_000.0,
            "liquidity_usd": 100_000.0,
        }
        score, details = BreakoutSignalEngines.engine_2_volume_intelligence(row)
        self.assertGreaterEqual(score, 70.0)
        self.assertGreaterEqual(details["burst_ratio_5m_1h"], 3.0)

    def test_engine_3_buy_sell_pressure(self):
        row = {
            "dexpaprika_1h_buy_usd": 80_000.0,
            "dexpaprika_1h_sell_usd": 20_000.0,
            "buys_24h": 400,
            "sells_24h": 100,
        }
        score, details = BreakoutSignalEngines.engine_3_buy_sell_pressure(row)
        self.assertGreaterEqual(score, 75.0)
        self.assertGreater(details["taker_buy_share_pct"], 70.0)

    def test_engine_4_liquidity_depth(self):
        deep_pool = {"liquidity_usd": 500_000.0, "volume_24h_usd": 200_000.0}
        score_deep, _ = BreakoutSignalEngines.engine_4_liquidity_depth(deep_pool)

        thin_pool = {"liquidity_usd": 4_000.0, "volume_24h_usd": 200_000.0}
        score_thin, _ = BreakoutSignalEngines.engine_4_liquidity_depth(thin_pool)

        self.assertGreater(score_deep, score_thin)

    def test_engine_5_accumulation_detection(self):
        # Tight consolidation with healthy volume
        row = {
            "change_1h_pct": 3.0,
            "change_6h_pct": 5.0,
            "change_24h_pct": 10.0,
            "volume_24h_usd": 150_000.0,
            "liquidity_usd": 80_000.0,
        }
        score, details = BreakoutSignalEngines.engine_5_accumulation_detection(row)
        self.assertGreaterEqual(score, 50.0)
        self.assertTrue(details["is_tight_coil"])

    def test_engine_6_smart_money_research(self):
        row = {"txns_24h": 1800, "promo_flags": ["community_takeover"]}
        score, details = BreakoutSignalEngines.engine_6_smart_money_research(row)
        self.assertGreater(score, 50.0)
        self.assertEqual(details["estimated_transactions"], 1800)

    def test_engine_7_breakout_prediction(self):
        row = {"potential_pct": 50.0, "change_1h_pct": 12.0, "change_5m_pct": 2.0}
        score, details = BreakoutSignalEngines.engine_7_breakout_prediction(row)
        self.assertGreaterEqual(score, 70.0)
        self.assertEqual(details["target_upside_pct"], 50.0)

    def test_engine_8_ecosystem_flow(self):
        sol_row = {"chain": "solana", "ecosystem_flow_score": 1.4}
        score, details = BreakoutSignalEngines.engine_8_ecosystem_flow(sol_row)
        self.assertGreaterEqual(score, 60.0)
        self.assertEqual(details["chain"], "solana")

    def test_engine_9_risk_intelligence(self):
        safe_row = {
            "liquidity_usd": 150_000.0,
            "age_hours": 18.0,
            "change_24h_pct": 35.0,
            "volume_24h_usd": 200_000.0,
        }
        score_safe, risks_safe = BreakoutSignalEngines.engine_9_risk_intelligence(safe_row)
        self.assertEqual(len(risks_safe), 0)
        self.assertGreaterEqual(score_safe, 90.0)

        risky_row = {
            "liquidity_usd": 3_000.0,
            "age_hours": 0.5,
            "change_24h_pct": 250.0,
            "volume_24h_usd": 200_000.0,
        }
        score_risky, risks_risky = BreakoutSignalEngines.engine_9_risk_intelligence(risky_row)
        self.assertGreater(len(risks_risky), 0)
        self.assertLess(score_risky, 50.0)

    def test_evaluate_all_engines_confluence(self):
        row = {
            "chain": "base",
            "change_5m_pct": 6.0,
            "change_1h_pct": 18.0,
            "change_6h_pct": 25.0,
            "change_24h_pct": 40.0,
            "volume_5m": 20_000.0,
            "volume_1h": 40_000.0,
            "volume_24h_usd": 250_000.0,
            "liquidity_usd": 120_000.0,
            "dexpaprika_1h_buy_usd": 70_000.0,
            "dexpaprika_1h_sell_usd": 30_000.0,
            "age_hours": 12.0,
        }
        res = BreakoutSignalEngines.evaluate_all_engines(row)
        self.assertIn("breakout_confluence_score", res)
        self.assertIn("engine_scores", res)
        self.assertEqual(len(res["engine_scores"]), 9)
        self.assertGreaterEqual(res["breakout_confluence_score"], 60.0)


class TestExpectedReturnEngine(unittest.TestCase):
    """Test 24h Probabilistic Expected Return, MFE/MAE, and Realistic Opportunity Ranking."""

    def test_mandatory_min_tp_enforcement(self):
        below_min = {"potential_pct": 32.5}
        res = ExpectedReturnEngine.estimate_outcomes(below_min)
        self.assertFalse(res["meets_mandatory_40pct_tp"])
        self.assertEqual(MANDATORY_MIN_TP_PCT, 40.00)

        above_min = {"potential_pct": 55.0}
        res = ExpectedReturnEngine.estimate_outcomes(above_min)
        self.assertTrue(res["meets_mandatory_40pct_tp"])

    def test_expected_return_and_excursion_projections(self):
        row = {
            "potential_pct": 60.0,
            "change_1h_pct": 15.0,
            "change_5m_pct": 3.0,
            "change_24h_pct": 30.0,
            "liquidity_usd": 85_000.0,
            "volume_24h_usd": 180_000.0,
        }
        confluence = {"breakout_confluence_score": 75.0}
        outcomes = ExpectedReturnEngine.estimate_outcomes(row, confluence)

        self.assertIn("probability_tp_hit_24h_pct", outcomes)
        self.assertGreater(outcomes["probability_tp_hit_24h_pct"], 50.0)
        self.assertIn("expected_net_24h_return_pct", outcomes)
        self.assertGreater(outcomes["expected_net_24h_return_pct"], 0.0)
        self.assertIn("potential_mfe_24h_pct", outcomes)
        self.assertGreaterEqual(outcomes["potential_mfe_24h_pct"], 60.0)
        self.assertIn("potential_mae_24h_pct", outcomes)
        self.assertLess(outcomes["potential_mae_24h_pct"], 0.0)
        self.assertIn("estimated_hours_to_target", outcomes)
        self.assertGreaterEqual(outcomes["estimated_hours_to_target"], 1.0)
        self.assertLessEqual(outcomes["estimated_hours_to_target"], 24.0)

    def test_realistic_opportunity_ranking_prefers_high_win_odds_over_hyped_tp(self):
        # Asset A: High theoretical TP (120%), but poor confluence (30) and high reversal risk
        asset_a = {
            "potential_pct": 120.0,
            "change_1h_pct": 75.0,  # Blow-off top
            "change_24h_pct": 250.0,
            "liquidity_usd": 12_000.0,  # Thin liquidity
        }
        confluence_a = {"breakout_confluence_score": 35.0}
        outcomes_a = ExpectedReturnEngine.estimate_outcomes(asset_a, confluence_a)

        # Asset B: Moderate TP (50%), strong confluence (80), healthy volume & liquidity
        asset_b = {
            "potential_pct": 50.0,
            "change_1h_pct": 12.0,
            "change_24h_pct": 25.0,
            "liquidity_usd": 150_000.0,
        }
        confluence_b = {"breakout_confluence_score": 82.0}
        outcomes_b = ExpectedReturnEngine.estimate_outcomes(asset_b, confluence_b)

        # Probability for Asset B should be significantly higher
        self.assertGreater(outcomes_b["probability_tp_hit_24h_pct"], outcomes_a["probability_tp_hit_24h_pct"])
        # Expected net return and realistic opportunity rank should favor Asset B!
        self.assertGreater(outcomes_b["realistic_opportunity_rank"], outcomes_a["realistic_opportunity_rank"])


if __name__ == "__main__":
    unittest.main()
