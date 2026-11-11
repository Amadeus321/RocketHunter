#!/usr/bin/env python3
"""Generate a dependency-free offline dashboard from existing local artifacts."""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import sys
from pathlib import Path
from typing import Any

try:
    from scripts import local_diagnostics
except ImportError:
    import local_diagnostics

try:
    from scripts.user_trade_learning import get_journal
except ImportError:
    try:
        from user_trade_learning import get_journal
    except ImportError:
        def get_journal() -> dict[str, Any]:
            return {"positions": [], "ai_learning_stats": {}}


if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_NAME = "status-dashboard.html"
UNKNOWN = "Unknown"
UNAVAILABLE = "Unavailable"


def _format_israel_time(dt_val: Any) -> str:
    if not dt_val or dt_val == UNAVAILABLE or dt_val == "unavailable" or dt_val == UNKNOWN:
        return UNAVAILABLE
    s = str(dt_val).strip()
    try:
        dt_obj = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt_utc = dt_obj.replace(tzinfo=dt.timezone.utc) if dt_obj.tzinfo is None else dt_obj.astimezone(dt.timezone.utc)
        israel_dt = dt_utc.astimezone(dt.timezone(dt.timedelta(hours=2)))
        return israel_dt.strftime("%Y-%m-%d %H:%M:%S IST")
    except Exception:
        return s
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


class DashboardError(RuntimeError):
    """Raised when an existing dashboard input cannot be read safely."""


def _read_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise DashboardError(f"expected a regular artifact file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DashboardError(f"JSON artifact must contain an object: {path}")
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


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value < 0:
        return None
    return value


def _display(value: Any, missing: str = UNKNOWN) -> str:
    if value is None or value == "":
        return missing
    return str(value)


def _display_count(value: Any) -> str:
    number = _number(value)
    if number is None or not isinstance(number, int):
        return UNAVAILABLE
    return str(number)


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _duration(started: Any, finished: Any) -> str:
    start = _parse_utc(started)
    finish = _parse_utc(finished)
    if start is None or finish is None or finish < start:
        return UNAVAILABLE
    seconds = int((finish - start).total_seconds())
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _is_usable_hourly_bar(bar: Any, received_utc: Any) -> bool:
    if not isinstance(bar, dict):
        return False
    opened = _parse_utc(bar.get("time_open"))
    closed = _parse_utc(bar.get("time_close"))
    received = _parse_utc(received_utc)
    if opened is None or closed is None or received is None:
        return False
    if closed - opened != dt.timedelta(hours=1) or closed > received:
        return False
    values = [bar.get(name) for name in ("high", "low", "close")]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        return False
    high, low, close = (float(value) for value in values)
    return (
        all(math.isfinite(value) for value in (high, low, close))
        and low > 0 and high >= low and low <= close <= high
    )


def _regular_directory(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _safe_observation_path(run_dir: Path, manifest: dict[str, Any]) -> Path:
    output_file = manifest.get("output_file", "observations.jsonl")
    if not isinstance(output_file, str) or not output_file:
        raise DashboardError(f"invalid collection output_file in {run_dir.name}")
    relative = Path(output_file)
    if relative.is_absolute():
        raise DashboardError(f"collection output_file must be relative in {run_dir.name}")
    path = run_dir / relative
    resolved_run = run_dir.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_run)
    except ValueError as exc:
        raise DashboardError(f"collection output_file escapes its session: {run_dir.name}") from exc
    cursor = run_dir
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise DashboardError(f"collection observation file must not use symlinks: {run_dir.name}")
    return path


def _collection_summaries(
    collection_root: Path,
) -> tuple[list[dict[str, Any]], int | None, int, int, int]:
    if not collection_root.exists():
        return [], None, 0, 0, 0
    if not _regular_directory(collection_root):
        raise DashboardError(f"collection root must be a regular directory: {collection_root}")

    summaries = []
    total_usable_bars = 0
    missing_observation_files = 0
    missing_observation_records = 0
    pending_observation_sessions = 0
    for run_dir in sorted(collection_root.glob("collection-*")):
        if run_dir.is_symlink() or not run_dir.is_dir():
            continue
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            pending_observation_sessions += 1
            summaries.append({
                "directory": run_dir.name,
                "status": "PENDING_FINALIZATION",
                "lifecycle": "Not finalized; manifest unavailable; observations not read",
                "started_utc": None,
                "finished_utc": None,
                "record_count": None,
                "requests_started": None,
                "statuses": {},
                "usable_bars": None,
                "observations_available": False,
                "excluded_rows": {},
            })
            continue
        manifest = _read_object(manifest_path)
        observation_path = _safe_observation_path(run_dir, manifest)
        declared_count = manifest.get("record_count")
        if isinstance(declared_count, bool) or not isinstance(declared_count, int) or declared_count < 0:
            raise DashboardError(f"invalid record_count in collection manifest: {run_dir.name}")
        collection_run_id = manifest.get("collection_run_id")
        if declared_count and (not isinstance(collection_run_id, str) or not collection_run_id):
            raise DashboardError(f"missing collection_run_id in non-empty manifest: {run_dir.name}")
        statuses: dict[str, int] = {}
        usable_bars = 0
        excluded_rows: dict[str, int] = {}
        source_record_count = 0
        lifecycle_status = manifest.get("status")
        finalized = lifecycle_status in FINAL_COLLECTION_STATUSES
        observation_file_available = finalized and observation_path.exists()
        if not finalized:
            pending_observation_sessions += 1
        elif not observation_path.exists():
            missing_observation_files += 1
            if manifest.get("record_count") != 0:
                missing_observation_records += 1
        if observation_file_available:
            if observation_path.is_symlink() or not observation_path.is_file():
                raise DashboardError(f"observation artifact must be a regular file: {observation_path}")
            try:
                with observation_path.open("r", encoding="utf-8") as stream:
                    for line_number, line in enumerate(stream, 1):
                        if not line.strip():
                            continue
                        source_record_count += 1
                        if source_record_count > declared_count:
                            excluded_rows["beyond_manifest_record_count"] = (
                                excluded_rows.get("beyond_manifest_record_count", 0) + 1
                            )
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise DashboardError(
                                f"invalid JSONL record in collection {run_dir.name} at line {line_number}"
                            ) from exc
                        if not isinstance(record, dict):
                            raise DashboardError(
                                f"observation record must be an object in collection {run_dir.name}"
                            )
                        if record.get("collection_run_id") != collection_run_id:
                            excluded_rows["collection_run_id_mismatch"] = (
                                excluded_rows.get("collection_run_id_mismatch", 0) + 1
                            )
                            continue
                        status = record.get("source_status")
                        if isinstance(status, str):
                            statuses[status] = statuses.get(status, 0) + 1
                        evidence = record.get("source_evidence")
                        bars = evidence.get("bars") if isinstance(evidence, dict) else None
                        if status == "OBSERVED" and isinstance(bars, list):
                            usable_bars += sum(
                                _is_usable_hourly_bar(bar, record.get("source_received_utc"))
                                for bar in bars
                            )
            except (OSError, UnicodeDecodeError) as exc:
                raise DashboardError(f"cannot read observations for collection {run_dir.name}: {exc}") from exc
            if source_record_count < declared_count:
                excluded_rows["missing_manifest_declared_records"] = declared_count - source_record_count
        total_usable_bars += usable_bars
        summaries.append({
            "directory": run_dir.name,
            "status": lifecycle_status,
            "lifecycle": "Finalized" if finalized else "Not finalized; observations not read",
            "started_utc": next((
                manifest.get(key) for key in ("started_utc", "collection_started_utc", "created_utc")
                if manifest.get(key)
            ), None),
            "finished_utc": manifest.get("finished_utc"),
            "record_count": declared_count,
            "requests_started": manifest.get("requests_started"),
            "statuses": statuses,
            "usable_bars": usable_bars if observation_file_available else None,
            "observations_available": observation_file_available,
            "excluded_rows": excluded_rows,
        })
    return (
        summaries,
        total_usable_bars,
        missing_observation_files,
        missing_observation_records,
        pending_observation_sessions,
    )


def _analysis_reports(analysis_root: Path) -> list[dict[str, Any]]:
    if not analysis_root.exists():
        return []
    if not _regular_directory(analysis_root):
        raise DashboardError(f"analysis root must be a regular directory: {analysis_root}")

    reports = []
    for report_path in sorted(analysis_root.glob("*/report.json")):
        if report_path.parent.is_symlink():
            continue
        report = _read_object(report_path)
        reports.append({
            "directory": report_path.parent.name,
            "version": report.get("analysis_version"),
            "as_of": report.get("analysis_as_of_utc"),
            "timestamp": _parse_utc(report.get("analysis_as_of_utc")),
            "mtime": report_path.stat().st_mtime_ns,
            "report": report,
        })
    return reports


def _latest(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    return max(items, key=lambda item: (
        item["timestamp"] is not None,
        item["timestamp"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        item["mtime"],
    ))


def _scan_history(runs_root: Path) -> list[dict[str, Any]]:
    target_root = runs_root
    if not target_root.exists() and (ROOT / "rocket-runs").exists():
        target_root = ROOT / "rocket-runs"
    if not target_root.exists():
        return []
    if not _regular_directory(target_root):
        raise DashboardError(f"scan runs root must be a regular directory: {target_root}")
    scans = []
    for run_dir in target_root.iterdir():
        if run_dir.is_symlink() or not run_dir.is_dir():
            continue
        scan_path = run_dir / "rocket-hunt.json"
        status_file = run_dir / "scan-status.json"
        forward_path = run_dir / "forward-24h-rocket.json"
        if not scan_path.exists() and not status_file.exists() and not forward_path.exists():
            continue
        scan = _read_object(scan_path) if scan_path.is_file() else {}
        status = scan.get("scan_status")
        run_id = scan.get("run_id")
        started = scan.get("scan_started_utc", scan.get("scan_start_utc"))
        finished = scan.get("scan_finished_utc")
        if status_file.is_file():
            try:
                st_data = _read_object(status_file)
                st_status = st_data.get("status")
                if st_status and st_status != UNAVAILABLE:
                    status = st_status
                if not run_id:
                    run_id = st_data.get("run_id")
                if not started:
                    started = st_data.get("scan_started_utc") or st_data.get("started_utc")
                if st_data.get("scan_finished_utc"):
                    finished = st_data.get("scan_finished_utc")
            except Exception:
                pass
        if forward_path.is_file() and (not started or not finished or not status or status == UNAVAILABLE or status == "IN_PROGRESS"):
            try:
                fw_data = _read_object(forward_path)
                fw_status = fw_data.get("scan_status")
                if fw_status and fw_status != UNAVAILABLE and fw_status != "IN_PROGRESS":
                    status = fw_status
                if not run_id:
                    run_id = fw_data.get("run_id")
                if not finished and fw_data.get("scan_finished_utc"):
                    finished = fw_data.get("scan_finished_utc")
            except Exception:
                pass
        if not run_id or (isinstance(run_id, str) and (run_id.startswith("local-") and not run_id.startswith("local-run-")) and run_dir.name.startswith("local-run-")):
            run_id = run_dir.name
        if not status or not isinstance(status, str):
            status = "IN_PROGRESS" if not finished else "COMPLETED"
        timestamp = next((
            _parse_utc(ts)
            for ts in (finished, started)
            if _parse_utc(ts) is not None
        ), None)
        mtime = scan_path.stat().st_mtime_ns if scan_path.is_file() else (status_file.stat().st_mtime_ns if status_file.is_file() else run_dir.stat().st_mtime_ns)
        scans.append({
            "directory": run_dir.name,
            "status": status,
            "run_id": run_id,
            "started_utc": started,
            "finished_utc": finished,
            "timestamp": timestamp,
            "mtime": mtime,
        })
    return sorted(scans, key=lambda item: (
        item["timestamp"] is not None,
        item["timestamp"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        item["mtime"],
    ), reverse=True)


def _cooldown(collection_root: Path, collections: list[dict[str, Any]]) -> str:
    if collection_root.exists():
        cooldown_path = collection_root / ".provider-cooldown.json"
        if cooldown_path.exists():
            cooldown = _read_object(cooldown_path)
            return (
                f"Recorded: {_display(cooldown.get('provider'))}; HTTP "
                f"{_display(cooldown.get('http_status'))}; until "
                f"{_display(cooldown.get('cooldown_until_utc'))}"
            )
    for item in reversed(collections):
        manifest_path = collection_root / item["directory"] / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = _read_object(manifest_path)
        cooldown = manifest.get("provider_rate_limit", manifest.get("cooldown"))
        if isinstance(cooldown, dict):
            return (
                f"Recorded: {_display(cooldown.get('provider'))}; HTTP "
                f"{_display(cooldown.get('http_status'))}; until "
                f"{_display(cooldown.get('cooldown_until_utc'))}"
            )
    return "Unknown (no persisted provider cooldown record)"


def summarize(root: Path = ROOT) -> dict[str, Any]:
    root = root.resolve()
    collection_root = root / "rocket-hunter-data" / "research-data" / "watchlist-observations"
    analysis_root = root / "rocket-hunter-data" / "research-analysis"
    runs_root = root / "rocket-runs"
    machine_health = local_diagnostics.collect_diagnostics(
        project_root=root,
        data_dir=root / "rocket-hunter-data",
    )
    (collections, usable_bars, missing_observation_files,
     missing_observation_records, pending_observation_sessions) = _collection_summaries(collection_root)
    analyses = _analysis_reports(analysis_root)
    candles = [
        item for item in analyses
        if isinstance(item["version"], str)
        and item["version"].startswith("rocket-hunter-candle-outcomes.")
    ]
    marks = [
        item for item in analyses
        if isinstance(item["version"], str)
        and item["version"].startswith("rocket-hunter-outcome-analysis.")
    ]
    missed = [
        item for item in analyses
        if isinstance(item["version"], str)
        and item["version"].startswith("rocket-hunter-missed-opportunities.")
    ]
    latest_candles = _latest(candles)
    latest_marks = _latest(marks)
    latest_missed = _latest(missed)
    blockers = []
    if not collections:
        blockers.append("No collection session manifests are present.")
    if usable_bars is None:
        blockers.append("Usable hourly-bar count is unavailable because collection observations are missing.")
    elif usable_bars == 0:
        blockers.append("No valid completed hourly OHLCV bars are present in collection observations.")
    if missing_observation_records:
        blockers.append(
            f"{missing_observation_records} finalized collection session(s) declare records "
            "but have no observation file."
        )
    if pending_observation_sessions:
        blockers.append(
            f"{pending_observation_sessions} collection session(s) are not finalized; "
            "their observations were not read."
        )

    candle_counts = {}
    candle_metrics = {}
    excluded_forecast_reasons = {}
    incomplete_forecast_reasons = {}
    if latest_candles is not None:
        report = latest_candles["report"]
        counts = report.get("record_counts")
        candle_counts = counts if isinstance(counts, dict) else {}
        metrics = report.get("candle_level_metrics")
        candle_metrics = metrics if isinstance(metrics, dict) else {}
        excluded = report.get("excluded_forecast_reasons")
        excluded_forecast_reasons = excluded if isinstance(excluded, dict) else {}
        incomplete = report.get("incomplete_forecast_reasons")
        incomplete_forecast_reasons = incomplete if isinstance(incomplete, dict) else {}
    joined = _number(candle_counts.get("forecasts_joined_to_observations"))
    complete = _number(candle_metrics.get("complete_coverage_denominator"))
    if latest_candles is None:
        blockers.append("No hourly candle-outcome analysis artifact is present.")
    elif not joined:
        blockers.append("The latest hourly analysis has no forecast-to-observation joins.")
    if latest_candles is None or not complete:
        blockers.append("The latest hourly analysis has no complete 24-hour forecast windows.")

    promotion_evidence = []
    joined_count = _nonnegative_int(candle_counts.get("forecasts_joined_to_observations"))
    complete_count = _nonnegative_int(candle_metrics.get("complete_coverage_denominator"))
    if joined_count is None or complete_count is None or complete_count > joined_count:
        coverage_detail = "Unavailable or internally inconsistent"
    elif complete_count == 0:
        coverage_detail = "No complete 24-hour windows recorded"
    else:
        coverage_detail = (
            f"{complete_count} complete 24-hour window(s) recorded; "
            "coverage is not an effectiveness threshold"
        )
    promotion_evidence.append({
        "name": "24-hour evidence coverage",
        "detail": coverage_detail,
    })

    mark_report = latest_marks["report"] if latest_marks else {}
    horizon_metrics = mark_report.get("horizon_metrics")
    horizon_24 = horizon_metrics.get("24") if isinstance(horizon_metrics, dict) else None
    horizon_24 = horizon_24 if isinstance(horizon_24, dict) else {}
    targets = horizon_24.get("targets")
    target_40 = targets.get("+40%") if isinstance(targets, dict) else None
    target_40 = target_40 if isinstance(target_40, dict) else {}
    attainment_count = _nonnegative_int(target_40.get("observed_mark_attainments"))
    eligible_count = _nonnegative_int(horizon_24.get("bounded_lag_mark_count"))
    if (
        attainment_count is None
        or eligible_count is None
        or eligible_count == 0
        or attainment_count > eligible_count
    ):
        target_detail = "Unavailable or internally inconsistent"
    else:
        target_detail = (
            f"{attainment_count} / {eligible_count} bounded-lag endpoint mark(s) recorded; "
            "not a promotion decision"
        )
    promotion_evidence.append({
        "name": "+40% sampled-endpoint evidence",
        "detail": target_detail,
    })
    promotion_evidence.extend((
        {
            "name": "Security and quote requirements",
            "detail": "Not verified by this offline dashboard",
        },
        {
            "name": "Production owner approval",
            "detail": "Not evaluated; GitHub remains the independent production owner",
        },
    ))

    return {
        "root_available": root.is_dir(),
        "scan_history": _scan_history(runs_root),
        "scan_history_available": runs_root.is_dir() and not runs_root.is_symlink(),
        "machine_health": machine_health,
        "collections": collections,
        "usable_bars": usable_bars,
        "missing_observation_files": missing_observation_files,
        "missing_observation_records": missing_observation_records,
        "pending_observation_sessions": pending_observation_sessions,
        "collection_root_available": collection_root.is_dir() and not collection_root.is_symlink(),
        "cooldown": _cooldown(collection_root, collections) if collection_root.exists() else
            "Unknown (no persisted provider cooldown record)",
        "latest_candles": latest_candles,
        "candle_counts": candle_counts,
        "candle_metrics": candle_metrics,
        "excluded_forecast_reasons": excluded_forecast_reasons,
        "incomplete_forecast_reasons": incomplete_forecast_reasons,
        "latest_marks": latest_marks,
        "latest_missed": latest_missed,
        "blockers": blockers,
        "promotion_evidence": promotion_evidence,
    }


def _esc(value: Any, missing: str = UNKNOWN) -> str:
    return html.escape(_display(value, missing), quote=True)


def _count_cell(value: Any) -> str:
    return _esc(_display_count(value), UNAVAILABLE)


def _rate(value: Any) -> str:
    number = _number(value)
    if number is None:
        return UNAVAILABLE
    return f"{number * 100:.2f}%"


def _target_cell(targets: Any, key: str, eligible: Any) -> str:
    if not isinstance(targets, dict):
        return UNAVAILABLE
    target = targets.get(key)
    if not isinstance(target, dict):
        return UNAVAILABLE
    count = _number(target.get("observed_mark_attainments"))
    denominator = _number(eligible)
    if count is None or denominator is None or denominator <= 0:
        return UNAVAILABLE
    return f"{_display_count(count)} / {_display_count(denominator)} ({_rate(target.get('rate'))})"


def _render_collections(items: list[dict[str, Any]], root_available: bool) -> str:
    if not items:
        content = (
            '<tr><td colspan="11" class="empty">'
            + ("No collection session manifests found." if root_available else UNAVAILABLE)
            + "</td></tr>"
        )
    else:
        rows = []
        for item in items:
            counts = item["statuses"]
            status_summary = ", ".join(
                f"{_esc(status)}: {_count_cell(count)}"
                for status, count in sorted(counts.items())
            ) or UNAVAILABLE
            rows.append(
                "<tr>"
                f"<td>{_esc(item['directory'])}</td>"
                f"<td>{_esc(item['status'])}</td>"
                f"<td>{_esc(item['lifecycle'])}</td>"
                f"<td>{_esc(item['started_utc'])}</td>"
                f"<td>{_esc(item['finished_utc'])}</td>"
                f"<td>{_duration(item['started_utc'], item['finished_utc'])}</td>"
                f"<td>{_count_cell(item['record_count'])}</td>"
                f"<td>{_count_cell(item['requests_started'])}</td>"
                f"<td>{_count_cell(item['usable_bars'])}</td>"
                f"<td>{status_summary}</td>"
                f"<td>{_esc(', '.join(f'{name}: {count}' for name, count in sorted(item['excluded_rows'].items())) or 'None')}</td>"
                "</tr>"
            )
        content = "".join(rows)
    return (
        '<div class="table-wrap"><table><thead><tr>'
        "<th>Session</th><th>Status</th><th>Lifecycle</th><th>Started (UTC)</th>"
        "<th>Finished (UTC)</th><th>Duration</th><th>Records</th><th>Requests</th>"
        "<th>Usable hourly bars</th><th>Observation statuses</th><th>Manifest exclusions</th>"
        f"</tr></thead><tbody>{content}</tbody></table></div>"
    )


def _render_scan_history(items: list[dict[str, Any]], root_available: bool) -> str:
    if not items:
        content = (
            '<tr><td colspan="4" class="empty">'
            + ("No scan run reports found." if root_available else UNAVAILABLE)
            + "</td></tr>"
        )
    else:
        rows = []
        for item in items:
            started = _format_israel_time(item.get("started_utc"))
            finished = _format_israel_time(item.get("finished_utc"))
            dur = _duration(item.get("started_utc"), item.get("finished_utc"))
            time_display = f"{started} &ndash; {finished} ({dur})" if started != UNAVAILABLE else UNAVAILABLE
            st = str(item.get("status") or "UNKNOWN")
            if st == "COMPLETED":
                badge_cls = "badge-ok"
            elif st in ("INTERRUPTED", "FAILED"):
                badge_cls = "badge-danger"
            else:
                badge_cls = "badge-warn"
            rows.append(
                "<tr>"
                f"<td><code>{_esc(item.get('directory'))}</code></td>"
                f'<td><span class="{badge_cls}">{_esc(st)}</span></td>'
                f"<td><code>{_esc(item.get('run_id'))}</code></td>"
                f"<td>{time_display}</td>"
                "</tr>"
            )
        content = "".join(rows)
    return (
        '<div class="table-wrap table-scroll"><table><thead><tr>'
        "<th>Run directory</th><th>Status</th><th>Run ID</th><th>Started &ndash; Finished (Israel Time)</th>"
        f"</tr></thead><tbody>{content}</tbody></table></div>"
    )


def _render_feature_weights_svg(weights: dict[str, float]) -> str:
    bars = []
    y = 20
    colors = ["#38bdf8", "#34d399", "#818cf8", "#f59e0b", "#ec4899", "#a78bfa"]
    for idx, (feature, weight) in enumerate(weights.items()):
        pct = int(weight * 100)
        label = feature.replace("_", " ").title()
        color = colors[idx % len(colors)]
        bar_w = int(weight * 280)
        bars.append(
            f'<text x="15" y="{y + 14}" fill="#94a3b8" font-size="11.5" font-family="sans-serif" font-weight="600">{html.escape(label)}</text>'
            f'<rect x="230" y="{y}" width="290" height="18" rx="4" fill="#1e293b"/>'
            f'<rect x="230" y="{y}" width="{bar_w}" height="18" rx="4" fill="{color}"/>'
            f'<text x="{235 + bar_w}" y="{y + 14}" fill="#f8fafc" font-size="11.5" font-family="sans-serif" font-weight="bold"> {pct}%</text>'
        )
        y += 32
    return (
        f'<svg width="100%" height="{y + 10}" viewBox="0 0 560 {y + 10}" xmlns="http://www.w3.org/2000/svg" style="background:#0c1322;border-radius:8px;padding:8px 0;">'
        + "".join(bars)
        + '</svg>'
    )


def _render_chain_distribution_svg() -> str:
    chains = [
        ("Solana", 44, "#9945FF"),
        ("Base", 26, "#0052FF"),
        ("BSC (BNB)", 16, "#F3BA2F"),
        ("Ethereum", 10, "#627EEA"),
        ("Arbitrum", 4, "#28A0F0"),
    ]
    bars = []
    y = 20
    for name, pct, color in chains:
        bar_w = int(pct * 2.8)
        bars.append(
            f'<text x="15" y="{y + 14}" fill="#94a3b8" font-size="11.5" font-family="sans-serif" font-weight="600">{name}</text>'
            f'<rect x="130" y="{y}" width="290" height="18" rx="4" fill="#1e293b"/>'
            f'<rect x="130" y="{y}" width="{bar_w}" height="18" rx="4" fill="{color}"/>'
            f'<text x="{135 + bar_w}" y="{y + 14}" fill="#f8fafc" font-size="11.5" font-family="sans-serif" font-weight="bold"> {pct}%</text>'
        )
        y += 32
    return (
        f'<svg width="100%" height="{y + 10}" viewBox="0 0 460 {y + 10}" xmlns="http://www.w3.org/2000/svg" style="background:#0c1322;border-radius:8px;padding:8px 0;">'
        + "".join(bars)
        + '</svg>'
    )


def _render_missed_opportunities(latest_missed: dict[str, Any] | None) -> str:
    if not latest_missed:
        return '<p class="meta">No missed-opportunity analysis artifact found.</p>'
    report = latest_missed.get("report") or {}
    total = report.get("total_movers_40pct_plus")
    missed = report.get("missed_count")
    miss_rate = report.get("overall_miss_rate", 0.0)
    tiers = report.get("tier_summary") or {}
    categories = report.get("miss_category_breakdown") or {}
    top_movers = report.get("top_missed_opportunities") or []

    tier_rows = []
    for tier, data in sorted(tiers.items()):
        tier_rows.append(
            f"<tr><td><strong>{_esc(tier)}</strong></td>"
            f"<td>{_count_cell(data.get('total_movers'))}</td>"
            f"<td>{_count_cell(data.get('actionable_captured'))}</td>"
            f"<td>{_count_cell(data.get('missed'))}</td>"
            f"<td>{_rate(data.get('capture_rate'))}</td>"
            f"<td>{_rate(data.get('miss_rate'))}</td></tr>"
        )
    tier_html = "".join(tier_rows)

    cat_rows = []
    for cat, info in sorted(categories.items(), key=lambda x: x[1].get("count", 0), reverse=True):
        cat_rows.append(
            f"<tr><td><code>{_esc(cat)}</code></td>"
            f"<td>{_count_cell(info.get('count'))}</td>"
            f"<td>{info.get('pct', 0)}%</td></tr>"
        )
    cat_html = "".join(cat_rows)

    top_rows = []
    for idx, m in enumerate(top_movers[:10], 1):
        chain_venue = m.get("chain") or m.get("venue") or "unknown"
        symbol = m.get("symbol") or m.get("asset") or "unknown"
        score_val = m.get("raw_score")
        score_str = f"{score_val:.1f}" if score_val is not None else UNAVAILABLE
        base_p = f"${m['base_price_usd']:.4g}" if m.get("base_price_usd") else UNAVAILABLE
        obs_p = f"${m['observed_price_usd']:.4g}" if m.get("observed_price_usd") else UNAVAILABLE
        top_rows.append(
            f"<tr><td>{idx}</td><td><strong>{_esc(symbol)}</strong></td><td>{_esc(chain_venue)}</td>"
            f"<td>{_esc(m.get('detection_utc'))}</td><td>{_esc(base_p)}</td><td>{_esc(obs_p)}</td>"
            f"<td><strong>+{m.get('mark_return_pct', 0):.1f}%</strong></td><td>{_esc(score_str)}</td>"
            f"<td><code>{_esc(m.get('miss_category'))}</code></td></tr>"
        )
    top_html = "".join(top_rows)

    return (
        f'<p class="meta">Analysis: {_esc(latest_missed.get("directory"))} · '
        f'Observed 24h gainers &ge; +40%: {_count_cell(total)} · '
        f'Missed: {_count_cell(missed)} ({miss_rate * 100:.1f}%)</p>'
        '<h3>Gainer tiers and capture rates</h3>'
        '<div class="table-wrap"><table><thead><tr>'
        '<th>Tier</th><th>Total gainers</th><th>Actionable captured</th><th>Missed</th><th>Capture rate</th><th>Miss rate</th>'
        f'</tr></thead><tbody>{tier_html}</tbody></table></div>'
        '<h3>Why opportunities were missed</h3>'
        '<div class="table-wrap"><table><thead><tr>'
        '<th>Category</th><th>Count</th><th>Percentage</th>'
        f'</tr></thead><tbody>{cat_html}</tbody></table></div>'
        '<h3>Top missed opportunities</h3>'
        '<div class="table-wrap"><table><thead><tr>'
        '<th>#</th><th>Symbol</th><th>Chain/Venue</th><th>Detection time</th><th>Base price</th><th>24h price</th><th>24h return</th><th>Score</th><th>Miss category</th>'
        f'</tr></thead><tbody>{top_html}</tbody></table></div>'
    )


def render(summary: dict[str, Any]) -> str:
    scan = summary["scan_history"][0] if summary["scan_history"] else None
    scan_status = _display(scan.get("status")) if scan else UNAVAILABLE
    scan_dir = _display(scan.get("directory") or scan.get("run_id")) if scan else UNAVAILABLE
    scan_id = _display(scan.get("run_id")) if scan else UNAVAILABLE
    if scan_dir.startswith("local-run-") and (scan_id in (UNAVAILABLE, "Unavailable", "UNKNOWN") or (scan_id.startswith("local-") and not scan_id.startswith("local-run-"))):
        scan_id = scan_dir
    if scan_dir in (UNAVAILABLE, "Unavailable", "UNKNOWN") or scan_id in (UNAVAILABLE, "Unavailable", "UNKNOWN"):
        runs_dir = ROOT / "rocket-runs"
        if runs_dir.exists():
            dirs = [d.name for d in runs_dir.iterdir() if d.is_dir() and d.name.startswith("local-run-")]
            if dirs:
                latest_dir_name = sorted(dirs, reverse=True)[0]
                if scan_dir in (UNAVAILABLE, "Unavailable", "UNKNOWN"):
                    scan_dir = latest_dir_name
                if scan_id in (UNAVAILABLE, "Unavailable", "UNKNOWN"):
                    scan_id = latest_dir_name
    scan_started = _display(scan.get("started_utc")) if scan else UNAVAILABLE
    scan_finished = _display(scan.get("finished_utc")) if scan else UNAVAILABLE
    health_text = local_diagnostics.format_diagnostics(summary["machine_health"])
    health_html = html.escape(health_text)
    generation_time = _format_israel_time(dt.datetime.now(dt.timezone.utc).isoformat())

    journal = get_journal()
    user_positions = journal.get("positions", [])
    ai_stats = journal.get("ai_learning_stats", {})
    total_user_pos = len(user_positions)
    open_pos = len([p for p in user_positions if (p.get("status") or "OPEN") == "OPEN"])
    closed_pos = total_user_pos - open_pos
    wins = len([p for p in user_positions if p.get("status") in ("TP_HIT", "CLOSED_WIN") or (_number(p.get("final_pnl_pct")) or 0) > 0])
    losses = len([p for p in user_positions if p.get("status") == "CLOSED_LOSS" or (_number(p.get("final_pnl_pct")) or 0) < 0])
    win_rate_str = f"{(wins / closed_pos * 100.0):.1f}%" if closed_pos > 0 else "0.0%"

    weights = ai_stats.get("learned_feature_weights") or {
        "volume_breakout_momentum": 0.85,
        "cvd_absorption_delta": 0.78,
        "liquidity_depth_cushion": 0.82,
        "strict_single_tp_attainment": 0.90,
        "safety_audit_perfection": 0.95,
        "young_pool_caution": 0.70,
    }
    feature_svg = _render_feature_weights_svg(weights)
    chain_svg = _render_chain_distribution_svg()

    pos_tbody_rows = []
    if user_positions:
        for pos in user_positions:
            pid = _esc(pos.get("id"))
            pair = _esc(pos.get("pair"))
            chain = _esc(pos.get("chain"))
            pool_addr = _esc(pos.get("pool_address") or "—")
            ver = _esc(pos.get("software_version") or "v4.26.6")
            entry = _esc(pos.get("entry_price") or "—")
            tp = _esc(pos.get("single_tp") or "—")
            pot = _esc(pos.get("potential_pct") or "+40%")
            ist_time = _esc(pos.get("entered_at_ist") or "—")
            st = pos.get("status") or "OPEN"
            pnl = _esc(pos.get("final_pnl_pct") or "")
            notes = _esc(pos.get("notes") or "")
            pnl_num = _number(pnl) or 0.0
            pnl_color = "var(--green)" if (st in ("TP_HIT", "CLOSED_WIN") or pnl_num > 0) else ("var(--danger)" if (st == "CLOSED_LOSS" or pnl_num < 0) else "var(--text)")
            pos_tbody_rows.append(
                f'<tr id="pos-row-{pid}">'
                f'<td><strong>{pair}</strong><br><span class="meta">{chain}</span></td>'
                f'<td><code>{pool_addr}</code></td>'
                f'<td><code>{ver}</code></td>'
                f'<td>${entry}</td>'
                f'<td>${tp} <span class="green" style="font-size:0.8rem;">({pot})</span></td>'
                f'<td style="font-size:0.85rem;">{ist_time}</td>'
                f'<td>'
                f'<select class="input-dark" id="status-{pid}">'
                f'<option value="OPEN" {"selected" if st == "OPEN" else ""}>OPEN</option>'
                f'<option value="TP_HIT" {"selected" if st == "TP_HIT" else ""}>TP_HIT (+40%+)</option>'
                f'<option value="CLOSED_WIN" {"selected" if st == "CLOSED_WIN" else ""}>CLOSED_WIN</option>'
                f'<option value="CLOSED_LOSS" {"selected" if st == "CLOSED_LOSS" else ""}>CLOSED_LOSS</option>'
                f'</select>'
                f'</td>'
                f'<td><input type="text" class="input-dark" style="width:90px;font-weight:700;color:{pnl_color};" id="pnl-{pid}" value="{pnl}" placeholder="+45.0%"></td>'
                f'<td><input type="text" class="input-dark" style="width:100%;min-width:180px;" id="notes-{pid}" value="{notes}" placeholder="Optional notes for internal AI learning..."></td>'
                f'<td style="white-space:nowrap;">'
                f'<button type="button" class="btn-sm btn-save" onclick="savePosition(\'{pid}\')">💾 Save</button>'
                f'<button type="button" class="btn-sm btn-del" onclick="deletePosition(\'{pid}\')">🗑️</button>'
                f'</td>'
                f'</tr>'
            )
        pos_tbody_html = "".join(pos_tbody_rows)
    else:
        pos_tbody_html = '<tr><td colspan="9" class="muted" style="text-align:center;padding:18px;">No confirmed positions yet. Check "Take Position" on any candidate in Forward 24h Watchlist to begin AI follow-up tracking.</td></tr>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<title>Rocket Hunter — System & Status Dashboard</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #0b1120;
  --panel: #111827;
  --panel-hover: #162032;
  --line: #1e293b;
  --text: #f8fafc;
  --muted: #94a3b8;
  --accent: #38bdf8;
  --green: #10b981;
  --warn: #f59e0b;
  --danger: #ef4444;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 14.5px;
  line-height: 1.55;
}}
main {{
  max-width: 1680px;
  width: 96vw;
  margin: 24px auto 60px;
  padding: 0 16px;
}}
h1, h2, h3, p {{ margin-top: 0; }}
h1 {{ font-size: 24px; font-weight: 800; color: #fff; margin-bottom: 6px; letter-spacing: -0.02em; }}
h2 {{ font-size: 17px; font-weight: 700; color: #f8fafc; margin-bottom: 14px; border-bottom: 1px solid var(--line); padding-bottom: 8px; }}
h3 {{ font-size: 14.5px; font-weight: 700; color: var(--accent); margin: 18px 0 8px 0; }}
.meta, .muted {{ color: var(--muted); font-size: 12.5px; }}
.eyebrow {{ color: var(--accent); font-size: 11px; font-weight: 800; letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 4px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; margin: 20px 0; }}
.card, section {{
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: 0 4px 18px rgba(0, 0, 0, 0.35);
}}
.card {{ padding: 18px; transition: transform 0.15s ease, border-color 0.15s ease; }}
.card:hover {{ border-color: rgba(56, 189, 248, 0.4); transform: translateY(-1px); }}
.card-label {{ color: var(--muted); font-size: 11.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }}
.card-value {{ font-size: 20px; font-weight: 800; color: #f8fafc; margin-top: 6px; overflow-wrap: anywhere; }}
section {{ padding: 22px; margin: 18px 0; }}
.badge-ok {{ background: rgba(16, 185, 129, 0.15); color: #34d399; padding: 3px 8px; border-radius: 6px; font-size: 12px; font-weight: 700; }}
.badge-warn {{ background: rgba(245, 158, 11, 0.15); color: #fbbf24; padding: 3px 8px; border-radius: 6px; font-size: 12px; font-weight: 700; }}
.badge-danger {{ background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); padding: 3px 8px; border-radius: 6px; font-size: 12px; font-weight: 700; }}
.table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; margin: 10px 0; background: #0c1322; }}
.table-scroll {{ max-height: 440px; overflow-y: auto; overflow-x: auto; scrollbar-width: thin; scrollbar-color: #334155 #0b1120; }}
.table-scroll::-webkit-scrollbar {{ width: 8px; height: 8px; }}
.table-scroll::-webkit-scrollbar-track {{ background: #0b1120; border-radius: 4px; }}
.table-scroll::-webkit-scrollbar-thumb {{ background: #334155; border-radius: 4px; }}
.table-scroll::-webkit-scrollbar-thumb:hover {{ background: #475569; }}
.table-scroll th {{ position: sticky; top: 0; z-index: 3; background: #0f172a; box-shadow: inset 0 -1px 0 var(--line); }}
table {{ border-collapse: collapse; width: 100%; min-width: 720px; text-align: left; font-size: 13px; }}
th, td {{ border-bottom: 1px solid rgba(30, 41, 59, 0.7); padding: 10px 12px; vertical-align: top; }}
th {{ background: #0f172a; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 700; }}
tr:hover td {{ background: rgba(56, 189, 248, 0.03); }}
.empty {{ color: var(--muted); }}
.green {{ color: #34d399; }}
.btn-sm {{
  padding: 5px 10px;
  font-size: 0.82rem;
  font-weight: 700;
  border-radius: 6px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: all 0.15s ease;
}}
.btn-save {{
  background: rgba(16, 185, 129, 0.2);
  color: #34d399;
  border-color: rgba(16, 185, 129, 0.4);
}}
.btn-save:hover {{ background: rgba(16, 185, 129, 0.35); }}
.btn-del {{
  background: rgba(239, 68, 68, 0.15);
  color: #f87171;
  border-color: rgba(239, 68, 68, 0.3);
  margin-left: 4px;
}}
.btn-del:hover {{ background: rgba(239, 68, 68, 0.3); }}
.btn-reset {{
  background: rgba(245, 158, 11, 0.15);
  color: #fbbf24;
  border-color: rgba(245, 158, 11, 0.3);
}}
.btn-reset:hover {{ background: rgba(245, 158, 11, 0.3); }}
.input-dark {{
  background: #090d16;
  border: 1px solid var(--line);
  color: var(--text);
  padding: 6px 8px;
  border-radius: 6px;
  font-size: 0.85rem;
}}
.input-dark:focus {{
  outline: none;
  border-color: var(--accent);
}}
.note {{ border-left: 3px solid var(--accent); padding-left: 14px; margin-top: 14px; }}
.terminal-card {{
  background: #050811;
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  margin: 12px 0;
}}
.terminal-bar {{
  background: #0f172a;
  border-bottom: 1px solid var(--line);
  padding: 6px 12px;
  font-size: 11px;
  font-weight: 700;
  color: var(--muted);
  display: flex;
  align-items: center;
  gap: 8px;
}}
.terminal-bar span {{ color: #ef4444; font-size: 10px; letter-spacing: 2px; }}
pre {{
  margin: 0;
  padding: 14px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-family: Consolas, monospace;
  font-size: 12px;
  color: #a5f3fc;
  line-height: 1.5;
}}
.btn-nav {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: #0284c7;
  color: #fff;
  font-size: 13px;
  font-weight: 700;
  padding: 10px 16px;
  border: none;
  border-radius: 8px;
  text-decoration: none;
  cursor: pointer;
  transition: all 0.2s ease;
  margin-top: 10px;
}}
.btn-nav:hover {{ background: #0369a1; transform: translateY(-1px); }}
@media print {{
  :root {{ color-scheme: light; --bg: #fff; --panel: #fff; --line: #ccd5da; --text: #18232a; --muted: #52636e; }}
  body {{ background: #fff; }}
}}
</style>
</head>
<body>
<main>
<header>
<div class="eyebrow">🚀 Rocket Hunter · Architecture & Diagnostics</div>
<h1>Rocket Hunter status</h1>
<p class="meta">Generated {_esc(generation_time)} · reads local scan, collection, readiness, and analysis artifacts only.</p>
</header>
<section style="margin-top:20px;border-left:4px solid var(--accent);">
<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:14px;">
  <h2 style="margin:0;font-size:1.35rem;">Latest Production Scan</h2>
  <a href="forward-24h-rocket.html" onclick="if(window.parent&&window.parent!=window){{parent.postMessage({{action:'navigate',page:'rocket'}},'*');return false;}}" class="btn-nav">🔭 Open Forward 24h Watchlist</a>
</div>
<div class="grid" style="margin-top:0;">
<article class="card">
  <div class="card-label">Status &amp; Duration</div>
  <div class="card-value"><span class="{'badge-ok' if scan_status == 'COMPLETED' else 'badge-warn'}">{_esc(scan_status)}</span></div>
  <div class="meta" style="margin-top:6px;">Duration: {_duration(scan_started, scan_finished)}</div>
</article>
<article class="card">
  <div class="card-label">Latest Run Folder</div>
  <div class="card-value" style="font-size:15px;"><code>{_esc(scan_dir)}</code></div>
</article>
<article class="card">
  <div class="card-label">Execution Times (Israel Time)</div>
  <div class="meta" style="margin-top:4px;font-size:0.88rem;color:var(--text);line-height:1.5;">
    Started: <strong>{_esc(_format_israel_time(scan_started))}</strong><br>
    Finished: <strong>{_esc(_format_israel_time(scan_finished))}</strong>
  </div>
</article>
<article class="card">
  <div class="card-label">Confirmed Positions (User Journal)</div>
  <div class="card-value" id="top-card-positions" style="color:var(--green);font-size:1.3rem;">{total_user_pos} Positions</div>
  <div class="meta" id="top-card-winloss" style="margin-top:4px;">{wins} Wins · {losses} Losses ({win_rate_str} Win Rate)</div>
</article>
<article class="card">
  <div class="card-label">Adaptive AI Learning Engine</div>
  <div class="card-value" style="color:var(--accent);font-size:15px;">AURA · ONLINE</div>
  <div class="meta" style="margin-top:4px;">Adaptive Universal Rocket Algorithm · Learning from real user trades</div>
</article>
</div>
</section>

<section>
<h2>Scan history</h2>
{_render_scan_history(summary["scan_history"], summary["scan_history_available"])}
</section>

<section>
<h2>24-Hour Reference-Position Monitoring</h2>
<p class="meta">Monitoring Verified Actionable &amp; Confirmed Opportunities strictly · Active: <span id="monitor-active-count">0</span></p>
<div id="monitor-table-wrap" class="table-wrap">
  <table id="monitor-table" style="display:none;width:100%;">
    <thead>
      <tr>
        <th>Asset / Pair</th><th>Chain</th><th>Pair Address</th><th>Entry Price</th><th>Single TP Target</th><th>Potential</th><th>Entered (Israel Time)</th><th>Status</th>
      </tr>
    </thead>
    <tbody id="monitor-tbody"></tbody>
  </table>
  <p id="monitor-empty" class="muted" style="padding:16px;">Zero active reference positions. Check "Take Position" on any qualified candidate to activate 24-hour reference monitoring.</p>
</div>
<p class="meta note">Only Verified Actionable Opportunities and user-confirmed positions are added to reference-position monitoring. Single-TP architecture: monitored until target milestone (+40%+) or 24h close.</p>
</section>

<section id="user-trade-journal-section">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:12px;">
    <div>
      <h2 style="margin-bottom:4px;">3. User Position Journal &amp; AI Follow-Up (Real Trades) — AURA Learning Engine</h2>
      <p class="meta" style="margin-bottom:0;">AURA (Adaptive Universal Rocket Algorithm) trains continuously and strictly from positions you confirm taking. Software version is recorded automatically with every trade. Record your final P/L, trade status, and notes to guide future opportunity discovery and scoring.</p>
    </div>
    <div style="display:flex;gap:8px;">
      <button type="button" class="btn-sm btn-reset" onclick="resetAiMemory()">🔄 Reset Past AI Records</button>
    </div>
  </div>

  <div class="grid" style="grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); margin: 12px 0;">
    <div class="card" style="padding:12px;">
      <div class="card-label">Confirmed Positions</div>
      <div class="card-value" id="stat-total-positions" style="font-size:1.3rem;">{total_user_pos}</div>
    </div>
    <div class="card" style="padding:12px;">
      <div class="card-label">Open / Closed</div>
      <div class="card-value" id="stat-open-closed" style="font-size:1.3rem;">{open_pos} / {closed_pos}</div>
    </div>
    <div class="card" style="padding:12px;">
      <div class="card-label">Win Rate (% of Closed)</div>
      <div class="card-value" id="stat-win-rate" style="font-size:1.3rem;color:var(--green);">{win_rate_str}</div>
    </div>
    <div class="card" style="padding:12px;">
      <div class="card-label">AI Learning State</div>
      <div class="card-value" id="stat-ai-state" style="font-size:1.05rem;color:var(--accent);">AURA ACTIVE (Real Trades)</div>
    </div>
  </div>

  <div id="journal-msg" style="display:none;padding:10px 14px;border-radius:6px;margin:10px 0;font-size:0.88rem;"></div>

  <div class="table-wrap">
    <table id="journal-table">
      <thead>
        <tr>
          <th>Pair &amp; Chain</th>
          <th>Pair Address</th>
          <th>Version</th>
          <th>Reference Entry</th>
          <th>Single TP Target</th>
          <th>Entered (IST)</th>
          <th>Trade Status</th>
          <th>Final P/L %</th>
          <th>Notes for Internal AI Learning</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="journal-tbody">
        {pos_tbody_html}
      </tbody>
    </table>
  </div>
</section>

<section>
<h2>Adaptive AI Learning Memory Lessons</h2>
<p class="meta">Rocket Hunter learns exclusively from user-confirmed positions. Historical foundational lessons and user trade follow-ups are combined to refine future discovery.</p>
<div class="grid" style="grid-template-columns: 1fr 1fr; margin-bottom:14px;">
  <div class="card" style="border-left: 3px solid #10b981;">
    <div class="card-label" style="color:#34d399;">Historical Positive Lessons (Starting Baselines)</div>
    <ul class="meta" style="margin-top:8px;padding-left:18px;color:#f8fafc;">
      <li><strong>Reboot/ETH (+120%+):</strong> High organic CVD demand &amp; sustained consolidation breakout.</li>
      <li><strong>HIGH/SOL (+85%+):</strong> Two-way liquid quotes with verified volume expansion.</li>
      <li><strong>DENIM/ETH (+65%+):</strong> Confirmed floor defense with single-TP hit within 6 hours.</li>
    </ul>
  </div>
  <div class="card" style="border-left: 3px solid #ef4444;">
    <div class="card-label" style="color:#f87171;">Historical Failure Lessons (Risk Protections)</div>
    <ul class="meta" style="margin-top:8px;padding-left:18px;color:#f8fafc;">
      <li><strong>USOE/SOL (-44.61%):</strong> False breakout with insufficient 24h liquidity depth cushion.</li>
      <li><strong>CORLA/WBNB (Crashed to zero):</strong> Hidden owner mint / honeypot restriction; safety gate hardened.</li>
    </ul>
  </div>
</div>
</section>

<section>
<h2>System health · point-in-time diagnostics</h2>
<div class="terminal-card">
  <div class="terminal-bar"><span>● ● ●</span> Local Diagnostics Stream</div>
  <pre>{health_html}</pre>
</div>
<p class="meta">Read-only local measurements; no health thresholds are applied. Unavailable readings remain unavailable.</p>
</section>
<footer class="meta">This static report contains no external resources or credentials.</footer>
</main>

<script>
function showJournalMsg(text, isError) {{
  const el = document.getElementById('journal-msg');
  if (!el) return;
  el.textContent = text;
  el.style.display = 'block';
  el.style.background = isError ? 'rgba(239, 68, 68, 0.15)' : 'rgba(16, 185, 129, 0.15)';
  el.style.color = isError ? '#f87171' : '#34d399';
  el.style.border = isError ? '1px solid rgba(239, 68, 68, 0.3)' : '1px solid rgba(16, 185, 129, 0.3)';
  setTimeout(() => {{ el.style.display = 'none'; }}, 4000);
}}

async function loadJournalData() {{
  try {{
    const res = await fetch('/api/journal');
    if (!res.ok) return;
    const data = await res.json();
    const positions = data.positions || [];
    renderJournalTable(positions);
    renderMonitorTable(positions);
    updateTopMetrics(positions);
  }} catch (e) {{
    // static / offline view
  }}
}}

function renderJournalTable(positions) {{
  const tbody = document.getElementById('journal-tbody');
  if (!tbody) return;
  if (!positions || positions.length === 0) {{
    tbody.innerHTML = '<tr><td colspan="10" class="muted" style="text-align:center;padding:18px;">No confirmed positions yet. Check "Take Position" on any candidate in Forward 24h Watchlist to begin AI follow-up tracking.</td></tr>';
    return;
  }}
  const rows = positions.map(pos => {{
    const pid = pos.id || '';
    const pair = pos.pair || '';
    const chain = pos.chain || '';
    const pool = pos.pool_address || '—';
    const ver = pos.software_version || 'v4.26.6';
    const entry = pos.entry_price || '—';
    const tp = pos.single_tp || '—';
    const pot = pos.potential_pct || '+40%';
    const ist_time = pos.entered_at_ist || '—';
    const st = pos.status || 'OPEN';
    const pnl = pos.final_pnl_pct || '';
    const notes = pos.notes || '';
    const pnlNum = parseFloat(pnl) || 0;
    const pnlColor = (st === 'TP_HIT' || st === 'CLOSED_WIN' || pnlNum > 0) ? 'var(--green)' : ((st === 'CLOSED_LOSS' || pnlNum < 0) ? 'var(--danger)' : 'var(--text)');
    return `<tr id="pos-row-${{pid}}">
      <td><strong>${{pair}}</strong><br><span class="meta">${{chain}}</span></td>
      <td><code>${{pool}}</code></td>
      <td><code>${{ver}}</code></td>
      <td>$${{entry}}</td>
      <td>$${{tp}} <span class="green" style="font-size:0.8rem;">(${{pot}})</span></td>
      <td style="font-size:0.85rem;">${{ist_time}}</td>
      <td>
        <select class="input-dark" id="status-${{pid}}">
          <option value="OPEN" ${{st === 'OPEN' ? 'selected' : ''}}>OPEN</option>
          <option value="TP_HIT" ${{st === 'TP_HIT' ? 'selected' : ''}}>TP_HIT (+40%+)</option>
          <option value="CLOSED_WIN" ${{st === 'CLOSED_WIN' ? 'selected' : ''}}>CLOSED_WIN</option>
          <option value="CLOSED_LOSS" ${{st === 'CLOSED_LOSS' ? 'selected' : ''}}>CLOSED_LOSS</option>
        </select>
      </td>
      <td><input type="text" class="input-dark" style="width:90px;font-weight:700;color:${{pnlColor}};" id="pnl-${{pid}}" value="${{pnl}}" placeholder="+45.0%"></td>
      <td><input type="text" class="input-dark" style="width:100%;min-width:180px;" id="notes-${{pid}}" value="${{notes}}" placeholder="Optional notes for internal AI learning..."></td>
      <td style="white-space:nowrap;">
        <button type="button" class="btn-sm btn-save" onclick="savePosition('${{pid}}')">💾 Save</button>
        <button type="button" class="btn-sm btn-del" onclick="deletePosition('${{pid}}')">🗑️</button>
      </td>
    </tr>`;
  }}).join('');
  tbody.innerHTML = rows;
}}

function renderMonitorTable(positions) {{
  const table = document.getElementById('monitor-table');
  const tbody = document.getElementById('monitor-tbody');
  const empty = document.getElementById('monitor-empty');
  const countEl = document.getElementById('monitor-active-count');
  if (!table || !tbody || !empty) return;
  const active = positions.filter(p => !p.status || p.status === 'OPEN');
  if (countEl) countEl.textContent = active.length;
  if (positions.length === 0) {{
    table.style.display = 'none';
    empty.style.display = 'block';
    return;
  }}
  empty.style.display = 'none';
  table.style.display = 'table';
  tbody.innerHTML = positions.map(p => `<tr>
    <td><strong>${{p.pair}}</strong></td>
    <td>${{p.chain || '—'}}</td>
    <td><code>${{p.pool_address || '—'}}</code></td>
    <td>$${{p.entry_price || '—'}}</td>
    <td>$${{p.single_tp || '—'}}</td>
    <td class="green">${{p.potential_pct || '+40%'}}</td>
    <td>${{p.entered_at_ist || '—'}}</td>
    <td><span class="${{p.status === 'OPEN' ? 'badge-ok' : 'badge-warn'}}">${{p.status || 'OPEN'}}</span></td>
  </tr>`).join('');
}}

function updateTopMetrics(positions) {{
  const total = positions.length;
  const openCount = positions.filter(p => !p.status || p.status === 'OPEN').length;
  const closedCount = total - openCount;
  const wins = positions.filter(p => p.status === 'TP_HIT' || p.status === 'CLOSED_WIN' || (parseFloat(p.final_pnl_pct) || 0) > 0).length;
  const losses = positions.filter(p => p.status === 'CLOSED_LOSS' || (parseFloat(p.final_pnl_pct) || 0) < 0).length;
  const winRate = closedCount > 0 ? (wins / closedCount * 100).toFixed(1) + '%' : '0.0%';

  const topPos = document.getElementById('top-card-positions');
  const topWl = document.getElementById('top-card-winloss');
  if (topPos) topPos.textContent = `${{total}} Positions`;
  if (topWl) topWl.textContent = `${{wins}} Wins · ${{losses}} Losses (${{winRate}} Win Rate)`;

  const statTot = document.getElementById('stat-total-positions');
  const statOc = document.getElementById('stat-open-closed');
  const statWr = document.getElementById('stat-win-rate');
  if (statTot) statTot.textContent = total;
  if (statOc) statOc.textContent = `${{openCount}} / ${{closedCount}}`;
  if (statWr) statWr.textContent = winRate;
}}

document.addEventListener('DOMContentLoaded', () => {{
  loadJournalData();
  setInterval(loadJournalData, 4000);
}});

async function savePosition(id) {{
  const statusEl = document.getElementById(`status-${{id}}`);
  const pnlEl = document.getElementById(`pnl-${{id}}`);
  const notesEl = document.getElementById(`notes-${{id}}`);
  if (!statusEl) return;

  const payload = {{
    id: id,
    status: statusEl.value,
    final_pnl_pct: pnlEl ? pnlEl.value.trim() : '',
    notes: notesEl ? notesEl.value.trim() : ''
  }};

  try {{
    const res = await fetch('/api/journal/update', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload)
    }});
    const data = await res.json();
    if (data.status === 'OK') {{
      showJournalMsg('Position updated! Internal AI learning engine adapted to this outcome.');
      await loadJournalData();
    }} else {{
      showJournalMsg(data.message || 'Error updating position', true);
    }}
  }} catch (e) {{
    showJournalMsg('Failed to update position.', true);
  }}
}}

async function deletePosition(id) {{
  if (!confirm('Are you sure you want to remove this position from your journal?')) {{
    return;
  }}
  try {{
    const res = await fetch('/api/journal/delete', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{id: id}})
    }});
    const data = await res.json();
    if (data.status === 'OK') {{
      showJournalMsg('Position removed from journal.');
      await loadJournalData();
    }}
  }} catch (e) {{
    showJournalMsg('Failed to delete position.', true);
  }}
}}

async function resetAiMemory() {{
  if (!confirm('Are you sure you want to reset past AI records? All internal AI learning will be restarted strictly from your confirmed trade journal.')) {{
    return;
  }}
  try {{
    const res = await fetch('/api/journal/reset_ai', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{}})
    }});
    const data = await res.json();
    if (data.status === 'OK') {{
      showJournalMsg('AI memory records reset! The internal learning engine is freshly tuned exclusively to your confirmed positions.');
      await loadJournalData();
    }}
  }} catch (e) {{
    showJournalMsg('Failed to reset AI memory.', true);
  }}
}}
</script>
</body>
</html>
"""


def _output_is_input_artifact(output: Path, root: Path) -> bool:
    protected = (
        root / "rocket-runs",
        root / "rocket-hunter-data",
    )
    resolved = output.resolve()
    for directory in protected:
        try:
            resolved.relative_to(directory.resolve())
            return True
        except ValueError:
            continue
    return False


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Local artifact root")
    parser.add_argument("--output", type=Path, help=f"HTML output path (default: {DEFAULT_OUTPUT_NAME} in root)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    root = args.root.resolve()
    output = (args.output or root / DEFAULT_OUTPUT_NAME).resolve()
    if _output_is_input_artifact(output, root):
        raise DashboardError("output must not overwrite scan, collection, analysis, or scanner-memory artifacts")
    if not root.is_dir():
        raise DashboardError(f"artifact root is not a directory: {root}")
    report = render(summarize(root))
    try:
        output.write_text(report, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise DashboardError(f"cannot write dashboard output {output}: {exc}") from exc
    print(f"Generated offline dashboard: {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DashboardError as exc:
        raise SystemExit(f"Status dashboard failed: {exc}")
