import math
import pytest
from scripts.rocket_hunt import score_candidate


class TestRocketHuntReversal:
    def test_strong_24h_move_with_negative_1h_applies_reversal_penalty(self):
        # 500% 24h gain with -5% 1h pullback
        row_with_pullback = {
            "change_24h_pct": 550.0,
            "change_6h_pct": 50.0,
            "change_1h_pct": -5.0,
            "volume_24h_usd": 100_000,
            "liquidity_usd": 50_000,
            "txns_24h": 500,
            "source": "gecko",
            "age_hours": 10,
        }
        # Exactly identical row but with 0% 1h movement
        row_flat_1h = dict(row_with_pullback, change_1h_pct=0.0)

        score_pullback = score_candidate(row_with_pullback)
        score_flat = score_candidate(row_flat_1h)

        # score_flat has 0 momentum for 1h and 0 penalty for 1h reversal.
        # score_pullback has 0 momentum for 1h (clamped) and 4.0 penalty for reversal.
        assert score_flat - score_pullback == pytest.approx(4.0, abs=1e-2)

    def test_positive_1h_movement_receives_momentum_and_no_penalty(self):
        row = {
            "change_24h_pct": 550.0,
            "change_6h_pct": 50.0,
            "change_1h_pct": 25.0,
            "volume_24h_usd": 100_000,
            "liquidity_usd": 50_000,
            "txns_24h": 500,
            "source": "gecko",
            "age_hours": 10,
        }
        row_flat = dict(row, change_1h_pct=0.0)

        score_pos = score_candidate(row)
        score_flat = score_candidate(row_flat)

        # Positive 1h adds momentum (25/150 * 14 = 2.333...) and has no penalty
        expected_diff = (25.0 / 150.0) * 14.0
        assert score_pos - score_flat == pytest.approx(expected_diff, abs=1e-2)

    def test_exactly_zero_movement_receives_no_penalty(self):
        row_zero = {
            "change_24h_pct": 550.0,
            "change_6h_pct": 50.0,
            "change_1h_pct": 0.0,
            "volume_24h_usd": 100_000,
            "liquidity_usd": 50_000,
            "txns_24h": 500,
            "source": "gecko",
            "age_hours": 10,
        }
        score = score_candidate(row_zero)
        # Momentum for 1h is 0, no penalty applied
        assert score > 0

    def test_missing_1h_measurement_receives_no_penalty(self):
        row_none = {
            "change_24h_pct": 550.0,
            "change_6h_pct": 50.0,
            "change_1h_pct": None,
            "volume_24h_usd": 100_000,
            "liquidity_usd": 50_000,
            "txns_24h": 500,
            "source": "gecko",
            "age_hours": 10,
        }
        row_empty = dict(row_none, change_1h_pct="")

        score_none = score_candidate(row_none)
        score_empty = score_candidate(row_empty)
        assert score_none == score_empty
        # Should match score with 0.0 change (no penalty)
        row_zero = dict(row_none, change_1h_pct=0.0)
        assert score_none == score_candidate(row_zero)

    def test_invalid_and_non_finite_measurements_handled_gracefully(self):
        base_row = {
            "change_24h_pct": 550.0,
            "change_6h_pct": 50.0,
            "volume_24h_usd": 100_000,
            "liquidity_usd": 50_000,
            "txns_24h": 500,
            "source": "gecko",
            "age_hours": 10,
        }
        score_nan_str = score_candidate(dict(base_row, change_1h_pct="NaN"))
        score_nan_float = score_candidate(dict(base_row, change_1h_pct=float("nan")))
        score_inf = score_candidate(dict(base_row, change_1h_pct=float("inf")))
        score_neg_inf = score_candidate(dict(base_row, change_1h_pct=float("-inf")))
        score_invalid = score_candidate(dict(base_row, change_1h_pct="invalid_string"))

        expected_baseline = score_candidate(dict(base_row, change_1h_pct=None))

        assert score_nan_str == expected_baseline
        assert score_nan_float == expected_baseline
        assert score_inf == expected_baseline
        assert score_neg_inf == expected_baseline
        assert score_invalid == expected_baseline
