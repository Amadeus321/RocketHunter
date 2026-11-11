#!/usr/bin/env python3
"""Explore prospective v4.25 scores against exact, bounded 24-hour endpoint marks."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

try:
    from scripts import analyze_phase4_shadow as common
except ImportError:
    import analyze_phase4_shadow as common

ROOT = common.ROOT
DEFAULT_MEMORY = common.DEFAULT_MEMORY
DEFAULT_OUTPUT_ROOT = common.DEFAULT_OUTPUT_ROOT
ANALYSIS_VERSION = "rocket-hunter-v425-shadow.v1"
MANAGED_BY = "rocket-hunter-analyze-v425-shadow"
SIGNAL_VERSION = "v4.25-early-breakout.v1"
MODEL_ID = "v4.25-early-breakout"
HORIZON_HOURS = 24
MAX_SIGNAL_AGE_MINUTES = 30.0


def _parse_cutoff(value: str) -> dt.datetime:
    parsed = common._parse_utc(value)
    if parsed is None:
        raise ValueError("split cutoff must be an ISO-8601 timestamp with an explicit UTC offset")
    return parsed


def _iso_utc(value: dt.datetime | None) -> str | None:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _score_snapshot(forecast: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    features = forecast.get("feature_snapshot")
    signal = features.get("v425_early_breakout") if isinstance(features, dict) else None
    if not isinstance(signal, dict):
        return None, "missing_v425_snapshot"
    if signal.get("signal_version") != SIGNAL_VERSION or signal.get("model_id") != MODEL_ID:
        return None, "signal_version_or_model_mismatch"
    score = common._finite_number(signal.get("score"))
    if score is None or not 0 <= score <= 100:
        return None, "invalid_score"
    source = signal.get("source")
    if not isinstance(source, str) or not source.strip():
        return None, "missing_source_label"
    source_url = signal.get("source_url")
    forecast_time = common._parse_utc(forecast.get("snapshot_utc"))
    retrieved_time = common._parse_utc(signal.get("retrieved_utc"))
    if not isinstance(source_url, str) or not source_url.strip():
        return None, "missing_source_url"
    if forecast_time is None or retrieved_time is None:
        return None, "missing_signal_timestamp"
    age_minutes = (forecast_time - retrieved_time).total_seconds() / 60.0
    if age_minutes < 0 or age_minutes > MAX_SIGNAL_AGE_MINUTES:
        return None, "stale_or_future_signal_timestamp"
    return signal, None


def analyze_ledgers(
    memory_dir: Path = DEFAULT_MEMORY,
    split_cutoff_utc: str | dt.datetime | None = None,
) -> dict[str, Any]:
    if split_cutoff_utc is None:
        raise ValueError("an explicit split_cutoff_utc is required for reproducibility")
    if isinstance(split_cutoff_utc, str):
        cutoff = _parse_cutoff(split_cutoff_utc)
    elif split_cutoff_utc.tzinfo is None:
        raise ValueError("split cutoff must include a timezone")
    else:
        cutoff = split_cutoff_utc.astimezone(dt.timezone.utc)
    memory_dir = Path(memory_dir)
    forecast_paths, outcome_paths, stats, source_files, snapshot_id = common._source_snapshot(memory_dir)
    counters: collections.Counter[str] = collections.Counter()
    forecast_rows = common._read_jsonl_gz(forecast_paths, "forecast", counters)
    outcome_rows = common._read_jsonl_gz(outcome_paths, "outcome", counters)

    forecasts: dict[str, dict[str, Any]] = {}
    all_forecast_ids: collections.Counter[str] = collections.Counter()
    duplicate_ids: set[str] = set()
    for forecast in forecast_rows:
        forecast_id = forecast.get("forecast_id")
        if isinstance(forecast_id, str) and forecast_id:
            all_forecast_ids[forecast_id] += 1
            if all_forecast_ids[forecast_id] > 1:
                duplicate_ids.add(forecast_id)
        signal = forecast.get("feature_snapshot")
        signal = signal.get("v425_early_breakout") if isinstance(signal, dict) else None
        if not isinstance(signal, dict):
            continue
        if signal.get("signal_version") != SIGNAL_VERSION or signal.get("model_id") != MODEL_ID:
            counters["excluded_signal_version_or_model_mismatch"] += 1
            continue
        if not isinstance(forecast_id, str) or not forecast_id:
            counters["excluded_missing_forecast_id"] += 1
            continue
        if forecast_id not in forecasts:
            forecasts[forecast_id] = forecast
    counters["duplicate_forecast_ids"] = len(duplicate_ids)
    outcomes_by_id = common._outcome_index(outcome_rows, counters)

    cohort = []
    for forecast_id, forecast in forecasts.items():
        if forecast_id in duplicate_ids:
            counters["excluded_duplicate_forecast_ids"] += 1
            continue
        if not common._exact_dex_identity(forecast):
            counters["excluded_incomplete_exact_identity"] += 1
            continue
        snapshot_time = common._parse_utc(forecast.get("snapshot_utc"))
        if snapshot_time is None:
            counters["excluded_invalid_forecast_timestamp"] += 1
            continue
        signal, signal_problem = _score_snapshot(forecast)
        if signal_problem:
            counters[f"signal:{signal_problem}"] += 1
        outcome, outcome_state = common._evaluate_outcome(
            forecast, outcomes_by_id.get(forecast_id, [])
        )
        counters[f"outcome:{outcome_state}"] += 1
        cohort.append({
            "forecast_id": forecast_id,
            "snapshot_time": snapshot_time,
            "score": signal.get("score") if signal is not None else None,
            "signal": signal,
            "signal_problem": signal_problem,
            "outcome": outcome,
            "outcome_state": outcome_state,
        })

    training = [item for item in cohort if item["snapshot_time"] < cutoff]
    holdout = [item for item in cohort if item["snapshot_time"] >= cutoff]
    for split_name, split_rows in (("training", training), ("holdout", holdout)):
        if not any(item["score"] is not None and item["outcome"] is not None for item in split_rows):
            raise common.AnalysisError(
                f"no prospective v4.25 score has a unique eligible 24-hour outcome in the {split_name} split"
            )
    training_scores = [item["score"] for item in training if item["score"] is not None]
    score_threshold = common._median(training_scores)
    split_results = {}
    grouped_results = {}
    for name, items in (("training", training), ("holdout", holdout)):
        states = collections.Counter(item["outcome_state"] for item in items)
        marked = [item for item in items if item["outcome"] is not None]
        marked_scored = [item for item in marked if item["score"] is not None]
        split_results[name] = {
            "forecast_count": len(items),
            "score_present_count": sum(item["score"] is not None for item in items),
            "score_missing_or_unusable_count": sum(item["score"] is None for item in items),
            "outcome_state_counts": dict(sorted(states.items())),
            "eligible_bounded_endpoint_marks": len(marked),
            "eligible_endpoint_marks_without_score": len(marked) - len(marked_scored),
            "eligible_endpoint_marks_with_score": len(marked_scored),
            "endpoint_return_distribution_with_score": common._distribution([
                item["outcome"]["mark_return_pct"] for item in marked_scored
            ]),
        }
        groups = {
            "score_at_or_below_training_median": [],
            "score_above_training_median": [],
        }
        if score_threshold is not None:
            for item in marked_scored:
                group = (
                    "score_at_or_below_training_median"
                    if item["score"] <= score_threshold
                    else "score_above_training_median"
                )
                groups[group].append(item["outcome"]["mark_return_pct"])
        grouped_results[name] = {
            label: common._distribution(values) for label, values in groups.items()
        }

    analysis = {
        "analysis_version": ANALYSIS_VERSION,
        "source_access": "read-only; no provider requests",
        "source_snapshot_id": snapshot_id,
        "source_files": source_files,
        "scope": {
            "signal_version": SIGNAL_VERSION,
            "model_id": MODEL_ID,
            "feature": "feature_snapshot.v425_early_breakout.score",
            "feature_interpretation": "prospectively captured 0-100 relative heuristic score; not a probability",
            "signal_provenance_requirement": (
                "versioned score includes a source label, source URL, and retrieved_utc that is not "
                f"future-dated and is at most {MAX_SIGNAL_AGE_MINUTES:g} minutes before forecast snapshot"
            ),
            "exact_identity": "DEX chain + token contract/mint + pool/market address, plus nonempty forecast identity and market_key",
            "outcome_join": (
                "unique forecast_id; exact market_key and forecast_utc match; "
                "horizon_hours=24; OBSERVED_MARK_NOT_EXECUTABLE_FILL"
            ),
            "outcome_lag_hours": {
                "minimum_inclusive": 0,
                "maximum_inclusive": common.MAX_OBSERVATION_LAG_HOURS,
            },
            "time_split_cutoff_utc": _iso_utc(cutoff),
            "training_period": {
                "first": _iso_utc(min((item["snapshot_time"] for item in training), default=None)),
                "last": _iso_utc(max((item["snapshot_time"] for item in training), default=None)),
            },
            "holdout_period": {
                "first": _iso_utc(min((item["snapshot_time"] for item in holdout), default=None)),
                "last": _iso_utc(max((item["snapshot_time"] for item in holdout), default=None)),
            },
            "training_score_median": score_threshold,
            "threshold_derived_from_training_scores_without_outcome_labels": True,
            "group_boundary": "low <= training score median; high > training score median",
        },
        "record_counts": {
            "forecast_source_records": len(forecast_rows),
            "outcome_source_records": len(outcome_rows),
            "v425_signal_snapshots": len(forecasts),
            "usable_exact_identity_forecasts": len(cohort),
            **dict(sorted(counters.items())),
        },
        "forecast_provenance": common._run_provenance_summary(list(forecasts.values())),
        "time_split": split_results,
        "endpoint_return_distributions_by_score_group": grouped_results,
        "interpretation": (
            "Exploratory prospective endpoint-mark associations only. These observations are not "
            "target-touch rates, backtest performance, profitability, or evidence of effectiveness."
        ),
        "limitations": [
            "Only prospective, version-matched v4.25 snapshots are considered; historical scores are not reconstructed.",
            "Endpoint marks do not establish intrahorizon target touches, timing, maximum adverse excursion, or executable returns.",
            "Missing, duplicate, unavailable, early, and late outcomes are coverage states, never negative returns.",
            "Forecasts can overlap for the same market; observations are not independent samples.",
            "The provider-selected discovery cohort is not full-universe coverage.",
            "No minimum sample-size or effectiveness claim is implied by this exploratory report.",
        ],
    }
    common._verify_source_snapshot(memory_dir, forecast_paths, outcome_paths, stats)
    return analysis


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def write_report(
    memory_dir: Path = DEFAULT_MEMORY,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    split_cutoff_utc: str | dt.datetime | None = None,
) -> Path:
    if split_cutoff_utc is None:
        raise ValueError("an explicit split_cutoff_utc is required for reproducibility")
    if isinstance(split_cutoff_utc, str):
        cutoff = _parse_cutoff(split_cutoff_utc)
    elif split_cutoff_utc.tzinfo is None:
        raise ValueError("split cutoff must include a timezone")
    else:
        cutoff = split_cutoff_utc.astimezone(dt.timezone.utc)
    memory_dir, output_root = Path(memory_dir), Path(output_root)
    common._ensure_disjoint_paths(memory_dir, output_root)
    _, _, _, source_files, snapshot_id = common._source_snapshot(memory_dir)
    config_id = hashlib.sha256(
        f"{ANALYSIS_VERSION}|{snapshot_id}|{cutoff.isoformat()}".encode("utf-8")
    ).hexdigest()
    output_dir = output_root / f"phase4-v425-shadow-v1-{config_id[:24]}"
    if output_dir.exists():
        if output_dir.is_symlink() or not output_dir.is_dir():
            raise common.AnalysisError(f"research output must be a regular directory: {output_dir}")
        manifest_path = output_dir / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise common.AnalysisError(f"research output manifest must be a regular file: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise common.AnalysisError(f"refusing to overwrite unmanaged research output: {output_dir}") from exc
        if (not isinstance(manifest, dict) or manifest.get("managed_by") != MANAGED_BY
                or manifest.get("configuration_id") != config_id):
            raise common.AnalysisError(f"refusing to overwrite a different research output: {output_dir}")
        hashes = manifest.get("output_files_sha256")
        if not isinstance(hashes, dict) or set(hashes) != {"report.json"}:
            raise common.AnalysisError(f"research output manifest is incomplete: {manifest_path}")
        path = output_dir / "report.json"
        if path.is_symlink() or not path.is_file() or common._sha256_file(path) != hashes["report.json"]:
            raise common.AnalysisError("research output failed checksum validation: report.json")
        return output_dir

    report = analyze_ledgers(memory_dir, cutoff)
    if output_root.is_symlink():
        raise common.AnalysisError(f"research output root must not be a symlink: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    if output_dir.is_symlink() or output_dir.exists():
        raise common.AnalysisError(f"unsafe or concurrent research output path: {output_dir}")
    _, _, _, _, current_snapshot_id = common._source_snapshot(memory_dir)
    if current_snapshot_id != snapshot_id:
        raise common.AnalysisError("source partitions changed between snapshot and report publication")
    staging = Path(tempfile.mkdtemp(prefix=".phase4-v425-shadow-", dir=output_root))
    try:
        common._write_json(staging / "report.json", report)
        output_hash = common._sha256_file(staging / "report.json")
        common._write_json(staging / "manifest.json", {
            "managed_by": MANAGED_BY,
            "analysis_version": ANALYSIS_VERSION,
            "configuration_id": config_id,
            "source_snapshot_id": snapshot_id,
            "source_files": source_files,
            "split_cutoff_utc": _iso_utc(cutoff),
            "output_files_sha256": {"report.json": output_hash},
        })
        if output_dir.exists():
            raise common.AnalysisError(f"research output appeared during analysis: {output_dir}")
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(output_dir)
    except Exception:
        for child in staging.iterdir():
            child.unlink()
        staging.rmdir()
        raise
    return output_dir


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--split-cutoff-utc", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        output = write_report(args.memory_dir, args.output_root, args.split_cutoff_utc)
    except (common.AnalysisError, ValueError) as exc:
        print(f"v4.25 shadow analysis failed: {exc}")
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
