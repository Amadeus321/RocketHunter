from datetime import datetime, timezone
import pytest
from unittest.mock import patch

from scripts import v425_early_breakout as v425
from scripts.breakout_signal_engines import BreakoutSignalEngines
from scripts.expected_return_engine import ExpectedReturnEngine


class TestEngineDiagnostics:
    def test_signal_engine_software_failure_is_explicitly_captured(self):
        row = {
            "change_5m_pct": 5.0,
            "change_1h_pct": 20.0,
            "change_6h_pct": 30.0,
            "change_24h_pct": 40.0,
            "liquidity_usd": 150_000,
            "volume_24h_usd": 100_000,
            "txns_24h": 500,
        }

        with patch.object(BreakoutSignalEngines, "evaluate_all_engines", side_effect=ZeroDivisionError("simulated math error")):
            result = v425.score_candidate(row)

            assert result["signal_engine_executed"] is False
            assert result["engine_failure_module"] == "breakout_signal_engines"
            assert result["engine_exception_category"] == "ZeroDivisionError"
            assert result["engine_result_status"] == "SOFTWARE_FAILURE"
            assert "BreakoutSignalEngines failed: ZeroDivisionError" in result["engine_failure_diagnostic"]
            assert "SOFTWARE_ERROR_SIGNAL_ENGINES_INCOMPLETE" in result["risk_flags"]
            assert "UNVERIFIED_DUE_TO_ENGINE_FAILURE" in result["risk_flags"]
            assert result["breakout_evidence_eligible"] is False
            assert result["evidence_coverage_pct"] == 0.0

    def test_expected_return_software_failure_is_explicitly_captured(self):
        row = {
            "change_5m_pct": 5.0,
            "change_1h_pct": 20.0,
            "change_6h_pct": 30.0,
            "change_24h_pct": 40.0,
            "liquidity_usd": 150_000,
            "volume_24h_usd": 100_000,
            "txns_24h": 500,
        }

        with patch.object(ExpectedReturnEngine, "estimate_outcomes", side_effect=TypeError("simulated type error")):
            result = v425.score_candidate(row)

            assert result["signal_engine_executed"] is True
            assert result["expected_return_engine_executed"] is False
            assert result["engine_failure_module"] == "expected_return_engine"
            assert result["engine_exception_category"] == "TypeError"
            assert result["expected_return_status"] == "SOFTWARE_FAILURE"
            assert "ExpectedReturnEngine failed: TypeError" in result["engine_failure_diagnostic"]
            assert "SOFTWARE_ERROR_SIGNAL_ENGINES_INCOMPLETE" in result["risk_flags"]

    def test_missing_data_distinguished_from_software_failure(self):
        # A row with missing momentum and volume data
        row_missing = {
            "change_5m_pct": None,
            "change_1h_pct": None,
            "change_24h_pct": 10.0,
            "liquidity_usd": 50_000,
        }
        result = v425.score_candidate(row_missing)

        assert result["signal_engine_executed"] is True
        assert result["expected_return_engine_executed"] is True
        assert result["engine_failure_module"] is None
        assert result["engine_exception_category"] is None
        assert result["engine_failure_diagnostic"] is None
        assert result["engine_result_status"] == "DATA_UNAVAILABLE"
        assert result["breakout_evidence_eligible"] is False
        assert "SOFTWARE_ERROR_SIGNAL_ENGINES_INCOMPLETE" not in result["risk_flags"]

    def test_candidate_with_software_failure_fails_closed_at_final_gate(self):
        now = datetime.now(timezone.utc)
        # Create a candidate that would otherwise pass all gates
        passing_candidate = {
            "market_type": "dex_pool",
            "chain": "ethereum",
            "pool_address": "0x1111111111111111111111111111111111111111",
            "contract_address": "0x2222222222222222222222222222222222222222",
            "final_pool_address": "0x1111111111111111111111111111111111111111",
            "final_token_contract": "0x2222222222222222222222222222222222222222",
            "final_quote_chain": "ethereum",
            "final_quote_token_orientation_verified": True,
            "final_price_usd": 1.0,
            "final_price_timestamp_utc": now.isoformat(),
            "final_price_verified": True,
            "target_price": 1.5,
            "target_price_source": "7D_SWING_RESISTANCE",
            "technical_tp_validation": {
                "status": "PASS",
                "chain": "ethereum",
                "pool_address": "0x1111111111111111111111111111111111111111",
                "token_address": "0x2222222222222222222222222222222222222222",
                "token_orientation_verified": True,
                "target_price": 1.5,
                "target_price_source": "7D_SWING_RESISTANCE",
                "current_price": 1.0,
                "atr_1h_pct": 10.0,
                "maximum_tp_distance_pct": 60.0,
                "candle_retrieved_utc": now.isoformat(),
            },
            "trade_plan": {"single_take_profit": 1.5},
            "dex_loss_guard_status": "PASS",
            "dex_loss_guard_evidence": {
                "status": "PASS",
                "checks": {
                    "exact_token_and_pool_identity": True,
                    "launch_wallet_analysis": True,
                    "lp_position_audit": True,
                    "token_security_audit": True,
                    "two_way_route_quotes": True,
                },
            },
            "safety_verified": True,
            "executable_route_verified": True,
            "dex_execution_quotes": {
                "provider": "uniswap_v3",
                "timestamp_utc": now.isoformat(),
                "buy_quote": {"amount_out": 100},
                "sell_quote": {"amount_out": 95},
            },
        }

        # 1. Verify candidate passes when software execution succeeds
        candidate_ok = dict(passing_candidate)
        candidate_ok["signal_engine_executed"] = True
        candidate_ok["expected_return_engine_executed"] = True
        candidate_ok["engine_result_status"] = "SUCCESS"
        qualified = v425.qualify_final_actionable_candidates([candidate_ok], now)
        assert len(qualified) == 1

        # 2. Verify candidate fails closed when signal engine had software failure
        candidate_failed = dict(passing_candidate)
        candidate_failed["signal_engine_executed"] = False
        candidate_failed["engine_result_status"] = "SOFTWARE_FAILURE"
        candidate_failed["early_breakout_risk_flags"] = ["SOFTWARE_ERROR_SIGNAL_ENGINES_INCOMPLETE"]
        qualified_failed = v425.qualify_final_actionable_candidates([candidate_failed], now)
        assert len(qualified_failed) == 0
        assert "signal engine software execution failed or threw an exception" in candidate_failed["qualification_blockers"]

    def test_missing_data_integrity_across_engines(self):
        # Empty row without manufactured numbers
        empty_row = {}
        engines = BreakoutSignalEngines()

        # Engine 1
        s1, d1 = engines.engine_1_momentum_acceleration(empty_row)
        assert s1 == 0.0
        assert d1["evidence_status"] == "MISSING"

        # Engine 2
        s2, d2 = engines.engine_2_volume_intelligence(empty_row)
        assert s2 == 0.0
        assert d2["evidence_status"] == "MISSING"

        # Engine 3
        s3, d3 = engines.engine_3_buy_sell_pressure(empty_row)
        assert s3 == 0.0
        assert d3["evidence_status"] == "MISSING"

        # Engine 4
        s4, d4 = engines.engine_4_liquidity_intelligence(empty_row)
        assert s4 == 0.0
        assert d4["evidence_status"] == "MISSING"

        # Engine 6
        s6, d6 = engines.engine_6_smart_money_research(empty_row)
        assert s6 == 0.0
        assert d6["evidence_status"] == "MISSING"

        # Engine 9
        s9, r9 = engines.engine_9_risk_intelligence(empty_row)
        assert s9 == 0.0
        assert "INSUFFICIENT_RISK_EVIDENCE" in r9
