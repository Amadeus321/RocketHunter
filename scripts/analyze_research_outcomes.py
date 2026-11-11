#!/usr/bin/env python3
"""Build a deterministic, read-only outcome coverage and mark analysis."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEMORY = ROOT / "rocket-hunter-data" / "scanner-memory"
DEFAULT_OUTPUT_ROOT = ROOT / "rocket-hunter-data" / "research-analysis"
DEFAULT_RUNS = ROOT / "rocket-runs"
DEFAULT_EXPORTS = ROOT / "rocket-hunter-data" / "research-exports"
ANALYSIS_VERSION = "rocket-hunter-outcome-analysis.v1.1"
MANAGED_BY = "rocket-hunter-analyze-research-outcomes"
TARGETS_PCT = (40, 60, 100)
MAX_OBSERVATION_LAG_HOURS = 6.0
OBSERVED_STATUS = "OBSERVED_MARK_NOT_EXECUTABLE_FILL"


class AnalysisError(RuntimeError):
    """Raised when a consistent read-only analysis cannot be produced."""


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _reject_json_constant(value: str):
    raise ValueError(f"non-standard JSON constant: {value}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_partitions(memory_dir: Path, dataset: str) -> list[Path]:
    directory = memory_dir / dataset
    if not directory.is_dir():
        raise AnalysisError(f"required source directory is missing: {directory}")
    paths = sorted(directory.glob("*.jsonl.gz"), key=lambda path: path.name)
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise AnalysisError(f"source partitions must be regular files: {directory}")
    return paths


def _source_snapshot(memory_dir: Path):
    partitions = {name: _source_partitions(memory_dir, name) for name in ("forecasts", "outcomes")}
    stats = {path: path.stat() for paths in partitions.values() for path in paths}
    sources = []
    digest = hashlib.sha256()
    for dataset, paths in partitions.items():
        for path in paths:
            item = {
                "dataset": dataset,
                "partition": path.name,
                "sha256": _sha256_file(path),
                "compressed_size_bytes": stats[path].st_size,
            }
            sources.append(item)
            digest.update(_json_text(item).encode("utf-8"))
    return partitions, stats, sources, digest.hexdigest()


def _verify_source_snapshot(memory_dir: Path, partitions, stats, sources) -> None:
    current_partitions = {
        name: _source_partitions(memory_dir, name) for name in ("forecasts", "outcomes")
    }
    if current_partitions != partitions:
        raise AnalysisError("source partition list changed during analysis")
    expected_hashes = {(item["dataset"], item["partition"]): item["sha256"] for item in sources}
    for path, before in stats.items():
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ) or _sha256_file(path) != expected_hashes[(path.parent.name, path.name)]:
            raise AnalysisError(f"source partition changed during analysis: {path.name}")


def _read_partition(path: Path, dataset: str, counts: dict[str, int],
                    rejections: collections.Counter[str]):
    records = []
    try:
        stream = gzip.open(path, "rt", encoding="utf-8", newline="")
        with stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                counts["source_records"] += 1
                try:
                    record = json.loads(line, parse_constant=_reject_json_constant)
                except (json.JSONDecodeError, ValueError):
                    rejections[f"{dataset}:malformed_json"] += 1
                    continue
                if not isinstance(record, dict):
                    rejections[f"{dataset}:non_object_record"] += 1
                    continue
                records.append(record)
    except (OSError, EOFError, UnicodeDecodeError, zlib.error):
        rejections[f"{dataset}:malformed_gzip_partition"] += 1
    return records


def _load_records(partitions, dataset: str, rejections: collections.Counter[str]):
    counts = {"source_records": 0, "accepted_records": 0, "rejected_records": 0}
    records = []
    for path in partitions[dataset]:
        records.extend(_read_partition(path, dataset, counts, rejections))
    return records, counts


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


def _load_forecasts(partitions, rejections):
    rows, counts = _load_records(partitions, "forecasts", rejections)
    forecasts = {}
    versions = collections.defaultdict(collections.Counter)
    dates = []
    feature_snapshots = 0
    for record in rows:
        forecast_id = record.get("forecast_id")
        if not isinstance(forecast_id, str) or not forecast_id:
            rejections["forecasts:missing_forecast_id"] += 1
            continue
        if forecast_id in forecasts:
            rejections["forecasts:duplicate_forecast_id"] += 1
            continue
        forecasts[forecast_id] = record
        counts["accepted_records"] += 1
        for field in ("schema", "pipeline_version", "core_runtime_version", "collector_overlay_version"):
            value = record.get(field)
            if isinstance(value, str) and value:
                versions[field][value] += 1
        snapshot = record.get("snapshot_utc")
        if isinstance(snapshot, str):
            dates.append(snapshot)
        if isinstance(record.get("feature_snapshot"), dict):
            feature_snapshots += 1
    counts["rejected_records"] = counts["source_records"] - counts["accepted_records"]
    return forecasts, counts, versions, dates, feature_snapshots


def _load_outcomes(partitions, forecasts, rejections):
    rows, counts = _load_records(partitions, "outcomes", rejections)
    outcomes = collections.defaultdict(dict)
    orphan_count = 0
    for record in rows:
        forecast_id = record.get("forecast_id")
        horizon = record.get("horizon_hours")
        if not isinstance(forecast_id, str) or not forecast_id:
            rejections["outcomes:missing_forecast_id"] += 1
            continue
        if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
            rejections["outcomes:invalid_horizon_hours"] += 1
            continue
        if forecast_id not in forecasts:
            orphan_count += 1
            rejections["outcomes:orphan_forecast_id"] += 1
            continue
        if horizon in outcomes[forecast_id]:
            rejections["outcomes:duplicate_forecast_horizon"] += 1
            continue
        outcomes[forecast_id][horizon] = record
        counts["accepted_records"] += 1
    counts["rejected_records"] = counts["source_records"] - counts["accepted_records"]
    return outcomes, counts, orphan_count


def _outcome_lag_hours(forecast: dict[str, Any], outcome: dict[str, Any], horizon: int) -> float | None:
    forecast_time = _parse_utc(forecast.get("snapshot_utc"))
    observed_time = _parse_utc(outcome.get("observed_utc"))
    if forecast_time is None or observed_time is None:
        return None
    target_time = forecast_time + dt.timedelta(hours=horizon)
    return (observed_time - target_time).total_seconds() / 3600.0


def _horizon_metrics(forecasts, outcomes, horizon: int):
    status_counts = collections.Counter()
    missing_count = 0
    observed_mark_count = 0
    invalid_mark_count = 0
    lag_unavailable_count = 0
    early_count = 0
    late_count = 0
    eligible_returns = []

    for forecast_id, forecast in forecasts.items():
        outcome = outcomes.get(forecast_id, {}).get(horizon)
        if outcome is None:
            missing_count += 1
            continue
        status = outcome.get("status")
        status_counts[str(status) if status is not None else "MISSING_STATUS"] += 1
        if status != OBSERVED_STATUS:
            continue
        mark_return = _finite_number(outcome.get("mark_return_pct"))
        if mark_return is None:
            invalid_mark_count += 1
            continue
        observed_mark_count += 1
        lag = _outcome_lag_hours(forecast, outcome, horizon)
        if lag is None:
            lag_unavailable_count += 1
        elif lag < 0:
            early_count += 1
        elif lag > MAX_OBSERVATION_LAG_HOURS:
            late_count += 1
        else:
            eligible_returns.append(mark_return)

    n = len(eligible_returns)
    unavailable_outcome_count = sum(
        count for status, count in status_counts.items() if status != OBSERVED_STATUS
    )
    targets = {}
    for target in TARGETS_PCT:
        hits = sum(value >= target for value in eligible_returns)
        targets[f"+{target}%"] = {
            "observed_mark_attainments": hits,
            "eligible_bounded_lag_marks": n,
            "rate": hits / n if n else None,
            "interpretation": "attainment at an observed post-horizon mark; not an intrahorizon touch rate",
        }
    negative_marks = [value for value in eligible_returns if value < 0]
    return {
        "forecast_detection_count": len(forecasts),
        "outcome_record_count": len(forecasts) - missing_count,
        "missing_outcome_record_count": missing_count,
        "unavailable_outcome_count": unavailable_outcome_count,
        "unlabeled_or_unavailable_forecast_count": missing_count + unavailable_outcome_count,
        "outcome_status_counts": dict(sorted(status_counts.items())),
        "observed_mark_count": observed_mark_count,
        "observed_mark_without_numeric_return_count": invalid_mark_count,
        "bounded_lag_mark_count": n,
        "bounded_lag_policy_hours": {"minimum_inclusive": 0, "maximum_inclusive": MAX_OBSERVATION_LAG_HOURS},
        "observed_marks_without_usable_timestamps": lag_unavailable_count,
        "observed_marks_before_horizon": early_count,
        "observed_marks_over_lag_limit": late_count,
        "targets": targets,
        "adverse_endpoint_marks": {
            "below_base_mark_count": len(negative_marks),
            "below_base_mark_rate": len(negative_marks) / n if n else None,
            "worst_observed_mark_return_pct": min(eligible_returns) if eligible_returns else None,
            "interpretation": "sampled mark below forecast base; not maximum adverse excursion",
        },
    }


def build_analysis(forecasts, outcomes, forecast_counts, outcome_counts, versions, dates,
                   feature_snapshots, orphan_count, rejections, source_files, snapshot_id):
    horizons = sorted({h for linked in outcomes.values() for h in linked})
    return {
        "analysis_version": ANALYSIS_VERSION,
        "source_access": "read-only",
        "source_snapshot_id": snapshot_id,
        "source_files": source_files,
        "source_record_counts": {"forecasts": forecast_counts, "outcomes": outcome_counts},
        "forecast_detection_count": len(forecasts),
        "feature_snapshot_count": feature_snapshots,
        "forecast_snapshot_time_range": {
            "first_recorded": min(dates) if dates else None,
            "last_recorded": max(dates) if dates else None,
        },
        "forecast_provenance_versions": {
            field: dict(sorted(counter.items())) for field, counter in sorted(versions.items())
        },
        "orphan_outcome_count": orphan_count,
        "rejected_record_counts": dict(sorted(rejections.items())),
        "horizon_metrics": {
            str(horizon): _horizon_metrics(forecasts, outcomes, horizon) for horizon in horizons
        },
        "methodology": {
            "feature_policy": "Use only feature_snapshot and forecast metadata stored in each forecast record; later outcomes are labels only.",
            "outcome_join": "forecast_id plus recorded horizon_hours; duplicate joins are rejected.",
            "horizon_policy": (
                "Evaluate only observed marks timestamped from the scheduled horizon through "
                f"{MAX_OBSERVATION_LAG_HOURS:g} hours afterward. Lag is computed from the forecast "
                "snapshot_utc, outcome observed_utc, and horizon_hours."
            ),
            "target_policy": "Count a target only when the bounded-lag sampled mark_return_pct is at or above the target.",
            "return_type": "Observed price mark, not an executable return.",
        },
        "unsupported_metrics": {
            "intrahorizon_target_touch_rate": "No intrahorizon price path/candle observations are present; endpoint marks cannot establish non-attainment.",
            "time_to_target": "No timestamped target-crossing observations are present.",
            "maximum_adverse_excursion": "Outcome records explicitly report no intrahorizon candles; only negative sampled endpoint marks are measurable.",
            "fees_slippage_fills_or_quote_adjusted_returns": "No execution quote, fee, slippage, or fill records are present; none are inferred.",
            "strategy_effectiveness": "No execution model or strategy replay is evaluated by this descriptive coverage analysis.",
        },
    }


def render_markdown(analysis: dict[str, Any]) -> str:
    lines = [
        "# Outcome analysis and backtesting coverage",
        "",
        f"- Analysis version: `{analysis['analysis_version']}`",
        f"- Source snapshot: `{analysis['source_snapshot_id']}`",
        f"- Forecast detections: {analysis['forecast_detection_count']:,}",
        f"- Forecast snapshot range: {analysis['forecast_snapshot_time_range']['first_recorded'] or 'unknown'} to {analysis['forecast_snapshot_time_range']['last_recorded'] or 'unknown'}",
        "- Source access: read-only; forecasts and outcomes remain unchanged.",
        "",
        "## Recorded-mark outcomes",
        "",
        "The table counts only observed marks in the interval from the scheduled horizon to 6 hours afterward. A threshold count means the sampled mark was at or above that return; it does not estimate the full intrahorizon touch rate.",
        "",
        "| Horizon | Detections | Outcome rows | Missing rows | Unavailable rows | Bounded-lag marks | +40% mark | +60% mark | +100% mark | Below-base marks | Worst mark |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for horizon, metrics in analysis["horizon_metrics"].items():
        target_cells = []
        for target in ("+40%", "+60%", "+100%"):
            result = metrics["targets"][target]
            rate = "n/a" if result["rate"] is None else f"{100 * result['rate']:.2f}%"
            target_cells.append(f"{result['observed_mark_attainments']:,} ({rate})")
        adverse = metrics["adverse_endpoint_marks"]
        rate = "n/a" if adverse["below_base_mark_rate"] is None else f"{100 * adverse['below_base_mark_rate']:.2f}%"
        worst = adverse["worst_observed_mark_return_pct"]
        worst_text = "n/a" if worst is None else f"{worst:.2f}%"
        lines.append(
            f"| {horizon}h | {metrics['forecast_detection_count']:,} | {metrics['outcome_record_count']:,} "
            f"| {metrics['missing_outcome_record_count']:,} | {metrics['unavailable_outcome_count']:,} "
            f"| {metrics['bounded_lag_mark_count']:,} "
            f"| {target_cells[0]} | {target_cells[1]} | {target_cells[2]} "
            f"| {adverse['below_base_mark_count']:,} ({rate}) | {worst_text} |"
        )
    lines.extend([
        "",
        "Outcome rows with unavailable statuses, missing marks, missing timestamps, or observation lag outside the bounded interval are excluded from mark thresholds and shown in `report.json`. A missing outcome row is classified as unobserved, not as a loss or a completed negative label.",
        "",
        "## Unsupported metrics and interpretation",
        "",
        "- **Actual target-touch rates and time-to-target:** unsupported without timestamped intrahorizon price observations. A mark below a target does not prove that the target was never reached earlier.",
        "- **Maximum adverse excursion:** unsupported without intrahorizon lows; negative endpoint marks are reported separately and are not drawdown or MAE.",
        "- **Executable or quote-adjusted returns:** unsupported. Marks are not fills, and fees, slippage, liquidity, and execution are not inferred.",
        "- **Strategy effectiveness:** not evaluated. This is a descriptive report of recorded detections and outcome coverage, not a strategy replay or ranking change.",
        "",
        "## Provenance",
        "",
        f"Forecast ledger rows: {analysis['source_record_counts']['forecasts']['source_records']:,} "
        f"({analysis['source_record_counts']['forecasts']['accepted_records']:,} accepted); "
        f"outcome ledger rows: {analysis['source_record_counts']['outcomes']['source_records']:,} "
        f"({analysis['source_record_counts']['outcomes']['accepted_records']:,} accepted).",
        f"Outcome rows not joined to a forecast: {analysis['orphan_outcome_count']:,}.",
        "Exact source partition SHA-256 hashes, schema/version counts, rejected-record counts, and per-horizon coverage are in `report.json`.",
        "",
    ])
    return "\n".join(lines)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n",
                    encoding="utf-8")


def _ensure_disjoint_paths(memory_dir: Path, output_root: Path) -> None:
    output = output_root.resolve()
    for protected in (memory_dir.resolve(), DEFAULT_EXPORTS.resolve(), DEFAULT_RUNS.resolve()):
        if output == protected or output in protected.parents or protected in output.parents:
            raise AnalysisError("research output must be separate from scanner memory, exports, and scan outputs")


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
    if not isinstance(manifest, dict):
        raise AnalysisError(f"refusing to overwrite unmanaged research output: {output_dir}")
    if (manifest.get("managed_by") != MANAGED_BY
            or manifest.get("analysis_version") != ANALYSIS_VERSION
            or manifest.get("source_snapshot_id") != snapshot_id):
        raise AnalysisError(f"refusing to overwrite a different research output: {output_dir}")
    expected = manifest.get("output_files_sha256")
    if not isinstance(expected, dict) or set(expected) != {"report.json", "report.md"}:
        raise AnalysisError(f"research output manifest is incomplete: {manifest_path}")
    for name, checksum in expected.items():
        path = output_dir / name
        if path.is_symlink() or not path.is_file() or _sha256_file(path) != checksum:
            raise AnalysisError(f"research output failed checksum validation: {name}")
    return output_dir


def analyze_ledgers(memory_dir: Path = DEFAULT_MEMORY, output_root: Path = DEFAULT_OUTPUT_ROOT) -> Path:
    memory_dir = Path(memory_dir)
    output_root = Path(output_root)
    _ensure_disjoint_paths(memory_dir, output_root)
    partitions, stats, source_files, snapshot_id = _source_snapshot(memory_dir)
    output_dir = output_root / f"outcomes-v1-1-{snapshot_id[:24]}"
    existing = _existing_report(output_dir, snapshot_id)
    if existing is not None:
        return existing

    rejections: collections.Counter[str] = collections.Counter()
    forecasts, forecast_counts, versions, dates, feature_snapshots = _load_forecasts(partitions, rejections)
    outcomes, outcome_counts, orphan_count = _load_outcomes(partitions, forecasts, rejections)
    analysis = build_analysis(
        forecasts, outcomes, forecast_counts, outcome_counts, versions, dates,
        feature_snapshots, orphan_count, rejections, source_files, snapshot_id,
    )
    markdown = render_markdown(analysis)
    output_root.mkdir(parents=True, exist_ok=True)
    if output_root.is_symlink():
        raise AnalysisError(f"research output root must not be a symlink: {output_root}")
    staging = Path(tempfile.mkdtemp(prefix=".outcomes-", dir=output_root))
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
            "source_access": "read-only",
            "output_files_sha256": output_hashes,
        })
        _verify_source_snapshot(memory_dir, partitions, stats, source_files)
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
        output_dir = analyze_ledgers(args.memory_dir, args.output_root)
    except AnalysisError as exc:
        print(f"Outcome analysis failed: {exc}", file=sys.stderr)
        return 2
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
