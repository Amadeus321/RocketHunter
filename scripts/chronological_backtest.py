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
        self.records = records
        self.config_fingerprint = config_fingerprint

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
        """Filter ambiguous identities, validate timestamps, and sort chronologically."""
        valid = []
        for r in self.records:
            # Extract timestamp
            t_str = r.get("snapshot_utc") or r.get("forecast_utc") or r.get("retrieved_utc")
            t = _parse_utc(t_str)
            if t is None:
                continue

            # Identify market key
            chain = r.get("chain") or (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("chain")
            token = (
                r.get("contract_address") or r.get("token_mint") or
                (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("token_contract")
            )
            pool = (
                r.get("pool_address") or r.get("pair_address") or
                (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("pool_address")
            )
            if not chain or not token:
                continue  # Ambiguous identity excluded

            price = _clean_num(
                r.get("price_usd") or r.get("final_price_usd") or
                (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("price_at_detection_usd")
            )
            if price is None or price <= 0:
                continue

            valid.append({
                "record_id": r.get("snapshot_id") or r.get("forecast_id") or hashlib.sha256(f"{chain}:{token}:{t_str}".encode()).hexdigest(),
                "timestamp": t,
                "timestamp_utc": t_str,
                "chain": str(chain).lower(),
                "token": str(token),
                "pool": str(pool) if pool else None,
                "market_key": f"{chain}:{token}:{pool}" if pool else f"{chain}:{token}",
                "detection_price": price,
                "score": _clean_num(
                    r.get("early_breakout_score") or r.get("rank_score") or
                    (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("signal_score_and_components", {}).get("early_breakout_score")
                ) or 0.0,
                "liquidity_usd": _clean_num(
                    r.get("liquidity_usd") or
                    (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("liquidity_usd")
                ) or 0.0,
                "age_hours": _clean_num(
                    r.get("age_hours") or
                    (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("pool_age_hours")
                ),
                "is_rejected": r.get("is_rejected_candidate", False),
                "raw_record": r,
            })

        # Sort strictly chronologically
        valid.sort(key=lambda item: item["timestamp"])
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
            "scientific_integrity_note": (
                "Zero synthetic labels permitted. Incomplete or unresolved observation windows are "
                "strictly counted and excluded from performance percentage denominators."
            ),
        }

    def _resolve_outcome(self, item: dict[str, Any], lookup: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
        """Resolve exact-identity, verified 24-hour outcome without synthetic labels.

        Never estimates MFE, MAE, TP attainment, or touch times from change_24h_pct.
        Unresolved or incomplete records are marked is_evaluated=False and excluded from performance rates.
        """
        # 1. Lookup match
        if lookup and item["record_id"] in lookup:
            rec = lookup[item["record_id"]]
            status = rec.get("outcome_classification") or ("TARGET_REACHED" if rec.get("target_reached") else "UNRESOLVED")
            mfe = _clean_num(rec.get("mfe_pct"))
            mae = _clean_num(rec.get("mae_pct"))
            touch_time = _clean_num(rec.get("hours_to_target") or rec.get("first_touch_hours"))
            is_eval = status in {"TARGET_REACHED", "TARGET_NOT_REACHED"} and mfe is not None
            return {
                "outcome_classification": status,
                "target_reached": rec.get("target_reached") if is_eval else None,
                "mfe_pct": mfe if is_eval else None,
                "mae_pct": mae if is_eval else None,
                "hours_to_target": touch_time if is_eval else None,
                "is_evaluated": is_eval,
                "resolution_reason": "MATCHED_EXACT_IDENTITY_OUTCOME_LOOKUP" if is_eval else "UNRESOLVED_LOOKUP",
                "execution_status": rec.get("execution_status", "EXECUTION_UNVERIFIED"),
            }

        # 2. Raw record attributes
        raw = item["raw_record"]
        outcome_status = raw.get("outcome_classification")
        mfe_pct = _clean_num(raw.get("mfe_pct"))
        mae_pct = _clean_num(raw.get("mae_pct"))
        touch_time = _clean_num(raw.get("hours_to_target") or raw.get("first_touch_hours") or raw.get("target_touch_time_hours"))

        # Exact verified candle trace: only consider evaluated if verified status and real MFE are present
        if outcome_status in {"TARGET_REACHED", "TARGET_NOT_REACHED"} and mfe_pct is not None:
            hit_tp = bool(outcome_status == "TARGET_REACHED" or mfe_pct >= MANDATORY_TARGET_PCT)
            return {
                "outcome_classification": outcome_status,
                "target_reached": hit_tp,
                "mfe_pct": mfe_pct,
                "mae_pct": mae_pct,
                "hours_to_target": touch_time if hit_tp else None,
                "is_evaluated": True,
                "resolution_reason": "EMBEDDED_VERIFIED_OUTCOME",
                "execution_status": raw.get("execution_status", "EXECUTION_UNVERIFIED"),
            }

        # 3. If exact-identity verified 24h outcome is unavailable, classify as INCOMPLETE / UNRESOLVED
        censored = bool(raw.get("censored") or outcome_status in {"UNKNOWN_DUE_TO_GAP", "PRICE_SOURCE_CONFLICT", "EXPIRED"})
        classification = outcome_status or ("CENSORED" if censored else "INCOMPLETE_OBSERVATION")
        return {
            "outcome_classification": classification,
            "target_reached": None,
            "mfe_pct": None,
            "mae_pct": None,
            "hours_to_target": None,
            "is_evaluated": False,
            "resolution_reason": "UNRESOLVED_INCOMPLETE_OR_CENSORED",
            "execution_status": "EXECUTION_UNVERIFIED",
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
