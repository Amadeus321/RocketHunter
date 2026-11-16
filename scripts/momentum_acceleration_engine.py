"""Engine 1: Momentum Acceleration Engine (Phase 4).

Quantifies changes in price velocity and detects explosive momentum expansion
across multi-resolution OHLCV intervals (5 minutes, 1 hour, 6 hours, 24 hours).

Invariants & Rules:
1. Strict OHLCV Geometric Validation: Rejects invalid, non-finite, zero, negative,
   or geometrically impossible candle tuples (e.g., high < low, close < low, open > high).
2. Sequence Integrity: Validates strict ascending timestamp order, detects duplicates,
   and detects sequence gaps exceeding allowable continuity tolerance.
3. Anti-Lookahead Defense: Any candle with timestamp > as_of_timestamp is unconditionally
   rejected with FUTURE_TIMESTAMP_LOOKAHEAD_REJECTED.
4. No Price Hallucination: Missing candles or gaps are never manufactured or substituted
   with guessed values; coverage ratio is penalized and missing intervals fail closed.
5. Attribution: Emits individual MeasurementAttribution records for every interval
   velocity, acceleration tier, and coverage metric.
6. Empirical Confidence Only: Zero invented probability calibration; probability_estimate
   remains strictly None.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import hashlib
import json
import math
import statistics
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from scripts.signal_evidence_framework import (
        AssetIdentity,
        BaseDetectionEngine,
        BlockchainAuthenticationStatus,
        EngineConfig,
        EvidenceState,
        FreshnessStatus,
        MeasurementAttribution,
        ObservationQuality,
        SignalEngineInput,
        SignalEvidenceRecord,
    )
except ImportError:
    from signal_evidence_framework import (
        AssetIdentity,
        BaseDetectionEngine,
        BlockchainAuthenticationStatus,
        EngineConfig,
        EvidenceState,
        FreshnessStatus,
        MeasurementAttribution,
        ObservationQuality,
        SignalEngineInput,
        SignalEvidenceRecord,
    )

ENGINE_1_NAME = "Engine_1_Momentum_Acceleration"
ENGINE_1_VERSION = "4.30.0"


# ==============================================================================
# 1. OHLCV Candle Data Model & Geometry Validator
# ==============================================================================

@dataclasses.dataclass(frozen=True)
class Candle:
    """Immutable OHLCV candle record."""
    timestamp_epoch: int   # UNIX timestamp in seconds
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Candle:
        raw_ts = data.get("timestamp") or data.get("time") or data.get("timestamp_epoch") or data.get("t")
        if raw_ts is None:
            raise ValueError("Candle missing timestamp")
        try:
            ts = int(raw_ts)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid candle timestamp: {raw_ts}")

        try:
            o = float(data.get("open") if data.get("open") is not None else data.get("o"))
            h = float(data.get("high") if data.get("high") is not None else data.get("h"))
            l = float(data.get("low") if data.get("low") is not None else data.get("l"))
            c = float(data.get("close") if data.get("close") is not None else data.get("c"))
            v = float(data.get("volume") if data.get("volume") is not None else data.get("v", 0.0))
        except (ValueError, TypeError) as e:
            raise ValueError(f"Non-numeric candle values in {data}: {e}")

        # Geometric validity checks
        for val, name in [(o, "open"), (h, "high"), (l, "low"), (c, "close"), (v, "volume")]:
            if not math.isfinite(val):
                raise ValueError(f"Non-finite candle value for {name}: {val}")

        if o <= 0.0 or h <= 0.0 or l <= 0.0 or c <= 0.0:
            raise ValueError(f"Non-positive price in candle: O={o}, H={h}, L={l}, C={c}")
        if v < 0.0:
            raise ValueError(f"Negative volume in candle: V={v}")

        if h < l:
            raise ValueError(f"Geometrically impossible: high ({h}) < low ({l})")
        if h < o or h < c:
            raise ValueError(f"Geometrically impossible: high ({h}) < open ({o}) or close ({c})")
        if l > o or l > c:
            raise ValueError(f"Geometrically impossible: low ({l}) > open ({o}) or close ({c})")

        return cls(timestamp_epoch=ts, open=o, high=h, low=l, close=c, volume=v)


# ==============================================================================
# 2. Sequence Validation & Interval Analysis
# ==============================================================================

@dataclasses.dataclass
class IntervalAnalysisResult:
    """Outcome of validating and measuring price return across a specific interval."""
    interval_name: str
    duration_hours: float
    candle_count: int
    expected_candle_count: int
    coverage_ratio: float
    start_price: Optional[float]
    end_price: Optional[float]
    return_pct: Optional[float]
    hourly_velocity_pct: Optional[float]
    freshness: FreshnessStatus
    evidence_state: EvidenceState
    quality: ObservationQuality
    reason_code: str
    latest_candle_timestamp: Optional[int]


class CandleSequenceValidator:
    """Validates chronological order, continuity, and anti-lookahead rules."""

    @staticmethod
    def validate_and_parse_candles(
        raw_candles: List[Any],
        as_of_epoch: int,
    ) -> Tuple[List[Candle], List[str]]:
        """Parses candles, validates geometry, checks ascending timestamps, and rejects future data."""
        parsed: List[Candle] = []
        errors: List[str] = []

        if not raw_candles:
            return [], ["EMPTY_CANDLE_SEQUENCE"]

        for idx, item in enumerate(raw_candles):
            try:
                if isinstance(item, Candle):
                    c = item
                elif isinstance(item, dict):
                    c = Candle.from_dict(item)
                else:
                    errors.append(f"UNSUPPORTED_CANDLE_TYPE_AT_INDEX_{idx}")
                    continue
                parsed.append(c)
            except Exception as e:
                errors.append(f"CANDLE_VALIDATION_ERROR_AT_INDEX_{idx}: {e}")

        if errors:
            return [], errors

        # Sort verification and anomaly detection
        seen_timestamps = set()
        for i in range(len(parsed)):
            c = parsed[i]

            # Anti-lookahead guard: strictly reject future timestamps
            if c.timestamp_epoch > as_of_epoch:
                return [], [f"FUTURE_TIMESTAMP_LOOKAHEAD_REJECTED: candle_ts={c.timestamp_epoch} > as_of={as_of_epoch}"]

            if c.timestamp_epoch in seen_timestamps:
                return [], [f"DUPLICATE_CANDLE_TIMESTAMP_DETECTED: ts={c.timestamp_epoch}"]
            seen_timestamps.add(c.timestamp_epoch)

            if i > 0 and c.timestamp_epoch <= parsed[i - 1].timestamp_epoch:
                return [], [f"OUT_OF_ORDER_CANDLE_SEQUENCE: ts[{i}]={c.timestamp_epoch} <= ts[{i-1}]={parsed[i-1].timestamp_epoch}"]

        return parsed, []


# ==============================================================================
# 3. Engine 1 Implementation: Momentum Acceleration
# ==============================================================================

class MomentumAccelerationEngine(BaseDetectionEngine):
    """Engine 1: Multi-interval velocity change and momentum expansion evaluator."""

    def __init__(self, config: Optional[EngineConfig] = None) -> None:
        if config is None:
            default_params = {
                "min_coverage_ratio": 0.80,
                "max_staleness_5m_seconds": 900,     # 15 minutes max staleness for 5m interval
                "max_staleness_1h_seconds": 5400,    # 90 minutes max staleness for 1h interval
                "max_staleness_6h_seconds": 28800,   # 8 hours max staleness for 6h interval
                "max_staleness_24h_seconds": 97200,  # 27 hours max staleness for 24h interval
                "w_accel_micro": 0.35,               # Weight for 5m vs 1h velocity change
                "w_accel_medium": 0.35,              # Weight for 1h vs 6h velocity change
                "w_velocity_1h": 0.20,               # Weight for absolute 1h velocity level
                "w_accel_macro": 0.10,               # Weight for 6h vs 24h velocity change
                "score_scale_denominator": 15.0,     # Denominator to scale raw score to 0..100
                "min_score_eligible": 50.0,
            }
            config = EngineConfig(
                engine_name=ENGINE_1_NAME,
                engine_version=ENGINE_1_VERSION,
                parameters=default_params,
            )
        super().__init__(config)

    @property
    def engine_name(self) -> str:
        return ENGINE_1_NAME

    @property
    def engine_version(self) -> str:
        return ENGINE_1_VERSION

    def compute_code_fingerprint(self) -> str:
        """Computes deterministic hash of this engine's algorithmic signature."""
        sig = f"{ENGINE_1_NAME}:{ENGINE_1_VERSION}:v_hourly:accel_multi_tier:anti_lookahead:geom_strict"
        return hashlib.sha256(sig.encode("utf-8")).hexdigest()

    def validate_inputs(self, engine_input: SignalEngineInput) -> Tuple[bool, List[str]]:
        """Validates presence of candles, asset identity, and timestamp bounds."""
        errors: List[str] = []
        if not engine_input.asset:
            errors.append("MISSING_ASSET_IDENTITY")

        if not engine_input.as_of_timestamp:
            errors.append("MISSING_AS_OF_TIMESTAMP")

        payloads = engine_input.raw_payloads
        if not payloads:
            errors.append("EMPTY_RAW_PAYLOADS")
            return False, errors

        # Expect either multi-interval dict or unified candle list
        has_candles = (
            "candles" in payloads
            or "candles_5m" in payloads
            or "candles_1h" in payloads
            or "candles_6h" in payloads
            or "candles_24h" in payloads
        )
        if not has_candles:
            errors.append("NO_CANDLE_DATA_FOUND_IN_PAYLOADS")

        return len(errors) == 0, errors

    def _determine_step_seconds(self, candles: List[Candle], fallback_step: int) -> int:
        """Determines the representative step size of a candle series using median delta."""
        if len(candles) < 2:
            return fallback_step
        diffs = [candles[i].timestamp_epoch - candles[i - 1].timestamp_epoch for i in range(1, len(candles))]
        valid_diffs = [d for d in diffs if d > 0]
        if not valid_diffs:
            return fallback_step
        return int(statistics.median(valid_diffs))

    def _analyze_interval_from_candles(
        self,
        candles: List[Candle],
        interval_name: str,
        duration_seconds: int,
        default_step_seconds: int,
        as_of_epoch: int,
        max_staleness_seconds: int,
        min_coverage_ratio: float,
    ) -> IntervalAnalysisResult:
        """Evaluates price return, velocity, continuity and freshness over a given interval."""
        duration_hours = duration_seconds / 3600.0

        if not candles:
            return IntervalAnalysisResult(
                interval_name=interval_name,
                duration_hours=duration_hours,
                candle_count=0,
                expected_candle_count=max(1, int(round(duration_seconds / default_step_seconds))),
                coverage_ratio=0.0,
                start_price=None,
                end_price=None,
                return_pct=None,
                hourly_velocity_pct=None,
                freshness=FreshnessStatus.EXPIRED,
                evidence_state=EvidenceState.MISSING,
                quality=ObservationQuality.UNAVAILABLE,
                reason_code="ZERO_CANDLES_PROVIDED",
                latest_candle_timestamp=None,
            )

        step_seconds = self._determine_step_seconds(candles, default_step_seconds)
        # For 5m interval, minimum expected count is 1; for multi-candle intervals, duration / step
        expected_count = max(1, int(round(duration_seconds / max(1, step_seconds))))

        # Filter candles within [as_of_epoch - duration_seconds, as_of_epoch]
        start_bound = as_of_epoch - duration_seconds
        in_window = [c for c in candles if start_bound <= c.timestamp_epoch <= as_of_epoch]

        if not in_window:
            latest_c = candles[-1]
            staleness = as_of_epoch - latest_c.timestamp_epoch
            return IntervalAnalysisResult(
                interval_name=interval_name,
                duration_hours=duration_hours,
                candle_count=0,
                expected_candle_count=expected_count,
                coverage_ratio=0.0,
                start_price=None,
                end_price=None,
                return_pct=None,
                hourly_velocity_pct=None,
                freshness=FreshnessStatus.STALE if staleness > max_staleness_seconds else FreshnessStatus.WARM,
                evidence_state=EvidenceState.MISSING,
                quality=ObservationQuality.UNAVAILABLE,
                reason_code="NO_CANDLES_IN_SPECIFIED_TIME_WINDOW",
                latest_candle_timestamp=latest_c.timestamp_epoch,
            )

        latest_c = in_window[-1]
        staleness = as_of_epoch - latest_c.timestamp_epoch

        if staleness > max_staleness_seconds:
            freshness = FreshnessStatus.STALE
        elif staleness > (step_seconds * 1.5):
            freshness = FreshnessStatus.WARM
        else:
            freshness = FreshnessStatus.FRESH

        count = len(in_window)
        coverage_ratio = min(1.0, count / float(expected_count))

        # Check coverage
        if coverage_ratio < min_coverage_ratio:
            return IntervalAnalysisResult(
                interval_name=interval_name,
                duration_hours=duration_hours,
                candle_count=count,
                expected_candle_count=expected_count,
                coverage_ratio=coverage_ratio,
                start_price=in_window[0].open,
                end_price=in_window[-1].close,
                return_pct=None,
                hourly_velocity_pct=None,
                freshness=freshness,
                evidence_state=EvidenceState.UNAVAILABLE,
                quality=ObservationQuality.DEGRADED,
                reason_code=f"INSUFFICIENT_COVERAGE_RATIO_{coverage_ratio:.2f}_BELOW_{min_coverage_ratio:.2f}",
                latest_candle_timestamp=latest_c.timestamp_epoch,
            )

        if freshness == FreshnessStatus.STALE:
            return IntervalAnalysisResult(
                interval_name=interval_name,
                duration_hours=duration_hours,
                candle_count=count,
                expected_candle_count=expected_count,
                coverage_ratio=coverage_ratio,
                start_price=in_window[0].open,
                end_price=in_window[-1].close,
                return_pct=None,
                hourly_velocity_pct=None,
                freshness=freshness,
                evidence_state=EvidenceState.STALE,
                quality=ObservationQuality.LOW,
                reason_code=f"CANDLE_STALENESS_{staleness}S_EXCEEDS_MAX_{max_staleness_seconds}S",
                latest_candle_timestamp=latest_c.timestamp_epoch,
            )

        # Calculate return and normalized velocity
        start_p = in_window[0].open
        end_p = in_window[-1].close
        ret_pct = ((end_p - start_p) / start_p) * 100.0

        # Hourly normalized velocity = Return % / Duration in Hours
        hourly_velocity = ret_pct / duration_hours

        quality = ObservationQuality.HIGH if coverage_ratio >= 0.95 else ObservationQuality.MEDIUM

        return IntervalAnalysisResult(
            interval_name=interval_name,
            duration_hours=duration_hours,
            candle_count=count,
            expected_candle_count=expected_count,
            coverage_ratio=coverage_ratio,
            start_price=start_p,
            end_price=end_p,
            return_pct=ret_pct,
            hourly_velocity_pct=hourly_velocity,
            freshness=freshness,
            evidence_state=EvidenceState.VERIFIED,
            quality=quality,
            reason_code="INTERVAL_EVALUATED_SUCCESSFULLY",
            latest_candle_timestamp=latest_c.timestamp_epoch,
        )

    def calculate_features(self, engine_input: SignalEngineInput) -> Dict[str, Any]:
        """Calculates velocities, accelerations, and attributions across 5m, 1h, 6h, 24h."""
        as_of_epoch = int(engine_input.as_of_timestamp.astimezone(timezone.utc).timestamp())
        as_of_str = engine_input.as_of_timestamp.astimezone(timezone.utc).isoformat()
        payloads = engine_input.raw_payloads

        min_cov = float(self.config.get("min_coverage_ratio", 0.80))
        max_stale_5m = int(self.config.get("max_staleness_5m_seconds", 900))
        max_stale_1h = int(self.config.get("max_staleness_1h_seconds", 5400))
        max_stale_6h = int(self.config.get("max_staleness_6h_seconds", 28800))
        max_stale_24h = int(self.config.get("max_staleness_24h_seconds", 97200))

        # Check for lookahead / sequence errors across provided candle sets
        validation_errors: List[str] = []

        def parse_series(key: str) -> List[Candle]:
            raw = payloads.get(key, [])
            c_list, errs = CandleSequenceValidator.validate_and_parse_candles(raw, as_of_epoch)
            if errs:
                validation_errors.extend(errs)
            return c_list

        candles_5m = parse_series("candles_5m") if "candles_5m" in payloads else []
        candles_1h = parse_series("candles_1h") if "candles_1h" in payloads else []
        candles_6h = parse_series("candles_6h") if "candles_6h" in payloads else []
        candles_24h = parse_series("candles_24h") if "candles_24h" in payloads else []

        # If uniform 'candles' series supplied without interval breakdown:
        if "candles" in payloads and not (candles_5m or candles_1h or candles_6h or candles_24h):
            base_candles = parse_series("candles")
            candles_5m = base_candles
            candles_1h = base_candles
            candles_6h = base_candles
            candles_24h = base_candles

        if validation_errors:
            return {
                "validation_errors": validation_errors,
                "measurements": [],
                "coverage_completeness": 0.0,
            }

        # Analyze each interval
        res_5m = self._analyze_interval_from_candles(
            candles_5m, "5m", 300, 300, as_of_epoch, max_stale_5m, min_cov
        )
        res_1h = self._analyze_interval_from_candles(
            candles_1h, "1h", 3600, 3600, as_of_epoch, max_stale_1h, min_cov
        )
        res_6h = self._analyze_interval_from_candles(
            candles_6h, "6h", 21600, 3600, as_of_epoch, max_stale_6h, min_cov
        )
        res_24h = self._analyze_interval_from_candles(
            candles_24h, "24h", 86400, 3600, as_of_epoch, max_stale_24h, min_cov
        )

        intervals = {"5m": res_5m, "1h": res_1h, "6h": res_6h, "24h": res_24h}

        # Calculate Accelerations
        v_5m = res_5m.hourly_velocity_pct
        v_1h = res_1h.hourly_velocity_pct
        v_6h = res_6h.hourly_velocity_pct
        v_24h = res_24h.hourly_velocity_pct

        accel_micro = (v_5m - v_1h) if (v_5m is not None and v_1h is not None) else None
        accel_medium = (v_1h - v_6h) if (v_1h is not None and v_6h is not None) else None
        accel_macro = (v_6h - v_24h) if (v_6h is not None and v_24h is not None) else None

        # Build detailed measurement attributions
        measurements: List[MeasurementAttribution] = []

        def add_measurement(
            name: str,
            val: Optional[float],
            units: str,
            int_str: Optional[str],
            res: Optional[IntervalAnalysisResult],
            reason: str = "",
        ) -> None:
            if res:
                freshness = res.freshness
                ev_state = res.evidence_state
                quality = res.quality
                cov = res.coverage_ratio
                r_code = res.reason_code if not reason else reason
            else:
                freshness = FreshnessStatus.FRESH if val is not None else FreshnessStatus.EXPIRED
                ev_state = EvidenceState.VERIFIED if val is not None else EvidenceState.UNAVAILABLE
                quality = ObservationQuality.HIGH if val is not None else ObservationQuality.UNAVAILABLE
                cov = None
                r_code = reason or ("CALCULATED" if val is not None else "UNAVAILABLE")

            measurements.append(
                MeasurementAttribution(
                    measurement_name=name,
                    value=val,
                    units=units,
                    interval_str=int_str,
                    freshness=freshness,
                    evidence_state=ev_state,
                    observation_quality=quality,
                    reason_code=r_code,
                    as_of_timestamp_utc=as_of_str,
                    coverage_ratio=cov,
                )
            )

        add_measurement("price_velocity_5m", v_5m, "%_per_hour", "5m", res_5m)
        add_measurement("price_velocity_1h", v_1h, "%_per_hour", "1h", res_1h)
        add_measurement("price_velocity_6h", v_6h, "%_per_hour", "6h", res_6h)
        add_measurement("price_velocity_24h", v_24h, "%_per_hour", "24h", res_24h)

        add_measurement("acceleration_micro_5m_vs_1h", accel_micro, "%_per_hour_sq", "5m_vs_1h", None)
        add_measurement("acceleration_medium_1h_vs_6h", accel_medium, "%_per_hour_sq", "1h_vs_6h", None)
        add_measurement("acceleration_macro_6h_vs_24h", accel_macro, "%_per_hour_sq", "6h_vs_24h", None)

        # Overall coverage completeness across the 4 required intervals
        cov_completeness = (
            res_5m.coverage_ratio * 0.4
            + res_1h.coverage_ratio * 0.3
            + res_6h.coverage_ratio * 0.2
            + res_24h.coverage_ratio * 0.1
        )

        return {
            "intervals": intervals,
            "v_5m": v_5m,
            "v_1h": v_1h,
            "v_6h": v_6h,
            "v_24h": v_24h,
            "accel_micro": accel_micro,
            "accel_medium": accel_medium,
            "accel_macro": accel_macro,
            "coverage_completeness": cov_completeness,
            "measurements": measurements,
            "validation_errors": [],
        }

    def score_features(
        self, features: Dict[str, Any]
    ) -> Tuple[Optional[float], Optional[float], EvidenceState, ObservationQuality, List[str]]:
        """Scores momentum acceleration features using documented weighting hierarchy."""
        val_errors: List[str] = features.get("validation_errors", [])
        if val_errors:
            return None, None, EvidenceState.ERROR, ObservationQuality.UNAVAILABLE, val_errors

        intervals = features.get("intervals", {})
        res_5m: Optional[IntervalAnalysisResult] = intervals.get("5m")
        res_1h: Optional[IntervalAnalysisResult] = intervals.get("1h")
        res_6h: Optional[IntervalAnalysisResult] = intervals.get("6h")
        res_24h: Optional[IntervalAnalysisResult] = intervals.get("24h")

        exclusions: List[str] = []

        # Check for missing/degraded intervals
        for name, res in [("5m", res_5m), ("1h", res_1h), ("6h", res_6h), ("24h", res_24h)]:
            if not res or res.evidence_state != EvidenceState.VERIFIED:
                exclusions.append(f"INTERVAL_{name}_UNVERIFIED_{res.reason_code if res else 'MISSING'}")

        if exclusions:
            return None, None, EvidenceState.UNAVAILABLE, ObservationQuality.DEGRADED, exclusions

        accel_micro = features["accel_micro"]
        accel_medium = features["accel_medium"]
        accel_macro = features["accel_macro"]
        v_1h = features["v_1h"]

        # Ensure all required acceleration terms are finite numbers
        if any(x is None or not math.isfinite(x) for x in [accel_micro, accel_medium, accel_macro, v_1h]):
            return None, None, EvidenceState.ERROR, ObservationQuality.UNAVAILABLE, ["NON_FINITE_ACCELERATION_TERMS"]

        w_micro = float(self.config.get("w_accel_micro", 0.35))
        w_medium = float(self.config.get("w_accel_medium", 0.35))
        w_v1h = float(self.config.get("w_velocity_1h", 0.20))
        w_macro = float(self.config.get("w_accel_macro", 0.10))
        scale_denom = float(self.config.get("score_scale_denominator", 15.0))

        # Reward expansion: positive acceleration
        contrib_micro = max(0.0, accel_micro) * w_micro
        contrib_medium = max(0.0, accel_medium) * w_medium
        contrib_v1h = max(0.0, v_1h) * w_v1h
        contrib_macro = max(0.0, accel_macro) * w_macro

        raw_score = contrib_micro + contrib_medium + contrib_v1h + contrib_macro

        # Severe deceleration penalty: if both micro and medium accelerations are negative
        if accel_micro < 0.0 and accel_medium < 0.0:
            raw_score = raw_score * 0.25  # Substantial penalty for decelerating/dumping momentum

        # Saturated scale [0.0, 100.0]
        scaled_score = min(100.0, max(0.0, (raw_score / scale_denom) * 100.0))

        # Confidence: product of coverage completeness and freshness
        cov = float(features.get("coverage_completeness", 0.0))
        freshness_factor = 1.0
        if any(r.freshness == FreshnessStatus.WARM for r in intervals.values()):
            freshness_factor = 0.8
        elif any(r.freshness == FreshnessStatus.STALE for r in intervals.values()):
            freshness_factor = 0.0

        confidence = round(cov * freshness_factor, 4)

        return round(scaled_score, 2), confidence, EvidenceState.VERIFIED, ObservationQuality.HIGH, []
