#!/usr/bin/env python3
"""Explore detection-time momentum against later scheduled endpoint marks."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import hashlib
import json
import math
import statistics
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEMORY = ROOT / "rocket-hunter-data" / "scanner-memory"
DEFAULT_OUTPUT_ROOT = ROOT / "rocket-hunter-data" / "research-analysis"
ANALYSIS_VERSION = "rocket-hunter-phase4-shadow.v1"
MANAGED_BY = "rocket-hunter-analyze-phase4-shadow"
SIGNAL_SOURCE = "dexscreener_search"
SIGNAL_FIELD = "change_1h_pct"
MODEL_ID = "v4.19.0-eb-beta-binomial"
HORIZON_HOURS = 24
MAX_OBSERVATION_LAG_HOURS = 6.0
MAX_SIGNAL_AGE_MINUTES = 30.0
SPLIT_CUTOFF_UTC = dt.datetime(2026, 11, 6, tzinfo=dt.timezone.utc)
DEFAULT_IDENTITY_KEYS = ("pool_address", "pair_address", "gecko_pool_id")


class AnalysisError(RuntimeError):
    """Raised when source data changes or an output path is unsafe."""


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _run_provenance_summary(forecasts: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for field in ("scanner_version", "configuration_fingerprint"):
        counts = collections.Counter()
        missing = 0
        for forecast in forecasts:
            value = forecast.get(field)
            if isinstance(value, str) and value.strip():
                counts[value.strip()] += 1
            else:
                missing += 1
        result[f"{field}_counts"] = dict(sorted(counts.items()))
        result[f"missing_{field}_count"] = missing
    return result


def _source_files(memory_dir: Path) -> tuple[list[Path], list[Path]]:
    forecast_dir = memory_dir / "forecasts"
    outcome_dir = memory_dir / "outcomes"
    if memory_dir.is_symlink() or forecast_dir.is_symlink() or outcome_dir.is_symlink():
        raise AnalysisError("memory and ledger directories must not be symlinks")
    if not forecast_dir.is_dir() or not outcome_dir.is_dir():
        raise AnalysisError("forecast and outcome ledger directories are both required")
    forecasts = sorted(forecast_dir.glob("*.jsonl.gz"), key=lambda path: path.name)
    outcomes = sorted(outcome_dir.glob("*.jsonl.gz"), key=lambda path: path.name)
    for path in forecasts + outcomes:
        if path.is_symlink() or not path.is_file():
            raise AnalysisError(f"source partitions must be regular files: {path}")
    return forecasts, outcomes


def _source_snapshot(memory_dir: Path):
    forecast_paths, outcome_paths = _source_files(memory_dir)
    files = []
    stats = {}
    digest = hashlib.sha256()
    for dataset, paths in (("forecasts", forecast_paths), ("outcomes", outcome_paths)):
        for path in paths:
            stat = path.stat()
            item = {
                "dataset": dataset,
                "path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                "sha256": _sha256_file(path),
                "size_bytes": stat.st_size,
            }
            files.append(item)
            stats[path] = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, item["sha256"])
            digest.update(_json_text(item).encode("utf-8"))
    digest.update(_json_text({
        "analysis_version": ANALYSIS_VERSION,
        "signal_source": SIGNAL_SOURCE,
        "signal_field": SIGNAL_FIELD,
        "model_id": MODEL_ID,
        "split_cutoff_utc": SPLIT_CUTOFF_UTC.isoformat(),
    }).encode("utf-8"))
    return forecast_paths, outcome_paths, stats, files, digest.hexdigest()


def _verify_source_snapshot(memory_dir: Path, forecast_paths, outcome_paths, stats) -> None:
    current_forecasts, current_outcomes = _source_files(memory_dir)
    if current_forecasts != forecast_paths or current_outcomes != outcome_paths:
        raise AnalysisError("source partition list changed during analysis")
    for path, before in stats.items():
        after = path.stat()
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns, _sha256_file(path)) != before:
            raise AnalysisError(f"source partition changed during analysis: {path}")


def _read_jsonl_gz(paths: list[Path], dataset: str, counters: collections.Counter[str]):
    records = []
    for path in paths:
        try:
            with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    counters[f"{dataset}_source_records"] += 1
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise AnalysisError(
                            f"malformed JSON in {path} at line {line_number}: {exc}"
                        ) from exc
                    if not isinstance(record, dict):
                        counters[f"{dataset}_non_object_records"] += 1
                        continue
                    records.append(record)
        except (OSError, EOFError, UnicodeDecodeError) as exc:
            raise AnalysisError(f"cannot read {dataset} partition {path}: {exc}") from exc
    return records


def _exact_dex_identity(forecast: dict[str, Any]) -> bool:
    features = forecast.get("feature_snapshot")
    if not isinstance(features, dict):
        return False
    market_key = forecast.get("market_key")
    market_type = str(features.get("market_type") or "").lower()
    is_dex = market_type.startswith("dex") or (isinstance(market_key, str) and market_key.startswith("dex:"))
    pool = next((features.get(field) for field in DEFAULT_IDENTITY_KEYS if features.get(field)), None)
    return bool(
        is_dex
        and isinstance(forecast.get("identity"), str) and forecast["identity"].strip()
        and isinstance(market_key, str) and market_key.strip()
        and isinstance(features.get("chain"), str) and features["chain"].strip()
        and isinstance(features.get("contract_address") or features.get("token_mint"), str)
        and (features.get("contract_address") or features.get("token_mint")).strip()
        and isinstance(pool, str) and pool.strip()
    )


def _signal_and_provenance(forecast: dict[str, Any], counters: collections.Counter[str]):
    features = forecast.get("feature_snapshot")
    if not isinstance(features, dict) or features.get("source") != SIGNAL_SOURCE:
        return None, None, "source_mismatch"
    source_url = features.get("source_url")
    if not isinstance(source_url, str) or not source_url.strip():
        counters["missing_signal_source_url"] += 1
        return None, None, "missing_source_url"
    forecast_time = _parse_utc(forecast.get("snapshot_utc"))
    retrieved_time = _parse_utc(features.get("retrieved_utc"))
    if forecast_time is None or retrieved_time is None:
        counters["missing_signal_timestamp"] += 1
        return None, None, "missing_signal_timestamp"
    age_minutes = (forecast_time - retrieved_time).total_seconds() / 60.0
    if age_minutes < 0 or age_minutes > MAX_SIGNAL_AGE_MINUTES:
        counters["stale_or_future_signal_timestamp"] += 1
        return None, None, "stale_or_future_signal_timestamp"
    value = _finite_number(features.get(SIGNAL_FIELD))
    if value is None:
        counters["missing_signal_value"] += 1
        return None, None, "missing_signal_value"
    return value, {
        "source": SIGNAL_SOURCE,
        "source_url_present": True,
        "retrieved_utc": features["retrieved_utc"],
        "age_minutes": age_minutes,
    }, None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _distribution(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    ordered = sorted(values)

    def quantile(probability: float) -> float:
        index = (len(ordered) - 1) * probability
        lower = math.floor(index)
        upper = math.ceil(index)
        if lower == upper:
            return ordered[lower]
        weight = index - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    return {
        "count": len(ordered),
        "minimum": ordered[0],
        "p25": quantile(0.25),
        "median": quantile(0.5),
        "p75": quantile(0.75),
        "maximum": ordered[-1],
    }


def _outcome_index(outcomes: list[dict[str, Any]], counters: collections.Counter[str]):
    indexed: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for outcome in outcomes:
        if outcome.get("horizon_hours") != HORIZON_HOURS:
            continue
        forecast_id = outcome.get("forecast_id")
        if isinstance(forecast_id, str) and forecast_id:
            indexed[forecast_id].append(outcome)
        else:
            counters["outcome_missing_forecast_id"] += 1
    for records in indexed.values():
        if len(records) > 1:
            counters["duplicate_24h_outcome_forecasts"] += 1
    return indexed


def _evaluate_outcome(forecast: dict[str, Any], rows: list[dict[str, Any]]):
    if not rows:
        return None, "MISSING_OUTCOME_ROW"
    if len(rows) != 1:
        return None, "DUPLICATE_OUTCOME_ROWS"
    outcome = rows[0]
    if outcome.get("market_key") != forecast.get("market_key"):
        return None, "MARKET_KEY_MISMATCH"
    if outcome.get("forecast_utc") != forecast.get("snapshot_utc"):
        return None, "FORECAST_TIME_MISMATCH"
    if outcome.get("status") != "OBSERVED_MARK_NOT_EXECUTABLE_FILL":
        status = outcome.get("status")
        return None, status if isinstance(status, str) and status else "MISSING_OUTCOME_STATUS"
    mark_return = _finite_number(outcome.get("mark_return_pct"))
    base_price = _finite_number(outcome.get("base_price_usd"))
    observed_price = _finite_number(outcome.get("observed_price_usd"))
    observed_time = _parse_utc(outcome.get("observed_utc"))
    forecast_time = _parse_utc(forecast.get("snapshot_utc"))
    if (mark_return is None or base_price is None or base_price <= 0
            or observed_price is None or observed_price <= 0
            or observed_time is None or forecast_time is None):
        return None, "INVALID_MARK_OR_TIMESTAMP"
    expected_return = (observed_price / base_price - 1.0) * 100.0
    if abs(expected_return - mark_return) > max(0.000002, abs(mark_return) * 1e-10):
        return None, "MARK_RETURN_PRICE_MISMATCH"
    lag_hours = (observed_time - (forecast_time + dt.timedelta(hours=HORIZON_HOURS))).total_seconds() / 3600.0
    if lag_hours < 0:
        return None, "MARK_BEFORE_SCHEDULED_HORIZON"
    if lag_hours > MAX_OBSERVATION_LAG_HOURS:
        return None, "MARK_AFTER_LAG_LIMIT"
    return {"mark_return_pct": mark_return, "lag_hours": lag_hours}, "ELIGIBLE_ENDPOINT_MARK"


def analyze_ledgers(memory_dir: Path = DEFAULT_MEMORY) -> dict[str, Any]:
    memory_dir = Path(memory_dir)
    forecast_paths, outcome_paths, stats, source_files, snapshot_id = _source_snapshot(memory_dir)
    counters: collections.Counter[str] = collections.Counter()
    forecast_rows = _read_jsonl_gz(forecast_paths, "forecast", counters)
    outcome_rows = _read_jsonl_gz(outcome_paths, "outcome", counters)

    forecasts: dict[str, dict[str, Any]] = {}
    duplicate_forecast_ids: set[str] = set()
    for forecast in forecast_rows:
        forecast_id = forecast.get("forecast_id")
        if not isinstance(forecast_id, str) or not forecast_id:
            counters["forecast_missing_id"] += 1
            continue
        if forecast_id in forecasts:
            duplicate_forecast_ids.add(forecast_id)
        else:
            forecasts[forecast_id] = forecast
    counters["duplicate_forecast_ids"] = len(duplicate_forecast_ids)
    outcomes_by_id = _outcome_index(outcome_rows, counters)

    cohort = []
    source_candidates = 0
    source_candidate_forecasts = []
    for forecast_id, forecast in forecasts.items():
        features = forecast.get("feature_snapshot")
        if not isinstance(features, dict) or features.get("source") != SIGNAL_SOURCE:
            continue
        if forecast.get("model_id") != MODEL_ID:
            continue
        source_candidates += 1
        source_candidate_forecasts.append(forecast)
        if forecast_id in duplicate_forecast_ids:
            counters["excluded_duplicate_forecast_ids"] += 1
            continue
        if not _exact_dex_identity(forecast):
            counters["excluded_incomplete_exact_identity"] += 1
            continue
        snapshot_time = _parse_utc(forecast.get("snapshot_utc"))
        if snapshot_time is None:
            counters["excluded_invalid_forecast_timestamp"] += 1
            continue
        signal, provenance, signal_problem = _signal_and_provenance(forecast, counters)
        outcome, outcome_state = _evaluate_outcome(
            forecast, outcomes_by_id.get(forecast_id, []))
        if outcome_state == "ELIGIBLE_ENDPOINT_MARK":
            counters["bounded_endpoint_marks"] += 1
        elif outcome_state == "MISSING_OUTCOME_ROW":
            counters["missing_outcome_rows"] += 1
        elif outcome_state == "UNAVAILABLE_IDENTITY_NOT_OBSERVED":
            counters["unavailable_identity_not_observed"] += 1
        elif outcome_state == "UNAVAILABLE_NO_VALID_PRICE_MARK":
            counters["unavailable_no_valid_price_mark"] += 1
        else:
            counters[f"excluded_outcome:{outcome_state}"] += 1
        cohort.append({
            "forecast_id": forecast_id,
            "snapshot_time": snapshot_time,
            "signal": signal,
            "signal_problem": signal_problem,
            "signal_provenance": provenance,
            "outcome": outcome,
            "outcome_state": outcome_state,
        })

    training = [item for item in cohort if item["snapshot_time"] < SPLIT_CUTOFF_UTC]
    holdout = [item for item in cohort if item["snapshot_time"] >= SPLIT_CUTOFF_UTC]
    training_signal = [item["signal"] for item in training if item["signal"] is not None]
    signal_threshold = _median(training_signal)
    counters["source_model_candidates"] = source_candidates
    counters["cohort_forecasts"] = len(cohort)

    split_results = {}
    for name, items in (("training", training), ("holdout", holdout)):
        status_counts = collections.Counter(item["outcome_state"] for item in items)
        marks_without_signal = [
            item["outcome"]["mark_return_pct"]
            for item in items if item["outcome"] is not None and item["signal"] is None
        ]
        marked_with_signal = [
            item for item in items
            if item["outcome"] is not None and item["signal"] is not None
        ]
        split_results[name] = {
            "forecast_count": len(items),
            "signal_present_count": sum(item["signal"] is not None for item in items),
            "signal_missing_or_unusable_count": sum(item["signal"] is None for item in items),
            "outcome_state_counts": dict(sorted(status_counts.items())),
            "eligible_bounded_endpoint_marks": sum(
                item["outcome"] is not None for item in items
            ),
            "eligible_endpoint_marks_without_signal": len(marks_without_signal),
            "eligible_endpoint_marks_with_signal": len(marked_with_signal),
            "endpoint_return_distribution_with_signal": _distribution([
                item["outcome"]["mark_return_pct"] for item in marked_with_signal
            ]),
        }

    groups = {}
    if signal_threshold is not None:
        for name, items in (("training", training), ("holdout", holdout)):
            grouped = {
                "signal_at_or_below_training_median": [],
                "signal_above_training_median": [],
            }
            for item in items:
                if item["signal"] is None or item["outcome"] is None:
                    continue
                label = (
                    "signal_at_or_below_training_median"
                    if item["signal"] <= signal_threshold
                    else "signal_above_training_median"
                )
                grouped[label].append(item["outcome"]["mark_return_pct"])
            groups[name] = {
                label: _distribution(values)
                for label, values in grouped.items()
            }

    forecast_times = [item["snapshot_time"] for item in cohort]
    analysis = {
        "analysis_version": ANALYSIS_VERSION,
        "source_access": "read-only; no provider requests",
        "source_snapshot_id": snapshot_id,
        "source_files": source_files,
        "scope": {
            "forecast_source": SIGNAL_SOURCE,
            "model_id": MODEL_ID,
            "feature": SIGNAL_FIELD,
            "feature_interpretation": "source-reported 1-hour price change frozen in the forecast feature snapshot",
            "exact_identity": "DEX chain + token contract/mint + pool/market address, plus nonempty forecast identity and market_key",
            "signal_provenance_requirement": (
                "feature source URL and retrieved_utc are present; retrieval is not future-dated "
                f"and is at most {MAX_SIGNAL_AGE_MINUTES:g} minutes before forecast snapshot"
            ),
            "outcome_join": (
                "unique forecast_id; exact market_key and forecast_utc match; "
                "horizon_hours=24; OBSERVED_MARK_NOT_EXECUTABLE_FILL"
            ),
            "outcome_lag_hours": {
                "minimum_inclusive": 0,
                "maximum_inclusive": MAX_OBSERVATION_LAG_HOURS,
            },
            "time_split_cutoff_utc": SPLIT_CUTOFF_UTC.isoformat().replace("+00:00", "Z"),
            "training_period": (
                f"{min((item['snapshot_time'] for item in training), default=None)} through "
                f"{max((item['snapshot_time'] for item in training), default=None)}"
            ),
            "holdout_period": (
                f"{min((item['snapshot_time'] for item in holdout), default=None)} through "
                f"{max((item['snapshot_time'] for item in holdout), default=None)}"
            ),
            "threshold": {
                "training_feature_median_pct": signal_threshold,
                "computed_from_training_feature_values_without_outcome_labels": True,
                "group_boundary": "low <= training median; high > training median",
            },
        },
        "record_counts": {
            **dict(sorted(counters.items())),
            "forecasts_loaded": len(forecasts),
            "forecast_source_records": len(forecast_rows),
            "outcome_source_records": len(outcome_rows),
            "outcome_24h_records": sum(
                outcome.get("horizon_hours") == HORIZON_HOURS for outcome in outcome_rows
            ),
            "outcome_duplicate_ids": counters["duplicate_24h_outcome_forecasts"],
        },
        "forecast_provenance": _run_provenance_summary(source_candidate_forecasts),
        "time_split": split_results,
        "endpoint_return_distributions_by_signal_group": groups,
        "interpretation": (
            "Exploratory endpoint-mark associations only. These observations are not "
            "target-touch rates, backtest performance, profitability, or evidence of effectiveness."
        ),
        "limitations": [
            "The signal is a single provider-reported 1-hour momentum feature, not the v4.25 early-breakout composite.",
            "The v4.25 composite score and component fields are not captured in the canonical forecast feature snapshot.",
            "Endpoint marks do not establish intrahorizon target touches, timing, maximum adverse excursion, or executable returns.",
            "Unavailable/missing outcomes are reported as missing coverage, never as negative returns.",
            "Forecasts can overlap for the same market; observations are not independent samples.",
            "The source cohort is a provider-selected DEX Screener search sample, not the full market universe.",
            "Training and holdout cover a short historical interval; this exploratory split cannot establish generalization or effectiveness.",
        ],
    }
    _verify_source_snapshot(memory_dir, forecast_paths, outcome_paths, stats)
    return analysis


def _render_markdown(analysis: dict[str, Any]) -> str:
    scope = analysis["scope"]
    provenance = analysis["forecast_provenance"]
    lines = [
        "# Phase 4 shadow signal analysis",
        "",
        f"- Analysis version: `{analysis['analysis_version']}`",
        f"- Source snapshot: `{analysis['source_snapshot_id']}`",
        f"- Forecast source/model: `{scope['forecast_source']}` / `{scope['model_id']}`",
        f"- Detection-time signal: `{scope['feature']}`",
        f"- Time split cutoff: {scope['time_split_cutoff_utc']}",
        f"- Forecast run provenance: {sum(provenance['scanner_version_counts'].values())} scanner-versioned / "
        f"{provenance['missing_scanner_version_count']} missing; "
        f"{sum(provenance['configuration_fingerprint_counts'].values())} configuration-fingerprinted / "
        f"{provenance['missing_configuration_fingerprint_count']} missing.",
        "",
        "**Interpretation: exploratory endpoint-mark associations only.** Not target-touch rates, backtest performance, profitability, or evidence of effectiveness.",
        "",
        "## Coverage by time split",
        "",
        "| Split | Forecasts | Signal present | Signal missing/unusable | Eligible bounded 24h endpoint marks | Marks without signal | Marks with signal |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("training", "holdout"):
        item = analysis["time_split"][name]
        lines.append(
            f"| {name} | {item['forecast_count']:,} | {item['signal_present_count']:,} "
            f"| {item['signal_missing_or_unusable_count']:,} "
            f"| {item['eligible_bounded_endpoint_marks']:,} "
            f"| {item['eligible_endpoint_marks_without_signal']:,} "
            f"| {item['eligible_endpoint_marks_with_signal']:,} |"
        )
        lines.append(f"\n{ name.capitalize()} outcome states: `{json.dumps(item['outcome_state_counts'], sort_keys=True)}`.")
    lines.extend([
        "",
        "The training median signal threshold was "
        f"**{scope['threshold']['training_feature_median_pct']:.6g}%** "
        "and was derived from training features only, without outcome labels. "
        "Groups are signal <= that median and signal > that median.",
        "",
        "## Endpoint-return distributions",
        "",
        "Returns are observed scheduled 24-hour marks, accepted only from 0 to 6 hours after the scheduled horizon and joined by unique forecast ID, exact market key, and forecast timestamp.",
        "",
        "| Split | Training-defined signal group | n | Min (%) | P25 (%) | Median (%) | P75 (%) | Max (%) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    for split_name in ("training", "holdout"):
        for group_name in ("signal_at_or_below_training_median", "signal_above_training_median"):
            row = analysis["endpoint_return_distributions_by_signal_group"].get(
                split_name, {}).get(group_name)
            if row is None:
                lines.append(f"| {split_name} | {group_name} | 0 | n/a | n/a | n/a | n/a | n/a |")
            else:
                lines.append(
                    f"| {split_name} | {group_name} | {row['count']:,} "
                    f"| {row['minimum']:.6g} | {row['p25']:.6g} | {row['median']:.6g} "
                    f"| {row['p75']:.6g} | {row['maximum']:.6g} |"
                )
    lines.extend([
        "",
        "## Limits",
        "",
        *[f"- {limitation}" for limitation in analysis["limitations"]],
        "",
        "No collection, provider request, scanner-memory write, ranking change, or qualification-gate change is performed by this analysis.",
        "",
    ])
    return "\n".join(lines)


def _ensure_disjoint_paths(memory_dir: Path, output_root: Path) -> None:
    output = output_root.resolve()
    protected_paths = (
        memory_dir.resolve(),
        (ROOT / "rocket-hunter-data" / "research-data").resolve(),
        (ROOT / "rocket-hunter-data" / "research-exports").resolve(),
        (ROOT / "rocket-runs").resolve(),
    )
    if any(output == path or output in path.parents or path in output.parents for path in protected_paths):
        raise AnalysisError("shadow output must be separate from memory, observations, exports, and scan outputs")


def _existing_report(output_dir: Path, snapshot_id: str) -> Path | None:
    if not output_dir.exists():
        return None
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise AnalysisError(f"research output path is not a regular directory: {output_dir}")
    manifest_path = output_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"refusing to overwrite unmanaged research output: {output_dir}") from exc
    if (not isinstance(manifest, dict)
            or manifest.get("managed_by") != MANAGED_BY
            or manifest.get("analysis_version") != ANALYSIS_VERSION
            or manifest.get("source_snapshot_id") != snapshot_id):
        raise AnalysisError(f"refusing to overwrite a different research output: {output_dir}")
    hashes = manifest.get("output_files_sha256")
    if not isinstance(hashes, dict) or set(hashes) != {"report.json", "report.md"}:
        raise AnalysisError(f"research output manifest is incomplete: {manifest_path}")
    for name, digest in hashes.items():
        path = output_dir / name
        if path.is_symlink() or not path.is_file() or _sha256_file(path) != digest:
            raise AnalysisError(f"research output failed checksum validation: {name}")
    return output_dir


def write_report(memory_dir: Path = DEFAULT_MEMORY,
                 output_root: Path = DEFAULT_OUTPUT_ROOT) -> Path:
    memory_dir = Path(memory_dir)
    output_root = Path(output_root)
    _ensure_disjoint_paths(memory_dir, output_root)
    forecast_paths, outcome_paths, stats, source_files, snapshot_id = _source_snapshot(memory_dir)
    output_dir = output_root / f"phase4-shadow-v1-{snapshot_id[:24]}"
    existing = _existing_report(output_dir, snapshot_id)
    if existing is not None:
        return existing
    analysis = analyze_ledgers(memory_dir)
    if analysis["source_snapshot_id"] != snapshot_id:
        raise AnalysisError("source partitions changed between snapshot and analysis")
    markdown = _render_markdown(analysis)
    output_root.mkdir(parents=True, exist_ok=True)
    if output_root.is_symlink():
        raise AnalysisError(f"research output root must not be a symlink: {output_root}")
    staging = Path(tempfile.mkdtemp(prefix=".phase4-shadow-", dir=output_root))
    try:
        _write_json(staging / "report.json", analysis)
        (staging / "report.md").write_text(markdown, encoding="utf-8")
        output_hashes = {
            name: _sha256_file(staging / name) for name in ("report.json", "report.md")
        }
        _write_json(staging / "manifest.json", {
            "managed_by": MANAGED_BY,
            "analysis_version": ANALYSIS_VERSION,
            "source_snapshot_id": snapshot_id,
            "source_files": source_files,
            "output_files_sha256": output_hashes,
        })
        _verify_source_snapshot(memory_dir, forecast_paths, outcome_paths, stats)
        if output_dir.exists():
            raise AnalysisError(f"research output appeared during analysis: {output_dir}")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        output_dir = write_report(args.memory_dir, args.output_root)
    except AnalysisError as exc:
        print(f"Phase 4 shadow analysis failed: {exc}")
        return 1
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
