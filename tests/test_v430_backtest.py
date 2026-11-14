import datetime as dt
import pytest
from scripts.chronological_backtest import ChronologicalBacktestEvaluator


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
            records.append({
                "snapshot_utc": t.isoformat(),
                "chain": "solana" if i % 2 == 0 else "base",
                "contract_address": f"Token{i}",
                "pool_address": f"Pool{i}",
                "price_usd": 1.0,
                "early_breakout_score": score,
                "liquidity_usd": 50_000 * (i + 1),
                "age_hours": 12.0,
                "mfe_pct": 45.0 if hit_tp else 15.0,
                "mae_pct": -8.0,
                "outcome_classification": "TARGET_REACHED" if hit_tp else "TARGET_NOT_REACHED",
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
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {},
        }
        item_pool_b = {
            "record_id": "rec_pool_b",
            "market_key": "dex:solana:TokenX:PoolB",
            "exact_identity": "dex:solana:TokenX:PoolB",
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
            },
            # If an attempt is made to join Pool B with Pool A's outcome, it must be rejected!
            "rec_pool_b": {
                "exact_identity": "dex:solana:TokenX:PoolA",  # Wrong pool!
                "outcome_classification": "TARGET_REACHED",
                "target_reached": True,
                "mfe_pct": 55.0,
            },
        }

        res_a = evaluator._resolve_outcome(item_pool_a, outcome_lookup)
        assert res_a["is_evaluated"] is True
        assert res_a["target_reached"] is True

        res_b = evaluator._resolve_outcome(item_pool_b, outcome_lookup)
        assert res_b["is_evaluated"] is False
        assert res_b["outcome_classification"] == "CONFLICTING_IDENTITY_REJECTED"
        assert "IDENTITY_MISMATCH" in res_b["resolution_reason"]

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
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "score": 88.0,
            "raw_record": {
                "outcome_classification": "TARGET_REACHED",
                "mfe_pct": 65.0,
                "price_usd_at_outcome": 2.5,  # Future price!
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
            "timestamp_utc": "2026-11-01T12:00:00Z",
            "detection_price": 1.0,
            "raw_record": {
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
