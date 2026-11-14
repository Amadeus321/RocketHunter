import datetime as dt
from typing import Any
import pytest
from scripts.chronological_backtest import ChronologicalBacktestEvaluator
from scripts.address_identity import validate_and_build_identity


def make_valid_trace(
    market_key: str,
    detection_dt: dt.datetime,
    duration_hours: float = 24.0,
    mfe_pct: float = 45.0,
    touch_hours: float | None = 3.5,
    interval: str = "1h",
    provenance: str = "gecko_terminal_exact_token_1h_ohlcv",
    has_gaps: bool = False,
) -> dict[str, Any]:
    trace_end_dt = detection_dt + dt.timedelta(hours=duration_hours)
    return {
        "trace_provenance": provenance,
        "market_key": market_key,
        "candle_interval": interval,
        "trace_start_utc": detection_dt.isoformat(),
        "trace_end_utc": trace_end_dt.isoformat(),
        "mfe_pct": mfe_pct,
        "hours_to_target": touch_hours,
        "has_gaps": has_gaps,
    }


class TestChronologicalBacktest:
    def test_chronological_ordering_and_no_future_leakage(self):
        t0 = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        records = [
            # Intentionally unordered input timestamps
            {
                "snapshot_utc": (t0 + dt.timedelta(days=10)).isoformat(),
                "chain": "solana", "contract_address": "TokenA", "pool_address": "PoolA",
                "price_usd": 1.0, "early_breakout_score": 85.0, "liquidity_usd": 100_000,
            },
            {
                "snapshot_utc": (t0 + dt.timedelta(days=1)).isoformat(),
                "chain": "base", "contract_address": "TokenB", "pool_address": "PoolB",
                "price_usd": 2.0, "early_breakout_score": 80.0, "liquidity_usd": 50_000,
            },
            {
                "snapshot_utc": (t0 + dt.timedelta(days=5)).isoformat(),
                "chain": "ethereum", "contract_address": "TokenC", "pool_address": "PoolC",
                "price_usd": 3.0, "early_breakout_score": 60.0, "liquidity_usd": 200_000,
            },
        ]

        evaluator = ChronologicalBacktestEvaluator(records)
        dataset = evaluator.prepare_chronological_dataset()

        # Check chronological sorting
        assert len(dataset) == 3
        assert dataset[0]["chain"] == "base"
        assert dataset[1]["chain"] == "ethereum"
        assert dataset[2]["chain"] == "solana"
        assert dataset[0]["timestamp"] < dataset[1]["timestamp"] < dataset[2]["timestamp"]

    def test_ambiguous_identities_are_excluded(self):
        records = [
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": None, "contract_address": "TokenA", "pool_address": "PoolA", "price_usd": 1.0},
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": "solana", "contract_address": "", "pool_address": "PoolA", "price_usd": 1.0},
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": "solana", "contract_address": "TokenValid", "pool_address": "PoolValid", "price_usd": None},
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": "solana", "contract_address": "TokenValid", "price_usd": 1.0},  # Missing pool address!
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": "solana", "contract_address": "TokenValid", "pool_address": "PoolValid", "price_usd": 1.0},  # Valid exact identity
        ]
        evaluator = ChronologicalBacktestEvaluator(records)
        dataset = evaluator.prepare_chronological_dataset()
        assert len(dataset) == 1
        assert dataset[0]["token"] == "TokenValid"
        assert dataset[0]["pool"] == "PoolValid"
        assert dataset[0]["market_key"] == "dex:solana:TokenValid:PoolValid"
        assert evaluator.diagnostics["missing_identity_records"] == 3
        assert evaluator.diagnostics["missing_identity_by_reason"]["MISSING_POOL"] == 1
        assert len(evaluator.diagnostics["rejected_records"]) == 4

    def test_walk_forward_evaluation_and_metrics_calculation(self):
        base_time = dt.datetime(2026, 11, 1, 0, 0, tzinfo=dt.timezone.utc)
        records = []

        # Generate 20 synthetic chronological records over 20 days
        for i in range(20):
            t = base_time + dt.timedelta(days=i)
            # High scores alternate hitting TP40 vs missing
            hit_tp = (i % 2 == 0)
            score = 80.0 if i % 2 == 0 else 76.0
            ch = "solana" if i % 2 == 0 else "base"
            m_key = f"dex:{ch}:Token{i}:Pool{i}"
            trace = make_valid_trace(
                market_key=m_key,
                detection_dt=t,
                duration_hours=24.0,
                mfe_pct=45.0 if hit_tp else 15.0,
                touch_hours=3.5 if hit_tp else None,
            )
            records.append({
                "snapshot_utc": t.isoformat(),
                "chain": ch,
                "contract_address": f"Token{i}",
                "pool_address": f"Pool{i}",
                "price_usd": 1.0,
                "early_breakout_score": score,
                "liquidity_usd": 50_000 * (i + 1),
                "age_hours": 12.0,
                "mfe_pct": 45.0 if hit_tp else 15.0,
                "mae_pct": -8.0,
                "hours_to_target": 3.5 if hit_tp else None,
                "outcome_classification": "TARGET_REACHED" if hit_tp else "TARGET_NOT_REACHED",
                "candle_trace": trace,
            })

        evaluator = ChronologicalBacktestEvaluator(records)
        report = evaluator.evaluate_walk_forward(
            train_window_hours=72,
            test_window_hours=48,
            step_hours=24,
            score_threshold=75.0,
        )

        assert report["status"] == "COMPLETED"
        assert report["evaluation_policy"].startswith("STRICT_CHRONOLOGICAL_WALK_FORWARD")
        assert "dataset_hash" in report
        assert report["walk_forward_windows_run"] > 0

        model_results = report["out_of_sample_model_results"]
        assert model_results["sample_size"] > 0
        assert 0.0 <= model_results["tp40_rate_pct"] <= 100.0
        assert 0.0 <= model_results["false_positive_rate_pct"] <= 100.0
        assert len(model_results["wilson_95_ci_pct"]) == 2
        assert model_results["wilson_95_ci_pct"][0] <= model_results["wilson_95_ci_pct"][1]

        # Milestone attainment dictionary check
        milestones = model_results["milestone_attainment_pct"]
        assert "within_1h" in milestones
        assert "within_24h" in milestones

        # Performance by chain check
        by_chain = report["performance_by_chain"]
        assert "solana" in by_chain or "base" in by_chain

        # Performance by liquidity check
        by_liq = report["performance_by_liquidity_tier"]
        assert isinstance(by_liq, dict)

    def test_synthetic_labels_strictly_rejected_without_verified_candle_outcome(self):
        evaluator = ChronologicalBacktestEvaluator([])
        # Candidate with change_24h_pct but no verified candle outcome
        item = {
            "record_id": "rec_no_outcome",
            "market_key": "solana:tokenA:poolA",
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "change_24h_pct": 80.0,  # Pump happened, but NO verified candle outcome!
            },
        }
        outcome = evaluator._resolve_outcome(item, lookup=None)
        # Must NEVER synthesize MFE, MAE, TP attainment, or touch hours from change_24h_pct
        assert outcome["is_evaluated"] is False
        assert outcome["target_reached"] is None
        assert outcome["mfe_pct"] is None
        assert outcome["mae_pct"] is None
        assert outcome["hours_to_target"] is None
        assert outcome["outcome_classification"] == "INCOMPLETE_OBSERVATION"
        assert outcome["resolution_reason"] == "UNRESOLVED_INCOMPLETE_OR_CENSORED"

    def test_incomplete_records_excluded_from_tp40_rate_and_counted(self):
        evaluator = ChronologicalBacktestEvaluator([])
        evaluations = [
            # 2 evaluated winners
            {"outcome": {"is_evaluated": True, "target_reached": True, "mfe_pct": 50.0, "hours_to_target": 2.0}},
            {"outcome": {"is_evaluated": True, "target_reached": True, "mfe_pct": 60.0, "hours_to_target": 3.0}},
            # 1 evaluated loser
            {"outcome": {"is_evaluated": True, "target_reached": False, "mfe_pct": 10.0, "hours_to_target": None}},
            # 3 incomplete / unresolved records
            {"outcome": {"is_evaluated": False, "outcome_classification": "INCOMPLETE_OBSERVATION", "target_reached": None}},
            {"outcome": {"is_evaluated": False, "outcome_classification": "UNRESOLVED", "target_reached": None}},
            {"outcome": {"is_evaluated": False, "outcome_classification": "UNKNOWN_DUE_TO_GAP", "target_reached": None}},
        ]
        metrics = evaluator._calculate_metrics(evaluations)
        assert metrics["total_records_considered"] == 6
        assert metrics["evaluated_records_count"] == 3
        assert metrics["unresolved_records_count"] == 2
        assert metrics["incomplete_records_count"] == 2
        assert metrics["censored_records_count"] == 1
        # tp40_rate_pct must be computed strictly on 3 evaluated records (2/3 = 66.67%), NOT 2/6 (33.33%)
        assert metrics["tp40_rate_pct"] == 66.67
        assert metrics["tp40_hits"] == 2

    def test_all_unresolved_records_returns_no_evaluated_records(self):
        evaluator = ChronologicalBacktestEvaluator([])
        evaluations = [
            {"outcome": {"is_evaluated": False, "outcome_classification": "INCOMPLETE_OBSERVATION", "target_reached": None}},
            {"outcome": {"is_evaluated": False, "outcome_classification": "UNRESOLVED", "target_reached": None}},
        ]
        metrics = evaluator._calculate_metrics(evaluations)
        assert metrics["status"] == "NO_EVALUATED_RECORDS"
        assert metrics["evaluated_records_count"] == 0
        assert metrics["tp40_rate_pct"] is None

    def test_two_pools_trading_same_token_cannot_share_outcomes(self):
        """Regression 1: Two pools for the same token must maintain independent identities and cannot share outcomes."""
        evaluator = ChronologicalBacktestEvaluator([])
        item_pool_a = {
            "record_id": "rec_pool_a",
            "market_key": "dex:solana:TokenX:PoolA",
            "exact_identity": "dex:solana:TokenX:PoolA",
            "timestamp": dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc),
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {},
        }
        item_pool_b = {
            "record_id": "rec_pool_b",
            "market_key": "dex:solana:TokenX:PoolB",
            "exact_identity": "dex:solana:TokenX:PoolB",
            "timestamp": dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc),
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 0.8,
            "raw_record": {},
        }

        # Lookup contains outcome for Pool A only
        outcome_lookup = {
            "rec_pool_a": {
                "exact_identity": "dex:solana:TokenX:PoolA",
                "outcome_classification": "TARGET_REACHED",
                "target_reached": True,
                "mfe_pct": 55.0,
                "hours_to_target": 2.0,
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", item_pool_a["timestamp"], mfe_pct=55.0, touch_hours=2.0),
            },
            # If an attempt is made to join Pool B with Pool A's outcome, it must be rejected!
            "rec_pool_b": {
                "exact_identity": "dex:solana:TokenX:PoolA",  # Wrong pool!
                "outcome_classification": "TARGET_REACHED",
                "target_reached": True,
                "mfe_pct": 55.0,
                "hours_to_target": 2.0,
            },
        }

        res_a = evaluator._resolve_outcome(item_pool_a, outcome_lookup)
        assert res_a["is_evaluated"] is True
        assert res_a["target_reached"] is True

        res_b = evaluator._resolve_outcome(item_pool_b, outcome_lookup)
        assert res_b["is_evaluated"] is False
        assert res_b["outcome_classification"] == "OUTCOME_IDENTITY_MISMATCH"
        assert "OUTCOME_IDENTITY_MISMATCH" in res_b["resolution_reason"]

    def test_two_tokens_with_same_symbol_cannot_share_outcomes(self):
        """Regression 2: Two different tokens with the same symbol (e.g. PEPE) have different contracts and cannot share outcomes."""
        records = [
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": "solana", "symbol": "PEPE", "contract_address": "MintRealPEPE", "pool_address": "Pool1", "price_usd": 1.0},
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": "solana", "symbol": "PEPE", "contract_address": "MintFakePEPE", "pool_address": "Pool2", "price_usd": 0.5},
        ]
        evaluator = ChronologicalBacktestEvaluator(records)
        dataset = evaluator.prepare_chronological_dataset()
        assert len(dataset) == 2
        assert dataset[0]["market_key"] != dataset[1]["market_key"]
        assert dataset[0]["token"] == "MintRealPEPE"
        assert dataset[1]["token"] == "MintFakePEPE"

    def test_same_token_on_two_chains_cannot_share_outcomes(self):
        """Regression 3: The same token deployed across different chains maintains distinct identities."""
        records = [
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": "ethereum", "contract_address": "0x1111111111111111111111111111111111111111", "pool_address": "0xaaaabbbbccccddddeeeeffff0000111122223333", "price_usd": 1.0},
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": "base", "contract_address": "0x1111111111111111111111111111111111111111", "pool_address": "0xaaaabbbbccccddddeeeeffff0000111122223333", "price_usd": 1.0},
        ]
        evaluator = ChronologicalBacktestEvaluator(records)
        dataset = evaluator.prepare_chronological_dataset()
        assert len(dataset) == 2
        assert dataset[0]["market_key"].startswith("dex:ethereum:")
        assert dataset[1]["market_key"].startswith("dex:base:")
        assert dataset[0]["market_key"] != dataset[1]["market_key"]

    def test_missing_pool_addresses_cannot_enter_dex_performance_denominators(self):
        """Regression 4: Records missing a DEX pool address are strictly excluded from dataset and cannot enter denominators."""
        records = [
            # Missing pool address
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": "solana", "contract_address": "MintNoPool", "price_usd": 1.0, "mfe_pct": 50.0},
            # Valid exact identity
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": "solana", "contract_address": "MintWithPool", "pool_address": "PoolX", "price_usd": 1.0, "mfe_pct": 50.0, "outcome_classification": "TARGET_REACHED"},
        ]
        evaluator = ChronologicalBacktestEvaluator(records)
        dataset = evaluator.prepare_chronological_dataset()
        assert len(dataset) == 1
        assert dataset[0]["token"] == "MintWithPool"
        assert evaluator.diagnostics["missing_identity_records"] == 1
        assert evaluator.diagnostics["missing_identity_by_reason"]["MISSING_POOL"] == 1

    def test_duplicate_observations_do_not_inflate_sample_count(self):
        """Regression 5: Duplicate observations for the same market key and timestamp are deduplicated."""
        records = [
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": "solana", "contract_address": "MintA", "pool_address": "PoolA", "price_usd": 1.0},
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": "solana", "contract_address": "MintA", "pool_address": "PoolA", "price_usd": 1.0},  # Duplicate!
            {"snapshot_utc": "2026-11-02T12:00:00Z", "chain": "solana", "contract_address": "MintA", "pool_address": "PoolA", "price_usd": 1.1},  # Different timestamp: valid
        ]
        evaluator = ChronologicalBacktestEvaluator(records)
        dataset = evaluator.prepare_chronological_dataset()
        assert len(dataset) == 2
        assert evaluator.diagnostics["duplicate_records"] == 1

    def test_future_information_cannot_overwrite_detection_time_features(self):
        """Regression 6: Resolving outcomes preserves frozen detection-time features (price, score, timestamp)."""
        evaluator = ChronologicalBacktestEvaluator([])
        item = {
            "record_id": "rec_future_test",
            "market_key": "dex:solana:MintFrozen:PoolFrozen",
            "timestamp": dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc),
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "score": 88.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "MintFrozen",
                "pool_address": "PoolFrozen",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 65.0,
                "hours_to_target": 3.0,
                "price_usd_at_outcome": 2.5,  # Future price!
                "candle_trace": make_valid_trace("dex:solana:MintFrozen:PoolFrozen", dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc), mfe_pct=65.0, touch_hours=3.0),
            },
        }
        outcome = evaluator._resolve_outcome(item, lookup=None)
        # Outcome is resolved
        assert outcome["is_evaluated"] is True
        assert outcome["target_reached"] is True
        # Detection time values on item remain unaltered
        assert item["detection_price"] == 1.0
        assert item["score"] == 88.0
        assert item["timestamp_utc"] == "2026-11-01T12:00:00Z"

    def test_incomplete_outcomes_remain_incomplete(self):
        """Regression 7: Incomplete, gap, or expired outcomes remain strictly unresolved and un-evaluated."""
        evaluator = ChronologicalBacktestEvaluator([])
        item = {
            "record_id": "rec_incomplete",
            "market_key": "dex:solana:MintInc:PoolInc",
            "timestamp": dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc),
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "MintInc",
                "pool_address": "PoolInc",
                "outcome_classification": "UNKNOWN_DUE_TO_GAP",
                "mfe_pct": 50.0,  # Even if an unverified MFE is claimed during a gap, must remain incomplete!
            },
        }
        outcome = evaluator._resolve_outcome(item, lookup=None)
        assert outcome["is_evaluated"] is False
        assert outcome["target_reached"] is None
        assert outcome["mfe_pct"] is None
        assert outcome["outcome_classification"] == "UNKNOWN_DUE_TO_GAP"
        assert outcome["resolution_reason"] == "UNRESOLVED_INCOMPLETE_OR_CENSORED"

    def test_conflicting_identities_detected_and_reported_in_diagnostics(self):
        """Regression 8: Conflicting token representations (e.g. mismatch between top-level and normalized_record) are rejected."""
        records = [
            {
                "snapshot_utc": "2026-11-01T12:00:00Z",
                "chain": "solana",
                "contract_address": "MintA",
                "pool_address": "Pool1",
                "price_usd": 1.0,
                "normalized_record": {
                    "chain": "solana",
                    "token_contract": "MintB",  # Conflicting token!
                    "pool_address": "Pool1",
                },
            }
        ]
        evaluator = ChronologicalBacktestEvaluator(records)
        dataset = evaluator.prepare_chronological_dataset()
        assert len(dataset) == 0
        assert evaluator.diagnostics["conflicting_identity_records"] == 1
        assert evaluator.diagnostics["missing_identity_by_reason"]["CONFLICTING_TOKEN_IDENTITY"] == 1

    def test_outcome_identity_missing_rejected(self):
        """Regression 9: Outcome record without canonical identity is rejected with OUTCOME_IDENTITY_MISSING."""
        evaluator = ChronologicalBacktestEvaluator([])
        item = {
            "record_id": "rec_outcome_missing_id",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {},
        }
        # Outcome has matching record_id but NO identity fields at all
        outcome_lookup = {
            "rec_outcome_missing_id": {
                "outcome_classification": "TARGET_REACHED",
                "target_reached": True,
                "mfe_pct": 50.0,
            }
        }
        res = evaluator._resolve_outcome(item, outcome_lookup)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "OUTCOME_IDENTITY_MISSING"
        assert "OUTCOME_IDENTITY_MISSING" in res["resolution_reason"]
        assert evaluator.diagnostics["outcome_identity_missing_records"] == 1
        assert evaluator.diagnostics["unresolved_outcomes"] == 1

    def test_outcome_identity_invalid_rejected(self):
        """Regression 10: Outcome record with malformed/conflicting identity is rejected with OUTCOME_IDENTITY_INVALID."""
        evaluator = ChronologicalBacktestEvaluator([])
        item = {
            "record_id": "rec_outcome_invalid_id",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {},
        }
        # Outcome has conflicting pool representation in normalized_record
        outcome_lookup = {
            "rec_outcome_invalid_id": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "normalized_record": {
                    "pool_address": "PoolConflicting",
                },
                "outcome_classification": "TARGET_REACHED",
                "target_reached": True,
                "mfe_pct": 50.0,
            }
        }
        res = evaluator._resolve_outcome(item, outcome_lookup)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "OUTCOME_IDENTITY_INVALID"
        assert "OUTCOME_IDENTITY_INVALID" in res["resolution_reason"]
        assert evaluator.diagnostics["outcome_identity_invalid_records"] == 1
        assert evaluator.diagnostics["unresolved_outcomes"] == 1

    def test_outcome_identity_mismatch_rejected(self):
        """Regression 11: Outcome record with valid identity that does not match forecast is rejected with OUTCOME_IDENTITY_MISMATCH."""
        evaluator = ChronologicalBacktestEvaluator([])
        item = {
            "record_id": "rec_outcome_mismatch",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {},
        }
        # Outcome has valid identity for PoolB instead of PoolA
        outcome_lookup = {
            "rec_outcome_mismatch": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolB",
                "outcome_classification": "TARGET_REACHED",
                "target_reached": True,
                "mfe_pct": 50.0,
            }
        }
        res = evaluator._resolve_outcome(item, outcome_lookup)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "OUTCOME_IDENTITY_MISMATCH"
        assert "OUTCOME_IDENTITY_MISMATCH" in res["resolution_reason"]
        assert evaluator.diagnostics["outcome_identity_mismatch_records"] == 1
        assert evaluator.diagnostics["conflicting_identity_records"] == 1
        assert evaluator.diagnostics["unresolved_outcomes"] == 1

    def test_contradictory_tp40_not_reached_with_high_mfe_rejected(self):
        """Regression 12: TARGET_NOT_REACHED with MFE >= 40% is contradictory and marked OUTCOME_EVIDENCE_CONFLICT."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_contradictory_1",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "technical_tp_pct": 40.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_NOT_REACHED",
                "mfe_pct": 52.0,  # Contradicts TARGET_NOT_REACHED!
                "mae_pct": -5.0,
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", t, duration_hours=24.0, mfe_pct=52.0, touch_hours=None),
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["target_reached"] is None
        assert res["outcome_classification"] == "OUTCOME_EVIDENCE_CONFLICT"
        assert evaluator.diagnostics["evidence_conflict_records"] == 1
        assert evaluator.diagnostics["unresolved_outcomes"] == 1

    def test_contradictory_tp40_reached_with_low_mfe_rejected(self):
        """Regression 13: TARGET_REACHED with MFE < 40% is contradictory and marked OUTCOME_EVIDENCE_CONFLICT."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_contradictory_2",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "technical_tp_pct": 40.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 18.0,  # Contradicts TARGET_REACHED!
                "mae_pct": -5.0,
                "hours_to_target": 4.0,
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", t, duration_hours=24.0, mfe_pct=18.0, touch_hours=4.0),
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["target_reached"] is None
        assert res["outcome_classification"] == "OUTCOME_EVIDENCE_CONFLICT"
        assert evaluator.diagnostics["evidence_conflict_records"] == 1
        assert evaluator.diagnostics["unresolved_outcomes"] == 1

    def test_target_touch_outside_24h_window_rejected(self):
        """Regression 14: Target touch occurring beyond the 24-hour window is rejected as TARGET_TOUCH_OUTSIDE_WINDOW."""
        evaluator = ChronologicalBacktestEvaluator([])
        # Case A: hours_to_target > 24.0
        t_a = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item_a = {
            "record_id": "rec_touch_outside_a",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t_a,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "technical_tp_pct": 40.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 60.0,
                "hours_to_target": 27.5,  # > 24.0h!
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", t_a, duration_hours=28.0, mfe_pct=60.0, touch_hours=27.5),
            },
        }
        res_a = evaluator._resolve_outcome(item_a, lookup=None)
        assert res_a["is_evaluated"] is False
        assert res_a["outcome_classification"] == "TARGET_TOUCH_OUTSIDE_WINDOW"
        assert evaluator.diagnostics["target_outside_window_records"] == 1

        # Case B: touch timestamp > 24.0h after detection timestamp
        t_b = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item_b = {
            "record_id": "rec_touch_outside_b",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t_b,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "technical_tp_pct": 40.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 60.0,
                "touch_timestamp_utc": "2026-11-02T16:00:00Z",  # 28 hours later!
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", t_b, duration_hours=28.0, mfe_pct=60.0, touch_hours=28.0),
            },
        }
        res_b = evaluator._resolve_outcome(item_b, lookup=None)
        assert res_b["is_evaluated"] is False
        assert res_b["outcome_classification"] == "TARGET_TOUCH_OUTSIDE_WINDOW"
        assert evaluator.diagnostics["target_outside_window_records"] == 2

    def test_invalid_touch_timestamp_rejected(self):
        """Regression 15: Target touch occurring before detection time is rejected as INVALID_TOUCH_TIMESTAMP."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_touch_invalid",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "technical_tp_pct": 40.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 60.0,
                "touch_timestamp_utc": "2026-11-01T10:00:00Z",  # 2 hours before detection!
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", t, duration_hours=24.0, mfe_pct=60.0, touch_hours=2.0),
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "INVALID_TOUCH_TIMESTAMP"
        assert evaluator.diagnostics["evidence_conflict_records"] == 1

    def test_frozen_detection_target_evaluated_not_recalculated_target(self):
        """Regression 16: Evaluates against the frozen detection-time target (+50%), not a lower recalculated target (+40%)."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_frozen_target",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "technical_tp_pct": 50.0,  # Frozen target was +50.0%
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",  # Provider claims reached based on +40%
                "mfe_pct": 45.0,  # Reached 45%, which is < frozen 50% target!
                "hours_to_target": 5.0,
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", t, duration_hours=24.0, mfe_pct=45.0, touch_hours=5.0),
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "OUTCOME_EVIDENCE_CONFLICT"

    def test_dex_record_with_venue_never_becomes_cex_when_pool_missing(self):
        """Regression 17: A DEX record with a venue/provider field and missing pool must NEVER become CEX."""
        rec = {
            "chain": "solana",
            "token_mint": "TokenA11111111111111111111111111111111111111",
            "venue": "raydium",  # Venue metadata present
            "source": "gecko",
            # pool_address is missing!
        }
        res = validate_and_build_identity(rec)
        assert res["is_valid"] is False
        assert res["market_type"] == "dex"
        assert res["rejection_reason"] == "MISSING_POOL"

    def test_ambiguous_market_type_strictly_rejected(self):
        """Regression 18: Records with contradictory DEX and CEX indicators are rejected as AMBIGUOUS_MARKET_TYPE."""
        rec = {
            "chain": "ethereum",
            "contract_address": "0x1111111111111111111111111111111111111111",
            "pool_address": "0x2222222222222222222222222222222222222222",
            "market_type": "cex_spot",  # Contradicts DEX fields!
        }
        res = validate_and_build_identity(rec)
        assert res["is_valid"] is False
        assert res["market_type"] == "ambiguous"
        assert res["rejection_reason"] == "AMBIGUOUS_MARKET_TYPE"

    def test_market_target_touch_separated_from_execution_net_profit(self):
        """Regression 19: Market target touch (+40%) is reported separately from executable net profit."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_exec_sep",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "technical_tp_pct": 40.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 45.0,
                "hours_to_target": 4.0,
                "execution_status": "EXECUTABLE",
                "execution_net_return_pct": 36.2,  # Net return after slippage and gas fees
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", t, duration_hours=24.0, mfe_pct=45.0, touch_hours=4.0),
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is True
        assert res["market_target_reached"] is True
        assert res["target_reached"] is True
        assert res["execution_status"] == "EXECUTABLE"
        assert res["execution_net_return_pct"] == 36.2

    def test_embedded_outcome_missing_identity_rejected(self):
        """Regression 20: Embedded outcome missing canonical identity is strictly rejected with OUTCOME_IDENTITY_MISSING."""
        evaluator = ChronologicalBacktestEvaluator([])
        item = {
            "record_id": "rec_embed_missing_id",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc),
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                # Embedded outcome dictionary without identity fields
                "outcome": {
                    "outcome_classification": "TARGET_REACHED",
                    "mfe_pct": 50.0,
                    "hours_to_target": 3.0,
                }
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "OUTCOME_IDENTITY_MISSING"
        assert "OUTCOME_IDENTITY_MISSING" in res["resolution_reason"]
        assert evaluator.diagnostics["outcome_identity_missing_records"] == 1

    def test_embedded_outcome_invalid_identity_rejected(self):
        """Regression 21: Embedded outcome with conflicting/invalid identity is strictly rejected with OUTCOME_IDENTITY_INVALID."""
        evaluator = ChronologicalBacktestEvaluator([])
        item = {
            "record_id": "rec_embed_invalid_id",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc),
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                # Embedded outcome has conflicting pool representation in normalized_record
                "outcome": {
                    "chain": "solana",
                    "contract_address": "TokenX",
                    "pool_address": "PoolA",
                    "normalized_record": {
                        "pool_address": "PoolConflicting",
                    },
                    "outcome_classification": "TARGET_REACHED",
                    "mfe_pct": 50.0,
                    "hours_to_target": 3.0,
                }
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "OUTCOME_IDENTITY_INVALID"
        assert "OUTCOME_IDENTITY_INVALID" in res["resolution_reason"]
        assert evaluator.diagnostics["outcome_identity_invalid_records"] == 1

    def test_embedded_outcome_different_pool_rejected(self):
        """Regression 22: Embedded outcome for a different pool than the forecast is rejected with OUTCOME_IDENTITY_MISMATCH."""
        evaluator = ChronologicalBacktestEvaluator([])
        item = {
            "record_id": "rec_embed_diff_pool",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc),
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                # Embedded outcome has valid identity for PoolOther instead of PoolA
                "outcome": {
                    "chain": "solana",
                    "contract_address": "TokenX",
                    "pool_address": "PoolOther",
                    "outcome_classification": "TARGET_REACHED",
                    "mfe_pct": 50.0,
                    "hours_to_target": 3.0,
                }
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "OUTCOME_IDENTITY_MISMATCH"
        assert "OUTCOME_IDENTITY_MISMATCH" in res["resolution_reason"]
        assert evaluator.diagnostics["outcome_identity_mismatch_records"] == 1

    def test_target_reached_missing_touch_evidence_remains_unevaluated(self):
        """Regression 23: TARGET_REACHED without verifiable touch time remains unevaluated with MISSING_TARGET_TOUCH_EVIDENCE."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_no_touch_time",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 55.0,  # High MFE, but NO touch time or touch timestamp!
                "hours_to_target": None,
                "touch_timestamp_utc": None,
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", t, duration_hours=24.0, mfe_pct=55.0, touch_hours=None),
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["target_reached"] is None
        assert res["outcome_classification"] == "MISSING_TARGET_TOUCH_EVIDENCE"
        assert evaluator.diagnostics["missing_touch_evidence_records"] == 1
        assert evaluator.diagnostics["unresolved_outcomes"] == 1

    def test_target_reached_missing_detection_time_rejected(self):
        """Regression 24: TARGET_REACHED where detection timestamp is missing or naive is rejected as MISSING_DETECTION_TIME."""
        evaluator = ChronologicalBacktestEvaluator([])
        item = {
            "record_id": "rec_naive_time",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": dt.datetime(2026, 11, 1, 12, 0),  # Naive, non-tz-aware datetime!
            "timestamp_utc": "2026-11-01T12:00:00",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 55.0,
                "hours_to_target": 4.0,
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "MISSING_DETECTION_TIME"
        assert evaluator.diagnostics["missing_detection_time_records"] == 1

    def test_conflicting_touch_timestamp_and_elapsed_time_rejected(self):
        """Regression 25: Disagreement between touch timestamp and claimed elapsed time triggers OUTCOME_EVIDENCE_CONFLICT."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_touch_conflict",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 55.0,
                "touch_timestamp_utc": "2026-11-01T14:00:00Z",  # 2.0h elapsed
                "hours_to_target": 7.5,  # Claimed 7.5h: disagrees by 5.5h (tolerance 0.1h)!
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", t, duration_hours=24.0, mfe_pct=55.0, touch_hours=7.5),
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "OUTCOME_EVIDENCE_CONFLICT"
        assert "conflicts with claimed elapsed time" in res["resolution_reason"]
        assert evaluator.diagnostics["evidence_conflict_records"] == 1

    def test_valid_target_touch_inside_24h_window_evaluated(self):
        """Regression 26: Consistent touch timestamp and elapsed hours within 0–24h window are verified."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_valid_touch",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 55.0,
                "touch_timestamp_utc": "2026-11-01T16:30:00Z",  # 4.5h elapsed
                "hours_to_target": 4.5,
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", t, duration_hours=24.0, mfe_pct=55.0, touch_hours=4.5),
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is True
        assert res["market_target_reached"] is True
        assert res["target_reached"] is True
        assert res["hours_to_target"] == 4.5
        assert evaluator.diagnostics["successfully_matched_outcomes"] == 1

    def test_cross_module_canonical_identity_unification(self):
        """Regression 27: scripts.v418_pipeline.market_key and scripts.address_identity produce the exact same canonical identity."""
        from scripts import v418_pipeline, address_identity

        test_cases = [
            {"chain": "solana", "contract_address": "Mint1111111111111111111111111111111111111111", "pool_address": "PoolRaydium111111111111111111111111111111111", "market_type": "dex_pool"},
            {"chain": "base", "contract_address": "0x1234567890abcdef1234567890abcdef12345678", "pool_address": "0xabcdef1234567890abcdef1234567890abcdef12", "market_type": "dex_pool"},
            {"chain": "ethereum", "contract_address": "0xABCDEF1234567890ABCDEF1234567890ABCDEF12", "pool_address": "0x1111222233334444555566667777888899990000", "market_type": "dex_pool"},
        ]

        for case in test_cases:
            pipe_key = v418_pipeline.market_key(case)
            canon_res = address_identity.validate_and_build_identity(case)
            assert canon_res["is_valid"] is True
            canon_key = canon_res["canonical_identity"]
            assert pipe_key == canon_key
            assert pipe_key.startswith("dex:")
            # Exactly 4 colon-separated parts: dex:<chain>:<token>:<pool>
            parts = pipe_key.split(":")
            assert len(parts) == 4
            assert parts[0] == "dex"
            assert parts[1] == case["chain"].lower()

    # --- ISSUE 1: Complete 24h Observation Tests for TARGET_NOT_REACHED ---

    def test_loss_5h_observation_rejected_as_incomplete(self):
        """Regression 28: TARGET_NOT_REACHED with only 5h observation window is rejected as INCOMPLETE_OBSERVATION."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_loss_5h",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_NOT_REACHED",
                "mfe_pct": 10.0,
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", t, duration_hours=5.0, mfe_pct=10.0, touch_hours=None),
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "INCOMPLETE_OBSERVATION"
        assert "PARTIAL_OBSERVATION_WINDOW" in res["resolution_reason"]

    def test_loss_23h_observation_rejected_as_incomplete(self):
        """Regression 29: TARGET_NOT_REACHED with 23h observation window is rejected as INCOMPLETE_OBSERVATION."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_loss_23h",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_NOT_REACHED",
                "mfe_pct": 15.0,
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", t, duration_hours=23.0, mfe_pct=15.0, touch_hours=None),
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "INCOMPLETE_OBSERVATION"
        assert "PARTIAL_OBSERVATION_WINDOW" in res["resolution_reason"]

    def test_loss_exactly_24h_observation_evaluated(self):
        """Regression 30: TARGET_NOT_REACHED with verified full 24h observation window and no gaps is evaluated as verified loss."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_loss_24h",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_NOT_REACHED",
                "mfe_pct": 20.0,
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", t, duration_hours=24.0, mfe_pct=20.0, touch_hours=None),
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is True
        assert res["target_reached"] is False
        assert res["market_target_reached"] is False
        assert res["outcome_classification"] == "TARGET_NOT_REACHED"

    def test_loss_missing_observation_end_rejected(self):
        """Regression 31: TARGET_NOT_REACHED without observation end timestamp is rejected."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_loss_no_end",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_NOT_REACHED",
                "mfe_pct": 12.0,
                "candle_trace": {
                    "trace_provenance": "gecko_terminal",
                    "market_key": "dex:solana:TokenX:PoolA",
                    "candle_interval": "1h",
                    "trace_start_utc": t.isoformat(),
                    "trace_end_utc": None,  # Missing end timestamp!
                },
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "MISSING_TRACE_TIMESTAMPS"

    def test_loss_with_candle_gap_rejected_as_incomplete(self):
        """Regression 32: Candle trace with detected gaps is rejected as INCOMPLETE_OBSERVATION."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_loss_gap",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_NOT_REACHED",
                "mfe_pct": 15.0,
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", t, duration_hours=24.0, mfe_pct=15.0, touch_hours=None, has_gaps=True),
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "INCOMPLETE_OBSERVATION"
        assert "CANDLE_GAP_DETECTED" in res["resolution_reason"]

    # --- ISSUE 2: Identity Representation Conflict Tests ---

    def test_identity_pool_a_structural_pool_b_rejected(self):
        """Regression 33: Conflict where identity specifies Pool A and structural fields specify Pool B is rejected."""
        rec = {
            "identity": "dex:solana:TokenX:PoolA",
            "chain": "solana",
            "contract_address": "TokenX",
            "pool_address": "PoolB",  # Conflicting pool!
        }
        res = validate_and_build_identity(rec)
        assert res["is_valid"] is False
        assert res["rejection_reason"] == "CONFLICTING_POOL_IDENTITY"

    def test_market_key_token_a_exact_identity_token_b_rejected(self):
        """Regression 34: Conflict between market_key and exact_identity strings is rejected as CONFLICTING_CANONICAL_STRINGS."""
        rec = {
            "market_key": "dex:solana:TokenA:PoolA",
            "exact_identity": "dex:solana:TokenB:PoolA",  # Conflicting token!
            "chain": "solana",
            "contract_address": "TokenA",
            "pool_address": "PoolA",
        }
        res = validate_and_build_identity(rec)
        assert res["is_valid"] is False
        assert res["rejection_reason"] == "CONFLICTING_CANONICAL_STRINGS"

    def test_identity_chain_ethereum_structural_chain_base_rejected(self):
        """Regression 35: Conflict between identity string chain and structural chain is rejected as CONFLICTING_CHAIN_IDENTITY."""
        rec = {
            "identity": "dex:ethereum:0x1111111111111111111111111111111111111111:0x2222222222222222222222222222222222222222",
            "chain": "base",  # Conflicting chain!
            "contract_address": "0x1111111111111111111111111111111111111111",
            "pool_address": "0x2222222222222222222222222222222222222222",
        }
        res = validate_and_build_identity(rec)
        assert res["is_valid"] is False
        assert res["rejection_reason"] == "CONFLICTING_CHAIN_IDENTITY"

    def test_matching_representations_accepted(self):
        """Regression 36: Identical representations across identity, market_key, and structural fields are accepted."""
        rec = {
            "identity": "dex:base:0xABCDEF1234567890ABCDEF1234567890ABCDEF12:0x2222222222222222222222222222222222222222",
            "market_key": "dex:base:0xabcdef1234567890abcdef1234567890abcdef12:0x2222222222222222222222222222222222222222",
            "chain": "base",
            "contract_address": "0xABCDEF1234567890ABCDEF1234567890ABCDEF12",
            "pool_address": "0x2222222222222222222222222222222222222222",
        }
        res = validate_and_build_identity(rec)
        assert res["is_valid"] is True
        assert res["canonical_identity"] == "dex:base:0xabcdef1234567890abcdef1234567890abcdef12:0x2222222222222222222222222222222222222222"

    def test_legacy_pipe_identity_strictly_rejected(self):
        """Regression 37: Legacy pipe-separated identity strings are rejected as LEGACY_IDENTITY_FORMAT."""
        rec = {
            "identity": "dex|base|token:0xabc|pool:0xdef",
            "chain": "base",
            "contract_address": "0xabc",
            "pool_address": "0xdef",
        }
        res = validate_and_build_identity(rec)
        assert res["is_valid"] is False
        assert res["rejection_reason"] == "LEGACY_IDENTITY_FORMAT"

    # --- ISSUE 3: Verifiable Outcome Evidence Provenance Tests ---

    def test_valid_trace_evidence_accepted(self):
        """Regression 38: Valid candle trace evidence with verified provenance, matching market, and interval is evaluated."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_trace_valid",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 50.0,
                "hours_to_target": 4.0,
                "candle_trace": make_valid_trace("dex:solana:TokenX:PoolA", t, duration_hours=24.0, mfe_pct=50.0, touch_hours=4.0),
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is True
        assert res["market_target_reached"] is True

    def test_absent_trace_provenance_rejected(self):
        """Regression 39: Candle trace lacking source provenance is rejected as MISSING_TRACE_PROVENANCE."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_no_prov",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 50.0,
                "hours_to_target": 4.0,
                "candle_trace": {
                    "trace_provenance": "",  # Absent provenance!
                    "market_key": "dex:solana:TokenX:PoolA",
                    "candle_interval": "1h",
                    "trace_start_utc": t.isoformat(),
                    "trace_end_utc": (t + dt.timedelta(hours=24)).isoformat(),
                },
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "MISSING_TRACE_PROVENANCE"

    def test_mismatched_market_provenance_rejected(self):
        """Regression 40: Candle trace tied to a different market is rejected as PROVENANCE_MARKET_MISMATCH."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_mismatch_prov",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 50.0,
                "hours_to_target": 4.0,
                "candle_trace": make_valid_trace("dex:solana:TokenOther:PoolOther", t, duration_hours=24.0, mfe_pct=50.0, touch_hours=4.0),
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "PROVENANCE_MARKET_MISMATCH"

    def test_incomplete_trace_rejected(self):
        """Regression 41: Candle trace with empty candle array is rejected as INCOMPLETE_TRACE."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_empty_candles",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 50.0,
                "hours_to_target": 4.0,
                "candle_trace": {
                    "trace_provenance": "gecko_terminal",
                    "market_key": "dex:solana:TokenX:PoolA",
                    "candle_interval": "1h",
                    "trace_start_utc": t.isoformat(),
                    "trace_end_utc": (t + dt.timedelta(hours=24)).isoformat(),
                    "candles": [],  # Empty candles list!
                },
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "INCOMPLETE_TRACE"

    def test_contradictory_trace_mfe_rejected(self):
        """Regression 42: Candle trace whose candle highs contradict claimed MFE is rejected as OUTCOME_EVIDENCE_CONFLICT."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_trace_mfe_conflict",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 60.0,  # Claims 60% MFE!
                "hours_to_target": 3.0,
                "candle_trace": {
                    "trace_provenance": "gecko_terminal",
                    "market_key": "dex:solana:TokenX:PoolA",
                    "candle_interval": "1h",
                    "trace_start_utc": t.isoformat(),
                    "trace_end_utc": (t + dt.timedelta(hours=24)).isoformat(),
                    "candles": [
                        {"timestamp_utc": t.isoformat(), "open": 1.0, "high": 1.20, "low": 0.95, "close": 1.15},  # Max high is 1.20 (only 20% MFE)!
                    ],
                },
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "OUTCOME_EVIDENCE_CONFLICT"
        assert "trace candle MFE" in res["resolution_reason"]

    def test_contradictory_trace_touch_time_rejected(self):
        """Regression 43: Candle trace whose touch candles contradict claimed touch time triggers OUTCOME_EVIDENCE_CONFLICT."""
        evaluator = ChronologicalBacktestEvaluator([])
        t = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        item = {
            "record_id": "rec_trace_touch_conflict",
            "market_key": "dex:solana:TokenX:PoolA",
            "timestamp": t,
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
                "chain": "solana",
                "contract_address": "TokenX",
                "pool_address": "PoolA",
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 50.0,
                "hours_to_target": 2.0,  # Claims touched at 2.0h!
                "candle_trace": {
                    "trace_provenance": "gecko_terminal",
                    "market_key": "dex:solana:TokenX:PoolA",
                    "candle_interval": "1h",
                    "trace_start_utc": t.isoformat(),
                    "trace_end_utc": (t + dt.timedelta(hours=24)).isoformat(),
                    "candles": [
                        {"timestamp_utc": (t + dt.timedelta(hours=1)).isoformat(), "high": 1.20},
                        {"timestamp_utc": (t + dt.timedelta(hours=8)).isoformat(), "high": 1.50},  # Target only touched at hour 8 (diff > 1.0h)!
                    ],
                },
            },
        }
        res = evaluator._resolve_outcome(item, lookup=None)
        assert res["is_evaluated"] is False
        assert res["outcome_classification"] == "OUTCOME_EVIDENCE_CONFLICT"
