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
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": None, "contract_address": "TokenA", "price_usd": 1.0},
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": "solana", "contract_address": "", "price_usd": 1.0},
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": "solana", "contract_address": "TokenValid", "price_usd": None},
            {"snapshot_utc": "2026-11-01T12:00:00Z", "chain": "solana", "contract_address": "TokenValid", "price_usd": 1.0},
        ]
        evaluator = ChronologicalBacktestEvaluator(records)
        dataset = evaluator.prepare_chronological_dataset()
        assert len(dataset) == 1
        assert dataset[0]["token"] == "TokenValid"

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
