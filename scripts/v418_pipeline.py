#!/usr/bin/env python3
"""v4.18 quality controls and persistent, hindsight-safe research ledger.

This module wraps the existing read-only v4.15.3 market collector. It never
places orders, signs transactions, or turns missing evidence into a pass.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

PIPELINE_VERSION = "4.18.1"
CORE_RUNTIME_VERSION = "4.15.3"
EARLY_BREAKOUT_SIGNAL_VERSION = "v4.25-early-breakout.v1"
EARLY_BREAKOUT_MODEL_ID = "v4.25-early-breakout"
EARLY_BREAKOUT_COMPONENT_LIMITS = {
    "5m_impulse": 10.0,
    "1h_momentum": 10.0,
    "6h_trend": 6.0,
    "24h_context": 4.0,
    "momentum_acceleration": 10.0,
    "transaction_activity": 8.0,
    "volume_turnover": 8.0,
    "liquidity_depth": 10.0,
    "buy_pressure": 8.0,
    "intraday_volume_acceleration": 8.0,
    "pool_novelty": 6.0,
    "multi_window_alignment": 5.0,
}
HORIZONS_HOURS = (1, 4, 6, 24, 72)
ROW_LISTS = (
    "top_24h_gainers", "top_24h_losers", "top_24h_movers", "top_6h_movers",
    "top_1h_movers", "new_pools_under_24h", "new_launch_movers",
    "extreme_movers_1000pct_plus", "extreme_movers_400pct_plus",
    "extreme_movers_100pct_plus", "high_risk_moonshot_scouts",
    "most_actionable_liquidity_screen", "execution_candidate_shortlist",
    "dex_discovery_leads", "web3_trending_leads", "reported_top_24h_gainers_raw",
    "reported_top_24h_losers_raw", "reported_top_24h_movers_raw", "new_launch_discovery_board_v418",
)


def utc_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def parse_created_at_ms(row: dict[str, Any]) -> datetime | None:
    for key in ("pair_created_at_ms", "pool_created_at_ms", "created_at_ms"):
        value = finite_number(row.get(key))
        if value is not None and value > 0:
            try:
                return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                pass
    return None


def asset_age_hours(row: dict[str, Any], as_of: datetime) -> float | None:
    age = finite_number(row.get("age_hours"))
    if age is not None and age >= 0:
        return age
    created = utc_dt(row.get("pool_created_at")) or parse_created_at_ms(row)
    if created is not None:
        return max(0.0, (as_of - created).total_seconds() / 3600.0)
    flags = row.get("risk_flags") or []
    if isinstance(flags, list) and any("NEW_POOL_UNDER_24H" in str(flag) for flag in flags):
        return None
    return None


def is_dex_pool(row: dict[str, Any]) -> bool:
    market_type = str(row.get("market_type") or "").lower()
    return market_type in {"dex_pool", "dex", "amm", "clmm", "pool"} or bool(row.get("pool_address") or row.get("gecko_pool_id"))


def _canonical_address(chain: str, address: Any) -> str:
    """Canonicalize EVM hex addresses; preserve case-sensitive chain addresses."""
    value = str(address or "").strip()
    chain_key = str(chain or "").strip().lower()
    evm_chains = {
        "ethereum", "eth", "base", "bsc", "bnb", "polygon", "polygon_pos",
        "arbitrum", "optimism", "avalanche", "avax", "fantom", "ftm",
        "linea", "scroll", "zksync", "mantle", "blast", "mode", "opbnb",
    }
    if chain_key in evm_chains and re.fullmatch(r"0x[0-9a-fA-F]+", value):
        return value.lower()
    return value


def market_key(row: dict[str, Any]) -> str | None:
    """Version-2 stable DEX identity: chain + token/mint + pool/pair."""
    market_type = str(row.get("market_type") or row.get("type") or "unknown").strip().lower()
    is_dex = is_dex_pool(row) or market_type in {"dex_token", "dex_pool"}
    if is_dex:
        identity = str(row.get("identity") or "").strip()
        chain = str(row.get("chain") or "").strip().lower()
        if not chain and identity.lower().startswith("dex:"):
            parts = identity.split(":", 2)
            if len(parts) == 3:
                chain = parts[1].strip().lower()
        contract_raw = row.get("contract_address") or row.get("address")
        pool_raw = row.get("pool_address") or row.get("pair_address") or row.get("gecko_pool_id")
        contract = _canonical_address(chain, contract_raw) if contract_raw else ""
        pool = _canonical_address(chain, pool_raw) if pool_raw else ""
        if not chain or not contract or not pool:
            return None
        return f"dex|{chain}|token:{contract}|pool:{pool}"

    # Preserve the prior CEX/catalogue behavior.
    identity = str(row.get("identity") or "").strip().lower()
    provider_or_venue = str(row.get("venue") or (
        row.get("source") if market_type in {"cex_spot", "cex_perp", "spot", "perpetual"} else ""
    ) or "").strip().lower()
    quote = str(row.get("quote_symbol") or "").strip().lower()
    if identity and (row.get("contract_address") or row.get("address") or provider_or_venue):
        return "|".join((identity, "", provider_or_venue, quote))
    chain = str(row.get("chain") or "").strip().lower()
    contract = str(row.get("contract_address") or row.get("address") or "").strip().lower()
    if chain and contract:
        return "|".join((chain, contract, "", provider_or_venue, quote, market_type))
    catalogue_id = str(row.get("id") or "").strip().lower()
    source = str(row.get("source") or "").strip().lower()
    if catalogue_id and source:
        return f"catalogue|{source}|{catalogue_id}"
    return None

def add_flag(row: dict[str, Any], flag: str) -> None:
    flags = row.get("risk_flags")
    if not isinstance(flags, list):
        flags = []
    if flag not in flags:
        flags.append(flag)
    row["risk_flags"] = flags


def age_classify(row: dict[str, Any], as_of: datetime) -> str:
    if not is_dex_pool(row):
        return "EXCHANGE_OR_AGGREGATOR_24H_FIELD" if row.get("change_24h_pct") is not None else "NO_24H_RETURN_FIELD"
    age = asset_age_hours(row, as_of)
    if age is None:
        row["age_hours_v418"] = None
        row["return_window_status"] = "UNKNOWN_POOL_AGE_NOT_COMPARABLE"
        if row.get("change_24h_pct") is not None:
            row["source_reported_change_24h_pct"] = row.get("change_24h_pct")
            row["change_24h_pct"] = None
        add_flag(row, "POOL_AGE_UNKNOWN_24H_RETURN_NOT_COMPARABLE")
        return "UNKNOWN_POOL_AGE_NOT_COMPARABLE"
    row["age_hours_v418"] = round(age, 4)
    if age < 24.0:
        reported = row.get("change_24h_pct")
        if reported is not None:
            row["source_reported_change_24h_pct"] = reported
            row["source_reported_change_during_available_pool_history_pct"] = reported
            row["change_24h_pct"] = None
        row["return_window_status"] = "INSUFFICIENT_24H_HISTORY"
        add_flag(row, "INSUFFICIENT_24H_HISTORY_REPORTED_MOVE_IS_SINCE_CREATION_OR_SOURCE_WINDOW")
        return "INSUFFICIENT_24H_HISTORY"
    row["return_window_status"] = "SOURCE_REPORTED_24H_POOL_AGE_GE_24H"
    return "SOURCE_REPORTED_24H_POOL_AGE_GE_24H"


def log_bucket(value: float, granularity: float = 0.01) -> int:
    return int(round(math.log(value) / granularity))


def fingerprint_clusters(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag repeated near-identical price/liquidity marks without deleting leads."""
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not is_dex_pool(row):
            continue
        price = finite_number(row.get("price_usd"))
        liquidity = finite_number(row.get("liquidity_usd"))
        chain = str(row.get("chain") or "").lower()
        source = str(row.get("source") or "").lower()
        key = market_key(row)
        if price is None or liquidity is None or price <= 0 or liquidity <= 0 or not key:
            continue
        # Buckets represent approximately 1% relative bands in log space.
        groups[(chain, source, log_bucket(price), log_bucket(liquidity))].append(row)
    clusters = []
    for key, members in groups.items():
        unique = {market_key(row): row for row in members}
        if len(unique) < 3:
            continue
        cluster_id = hashlib.sha256("|".join(map(str, key)).encode()).hexdigest()[:16]
        for row in unique.values():
            row["fingerprint_cluster_id"] = cluster_id
            row["fingerprint_cluster_size"] = len(unique)
            gate = row.get("quality_gate") if isinstance(row.get("quality_gate"), dict) else {}
            reasons = gate.get("reasons") if isinstance(gate.get("reasons"), list) else []
            if "REPEATED_NEAR_IDENTICAL_PRICE_LIQUIDITY_FINGERPRINT" not in reasons:
                reasons = list(reasons) + ["REPEATED_NEAR_IDENTICAL_PRICE_LIQUIDITY_FINGERPRINT"]
            row["quality_gate"] = {**gate, "status": "QUARANTINED_FROM_COMPARABLE_RETURN_RANKING", "execution_ready": False, "reasons": reasons}
            add_flag(row, "REPEATED_NEAR_IDENTICAL_PRICE_LIQUIDITY_FINGERPRINT")
        clusters.append({
            "cluster_id": cluster_id, "chain": key[0], "source": key[1],
            "member_count": len(unique), "market_keys": sorted(unique),
        })
    return clusters


def collect_rows(main: dict[str, Any], forward: dict[str, Any] | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    rows: dict[str, dict[str, Any]] = {}
    lanes: dict[str, set[str]] = defaultdict(set)
    sources = list(ROW_LISTS)
    for lane in sources:
        items = main.get(lane)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            key = market_key(item)
            if not key:
                continue
            if key not in rows:
                rows[key] = copy.deepcopy(item)
            else:
                existing = rows[key]
                # Fill null/absent properties only; earlier snapshot values are immutable once non-empty.
                for field, value in item.items():
                    if existing.get(field) is None and value is not None:
                        existing[field] = copy.deepcopy(value)
            lanes[key].add(lane)
    if isinstance(forward, dict):
        for item in forward.get("candidates", []) if isinstance(forward.get("candidates"), list) else []:
            if not isinstance(item, dict):
                continue
            key = market_key(item)
            if not key:
                continue
            if key not in rows:
                rows[key] = copy.deepcopy(item)
            else:
                for field, value in item.items():
                    if rows[key].get(field) is None and value is not None:
                        rows[key][field] = copy.deepcopy(value)
            lanes[key].add("forward_forecast_candidate")
    return rows, lanes


def _validated_signal_number(value: Any, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(number) or not 0.0 <= number <= maximum:
        return None
    return number


def _early_breakout_snapshot(row: dict[str, Any]) -> dict[str, Any] | None:
    if (row.get("early_breakout_signal_version") != EARLY_BREAKOUT_SIGNAL_VERSION
            or row.get("early_breakout_model_id") != EARLY_BREAKOUT_MODEL_ID):
        return None
    raw_components = row.get("early_breakout_components")
    raw_components = raw_components if isinstance(raw_components, dict) else {}
    return {
        "signal_version": EARLY_BREAKOUT_SIGNAL_VERSION,
        "model_id": EARLY_BREAKOUT_MODEL_ID,
        "score": _validated_signal_number(row.get("early_breakout_score"), 100.0),
        "tier": row.get("early_breakout_tier")
        if isinstance(row.get("early_breakout_tier"), str) else None,
        "components": {
            name: _validated_signal_number(raw_components.get(name), maximum)
            for name, maximum in EARLY_BREAKOUT_COMPONENT_LIMITS.items()
        },
        "source": row.get("source") if isinstance(row.get("source"), str) else None,
        "source_url": row.get("source_url") if isinstance(row.get("source_url"), str) else None,
        "retrieved_utc": row.get("retrieved_utc")
        if isinstance(row.get("retrieved_utc"), str) else None,
        "provider_timestamp": row.get("dexpaprika_provider_timestamp")
        if isinstance(row.get("dexpaprika_provider_timestamp"), str) else None,
    }


def _run_provenance(outdir: Path, run_id: str) -> dict[str, str]:
    status_path = outdir / "scan-status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(status, dict) or str(status.get("run_id") or "") != str(run_id):
        return {}
    return {
        key: status[key]
        for key in ("scanner_version", "configuration_fingerprint")
        if isinstance(status.get(key), str) and status[key]
    }


def make_forecast_record(key: str, row: dict[str, Any], lanes: Iterable[str], snapshot_utc: datetime, run_id: str,
                         run_provenance: dict[str, str] | None = None) -> dict[str, Any]:
    forecast_id = hashlib.sha256(f"{run_id}|{key}".encode()).hexdigest()
    fields = (
        "asset", "name", "symbol", "chain", "market_type", "contract_address", "pool_address",
        "pair_address", "gecko_pool_id",
        "pair", "venue", "quote_symbol", "price_usd", "change_5m_pct", "change_1h_pct",
        "change_6h_pct", "change_24h_pct", "volume_1h_usd", "volume_6h_usd", "volume_24h_usd",
        "liquidity_usd", "best_liquidity_pool_usd", "age_hours", "age_hours_v418", "pool_created_at",
        "rank_score", "forecast_score", "source", "source_url", "retrieved_utc", "return_window_status",
        "source_reported_change_24h_pct", "source_reported_change_during_available_pool_history_pct", "fingerprint_cluster_id",
    )
    feature_snapshot = {field: row.get(field) for field in fields if row.get(field) is not None}
    early_breakout = _early_breakout_snapshot(row)
    if early_breakout is not None:
        feature_snapshot["v425_early_breakout"] = early_breakout
    raw_score = finite_number(row.get("forecast_score"))
    if raw_score is None:
        raw_score = finite_number(row.get("rank_score"))
    record = {
        "schema": "crypto-prebreakout.forecast-ledger.v1",
        "pipeline_version": PIPELINE_VERSION,
        "core_runtime_version": CORE_RUNTIME_VERSION,
        "forecast_id": forecast_id,
        "run_id": str(run_id),
        "snapshot_utc": iso_utc(snapshot_utc),
        "market_key": key,
        "identity": row.get("identity"),
        "discovery_lanes": sorted(set(lanes)),
        "model_id": str(row.get("model_id") or "v4.15.3-heuristic-baseline"),
        "raw_score": raw_score,
        "score_is_probability": False,
        "feature_snapshot": feature_snapshot,
    }
    if run_provenance:
        record.update(run_provenance)
    return record


def read_jsonl_gz(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except (OSError, EOFError, json.JSONDecodeError):
        # Corrupt memory must be surfaced, never silently overwritten.
        raise RuntimeError(f"Unable to read learning memory partition: {path}")


def append_jsonl_gz(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "at", encoding="utf-8", compresslevel=9) as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")


def all_forecasts(memory_dir: Path) -> list[dict[str, Any]]:
    result = []
    for path in sorted((memory_dir / "forecasts").glob("*.jsonl.gz")):
        result.extend(read_jsonl_gz(path))
    return result


def all_outcomes(memory_dir: Path) -> list[dict[str, Any]]:
    result = []
    for path in sorted((memory_dir / "outcomes").glob("*.jsonl.gz")):
        result.extend(read_jsonl_gz(path))
    return result


def grade_outcomes(memory_dir: Path, now: datetime, current_rows: dict[str, dict[str, Any]], run_id: str) -> dict[str, Any]:
    forecasts = all_forecasts(memory_dir)
    existing = all_outcomes(memory_dir)
    completed = {f"{item.get('forecast_id')}:{item.get('horizon_hours')}" for item in existing}
    new_outcomes: list[dict[str, Any]] = []
    pending = 0
    unavailable = 0
    for forecast in forecasts:
        forecast_time = utc_dt(forecast.get("snapshot_utc"))
        if forecast_time is None:
            continue
        key = str(forecast.get("market_key") or "")
        for horizon in HORIZONS_HOURS:
            outcome_key = f"{forecast.get('forecast_id')}:{horizon}"
            if outcome_key in completed:
                continue
            elapsed = (now - forecast_time).total_seconds() / 3600.0
            if elapsed < horizon:
                pending += 1
                continue
            current = current_rows.get(key)
            current_price = finite_number(current.get("price_usd")) if current else None
            feature_snapshot = forecast.get("feature_snapshot")
            feature_snapshot = feature_snapshot if isinstance(feature_snapshot, dict) else {}
            base_price = finite_number(feature_snapshot.get("price_usd"))
            is_dex = str(feature_snapshot.get("market_type") or "").lower().startswith("dex") or key.startswith("dex")
            contract = feature_snapshot.get("contract_address")
            pool = (feature_snapshot.get("pool_address") or feature_snapshot.get("pair_address")
                    or feature_snapshot.get("gecko_pool_id"))
            identity_complete = not is_dex or bool(
                feature_snapshot.get("chain") and contract and pool
            )
            base_price_at = utc_dt(feature_snapshot.get("retrieved_utc"))
            base_price_age = (
                (forecast_time - base_price_at).total_seconds() if base_price_at is not None else None
            )
            base_price_verified = (
                base_price_age is not None and -60.0 <= base_price_age <= 30.0 * 60.0
            )
            observed_at = utc_dt(current.get("retrieved_utc")) if current else None
            if (current_price is not None and base_price is not None and current_price > 0 and base_price > 0
                    and identity_complete and base_price_verified and observed_at is not None):
                target_time = forecast_time + timedelta(hours=horizon)
                if observed_at < target_time or observed_at > now:
                    pending += 1
                    continue
                new_outcomes.append({
                    "schema": "crypto-prebreakout.outcome-ledger.v1",
                    "pipeline_version": PIPELINE_VERSION,
                    "forecast_id": forecast.get("forecast_id"),
                    "run_id": str(run_id),
                    "market_key": key,
                    "horizon_hours": horizon,
                    "forecast_utc": forecast.get("snapshot_utc"),
                    "observed_utc": iso_utc(observed_at),
                    "observation_lag_hours": round((observed_at - target_time).total_seconds() / 3600.0, 4),
                    "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL",
                    "base_price_usd": base_price,
                    "observed_price_usd": current_price,
                    "mark_return_pct": round((current_price / base_price - 1.0) * 100.0, 6),
                    "mfe_mae_status": "UNAVAILABLE_NO_INTRAHORIZON_CANDLES",
                    "fees_slippage_status": "NOT_MODELED",
                })
                completed.add(outcome_key)
            elif elapsed >= horizon + max(24.0, horizon):
                new_outcomes.append({
                    "schema": "crypto-prebreakout.outcome-ledger.v1",
                    "pipeline_version": PIPELINE_VERSION,
                    "forecast_id": forecast.get("forecast_id"),
                    "run_id": str(run_id),
                    "market_key": key,
                    "horizon_hours": horizon,
                    "forecast_utc": forecast.get("snapshot_utc"),
                    "observed_utc": iso_utc(now),
                    "status": (
                        "UNAVAILABLE_IDENTITY_NOT_OBSERVED"
                        if current is None or not identity_complete
                        else "UNAVAILABLE_NO_VALID_PRICE_MARK"
                    ),
                    "base_price_usd": base_price,
                    "observed_price_usd": None,
                    "mark_return_pct": None,
                    "mfe_mae_status": "UNAVAILABLE",
                    "fees_slippage_status": "NOT_MODELED",
                })
                unavailable += 1
                completed.add(outcome_key)
            else:
                pending += 1
    append_jsonl_gz(memory_dir / "outcomes" / f"{now:%Y-%m-%d}.jsonl.gz", new_outcomes)
    statuses = defaultdict(int)
    for record in existing + new_outcomes:
        statuses[str(record.get("status") or "UNKNOWN")] += 1
    return {
        "historical_forecasts": len(forecasts),
        "historical_outcomes_before_run": len(existing),
        "new_outcomes": len(new_outcomes),
        "pending_horizon_checks": pending,
        "new_unavailable_outcomes": unavailable,
        "outcome_status_counts": dict(statuses),
    }



def clean_json(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): clean_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_json(item) for item in value]
    return value


def process_snapshot(main_path: Path, forward_path: Path | None, outdir: Path, memory_dir: Path, run_id: str, now_override: datetime | None = None) -> dict[str, Any]:
    main = json.loads(main_path.read_text(encoding="utf-8"))
    forward = json.loads(forward_path.read_text(encoding="utf-8")) if forward_path and forward_path.exists() else None
    snapshot = now_override or utc_dt(main.get("created_utc")) or utc_dt(main.get("scan_start_utc")) or datetime.now(timezone.utc)
    raw_24h_lists = {}
    for name in ("top_24h_gainers", "top_24h_losers", "top_24h_movers"):
        preserved = main.get(f"reported_{name}_raw")
        raw_24h_lists[name] = copy.deepcopy(preserved if isinstance(preserved, list) else main.get(name, []))
    rows, lanes = collect_rows(main, forward)

    # Classify every retained discovery row. Under-24h and age-unknown DEX rows
    # remain visible as launch/discovery leads, but leave comparable 24h boards.
    for row in rows.values():
        age_classify(row, snapshot)
    clusters = fingerprint_clusters(list(rows.values()))

    # Propagate enriched values back into every report lane by market key.
    for lane in ROW_LISTS:
        items = main.get(lane)
        if not isinstance(items, list):
            continue
        enriched = []
        for original in items:
            if not isinstance(original, dict):
                enriched.append(original)
                continue
            key = market_key(original)
            enriched.append(copy.deepcopy(rows[key]) if key in rows else original)
        main[lane] = enriched

    def list_by_status(names: tuple[str, ...], accepted: set[str]) -> list[dict[str, Any]]:
        seen = set(); items = []
        for name in names:
            for row in main.get(name, []) if isinstance(main.get(name), list) else []:
                if not isinstance(row, dict):
                    continue
                key = market_key(row)
                if not key or key in seen:
                    continue
                seen.add(key)
                if row.get("return_window_status") in accepted:
                    items.append(row)
        return items

    # Preserve original source tables intact for audit; canonical top-24h arrays
    # below contain only age-qualified comparable marks.
    for name, raw in raw_24h_lists.items():
        main[f"reported_{name}_raw"] = raw
    valid_statuses = {"SOURCE_REPORTED_24H_POOL_AGE_GE_24H", "EXCHANGE_OR_AGGREGATOR_24H_FIELD"}
    comparable_all = list_by_status(("top_24h_gainers", "top_24h_movers", "top_24h_losers"), valid_statuses)
    comparable_gainers = [r for r in comparable_all if finite_number(r.get("change_24h_pct")) is not None and finite_number(r.get("change_24h_pct")) >= 0 and not r.get("fingerprint_cluster_id")]
    comparable_losers = [r for r in comparable_all if finite_number(r.get("change_24h_pct")) is not None and finite_number(r.get("change_24h_pct")) < 0 and not r.get("fingerprint_cluster_id")]
    comparable_gainers.sort(key=lambda r: finite_number(r.get("change_24h_pct")) if finite_number(r.get("change_24h_pct")) is not None else -1e99, reverse=True)
    comparable_losers.sort(key=lambda r: finite_number(r.get("change_24h_pct")) if finite_number(r.get("change_24h_pct")) is not None else 1e99)
    main["comparable_24h_gainers_v418"] = comparable_gainers[:100]
    main["comparable_24h_losers_v418"] = comparable_losers[:100]
    main["top_24h_gainers"] = comparable_gainers[:100]
    main["top_24h_losers"] = comparable_losers[:100]
    main["top_24h_movers"] = sorted(comparable_gainers + comparable_losers, key=lambda r: abs(finite_number(r.get("change_24h_pct")) or 0), reverse=True)[:100]
    # Suspicious fingerprint clusters remain visible on discovery boards.
    launch = [row for row in rows.values() if row.get("return_window_status") in {"INSUFFICIENT_24H_HISTORY", "UNKNOWN_POOL_AGE_NOT_COMPARABLE"}]
    launch.sort(key=lambda r: (finite_number(r.get("change_1h_pct")) or -1e99, finite_number(r.get("volume_1h_usd")) or 0), reverse=True)
    main["new_launch_discovery_board_v418"] = launch[:500]
    fingerprint_rows = [r for r in rows.values() if r.get("fingerprint_cluster_id")]
    main["market_data_fingerprint_anomalies_v418"] = fingerprint_rows[:500]

    # Build immutable snapshots for every identified discovery row, including
    # low-ranked, high-risk and forward-candidate rows.
    provenance = _run_provenance(outdir, run_id)
    forecasts = [
        make_forecast_record(k, v, lanes.get(k, set()), snapshot, run_id, provenance)
        for k, v in sorted(rows.items())
    ]
    forecast_path = memory_dir / "forecasts" / f"{snapshot:%Y-%m-%d}.jsonl.gz"
    existing_current = read_jsonl_gz(forecast_path)
    seen_ids = {r.get("forecast_id") for r in existing_current}
    append = [r for r in forecasts if r["forecast_id"] not in seen_ids]
    append_jsonl_gz(forecast_path, append)

    try:
        try:
            from scripts.prospective_opportunity_dataset import ProspectiveOpportunityDataset
        except ImportError:
            from prospective_opportunity_dataset import ProspectiveOpportunityDataset

        prospective_snapshots = [
            ProspectiveOpportunityDataset.create_snapshot(
                candidate=v,
                run_id=str(run_id),
                scanner_version=str(provenance.get("scanner_version") or PIPELINE_VERSION),
                configuration_fingerprint=str(provenance.get("configuration_fingerprint") or "default"),
                qualification_status=str(v.get("opportunity_status") or v.get("entry_decision") or "DISCOVERY_LEAD"),
                rejection_reasons=v.get("qualification_blockers") or v.get("actionable_blockers") or [],
                snapshot_time=snapshot,
            )
            for k, v in sorted(rows.items())
        ]
        ProspectiveOpportunityDataset.append_snapshots(
            memory_dir,
            prospective_snapshots,
            partition_date=f"{snapshot:%Y%m%d}",
        )
    except Exception as exc:
        # Prospective snapshot capture failure must not silently crash memory pipeline
        pass

    grading = grade_outcomes(memory_dir, snapshot, rows, run_id)

    summary = {
        "schema": "crypto-prebreakout.v418-quality-audit.v1",
        "pipeline_version": PIPELINE_VERSION,
        "core_runtime_version": str(main.get("skill_version") or CORE_RUNTIME_VERSION),
        "run_id": str(run_id),
        "snapshot_utc": iso_utc(snapshot),
        "retained_report_rows_deduplicated": len(rows),
        "reported_source_unique_asset_rows": (main.get("coverage") or {}).get("unique_asset_rows"),
        "forecast_snapshots_new": len(append),
        "return_window_counts": {
            status: sum(1 for row in rows.values() if row.get("return_window_status") == status)
            for status in ("INSUFFICIENT_24H_HISTORY", "UNKNOWN_POOL_AGE_NOT_COMPARABLE", "SOURCE_REPORTED_24H_POOL_AGE_GE_24H", "EXCHANGE_OR_AGGREGATOR_24H_FIELD")
        },
        "comparable_24h_gainers": len(comparable_gainers),
        "comparable_24h_losers": len(comparable_losers),
        "new_launch_discovery_leads": len(launch),
        "fingerprint_clusters": len(clusters),
        "fingerprint_rows": len(fingerprint_rows),
        "fingerprint_cluster_details": clusters,
        "learning": grading,
        "learning_policy": "IMMUTABLE_FEATURE_SNAPSHOTS; MARK-TO-MARKET OUTCOMES; NO AUTOMATIC MODEL WEIGHT CHANGES",
        "coverage_note": "Observed sampled public feeds only; this does not establish exhaustive Web3 coverage.",
    }
    memory_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": "crypto-prebreakout.memory-manifest.v1",
        "pipeline_version": PIPELINE_VERSION,
        "core_runtime_version": summary["core_runtime_version"],
        "last_run_id": str(run_id),
        "last_snapshot_utc": iso_utc(snapshot),
        "forecast_partitions": len(list((memory_dir / "forecasts").glob("*.jsonl.gz"))),
        "outcome_partitions": len(list((memory_dir / "outcomes").glob("*.jsonl.gz"))),
        "historical_forecasts": grading["historical_forecasts"],
        "historical_outcomes": grading["historical_outcomes_before_run"] + grading["new_outcomes"],
        "new_forecast_snapshots": len(append),
        "new_outcomes": grading["new_outcomes"],
        "idempotency_key": str(run_id),
        "coverage_boundary": "Only detailed rows exported into the core collector's report lanes are snapshotted; internal-only aggregate counts are not full row exports.",
    }
    manifest_tmp = memory_dir / "memory-manifest.json.tmp"
    manifest_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_tmp.replace(memory_dir / "memory-manifest.json")

    main["pipeline_version"] = PIPELINE_VERSION
    main["core_runtime_version"] = summary["core_runtime_version"]
    main["v418_quality_audit"] = summary
    main_path.write_text(json.dumps(clean_json(main), indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")

    if forward is not None and forward_path is not None:
        forward["pipeline_version"] = PIPELINE_VERSION
        forward["core_runtime_version"] = str(forward.get("skill_version") or CORE_RUNTIME_VERSION)
        forward["learning_snapshot_count"] = len(append)
        forward["learning_is_probability"] = False
        forward["coverage_statement"] = "Ranked only within the sampled accessible universe; not all Web3 assets."
        forward_path.write_text(json.dumps(clean_json(forward), indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")

    report_path = outdir / "rocket-hunt.md"
    if report_path.exists():
        md = report_path.read_text(encoding="utf-8")
        md = md.replace("## Top 24h gainers", "## Raw provider-reported 24h gainers (pre-v4.18 age validation)\n\n> Raw source-reported changes may cover less than 24 hours for newly created pools. Use the age-qualified v4.18 board below for comparable 24h rankings.")
        section = [
            "", "## v4.18 quality and learning audit", "",
            f"- Pipeline overlay: **v{PIPELINE_VERSION}**; market collector core: **v{summary['core_runtime_version']}**.",
            f"- Identified markets/leads retained in report lanes: **{len(rows)}**; source collector reports **{(main.get('coverage') or {}).get('unique_asset_rows', 'unknown')}** deduplicated universe rows, which are not all exported as detailed rows by the v4.15.3 core.",
            f"- New immutable forecast snapshots from available detailed rows this run: **{len(append)}**.",
            f"- Comparable provider-reported 24h gainers after pool-age filtering: **{len(comparable_gainers)}**.",
            f"- Under-24h launch leads retained separately: **{len(launch)}**; age-unknown DEX leads retained separately: **{summary['return_window_counts']['UNKNOWN_POOL_AGE_NOT_COMPARABLE']}**.",
            f"- Suspicious repeated price/liquidity fingerprint clusters: **{len(clusters)}** (members are kept visible but quarantined from comparable-return ranking).",
            "- Outcome records are delayed same-market price marks, not executable fills; MFE/MAE and fees/slippage are not inferred when not available.",
            "- No automatic model-weight changes are made. Forecast scores are not probabilities.",
            "", "### Age-qualified 24h gainers", "",
            "| # | Asset | Chain | Reported 24h move | Pool age (h) | Liquidity USD | Source |", "|---:|---|---|---:|---:|---:|---|",
        ]
        for i, row in enumerate(comparable_gainers[:25], 1):
            section.append("| {} | {} | {} | {} | {} | {} | {} |".format(
                i, str(row.get("asset") or row.get("symbol") or "—").replace("|", "/"),
                row.get("chain") or row.get("venue") or "—",
                row.get("change_24h_pct") if row.get("change_24h_pct") is not None else "—",
                row.get("age_hours_v418", row.get("age_hours", "—")),
                row.get("liquidity_usd", "—"), row.get("source", "—")))
        section.extend(["", "### Newly launched / age-unknown leads", "", "These assets remain discovery candidates; their reported 24h move is not treated as a completed 24h return.", ""])
        for row in launch[:30]:
            section.append(f"- **{row.get('asset') or row.get('symbol') or 'Unnamed'}** ({row.get('chain') or 'unknown chain'}) — {row.get('return_window_status')}; recent 1h change: {row.get('change_1h_pct', 'unavailable')}; {row.get('source') or 'source unavailable'}.")
        section.extend(["", "### Learning ledger", "", f"- Historical forecasts: {grading['historical_forecasts']}.", f"- New outcome marks: {grading['new_outcomes']}.", f"- Pending horizon checks: {grading['pending_horizon_checks']}.", f"- Unavailable identity outcomes newly marked: {grading['new_unavailable_outcomes']}.", "", "This report separates new-launch discovery from comparable 24h return ranking and makes the actual sampled universe explicit.", ""])
        marker = "## v4.18 quality and learning audit"
        if marker in md:
            md = md.split(marker, 1)[0].rstrip()
        report_path.write_text(md + "\n\n" + "\n".join(section), encoding="utf-8")

    audit_path = outdir / "v418-quality-audit.json"
    audit_path.write_text(json.dumps(clean_json(summary), indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return summary


def main_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", type=Path, default=Path("rocket-run"))
    parser.add_argument("--memory-dir", type=Path, required=True)
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local-run"))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        from datetime import datetime, timezone
        sample = {"market_type": "dex_pool", "age_hours": 12, "change_24h_pct": 900, "risk_flags": []}
        assert age_classify(sample, datetime.now(timezone.utc)) == "INSUFFICIENT_24H_HISTORY"
        assert sample["change_24h_pct"] is None and sample["source_reported_change_24h_pct"] == 900
        print("v4.18 self-test passed")
        return 0
    main_path = args.outdir / "rocket-hunt.json"
    if not main_path.exists():
        parser.error(f"missing scan file: {main_path}")
    summary = process_snapshot(main_path, args.outdir / "forward-24h-rocket.json", args.outdir, args.memory_dir, args.run_id)
    print(json.dumps({k: summary[k] for k in ("pipeline_version", "retained_report_rows_deduplicated", "forecast_snapshots_new", "comparable_24h_gainers", "new_launch_discovery_leads", "fingerprint_clusters", "learning")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
