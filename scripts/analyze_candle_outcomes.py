#!/usr/bin/env python3
"""Analyze post-detection hourly candle coverage from collected watchlist snapshots."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEMORY = ROOT / "rocket-hunter-data" / "scanner-memory"
DEFAULT_OBSERVATIONS = ROOT / "rocket-hunter-data" / "research-data" / "watchlist-observations"
DEFAULT_OUTPUT_ROOT = ROOT / "rocket-hunter-data" / "research-analysis"
DEFAULT_EXPORTS = ROOT / "rocket-hunter-data" / "research-exports"
DEFAULT_RUNS = ROOT / "rocket-runs"
ANALYSIS_VERSION = "rocket-hunter-candle-outcomes.v4"
MANAGED_BY = "rocket-hunter-analyze-candle-outcomes"
OBSERVATION_SCHEMA = "crypto-prebreakout.market-observation.v1"
OBSERVATION_TYPE = "watchlist_market_observation"
OBSERVED_STATUS = "OBSERVED"
USD_GECKO_SOURCE = "GeckoTerminal exact-token USD 1h OHLCV"
BAR_SECONDS = 3600
WINDOW_HOURS = 24
TARGETS_PCT = (40, 60, 100)


class AnalysisError(RuntimeError):
    """Raised when source files change or an output path is unsafe."""


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n",
                    encoding="utf-8")


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
    result = float(value)
    return result if math.isfinite(result) else None


def _forecast_files(memory_dir: Path) -> list[Path]:
    directory = memory_dir / "forecasts"
    if not directory.is_dir():
        raise AnalysisError(f"forecast ledger directory is missing: {directory}")
    files = sorted(directory.glob("*.jsonl.gz"), key=lambda item: item.name)
    if any(path.is_symlink() or not path.is_file() for path in files):
        raise AnalysisError(f"forecast source partitions must be regular files: {directory}")
    return files


def _observation_sources(
    observation_root: Path,
) -> tuple[list[Path], list[Path], dict[Path, dict[str, Any]]]:
    if not observation_root.exists():
        return [], [], {}
    if observation_root.is_symlink() or not observation_root.is_dir():
        raise AnalysisError(f"observation source must be a regular directory: {observation_root}")

    manifests = []
    observations = []
    manifest_by_observation = {}
    for run_dir in sorted(observation_root.glob("collection-*"), key=lambda item: str(item)):
        if run_dir.is_symlink() or not run_dir.is_dir():
            continue
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise AnalysisError(f"collection manifest must be a regular file: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AnalysisError(f"cannot read collection manifest: {manifest_path}") from exc
        if not isinstance(manifest, dict):
            raise AnalysisError(f"collection manifest must contain an object: {manifest_path}")
        manifests.append(manifest_path)

        collection_run_id = manifest.get("collection_run_id")
        record_count = manifest.get("record_count")
        if not isinstance(collection_run_id, str) or not collection_run_id:
            raise AnalysisError(f"collection manifest has no collection_run_id: {manifest_path}")
        if isinstance(record_count, bool) or not isinstance(record_count, int) or record_count < 0:
            raise AnalysisError(f"collection manifest has invalid record_count: {manifest_path}")
        output_file = manifest.get("output_file", "observations.jsonl")
        if not isinstance(output_file, str) or not output_file:
            raise AnalysisError(f"invalid collection output_file in {run_dir.name}")
        relative = Path(output_file)
        if relative.is_absolute():
            raise AnalysisError(f"collection output_file must be relative in {run_dir.name}")
        observation_path = run_dir / relative
        try:
            observation_path.resolve().relative_to(run_dir.resolve())
        except ValueError as exc:
            raise AnalysisError(f"collection output_file escapes its session: {run_dir.name}") from exc
        cursor = run_dir
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise AnalysisError(f"collection observation file must not use symlinks: {run_dir.name}")
        present = observation_path.exists()
        manifest_by_observation[observation_path] = {
            "collection_run_id": collection_run_id,
            "record_count": record_count,
            "present": present,
        }
        if not present:
            continue
        if not observation_path.is_file():
            raise AnalysisError(f"collection observation source must be a regular file: {observation_path}")
        observations.append(observation_path)
    return manifests, observations, manifest_by_observation


def _source_snapshot(memory_dir: Path, observation_root: Path):
    forecast_files = _forecast_files(memory_dir)
    manifest_files, observation_files, manifest_by_observation = _observation_sources(observation_root)
    files = []
    stats = {}
    digest = hashlib.sha256()
    for kind, paths in (
        ("forecasts", forecast_files),
        ("collection_manifests", manifest_files),
        ("observations", observation_files),
    ):
        for path in paths:
            stat = path.stat()
            item = {
                "dataset": kind,
                "path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                "sha256": _sha256_file(path),
                "size_bytes": stat.st_size,
            }
            files.append(item)
            stats[path] = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, item["sha256"])
            digest.update(_json_text(item).encode("utf-8"))
    return (
        forecast_files, manifest_files, observation_files, manifest_by_observation,
        stats, files, digest.hexdigest(),
    )


def _verify_source_snapshot(memory_dir, observation_root, forecast_files, manifest_files,
                            observation_files, stats):
    if _forecast_files(memory_dir) != forecast_files:
        raise AnalysisError("forecast partition list changed during analysis")
    current_manifests, current_observations, _ = _observation_sources(observation_root)
    if current_manifests != manifest_files:
        raise AnalysisError("collection manifest list changed during analysis")
    if current_observations != observation_files:
        raise AnalysisError("observation file list changed during analysis")
    for path, before in stats.items():
        after = path.stat()
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns, _sha256_file(path)) != before:
            raise AnalysisError(f"source file changed during analysis: {path}")


def _read_forecasts(paths: list[Path], counters: collections.Counter[str]):
    records = {}
    for path in paths:
        import gzip
        try:
            with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    counters["forecast_source_records"] += 1
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        counters["forecast_malformed_json"] += 1
                        continue
                    if not isinstance(record, dict):
                        counters["forecast_non_object_records"] += 1
                        continue
                    forecast_id = record.get("forecast_id")
                    if not isinstance(forecast_id, str) or not forecast_id:
                        counters["forecast_missing_id"] += 1
                        continue
                    if forecast_id in records:
                        counters["forecast_duplicate_ids"] += 1
                        continue
                    records[forecast_id] = record
                    counters["forecasts_loaded"] += 1
        except (OSError, EOFError, UnicodeDecodeError, zlib.error):
            counters["forecast_malformed_partitions"] += 1
    return records


def _identity_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


def _forecast_identity(forecast: dict[str, Any]) -> tuple[tuple[str, str, str] | None, list[str], dict[str, str | None]]:
    features = forecast.get("feature_snapshot")
    if not isinstance(features, dict):
        features = {}
    chain = _identity_text(features.get("chain"))
    token = _identity_text(features.get("contract_address") or features.get("token_mint"))
    pool = _identity_text(features.get("pool_address") or features.get("pair_address"))
    missing = [field for field, value in (("chain", chain), ("token", token), ("pool", pool)) if value is None]
    optional = {
        "quote_asset": _identity_text(features.get("quote_asset")),
        "quote_symbol": _identity_text(features.get("quote_symbol")),
        "pair_orientation": _identity_text(features.get("pair_orientation")),
    }
    return ((chain, token, pool) if not missing else None), missing, optional


def _observation_identity(observation: dict[str, Any]):
    fields = observation.get("identity_fields")
    if not isinstance(fields, dict):
        return None, ["identity_fields"]
    chain = _identity_text(fields.get("chain"))
    token = _identity_text(fields.get("contract_address") or fields.get("token_mint"))
    pool = _identity_text(fields.get("pool_address") or fields.get("pair_address"))
    missing = [field for field, value in (("chain", chain), ("token", token), ("pool", pool)) if value is None]
    optional = {
        "quote_asset": _identity_text(fields.get("quote_asset")),
        "quote_symbol": _identity_text(fields.get("quote_symbol")),
        "pair_orientation": _identity_text(fields.get("pair_orientation")),
    }
    return ((chain, token, pool) if not missing else None), missing, optional


def _identity_conflicts(forecast_optional: dict[str, str | None],
                        observation_optional: dict[str, str | None]) -> list[str]:
    conflicts = []
    for field in ("quote_asset", "quote_symbol", "pair_orientation"):
        expected = forecast_optional.get(field)
        actual = observation_optional.get(field)
        if expected is not None and actual is not None and expected != actual:
            conflicts.append(field)
    return conflicts


def _read_observations(
    paths: list[Path],
    counters: collections.Counter[str],
    manifest_by_observation: dict[Path, dict[str, Any]],
):
    records = []
    as_of_candidates = []
    counters["observation_records_declared"] = sum(
        manifest["record_count"] for manifest in manifest_by_observation.values()
    )
    for manifest in manifest_by_observation.values():
        if not manifest["present"]:
            counters["observation_files_missing"] += 1
            counters["observation_records_missing_from_manifest_count"] += manifest["record_count"]
    for path in paths:
        manifest = manifest_by_observation[path]
        declared_count = manifest["record_count"]
        collection_run_id = manifest["collection_run_id"]
        source_record_count = 0
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    source_record_count += 1
                    counters["observation_source_records"] += 1
                    if source_record_count > declared_count:
                        counters["observation_records_beyond_manifest_count"] += 1
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        counters["observation_malformed_json"] += 1
                        continue
                    if not isinstance(record, dict):
                        counters["observation_non_object_records"] += 1
                        continue
                    if record.get("collection_run_id") != collection_run_id:
                        counters["observation_collection_run_id_mismatch"] += 1
                        continue
                    if record.get("schema") != OBSERVATION_SCHEMA or record.get("record_type") != OBSERVATION_TYPE:
                        counters["observation_unsupported_schema"] += 1
                        continue
                    if not isinstance(record.get("record_id"), str) or not record["record_id"]:
                        counters["observation_missing_record_id"] += 1
                        continue
                    for field in ("source_received_utc", "collected_utc"):
                        stamp = _parse_utc(record.get(field))
                        if stamp is not None:
                            as_of_candidates.append(stamp)
                    if record.get("source_status") != OBSERVED_STATUS:
                        counters[f"observation_status:{record.get('source_status') or 'MISSING'}"] += 1
                        continue
                    records.append((record, str(path)))
                    counters["observations_loaded"] += 1
        except (OSError, UnicodeDecodeError):
            counters["observation_unreadable_files"] += 1
        if source_record_count < declared_count:
            counters["observation_records_missing_from_manifest_count"] += (
                declared_count - source_record_count
            )
    as_of = max(as_of_candidates) if as_of_candidates else None
    return records, as_of


def _bar_from_record(raw: Any, source_received: dt.datetime | None):
    if not isinstance(raw, dict):
        return None, "malformed_bar"
    opened = _parse_utc(raw.get("time_open"))
    closed = _parse_utc(raw.get("time_close"))
    if opened is None or closed is None:
        return None, "missing_or_invalid_bar_timestamp"
    if opened.minute or opened.second or opened.microsecond:
        return None, "bar_open_not_hour_aligned"
    if closed - opened != dt.timedelta(hours=1):
        return None, "bar_duration_not_one_hour"
    if source_received is None:
        return None, "missing_source_received_timestamp"
    if closed > source_received:
        return None, "bar_not_closed_when_observed"
    high, low, close = (_finite_number(raw.get(field)) for field in ("high", "low", "close"))
    if high is None or low is None or close is None:
        return None, "missing_or_invalid_price"
    if low <= 0 or high < low or close < low or close > high:
        return None, "invalid_ohlc_relationship"
    return {
        "time_open": opened,
        "time_close": closed,
        "high": high,
        "low": low,
        "close": close,
    }, None


def _forecast_price(forecast: dict[str, Any]):
    features = forecast.get("feature_snapshot")
    if not isinstance(features, dict):
        return None, "missing_feature_snapshot"
    price = _finite_number(features.get("price_usd"))
    if price is None or price <= 0:
        return None, "missing_or_invalid_forecast_price_usd"
    snapshot = _parse_utc(forecast.get("snapshot_utc"))
    retrieved = _parse_utc(features.get("retrieved_utc"))
    if snapshot is None or retrieved is None:
        return None, "missing_forecast_price_timestamp"
    age_seconds = (snapshot - retrieved).total_seconds()
    if age_seconds < -60 or age_seconds > 30 * 60:
        return None, "forecast_price_not_fresh_at_detection"
    return (price, snapshot), None


def _first_full_bar(snapshot: dt.datetime) -> dt.datetime:
    floored = snapshot.replace(minute=0, second=0, microsecond=0)
    return floored + dt.timedelta(hours=1)


def _expected_bar_opens(snapshot: dt.datetime) -> list[dt.datetime]:
    horizon_end = snapshot + dt.timedelta(hours=WINDOW_HOURS)
    current = _first_full_bar(snapshot)
    opens = []
    while current + dt.timedelta(hours=1) <= horizon_end:
        opens.append(current)
        current += dt.timedelta(hours=1)
    return opens


def _identical_bar(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (left["high"], left["low"], left["close"]) == (
        right["high"], right["low"], right["close"]
    )


def _gap_runs(missing: list[dt.datetime]) -> list[dict[str, Any]]:
    if not missing:
        return []
    runs = []
    start = previous = missing[0]
    count = 1
    for opened in missing[1:]:
        if opened - previous == dt.timedelta(hours=1):
            count += 1
        else:
            runs.append({"start_utc": start.isoformat().replace("+00:00", "Z"), "hours": count})
            start = opened
            count = 1
        previous = opened
    runs.append({"start_utc": start.isoformat().replace("+00:00", "Z"), "hours": count})
    return runs


def _make_forecast_observations(forecasts, observations, as_of, counters):
    forecast_by_identity = collections.defaultdict(list)
    forecast_info = {}
    forecast_exclusion_reasons = collections.defaultdict(set)
    for forecast_id, forecast in forecasts.items():
        identity, missing, optional = _forecast_identity(forecast)
        if identity is None:
            counters["forecasts_missing_join_identity"] += 1
            forecast_exclusion_reasons[forecast_id].add("missing_or_ambiguous_forecast_identity")
            for field in missing:
                counters[f"forecast_missing_identity_field:{field}"] += 1
            continue
        forecast_by_identity[identity].append((forecast_id, forecast, optional))
        forecast_info[forecast_id] = {
            "identity": identity,
            "optional_identity": optional,
        }

    bars_by_forecast = collections.defaultdict(dict)
    conflicts_by_forecast = collections.defaultdict(set)
    duplicates_by_forecast = collections.defaultdict(set)
    joinable_forecasts = set()
    observation_ids = set()
    duplicate_observation_ids = 0
    for observation, source_path in observations:
        observation_id = observation["record_id"]
        if observation_id in observation_ids:
            duplicate_observation_ids += 1
            counters["duplicate_observation_record_ids"] += 1
            continue
        observation_ids.add(observation_id)
        identity, missing, optional = _observation_identity(observation)
        if identity is None:
            counters["unjoinable_observations_missing_identity"] += 1
            for field in missing:
                counters[f"observation_missing_identity_field:{field}"] += 1
            continue
        candidates = forecast_by_identity.get(identity, [])
        if not candidates:
            counters["unjoinable_observations_no_exact_forecast_identity"] += 1
            continue
        market_type = str(observation.get("source") or "")
        if market_type != USD_GECKO_SOURCE:
            counters["unjoinable_observations_unsupported_price_unit"] += 1
            for forecast_id, _, _ in candidates:
                forecast_exclusion_reasons[forecast_id].add("unsupported_observation_price_source")
            continue
        conflicts = [
            (forecast_id, fields)
            for forecast_id, _, forecast_optional in candidates
            if (fields := _identity_conflicts(forecast_optional, optional))
        ]
        if len(conflicts) == len(candidates):
            counters["unjoinable_observations_identity_conflict"] += 1
            for forecast_id, fields in conflicts:
                for field in fields:
                    forecast_exclusion_reasons[forecast_id].add(
                        f"optional_identity_conflict:{field}"
                    )
                for field in fields:
                    counters[f"identity_conflict:{field}"] += 1
            continue
        source_evidence = observation.get("source_evidence")
        if not isinstance(source_evidence, dict) or source_evidence.get("resolution") != "1h":
            counters["observations_unsupported_resolution"] += 1
            for forecast_id, _, _ in candidates:
                forecast_exclusion_reasons[forecast_id].add("unsupported_observation_resolution")
            continue
        source_received = _parse_utc(observation.get("source_received_utc"))
        raw_bars = source_evidence.get("bars")
        if not isinstance(raw_bars, list):
            counters["observations_missing_bar_list"] += 1
            for forecast_id, _, _ in candidates:
                forecast_exclusion_reasons[forecast_id].add("missing_observation_bar_list")
            continue
        applicable_candidates = []
        for forecast_id, forecast, forecast_optional in candidates:
            if _identity_conflicts(forecast_optional, optional):
                counters["observation_forecast_optional_identity_conflict"] += 1
                for field in _identity_conflicts(forecast_optional, optional):
                    forecast_exclusion_reasons[forecast_id].add(
                        f"optional_identity_conflict:{field}"
                    )
                continue
            usable, reason = _forecast_price(forecast)
            if reason:
                counters[f"forecast_excluded:{reason}"] += 1
                forecast_exclusion_reasons[forecast_id].add(
                    f"invalid_forecast_price:{reason}"
                )
                continue
            baseline, snapshot = usable
            applicable_candidates.append((forecast_id, forecast, baseline, snapshot))
        if not applicable_candidates:
            counters["observations_without_compatible_forecast"] += 1
            continue
        bars = []
        for raw_bar in raw_bars:
            bar, reason = _bar_from_record(raw_bar, source_received)
            if reason:
                counters[f"bar_excluded:{reason}"] += 1
                continue
            bars.append(bar)
        if not bars:
            counters["observations_without_valid_bars"] += 1
            for forecast_id, _, _, _ in applicable_candidates:
                forecast_exclusion_reasons[forecast_id].add("no_valid_hourly_bars")
            continue
        counters["identity_joined_observations"] += 1
        for forecast_id, forecast, baseline, snapshot in applicable_candidates:
            joinable_forecasts.add(forecast_id)
            expected = _expected_bar_opens(snapshot)
            expected_set = set(expected)
            horizon_end = snapshot + dt.timedelta(hours=WINDOW_HOURS)
            for bar in bars:
                opened = bar["time_open"]
                if opened <= snapshot:
                    counters["detection_overlapping_or_prior_bars_excluded"] += 1
                    continue
                if opened not in expected_set:
                    counters["bars_outside_forecast_window_excluded"] += 1
                    continue
                existing = bars_by_forecast[forecast_id].get(opened)
                if opened in conflicts_by_forecast[forecast_id]:
                    counters["duplicate_bar_records"] += 1
                    duplicates_by_forecast[forecast_id].add(opened)
                    continue
                if existing is not None:
                    counters["duplicate_bar_records"] += 1
                    duplicates_by_forecast[forecast_id].add(opened)
                    if not _identical_bar(existing, bar):
                        del bars_by_forecast[forecast_id][opened]
                        conflicts_by_forecast[forecast_id].add(opened)
                        counters["conflicting_duplicate_bar_timestamps"] += 1
                    continue
                bars_by_forecast[forecast_id][opened] = bar
    return (
        forecast_info, bars_by_forecast, conflicts_by_forecast,
        duplicates_by_forecast, joinable_forecasts, forecast_exclusion_reasons,
    )


def _forecast_path_label(forecast: dict[str, Any], bars: dict[dt.datetime, dict[str, Any]],
                         duplicates: set[dt.datetime], conflicts: set[dt.datetime],
                         as_of: dt.datetime | None,
                         counters: collections.Counter[str]):
    usable, reason = _forecast_price(forecast)
    if reason:
        return None, reason
    baseline, snapshot = usable
    expected = _expected_bar_opens(snapshot)
    horizon_end = snapshot + dt.timedelta(hours=WINDOW_HOURS)
    elapsed_opens = [
        opened for opened in expected
        if as_of is not None and opened + dt.timedelta(hours=1) <= as_of
    ]
    present = {opened: bar for opened, bar in bars.items() if opened in set(elapsed_opens)}
    missing = [opened for opened in elapsed_opens if opened not in present]
    gaps = _gap_runs(missing)
    future_censored = len(expected) - len(elapsed_opens)
    complete = len(expected) == len(present) and not missing and future_censored == 0
    identity, _, _ = _forecast_identity(forecast)
    incomplete_reasons = []
    if future_censored:
        incomplete_reasons.append("future_hours_censored")
    if missing:
        incomplete_reasons.append("missing_elapsed_hours")
    if not present:
        incomplete_reasons.append("no_elapsed_valid_bars")
    if any(opened in expected for opened in conflicts):
        incomplete_reasons.append("conflicting_duplicate_bar_timestamps")
    summary = {
        "forecast_id": forecast.get("forecast_id"),
        "forecast_utc": forecast.get("snapshot_utc"),
        "exact_join_identity": {
            "chain": identity[0],
            "token": identity[1],
            "pool": identity[2],
        },
        "incomplete_reasons": incomplete_reasons,
        "detection_candle_excluded": True,
        "price_basis": "forecast feature_snapshot.price_usd and matching GeckoTerminal exact-token USD candles",
        "expected_full_hour_bars_in_24h_window": len(expected),
        "elapsed_full_hour_bars_at_analysis_as_of": len(elapsed_opens),
        "unique_valid_bars": len(present),
        "missing_elapsed_hours": len(missing),
        "gap_runs": gaps,
        "duplicate_bar_timestamps_excluded": sum(1 for opened in duplicates if opened in expected),
        "conflicting_duplicate_bar_timestamps_excluded": sum(
            1 for opened in conflicts if opened in expected
        ),
        "future_hours_censored": future_censored,
        "coverage_complete": complete,
        "coverage_censored": not complete,
        "targets": {},
        "hourly_adverse_movement": None,
        "target_and_adverse_same_bar_order_unknown": {},
    }
    counters["forecast_records_with_identity_and_price"] += 1
    counters["forecasts_with_complete_candle_window" if complete else "forecasts_with_censored_candle_window"] += 1
    if missing:
        counters["forecasts_with_missing_elapsed_hours"] += 1
        counters["total_missing_elapsed_hours"] += len(missing)
        counters["total_gap_runs"] += len(gaps)
    counters["total_future_censored_hours"] += future_censored

    if not present:
        counters["forecasts_without_elapsed_valid_bars"] += 1
        return summary, None
    ordered = [present[key] for key in sorted(present)]
    worst_low = min(bar["low"] for bar in ordered)
    summary["hourly_adverse_movement"] = {
        "worst_bar_low_return_pct": (worst_low / baseline - 1) * 100,
        "worst_bar_low_utc": min(ordered, key=lambda bar: bar["low"])["time_open"].isoformat().replace("+00:00", "Z"),
        "interpretation": "hourly candle low relative to frozen detection price; not exact intrahour path or executable return",
    }
    for target_pct in TARGETS_PCT:
        level = baseline * (1 + target_pct / 100)
        hit_bars = [bar for bar in ordered if bar["high"] >= level]
        ambiguous_bars = [bar for bar in hit_bars if bar["low"] < baseline]
        summary["targets"][f"+{target_pct}%"] = {
            "candle_level_presence": bool(hit_bars) if complete else None,
            "hit_candle_count": len(hit_bars),
            "eligible_forecast_denominator": 1 if complete else 0,
            "evaluated_valid_bars": len(ordered),
            "first_hit_candle_open_utc": (
                min(hit_bars, key=lambda bar: bar["time_open"])["time_open"].isoformat().replace("+00:00", "Z")
                if hit_bars else None
            ),
            "same_candle_low_below_detection_price_count": len(ambiguous_bars),
            "within_candle_order_unknown": bool(ambiguous_bars),
            "interpretation": (
                "high reached threshold in at least one full-window candle"
                if complete else "partial/censored window; no complete-window target label"
            ),
        }
        if complete:
            counters[f"complete_window_target_{target_pct}_denominator"] += 1
            if hit_bars:
                counters[f"complete_window_target_{target_pct}_hits"] += 1
        if ambiguous_bars:
            counters[f"target_{target_pct}_and_below_base_same_candle"] += len(ambiguous_bars)
    if complete:
        counters["eligible_complete_window_forecasts"] += 1
    return summary, None


def _make_report(forecasts, observation_records, as_of, bars_by_forecast,
                 conflicts_by_forecast, duplicates_by_forecast, forecast_info, joinable_forecasts,
                 forecast_exclusion_reasons,
                 source_files, snapshot_id, counters):
    labels = []
    excluded = collections.Counter()
    excluded_details = []
    incomplete = collections.Counter()
    for forecast_id, forecast in forecasts.items():
        if forecast_id not in forecast_info:
            reasons = forecast_exclusion_reasons.get(
                forecast_id, {"missing_or_ambiguous_forecast_identity"}
            )
            for reason in reasons:
                excluded[reason] += 1
            excluded_details.append({
                "forecast_id": forecast_id,
                "reasons": sorted(reasons),
            })
            continue
        if forecast_id not in joinable_forecasts:
            reasons = forecast_exclusion_reasons.get(
                forecast_id, {"no_eligible_observation_for_exact_identity"}
            )
            excluded["no_valid_joined_hourly_observations"] += 1
            for reason in reasons:
                excluded[reason] += 1
            excluded_details.append({
                "forecast_id": forecast_id,
                "reasons": ["no_valid_joined_hourly_observations", *sorted(reasons)],
            })
            continue
        _, reason = _forecast_price(forecast)
        if reason:
            excluded[reason] += 1
            excluded_details.append({
                "forecast_id": forecast_id,
                "reasons": [f"invalid_forecast_price:{reason}"],
            })
            continue
        label, reason = _forecast_path_label(
            forecast, bars_by_forecast.get(forecast_id, {}),
            duplicates_by_forecast.get(forecast_id, set()),
            conflicts_by_forecast.get(forecast_id, set()), as_of, counters,
        )
        if reason:
            excluded[reason] += 1
            excluded_details.append({
                "forecast_id": forecast_id,
                "reasons": [reason],
            })
            continue
        if label is not None:
            labels.append(label)
            for item in label["incomplete_reasons"]:
                incomplete[item] += 1
    for horizon in (WINDOW_HOURS,):
        for target in TARGETS_PCT:
            denominator = counters[f"complete_window_target_{target}_denominator"]
            hit_count = counters[f"complete_window_target_{target}_hits"]
            counters[f"complete_window_target_{target}_rate"] = (
                hit_count / denominator if denominator else None
            )
    return {
        "analysis_version": ANALYSIS_VERSION,
        "source_access": "read-only",
        "source_snapshot_id": snapshot_id,
        "analysis_as_of_utc": as_of.isoformat().replace("+00:00", "Z") if as_of else None,
        "source_files": source_files,
        "record_counts": {
            "forecast_records": counters["forecast_source_records"],
            "forecasts_loaded": len(forecasts),
            "observation_records": counters["observation_source_records"],
            "observation_records_declared": counters["observation_records_declared"],
            "observations_loaded": len(observation_records),
            "observation_files_missing": counters["observation_files_missing"],
            "forecasts_with_reliable_core_identity": len(forecast_info),
            "forecasts_joined_to_observations": len(joinable_forecasts),
            "forecast_path_labels_reported": len(labels),
        },
        "join_policy": {
            "required_identity": "Exact normalized chain + contract/token address + pool address; all three must exist on both records.",
            "additional_identity": "Quote asset, quote symbol, and pair orientation are compared whenever both forecast and observation have values; conflicts are rejected. Missing optional fields are not inferred.",
            "time_policy": "Use only hourly bars strictly after the forecast detection-overlapping candle and fully within the next 24 hours.",
            "price_policy": "Only positive, fresh forecast feature_snapshot.price_usd paired with the collector's GeckoTerminal exact-token USD source is eligible.",
            "source_observation_policy": "Deduplicate by forecast_id + hourly time_open; repeated polling of the same bar does not create another sample. Conflicting duplicate bar values are excluded.",
            "manifest_policy": "Only observation files named by collection manifests are read; records must match the manifest collection_run_id and declared record_count.",
        },
        "coverage_summary": {
            key: value for key, value in sorted(counters.items())
            if key.startswith(("forecasts_", "forecast_", "observations_", "observation_",
                               "unjoinable_", "identity_", "duplicate_", "conflicting_",
                               "bar_excluded:", "bars_outside_", "detection_overlapping_",
                               "forecasts_with_", "forecasts_without_", "total_", "eligible_",
                               "complete_window_", "target_"))
        },
        "candle_level_metrics": {
            "time_window_hours": WINDOW_HOURS,
            "window_anchor": "forecast snapshot_utc",
            "coverage_identity_policy": "A complete-window count requires an exact normalized chain + token address + pool address forecast/observation match.",
            "bar_resolution": "1h",
            "excluded_boundary_bars": "Detection-overlapping and horizon-overlapping candles are excluded; only fully contained hourly bars are evaluated.",
            "complete_coverage_denominator": counters["eligible_complete_window_forecasts"],
            "target_results": {
                f"+{target}%": {
                    "complete_coverage_forecast_denominator": counters[f"complete_window_target_{target}_denominator"],
                    "candle_level_presence_count": counters[f"complete_window_target_{target}_hits"],
                    "candle_level_presence_rate": counters[f"complete_window_target_{target}_rate"],
                    "same_bar_target_and_below_base_count": counters[f"target_{target}_and_below_base_same_candle"],
                    "interpretation": "A full-window hourly high reached the threshold; this is a candle-level presence label, not exact intrahour timing or an executable result.",
                }
                for target in TARGETS_PCT
            },
            "adverse_movement": {
                "eligible_complete_coverage_denominator": counters["eligible_complete_window_forecasts"],
                "measurement": "Minimum hourly low relative to frozen forecast price across complete 24-hour windows.",
                "unit": "percent mark movement",
                "limitations": "Hourly low does not give the path, exact timing, or executable return.",
            },
            "forecast_paths": labels,
        },
        "excluded_forecast_reasons": dict(sorted(excluded.items())),
        "excluded_forecast_details": excluded_details,
        "incomplete_forecast_reasons": dict(sorted(incomplete.items())),
        "unjoinable_observation_reasons": {
            key: value for key, value in sorted(counters.items())
            if key.startswith(("unjoinable_", "observation_missing_identity_field:",
                               "observations_unsupported_", "observations_without_",
                               "observation_forecast_optional_identity_conflict"))
        },
        "unjoinable_observation_record_count": sum(
            value for key, value in counters.items()
            if key.startswith("unjoinable_")
        ),
        "unsupported_metrics": {
            "intrahour_target_time_or_order": "Hourly high/low can show a candle-level presence, but cannot resolve event order within the bar; same-bar target and below-base movement is explicitly ambiguous.",
            "time_to_target": "The first target-bearing hourly candle can be identified, but the exact target-crossing time within that candle is not observable.",
            "exact_target_touch_rate": "Only complete hourly candle windows support candle-level presence; sampling does not establish exact intrahour touches, and partial/censored windows have no negative target label.",
            "executable_returns": "No fills, fees, slippage, order books, or quote execution evidence are present.",
            "full_universe_performance": "Observations cover only identities in the active watchlist and cannot represent the full forecast universe.",
        },
        "limitations": [
            "Results are hourly-candle resolution even when the collector polls more frequently.",
            "Censored windows and missing-hour gaps are not treated as target non-hits.",
            "Multiple forecasts for one market can share the same candle history; overlapping labels are correlated and are not independent samples.",
            "The collector adapters retain normalized bars, not raw provider response bodies or headers.",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Hourly candle outcome analysis",
        "",
        f"- Analysis version: `{report['analysis_version']}`",
        f"- Source snapshot: `{report['source_snapshot_id']}`",
        f"- Analysis as of: {report['analysis_as_of_utc'] or 'unknown'}",
        f"- Forecasts loaded: {report['record_counts']['forecasts_loaded']:,}",
        f"- Observation records loaded: {report['record_counts']['observations_loaded']:,}",
        f"- Forecasts joined to observations: {report['record_counts']['forecasts_joined_to_observations']:,}",
        "- Scope: exact-identity active-watchlist observations only; read-only; hourly resolution.",
        "- A complete-window count requires an exact normalized chain + token address + pool address match.",
        "",
        "## Complete 24-hour candle coverage",
        "",
        "Only complete, fully contained hourly candles after the detection-overlapping candle are used. Target presence is based on candle highs. Partial windows are censored, not target misses.",
        "",
        "| Target | Complete-window forecast denominator | Candle-level presence | Presence rate | Same candle also below base (order unknown) |",
        "|---:|---:|---:|---:|---:|",
    ]
    results = report["candle_level_metrics"]["target_results"]
    for target in ("+40%", "+60%", "+100%"):
        row = results[target]
        rate = "n/a" if row["candle_level_presence_rate"] is None else f"{100 * row['candle_level_presence_rate']:.2f}%"
        lines.append(
            f"| {target} | {row['complete_coverage_forecast_denominator']:,} "
            f"| {row['candle_level_presence_count']:,} | {rate} "
            f"| {row['same_bar_target_and_below_base_count']:,} |"
        )
    adverse = report["candle_level_metrics"]["adverse_movement"]
    lines.extend([
        "",
        f"Hourly-low adverse movement is measured only for {adverse['eligible_complete_coverage_denominator']:,} "
        "complete-coverage forecasts; per-forecast values are in `report.json`.",
        "",
        "## Joinability and coverage",
        "",
        f"Unjoinable observation records: {report['unjoinable_observation_record_count']:,}. "
        "Reasons and excluded forecasts are in `report.json`.",
        f"Excluded forecast reasons: `{json.dumps(report['excluded_forecast_reasons'], sort_keys=True)}`.",
        f"Incomplete joined-window reasons: `{json.dumps(report['incomplete_forecast_reasons'], sort_keys=True)}`.",
        "Per-forecast coverage includes expected bars, elapsed bars, missing hours, gap runs, duplicate timestamps, and future-censored hours.",
        "",
        "## Limits",
        "",
        "- These are hourly candle-level labels, not exact intrahour target times or ordering. If a candle high reaches a target while its low falls below the detection price, the order is unknown.",
        "- No executable fills, fees, slippage, or quote-adjusted returns are calculated.",
        "- Coverage is restricted to active-watchlist identities, not the full universe; overlapping forecasts may share bars and are correlated.",
        "- Frequent polling does not increase candle resolution, and repeated polls of one hourly candle are deduplicated.",
        "",
        "Source partition/file hashes and rejected or excluded-record counts are in `report.json` and `manifest.json`.",
        "",
    ])
    return "\n".join(lines)


def _ensure_disjoint_paths(memory_dir: Path, observation_root: Path, output_root: Path) -> None:
    output = output_root.resolve()
    protected = (memory_dir.resolve(), observation_root.resolve(), DEFAULT_EXPORTS.resolve(), DEFAULT_RUNS.resolve())
    if any(output == path or output in path.parents or path in output.parents for path in protected):
        raise AnalysisError("analysis output must be separate from source ledgers, observations, exports, and scan outputs")


def _existing_output(output_dir: Path, snapshot_id: str):
    if not output_dir.exists():
        return None
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise AnalysisError(f"candle analysis output is not a regular directory: {output_dir}")
    manifest_path = output_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"refusing to overwrite an unmanaged report: {output_dir}") from exc
    if (not isinstance(manifest, dict) or manifest.get("managed_by") != MANAGED_BY
            or manifest.get("analysis_version") != ANALYSIS_VERSION
            or manifest.get("source_snapshot_id") != snapshot_id):
        raise AnalysisError(f"refusing to overwrite a different report: {output_dir}")
    expected = manifest.get("output_files_sha256")
    if not isinstance(expected, dict) or set(expected) != {"report.json", "report.md"}:
        raise AnalysisError(f"report manifest is incomplete: {manifest_path}")
    for name, checksum in expected.items():
        path = output_dir / name
        if path.is_symlink() or not path.is_file() or _sha256_file(path) != checksum:
            raise AnalysisError(f"report failed checksum validation: {name}")
    return output_dir


def analyze_candles(
    memory_dir: Path = DEFAULT_MEMORY,
    observation_root: Path = DEFAULT_OBSERVATIONS,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> Path:
    memory_dir, observation_root, output_root = map(Path, (memory_dir, observation_root, output_root))
    _ensure_disjoint_paths(memory_dir, observation_root, output_root)
    (forecast_files, manifest_files, observation_files, manifest_by_observation,
     stats, source_files, snapshot_id) = _source_snapshot(
        memory_dir, observation_root
    )
    output_dir = output_root / f"candle-outcomes-v4-{snapshot_id[:24]}"
    existing = _existing_output(output_dir, snapshot_id)
    if existing is not None:
        return existing
    counters: collections.Counter[str] = collections.Counter()
    forecasts = _read_forecasts(forecast_files, counters)
    observation_records, as_of = _read_observations(
        observation_files, counters, manifest_by_observation
    )
    (forecast_info, bars, conflicts, duplicates, joinable,
     forecast_exclusion_reasons) = _make_forecast_observations(
        forecasts, observation_records, as_of, counters
    )
    report = _make_report(
        forecasts, observation_records, as_of, bars, conflicts, duplicates,
        forecast_info, joinable, forecast_exclusion_reasons,
        source_files, snapshot_id, counters,
    )
    output_root.mkdir(parents=True, exist_ok=True)
    if output_root.is_symlink():
        raise AnalysisError(f"output root must not be a symlink: {output_root}")
    staging = Path(tempfile.mkdtemp(prefix=".candle-outcomes-", dir=output_root))
    try:
        _write_json(staging / "report.json", report)
        (staging / "report.md").write_text(render_markdown(report), encoding="utf-8")
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
        _verify_source_snapshot(
            memory_dir, observation_root, forecast_files, manifest_files, observation_files, stats
        )
        if output_dir.exists():
            raise AnalysisError(f"report appeared during analysis: {output_dir}")
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
    parser.add_argument("--observation-root", type=Path, default=DEFAULT_OBSERVATIONS)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        output_dir = analyze_candles(args.memory_dir, args.observation_root, args.output_root)
    except AnalysisError as exc:
        print(f"Candle outcome analysis failed: {exc}", file=sys.stderr)
        return 2
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
