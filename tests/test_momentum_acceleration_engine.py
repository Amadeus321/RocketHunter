"""Unit tests for Engine 1: Momentum Acceleration Engine (Phase 4).

Verifies:
1. Valid momentum velocity and acceleration calculations across 5m, 1h, 6h, 24h.
2. Geometric candle validation (negative prices, high < low, close > high, negative volume).
3. Sequence validation (duplicate timestamps, out-of-order timestamps).
4. Anti-lookahead protection (candle timestamp > as_of_timestamp strictly rejected).
5. Staleness detection across intervals.
6. Coverage threshold enforcement (< 0.80 fails closed to UNAVAILABLE).
7. Severe deceleration penalties.
8. Deterministic reproducibility under identical inputs.
9. Probability estimate strictly None (no invented probabilities).
10. Detailed MeasurementAttribution records emitted with correct units.
"""
from __future__ import annotations

import datetime as _dt
from datetime import datetime, timezone
import unittest

from scripts.momentum_acceleration_engine import (
    Candle,
    CandleSequenceValidator,
    ENGINE_1_NAME,
    ENGINE_1_VERSION,
    MomentumAccelerationEngine,
)
from scripts.signal_evidence_framework import (
    AssetIdentity,
    EngineConfig,
    EvidenceState,
    FreshnessStatus,
    ObservationQuality,
    SignalEngineInput,
)


class TestMomentumAccelerationEngine(unittest.TestCase):
    """Exhaustive unit test suite for Momentum Acceleration Engine."""

    def setUp(self) -> None:
        self.asset = AssetIdentity(
            chain="ethereum",
            token_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            pool_address="0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
            token_symbol="TEST",
        )
        self.engine = MomentumAccelerationEngine()
        self.base_epoch = 1763300000  # Fixed deterministic reference epoch
        self.as_of_dt = datetime.fromtimestamp(self.base_epoch, tz=timezone.utc)

    def _generate_synthetic_candles(
        self,
        count: int,
        step_seconds: int,
        start_price: float,
        end_price: float,
        end_epoch: int,
    ) -> list[dict]:
        """Generates continuous geometric candles smoothly trending from start to end price."""
        candles = []
        start_epoch = end_epoch - (count - 1) * step_seconds
        price_step = (end_price - start_price) / max(1, count - 1)

        for i in range(count):
            ts = start_epoch + i * step_seconds
            p = start_price + i * price_step
            # Synthetic candle with small high/low spread
            c = {
                "timestamp": ts,
                "open": round(p * 0.998, 4),
                "high": round(p * 1.005, 4),
                "low": round(p * 0.995, 4),
                "close": round(p * 1.002, 4),
                "volume": 1000.0,
            }
            candles.append(c)
        return candles

    def test_geometric_candle_validation(self) -> None:
        """Invalid, non-finite, negative or geometrically impossible candles must be rejected."""
        # Non-positive open
        with self.assertRaises(ValueError):
            Candle.from_dict({"timestamp": 100, "open": 0.0, "high": 10.0, "low": 5.0, "close": 8.0, "volume": 10})

        # Negative price
        with self.assertRaises(ValueError):
            Candle.from_dict({"timestamp": 100, "open": -1.0, "high": 10.0, "low": 5.0, "close": 8.0, "volume": 10})

        # High < Low
        with self.assertRaises(ValueError):
            Candle.from_dict({"timestamp": 100, "open": 10.0, "high": 8.0, "low": 9.0, "close": 8.5, "volume": 10})

        # Open > High
        with self.assertRaises(ValueError):
            Candle.from_dict({"timestamp": 100, "open": 12.0, "high": 10.0, "low": 5.0, "close": 8.0, "volume": 10})

        # Close < Low
        with self.assertRaises(ValueError):
            Candle.from_dict({"timestamp": 100, "open": 8.0, "high": 10.0, "low": 5.0, "close": 4.0, "volume": 10})

        # Negative volume
        with self.assertRaises(ValueError):
            Candle.from_dict({"timestamp": 100, "open": 8.0, "high": 10.0, "low": 5.0, "close": 9.0, "volume": -10})

    def test_sequence_validator_anti_lookahead(self) -> None:
        """Any candle timestamp ahead of as_of_epoch must be unconditionally rejected."""
        # One candle at future epoch: base_epoch + 60
        raw = [
            {"timestamp": self.base_epoch - 300, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100},
            {"timestamp": self.base_epoch + 60, "open": 10.5, "high": 12, "low": 10, "close": 11.5, "volume": 100},
        ]
        parsed, errs = CandleSequenceValidator.validate_and_parse_candles(raw, self.base_epoch)
        self.assertEqual(len(parsed), 0)
        self.assertTrue(any("FUTURE_TIMESTAMP_LOOKAHEAD_REJECTED" in e for e in errs))

    def test_sequence_validator_duplicates_and_out_of_order(self) -> None:
        """Duplicate timestamps and descending timestamps must be rejected."""
        # Duplicates
        raw_dup = [
            {"timestamp": self.base_epoch - 600, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100},
            {"timestamp": self.base_epoch - 600, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100},
        ]
        parsed_dup, errs_dup = CandleSequenceValidator.validate_and_parse_candles(raw_dup, self.base_epoch)
        self.assertEqual(len(parsed_dup), 0)
        self.assertTrue(any("DUPLICATE_CANDLE_TIMESTAMP" in e for e in errs_dup))

        # Out of order
        raw_order = [
            {"timestamp": self.base_epoch - 300, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100},
            {"timestamp": self.base_epoch - 600, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100},
        ]
        parsed_order, errs_order = CandleSequenceValidator.validate_and_parse_candles(raw_order, self.base_epoch)
        self.assertEqual(len(parsed_order), 0)
        self.assertTrue(any("OUT_OF_ORDER_CANDLE_SEQUENCE" in e for e in errs_order))

    def test_valid_momentum_acceleration_evaluation(self) -> None:
        """Evaluates high-velocity momentum expansion across all 4 intervals."""
        # 5m candles: 12 candles over last 1h (step 300s). Recent candles accelerate sharply
        c_5m = self._generate_synthetic_candles(
            count=12, step_seconds=300, start_price=100.0, end_price=130.0, end_epoch=self.base_epoch
        )
        # Give the last 5m candle an explosive surge
        c_5m[-1] = {
            "timestamp": self.base_epoch,
            "open": 120.0,
            "high": 131.0,
            "low": 119.5,
            "close": 130.0,
            "volume": 5000.0,
        }

        # 1h candles: 6 candles over last 6h (step 3600s), moving from $95 to $130
        c_1h = self._generate_synthetic_candles(
            count=6, step_seconds=3600, start_price=95.0, end_price=130.0, end_epoch=self.base_epoch
        )
        # 6h candles: 4 candles over last 24h (step 21600s), moving from $85 to $130
        c_6h = self._generate_synthetic_candles(
            count=4, step_seconds=21600, start_price=85.0, end_price=130.0, end_epoch=self.base_epoch
        )
        # 24h candles: 2 candles (step 86400s), moving from $80 to $130
        c_24h = self._generate_synthetic_candles(
            count=2, step_seconds=86400, start_price=80.0, end_price=130.0, end_epoch=self.base_epoch
        )

        payloads = {
            "candles_5m": c_5m,
            "candles_1h": c_1h,
            "candles_6h": c_6h,
            "candles_24h": c_24h,
        }

        engine_input = SignalEngineInput(
            asset=self.asset,
            as_of_timestamp=self.as_of_dt,
            run_id="run_test_valid",
            forecast_id="fc_test_valid",
            raw_payloads=payloads,
        )

        record = self.engine.evaluate(engine_input)

        self.assertEqual(record.engine_name, ENGINE_1_NAME)
        self.assertEqual(record.overall_evidence_state, EvidenceState.VERIFIED)
        self.assertEqual(record.overall_observation_quality, ObservationQuality.HIGH)
        self.assertIsNotNone(record.score)
        self.assertGreater(record.score, 50.0)  # Strong momentum breakout expansion
        self.assertIsNotNone(record.confidence)
        self.assertGreaterEqual(record.confidence, 0.8)
        self.assertIsNone(record.probability_estimate)  # Never invented
        self.assertEqual(record.eligibility_verdict, "ELIGIBLE")

        # Verify individual measurements
        measurement_names = {m.measurement_name for m in record.measurements}
        self.assertIn("price_velocity_5m", measurement_names)
        self.assertIn("price_velocity_1h", measurement_names)
        self.assertIn("price_velocity_6h", measurement_names)
        self.assertIn("price_velocity_24h", measurement_names)
        self.assertIn("acceleration_micro_5m_vs_1h", measurement_names)
        self.assertIn("acceleration_medium_1h_vs_6h", measurement_names)
        self.assertIn("acceleration_macro_6h_vs_24h", measurement_names)

    def test_stale_candles_detected_and_penalized(self) -> None:
        """Stale candle data exceeding allowable staleness thresholds fails closed."""
        # Candles ending 2 hours before as_of_epoch (stale for 5m interval, max staleness is 900s)
        stale_epoch = self.base_epoch - 7200
        c_5m_stale = self._generate_synthetic_candles(
            count=12, step_seconds=300, start_price=100.0, end_price=110.0, end_epoch=stale_epoch
        )
        c_1h = self._generate_synthetic_candles(
            count=6, step_seconds=3600, start_price=90.0, end_price=120.0, end_epoch=self.base_epoch
        )
        c_6h = self._generate_synthetic_candles(
            count=4, step_seconds=21600, start_price=80.0, end_price=120.0, end_epoch=self.base_epoch
        )
        c_24h = self._generate_synthetic_candles(
            count=2, step_seconds=86400, start_price=75.0, end_price=120.0, end_epoch=self.base_epoch
        )

        payloads = {
            "candles_5m": c_5m_stale,
            "candles_1h": c_1h,
            "candles_6h": c_6h,
            "candles_24h": c_24h,
        }

        engine_input = SignalEngineInput(
            asset=self.asset,
            as_of_timestamp=self.as_of_dt,
            run_id="run_stale",
            forecast_id="fc_stale",
            raw_payloads=payloads,
        )

        record = self.engine.evaluate(engine_input)
        self.assertEqual(record.overall_evidence_state, EvidenceState.UNAVAILABLE)
        self.assertIsNone(record.score)
        self.assertEqual(record.eligibility_verdict, "UNAVAILABLE")
        self.assertTrue(any("INTERVAL_5m_UNVERIFIED" in ex for ex in record.exclusion_reasons))

    def test_insufficient_interval_coverage_fails_closed(self) -> None:
        """Interval coverage below 80% fails closed without price hallucination."""
        # For 1h window with 300s step candles, expected count is 12. Only provide 2 candles (16.7% coverage)
        c_5m = self._generate_synthetic_candles(
            count=12, step_seconds=300, start_price=100.0, end_price=120.0, end_epoch=self.base_epoch
        )
        c_1h_sparse = self._generate_synthetic_candles(
            count=2, step_seconds=300, start_price=90.0, end_price=120.0, end_epoch=self.base_epoch
        )
        c_6h = self._generate_synthetic_candles(
            count=4, step_seconds=21600, start_price=80.0, end_price=120.0, end_epoch=self.base_epoch
        )
        c_24h = self._generate_synthetic_candles(
            count=2, step_seconds=86400, start_price=75.0, end_price=120.0, end_epoch=self.base_epoch
        )

        payloads = {
            "candles_5m": c_5m,
            "candles_1h": c_1h_sparse,
            "candles_6h": c_6h,
            "candles_24h": c_24h,
        }

        engine_input = SignalEngineInput(
            asset=self.asset,
            as_of_timestamp=self.as_of_dt,
            run_id="run_sparse",
            forecast_id="fc_sparse",
            raw_payloads=payloads,
        )

        record = self.engine.evaluate(engine_input)
        self.assertEqual(record.overall_evidence_state, EvidenceState.UNAVAILABLE)
        self.assertIsNone(record.score)
        self.assertTrue(any("INSUFFICIENT_COVERAGE" in ex for ex in record.exclusion_reasons))

    def test_severe_deceleration_penalty(self) -> None:
        """When price velocity is sharply dropping across intervals, score is heavily penalized."""
        # Price dumped recently: 5m negative return, 1h negative return
        c_5m = self._generate_synthetic_candles(
            count=12, step_seconds=300, start_price=120.0, end_price=100.0, end_epoch=self.base_epoch
        )
        c_1h = self._generate_synthetic_candles(
            count=6, step_seconds=3600, start_price=130.0, end_price=100.0, end_epoch=self.base_epoch
        )
        c_6h = self._generate_synthetic_candles(
            count=4, step_seconds=21600, start_price=110.0, end_price=100.0, end_epoch=self.base_epoch
        )
        c_24h = self._generate_synthetic_candles(
            count=2, step_seconds=86400, start_price=100.0, end_price=100.0, end_epoch=self.base_epoch
        )

        payloads = {
            "candles_5m": c_5m,
            "candles_1h": c_1h,
            "candles_6h": c_6h,
            "candles_24h": c_24h,
        }

        engine_input = SignalEngineInput(
            asset=self.asset,
            as_of_timestamp=self.as_of_dt,
            run_id="run_dump",
            forecast_id="fc_dump",
            raw_payloads=payloads,
        )

        record = self.engine.evaluate(engine_input)
        self.assertEqual(record.overall_evidence_state, EvidenceState.VERIFIED)
        self.assertIsNotNone(record.score)
        self.assertEqual(record.score, 0.0)  # Severe dump yields 0.0 score
        self.assertEqual(record.eligibility_verdict, "INELIGIBLE")

    def test_reproducibility_under_identical_inputs(self) -> None:
        """Evaluating the same inputs multiple times produces bit-for-bit identical results."""
        c_5m = self._generate_synthetic_candles(12, 300, 100.0, 110.0, self.base_epoch)
        c_1h = self._generate_synthetic_candles(6, 3600, 95.0, 110.0, self.base_epoch)
        c_6h = self._generate_synthetic_candles(4, 21600, 90.0, 110.0, self.base_epoch)
        c_24h = self._generate_synthetic_candles(2, 86400, 85.0, 110.0, self.base_epoch)

        payloads = {"candles_5m": c_5m, "candles_1h": c_1h, "candles_6h": c_6h, "candles_24h": c_24h}

        inp1 = SignalEngineInput(self.asset, self.as_of_dt, "run_id_1", "fc_id_1", payloads)
        inp2 = SignalEngineInput(self.asset, self.as_of_dt, "run_id_1", "fc_id_1", payloads)

        res1 = self.engine.evaluate(inp1)
        res2 = self.engine.evaluate(inp2)

        self.assertEqual(res1.score, res2.score)
        self.assertEqual(res1.confidence, res2.confidence)
        self.assertEqual(res1.reproducible_input_hash, res2.reproducible_input_hash)
        self.assertEqual(len(res1.measurements), len(res2.measurements))
        for m1, m2 in zip(res1.measurements, res2.measurements):
            self.assertEqual(m1.value, m2.value)
            self.assertEqual(m1.units, m2.units)

    def test_parameter_change_updates_config_fingerprint(self) -> None:
        """Modifying weights or thresholds updates configuration fingerprint deterministically."""
        cfg_default = EngineConfig(ENGINE_1_NAME, ENGINE_1_VERSION, {"min_coverage_ratio": 0.80})
        cfg_custom = EngineConfig(ENGINE_1_NAME, ENGINE_1_VERSION, {"min_coverage_ratio": 0.90})

        eng_default = MomentumAccelerationEngine(cfg_default)
        eng_custom = MomentumAccelerationEngine(cfg_custom)

        self.assertNotEqual(eng_default.config.config_fingerprint, eng_custom.config.config_fingerprint)


if __name__ == "__main__":
    unittest.main()
