import datetime as dt
import gzip
import json
from pathlib import Path
import pytest

from scripts.prospective_opportunity_dataset import ProspectiveOpportunityDataset
from scripts.collect_watchlist_observations import calculate_simulated_execution
from scripts.analyze_candle_outcomes import _forecast_path_label


class TestProspectiveOpportunityDataset:
    def test_create_snapshot_contains_all_22_fields(self):
        candidate = {
            "chain": "solana",
            "contract_address": "TokenMintAddress111111111111111111111111",
            "pool_address": "RaydiumPoolAddress111111111111111111111",
            "quote_symbol": "SOL",
            "quote_address": "So11111111111111111111111111111111111111112",
            "venue": "raydium",
            "token_age_hours": 12.5,
            "age_hours": 8.0,
            "final_price_usd": 0.05,
            "market_cap_usd": 500_000,
            "fdv_usd": 600_000,
            "liquidity_usd": 80_000,
            "volume_24h_usd": 250_000,
            "txns_24h": 1200,
            "buys_24h": 700,
            "sells_24h": 500,
            "change_1h_pct": 14.5,
            "change_24h_pct": 85.0,
            "breakout_confluence_score": 78.5,
            "breakout_engine_scores": {"engine_1_momentum_acceleration": 80.0},
            "engine_evidence_status": {"engine_1_momentum_acceleration": "VERIFIED"},
            "early_breakout_score": 75.0,
            "early_breakout_tier": "EARLY_BREAKOUT_LEADER",
            "retrieved_utc": "2026-11-13T12:00:00Z",
        }

        snapshot = ProspectiveOpportunityDataset.create_snapshot(
            candidate=candidate,
            run_id="local-run-test-1",
            scanner_version="v4.30.0",
            configuration_fingerprint="fingerprint-abc",
            qualification_status="QUALIFIED",
            rejection_reasons=[],
        )

        assert snapshot["schema"] == "crypto-prebreakout.prospective-opportunity.v1"
        assert snapshot["is_rejected_candidate"] is False
        assert snapshot["qualification_status"] == "QUALIFIED"
        assert "snapshot_id" in snapshot

        norm = snapshot["normalized_record"]
        # Verify mandatory 22 fields
        assert norm["chain"] == "solana"
        assert norm["token_contract"] == "TokenMintAddress111111111111111111111111"
        assert norm["pool_address"] == "RaydiumPoolAddress111111111111111111111"
        assert norm["quote_token_address"] == "So11111111111111111111111111111111111111112"
        assert "orientation" in norm
        assert norm["venue"] == "raydium"
        assert norm["token_age_hours"] == 12.5
        assert norm["pool_age_hours"] == 8.0
        assert norm["price_at_detection_usd"] == 0.05
        assert norm["market_cap_usd"] == 500_000
        assert norm["fdv_usd"] == 600_000
        assert norm["liquidity_usd"] == 80_000
        assert norm["volume"]["volume_24h_usd"] == 250_000
        assert norm["transactions"]["txns_24h"] == 1200
        assert norm["technical_indicators"]["change_1h_pct"] == 14.5
        assert norm["breakout_engines"]["confluence_score"] == 78.5
        assert "security_evidence_status" in norm
        assert "lp_and_wallet_evidence_status" in norm
        assert "source_provider_timestamps" in norm
        assert norm["local_retrieval_timestamp_utc"] == "2026-11-13T12:00:00Z"
        assert norm["scanner_version"] == "v4.30.0"
        assert norm["configuration_fingerprint"] == "fingerprint-abc"
        assert norm["signal_score_and_components"]["early_breakout_score"] == 75.0
        assert norm["qualification_status"] == "QUALIFIED"
        assert norm["exact_rejection_reasons"] == []

        # Raw evidence separated from normalized
        assert "raw_source_evidence" in snapshot
        assert snapshot["raw_source_evidence"]["contract_address"] == candidate["contract_address"]

    def test_rejected_candidate_preserved_without_selection_bias(self, tmp_path):
        rejected_candidate = {
            "chain": "base",
            "contract_address": "0x1234567890abcdef1234567890abcdef12345678",
            "pool_address": "0xabcdef1234567890abcdef1234567890abcdef12",
            "price_usd": 0.01,
            "liquidity_usd": 4_000,  # Below minimum threshold
        }
        rejection_reasons = ["LIQUIDITY_UNDER_5000_USD", "THIN_LIQUIDITY_RUG_RISK"]

        snapshot = ProspectiveOpportunityDataset.create_snapshot(
            candidate=rejected_candidate,
            run_id="local-run-test-2",
            scanner_version="v4.30.0",
            configuration_fingerprint="fingerprint-def",
            qualification_status="NOT_QUALIFIED",
            rejection_reasons=rejection_reasons,
        )

        assert snapshot["is_rejected_candidate"] is True
        assert snapshot["qualification_status"] == "NOT_QUALIFIED"
        assert snapshot["normalized_record"]["exact_rejection_reasons"] == sorted(rejection_reasons)

        # Test partition write
        dest = tmp_path / "scanner-memory"
        partition = ProspectiveOpportunityDataset.append_snapshots(dest, [snapshot], partition_date="20261113")
        assert partition.exists()

        # Read back gzipped content
        with gzip.open(partition, "rt", encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle]
        assert len(lines) == 1
        assert lines[0]["snapshot_id"] == snapshot["snapshot_id"]
        assert lines[0]["is_rejected_candidate"] is True


class TestOutcomeCollectionAndClassification:
    def test_calculate_simulated_execution_target_reached_with_touch_time(self):
        bars = [
            {"time_open": "2026-11-13T12:00:00Z", "high": 1.10, "low": 0.98, "close": 1.05},
            {"time_open": "2026-11-13T12:05:00Z", "high": 1.45, "low": 1.02, "close": 1.42},
            {"time_open": "2026-11-13T12:10:00Z", "high": 1.50, "low": 1.35, "close": 1.48},
        ]
        sim = calculate_simulated_execution(
            entry_price=1.00,
            target_price=1.40,
            bars_5m=bars,
            liquidity_usd=150_000,
        )

        assert sim["target_hit"] is True
        assert sim["outcome_classification"] == "TARGET_REACHED"
        assert sim["execution_status"] == "EXECUTION_UNVERIFIED"
        assert sim["first_observed_target_touch_utc"] == "2026-11-13T12:05:00Z"
        assert sim["mfe_pct"] == 50.0
        assert sim["mae_pct"] == -2.0

    def test_calculate_simulated_execution_target_not_reached(self):
        bars = [
            {"time_open": "2026-11-13T12:00:00Z", "high": 1.15, "low": 0.95, "close": 1.10},
            {"time_open": "2026-11-13T12:05:00Z", "high": 1.20, "low": 1.05, "close": 1.12},
        ]
        sim = calculate_simulated_execution(
            entry_price=1.00,
            target_price=1.40,
            bars_5m=bars,
            liquidity_usd=150_000,
        )

        assert sim["target_hit"] is False
        assert sim["outcome_classification"] == "TARGET_NOT_REACHED"
        assert sim["execution_status"] == "EXECUTION_UNVERIFIED"
        assert sim["first_observed_target_touch_utc"] is None
        assert sim["mfe_pct"] == 20.0
        assert sim["mae_pct"] == -5.0
