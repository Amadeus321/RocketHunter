#!/usr/bin/env python3
"""Summarize existing local research coverage without contacting providers."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COLLECTION_ROOT = ROOT / "rocket-hunter-data" / "research-data" / "watchlist-observations"
DEFAULT_ANALYSIS_ROOT = ROOT / "rocket-hunter-data" / "research-analysis"
FINAL_COLLECTION_STATUSES = {
    "COMPLETED",
    "DURATION_ELAPSED",
    "FAILED",
    "INTERRUPTED",
    "ONE_CYCLE_COMPLETED",
    "PROVIDER_ACCESS_DENIED",
    "PROVIDER_COOLDOWN_ACTIVE",
    "PROVIDER_RATE_LIMITED",
    "REQUEST_LIMIT_REACHED",
}


class ReadinessError(RuntimeError):
    """Raised when an existing readiness artifact cannot be read safely."""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReadinessError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReadinessError(f"JSON artifact must contain an object: {path}")
    return value


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


def _usable_hourly_bar(bar: Any, received_utc: Any) -> bool:
    if not isinstance(bar, dict):
        return False
    opened = _parse_utc(bar.get("time_open"))
    closed = _parse_utc(bar.get("time_close"))
    received = _parse_utc(received_utc)
    if opened is None or closed is None or received is None:
        return False
    if closed - opened != dt.timedelta(hours=1) or closed > received:
        return False
    high, low, close = (_finite_number(bar.get(name)) for name in ("high", "low", "close"))
    return (
        high is not None and low is not None and close is not None
        and low > 0 and high >= low and low <= close <= high
    )
def _safe_observation_path(run_dir: Path, manifest: dict[str, Any]) -> Path:
    output_file = manifest.get("output_file", "observations.jsonl")
    if not isinstance(output_file, str) or not output_file:
        raise ReadinessError(f"invalid collection output_file in {run_dir.name}")
    relative = Path(output_file)
    if relative.is_absolute():
        raise ReadinessError(f"collection output_file must be relative in {run_dir.name}")
    path = run_dir / relative
    try:
        path.resolve().relative_to(run_dir.resolve())
    except ValueError as exc:
        raise ReadinessError(f"collection output_file escapes its session: {run_dir.name}") from exc
    cursor = run_dir
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ReadinessError(f"observation artifact must not use symlinks: {cursor}")
    return path


def _collection_summaries(
    collection_root: Path,
) -> tuple[list[dict[str, Any]], Counter[str], int, int, int, Counter[str], list[str]]:
    if not collection_root.exists():
        return [], Counter(), 0, 0, 0, Counter(), []
    if collection_root.is_symlink() or not collection_root.is_dir():
        raise ReadinessError(f"collection root must be a regular directory: {collection_root}")

    summaries = []
    statuses: Counter[str] = Counter()
    usable_bars = 0
    missing_observation_files = 0
    missing_observation_records = 0
    excluded_records: Counter[str] = Counter()
    pending_sessions: list[str] = []
    for run_dir in sorted(collection_root.glob("collection-*")):
        if run_dir.is_symlink() or not run_dir.is_dir():
            continue
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            pending_sessions.append(f"{run_dir.name} (manifest unavailable)")
            continue
        manifest = _read_object(manifest_path)
        lifecycle_status = manifest.get("status")
        if lifecycle_status not in FINAL_COLLECTION_STATUSES:
            pending_sessions.append(f"{run_dir.name} ({lifecycle_status or 'unknown status'})")
            continue
        declared_count = manifest.get("record_count")
        if isinstance(declared_count, bool) or not isinstance(declared_count, int) or declared_count < 0:
            raise ReadinessError(f"invalid record_count in collection manifest: {manifest_path}")
        collection_run_id = manifest.get("collection_run_id")
        if declared_count and (not isinstance(collection_run_id, str) or not collection_run_id):
            raise ReadinessError(f"missing collection_run_id in non-empty manifest: {manifest_path}")
        observation_path = _safe_observation_path(run_dir, manifest)
        run_statuses: Counter[str] = Counter()
        run_excluded_records: Counter[str] = Counter()
        run_bars = 0
        source_record_count = 0
        observations_available = observation_path.exists()
        if not observations_available:
            missing_observation_files += 1
            if manifest.get("record_count") != 0:
                missing_observation_records += 1
        if observations_available:
            try:
                with observation_path.open("r", encoding="utf-8") as stream:
                    for line_number, line in enumerate(stream, 1):
                        if not line.strip():
                            continue
                        source_record_count += 1
                        if source_record_count > declared_count:
                            excluded_records["beyond_manifest_record_count"] += 1
                            run_excluded_records["beyond_manifest_record_count"] += 1
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise ReadinessError(
                                f"invalid JSONL record at {observation_path}:{line_number}: {exc}"
                            ) from exc
                        if not isinstance(record, dict):
                            raise ReadinessError(
                                f"observation record must be an object at {observation_path}:{line_number}"
                            )
                        if record.get("collection_run_id") != collection_run_id:
                            excluded_records["collection_run_id_mismatch"] += 1
                            run_excluded_records["collection_run_id_mismatch"] += 1
                            continue
                        status = record.get("source_status")
                        if isinstance(status, str):
                            statuses[status] += 1
                            run_statuses[status] += 1
                        evidence = record.get("source_evidence")
                        bars = evidence.get("bars") if isinstance(evidence, dict) else None
                        if status == "OBSERVED" and isinstance(bars, list):
                            count = sum(
                                _usable_hourly_bar(bar, record.get("source_received_utc"))
                                for bar in bars
                            )
                            usable_bars += count
                            run_bars += count
            except (OSError, UnicodeDecodeError) as exc:
                raise ReadinessError(f"cannot read observations {observation_path}: {exc}") from exc
            if source_record_count < declared_count:
                excluded_records["missing_manifest_declared_records"] += declared_count - source_record_count
                run_excluded_records["missing_manifest_declared_records"] += declared_count - source_record_count
        summaries.append({
            "directory": run_dir.name,
            "status": manifest.get("status", "UNKNOWN"),
            "finished_utc": manifest.get("finished_utc"),
            "record_count": declared_count,
            "requests_started": manifest.get("requests_started"),
            "statuses": dict(sorted(run_statuses.items())),
            "usable_bars": run_bars,
            "observations_available": observations_available,
            "excluded_records": dict(sorted(run_excluded_records.items())),
        })
    return (
        summaries, statuses, usable_bars, missing_observation_files,
        missing_observation_records, excluded_records, pending_sessions,
    )


def _analysis_summaries(analysis_root: Path) -> list[dict[str, Any]]:
    if not analysis_root.exists():
        return []
    if analysis_root.is_symlink() or not analysis_root.is_dir():
        raise ReadinessError(f"analysis root must be a regular directory: {analysis_root}")

    summaries = []
    for report_path in sorted(analysis_root.glob("*/report.json")):
        if report_path.is_symlink():
            raise ReadinessError(f"analysis report must not be a symlink: {report_path}")
        report = _read_object(report_path)
        record_counts = report.get("record_counts")
        if not isinstance(record_counts, dict):
            record_counts = {}
        coverage = report.get("candle_level_metrics")
        if not isinstance(coverage, dict):
            coverage = {}
        excluded_reasons = report.get("excluded_forecast_reasons")
        if not isinstance(excluded_reasons, dict):
            excluded_reasons = {}
        incomplete_reasons = report.get("incomplete_forecast_reasons")
        if not isinstance(incomplete_reasons, dict):
            incomplete_reasons = {}
        summaries.append({
            "directory": report_path.parent.name,
            "version": report.get("analysis_version", "unknown"),
            "as_of": report.get("analysis_as_of_utc") or report.get("generated_utc"),
            "timestamp": _parse_utc(report.get("analysis_as_of_utc") or report.get("generated_utc")),
            "mtime": report_path.stat().st_mtime_ns,
            "forecasts": record_counts.get("forecasts_loaded", report.get("forecast_detection_count")),
            "observations": record_counts.get("observations_loaded"),
            "joined": record_counts.get("forecasts_joined_to_observations"),
            "complete_24h": coverage.get("complete_coverage_denominator"),
            "targets": coverage.get("target_results"),
            "excluded_forecast_reasons": excluded_reasons,
            "incomplete_forecast_reasons": incomplete_reasons,
            "missed_summary": {
                "total_movers": report.get("total_movers_40pct_plus"),
                "captured": report.get("actionable_captured_count"),
                "missed": report.get("missed_count"),
                "tier_summary": report.get("tier_summary"),
                "category_breakdown": report.get("miss_category_breakdown"),
            } if isinstance(report.get("total_movers_40pct_plus"), int) else None,
        })
    return summaries


def _latest(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    return max(items, key=lambda item: (
        item["timestamp"] is not None,
        item["timestamp"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        item["mtime"],
    ))


def _latest_collection(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    return max(items, key=lambda item: (
        _parse_utc(item.get("finished_utc")) is not None,
        _parse_utc(item.get("finished_utc"))
        or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        item["directory"],
    ))


def _provider_cooldown(collection_root: Path, manifests: list[dict[str, Any]]) -> str:
    cooldown_path = collection_root / ".provider-cooldown.json"
    if cooldown_path.exists():
        cooldown = _read_object(cooldown_path)
        provider = cooldown.get("provider", "unknown provider")
        expiry = cooldown.get("cooldown_until_utc", "unknown expiry")
        status = cooldown.get("http_status", "unknown status")
        return f"recorded: {provider} HTTP {status}, until {expiry}"
    recorded = [
        item["cooldown"] for item in manifests
        if isinstance(item.get("cooldown"), dict)
    ]
    if recorded:
        item = recorded[-1]
        return (
            f"recorded: {item.get('provider', 'unknown provider')} HTTP "
            f"{item.get('http_status', 'unknown status')}, until "
            f"{item.get('cooldown_until_utc', 'unknown expiry')}"
        )
    return "unknown (no persisted provider cooldown record)"


def summarize(collection_root: Path, analysis_root: Path) -> dict[str, Any]:
    (collections, statuses, usable_bars, missing_observation_files,
     missing_observation_records, excluded_records,
     pending_sessions) = _collection_summaries(collection_root)
    analyses = _analysis_summaries(analysis_root)
    for item in collections:
        manifest_path = collection_root / item["directory"] / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = _read_object(manifest_path)
        item["cooldown"] = manifest.get("provider_rate_limit")

    candle_reports = [
        item for item in analyses
        if isinstance(item["version"], str) and item["version"].startswith("rocket-hunter-candle-outcomes.")
    ]
    mark_reports = [
        item for item in analyses
        if isinstance(item["version"], str) and item["version"].startswith("rocket-hunter-outcome-analysis.")
    ]
    missed_reports = [
        item for item in analyses
        if isinstance(item["version"], str) and item["version"].startswith("rocket-hunter-missed-opportunities.")
    ]
    latest_candles = _latest(candle_reports)
    latest_marks = _latest(mark_reports)
    latest_missed = _latest(missed_reports)
    blockers = []
    if not any(item.get("status") != "PENDING_FINALIZATION" for item in collections):
        blockers.append("No finalized collection session manifests are present.")
    if usable_bars == 0:
        blockers.append("No valid completed hourly OHLCV bars are present in collection observations.")
    if missing_observation_records:
        blockers.append(
            f"{missing_observation_records} finalized collection session(s) declare records "
            "but have no observation file."
        )
    if pending_sessions:
        blockers.append(
            f"{len(pending_sessions)} collection session(s) are not finalized "
            f"({'; '.join(pending_sessions)}); their observations were not read."
        )
    if latest_candles is None:
        blockers.append("No hourly candle-outcome analysis artifact is present.")
    elif not latest_candles.get("joined"):
        blockers.append("The latest hourly analysis has no forecast-to-observation joins.")
    if latest_candles is None or not latest_candles.get("complete_24h"):
        blockers.append("The latest hourly analysis has no complete 24-hour forecast windows.")
    return {
        "collections": collections,
        "latest_collection": _latest_collection(collections),
        "status_counts": dict(sorted(statuses.items())),
        "usable_bars": usable_bars,
        "missing_observation_files": missing_observation_files,
        "missing_observation_records": missing_observation_records,
        "pending_collection_sessions": len(pending_sessions),
        "pending_collection_details": pending_sessions,
        "excluded_observation_records": dict(sorted(excluded_records.items())),
        "provider_cooldown": _provider_cooldown(collection_root, collections),
        "latest_candle_analysis": latest_candles,
        "latest_mark_analysis": latest_marks,
        "latest_missed_analysis": latest_missed,
        "blockers": blockers,
    }


def render(summary: dict[str, Any]) -> str:
    lines = [
        "Research coverage readiness (read-only; no provider requests)",
        "Overall platform readiness: NOT READY (this report summarizes coverage; it does not certify readiness)",
        f"Collection sessions: {len(summary['collections'])}",
        f"Collection sessions pending finalization: {summary['pending_collection_sessions']}",
        f"Pending collection details: {'; '.join(summary['pending_collection_details']) or 'none'}",
        f"Collection record statuses: {json.dumps(summary['status_counts'], sort_keys=True)}",
        f"Valid completed hourly bars: {summary['usable_bars']}",
        f"Missing observation files: {summary['missing_observation_files']} "
        f"({summary['missing_observation_records']} with manifest-declared records)",
        f"Excluded observation records: {json.dumps(summary['excluded_observation_records'], sort_keys=True)}",
        f"Provider cooldown: {summary['provider_cooldown']}",
    ]
    latest_collection = summary["latest_collection"]
    if latest_collection is not None:
        lines.append(
            f"Latest collection: {latest_collection['directory']} "
            f"({latest_collection['status']}; {latest_collection['finished_utc'] or 'finish time unknown'}; "
            f"{latest_collection['record_count']} records; "
            f"{latest_collection['requests_started'] if latest_collection['requests_started'] is not None else 'unknown'} "
            "requests)"
        )
    for label, key in (
        ("Latest hourly candle analysis", "latest_candle_analysis"),
        ("Latest sampled-mark analysis", "latest_mark_analysis"),
    ):
        item = summary[key]
        if item is None:
            lines.append(f"{label}: not found")
            continue
        lines.append(
            f"{label}: {item['directory']} ({item['version']}; as of {item['as_of'] or 'unknown'})"
        )
        if key == "latest_candle_analysis":
            lines.append(
                f"  forecasts={item['forecasts']}; observations_loaded={item['observations']}; "
                f"exact_identity_joins={item['joined']}; "
                f"exact_identity_complete_24h_windows={item['complete_24h']}"
            )
            lines.append(
                f"  excluded_forecast_reasons="
                f"{json.dumps(item['excluded_forecast_reasons'], sort_keys=True)}"
            )
            lines.append(
                f"  incomplete_window_reasons="
                f"{json.dumps(item['incomplete_forecast_reasons'], sort_keys=True)}"
            )
        else:
            lines.append(f"  forecast_detections={item['forecasts']}")
    if summary.get("latest_missed_analysis"):
        missed = summary["latest_missed_analysis"]
        data = missed.get("missed_summary") or {}
        lines.append(
            f"Latest missed-opportunity analysis: {missed['directory']} ({missed['version']}; as of {missed['as_of'] or 'unknown'})"
        )
        lines.append(
            f"  gainers_40pct_plus={data.get('total_movers', 0)}; actionable_captured={data.get('captured', 0)}; "
            f"missed={data.get('missed', 0)}"
        )
    lines.append("Blockers:")
    if summary["blockers"]:
        lines.extend(f"- {blocker}" for blocker in summary["blockers"])
    else:
        lines.append("- No zero-coverage blockers detected; this summary does not certify platform readiness.")
    lines.append(
        "HTTP 403/429, missing records, and zero joins are collection/coverage states, not target misses."
    )
    lines.append(
        "Sampled marks are endpoint observations only; they do not establish strategy effectiveness."
    )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection-root", type=Path, default=DEFAULT_COLLECTION_ROOT)
    parser.add_argument("--analysis-root", type=Path, default=DEFAULT_ANALYSIS_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        print(render(summarize(args.collection_root, args.analysis_root)))
    except ReadinessError as exc:
        print(f"Research readiness check failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
