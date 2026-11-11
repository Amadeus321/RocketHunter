#!/usr/bin/env python3
"""Export existing forecast and outcome ledgers to versioned Parquet snapshots."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import os
import shutil
import sys
import tempfile
import zlib
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEMORY = ROOT / "rocket-hunter-data" / "scanner-memory"
DEFAULT_EXPORTS = ROOT / "rocket-hunter-data" / "research-exports"
DEFAULT_RUNS = ROOT / "rocket-runs"
EXPORT_SCHEMA_VERSION = "crypto-prebreakout.research-export.v1"
MANAGED_BY = "rocket-hunter-export-research-data"
BATCH_SIZE = 5000

IDENTITY_FIELDS = (
    "identity", "market_key", "chain", "market_type", "contract_address", "token_mint",
    "pool_address", "pair_address", "venue", "base_asset", "quote_asset", "quote_symbol",
    "pair_orientation", "pair", "asset", "symbol",
)
LOCAL_CONFIGURATION_FIELDS = (
    "max_runtime_minutes", "gecko_pages", "network_sweep_limit", "scanner_workers",
    "preflight_workers", "birdeye", "gecko_request_interval_seconds", "retries",
    "dex_search_limit", "dex_lookup_limit",
)


class ExportError(RuntimeError):
    """Raised when source data cannot be exported as a consistent snapshot."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc_iso(value: dt.datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _reject_json_constant(value: str):
    raise ValueError(f"non-standard JSON constant: {value}")


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid_numeric_field")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("invalid_numeric_field")
    return number


def _optional_horizon(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("invalid_horizon_hours")
    return value


def _arrow_schema(pa):
    fields = [
        ("schema_version", pa.string()),
        ("record_type", pa.string()),
        ("record_id", pa.string()),
        ("forecast_id", pa.string()),
        ("source_schema", pa.string()),
        ("source_partition", pa.string()),
        ("run_id", pa.string()),
        ("market_key", pa.string()),
        ("identity", pa.string()),
        ("chain", pa.string()),
        ("market_type", pa.string()),
        ("contract_address", pa.string()),
        ("token_mint", pa.string()),
        ("pool_address", pa.string()),
        ("pair_address", pa.string()),
        ("venue", pa.string()),
        ("base_asset", pa.string()),
        ("quote_asset", pa.string()),
        ("quote_symbol", pa.string()),
        ("pair_orientation", pa.string()),
        ("pair", pa.string()),
        ("asset", pa.string()),
        ("symbol", pa.string()),
        ("source", pa.string()),
        ("source_url", pa.string()),
        ("source_retrieved_utc", pa.string()),
        ("snapshot_utc", pa.string()),
        ("forecast_utc", pa.string()),
        ("observed_utc", pa.string()),
        ("ingested_utc", pa.string()),
        ("horizon_hours", pa.int32()),
        ("status", pa.string()),
        ("raw_score", pa.float64()),
        ("price_usd", pa.float64()),
        ("base_price_usd", pa.float64()),
        ("observed_price_usd", pa.float64()),
        ("mark_return_pct", pa.float64()),
        ("scanner_version", pa.string()),
        ("pipeline_version", pa.string()),
        ("core_runtime_version", pa.string()),
        ("collector_overlay_version", pa.string()),
        ("configuration_fingerprint", pa.string()),
        ("local_configuration_json", pa.string()),
        ("identity_source", pa.string()),
        ("feature_snapshot_json", pa.string()),
        ("source_record_json", pa.string()),
    ]
    return pa.schema(fields, metadata={b"schema_version": EXPORT_SCHEMA_VERSION.encode("ascii")})


def _load_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ExportError(
            "Parquet export requires PyArrow. Install it with: "
            "python -m pip install -r requirements-export.txt"
        ) from exc
    return pa, pq


def _source_partitions(memory_dir: Path, kind: str) -> list[Path]:
    directory = memory_dir / kind
    if not directory.is_dir():
        raise ExportError(f"required source directory is missing: {directory}")
    paths = sorted(directory.glob("*.jsonl.gz"), key=lambda path: path.name)
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ExportError(f"source partitions must be regular files: {directory}")
    return paths


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_snapshot(memory_dir: Path) -> tuple[dict[str, list[Path]], dict[Path, os.stat_result], list[dict[str, Any]], str]:
    source_files = {kind: _source_partitions(memory_dir, kind) for kind in ("forecasts", "outcomes")}
    source_stats = {path: path.stat() for paths in source_files.values() for path in paths}
    descriptions = []
    digest = hashlib.sha256()
    for kind, paths in source_files.items():
        for path in paths:
            file_hash = _sha256_file(path)
            entry = {
                "dataset": kind,
                "partition": path.name,
                "compressed_size_bytes": source_stats[path].st_size,
                "source_sha256": file_hash,
            }
            descriptions.append(entry)
            digest.update(_json_text(entry).encode("utf-8"))
    return source_files, source_stats, descriptions, digest.hexdigest()


def _verify_source_snapshot(
    memory_dir: Path,
    source_files: dict[str, list[Path]],
    source_stats: dict[Path, os.stat_result],
) -> None:
    for kind, expected_paths in source_files.items():
        if _source_partitions(memory_dir, kind) != expected_paths:
            raise ExportError(f"source partition list changed during export: {kind}")
    for path, before in source_stats.items():
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise ExportError(f"source partition changed during export: {path.name}")


def _load_run_metadata(run_root: Path) -> tuple[dict[str, dict[str, Any]], int]:
    records: dict[str, dict[str, Any]] = {}
    errors = 0
    for path in sorted(run_root.glob("*/scan-status.json")):
        try:
            status = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            errors += 1
            continue
        if not isinstance(status, dict):
            errors += 1
            continue
        run_id = _optional_text(status.get("run_id"))
        if not run_id:
            continue
        records[run_id] = {
            key: status[key]
            for key in ("scanner_version", "configuration_fingerprint", "local_configuration")
            if key in status
        }
    return records, errors


def _record_identity(record: dict[str, Any]) -> dict[str, str | None]:
    features = record.get("feature_snapshot")
    if not isinstance(features, dict):
        features = {}
    values = {}
    for field in IDENTITY_FIELDS:
        value = record.get(field)
        if value is None:
            value = features.get(field)
        values[field] = _optional_text(value)
    return values


def _make_export_row(
    record_type: str,
    record: dict[str, Any],
    source_partition: str,
    run_metadata: dict[str, dict[str, Any]],
    forecast_identities: dict[str, dict[str, str | None]],
) -> dict[str, Any]:
    is_forecast = record_type == "forecast"
    forecast_id = _optional_text(record.get("forecast_id"))
    if not forecast_id:
        raise ValueError("missing_forecast_id")
    horizon = None if is_forecast else _optional_horizon(record.get("horizon_hours"))
    record_id = forecast_id if is_forecast else f"{forecast_id}:{horizon}"

    identity = _record_identity(record)
    identity_source = "record" if any(identity.values()) else "missing"
    if not is_forecast:
        linked_identity = forecast_identities.get(forecast_id)
        if linked_identity:
            for field, value in linked_identity.items():
                if identity[field] is None:
                    identity[field] = value
            if any(identity.values()):
                identity_source = "linked_forecast" if identity_source == "missing" else "record_and_linked_forecast"

    features = record.get("feature_snapshot") if is_forecast else None
    if not isinstance(features, dict):
        features = None
    run_id = _optional_text(record.get("run_id"))
    metadata = run_metadata.get(run_id or "", {})
    configuration = record.get("local_configuration")
    if configuration is None:
        configuration = metadata.get("local_configuration")
    if isinstance(configuration, dict):
        configuration = {key: configuration[key] for key in LOCAL_CONFIGURATION_FIELDS
                          if key in configuration}
    else:
        configuration = None

    row: dict[str, Any] = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "record_type": record_type,
        "record_id": record_id,
        "forecast_id": forecast_id,
        "source_schema": _optional_text(record.get("schema")),
        "source_partition": source_partition,
        "run_id": run_id,
        **identity,
        "source": _optional_text(record.get("source")) or (
            _optional_text(features.get("source")) if features else None
        ),
        "source_url": _optional_text(record.get("source_url")) or (
            _optional_text(features.get("source_url")) if features else None
        ),
        "source_retrieved_utc": (
            _optional_text(record.get("retrieved_utc"))
            or (_optional_text(features.get("retrieved_utc")) if features else None)
        ),
        "snapshot_utc": _optional_text(record.get("snapshot_utc")),
        "forecast_utc": _optional_text(record.get("forecast_utc")),
        "observed_utc": _optional_text(record.get("observed_utc")),
        "ingested_utc": _optional_text(record.get("ingested_utc")),
        "horizon_hours": horizon,
        "status": _optional_text(record.get("status")),
        "raw_score": _optional_number(record.get("raw_score")),
        "price_usd": _optional_number(features.get("price_usd")) if features else None,
        "base_price_usd": _optional_number(record.get("base_price_usd")),
        "observed_price_usd": _optional_number(record.get("observed_price_usd")),
        "mark_return_pct": _optional_number(record.get("mark_return_pct")),
        "scanner_version": (
            _optional_text(record.get("scanner_version"))
            or _optional_text(metadata.get("scanner_version"))
        ),
        "pipeline_version": _optional_text(record.get("pipeline_version")),
        "core_runtime_version": _optional_text(record.get("core_runtime_version")),
        "collector_overlay_version": _optional_text(record.get("collector_overlay_version")),
        "configuration_fingerprint": (
            _optional_text(record.get("configuration_fingerprint"))
            or _optional_text(metadata.get("configuration_fingerprint"))
        ),
        "local_configuration_json": _json_text(configuration) if configuration is not None else None,
        "identity_source": identity_source,
        "feature_snapshot_json": _json_text(features) if features is not None else None,
        "source_record_json": _json_text(record),
    }
    return row


def _schema_document() -> dict[str, Any]:
    path = ROOT / "schemas" / "research-export-v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_snapshot_id(snapshot_id: str, output_format: str) -> str:
    return f"research-{snapshot_id[:24]}-{output_format}"


def _ensure_disjoint_paths(memory_dir: Path, export_root: Path, run_root: Path) -> None:
    memory_dir, export_root, run_root = memory_dir.resolve(), export_root.resolve(), run_root.resolve()
    if export_root == memory_dir or memory_dir in export_root.parents or export_root in memory_dir.parents:
        raise ExportError("export destination and scanner memory must be separate, non-nested directories")
    if export_root == run_root or run_root in export_root.parents or export_root in run_root.parents:
        raise ExportError("export destination must be separate from the local run output tree")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n",
                    encoding="utf-8")


def _existing_export(output_dir: Path, snapshot_id: str, output_format: str) -> Path | None:
    if not output_dir.exists():
        return None
    if not output_dir.is_dir():
        raise ExportError(f"export path exists and is not a directory: {output_dir}")
    manifest_path = output_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ExportError(f"refusing to overwrite an existing export without a manifest: {output_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"cannot read existing export manifest: {manifest_path}") from exc
    if (isinstance(manifest, dict)
            and manifest.get("managed_by") == MANAGED_BY
            and manifest.get("export_schema_version") == EXPORT_SCHEMA_VERSION
            and manifest.get("source_snapshot_id") == snapshot_id
            and manifest.get("format") == output_format
            and manifest.get("status") == "COMPLETE"):
        expected_hashes = manifest.get("output_files_sha256")
        if not isinstance(expected_hashes, dict) or not expected_hashes:
            raise ExportError(f"existing export has no output checksums: {manifest_path}")
        for relative_path, expected_hash in expected_hashes.items():
            if not isinstance(relative_path, str):
                raise ExportError(f"existing export has an invalid output path: {manifest_path}")
            relative = Path(relative_path)
            path = output_dir / relative
            if (relative.is_absolute() or ".." in relative.parts
                    or path.is_symlink() or output_dir.resolve() not in path.resolve().parents
                    or not path.is_file() or not isinstance(expected_hash, str)
                    or _sha256_file(path) != expected_hash):
                raise ExportError(f"existing export file failed checksum validation: {relative_path}")
        return output_dir
    raise ExportError(f"refusing to overwrite a different or incomplete export: {output_dir}")


def _partition_date(path: Path) -> str:
    try:
        partition = path.name[:-len(".jsonl.gz")] if path.name.endswith(".jsonl.gz") else path.stem
        dt.datetime.strptime(partition, "%Y-%m-%d")
        return partition
    except ValueError:
        return "unknown"


def _append_rejection(
    handle,
    totals: dict[str, int],
    reasons: Counter[str],
    dataset: str,
    source_partition: str,
    line_number: int | None,
    reason: str,
) -> None:
    totals["rejected_records"] += 1
    reasons[reason] += 1
    handle.write(_json_text({
        "dataset": dataset,
        "source_partition": source_partition,
        "line_number": line_number,
        "reason": reason,
    }) + "\n")


def _write_partition(
    pa,
    pq,
    schema,
    output_format: str,
    dataset: str,
    record_type: str,
    source_path: Path,
    destination: Path,
    run_metadata: dict[str, dict[str, Any]],
    forecast_identities: dict[str, dict[str, str | None]],
    seen_record_ids: set[str],
    rejection_handle,
    reason_counts: Counter[str],
) -> dict[str, int]:
    counts = {"source_records": 0, "exported_records": 0, "rejected_records": 0}
    date = _partition_date(source_path)
    temp = destination.with_name(destination.name + ".tmp")
    destination.parent.mkdir(parents=True, exist_ok=True)
    batch: list[dict[str, Any]] = []

    def consume(write_batch) -> None:
        try:
            source = gzip.open(source_path, "rt", encoding="utf-8", newline="")
        except (OSError, EOFError, UnicodeDecodeError, zlib.error):
            _append_rejection(rejection_handle, counts, reason_counts, dataset,
                              source_path.name, None, "malformed_gzip_partition")
            return

        with source:
            line_number = 0
            while True:
                try:
                    line = source.readline()
                except (OSError, EOFError, UnicodeDecodeError, zlib.error):
                    _append_rejection(rejection_handle, counts, reason_counts, dataset,
                                      source_path.name, None, "malformed_gzip_partition")
                    break
                if not line:
                    break
                line_number += 1
                if not line.strip():
                    continue
                counts["source_records"] += 1
                try:
                    record = json.loads(line, parse_constant=_reject_json_constant)
                except (json.JSONDecodeError, ValueError):
                    _append_rejection(rejection_handle, counts, reason_counts, dataset,
                                      source_path.name, line_number, "malformed_json")
                    continue
                if not isinstance(record, dict):
                    _append_rejection(rejection_handle, counts, reason_counts, dataset,
                                      source_path.name, line_number, "non_object_record")
                    continue
                try:
                    row = _make_export_row(
                        record_type, record, source_path.name, run_metadata, forecast_identities
                    )
                except ValueError as exc:
                    _append_rejection(rejection_handle, counts, reason_counts, dataset,
                                      source_path.name, line_number, str(exc))
                    continue
                record_id = row["record_id"]
                if record_id in seen_record_ids:
                    _append_rejection(rejection_handle, counts, reason_counts, dataset,
                                      source_path.name, line_number, "duplicate_record_id")
                    continue
                seen_record_ids.add(record_id)
                if record_type == "forecast":
                    forecast_identities[row["forecast_id"]] = {
                        field: row[field] for field in IDENTITY_FIELDS
                    }
                batch.append(row)
                counts["exported_records"] += 1
                if len(batch) >= BATCH_SIZE:
                    write_batch(batch)
                    batch.clear()
        if batch:
            write_batch(batch)
            batch.clear()

    try:
        if output_format == "parquet":
            with pq.ParquetWriter(temp, schema, compression="zstd") as writer:
                consume(lambda rows: writer.write_table(pa.Table.from_pylist(rows, schema=schema)))
                if counts["exported_records"] == 0:
                    writer.write_table(pa.Table.from_pylist([], schema=schema))
        else:
            with temp.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as compressed:
                    with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text:
                        writer = csv.DictWriter(text, fieldnames=list(schema), lineterminator="\n")
                        writer.writeheader()
                        consume(writer.writerows)
        temp.replace(destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return counts


def _write_dataset(
    pa,
    pq,
    schema,
    output_format: str,
    dataset: str,
    partitions: list[Path],
    staging: Path,
    run_metadata: dict[str, dict[str, Any]],
    forecast_identities: dict[str, dict[str, str | None]],
    rejection_handle,
    reason_counts: Counter[str],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    record_type = "forecast" if dataset == "forecasts" else "outcome"
    seen_record_ids: set[str] = set()
    totals = {"source_records": 0, "exported_records": 0, "rejected_records": 0}
    partition_details = []
    for partition_index, source_path in enumerate(partitions):
        date = _partition_date(source_path)
        suffix = "parquet" if output_format == "parquet" else "csv.gz"
        destination = (
            staging / f"record_type={record_type}" / f"partition_date={date}"
            / f"part-{partition_index:05d}.{suffix}"
        )
        counts = _write_partition(
            pa, pq, schema, output_format, dataset, record_type, source_path, destination, run_metadata,
            forecast_identities, seen_record_ids, rejection_handle, reason_counts,
        )
        for key in totals:
            totals[key] += counts[key]
        partition_details.append({
            "source_partition": source_path.name,
            "partition_date": date,
            "export_file": destination.relative_to(staging).as_posix(),
            **counts,
        })
    return totals, partition_details


def _sha256_tree(root: Path, files: list[Path]) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): _sha256_file(path) for path in sorted(files)}


def export_ledgers(
    memory_dir: Path = DEFAULT_MEMORY,
    output_dir: Path | None = None,
    export_root: Path = DEFAULT_EXPORTS,
    run_root: Path = DEFAULT_RUNS,
    output_format: str = "csv",
) -> Path:
    """Create an immutable CSV snapshot; Parquet is available as an optional format."""
    if output_format not in ("csv", "parquet"):
        raise ExportError(f"unsupported export format: {output_format}")
    pa = pq = None
    if output_format == "parquet":
        pa, pq = _load_pyarrow()

    memory_dir = Path(memory_dir).resolve()
    export_root = Path(export_root).resolve()
    run_root = Path(run_root).resolve()
    if not memory_dir.is_dir():
        raise ExportError(f"scanner memory directory does not exist: {memory_dir}")
    _ensure_disjoint_paths(memory_dir, export_root, run_root)

    run_metadata, run_metadata_errors = _load_run_metadata(run_root)
    source_files, source_stats, source_descriptions, ledger_snapshot_id = _source_snapshot(memory_dir)
    provenance_hash = hashlib.sha256(_json_text({
        "run_metadata": run_metadata,
        "run_metadata_read_errors": run_metadata_errors,
    }).encode("utf-8")).hexdigest()
    snapshot_id = hashlib.sha256(
        f"{ledger_snapshot_id}|{provenance_hash}".encode("ascii")
    ).hexdigest()
    _verify_source_snapshot(memory_dir, source_files, source_stats)
    if output_dir is None:
        output_dir = export_root / _safe_snapshot_id(snapshot_id, output_format)
    else:
        output_dir = Path(output_dir).resolve()
        if output_dir == export_root or export_root not in output_dir.parents:
            raise ExportError("explicit output directory must be inside --export-root")
    _ensure_disjoint_paths(memory_dir, output_dir, run_root)
    existing = _existing_export(output_dir, snapshot_id, output_format)
    if existing is not None:
        return existing

    schema = _arrow_schema(pa) if output_format == "parquet" else list(_schema_document()["properties"])
    export_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="." + output_dir.name + ".building-", dir=export_root))
    reason_counts: Counter[str] = Counter()
    forecast_identities: dict[str, dict[str, str | None]] = {}
    validation_totals: dict[str, dict[str, int]] = {}
    datasets = {}
    rejection_path = staging / "validation-rejections.jsonl"
    try:
        with rejection_path.open("w", encoding="utf-8", newline="\n") as rejection_file:
            for dataset in ("forecasts", "outcomes"):
                totals, partitions = _write_dataset(
                    pa, pq, schema, output_format, dataset, source_files[dataset], staging, run_metadata,
                    forecast_identities, rejection_file, reason_counts,
                )
                validation_totals[dataset] = totals
                datasets[dataset] = {"record_counts": totals, "partitions": partitions}
            rejection_file.flush()
            os.fsync(rejection_file.fileno())

        _verify_source_snapshot(memory_dir, source_files, source_stats)
        now = _utc_iso(_utc_now())
        schema_document = _schema_document()
        _write_json(staging / "schema.json", schema_document)
        summary = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "exported_at_utc": now,
            "source_access": "read-only",
            "source_snapshot_id": snapshot_id,
            "run_metadata_sha256": provenance_hash,
            "record_counts": validation_totals,
            "rejected_records": sum(values["rejected_records"] for values in validation_totals.values()),
            "rejections_by_reason": dict(sorted(reason_counts.items())),
            "run_metadata_read_errors": run_metadata_errors,
            "outcome_values_are_executable_fills": False,
            "partitions": [
                {"dataset": dataset, **partition}
                for dataset, details in datasets.items()
                for partition in details["partitions"]
            ],
        }
        _write_json(staging / "validation-summary.json", summary)
        file_paths = [path for path in staging.rglob("*") if path.is_file()]
        output_hashes = _sha256_tree(staging, file_paths)
        manifest = {
            "managed_by": MANAGED_BY,
            "status": "COMPLETE",
            "format": output_format,
            "export_schema_version": EXPORT_SCHEMA_VERSION,
            "source_snapshot_id": snapshot_id,
            "source": "scanner-memory/forecasts and scanner-memory/outcomes",
            "source_access": "read-only",
            "source_memory_path": str(memory_dir),
            "source_snapshot_verified_utc": now,
            "source_partitions": source_descriptions,
            "datasets": datasets,
            "output_files_sha256": output_hashes,
            "validation_summary": "validation-summary.json",
            "validation_rejections": "validation-rejections.jsonl",
            "schema_file": "schema.json",
            "research_limits": [
                "Forecast features and outcomes are exported as recorded; no unavailable fields are inferred.",
                "Outcome marks are not executable fills, and this export makes no claim about fees or slippage.",
                "Missing values remain null; an absent ingestion timestamp or configuration fingerprint is not inferred.",
            ],
        }
        _write_json(staging / "manifest.json", manifest)
        _verify_source_snapshot(memory_dir, source_files, source_stats)
        if output_dir.exists():
            raise ExportError(f"refusing to overwrite an existing export: {output_dir}")
        staging.replace(output_dir)
        return output_dir
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY,
                        help="existing authoritative scanner-memory directory (read-only)")
    parser.add_argument("--export-root", type=Path, default=DEFAULT_EXPORTS,
                        help="separate root for immutable research export snapshots")
    parser.add_argument("--outdir", type=Path,
                        help="new snapshot directory inside --export-root; defaults to a content-addressed name")
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUNS,
                        help="local scan statuses used only for matching recorded run provenance")
    parser.add_argument("--format", choices=("csv", "parquet"), default="csv",
                        help="output format (default: CSV); Parquet requires optional PyArrow")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        result = export_ledgers(args.memory_dir, args.outdir, args.export_root, args.run_root, args.format)
    except (ExportError, OSError, ValueError) as exc:
        print(f"Research export failed: {exc}", file=sys.stderr)
        return 2
    summary = json.loads((result / "validation-summary.json").read_text(encoding="utf-8"))
    print(f"Created or reused read-only-source {args.format} export: {result}")
    for dataset, counts in summary["record_counts"].items():
        print(
            f"  {dataset}: {counts['exported_records']} exported / "
            f"{counts['source_records']} source / {counts['rejected_records']} rejected"
        )
    if summary["rejections_by_reason"]:
        print("  rejection reasons: " + json.dumps(summary["rejections_by_reason"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
