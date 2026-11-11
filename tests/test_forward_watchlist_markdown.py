import unittest

from scripts.render_forward_watchlist_markdown import render_report, render_html


class ForwardWatchlistMarkdownTests(unittest.TestCase):
    def test_renders_source_backed_pair_tp_and_final_quality_gate(self):
        document = {
            "age_guard_summary": {"run_id": "test-1", "eligible_forward_candidates": 1, "raw_candidate_count": 3},
            "as_of_utc": "2026-11-05T22:35:53Z",
            "forecast_horizon_hours": 24,
            "status": "REFERENCE_PLAN",
            "candidates": [{
                "pair": "Snoopy/SOL",
                "price": 0.000072,
                "projected_24h_upside_pct": 45.0,
                "upside_projection_method": "1H_ATR_RANGE_MOMENTUM_SCENARIO_NOT_CALIBRATED",
                "target_price": 0.000078139889,
                "target_price_source": "7D_SWING_RESISTANCE",
                "trade_plan": {
                    "entry_price": 0.000072,
                    "single_take_profit": 0.000078139889,
                    "target_method": "7D_SWING_RESISTANCE",
                },
                "quality_gate": {
                    "status": "NOT_SHORTLISTED",
                    "execution_ready": False,
                    "reasons": ["DEX liquidity below $100k"],
                },
                "entry_decision": "TOP ROCKET - EXECUTION UNVERIFIED",
                "age_qualified_24h_forecast": True,
                "pair_address": "pool-address",
                "pair_url": "https://example.invalid/pool",
                "calibrated_prob_24h": 0.25,
                "score_is_probability": False,
                "calibration_observations_total": 0,
            }],
        }

        report = render_report(document)

        self.assertIn("Snoopy/SOL TP: +8.53%", report)
        self.assertIn("24h upside scenario:** +45.00%", report)
        self.assertIn("+45.00%", report)
        self.assertIn("NOT_SHORTLISTED", report)
        self.assertIn("Execution ready", report)
        self.assertIn("false", report)
        self.assertIn("Calibrated 24h probability:** unavailable", report)
        self.assertNotIn("HIGH/SOL", report)


    def test_numeric_strings_are_treated_as_finite_numbers_for_rendering(self):
        document = {
            "status": "NEXT_24H_OPPORTUNITY_RANKED_EXECUTION_UNVERIFIED",
            "candidates": [{
                "pair": "ALPHA/USDC",
                "price": "1.0",
                "target_price": "1.4",
                "target_price_source": "7D_SWING_RESISTANCE",
                "trade_plan": {
                    "entry_price": "1.0",
                    "single_take_profit": "1.4",
                    "target_method": "7D_SWING_RESISTANCE",
                },
                "quality_gate": {"status": "NOT_SHORTLISTED", "execution_ready": False},
            }],
        }

        report = render_report(document)

        self.assertIn("ALPHA/USDC TP: +40.00%", report)
        self.assertIn("Single TP:** `1.4`", report)

    def test_renders_market_regime_downside_reward_risk_and_hyperliquid_context(self):
        document = {
            "status": "NEXT_24H_OPPORTUNITY_RANKED_EXECUTION_UNVERIFIED",
            "as_of_utc": "2026-11-07T15:00:00Z",
            "market_context": {
                "label": "RISK_ON", "breadth_sample_count": 30,
                "alt_breadth_pct": 70.0,
            },
            "hyperliquid_derivatives_context": {
                "status": "OK", "matched_candidates": 3, "markets_received": 100,
            },
            "forward_opportunity_ranking": {"candidate_count": 1},
            "candidates": [{
                "pair": "ALPHA/SOL",
                "forward_opportunity_rank": 1,
                "forward_opportunity_score": 88.0,
                "projected_24h_upside_pct": 80.0,
                "initial_stop_price": 0.0009,
                "stop_loss_pct": 10.0,
                "stop_method": "1.8X_1H_ATR",
                "atr_1h_pct": 5.5,
                "scenario_reward_risk_ratio": 8.0,
                "early_breakout_score": 92.0,
                "price": 0.001,
                "target_price": 0.0012,
                "target_price_source": "7D_SWING_RESISTANCE",
                "trade_plan": {
                    "entry_price": 0.001,
                    "single_take_profit": 0.0012,
                    "target_method": "7D_SWING_RESISTANCE",
                },
                "hyperliquid_context_available": True,
                "hyperliquid_match_status": "SYMBOL_AND_PRICE_CROSSCHECK_WITHIN_10PCT",
                "hyperliquid_mark_price_usd": 0.00101,
                "hyperliquid_funding_hourly_pct": 0.001,
                "oi_change_4h_pct": 4.5,
                "quality_gate": {"status": "NOT_SHORTLISTED", "execution_ready": False},
            }],
        }

        report = render_report(document)

        self.assertIn("Market regime:** RISK_ON", report)
        self.assertIn("Hyperliquid derivatives cross-check:** OK", report)
        self.assertIn("Initial downside", report)
        self.assertIn("8.00x", report)
        self.assertIn("Initial protective stop", report)
        self.assertIn("Hyperliquid perp cross-check", report)
        self.assertIn("open-interest change (4h-normalized):** +4.50%", report)

    def test_position_monitor_renders_trailing_stop_and_market_type(self):
        document = {
            "status": "WATCHING",
            "candidates": [],
            "position_monitor": {
                "status": "OK", "provider": "CEX / DEX hourly candles",
                "ohlcv_resolution": "1h", "max_open_reference_positions": 12,
                "active_positions": [{
                    "asset": "Alpha", "pair": "ALPHA/SOL",
                    "market_type": "dex_pool", "chain": "solana",
                    "time_stop_utc": "2026-11-08T10:00:00Z",
                    "max_favorable_excursion_pct": 12.0,
                    "latest_return_from_entry_pct": 8.0,
                    "initial_stop_price": 0.0009,
                    "trailing_stop_price": 0.00108,
                    "position_status": "TP REACHED — TRAILING RUNNER OPEN",
                }],
            },
        }

        report = render_report(document)

        self.assertIn("Protective stop", report)
        self.assertIn("trail: 0.00108", report)
        self.assertIn("TP REACHED — TRAILING RUNNER OPEN", report)

    def test_does_not_invent_target_when_target_method_is_unverified(self):
        document = {
            "status": "NO_VERIFIED_TARGET",
            "candidates": [{
                "pair": "Example/ETH",
                "price": 1.0,
                "target_price": 1.5,
                "target_price_source": "UNVERIFIED",
                "quality_gate": {"status": "NOT_SHORTLISTED", "execution_ready": False},
            }],
        }

        report = render_report(document)

        self.assertIn("Example/ETH - TP forming / unavailable", report)
        self.assertIn("Single TP:** not source-verified", report)
        self.assertNotIn("Example/ETH TP: +50.00%", report)


    def test_renders_top_25_and_reports_full_evaluated_count(self):
        document = {
            "status": "NEXT_24H_OPPORTUNITY_RANKED_EXECUTION_UNVERIFIED",
            "forward_opportunity_ranking": {"candidate_count": 30},
            "candidates": [
                {"pair": "PAIR%s/USDC" % i, "market_type": "dex_pool", "age_hours": 24.0,
                 "forward_opportunity_rank": i + 1,
                 "forward_opportunity_score": 90 - i}
                for i in range(30)
            ],
        }

        report = render_report(document)

        self.assertIn("top 25 of 30 mature/unverified research candidates", report)
        self.assertIn("### 25. PAIR24/USDC", report)
        self.assertNotIn("### 26. PAIR25/USDC", report)
        self.assertIn("ranked next-24h opportunities", report.lower())

    def test_empty_candidate_array_keeps_feed_gap_distinct_from_market_opportunity(self):
        report = render_report({"status": "NO_FEED", "candidates": []})

        self.assertIn("No detailed forward candidate array", report)
        self.assertIn("empty execution shortlist is not evidence", report)

    def test_incomplete_scan_cannot_render_actionable_rows(self):
        report = render_report({
            "scan_status": "IN_PROGRESS", "verified_actionable_opportunities": [{
                "pair": "Secret/USDC", "chain": "ethereum", "final_price_usd": 1,
                "final_pool_address": "0xpool", "final_token_contract": "0xtoken",
                "tp_upside_pct": 50, "trade_plan": {"single_take_profit": 1.5},
            }], "candidates": [],
        })
        self.assertIn("SCAN NOT COMPLETED", report)
        self.assertNotIn("Secret/USDC", report)

    def test_completed_scan_reports_zero_instead_of_filling_actionable_lane(self):
        report = render_report({
            "scan_status": "COMPLETED", "verified_actionable_opportunities": [],
            "actionable_gate": {"status": "ZERO_VERIFIED_OPPORTUNITIES"}, "candidates": [],
        })
        self.assertIn("ZERO VERIFIED OPPORTUNITIES", report)

    def test_general_version_and_clickable_github_completion_details(self):
        document = {
            "general_version": "v4.26.1",
            "scan_finished_utc": "2026-11-10T00:30:00Z",
            "github_actions_run_id": "12345678",
            "github_actions_run_url": "https://github.com/org/repo/actions/runs/12345678",
            "scan_status": "COMPLETED",
            "verified_actionable_opportunities": [{
                "pair": "ROCKET/SOL", "chain": "solana", "final_price_usd": 10.0,
                "final_pool_address": "0xpool123", "final_token_contract": "0xtoken456",
                "tp_upside_pct": 45.0, "trade_plan": {"single_take_profit": 14.5},
                "verification_status": "PASS", "safety_status": "PASS", "execution_status": "PASS",
            }],
            "candidates": [],
        }
        report = render_report(document)
        # General version only (no component version table)
        self.assertIn("- **General version:** `v4.26.1`", report)
        self.assertNotIn("Component version", report)
        # Scan finished Israel Time
        self.assertIn("- **Scan finished (Israel Time):** `2026-11-10 02:30:00 IST`", report)
        self.assertNotIn("- **As of (UTC):**", report)
        # GitHub Actions run ID
        self.assertIn("- **GitHub Actions run ID:** `12345678`", report)
        # Clickable run URL
        self.assertIn("- **GitHub Actions run URL:** [https://github.com/org/repo/actions/runs/12345678](https://github.com/org/repo/actions/runs/12345678)", report)
        # Complete opportunity data in table
        self.assertIn("ROCKET/SOL", report)
        self.assertIn("solana", report)
        self.assertIn("10", report)
        self.assertIn("14.5", report)
        self.assertIn("+45.00%", report)
        self.assertIn("`0xpool123`", report)
        self.assertIn("`0xtoken456`", report)

    def test_render_html_with_actionable_and_zero_opportunities(self):
        # 1. Actionable candidate present in completed scan
        doc_with_actionable = {
            "general_version": "v4.26.1",
            "scan_finished_utc": "2026-11-10T11:00:00Z",
            "github_actions_run_id": "999888",
            "github_actions_run_url": "https://github.com/org/repo/actions/runs/999888",
            "scan_status": "COMPLETED",
            "verified_actionable_opportunities": [{
                "pair": "SOLAR/SOL", "chain": "solana", "final_price_usd": 1.25,
                "final_pool_address": "pool-xyz", "final_token_contract": "token-xyz",
                "tp_upside_pct": 52.0, "trade_plan": {"single_take_profit": 1.90},
                "verification_status": "PASS", "safety_status": "PASS", "execution_status": "PASS",
            }],
            "candidates": [],
        }
        html_out = render_html(doc_with_actionable)
        self.assertIn("<!doctype html>", html_out)
        self.assertIn("v4.26.1", html_out)
        self.assertIn("2026-11-10 13:00:00 IST", html_out)
        self.assertIn("SOLAR/SOL", html_out)
        self.assertIn("solana", html_out)
        self.assertIn("$1.25", html_out)
        self.assertIn("$1.9", html_out)
        self.assertIn("+52.00%", html_out)
        self.assertIn("pool-xyz", html_out)
        self.assertIn("token-xyz", html_out)

        # 2. Zero verified opportunities
        doc_zero = {
            "general_version": "v4.26.1",
            "scan_finished_utc": "2026-11-10T11:00:00Z",
            "scan_status": "COMPLETED",
            "verified_actionable_opportunities": [],
            "candidates": [
                {
                    "pair": "UNVER/SOL", "chain": "solana", "forward_opportunity_rank": 1,
                    "forward_opportunity_score": 50.0, "early_breakout_score": 60.0,
                    "projected_24h_upside_pct": 55.0, "stop_loss_pct": 12.0,
                    "scenario_reward_risk_ratio": 4.5, "price": 0.5,
                    "target_price": 0.75, "target_price_source": "7D_SWING_RESISTANCE",
                    "pool_address": "pool-unver", "contract_address": "token-unver",
                    "trade_plan": {"entry_price": 0.5, "single_take_profit": 0.75, "target_method": "7D_SWING_RESISTANCE"},
                },
                {
                    "pair": "SUB40/SOL", "chain": "solana", "forward_opportunity_rank": 2,
                    "forward_opportunity_score": 45.0, "early_breakout_score": 50.0,
                    "price": 1.0, "target_price": 1.20, "target_price_source": "7D_SWING_RESISTANCE",
                    "pool_address": "pool-sub40", "contract_address": "token-sub40",
                    "trade_plan": {"entry_price": 1.0, "single_take_profit": 1.20, "target_method": "7D_SWING_RESISTANCE"},
                },
            ],
        }
        html_zero = render_html(doc_zero)
        self.assertIn("ZERO VERIFIED OPPORTUNITIES", html_zero)
        self.assertIn("FAIL-CLOSED ENFORCED", html_zero)
        # UNVER/SOL has +50.0% upside (>= +40%) so it qualifies for display
        self.assertIn("UNVER/SOL", html_zero)
        self.assertIn("+50.00%", html_zero)
        self.assertIn("pool-unver", html_zero)
        self.assertIn("token-unver", html_zero)
        self.assertIn("NOT QUALIFIED", html_zero)
        # SUB40/SOL has +20.0% upside (< +40%) so it must be strictly excluded
        self.assertNotIn("SUB40/SOL", html_zero)

    def test_strict_40_percent_exclusion_and_complete_opportunity_columns(self):
        doc = {
            "general_version": "v4.26.1",
            "scan_finished_utc": "2026-11-10T12:00:00Z",
            "scan_status": "COMPLETED",
            "unverified_research_watchlist": [
                {
                    "pair": "HIGH/SOL", "chain": "solana", "price": 10.0, "target_price": 15.0,
                    "pool_address": "pool-high", "contract_address": "token-high",
                    "tp_upside_pct": 50.0,
                    "trade_plan": {"entry_price": 10.0, "single_take_profit": 15.0},
                },
                {
                    "pair": "LOW/SOL", "chain": "solana", "price": 10.0, "target_price": 12.0,
                    "pool_address": "pool-low", "contract_address": "token-low",
                    "tp_upside_pct": 20.0,
                    "trade_plan": {"entry_price": 10.0, "single_take_profit": 12.0},
                },
            ],
            "young_discovery_only_leads": [
                {
                    "pair": "YOUNG_HIGH/SOL", "chain": "solana", "price": 0.01, "target_price": 0.02,
                    "pool_address": "pool-y-high", "contract_address": "token-y-high",
                    "tp_upside_pct": 100.0, "age_hours": 2.5,
                    "trade_plan": {"entry_price": 0.01, "single_take_profit": 0.02},
                },
                {
                    "pair": "YOUNG_LOW/SOL", "chain": "solana", "price": 0.01, "target_price": 0.011,
                    "pool_address": "pool-y-low", "contract_address": "token-y-low",
                    "tp_upside_pct": 10.0, "age_hours": 1.0,
                    "trade_plan": {"entry_price": 0.01, "single_take_profit": 0.011},
                },
            ],
        }

        # Check Markdown report
        md = render_report(doc)
        # Unified table header contains complete opportunity columns including Pool Age and Status
        self.assertIn("| Pair | Chain | Current Price | Single TP | Potential (% till TP) | Pool Address | Token Contract | Pool Age | Status |", md)
        self.assertNotIn("Young discovery-only leads", md)
        # Qualifying assets (>= +40%) are included with complete data
        self.assertIn("HIGH/SOL", md)
        self.assertIn("+50.00%", md)
        self.assertIn("`pool-high`", md)
        self.assertIn("`token-high`", md)
        self.assertIn("YOUNG_HIGH/SOL", md)
        self.assertIn("+100.00%", md)
        self.assertIn("`pool-y-high`", md)
        self.assertIn("`token-y-high`", md)
        # Sub-40% assets (< +40%) are strictly excluded
        self.assertNotIn("LOW/SOL", md)
        self.assertNotIn("YOUNG_LOW/SOL", md)

        # Check HTML report
        h = render_html(doc)
        self.assertIn("<th>Pair</th><th>Chain</th><th>Current Price</th><th>Single TP</th><th>Potential (% till TP)</th><th>Pool Address</th><th>Token Contract</th><th>Pool Age</th><th>Status</th>", h)
        self.assertNotIn("Young / Unknown-Age DEX Discovery Leads", h)
        self.assertIn("HIGH/SOL", h)
        self.assertIn("+50.00%", h)
        self.assertIn("pool-high", h)
        self.assertIn("token-high", h)
        self.assertIn("YOUNG_HIGH/SOL", h)
        self.assertIn("+100.00%", h)
        self.assertNotIn("LOW/SOL", h)
        self.assertNotIn("YOUNG_LOW/SOL", h)


if __name__ == "__main__":
    unittest.main()
