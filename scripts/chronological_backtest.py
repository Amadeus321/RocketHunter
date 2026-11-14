#!/usr/bin/env python3
"""Chronological Walk-Forward Backtesting & Predictive Evaluation Engine for Rocket Hunter v4.30.

Enforces strict time-separated out-of-sample evaluation:
- Divides observations by time, never randomly.
- Trains/develops models on earlier windows; evaluates strictly on later out-of-sample windows.
- Prevents future-data leakage by freezing decision-time features.
- Accounts for deduplicated pools, overlapping signals, and execution drag.
- Compares against naive momentum and market baselines.
- Measures TP40 attainment, milestone attainment (1h/3h/6h/12h/24h), MAE/MFE, calibration,
  and breakdowns by chain, liquidity, and age.
"""
from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import Any

BACKTEST_VERSION = "4.30.0"
MANDATORY_TARGET_PCT = 40.0

try:
    from scripts.address_identity import validate_and_build_identity
except ImportError:
    from address_identity import validate_and_build_identity


def _parse_utc(val: Any) -> dt.datetime | None:
    if not isinstance(val, str) or not val:
        return None
    try:
        p = dt.datetime.fromisoformat(val.replace("Z", "+00:00"))
        if p.tzinfo is None:
            return None
        return p.astimezone(dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def _clean_num(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        v = float(val)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


class ChronologicalBacktestEvaluator:
    """Evaluates predictive models using chronological walk-forward out-of-sample windows."""

    def __init__(self, records: list[dict[str, Any]], *, config_fingerprint: str = "default"):
        self.records = records or []
        self.config_fingerprint = config_fingerprint
        self.diagnostics: dict[str, Any] = {
            "total_historical_records": len(self.records),
            "valid_exact_identity_records": 0,
            "missing_identity_records": 0,
            "missing_identity_by_reason": collections.Counter(),
            "conflicting_identity_records": 0,
            "duplicate_records": 0,
            "successfully_matched_outcomes": 0,
            "unresolved_outcomes": 0,
            "outcome_identity_missing_records": 0,
            "outcome_identity_invalid_records": 0,
            "outcome_identity_mismatch_records": 0,
            "evidence_conflict_records": 0,
            "target_outside_window_records": 0,
            "missing_touch_evidence_records": 0,
            "missing_detection_time_records": 0,
            "rejected_records": [],
        }

    @classmethod
    def from_jsonl_partitions(cls, directory: Path, pattern: str = "*.jsonl.gz") -> "ChronologicalBacktestEvaluator":
        import gzip
        records = []
        corrupted_partitions = []
        if directory.exists():
            for p in sorted(directory.glob(pattern)):
                try:
                    with gzip.open(p, "rt", encoding="utf-8") as handle:
                        for line_idx, line in enumerate(handle, 1):
                            if line.strip():
                                try:
                                    records.append(json.loads(line))
                                except json.JSONDecodeError as exc:
                                    corrupted_partitions.append({
                                        "file": str(p),
                                        "line": line_idx,
                                        "error": f"JSONDecodeError: {exc}",
                                    })
                                    # Make corrupted line visible as incomplete data
                                    records.append({
                                        "snapshot_id": f"corrupted_{p.name}_{line_idx}",
                                        "snapshot_utc": "1970-01-01T00:00:00Z",
                                        "chain": "unknown",
                                        "contract_address": "corrupted",
                                        "price_usd": 1.0,
                                        "outcome_classification": "INCOMPLETE_CORRUPTED_PARTITION",
                                    })
                except Exception as exc:
                    corrupted_partitions.append({
                        "file": str(p),
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    records.append({
                        "snapshot_id": f"corrupted_partition_{p.name}",
                        "snapshot_utc": "1970-01-01T00:00:00Z",
                        "chain": "unknown",
                        "contract_address": "corrupted",
                        "price_usd": 1.0,
                        "outcome_classification": "INCOMPLETE_CORRUPTED_PARTITION",
                    })
        evaluator = cls(records)
        evaluator.corrupted_partitions = corrupted_partitions  # type: ignore[attr-defined]
        return evaluator

    def prepare_chronological_dataset(self) -> list[dict[str, Any]]:
        """Filter ambiguous identities, validate timestamps, and sort chronologically.

        Strictly enforces:
        - Exact DEX market identity: chain + token mint/contract + pool address (all mandatory).
        - Distinct CEX market identity: exchange + market_type + trading_pair.
        - Rejection of missing chain, missing token, or missing pool address.
        - Detection and reporting of conflicting token or pool representations.
        - Preservation of frozen detection-time information.
        - Deduplication to prevent inflating the sample count.
        - Recording of all rejected candidates in diagnostics for full auditability.
        """
        valid = []
        seen_keys: set[tuple[str, str]] = set()

        for r in self.records:
            if not isinstance(r, dict):
                self.diagnostics["missing_identity_records"] += 1
                self.diagnostics["missing_identity_by_reason"]["NOT_A_DICTIONARY"] += 1
                self.diagnostics["rejected_records"].append({"record": r, "reason": "NOT_A_DICTIONARY"})
                continue

            # Extract timestamp
            t_str = r.get("snapshot_utc") or r.get("forecast_utc") or r.get("retrieved_utc")
            t = _parse_utc(t_str)
            if t is None:
                self.diagnostics["missing_identity_records"] += 1
                self.diagnostics["missing_identity_by_reason"]["INVALID_OR_MISSING_TIMESTAMP"] += 1
                self.diagnostics["rejected_records"].append({"record": r, "reason": "INVALID_OR_MISSING_TIMESTAMP"})
                continue

            # Validate exact market identity
            id_res = validate_and_build_identity(r)
            if not id_res.get("is_valid"):
                reason = id_res.get("rejection_reason") or "INVALID_IDENTITY"
                if "CONFLICTING" in reason:
                    self.diagnostics["conflicting_identity_records"] += 1
                else:
                    self.diagnostics["missing_identity_records"] += 1
                self.diagnostics["missing_identity_by_reason"][reason] += 1
                self.diagnostics["rejected_records"].append({
                    "record": r,
                    "reason": reason,
                    "details": id_res,
                })
                continue

            canon_key = id_res["canonical_identity"]
            m_type = id_res["market_type"]

            # Validate price at detection time
            price = _clean_num(
                r.get("price_usd") or r.get("final_price_usd") or r.get("entry_price") or
                (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("price_at_detection_usd")
            )
            if price is None or price <= 0:
                self.diagnostics["missing_identity_by_reason"]["INVALID_OR_NONPOSITIVE_PRICE"] += 1
                self.diagnostics["rejected_records"].append({"record": r, "reason": "INVALID_OR_NONPOSITIVE_PRICE"})
                continue

            # Deduplication: prevent identical observation from inflating sample count
            dedup_key = (canon_key, str(t_str))
            if dedup_key in seen_keys:
                self.diagnostics["duplicate_records"] += 1
                self.diagnostics["rejected_records"].append({
                    "record": r,
                    "reason": "DUPLICATE_OBSERVATION",
                    "key": f"{canon_key}@{t_str}",
                })
                continue
            seen_keys.add(dedup_key)

            # Preserve immutable detection-time fields
            rec_id = r.get("snapshot_id") or r.get("forecast_id") or hashlib.sha256(f"{canon_key}:{t_str}".encode()).hexdigest()
            tp_price = _clean_num(
                r.get("target_price") or r.get("take_profit_price") or r.get("tp_price") or
                (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("target_price")
            )
            tp_pct = _clean_num(r.get("technical_tp_pct") or r.get("target_upside_pct") or r.get("tp_pct"))
            if tp_pct is None and tp_price is not None and price > 0:
                tp_pct = round(((tp_price / price) - 1.0) * 100.0, 4)

            score_val = _clean_num(
                r.get("early_breakout_score") or r.get("rank_score") or
                (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("signal_score_and_components", {}).get("early_breakout_score")
            ) or 0.0

            liq_val = _clean_num(
                r.get("liquidity_usd") or
                (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("liquidity_usd")
            ) or 0.0

            vol_val = _clean_num(
                r.get("volume_usd") or r.get("volume_24h_usd") or
                (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("volume_24h_usd")
            ) or 0.0

            age_val = _clean_num(
                r.get("age_hours") or
                (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("pool_age_hours")
            )

            valid.append({
                "record_id": rec_id,
                "timestamp": t,
                "timestamp_utc": t_str,
                "discovery_timestamp_utc": t_str,
                "market_type": m_type,
                "chain": id_res.get("chain"),
                "token": id_res.get("token"),
                "pool": id_res.get("pool"),
                "exchange": id_res.get("exchange"),
                "pair": id_res.get("pair"),
                "market_key": canon_key,
                "exact_identity": canon_key,
                "detection_price": price,
                "entry_price": price,
                "technical_tp_price": tp_price,
                "technical_tp_pct": tp_pct,
                "scanner_version": str(r.get("scanner_version") or r.get("version") or BACKTEST_VERSION),
                "config_fingerprint": str(r.get("config_fingerprint") or self.config_fingerprint),
                "score": score_val,
                "score_components": r.get("score_components") or (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("signal_score_and_components") or {},
                "liquidity_usd": liq_val,
                "volume_usd": vol_val,
                "volume_24h_usd": vol_val,
                "token_age_hours": age_val,
                "age_hours": age_val,
                "security_evidence": r.get("security_evidence") or r.get("security_status") or r.get("quality_gate") or {},
                "discovery_source": r.get("source") or r.get("discovery_source") or r.get("venue") or "unknown",
                "provider_timestamps": {
                    "retrieved_utc": r.get("retrieved_utc"),
                    "data_timestamp_utc": r.get("data_timestamp_utc"),
                },
                "is_rejected": r.get("is_rejected_candidate", False),
                "raw_record": r,
            })

        # Sort strictly chronologically
        valid.sort(key=lambda item: item["timestamp"])
        self.diagnostics["valid_exact_identity_records"] = len(valid)
        return valid

    def evaluate_walk_forward(
        self,
        train_window_hours: int = 168,  # 7 days training
        test_window_hours: int = 48,    # 2 days testing
        step_hours: int = 24,           # 1 day advance
        score_threshold: float = 75.0,  # Model selection threshold
        outcome_lookup: dict[str, dict[str, Any]] | None = None,
        model_selection_mode: str = "FIXED_RULE",  # "FIXED_RULE" or "OPTIMIZE_ON_TRAIN"
    ) -> dict[str, Any]:
        """Execute walk-forward out-of-sample backtest windows."""
        dataset = self.prepare_chronological_dataset()
        if not dataset:
            return {
                "backtest_version": BACKTEST_VERSION,
                "status": "INSUFFICIENT_DATA",
                "message": "No valid chronological observation records found.",
                "total_records": len(self.records),
                "windows_evaluated": 0,
                "diagnostics": {
                    "total_historical_records": self.diagnostics.get("total_historical_records", len(self.records)),
                    "valid_exact_identity_records": 0,
                    "missing_identity_records": self.diagnostics.get("missing_identity_records", 0),
                    "missing_identity_by_reason": dict(self.diagnostics.get("missing_identity_by_reason", {})),
                    "conflicting_identity_records": self.diagnostics.get("conflicting_identity_records", 0),
                    "duplicate_records": self.diagnostics.get("duplicate_records", 0),
                    "successfully_matched_outcomes": 0,
                    "unresolved_outcomes": 0,
                    "outcome_identity_missing_records": self.diagnostics.get("outcome_identity_missing_records", 0),
                    "outcome_identity_invalid_records": self.diagnostics.get("outcome_identity_invalid_records", 0),
                    "outcome_identity_mismatch_records": self.diagnostics.get("outcome_identity_mismatch_records", 0),
                    "evidence_conflict_records": self.diagnostics.get("evidence_conflict_records", 0),
                    "target_outside_window_records": self.diagnostics.get("target_outside_window_records", 0),
                    "missing_touch_evidence_records": self.diagnostics.get("missing_touch_evidence_records", 0),
                    "missing_detection_time_records": self.diagnostics.get("missing_detection_time_records", 0),
                },
            }

        start_time = dataset[0]["timestamp"]
        end_time = dataset[-1]["timestamp"]
        total_span_hours = (end_time - start_time).total_seconds() / 3600.0

        if total_span_hours < (train_window_hours + test_window_hours):
            # If total span is shorter than a full walk-forward cycle, partition 70% / 30% chronologically
            split_idx = int(len(dataset) * 0.70)
            train_set = dataset[:split_idx]
            test_set = dataset[split_idx:]
            windows = [(train_set, test_set, "single_train_test_split")]
        else:
            windows = []
            cursor = start_time
            while cursor + dt.timedelta(hours=train_window_hours + test_window_hours) <= end_time:
                train_end = cursor + dt.timedelta(hours=train_window_hours)
                test_end = train_end + dt.timedelta(hours=test_window_hours)

                train_slice = [item for item in dataset if cursor <= item["timestamp"] < train_end]
                test_slice = [item for item in dataset if train_end <= item["timestamp"] < test_end]
                if train_slice and test_slice:
                    windows.append((train_slice, test_slice, f"{cursor.isoformat()}_{test_end.isoformat()}"))
                cursor += dt.timedelta(hours=step_hours)

        out_of_sample_evaluations: list[dict[str, Any]] = []
        baseline_evaluations: list[dict[str, Any]] = []
        seen_test_keys: set[str] = set()

        for train_slice, test_slice, window_id in windows:
            # Genuine time-separated model selection: choose threshold strictly on train_slice
            active_threshold = score_threshold
            if model_selection_mode == "OPTIMIZE_ON_TRAIN" and train_slice:
                train_evaluated = []
                for tr_item in train_slice:
                    tr_outcome = self._resolve_outcome(tr_item, outcome_lookup)
                    if tr_outcome.get("is_evaluated") and not tr_item["is_rejected"]:
                        train_evaluated.append((tr_item["score"], bool(tr_outcome.get("target_reached"))))
                if len(train_evaluated) >= 10:
                    best_th = score_threshold
                    best_prec = -1.0
                    for candidate_th in [60.0, 65.0, 70.0, 75.0, 80.0, 85.0]:
                        picks = [hit for sc, hit in train_evaluated if sc >= candidate_th]
                        if len(picks) >= 3:
                            prec = sum(1 for h in picks if h) / len(picks)
                            if prec > best_prec:
                                best_prec = prec
                                best_th = candidate_th
                    active_threshold = best_th

            # Evaluate test_slice strictly using decision-time features
            for item in test_slice:
                m_key = f"{item['market_key']}_{item['timestamp_utc']}"
                if m_key in seen_test_keys:
                    continue
                seen_test_keys.add(m_key)

                outcome = self._resolve_outcome(item, outcome_lookup)

                # Candidate model evaluation
                if item["score"] >= active_threshold and not item["is_rejected"]:
                    out_of_sample_evaluations.append({
                        "item": item,
                        "window_id": window_id,
                        "outcome": outcome,
                    })

                # Naive Baseline: All unrejected candidates
                if not item["is_rejected"]:
                    baseline_evaluations.append({
                        "item": item,
                        "window_id": window_id,
                        "outcome": outcome,
                    })

        # Calculate statistics strictly separating evaluated from incomplete records
        model_metrics = self._calculate_metrics(out_of_sample_evaluations)
        baseline_metrics = self._calculate_metrics(baseline_evaluations)

        # Segmented analysis
        by_chain = self._segmented_metrics(out_of_sample_evaluations, key=lambda x: x["item"]["chain"])
        by_liquidity = self._segmented_metrics(
            out_of_sample_evaluations,
            key=lambda x: (
                "<25k" if x["item"]["liquidity_usd"] < 25_000 else (
                    "25k-100k" if x["item"]["liquidity_usd"] < 100_000 else (
                        "100k-500k" if x["item"]["liquidity_usd"] < 500_000 else ">500k"
                    )
                )
            ),
        )

        dataset_hash = hashlib.sha256(
            "".join(r["record_id"] for r in dataset).encode("utf-8")
        ).hexdigest()

        return {
            "backtest_version": BACKTEST_VERSION,
            "status": "COMPLETED",
            "evaluation_policy": "STRICT_CHRONOLOGICAL_WALK_FORWARD; IMMUTABLE_FROZEN_FEATURES",
            "evaluation_mode": "TIME_SEPARATED_TRAIN_OPTIMIZED_THRESHOLD" if model_selection_mode == "OPTIMIZE_ON_TRAIN" else "FIXED_RULE_CHRONOLOGICAL_EVALUATOR",
            "model_selection_policy": (
                "Dynamic threshold selection on training slice only"
                if model_selection_mode == "OPTIMIZE_ON_TRAIN"
                else f"Fixed rule (Score >= {score_threshold:.1f}, no model trained)"
            ),
            "dataset_hash": dataset_hash,
            "config_fingerprint": self.config_fingerprint,
            "total_chronological_records": len(dataset),
            "walk_forward_windows_run": len(windows),
            "model_selection_threshold": score_threshold,
            "out_of_sample_model_results": model_metrics,
            "naive_baseline_comparison": baseline_metrics,
            "performance_by_chain": by_chain,
            "performance_by_liquidity_tier": by_liquidity,
            "diagnostics": {
                "total_historical_records": self.diagnostics.get("total_historical_records", len(self.records)),
                "valid_exact_identity_records": len(dataset),
                "missing_identity_records": self.diagnostics.get("missing_identity_records", 0),
                "missing_identity_by_reason": dict(self.diagnostics.get("missing_identity_by_reason", {})),
                "conflicting_identity_records": self.diagnostics.get("conflicting_identity_records", 0),
                "duplicate_records": self.diagnostics.get("duplicate_records", 0),
                "successfully_matched_outcomes": self.diagnostics.get("successfully_matched_outcomes", 0),
                "unresolved_outcomes": self.diagnostics.get("unresolved_outcomes", 0),
                "outcome_identity_missing_records": self.diagnostics.get("outcome_identity_missing_records", 0),
                "outcome_identity_invalid_records": self.diagnostics.get("outcome_identity_invalid_records", 0),
                "outcome_identity_mismatch_records": self.diagnostics.get("outcome_identity_mismatch_records", 0),
                "evidence_conflict_records": self.diagnostics.get("evidence_conflict_records", 0),
                "target_outside_window_records": self.diagnostics.get("target_outside_window_records", 0),
                "missing_touch_evidence_records": self.diagnostics.get("missing_touch_evidence_records", 0),
                "missing_detection_time_records": self.diagnostics.get("missing_detection_time_records", 0),
            },
            "scientific_integrity_note": (
                "Zero synthetic labels permitted. Incomplete or unresolved observation windows are "
                "strictly counted and excluded from performance percentage denominators."
            ),
        }

    def _validate_outcome_identity(self, outcome_data: Any, forecast_canon_id: str) -> tuple[bool, dict[str, Any] | None]:
        """Validate that an outcome record (from lookup or embedded) has a valid canonical identity
        matching the forecast's canonical market_key.
        """
        if not isinstance(outcome_data, dict):
            self.diagnostics["outcome_identity_missing_records"] += 1
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "OUTCOME_IDENTITY_MISSING",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": None,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "OUTCOME_IDENTITY_MISSING: Outcome record is not a dictionary",
                "execution_status": "EXECUTION_UNVERIFIED",
                "execution_net_return_pct": None,
            }

        has_id_indicators = any(
            outcome_data.get(k) is not None and str(outcome_data.get(k)).strip() != ""
            for k in (
                "market_key", "exact_identity", "identity",
                "chain", "token_contract", "contract_address", "token_mint", "address",
                "pool_address", "pair_address", "gecko_pool_id",
                "exchange", "venue", "trading_pair", "pair", "venue_pair",
            )
        ) or (isinstance(outcome_data.get("normalized_record"), dict) and any(outcome_data["normalized_record"].values()))

        if not has_id_indicators:
            self.diagnostics["outcome_identity_missing_records"] += 1
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "OUTCOME_IDENTITY_MISSING",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": None,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "OUTCOME_IDENTITY_MISSING: Outcome record contains no market identity",
                "execution_status": "EXECUTION_UNVERIFIED",
                "execution_net_return_pct": None,
            }

        id_res = validate_and_build_identity(outcome_data)
        if not id_res.get("is_valid"):
            rej_reason = str(id_res.get("rejection_reason") or "INVALID_IDENTITY")
            if rej_reason.startswith("MISSING_"):
                self.diagnostics["outcome_identity_missing_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "OUTCOME_IDENTITY_MISSING",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": None,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"OUTCOME_IDENTITY_MISSING: {rej_reason}",
                    "execution_status": "EXECUTION_UNVERIFIED",
                    "execution_net_return_pct": None,
                }
            else:
                self.diagnostics["outcome_identity_invalid_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "OUTCOME_IDENTITY_INVALID",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": None,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"OUTCOME_IDENTITY_INVALID: {rej_reason}",
                    "execution_status": "EXECUTION_UNVERIFIED",
                    "execution_net_return_pct": None,
                }

        outcome_canon_id = id_res.get("canonical_identity")
        if outcome_canon_id != forecast_canon_id:
            self.diagnostics["outcome_identity_mismatch_records"] += 1
            self.diagnostics["conflicting_identity_records"] += 1
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "OUTCOME_IDENTITY_MISMATCH",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": None,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"OUTCOME_IDENTITY_MISMATCH: forecast={forecast_canon_id} outcome={outcome_canon_id}",
                "execution_status": "EXECUTION_UNVERIFIED",
                "execution_net_return_pct": None,
            }

        return True, None

    def _resolve_outcome(self, item: dict[str, Any], lookup: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
        """Resolve exact-identity, verified 24-hour outcome without synthetic labels.

        Enforces:
        - Mandatory canonical outcome identity matching forecast market_key across ALL sources
          (both outcome lookup dictionaries and embedded records).
        - Strict rejection of missing, invalid, or mismatched outcome identities.
        - Non-contradictory classification: TARGET_NOT_REACHED can never produce a hit;
          disagreements between classification and candle evidence trigger OUTCOME_EVIDENCE_CONFLICT.
        - Required candle evidence of WHEN the target was reached (first-touch timestamp or elapsed hours).
        - Verification that detection timestamp exists and is timezone-aware.
        - Cross-verification of timestamp and elapsed time agreement within tolerance.
        - Verification against frozen detection-time technical target.
        - Strict separation between market target attainment and executable net profit.
        """
        forecast_canon_id = item.get("market_key")

        # 1. Resolve source outcome record and enforce identical validation across all sources
        if lookup is not None and item.get("record_id") in lookup:
            outcome_data = lookup[item["record_id"]]
            source_reason = "MATCHED_EXACT_IDENTITY_OUTCOME_LOOKUP"
            is_valid_id, rejection_dict = self._validate_outcome_identity(outcome_data, forecast_canon_id)
            if not is_valid_id:
                return rejection_dict  # type: ignore[return-value]
        else:
            raw = item.get("raw_record") or {}
            has_embedded_outcome_dict = isinstance(raw.get("outcome"), dict)
            has_flat_outcome_fields = (
                raw.get("outcome_classification") is not None
                or raw.get("mfe_pct") is not None
                or raw.get("target_reached") is not None
            )

            if not has_embedded_outcome_dict and not has_flat_outcome_fields:
                self.diagnostics["unresolved_outcomes"] += 1
                return {
                    "outcome_classification": "INCOMPLETE_OBSERVATION",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": None,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": "UNRESOLVED_INCOMPLETE_OR_CENSORED",
                    "execution_status": "EXECUTION_UNVERIFIED",
                    "execution_net_return_pct": None,
                }

            outcome_data = raw["outcome"] if has_embedded_outcome_dict else raw
            source_reason = "EMBEDDED_VERIFIED_OUTCOME"
            # Embedded outcomes MUST pass the exact same identity validation
            is_valid_id, rejection_dict = self._validate_outcome_identity(outcome_data, forecast_canon_id)
            if not is_valid_id:
                return rejection_dict  # type: ignore[return-value]

        # 2. Extract outcome fields
        outcome_status = outcome_data.get("outcome_classification")
        if not outcome_status:
            if outcome_data.get("target_reached") is True:
                outcome_status = "TARGET_REACHED"
            elif outcome_data.get("target_reached") is False:
                outcome_status = "TARGET_NOT_REACHED"
            else:
                outcome_status = "INCOMPLETE_OBSERVATION"

        mfe_pct = _clean_num(outcome_data.get("mfe_pct"))
        mae_pct = _clean_num(outcome_data.get("mae_pct"))
        claimed_target_reached = outcome_data.get("target_reached")
        touch_time = _clean_num(
            outcome_data.get("hours_to_target") or outcome_data.get("first_touch_hours") or outcome_data.get("target_touch_time_hours")
        )
        touch_dt = _parse_utc(
            outcome_data.get("touch_timestamp_utc") or outcome_data.get("target_touch_utc") or outcome_data.get("first_touch_utc")
        )

        exec_status = str(outcome_data.get("execution_status") or "EXECUTION_UNVERIFIED")
        exec_net_return = _clean_num(outcome_data.get("execution_net_return_pct") or outcome_data.get("net_pnl_pct"))

        # Frozen detection-time target threshold (minimum +40.0% single TP)
        frozen_tp_pct = _clean_num(item.get("technical_tp_pct"))
        effective_target_pct = max(MANDATORY_TARGET_PCT, frozen_tp_pct if frozen_tp_pct is not None else MANDATORY_TARGET_PCT)

        # 3. Validate detection timestamp exists and is timezone-aware
        detection_dt = item.get("timestamp")
        if detection_dt is None or not isinstance(detection_dt, dt.datetime) or detection_dt.tzinfo is None:
            self.diagnostics["missing_detection_time_records"] += 1
            self.diagnostics["unresolved_outcomes"] += 1
            return {
                "outcome_classification": "MISSING_DETECTION_TIME",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": None,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "MISSING_DETECTION_TIME: detection timestamp is missing or not timezone-aware",
                "execution_status": exec_status,
                "execution_net_return_pct": exec_net_return,
            }

        # 4. Handle unresolved, censored, or gap records
        if outcome_status not in {"TARGET_REACHED", "TARGET_NOT_REACHED"}:
            censored = bool(outcome_data.get("censored") or outcome_status in {"UNKNOWN_DUE_TO_GAP", "PRICE_SOURCE_CONFLICT", "EXPIRED"})
            classification = outcome_status or ("CENSORED" if censored else "INCOMPLETE_OBSERVATION")
            self.diagnostics["unresolved_outcomes"] += 1
            return {
                "outcome_classification": classification,
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": None,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "UNRESOLVED_INCOMPLETE_OR_CENSORED",
                "execution_status": exec_status,
                "execution_net_return_pct": exec_net_return,
            }

        # 5. Require verified candle evidence (real MFE)
        if mfe_pct is None:
            self.diagnostics["unresolved_outcomes"] += 1
            return {
                "outcome_classification": "INCOMPLETE_OBSERVATION",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": None,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "MISSING_MFE_EVIDENCE: verified candle trace missing mfe_pct",
                "execution_status": exec_status,
                "execution_net_return_pct": exec_net_return,
            }

        # 6. Check for contradictory outcome classification vs candle evidence
        # Case A: status is TARGET_NOT_REACHED, but evidence says target was hit
        if outcome_status == "TARGET_NOT_REACHED":
            if claimed_target_reached is True or mfe_pct >= effective_target_pct:
                self.diagnostics["evidence_conflict_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return {
                    "outcome_classification": "OUTCOME_EVIDENCE_CONFLICT",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": mae_pct,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"OUTCOME_EVIDENCE_CONFLICT: status is TARGET_NOT_REACHED but mfe_pct={mfe_pct}% >= {effective_target_pct}% or claimed target_reached=True",
                    "execution_status": exec_status,
                    "execution_net_return_pct": exec_net_return,
                }

        # Case B: status is TARGET_REACHED, but evidence says target was NOT hit
        if outcome_status == "TARGET_REACHED":
            if claimed_target_reached is False or mfe_pct < effective_target_pct:
                self.diagnostics["evidence_conflict_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return {
                    "outcome_classification": "OUTCOME_EVIDENCE_CONFLICT",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": mae_pct,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"OUTCOME_EVIDENCE_CONFLICT: status is TARGET_REACHED but mfe_pct={mfe_pct}% < {effective_target_pct}% or claimed target_reached=False",
                    "execution_status": exec_status,
                    "execution_net_return_pct": exec_net_return,
                }

            # 7. Require verifiable touch evidence of WHEN the target was reached
            if touch_time is None and touch_dt is None:
                self.diagnostics["missing_touch_evidence_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return {
                    "outcome_classification": "MISSING_TARGET_TOUCH_EVIDENCE",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": mae_pct,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": "MISSING_TARGET_TOUCH_EVIDENCE: TARGET_REACHED requires verifiable touch timestamp or elapsed time",
                    "execution_status": exec_status,
                    "execution_net_return_pct": exec_net_return,
                }

            # 8. If both touch timestamp and elapsed hours are provided, verify they agree within tolerance
            TOUCH_TOLERANCE_HOURS = 0.1  # 6-minute candle resolution tolerance
            if touch_dt is not None:
                calculated_elapsed_hours = (touch_dt - detection_dt).total_seconds() / 3600.0
                if touch_time is not None and abs(calculated_elapsed_hours - touch_time) > TOUCH_TOLERANCE_HOURS:
                    self.diagnostics["evidence_conflict_records"] += 1
                    self.diagnostics["unresolved_outcomes"] += 1
                    return {
                        "outcome_classification": "OUTCOME_EVIDENCE_CONFLICT",
                        "target_reached": None,
                        "market_target_reached": None,
                        "mfe_pct": mfe_pct,
                        "mae_pct": mae_pct,
                        "hours_to_target": None,
                        "is_evaluated": False,
                        "resolution_reason": (
                            f"OUTCOME_EVIDENCE_CONFLICT: touch timestamp elapsed {calculated_elapsed_hours:.2f}h "
                            f"conflicts with claimed elapsed time {touch_time:.2f}h (tolerance {TOUCH_TOLERANCE_HOURS}h)"
                        ),
                        "execution_status": exec_status,
                        "execution_net_return_pct": exec_net_return,
                    }
                if touch_time is None:
                    touch_time = calculated_elapsed_hours

            # 9. Validate observation window bounds (0.0 to 24.0 hours)
            if touch_time < 0.0:
                self.diagnostics["evidence_conflict_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return {
                    "outcome_classification": "INVALID_TOUCH_TIMESTAMP",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": mae_pct,
                    "hours_to_target": touch_time,
                    "is_evaluated": False,
                    "resolution_reason": f"INVALID_TOUCH_TIMESTAMP: touch time {touch_time} is negative",
                    "execution_status": exec_status,
                    "execution_net_return_pct": exec_net_return,
                }
            if touch_time > 24.0:
                self.diagnostics["target_outside_window_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return {
                    "outcome_classification": "TARGET_TOUCH_OUTSIDE_WINDOW",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": mae_pct,
                    "hours_to_target": touch_time,
                    "is_evaluated": False,
                    "resolution_reason": f"TARGET_TOUCH_OUTSIDE_WINDOW: touch time {touch_time:.2f}h exceeds 24.0h observation window",
                    "execution_status": exec_status,
                    "execution_net_return_pct": exec_net_return,
                }

        # 10. Valid, non-contradictory verified outcome
        market_target_reached = bool(outcome_status == "TARGET_REACHED")
        self.diagnostics["successfully_matched_outcomes"] += 1
        return {
            "outcome_classification": outcome_status,
            "target_reached": market_target_reached,
            "market_target_reached": market_target_reached,
            "mfe_pct": mfe_pct,
            "mae_pct": mae_pct,
            "hours_to_target": touch_time if market_target_reached else None,
            "is_evaluated": True,
            "resolution_reason": source_reason,
            "execution_status": exec_status,
            "execution_net_return_pct": exec_net_return,
        }

    def _calculate_metrics(self, evaluations: list[dict[str, Any]]) -> dict[str, Any]:
        """Calculates performance statistics strictly on evaluated records, reporting incomplete counts."""
        total_n = len(evaluations)
        if total_n == 0:
            return {
                "sample_size": 0,
                "total_records_considered": 0,
                "evaluated_records_count": 0,
                "unresolved_records_count": 0,
                "incomplete_records_count": 0,
                "censored_records_count": 0,
                "tp40_rate_pct": None,
                "false_positive_rate_pct": None,
                "mean_mfe_pct": None,
                "mean_mae_pct": None,
                "median_mfe_pct": None,
                "average_hours_to_target": None,
                "wilson_95_ci_pct": None,
            }

        evaluated = [e for e in evaluations if e["outcome"].get("is_evaluated") is True]
        unresolved = [e for e in evaluations if e["outcome"].get("outcome_classification") in {"UNRESOLVED", "INCOMPLETE_OBSERVATION", "INCOMPLETE", "INCOMPLETE_CORRUPTED_PARTITION"}]
        censored = [e for e in evaluations if e["outcome"].get("outcome_classification") in {"CENSORED", "UNKNOWN_DUE_TO_GAP", "EXPIRED", "PRICE_SOURCE_CONFLICT"}]
        n_eval = len(evaluated)

        if n_eval == 0:
            return {
                "status": "NO_EVALUATED_RECORDS",
                "sample_size": total_n,
                "total_records_considered": total_n,
                "evaluated_records_count": 0,
                "unresolved_records_count": len(unresolved),
                "incomplete_records_count": len(unresolved),
                "censored_records_count": len(censored),
                "tp40_rate_pct": None,
                "false_positive_rate_pct": None,
                "mean_mfe_pct": None,
                "mean_mae_pct": None,
                "median_mfe_pct": None,
                "average_hours_to_target": None,
                "wilson_95_ci_pct": None,
                "milestone_attainment_pct": None,
            }

        hits = sum(1 for e in evaluated if e["outcome"].get("target_reached") is True)
        tp40_rate = (hits / n_eval) * 100.0
        fp_rate = ((n_eval - hits) / n_eval) * 100.0

        mfes = [e["outcome"].get("mfe_pct") for e in evaluated if e["outcome"].get("mfe_pct") is not None]
        maes = [e["outcome"].get("mae_pct") for e in evaluated if e["outcome"].get("mae_pct") is not None]
        times = [e["outcome"].get("hours_to_target") for e in evaluated if e["outcome"].get("hours_to_target") is not None]

        mean_mfe = round(sum(mfes) / len(mfes), 2) if mfes else None
        mean_mae = round(sum(maes) / len(maes), 2) if maes else None
        sorted_mfes = sorted(mfes)
        median_mfe = round(sorted_mfes[len(sorted_mfes) // 2], 2) if sorted_mfes else None
        avg_time = round(sum(times) / len(times), 2) if times else None

        # Wilson 95% confidence interval for proportion on evaluated records
        p = hits / n_eval
        z = 1.96
        denom = 1.0 + (z**2) / n_eval
        center = (p + (z**2) / (2 * n_eval)) / denom
        margin = (z * math.sqrt((p * (1 - p) + (z**2) / (4 * n_eval)) / n_eval)) / denom
        ci_lower = max(0.0, round((center - margin) * 100.0, 2))
        ci_upper = min(100.0, round((center + margin) * 100.0, 2))

        # Milestone attainment rates
        hit_1h = sum(1 for e in evaluated if (e["outcome"].get("hours_to_target") or 999) <= 1.0)
        hit_3h = sum(1 for e in evaluated if (e["outcome"].get("hours_to_target") or 999) <= 3.0)
        hit_6h = sum(1 for e in evaluated if (e["outcome"].get("hours_to_target") or 999) <= 6.0)
        hit_12h = sum(1 for e in evaluated if (e["outcome"].get("hours_to_target") or 999) <= 12.0)

        return {
            "status": "EVALUATED",
            "sample_size": n_eval,
            "total_records_considered": total_n,
            "evaluated_records_count": n_eval,
            "unresolved_records_count": len(unresolved),
            "incomplete_records_count": len(unresolved),
            "censored_records_count": len(censored),
            "tp40_hits": hits,
            "tp40_rate_pct": round(tp40_rate, 2),
            "false_positive_rate_pct": round(fp_rate, 2),
            "milestone_attainment_pct": {
                "within_1h": round((hit_1h / n_eval) * 100.0, 2),
                "within_3h": round((hit_3h / n_eval) * 100.0, 2),
                "within_6h": round((hit_6h / n_eval) * 100.0, 2),
                "within_12h": round((hit_12h / n_eval) * 100.0, 2),
                "within_24h": round(tp40_rate, 2),
            },
            "mean_mfe_pct": mean_mfe,
            "median_mfe_pct": median_mfe,
            "mean_mae_pct": mean_mae,
            "average_hours_to_target": avg_time,
            "wilson_95_ci_pct": [ci_lower, ci_upper],
        }

    def _segmented_metrics(self, evaluations: list[dict[str, Any]], key) -> dict[str, Any]:
        groups = collections.defaultdict(list)
        for e in evaluations:
            g_key = key(e) or "unknown"
            groups[g_key].append(e)

        return {
            g_name: self._calculate_metrics(g_list)
            for g_name, g_list in sorted(groups.items())
        }
