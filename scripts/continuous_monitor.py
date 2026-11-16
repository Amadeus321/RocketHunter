#!/usr/bin/env python3
"""Bounded, restartable research monitoring for forecast-matched candidates."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

try:
    from scripts import collect_watchlist_observations as collector
    from scripts import research_run_contract as run_contract
    from scripts import v425_early_breakout as monitor
    from scripts import venues
    from scripts.address_identity import address_key, same_address
except ImportError:
    import collect_watchlist_observations as collector
    import research_run_contract as run_contract
    import v425_early_breakout as monitor
    import venues
    from address_identity import address_key, same_address


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_ROOT = ROOT / "rocket-hunter-data" / "research-data" / "continuous-monitoring"
DEFAULT_WATCHLIST = ROOT / "rocket-hunter-data" / "scanner-memory" / "active-24h-watchlist.json"
STATE_SCHEMA = "crypto-prebreakout.continuous-monitor-state.v1"
MANIFEST_SCHEMA = "crypto-prebreakout.continuous-monitor-manifest.v1"
PHASE4_WATCHLIST_SCHEMA = "crypto-prebreakout.active-24h-watchlist.v2"
PHASE4_SCORE_SNAPSHOT_SCHEMA = "rocket-hunter.phase4-score-snapshot.v1"
PROVIDER = "geckoterminal"
MIN_PROVIDER_PACING_SECONDS = 12.0
MAX_SESSION_MINUTES = 180
MAX_ACTIVE_CANDIDATES = 12
MAX_HORIZON = dt.timedelta(hours=24)
MAX_SOURCE_EVENT_LAG = dt.timedelta(hours=2)
MAX_SOURCE_DOCUMENT_AGE = MAX_HORIZON
MAX_FUTURE_TIMESTAMP_SKEW = dt.timedelta(minutes=5)
PRIORITY_INTERVAL_DEFAULT = 60
BASE_INTERVAL_DEFAULT = 300
GECKO_ROOT = "https://api.geckoterminal.com/api/v2"
GECKO_ALIASES = {
    "avalanche": "avax", "fantom": "ftm", "cronos": "cro",
    "sei": "sei-network",
}
USER_AGENT = "RocketHunter-ContinuousResearchMonitor/1.0 (read-only)"


class MonitorError(RuntimeError):
    """Raised when persisted monitoring state or its inputs are unsafe."""


class ProviderStop(MonitorError):
    """Raised when provider policy requires collection to stop immediately."""

    def __init__(self, message: str, metadata: dict[str, Any]):
        super().__init__(message)
        self.metadata = metadata


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_iso(value: dt.datetime | None = None) -> str:
    stamp = value or utc_now()
    return stamp.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(dt.timezone.utc)


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _expected_scanner_version() -> str:
    try:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise MonitorError(f"cannot read repository scanner version: {exc}") from exc
    if not version:
        raise MonitorError("repository scanner version is empty")
    return version


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def validate_watchlist_document(document: Any, now: dt.datetime) -> tuple[list[Any], dict[str, str]]:
    if not isinstance(document, dict):
        raise MonitorError("Phase 4 watchlist must be a provenance-bearing object")
    if document.get("schema") != PHASE4_WATCHLIST_SCHEMA:
        raise MonitorError("Phase 4 watchlist schema is missing or unsupported")
    scanner_version = document.get("scanner_version")
    if scanner_version != _expected_scanner_version():
        raise MonitorError("Phase 4 watchlist scanner version is missing or stale")
    configuration_fingerprint = document.get("configuration_fingerprint")
    if not _is_sha256(configuration_fingerprint):
        raise MonitorError("Phase 4 watchlist configuration fingerprint is missing or invalid")
    source_run_id = document.get("source_scan_run_id")
    if not isinstance(source_run_id, str) or not source_run_id.strip():
        raise MonitorError("Phase 4 watchlist source scan run ID is missing")
    if source_run_id != document.get("run_id"):
        raise MonitorError("Phase 4 watchlist source scan run ID conflicts with watchlist run ID")
    if document.get("source_provenance_status") != "PRESENT":
        raise MonitorError("Phase 4 watchlist source provenance is incomplete")
    updated = parse_utc(document.get("updated_utc"))
    if updated is None:
        raise MonitorError("Phase 4 watchlist update timestamp is missing or timezone-naive")
    if updated > now + MAX_FUTURE_TIMESTAMP_SKEW:
        raise MonitorError("Phase 4 watchlist update timestamp is in the future")
    if now - updated > MAX_SOURCE_DOCUMENT_AGE:
        raise MonitorError("Phase 4 watchlist is stale")
    positions = document.get("positions")
    if not isinstance(positions, list):
        raise MonitorError("Phase 4 watchlist positions must be a list")
    provenance = {
        "schema": str(document["schema"]),
        "scanner_version": scanner_version,
        "configuration_fingerprint": configuration_fingerprint,
        "source_scan_run_id": source_run_id,
        "updated_utc": utc_iso(updated),
    }
    return positions, provenance


def _validate_candidate_source(candidate: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    provenance = candidate.get("source_provenance")
    if not isinstance(provenance, dict):
        raise ValueError("Phase 4 source provenance is missing")
    scanner_version = provenance.get("scanner_version")
    if scanner_version != _expected_scanner_version():
        raise ValueError("Phase 4 source scanner version is missing or stale")
    configuration_fingerprint = provenance.get("configuration_fingerprint")
    if not _is_sha256(configuration_fingerprint):
        raise ValueError("Phase 4 source configuration fingerprint is missing or invalid")
    source_run_id = provenance.get("source_run_id")
    created_run_id = candidate.get("created_run_id")
    if not isinstance(source_run_id, str) or not source_run_id.strip():
        raise ValueError("Phase 4 source run ID is missing")
    if not isinstance(created_run_id, str) or source_run_id != created_run_id:
        raise ValueError("Phase 4 source run ID conflicts with candidate creation run")
    source_event = parse_utc(provenance.get("source_event_utc"))
    source_received = parse_utc(provenance.get("source_received_utc"))
    if source_event is None or source_received is None:
        raise ValueError("Phase 4 source event/received timestamps are missing or invalid")
    if source_received > now + MAX_FUTURE_TIMESTAMP_SKEW or source_event > now + MAX_FUTURE_TIMESTAMP_SKEW:
        raise ValueError("Phase 4 source timestamp is in the future")
    if now - source_received > MAX_SOURCE_DOCUMENT_AGE:
        raise ValueError("Phase 4 source provenance is stale")
    if source_received < source_event - MAX_FUTURE_TIMESTAMP_SKEW:
        raise ValueError("Phase 4 source received timestamp precedes event timestamp")
    if source_received - source_event > MAX_SOURCE_EVENT_LAG:
        raise ValueError("Phase 4 source event evidence is stale")
    candidate_event = parse_utc(
        candidate.get("entry_timestamp_utc")
        or candidate.get("forecast_utc")
        or candidate.get("data_timestamp_utc")
    )
    if candidate_event is None or candidate_event != source_event:
        raise ValueError("Phase 4 source event timestamp conflicts with candidate entry timestamp")

    snapshot = candidate.get("phase4_score_snapshot")
    required_snapshot_fields = (
        "early_breakout_score", "breakout_confluence_score", "breakout_engine_scores",
        "breakout_acceleration_signals", "engine_evidence_status",
        "evidence_coverage_pct", "breakout_evidence_eligible",
        "signal_engine_executed", "expected_return_engine_executed",
        "engine_result_status", "expected_return_status",
    )
    if not isinstance(snapshot, dict) or snapshot.get("schema") != PHASE4_SCORE_SNAPSHOT_SCHEMA:
        raise ValueError("Phase 4 score snapshot is missing or unsupported")
    if any(field not in snapshot for field in required_snapshot_fields):
        raise ValueError("Phase 4 score snapshot is incomplete")
    if not isinstance(snapshot["breakout_engine_scores"], dict):
        raise ValueError("Phase 4 engine score bundle is invalid")
    if not isinstance(snapshot["engine_evidence_status"], dict):
        raise ValueError("Phase 4 engine evidence status is invalid")
    if not isinstance(snapshot["breakout_evidence_eligible"], bool):
        raise ValueError("Phase 4 fail-closed eligibility status is missing")
    if not isinstance(snapshot["engine_result_status"], str) or not isinstance(snapshot["expected_return_status"], str):
        raise ValueError("Phase 4 engine execution status is missing")
    return {
        "provenance": provenance,
        "score_snapshot": snapshot,
        "source_freshness_status": "FRESH",
    }


def _candidate_identity(candidate: dict[str, Any]) -> tuple[str, dict[str, str]]:
    market_type = str(candidate.get("market_type") or "").lower()
    if market_type == "dex_pool":
        chain = str(candidate.get("chain") or "").strip().lower()
        token = str(candidate.get("token_mint") or candidate.get("contract_address") or "").strip()
        contract = str(candidate.get("contract_address") or "").strip()
        token_mint = str(candidate.get("token_mint") or "").strip()
        pool = str(candidate.get("pool_address") or "").strip()
        if not pool:
            pool = str(candidate.get("pair_address") or "").strip()
        if not chain or not token or not pool:
            raise ValueError("DEX identity requires exact chain, token mint/address, and pool address")
        if contract and token_mint and not same_address(chain, contract, token_mint):
            raise ValueError("contract_address conflicts with token_mint")
        pair = str(candidate.get("pair_address") or "").strip()
        if pair and not same_address(chain, pair, pool):
            raise ValueError("pair_address conflicts with pool_address")
        identity = {
            "market_type": market_type,
            "chain": chain,
            "token_mint": address_key(chain, token),
            "pool_address": address_key(chain, pool),
        }
        return _sha256(_canonical_json(identity)), identity
    if market_type == "cex_spot":
        venue = str(candidate.get("venue") or "").strip().lower()
        pair = str(candidate.get("venue_pair") or "").strip()
        if not venue or not pair:
            raise ValueError("CEX identity requires exact venue and venue_pair")
        identity = {"market_type": market_type, "venue": venue, "venue_pair": pair}
        return _sha256(_canonical_json(identity)), identity
    raise ValueError("unsupported_market_type")


def validate_candidate(candidate: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    source = _validate_candidate_source(candidate, now)
    key, identity = _candidate_identity(candidate)
    plan = candidate.get("trade_plan") if isinstance(candidate.get("trade_plan"), dict) else {}
    entry = _finite_number(candidate.get("entry_price"))
    if entry is None:
        entry = _finite_number(candidate.get("price_usd"))
    if entry is None:
        entry = _finite_number(candidate.get("price"))
    target = _finite_number(candidate.get("single_take_profit"))
    if target is None:
        target = _finite_number(plan.get("single_take_profit"))
    if entry is None or entry <= 0:
        raise ValueError("positive entry price is required")
    if target is None or target < entry * 1.4:
        raise ValueError("exactly one single_take_profit of at least +40.00% is required")
    entry_stamp = (
        parse_utc(candidate.get("entry_timestamp_utc"))
        or parse_utc(candidate.get("forecast_utc"))
        or parse_utc(candidate.get("data_timestamp_utc"))
    )
    if entry_stamp is None:
        raise ValueError("timezone-qualified candidate entry timestamp is required")
    expiry = entry_stamp + MAX_HORIZON
    external_id = str(candidate.get("forecast_id") or candidate.get("created_run_id") or "")
    candidate_key = _sha256(f"{key}|{utc_iso(entry_stamp)}|{external_id}")
    score = _finite_number(source["score_snapshot"].get("early_breakout_score"))
    return {
        "candidate_key": candidate_key,
        "identity_key": key,
        "identity": identity,
        "market_key": str(candidate.get("market_key") or candidate.get("identity") or ""),
        "source_candidate": candidate,
        "entry_price": entry,
        "target_price": target,
        "entry_utc": utc_iso(entry_stamp),
        "expires_utc": utc_iso(expiry),
        "score": score,
        "source_provenance": source["provenance"],
        "phase4_score_snapshot": source["score_snapshot"],
        "source_provenance_status": "VERIFIED",
        "source_freshness_status": source["source_freshness_status"],
        "source_run_id": str(candidate.get("created_run_id") or candidate.get("run_id") or ""),
        "forecast_id": str(candidate.get("forecast_id") or ""),
        "status": "EXPIRED" if now >= expiry else "QUEUED",
        "outcome": "EXPIRED_BEFORE_MONITORING" if now >= expiry else None,
    }


def _ensure_disjoint_paths(memory_dir: Path, state_root: Path) -> None:
    output = state_root.resolve()
    protected_roots = [
        memory_dir.resolve(),
        collector.DEFAULT_EXPORTS.resolve(),
        collector.DEFAULT_ANALYSIS.resolve(),
        collector.DEFAULT_RUNS.resolve(),
        ROOT / "rocket-hunter-data" / "user_positions_journal.json",
        ROOT / "rocket-hunter-data" / "ai_learning_engine.json",
    ]
    configured_user_data = os.environ.get("ROCKET_HUNTER_USER_DATA_DIR")
    if configured_user_data:
        protected_roots.append(Path(configured_user_data).resolve())
    for protected in protected_roots:
        protected = protected.resolve()
        if output == protected or output in protected.parents or protected in output.parents:
            raise MonitorError(
                "monitoring state must be separate from scanner memory, exports, analysis, and scan outputs"
            )


def open_state(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise MonitorError(f"monitoring database must not be a symlink: {path}")
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS candidates (
            candidate_key TEXT PRIMARY KEY,
            identity_key TEXT NOT NULL,
            identity_json TEXT NOT NULL,
            candidate_json TEXT NOT NULL,
            market_key TEXT NOT NULL,
            entry_price REAL NOT NULL,
            target_price REAL NOT NULL,
            entry_utc TEXT NOT NULL,
            expires_utc TEXT NOT NULL,
            score REAL,
            source_provenance_json TEXT NOT NULL DEFAULT '{}',
            phase4_score_snapshot_json TEXT NOT NULL DEFAULT '{}',
            source_provenance_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
            source_freshness_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
            source_run_id TEXT NOT NULL,
            forecast_id TEXT NOT NULL,
            status TEXT NOT NULL,
            outcome TEXT,
            first_valid_observation_utc TEXT,
            first_target_touch_utc TEXT,
            next_due_utc TEXT,
            last_observation_utc TEXT,
            latest_metrics_json TEXT,
            missing_5m_intervals INTEGER,
            conflict_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            updated_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS candidate_state_due
            ON candidates(status, next_due_utc, score);
        CREATE TABLE IF NOT EXISTS observations (
            event_id TEXT PRIMARY KEY,
            candidate_key TEXT NOT NULL REFERENCES candidates(candidate_key),
            collection_run_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            resolution TEXT NOT NULL,
            source_event_utc TEXT,
            received_utc TEXT NOT NULL,
            source_url TEXT,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            configuration_fingerprint TEXT NOT NULL DEFAULT '',
            watchlist_sha256 TEXT NOT NULL DEFAULT '',
            scanner_version TEXT NOT NULL DEFAULT 'unknown',
            source_configuration_fingerprint TEXT NOT NULL DEFAULT '',
            source_run_id TEXT NOT NULL DEFAULT '',
            candidate_source_event_utc TEXT NOT NULL DEFAULT '',
            candidate_source_received_utc TEXT NOT NULL DEFAULT '',
            source_provenance_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
            source_freshness_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
            phase4_score_snapshot_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(candidate_key) REFERENCES candidates(candidate_key)
        );
        CREATE INDEX IF NOT EXISTS observations_candidate_time
            ON observations(candidate_key, resolution, source_event_utc);
        CREATE TABLE IF NOT EXISTS rejected_candidates (
            rejection_id TEXT PRIMARY KEY,
            collection_run_id TEXT NOT NULL,
            rejected_utc TEXT NOT NULL,
            market_key TEXT,
            reason TEXT NOT NULL,
            candidate_sha256 TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS provider_state (
            provider TEXT PRIMARY KEY,
            cooldown_until_utc TEXT,
            last_http_status INTEGER,
            updated_utc TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS observations_no_update
        BEFORE UPDATE ON observations BEGIN
            SELECT RAISE(ABORT, 'monitoring observations are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS observations_no_delete
        BEFORE DELETE ON observations BEGIN
            SELECT RAISE(ABORT, 'monitoring observations are immutable');
        END;
    """)
    observation_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(observations)")
    }
    for column, definition in (
        ("configuration_fingerprint", "TEXT NOT NULL DEFAULT ''"),
        ("watchlist_sha256", "TEXT NOT NULL DEFAULT ''"),
        ("scanner_version", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("source_configuration_fingerprint", "TEXT NOT NULL DEFAULT ''"),
        ("source_run_id", "TEXT NOT NULL DEFAULT ''"),
        ("candidate_source_event_utc", "TEXT NOT NULL DEFAULT ''"),
        ("candidate_source_received_utc", "TEXT NOT NULL DEFAULT ''"),
        ("source_provenance_status", "TEXT NOT NULL DEFAULT 'UNVERIFIED'"),
        ("source_freshness_status", "TEXT NOT NULL DEFAULT 'UNVERIFIED'"),
        ("phase4_score_snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if column not in observation_columns:
            connection.execute(f"ALTER TABLE observations ADD COLUMN {column} {definition}")
    candidate_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(candidates)")
    }
    for column, definition in (
        ("source_provenance_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("phase4_score_snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("source_provenance_status", "TEXT NOT NULL DEFAULT 'UNVERIFIED'"),
        ("source_freshness_status", "TEXT NOT NULL DEFAULT 'UNVERIFIED'"),
    ):
        if column not in candidate_columns:
            connection.execute(f"ALTER TABLE candidates ADD COLUMN {column} {definition}")
    connection.commit()
    return connection


def _record_rejection(connection: sqlite3.Connection, run_id: str, now: dt.datetime,
                      candidate: Any, reason: str) -> None:
    market_key = (
        candidate.get("market_key") or candidate.get("identity")
        if isinstance(candidate, dict) else None
    )
    try:
        raw = _canonical_json(candidate)
    except (TypeError, ValueError):
        raw = repr(candidate)
    rejection_id = _sha256(f"{run_id}|{raw}|{reason}")
    connection.execute(
        "INSERT OR IGNORE INTO rejected_candidates VALUES (?, ?, ?, ?, ?, ?)",
        (rejection_id, run_id, utc_iso(now), str(market_key or ""), reason, _sha256(raw)),
    )


def ingest_candidates(connection: sqlite3.Connection, candidates: list[dict[str, Any]],
                      run_id: str, now: dt.datetime) -> tuple[int, int, int]:
    accepted = rejected = duplicates = 0
    for candidate in candidates:
        try:
            normalized = validate_candidate(candidate, now)
            candidate_json = _canonical_json(candidate)
        except (ValueError, TypeError) as exc:
            _record_rejection(connection, run_id, now, candidate, str(exc))
            rejected += 1
            continue
        except (OverflowError, RecursionError) as exc:
            _record_rejection(connection, run_id, now, candidate, f"invalid candidate encoding: {exc}")
            rejected += 1
            continue
        existing = connection.execute(
            "SELECT identity_json, entry_price, target_price, entry_utc FROM candidates WHERE candidate_key=?",
            (normalized["candidate_key"],),
        ).fetchone()
        if existing:
            if (
                existing["identity_json"] != _canonical_json(normalized["identity"])
                or existing["entry_price"] != normalized["entry_price"]
                or existing["target_price"] != normalized["target_price"]
                or existing["entry_utc"] != normalized["entry_utc"]
            ):
                _record_rejection(connection, run_id, now, candidate, "conflicting duplicate candidate baseline")
                rejected += 1
            else:
                duplicates += 1
            continue
        connection.execute("""
            INSERT INTO candidates (
                candidate_key, identity_key, identity_json, candidate_json, market_key,
                entry_price, target_price, entry_utc, expires_utc, score,
                source_provenance_json, phase4_score_snapshot_json,
                source_provenance_status, source_freshness_status, source_run_id,
                forecast_id, status, outcome, updated_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            normalized["candidate_key"], normalized["identity_key"],
            _canonical_json(normalized["identity"]), candidate_json,
            normalized["market_key"], normalized["entry_price"], normalized["target_price"],
            normalized["entry_utc"], normalized["expires_utc"], normalized["score"],
            _canonical_json(normalized["source_provenance"]),
            _canonical_json(normalized["phase4_score_snapshot"]),
            normalized["source_provenance_status"], normalized["source_freshness_status"],
            normalized["source_run_id"], normalized["forecast_id"], normalized["status"],
            normalized["outcome"], utc_iso(now),
        ))
        accepted += 1
    connection.commit()
    return accepted, rejected, duplicates


def promote_queued(connection: sqlite3.Connection, now: dt.datetime,
                   max_active: int = MAX_ACTIVE_CANDIDATES) -> int:
    active_count = connection.execute(
        "SELECT COUNT(*) FROM candidates WHERE status IN ('ACTIVE', 'TARGET_TOUCHED')"
    ).fetchone()[0]
    slots = max(0, max_active - active_count)
    if not slots:
        return 0
    rows = connection.execute("""
        SELECT candidate_key FROM candidates
        WHERE status='QUEUED' AND expires_utc>?
        ORDER BY CASE WHEN score IS NULL THEN 1 ELSE 0 END, score DESC, entry_utc, candidate_key
        LIMIT ?
    """, (utc_iso(now), slots)).fetchall()
    for row in rows:
        connection.execute(
            "UPDATE candidates SET status='ACTIVE', next_due_utc=?, updated_utc=? WHERE candidate_key=?",
            (utc_iso(now), utc_iso(now), row["candidate_key"]),
        )
    connection.execute("""
        UPDATE candidates SET status='EXPIRED', outcome='EXPIRED_BEFORE_MONITORING',
            updated_utc=? WHERE status='QUEUED' AND expires_utc<=?
    """, (utc_iso(now), utc_iso(now)))
    connection.commit()
    return len(rows)


def _normalise_bar(raw: list[Any], resolution_seconds: int, now: dt.datetime) -> dict[str, Any] | None:
    if not isinstance(raw, list) or len(raw) < 6:
        return None
    try:
        opened = dt.datetime.fromtimestamp(int(raw[0]), dt.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    close_time = opened + dt.timedelta(seconds=resolution_seconds)
    if close_time > now:
        return None
    values = [_finite_number(value) for value in raw[1:6]]
    if any(value is None for value in values):
        return None
    _, high, low, close, volume = values
    if high <= 0 or low <= 0 or close <= 0 or volume < 0 or low > high:
        return None
    return {
        "time_open": utc_iso(opened),
        "time_close": utc_iso(close_time),
        "open": values[0],
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def _pool_url(identity: dict[str, str]) -> str:
    chain = GECKO_ALIASES.get(identity["chain"], identity["chain"])
    return "%s/networks/%s/pools/%s" % (
        GECKO_ROOT, urllib.parse.quote(chain, safe=""),
        urllib.parse.quote(identity["pool_address"], safe=""),
    )


def _ohlcv_url(identity: dict[str, str], aggregate: int, limit: int) -> str:
    chain = GECKO_ALIASES.get(identity["chain"], identity["chain"])
    params = urllib.parse.urlencode({
        "aggregate": aggregate,
        "limit": limit,
        "currency": "usd",
        "token": identity["token_mint"],
    })
    return "%s/networks/%s/pools/%s/ohlcv/minute?%s" % (
        GECKO_ROOT, urllib.parse.quote(chain, safe=""),
        urllib.parse.quote(identity["pool_address"], safe=""), params,
    )


def _reserve_provider_slot(state_path: Path, deadline: float,
                           interval: float = MIN_PROVIDER_PACING_SECONDS) -> None:
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with collector._locked_writer(lock_path, deadline):
        try:
            next_allowed = float(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            next_allowed = 0.0
        wait = max(0.0, next_allowed - time.time())
        if time.monotonic() + wait >= deadline:
            raise MonitorError("session duration limit reached while waiting for provider pacing")
        if wait:
            time.sleep(wait)
        if time.monotonic() >= deadline:
            raise MonitorError("session duration limit reached during provider pacing")
        temp = state_path.with_suffix(state_path.suffix + ".tmp")
        temp.write_text(json.dumps(time.time() + max(MIN_PROVIDER_PACING_SECONDS, interval)),
                        encoding="utf-8")
        temp.replace(state_path)


def _request_timeout(deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 1:
        raise MonitorError("session duration limit reached before provider request")
    return max(1, min(8, int(remaining)))


def _request_json(url: str, timeout: int = 8) -> Any:
    request = urllib.request.Request(url, headers={
        "Accept": "application/json;version=20230203",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        metadata = {"provider": PROVIDER, "http_status": exc.code}
        retry_after = collector.monitor._retry_after_metadata(
            collector.monitor._safe_retry_after_header(
                collector.monitor._response_header_value(exc.headers, "Retry-After")
            )
        )
        if retry_after is not None:
            metadata.update(retry_after)
        if exc.code in (403, 418, 429):
            raise ProviderStop(f"provider returned HTTP {exc.code}", metadata) from exc
        raise MonitorError(f"provider HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise MonitorError(f"{type(exc).__name__}: {exc}") from exc


def _response_attributes(payload: Any) -> tuple[str | None, dict[str, Any]]:
    if not isinstance(payload, dict):
        return None, {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, {}
    attrs = data.get("attributes")
    return data.get("id") if isinstance(data.get("id"), str) else None, attrs if isinstance(attrs, dict) else {}


def _validate_pool_payload(payload: Any, identity: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    pool_id, attrs = _response_attributes(payload)
    if not pool_id:
        raise MonitorError("pool response omitted exact pool identifier")
    base = attrs.get("base_token_address")
    quote = attrs.get("quote_token_address")
    if not isinstance(base, str) or not isinstance(quote, str):
        raise MonitorError("pool response omitted exact base/quote token addresses")
    chain = identity["chain"]
    if not (same_address(chain, base, identity["token_mint"]) or
            same_address(chain, quote, identity["token_mint"])):
        raise MonitorError("provider pool response token identity mismatch")
    expected_chain = GECKO_ALIASES.get(chain, chain)
    if not pool_id.lower().startswith(expected_chain.lower() + "_"):
        raise MonitorError("provider pool response chain identity mismatch")
    suffix = pool_id.rsplit("_", 1)[-1]
    if not same_address(chain, suffix, identity["pool_address"]):
        raise MonitorError("provider pool response pool identity mismatch")
    token_is_base = same_address(chain, base, identity["token_mint"])
    price = _finite_number(attrs.get("base_token_price_usd" if token_is_base else "quote_token_price_usd"))
    volume = attrs.get("volume_usd")
    volume_h24 = _finite_number(volume.get("h24")) if isinstance(volume, dict) else None
    transactions = attrs.get("transactions")
    h24 = transactions.get("h24") if isinstance(transactions, dict) else None
    pressure = None
    if isinstance(h24, dict):
        buys = _finite_number(h24.get("buys"))
        sells = _finite_number(h24.get("sells"))
        if buys is not None and sells is not None and buys >= 0 and sells >= 0:
            pressure = {
                "provider": PROVIDER,
                "window": "provider-reported rolling h24",
                "buys": buys,
                "sells": sells,
                "semantics": (
                    "Provider-reported pool transaction buy/sell counts; not USD volume, "
                    "wallet-level flow, or a signed order-flow measure."
                ),
            }
    return {
        "price_usd": price,
        "liquidity_usd": _finite_number(attrs.get("reserve_in_usd")),
        "volume_24h_usd": volume_h24,
        "buy_sell_pressure": pressure,
        "risk_status": "NOT_PROVIDED_BY_THIS_POOL_ENDPOINT",
    }, {"pool_id": pool_id, "base_token_address": base, "quote_token_address": quote}


def fetch_gecko_market(identity: dict[str, str], deadline: float,
                       state_root: Path, now_fn=utc_now,
                       on_request=None) -> dict[str, Any]:
    """Fetch exact-pool minute candles and provider-reported pool snapshot."""
    observations: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    state_path = collector.DEFAULT_OUTPUT_ROOT / ".gecko-cex-request-rate.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)

    def get(url: str, resolution: str, seconds: int) -> None:
        if time.monotonic() >= deadline:
            raise MonitorError("session duration limit reached before provider request")
        _reserve_provider_slot(state_path, deadline)
        if on_request:
            on_request()
        started = utc_iso(now_fn())
        payload = _request_json(url, timeout=_request_timeout(deadline))
        received = utc_iso(now_fn())
        _, attrs = _response_attributes(payload)
        raw_bars = attrs.get("ohlcv_list") or []
        bars = [
            bar for item in raw_bars
            if (bar := _normalise_bar(item, seconds, now_fn())) is not None
        ]
        bars.sort(key=lambda row: row["time_open"])
        observations.extend({
            "provider": PROVIDER,
            "resolution": resolution,
            "source_event_utc": bar["time_open"],
            "received_utc": received,
            "source_url": url,
            "status": "OBSERVED",
            "payload": bar,
        } for bar in bars)
        responses.append({
            "resolution": resolution,
            "request_started_utc": started,
            "received_utc": received,
            "source_url": url,
            "bar_count": len(bars),
        })

    pool_url = _pool_url(identity)
    if time.monotonic() >= deadline:
        raise MonitorError("session duration limit reached before provider request")
    _reserve_provider_slot(state_path, deadline)
    if on_request:
        on_request()
    started = utc_iso(now_fn())
    snapshot, pool_metadata = _validate_pool_payload(_request_json(
        pool_url, timeout=_request_timeout(deadline)
    ), identity)
    received = utc_iso(now_fn())
    observations.append({
        "provider": PROVIDER,
        "resolution": "pool_snapshot",
        "source_event_utc": None,
        "received_utc": received,
        "source_url": pool_url,
        "status": "OBSERVED",
        "payload": {
            **snapshot,
            **pool_metadata,
            "snapshot_timestamp_semantics": "local response-received time; provider event timestamp unavailable",
        },
    })
    responses.append({
        "resolution": "pool_snapshot",
        "request_started_utc": started,
        "received_utc": received,
        "source_url": pool_url,
        "bar_count": 1,
    })
    get(_ohlcv_url(identity, aggregate=1, limit=60), "1m", 60)
    get(_ohlcv_url(identity, aggregate=5, limit=300), "5m", 300)
    return {"observations": observations, "responses": responses, "market_type": "dex_pool"}


def fetch_cex_market(candidate: dict[str, Any], now: dt.datetime, deadline: float,
                     state_root: Path, on_request=None) -> dict[str, Any]:
    rate_path = collector.DEFAULT_OUTPUT_ROOT / ".gecko-cex-request-rate.json"
    rate_path.parent.mkdir(parents=True, exist_ok=True)
    fetcher = collector._BoundedVenueFetcher(
        rate_path, 3,
        lambda path, interval: _reserve_provider_slot(path, deadline, interval),
        deadline,
    )
    try:
        bars, url, error = monitor._fetch_cex_position_bars(candidate, venues, fetcher, now)
    finally:
        if on_request and fetcher.requests_started:
            on_request(fetcher.requests_started)
    if error:
        status = fetcher.log[-1].get("status") if fetcher.log else None
        if status in (403, 418, 429):
            raise ProviderStop(
                f"CEX venue provider returned HTTP {status}",
                {"provider": str(candidate["venue"]), "http_status": status},
            )
        raise MonitorError(error)
    observations = []
    for bar in bars:
        event_utc = bar.get("time_open")
        observations.append({
            "provider": f"cex:{candidate['venue']}",
            "resolution": "1h",
            "source_event_utc": event_utc,
            "received_utc": utc_iso(now),
            "source_url": url,
            "status": "OBSERVED",
            "payload": bar,
        })
    return {"observations": observations, "responses": [{
        "resolution": "1h",
        "received_utc": utc_iso(now),
        "source_url": url,
        "bar_count": len(observations),
    }], "market_type": "cex_spot"}


def _bar_values(connection: sqlite3.Connection, candidate_key: str) -> list[dict[str, Any]]:
    rows = connection.execute("""
        SELECT resolution, source_event_utc, payload_json FROM observations
        WHERE candidate_key=? AND status='OBSERVED' AND resolution IN ('1m', '5m', '1h')
          AND NOT EXISTS (
              SELECT 1 FROM observations conflict
              WHERE conflict.candidate_key=observations.candidate_key
                AND conflict.provider=observations.provider
                AND conflict.resolution=observations.resolution
                AND conflict.source_event_utc=observations.source_event_utc
                AND conflict.status='CONFLICTING_PROVIDER_DATA'
          )
        ORDER BY source_event_utc, resolution, event_id
    """, (candidate_key,)).fetchall()
    result = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        result.append({"resolution": row["resolution"], "event_utc": row["source_event_utc"], **payload})
    return result


def _missing_5m_intervals(bars: list[dict[str, Any]], start: dt.datetime,
                          end: dt.datetime) -> int:
    start_ts = math.ceil(start.timestamp() / 300) * 300
    end_ts = math.floor(end.timestamp() / 300) * 300
    if end_ts <= start_ts:
        return 0
    present = set()
    for bar in bars:
        if bar.get("resolution") != "5m":
            continue
        stamp = parse_utc(bar.get("event_utc") or bar.get("time_open"))
        if stamp is not None:
            present.add(int(stamp.timestamp()))
    expected = range(start_ts, end_ts, 300)
    return sum(1 for stamp in expected if stamp not in present)


def _store_observation(connection: sqlite3.Connection, candidate_key: str,
                       run_id: str, observation: dict[str, Any],
                       provenance: dict[str, str] | None = None) -> tuple[str, bool]:
    payload_json = _canonical_json(observation.get("payload") or {})
    source_event = observation.get("source_event_utc")
    provider = str(observation.get("provider") or PROVIDER)
    resolution = str(observation.get("resolution") or "unknown")
    event_id = _sha256("|".join((
        candidate_key, provider, resolution, str(source_event or ""),
        observation.get("received_utc") if source_event is None else "", payload_json,
    )))
    duplicate = connection.execute(
        "SELECT 1 FROM observations WHERE event_id=?", (event_id,)
    ).fetchone() is not None
    if duplicate:
        return event_id, True
    conflict = False
    if source_event is not None:
        prior = connection.execute("""
            SELECT payload_json FROM observations
            WHERE candidate_key=? AND provider=? AND resolution=? AND source_event_utc=?
        """, (candidate_key, provider, resolution, source_event)).fetchall()
        conflict = any(row["payload_json"] != payload_json for row in prior)
    status = "CONFLICTING_PROVIDER_DATA" if conflict else str(observation.get("status") or "OBSERVED")
    provenance = provenance or {}
    connection.execute("""
        INSERT INTO observations (
            event_id, candidate_key, collection_run_id, provider, resolution,
            source_event_utc, received_utc, source_url, status, payload_json,
            configuration_fingerprint, watchlist_sha256, scanner_version,
            source_configuration_fingerprint, source_run_id, candidate_source_event_utc,
            candidate_source_received_utc, source_provenance_status,
            source_freshness_status, phase4_score_snapshot_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        event_id, candidate_key, run_id, provider, resolution, source_event,
        str(observation.get("received_utc") or utc_iso()), observation.get("source_url"),
        status, payload_json, provenance.get("configuration_fingerprint", ""),
        provenance.get("watchlist_sha256", ""), provenance.get("scanner_version", "unknown"),
        provenance.get("source_configuration_fingerprint", ""),
        provenance.get("source_run_id", ""),
        provenance.get("candidate_source_event_utc", ""),
        provenance.get("candidate_source_received_utc", ""),
        provenance.get("source_provenance_status", "UNVERIFIED"),
        provenance.get("source_freshness_status", "UNVERIFIED"),
        provenance.get("phase4_score_snapshot_json", "{}"),
    ))
    return event_id, False


def update_lifecycle(connection: sqlite3.Connection, candidate_key: str, now: dt.datetime,
                     next_due: dt.datetime | None = None) -> dict[str, Any]:
    candidate = connection.execute(
        "SELECT * FROM candidates WHERE candidate_key=?", (candidate_key,)
    ).fetchone()
    bars = _bar_values(connection, candidate_key)
    entry = parse_utc(candidate["entry_utc"])
    expiry = parse_utc(candidate["expires_utc"])
    if entry is None or expiry is None:
        raise MonitorError("persisted candidate has invalid lifecycle timestamps")
    valid_bars = [
        row for row in bars
        if (stamp := parse_utc(row.get("time_close") or row.get("event_utc"))) is not None
        and entry <= stamp <= expiry
    ]
    first_valid = candidate["first_valid_observation_utc"]
    if first_valid is None and valid_bars:
        first_row = connection.execute("""
            SELECT MIN(received_utc) FROM observations
            WHERE candidate_key=? AND status='OBSERVED' AND resolution IN ('1m','5m','1h')
              AND source_event_utc>=? AND source_event_utc<=?
        """, (candidate_key, utc_iso(entry), utc_iso(expiry))).fetchone()
        first_valid = first_row[0] if first_row and first_row[0] else utc_iso(now)
    touches = []
    for row in valid_bars:
        high = _finite_number(row.get("high"))
        bar_open = parse_utc(row.get("time_open") or row.get("event_utc"))
        if (high is not None and high >= candidate["target_price"] and bar_open is not None
                and entry <= bar_open < expiry):
            touches.append(bar_open)
    first_touch = min(touches) if touches else parse_utc(candidate["first_target_touch_utc"])
    missing = _missing_5m_intervals(bars, entry, min(now, expiry))
    latest = None
    for row in reversed(bars):
        if row.get("resolution") not in ("1m", "5m", "1h"):
            continue
        if any(_finite_number(row.get(field)) is not None for field in ("close", "volume")):
            event_close = parse_utc(row.get("time_close") or row.get("event_utc"))
            age_limit = {
                "1m": dt.timedelta(minutes=5),
                "5m": dt.timedelta(minutes=15),
                "1h": dt.timedelta(hours=2),
            }[row["resolution"]]
            latest = {
                "price_usd": _finite_number(row.get("close")),
                "volume": _finite_number(row.get("volume")),
                "liquidity_usd": None,
                "risk_status": "NOT_PROVIDED_BY_CANDLE_SOURCE",
                "source_event_utc": row.get("event_utc"),
                "price_freshness_status": (
                    "FRESH" if event_close and now - event_close <= age_limit
                    else "STALE_PROVIDER_DATA"
                ),
            }
            break
    snapshots = connection.execute("""
        SELECT payload_json FROM observations WHERE candidate_key=? AND resolution='pool_snapshot'
        ORDER BY received_utc DESC, event_id DESC LIMIT 1
    """, (candidate_key,)).fetchone()
    if snapshots:
        snapshot = json.loads(snapshots["payload_json"])
        if latest is None:
            latest = {}
        latest.update({
            "liquidity_usd": snapshot.get("liquidity_usd"),
            "volume_24h_usd": snapshot.get("volume_24h_usd"),
            "buy_sell_pressure": snapshot.get("buy_sell_pressure"),
            "risk_status": snapshot.get("risk_status", "NOT_PROVIDED_BY_THIS_POOL_ENDPOINT"),
            "snapshot_received_utc": snapshot.get("snapshot_timestamp_semantics"),
        })
    previous_metrics = {}
    if candidate["latest_metrics_json"]:
        try:
            previous_metrics = json.loads(candidate["latest_metrics_json"])
        except json.JSONDecodeError:
            previous_metrics = {}
    if latest is not None:
        changes = {}
        for field in ("price_usd", "volume", "volume_24h_usd", "liquidity_usd"):
            before = _finite_number(previous_metrics.get(field))
            after = _finite_number(latest.get(field))
            if before is not None and after is not None:
                changes[field] = {
                    "previous": before,
                    "current": after,
                    "absolute_change": after - before,
                    "percent_change": (after - before) / abs(before) * 100.0 if before else None,
                }
        latest["changes_since_previous_observation"] = changes
        latest["risk_changes_status"] = "UNAVAILABLE_FROM_SELECTED_PROVIDER_ENDPOINTS"
    conflict_count = connection.execute("""
        SELECT COUNT(*) FROM observations WHERE candidate_key=? AND status='CONFLICTING_PROVIDER_DATA'
    """, (candidate_key,)).fetchone()[0]
    failure_count = connection.execute("""
        SELECT COUNT(*) FROM observations WHERE candidate_key=? AND status IN
            ('FETCH_FAILED', 'STALE_PROVIDER_DATA', 'PROVIDER_RATE_LIMITED', 'PROVIDER_ACCESS_DENIED')
    """, (candidate_key,)).fetchone()[0]
    status = candidate["status"]
    outcome = candidate["outcome"]
    if first_touch is not None and status in ("ACTIVE", "TARGET_TOUCHED"):
        status = "TARGET_TOUCHED"
        outcome = "TECHNICAL_SINGLE_TP_TOUCH_OBSERVED"
    if now >= expiry and status not in ("EXPIRED", "REJECTED"):
        if first_touch is not None:
            outcome = "EXPIRED_AFTER_TECHNICAL_TARGET_TOUCH"
        elif missing:
            outcome = "EXPIRED_INCONCLUSIVE_EVIDENCE_GAPS"
        else:
            outcome = "EXPIRED_WITH_COMPLETE_5M_COVERAGE_NO_TARGET_TOUCH"
        status = "EXPIRED"
    if status not in ("EXPIRED", "REJECTED"):
        status = "TARGET_TOUCHED" if first_touch is not None else "ACTIVE"
    connection.execute("""
        UPDATE candidates SET status=?, outcome=?, first_valid_observation_utc=?,
            first_target_touch_utc=?, next_due_utc=?, last_observation_utc=?,
            latest_metrics_json=?, missing_5m_intervals=?, conflict_count=?,
            failure_count=?, updated_utc=? WHERE candidate_key=?
    """, (
        status, outcome, first_valid,
        utc_iso(first_touch) if first_touch is not None else None,
        utc_iso(next_due) if next_due is not None and status != "EXPIRED" else None,
        utc_iso(now) if bars else candidate["last_observation_utc"],
        _canonical_json(latest) if latest is not None else candidate["latest_metrics_json"],
        missing, conflict_count, failure_count, utc_iso(now), candidate_key,
    ))
    connection.commit()
    return {
        "status": status,
        "outcome": outcome,
        "first_valid_observation_utc": first_valid,
        "first_target_touch_utc": utc_iso(first_touch) if first_touch is not None else None,
        "missing_5m_intervals": missing,
        "conflict_count": conflict_count,
        "failure_count": failure_count,
        "target_touch_is_executable_fill": False,
        "target_touch_time_precision": "candle interval only; exact touch time/order is unknown",
    }


def _config_fingerprint(args, watchlist_sha256: str) -> str:
    config = {
        "schema": STATE_SCHEMA,
        "interval_seconds": args.interval_seconds,
        "priority_interval_seconds": args.priority_interval_seconds,
        "priority_count": args.priority_count,
        "max_active_candidates": args.max_active_candidates,
        "provider_pacing_seconds": MIN_PROVIDER_PACING_SECONDS,
        "max_duration_minutes": args.duration_minutes,
        "watchlist_sha256": watchlist_sha256,
        "target_policy": "one technical single TP >= +40%; 24-hour horizon",
    }
    return _sha256(_canonical_json(config))


def _logical_state_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table, order in (
        ("candidates", "candidate_key"),
        ("observations", "event_id"),
        ("rejected_candidates", "rejection_id"),
        ("provider_state", "provider"),
    ):
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            digest.update(_canonical_json(dict(row)).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _actual_cadence_report(connection: sqlite3.Connection) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dt.datetime]] = {}
    rows = connection.execute("""
        SELECT candidate_key, resolution, received_utc
        FROM observations
        WHERE resolution IN ('1m','5m','1h','pool_snapshot','poll_attempt')
        GROUP BY candidate_key, resolution, received_utc
        ORDER BY candidate_key, resolution, received_utc
    """)
    for row in rows:
        stamp = parse_utc(row["received_utc"])
        if stamp is not None:
            grouped.setdefault((row["candidate_key"], row["resolution"]), []).append(stamp)
    report = {}
    for (candidate_key, resolution), stamps in sorted(grouped.items()):
        deltas = [(right - left).total_seconds() for left, right in zip(stamps, stamps[1:])]
        report[f"{candidate_key}:{resolution}"] = {
            "poll_count": len(stamps),
            "first_received_utc": utc_iso(stamps[0]),
            "last_received_utc": utc_iso(stamps[-1]),
            "mean_poll_interval_seconds": round(sum(deltas) / len(deltas), 3) if deltas else None,
            "minimum_poll_interval_seconds": min(deltas) if deltas else None,
            "maximum_poll_interval_seconds": max(deltas) if deltas else None,
        }
    return report


def _write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    manifest["state_digest_sha256"] = manifest.pop("_state_digest")
    content = _canonical_json(manifest)
    manifest["manifest_sha256"] = _sha256(content)
    temp = run_dir / "manifest.json.tmp"
    temp.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    temp.replace(run_dir / "manifest.json")


def _insert_failure(connection: sqlite3.Connection, run_id: str, candidate_key: str,
                    now: dt.datetime, status: str, message: str,
                    metadata: dict[str, Any] | None = None,
                    provider: str = PROVIDER,
                    provenance: dict[str, str] | None = None) -> str:
    observation = {
        "provider": provider,
        "resolution": "poll_attempt",
        "source_event_utc": None,
        "received_utc": utc_iso(now),
        "source_url": None,
        "status": status,
        "payload": {"error": message, "provider_response": metadata},
    }
    event_id, _ = _store_observation(
        connection, candidate_key, run_id, observation, provenance
    )
    connection.commit()
    return event_id


def _collect(args, *, now_fn=utc_now, sleep_fn=time.sleep, fetch_fn=None,
             run_id: str | None = None, lease_health_check=None) -> Path:
    lease_health_failed = False

    def check_lease_health() -> None:
        nonlocal lease_health_failed
        if lease_health_check is None:
            return
        try:
            lease_health_check()
        except run_contract.ResearchRunError:
            lease_health_failed = True
            raise

    check_lease_health()
    if not 1 <= args.duration_minutes <= MAX_SESSION_MINUTES:
        raise MonitorError(f"session duration must be 1-{MAX_SESSION_MINUTES} minutes")
    if not MIN_PROVIDER_PACING_SECONDS <= args.interval_seconds <= 3600:
        raise MonitorError("base collection interval must be 12-3600 seconds")
    if not MIN_PROVIDER_PACING_SECONDS <= args.priority_interval_seconds <= 3600:
        raise MonitorError("priority collection interval must be 12-3600 seconds")
    memory_dir = Path(args.memory_dir)
    watchlist_path = Path(args.watchlist)
    state_root = Path(args.state_root)
    _ensure_disjoint_paths(memory_dir, state_root)
    if watchlist_path.is_symlink() or not watchlist_path.is_file():
        raise MonitorError(f"watchlist must be a regular file: {watchlist_path}")
    raw_watchlist = watchlist_path.read_bytes()
    try:
        document = json.loads(raw_watchlist.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MonitorError(f"cannot read watchlist JSON: {watchlist_path}") from exc
    input_checked_at = now_fn()
    positions, watchlist_provenance = validate_watchlist_document(document, input_checked_at)
    invalid_rows = [row for row in positions if not isinstance(row, dict)]
    positions = [row for row in positions if isinstance(row, dict)]
    watchlist_sha256 = _sha256(raw_watchlist)
    check_lease_health()
    state_root.mkdir(parents=True, exist_ok=True)
    if state_root.is_symlink():
        raise MonitorError(f"monitoring state directory must not be a symlink: {state_root}")
    run_id = run_id or f"{now_fn().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    run_dir = state_root / f"session-{run_id}"
    run_dir.mkdir(exist_ok=False)
    config_fingerprint = _config_fingerprint(args, watchlist_sha256)
    started = now_fn()
    deadline = time.monotonic() + args.duration_minutes * 60
    counts: dict[str, int] = {}
    record_ids: list[str] = []
    stop_status = "DURATION_ELAPSED"
    provider_metadata = None
    requests_started = 0
    duplicate_records = 0
    monitor_version = collector._read_scanner_version()
    provenance = {
        "configuration_fingerprint": config_fingerprint,
        "watchlist_sha256": watchlist_sha256,
    }

    def count_request(count: int = 1) -> None:
        nonlocal requests_started
        requests_started += count

    db_path = state_root / "monitor-state.sqlite3"
    check_lease_health()
    connection = open_state(db_path)
    accepted = rejected = duplicate_candidates = 0

    def finalize_session(finished: dt.datetime) -> None:
        nonlocal stop_status
        actual_record_count = connection.execute(
            "SELECT COUNT(*) FROM observations WHERE collection_run_id=?", (run_id,)
        ).fetchone()[0]
        if actual_record_count != len(record_ids):
            connection.rollback()
            stop_status = "FAILED"
            return
        check_lease_health()
        connection.commit()
        candidates_summary = {
            row["status"]: row["count"] for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM candidates GROUP BY status ORDER BY status"
            )
        }
        rejected_candidate_reasons = {
            row["reason"]: row["count"] for row in connection.execute(
                "SELECT reason, COUNT(*) AS count FROM rejected_candidates "
                "WHERE collection_run_id=? GROUP BY reason ORDER BY reason",
                (run_id,),
            )
        }
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "run_lifecycle_schema": "rocket-hunter.research-run.v1",
            "activity_run_id": run_id,
            "state_schema": STATE_SCHEMA,
            "status": stop_status,
            "collection_run_id": run_id,
            "started_utc": utc_iso(started),
            "finished_utc": utc_iso(finished),
            "duration_seconds": round((finished - started).total_seconds(), 3),
            "max_session_minutes": MAX_SESSION_MINUTES,
            "configuration_fingerprint": config_fingerprint,
            "watchlist_sha256": watchlist_sha256,
            "source_watchlist_provenance": watchlist_provenance,
            "watchlist_candidate_count": len(positions) + len(invalid_rows),
            "accepted_candidate_count": accepted,
            "rejected_candidate_count": rejected,
            "rejected_candidate_reasons": rejected_candidate_reasons,
            "duplicate_candidate_count": duplicate_candidates,
            "candidate_status_counts": candidates_summary,
            "record_count": len(record_ids),
            "actual_record_count": actual_record_count,
            "record_count_validation": (
                "MATCH" if actual_record_count == len(record_ids) else "FAILED"
            ),
            "duplicate_record_count": duplicate_records,
            "requests_started": requests_started,
            "record_ids_sha256": _sha256("\n".join(sorted(record_ids))),
            "record_status_counts": dict(sorted(counts.items())),
            "evidence_states": [
                "STALE", "MISSING", "CONFLICTING", "FAILED", "PENDING",
            ],
            "provider": PROVIDER,
            "scanner_version": watchlist_provenance["scanner_version"],
            "monitor_version": monitor_version,
            "provider_pacing_minimum_seconds": MIN_PROVIDER_PACING_SECONDS,
            "provider_rate_limit": provider_metadata,
            "reproducibility": {
                "database_logical_state_digest": "state_digest_sha256",
                "record_digest": "SHA-256 over sorted immutable event IDs inserted in this session",
                "watchlist_digest": "SHA-256 of exact source watchlist bytes",
                "manifest_digest": "SHA-256 of canonical manifest contents before manifest_sha256",
            },
            "actual_observation_resolutions": ["DEX GeckoTerminal: 1m, 5m; CEX venue adapter: 1h"],
            "requested_cadence_seconds": {
                "highest_scored_candidates": args.priority_interval_seconds,
                "remaining_active_candidates": args.interval_seconds,
            },
            "actual_achieved_cadence": _actual_cadence_report(connection),
            "provider_semantics": {
                "buy_sell_pressure": (
                    "GeckoTerminal pool h24 transaction buy/sell counts; not USD volume, "
                    "wallet flow, or a signed order-flow measure."
                ),
                "liquidity_timestamp": "provider pool snapshot; local received time retained, source event time unavailable",
                "risk_changes": "not available from the selected pool/candle endpoints; recorded as unavailable",
            },
            "lifecycle_policy": {
                "target": "one technical single_take_profit >= +40.00%",
                "horizon_hours": 24,
                "target_touch_is_fill": False,
                "target_touch_time_precision": "candle interval only; exact touch time/order is unknown",
                "stop_loss": "NONE",
                "trailing_stop": "NONE",
                "missing_5m_intervals_fail_closed": True,
                "candidate_capacity_overflow": "persistent QUEUED state; never silently truncated",
                "recovery": (
                    "SQLite WAL-backed candidates and immutable observations survive restart; "
                    "an interrupted session remains pending and is never resumed"
                ),
                "identity": "(chain, token_mint, pool_address)",
                "timestamps": (
                    "source_event_utc is provider event time; received_utc is local ingestion time"
                ),
            },
            "network_access": "read-only public provider data",
            "_state_digest": _logical_state_digest(connection),
        }
        check_lease_health()
        _write_manifest(run_dir, manifest)

    try:
        check_lease_health()
        accepted, rejected, duplicate_candidates = ingest_candidates(
            connection, positions, run_id, started
        )
        for invalid in invalid_rows:
            _record_rejection(connection, run_id, started, invalid, "candidate row must be a JSON object")
        rejected += len(invalid_rows)
        check_lease_health()
        connection.commit()
        cooldown = connection.execute(
            "SELECT * FROM provider_state ORDER BY provider"
        ).fetchall()
        active_cooldowns = [
            row for row in cooldown
            if parse_utc(row["cooldown_until_utc"])
            and parse_utc(row["cooldown_until_utc"]) > started
        ]
        if active_cooldowns:
            provider_metadata = {
                "active_cooldowns": [
                    {
                        "provider": row["provider"],
                        "http_status": row["last_http_status"],
                        "cooldown_until_utc": row["cooldown_until_utc"],
                    }
                    for row in active_cooldowns
                ],
                "source": "provider Retry-After response headers",
            }
            stop_status = "PROVIDER_COOLDOWN_ACTIVE"
        expired_rows = connection.execute("""
            SELECT candidate_key FROM candidates
            WHERE status IN ('ACTIVE','TARGET_TOUCHED') AND expires_utc<=?
        """, (utc_iso(started),)).fetchall()
        for row in expired_rows:
            update_lifecycle(connection, row["candidate_key"], started)
        promote_queued(connection, started, args.max_active_candidates)
        once_passed: set[str] = set()
        while stop_status != "PROVIDER_COOLDOWN_ACTIVE" and time.monotonic() < deadline:
            check_lease_health()
            now = now_fn()
            expired_rows = connection.execute("""
                SELECT candidate_key FROM candidates
                WHERE status IN ('ACTIVE','TARGET_TOUCHED') AND expires_utc<=?
            """, (utc_iso(now),)).fetchall()
            for row in expired_rows:
                update_lifecycle(connection, row["candidate_key"], now)
            promote_queued(connection, now, args.max_active_candidates)
            due_rows = connection.execute("""
                SELECT * FROM candidates
                WHERE status IN ('ACTIVE', 'TARGET_TOUCHED') AND
                    (next_due_utc IS NULL OR next_due_utc<=?)
                ORDER BY CASE WHEN score IS NULL THEN 1 ELSE 0 END, score DESC,
                    COALESCE(next_due_utc, entry_utc), candidate_key
            """, (utc_iso(now),)).fetchall()
            if not due_rows:
                promote_queued(connection, now, args.max_active_candidates)
                due_rows = connection.execute("""
                    SELECT * FROM candidates
                    WHERE status IN ('ACTIVE', 'TARGET_TOUCHED') AND
                        (next_due_utc IS NULL OR next_due_utc<=?)
                    ORDER BY CASE WHEN score IS NULL THEN 1 ELSE 0 END, score DESC,
                        COALESCE(next_due_utc, entry_utc), candidate_key
                """, (utc_iso(now),)).fetchall()
            if not due_rows:
                upcoming = connection.execute("""
                    SELECT MIN(next_due_utc) FROM candidates
                    WHERE status IN ('ACTIVE', 'TARGET_TOUCHED') AND next_due_utc IS NOT NULL
                """).fetchone()[0]
                if args.once or not upcoming:
                    stop_status = "ONE_CYCLE_COMPLETED" if args.once else "NO_ACTIVE_CANDIDATES"
                    break
                delay = max(1.0, min(30.0, (parse_utc(upcoming) - now).total_seconds()))
                sleep_fn(min(delay, max(0.0, deadline - time.monotonic())))
                continue
            for candidate in due_rows:
                check_lease_health()
                if time.monotonic() >= deadline:
                    stop_status = "DURATION_ELAPSED"
                    break
                candidate_key = candidate["candidate_key"]
                if args.once and candidate_key in once_passed:
                    continue
                once_passed.add(candidate_key)
                candidate_json = json.loads(candidate["candidate_json"])
                candidate_source_provenance = json.loads(candidate["source_provenance_json"])
                candidate_score_snapshot = candidate["phase4_score_snapshot_json"]
                observation_provenance = {
                    **provenance,
                    "scanner_version": candidate_source_provenance["scanner_version"],
                    "source_configuration_fingerprint": candidate_source_provenance["configuration_fingerprint"],
                    "source_run_id": candidate_source_provenance["source_run_id"],
                    "candidate_source_event_utc": candidate_source_provenance["source_event_utc"],
                    "candidate_source_received_utc": candidate_source_provenance["source_received_utc"],
                    "source_provenance_status": candidate["source_provenance_status"],
                    "source_freshness_status": candidate["source_freshness_status"],
                    "phase4_score_snapshot_json": candidate_score_snapshot,
                }
                identity = json.loads(candidate["identity_json"])
                poll_started_utc = now_fn()
                requests_before = requests_started
                try:
                    check_lease_health()
                    if fetch_fn is not None:
                        result = fetch_fn(candidate_json, identity, deadline)
                    elif identity["market_type"] == "dex_pool":
                        result = fetch_gecko_market(
                            identity, deadline, state_root, now_fn, count_request
                        )
                    else:
                        result = fetch_cex_market(
                            candidate_json, now_fn(), deadline, state_root, count_request
                        )
                    check_lease_health()
                    observations = result.get("observations", [])
                    response_rows = result.get("responses", [])
                    check_lease_health()
                    poll_event_id, poll_duplicate = _store_observation(
                        connection, candidate_key, run_id, {
                            "provider": PROVIDER if identity["market_type"] == "dex_pool"
                            else f"cex:{candidate_json.get('venue')}",
                            "resolution": "poll_attempt",
                            "source_event_utc": None,
                            "received_utc": utc_iso(now_fn()),
                            "source_url": None,
                            "status": "POLL_COMPLETED",
                            "payload": {
                                "request_count": requests_started - requests_before,
                                "responses": response_rows,
                                "observation_count": len(observations),
                            },
                        }, observation_provenance,
                    )
                    if not poll_duplicate:
                        record_ids.append(poll_event_id)
                    else:
                        duplicate_records += 1
                    for response in result.get("responses", []):
                        if response.get("bar_count") == 0:
                            observations.append({
                                "provider": PROVIDER if identity["market_type"] == "dex_pool"
                                else f"cex:{candidate_json.get('venue')}",
                                "resolution": response.get("resolution", "unknown"),
                                "source_event_utc": None,
                                "received_utc": response.get("received_utc") or utc_iso(now_fn()),
                                "source_url": response.get("source_url"),
                                "status": "NO_COMPLETED_BARS",
                                "payload": {"bar_count": 0, "reason": "provider returned no completed candles"},
                            })
                    inserted = 0
                    for observation in observations:
                        check_lease_health()
                        event_id, duplicate = _store_observation(
                            connection, candidate_key, run_id, observation, observation_provenance
                        )
                        if not duplicate:
                            record_ids.append(event_id)
                            inserted += 1
                        else:
                            duplicate_records += 1
                    counts["observed"] = counts.get("observed", 0) + inserted
                    for resolution, age_limit in (
                        ("1m", dt.timedelta(minutes=5)),
                        ("5m", dt.timedelta(minutes=15)),
                        ("1h", dt.timedelta(hours=2)),
                    ):
                        latest_bar = max(
                            (
                                parse_utc(
                                    item.get("time_close")
                                    or (item.get("payload") or {}).get("time_close")
                                )
                                for item in observations
                                if item.get("resolution") == resolution
                                and parse_utc(
                                    item.get("time_close")
                                    or (item.get("payload") or {}).get("time_close")
                                )
                            ),
                            default=None,
                        )
                        if latest_bar is not None and now_fn() - latest_bar > age_limit:
                            check_lease_health()
                            event_id = _insert_failure(
                                connection, run_id, candidate_key, now_fn(),
                                "STALE_PROVIDER_DATA",
                                f"latest completed {resolution} candle is older than {age_limit.total_seconds():g} seconds",
                                provenance=observation_provenance,
                            )
                            record_ids.append(event_id)
                            counts["stale"] = counts.get("stale", 0) + 1
                    check_lease_health()
                    connection.commit()
                    score = candidate["score"]
                    rank = connection.execute("""
                        SELECT COUNT(*) FROM candidates c
                        WHERE c.status IN ('ACTIVE','TARGET_TOUCHED') AND
                            (c.score>? OR (c.score=? AND c.candidate_key<?))
                    """, (score, score, candidate_key)).fetchone()[0] if score is not None else args.priority_count
                    interval = (
                        args.priority_interval_seconds
                        if score is not None and rank < args.priority_count
                        else args.interval_seconds
                    )
                    next_due = poll_started_utc + dt.timedelta(seconds=interval)
                    check_lease_health()
                    lifecycle = update_lifecycle(connection, candidate_key, now_fn(), next_due)
                    counts[lifecycle["status"].lower()] = counts.get(lifecycle["status"].lower(), 0) + 1
                except ProviderStop as exc:
                    check_lease_health()
                    provider_metadata = exc.metadata
                    event_id = _insert_failure(
                        connection, run_id, candidate_key, now_fn(),
                        "PROVIDER_RATE_LIMITED" if exc.metadata.get("http_status") in (418, 429)
                        else "PROVIDER_ACCESS_DENIED", str(exc), exc.metadata,
                        str(exc.metadata.get("provider") or PROVIDER),
                        observation_provenance,
                    )
                    record_ids.append(event_id)
                    status = int(exc.metadata.get("http_status") or 0)
                    provider_name = str(exc.metadata.get("provider") or PROVIDER)
                    if status in (418, 429):
                        cooldown_until = parse_utc(exc.metadata.get("cooldown_until_utc"))
                        if cooldown_until is not None:
                            connection.execute("""
                                INSERT INTO provider_state(provider, cooldown_until_utc, last_http_status, updated_utc)
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(provider) DO UPDATE SET cooldown_until_utc=excluded.cooldown_until_utc,
                                    last_http_status=excluded.last_http_status, updated_utc=excluded.updated_utc
                            """, (provider_name, utc_iso(cooldown_until), status, utc_iso(now_fn())))
                        stop_status = "PROVIDER_RATE_LIMITED"
                    else:
                        cooldown_until = parse_utc(exc.metadata.get("cooldown_until_utc"))
                        if cooldown_until is not None:
                            connection.execute("""
                                INSERT INTO provider_state(provider, cooldown_until_utc, last_http_status, updated_utc)
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(provider) DO UPDATE SET cooldown_until_utc=excluded.cooldown_until_utc,
                                    last_http_status=excluded.last_http_status, updated_utc=excluded.updated_utc
                            """, (provider_name, utc_iso(cooldown_until), status, utc_iso(now_fn())))
                        stop_status = "PROVIDER_ACCESS_DENIED"
                    check_lease_health()
                    connection.commit()
                    counts[stop_status.lower()] = counts.get(stop_status.lower(), 0) + 1
                    break
                except MonitorError as exc:
                    check_lease_health()
                    event_id = _insert_failure(
                        connection, run_id, candidate_key, now_fn(), "FETCH_FAILED", str(exc),
                        provider=PROVIDER if identity["market_type"] == "dex_pool"
                        else f"cex:{candidate_json.get('venue')}",
                        provenance=observation_provenance,
                    )
                    record_ids.append(event_id)
                    counts["fetch_failed"] = counts.get("fetch_failed", 0) + 1
                    lifecycle = update_lifecycle(connection, candidate_key, now_fn())
                if stop_status in ("PROVIDER_RATE_LIMITED", "PROVIDER_ACCESS_DENIED"):
                    break
            if stop_status in ("PROVIDER_RATE_LIMITED", "PROVIDER_ACCESS_DENIED"):
                break
            if args.once:
                stop_status = "ONE_CYCLE_COMPLETED"
                break
            if time.monotonic() < deadline:
                sleep_fn(min(1.0, max(0.0, deadline - time.monotonic())))
        else:
            if stop_status != "PROVIDER_COOLDOWN_ACTIVE":
                stop_status = "DURATION_ELAPSED"
    except KeyboardInterrupt:
        stop_status = "INTERRUPTED"
    except Exception:
        stop_status = "FAILED"
        raise
    finally:
        finished = now_fn()
        try:
            if lease_health_failed or stop_status in ("FAILED", "INTERRUPTED"):
                connection.rollback()
            else:
                check_lease_health()
                finalize_session(finished)
        finally:
            connection.close()
    print(f"Continuous monitoring {stop_status}: {len(record_ids)} new records; {run_dir}")
    return run_dir


def collect(args, *, now_fn=utc_now, sleep_fn=time.sleep, fetch_fn=None) -> Path:
    if not 1 <= args.duration_minutes <= MAX_SESSION_MINUTES:
        raise MonitorError(f"session duration must be 1-{MAX_SESSION_MINUTES} minutes")
    if not MIN_PROVIDER_PACING_SECONDS <= args.interval_seconds <= 3600:
        raise MonitorError("base collection interval must be 12-3600 seconds")
    if not MIN_PROVIDER_PACING_SECONDS <= args.priority_interval_seconds <= 3600:
        raise MonitorError("priority collection interval must be 12-3600 seconds")
    run_id = f"{now_fn().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    state_root = Path(args.state_root)
    memory_dir = Path(args.memory_dir)
    _ensure_disjoint_paths(memory_dir, state_root)
    activity_db = Path(args.activity_db) if args.activity_db else state_root / "monitor-state.sqlite3"
    run_contract.ensure_activity_db_disjoint(activity_db, [
        memory_dir, collector.DEFAULT_EXPORTS, collector.DEFAULT_ANALYSIS,
        collector.DEFAULT_RUNS,
    ])
    watchlist_path = Path(args.watchlist)
    if watchlist_path.is_symlink() or not watchlist_path.is_file():
        raise MonitorError(f"watchlist must be a regular file: {watchlist_path}")
    try:
        watchlist_document = json.loads(watchlist_path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MonitorError(f"cannot read watchlist JSON: {watchlist_path}") from exc
    validate_watchlist_document(watchlist_document, now_fn())
    lease = run_contract.ResearchRunLease(
        activity_db, run_id, "continuous-monitor",
        int(args.duration_minutes * 60),
    ).acquire()
    try:
        run_dir = _collect(
            args, now_fn=now_fn, sleep_fn=sleep_fn, fetch_fn=fetch_fn, run_id=run_id,
            lease_health_check=lease.check_health,
        )
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            lease.finish("FAILED", "monitor output is incomplete; session remains manifestless and pending")
            return run_dir
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        lease.finish(str(manifest.get("status") or "FAILED"))
        return run_dir
    except BaseException as exc:
        lease.finish(
            "INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "FAILED",
            f"{type(exc).__name__}: {exc}",
        )
        raise


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-dir", type=Path, default=ROOT / "rocket-hunter-data" / "scanner-memory")
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--activity-db", type=Path, default=None,
                        help="shared SQLite scanner/collector lifecycle database")
    parser.add_argument("--duration-minutes", type=int, default=60,
                        help=f"bounded foreground session, 1-{MAX_SESSION_MINUTES} minutes")
    parser.add_argument("--interval-seconds", type=int, default=BASE_INTERVAL_DEFAULT,
                        help="base candidate collection cadence")
    parser.add_argument("--priority-interval-seconds", type=int, default=PRIORITY_INTERVAL_DEFAULT,
                        help="cadence for the highest-scored active candidates")
    parser.add_argument("--priority-count", type=int, default=3,
                        help="number of highest-scored candidates to prioritize")
    parser.add_argument("--max-active-candidates", type=int, default=MAX_ACTIVE_CANDIDATES,
                        help="active candidate cap; overflow remains persistently queued")
    parser.add_argument("--once", action="store_true", help="collect each due active candidate at most once")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.duration_minutes <= MAX_SESSION_MINUTES:
        parser.error(f"--duration-minutes must be 1-{MAX_SESSION_MINUTES}")
    if args.interval_seconds < MIN_PROVIDER_PACING_SECONDS:
        parser.error(f"--interval-seconds must be at least {MIN_PROVIDER_PACING_SECONDS}")
    if args.priority_interval_seconds < MIN_PROVIDER_PACING_SECONDS:
        parser.error(f"--priority-interval-seconds must be at least {MIN_PROVIDER_PACING_SECONDS}")
    if args.priority_count < 0:
        parser.error("--priority-count must be nonnegative")
    if not 1 <= args.max_active_candidates <= MAX_ACTIVE_CANDIDATES:
        parser.error(f"--max-active-candidates must be 1-{MAX_ACTIVE_CANDIDATES}")
    try:
        collect(args)
    except (MonitorError, run_contract.ResearchRunError, OSError, sqlite3.Error) as exc:
        print(f"Continuous monitoring failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
