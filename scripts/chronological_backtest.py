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
from urllib.parse import urlparse

BACKTEST_VERSION = "4.30.0"
MANDATORY_TARGET_PCT = 40.0

RECOGNIZED_TRACE_PROVIDERS = {
    "geckoterminal",
    "dexscreener",
    "birdeye",
    "dexpaprika",
    "binance",
    "coinbase",
    "kraken",
    "bybit",
    "okx",
    "kucoin",
    "gateio",
}

PROVIDER_ALIASES: dict[str, str] = {
    "geckoterminal": "geckoterminal",
    "gecko_terminal": "geckoterminal",
    "gecko-terminal": "geckoterminal",
    "dexscreener": "dexscreener",
    "dex_screener": "dexscreener",
    "dex-screener": "dexscreener",
    "birdeye": "birdeye",
    "bird_eye": "birdeye",
    "bird-eye": "birdeye",
    "dexpaprika": "dexpaprika",
    "dex_paprika": "dexpaprika",
    "dex-paprika": "dexpaprika",
    "binance": "binance",
    "coinbase": "coinbase",
    "kraken": "kraken",
    "bybit": "bybit",
    "okx": "okx",
    "kucoin": "kucoin",
    "gateio": "gateio",
    "gate_io": "gateio",
    "gate-io": "gateio",
    "gate.io": "gateio",
}

PROVIDER_DOMAIN_ALLOWLIST: dict[str, set[str]] = {
    "geckoterminal": {"geckoterminal.com", "api.geckoterminal.com"},
    "dexscreener": {"dexscreener.com", "api.dexscreener.com", "io.dexscreener.com"},
    "birdeye": {"birdeye.so", "public-api.birdeye.so"},
    "dexpaprika": {"dexpaprika.com", "api.dexpaprika.com"},
    "binance": {"binance.com", "api.binance.com", "data-api.binance.vision"},
    "coinbase": {"coinbase.com", "api.coinbase.com", "api.exchange.coinbase.com"},
    "kraken": {"kraken.com", "api.kraken.com"},
    "bybit": {"bybit.com", "api.bybit.com"},
    "okx": {"okx.com", "www.okx.com", "aws.okx.com"},
    "kucoin": {"kucoin.com", "api.kucoin.com"},
    "gateio": {"gate.io", "api.gateio.ws", "www.gate.io"},
}

CANDLE_INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

CANDLE_TRACE_SCHEMA = {
    "required_fields": [
        "provider",            # recognized exact data source identifier
        "source_ref",          # reference URL with approved domain
        "retrieved_utc",       # timestamp of evidence retrieval
        "market_key",          # exact canonical market identity
        "candle_interval",     # declared candle resolution (1m, 5m, 15m, 30m, 1h, 4h, 1d)
        "trace_start_utc",     # trace window start ISO timestamp
        "trace_end_utc",       # trace window end ISO timestamp
        "candles",             # non-empty array of timestamped OHLC observations
        "captured_response",   # immutable captured provider HTTP response evidence
    ],
    "candle_fields": ["timestamp_utc", "open", "high", "low", "close"],
    "response_evidence_fields": ["request_url", "http_status", "content_hash", "raw_body", "retrieval_timestamp_utc"],
    "recognized_providers": sorted(RECOGNIZED_TRACE_PROVIDERS),
    "provider_domain_allowlist": {k: sorted(v) for k, v in PROVIDER_DOMAIN_ALLOWLIST.items()},
    "recognized_intervals": list(CANDLE_INTERVAL_SECONDS.keys()),
}


def extract_candles_from_response_payload(payload: Any) -> list[dict[str, Any]] | None:
    """Extract candle observation dictionaries from standard provider response payloads.

    Supports:
    - Direct candles list: {"candles": [{"timestamp_utc": ..., "open": ..., ...}, ...]}
    - Nested data: {"data": {"candles": [...]}}
    - GeckoTerminal format: {"data": {"attributes": {"ohlcv_list": [[ts, o, h, l, c, v], ...]}}}
    - DexScreener format: {"pairs": [{"candles": [...]}]} or {"candles": [...]}
    - Binance/Bybit format: [[ts_ms, "open", "high", "low", "close", ...], ...]
    - Raw candle array: [{"t": ..., "o": ..., "h": ..., "l": ..., "c": ...}, ...]
    """
    if isinstance(payload, dict):
        if "candles" in payload and isinstance(payload["candles"], list):
            return payload["candles"]
        if "data" in payload and isinstance(payload["data"], dict):
            if "candles" in payload["data"] and isinstance(payload["data"]["candles"], list):
                return payload["data"]["candles"]
            attrs = payload["data"].get("attributes")
            if isinstance(attrs, dict) and "ohlcv_list" in attrs and isinstance(attrs["ohlcv_list"], list):
                extracted = []
                for entry in attrs["ohlcv_list"]:
                    if isinstance(entry, (list, tuple)) and len(entry) >= 5:
                        ts = entry[0]
                        ts_str = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat() if isinstance(ts, (int, float)) else str(ts)
                        extracted.append({
                            "timestamp_utc": ts_str,
                            "open": entry[1], "high": entry[2], "low": entry[3], "close": entry[4],
                        })
                return extracted
        if "pairs" in payload and isinstance(payload["pairs"], list) and len(payload["pairs"]) > 0:
            first_pair = payload["pairs"][0]
            if isinstance(first_pair, dict) and "candles" in first_pair and isinstance(first_pair["candles"], list):
                return first_pair["candles"]

    if isinstance(payload, list):
        if len(payload) == 0:
            return []
        if isinstance(payload[0], dict):
            return payload
        if isinstance(payload[0], (list, tuple)):
            extracted = []
            for entry in payload:
                if len(entry) >= 5:
                    ts = entry[0]
                    if isinstance(ts, (int, float)):
                        ts_s = ts / 1000.0 if ts > 1e11 else float(ts)
                        ts_str = dt.datetime.fromtimestamp(ts_s, tz=dt.timezone.utc).isoformat()
                    else:
                        ts_str = str(ts)
                    extracted.append({
                        "timestamp_utc": ts_str,
                        "open": entry[1], "high": entry[2], "low": entry[3], "close": entry[4],
                    })
            return extracted
    return None


def normalize_trace_provider(raw_prov: Any) -> str | None:
    """Extract and normalize recognized data provider from exact identifier.

    Rejects substring matching, arbitrary URLs, and fabricated identifiers.
    """
    if not raw_prov or not isinstance(raw_prov, str):
        return None
    cleaned = raw_prov.strip().lower()
    # Reject URLs and paths passed as provider identifiers
    if "://" in cleaned or "/" in cleaned or "\\" in cleaned:
        return None
    return PROVIDER_ALIASES.get(cleaned)


def parse_and_validate_url(url_val: Any) -> tuple[bool, str | None, str]:
    """Strictly parse and validate an HTTPS URL for request_url and source_ref.

    Requirements:
    - Must be a non-empty string.
    - Scheme must be strictly 'https'. Rejects HTTP, FTP, protocol-relative ('//...'), and missing scheme.
    - Parsed using urllib.parse.urlparse() with parsed .hostname extracted.
    - Credentials (username, password, '@' in authority) are rejected.
    - Invalid ports (non-numeric, negative, out of 1-65535 range, or malformed) are rejected.
    - Missing hostnames, malformed authorities, and deceptive hostnames are rejected.
    - No manual colon splitting.

    Returns (is_valid, hostname, diagnostic_reason).
    """
    if url_val is None or not isinstance(url_val, str):
        return False, None, "MISSING_URL"
    s = url_val.strip()
    if not s:
        return False, None, "MISSING_URL"

    # Reject protocol-relative URLs
    if s.startswith("//"):
        return False, None, "PROTOCOL_RELATIVE_URL_DISALLOWED"

    # Must contain explicit scheme separator
    if "://" not in s:
        return False, None, "MISSING_SCHEME"

    try:
        parsed = urlparse(s)
    except Exception as exc:
        return False, None, f"MALFORMED_URL: {exc}"

    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        if scheme == "http":
            return False, None, "UNSUPPORTED_SCHEME_HTTP"
        if scheme == "ftp":
            return False, None, "UNSUPPORTED_SCHEME_FTP"
        return False, None, f"INVALID_SCHEME_{scheme.upper()}"

    # Check for credentials in authority
    if parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or ""):
        return False, None, "CREDENTIALS_DISALLOWED"

    # Check for invalid port
    try:
        port = parsed.port
        if port is not None and not (1 <= port <= 65535):
            return False, None, "INVALID_PORT"
    except ValueError:
        return False, None, "INVALID_PORT"

    # Extract parsed.hostname (in lowercase)
    hostname = parsed.hostname
    if not hostname or not isinstance(hostname, str) or not hostname.strip():
        return False, None, "MISSING_HOSTNAME"

    hostname = hostname.strip().lower()

    # Reject malformed authority or deceptive characters
    if any(c in hostname for c in (" ", "\t", "\r", "\n", "\\", "/", "?", "#", "@", ":")):
        return False, None, "MALFORMED_AUTHORITY"

    if hostname.startswith(".") or hostname.endswith(".") or ".." in hostname:
        return False, None, "MALFORMED_HOSTNAME"

    labels = hostname.split(".")
    if len(labels) < 2:
        return False, None, "INVALID_HOSTNAME"

    for lbl in labels:
        if not lbl or len(lbl) > 63 or lbl.startswith("-") or lbl.endswith("-"):
            return False, None, "MALFORMED_HOSTNAME"
        if not all(c.isalnum() or c == "-" for c in lbl):
            return False, None, "MALFORMED_HOSTNAME"

    return True, hostname, "OK"


def extract_source_domain(source_ref: Any) -> str | None:
    """Extract and validate lowercase network hostname/domain from a source reference or URL.

    Strictly requires HTTPS for URLs, rejects credentials, invalid ports, and deceptive hostnames.
    """
    if not source_ref or not isinstance(source_ref, str):
        return None
    s = source_ref.strip()
    if not s:
        return None

    # If it represents a URL (contains scheme or starts with //)
    if "://" in s or s.startswith("//"):
        is_valid, host, _ = parse_and_validate_url(s)
        return host if is_valid else None

    # If it is a bare domain, validate hostname format strictly without colon splitting
    if "@" in s or ":" in s or "/" in s or "\\" in s or " " in s:
        return None
    labels = s.lower().split(".")
    if len(labels) < 2:
        return None
    for lbl in labels:
        if not lbl or len(lbl) > 63 or lbl.startswith("-") or lbl.endswith("-"):
            return None
        if not all(c.isalnum() or c == "-" for c in lbl):
            return None
    return s.lower()


def validate_provider_source_domain(provider: str, domain: str) -> tuple[bool, str]:
    """Verify that complete hostname matches provider allowlist and detect provider/domain mismatch.

    Validates complete hostname against provider allowlist without manual colon splitting.
    Returns (is_valid, status_code).
    """
    if not provider or not domain:
        return False, "UNAPPROVED_SOURCE_DOMAIN"

    dom = domain.strip().lower()
    allowed_domains = PROVIDER_DOMAIN_ALLOWLIST.get(provider, set())
    if dom in allowed_domains:
        return True, "OK"

    # Check if domain belongs to another recognized provider
    for other_prov, other_domains in PROVIDER_DOMAIN_ALLOWLIST.items():
        if other_prov != provider and dom in other_domains:
            return False, "PROVIDER_DOMAIN_MISMATCH"

    return False, "UNAPPROVED_SOURCE_DOMAIN"

try:
    from scripts.address_identity import validate_and_build_identity
except ImportError:
    from address_identity import validate_and_build_identity


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


def _clean_strictly_positive_num(val: Any) -> float | None:
    """Validate and return float only if numeric, finite, and strictly positive (> 0).

    Rejects booleans, None, empty strings, NaN, infinity, zero, and negative numbers.
    """
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        v = float(val)
        if math.isnan(v) or math.isinf(v) or v <= 0.0:
            return None
        return v
    except (ValueError, TypeError):
        return None


class ChronologicalBacktestEvaluator:
    """Evaluates predictive models using chronological walk-forward out-of-sample windows."""

    def __init__(self, records: list[dict[str, Any]], *, config_fingerprint: str = "default"):
        self.records = records or []
        self.config_fingerprint = config_fingerprint
        self.diagnostics: dict[str, Any] = {
            "total_historical_records": len(self.records),
            "valid_exact_identity_records": 0,
            "missing_identity_records": 0,
            "missing_identity_by_reason": collections.Counter(),
            "conflicting_identity_records": 0,
            "duplicate_records": 0,
            "successfully_matched_outcomes": 0,
            "unresolved_outcomes": 0,
            "outcome_identity_missing_records": 0,
            "outcome_identity_invalid_records": 0,
            "outcome_identity_mismatch_records": 0,
            "evidence_conflict_records": 0,
            "target_outside_window_records": 0,
            "missing_touch_evidence_records": 0,
            "missing_detection_time_records": 0,
            "rejected_records": [],
        }

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
        """Filter ambiguous identities, validate timestamps, and sort chronologically.

        Strictly enforces:
        - Exact DEX market identity: chain + token mint/contract + pool address (all mandatory).
        - Distinct CEX market identity: exchange + market_type + trading_pair.
        - Rejection of missing chain, missing token, or missing pool address.
        - Detection and reporting of conflicting token or pool representations.
        - Preservation of frozen detection-time information.
        - Deduplication to prevent inflating the sample count.
        - Recording of all rejected candidates in diagnostics for full auditability.
        """
        valid = []
        seen_keys: set[tuple[str, str]] = set()

        for r in self.records:
            if not isinstance(r, dict):
                self.diagnostics["missing_identity_records"] += 1
                self.diagnostics["missing_identity_by_reason"]["NOT_A_DICTIONARY"] += 1
                self.diagnostics["rejected_records"].append({"record": r, "reason": "NOT_A_DICTIONARY"})
                continue

            # Extract timestamp
            t_str = r.get("snapshot_utc") or r.get("forecast_utc") or r.get("retrieved_utc")
            t = _parse_utc(t_str)
            if t is None:
                self.diagnostics["missing_identity_records"] += 1
                self.diagnostics["missing_identity_by_reason"]["INVALID_OR_MISSING_TIMESTAMP"] += 1
                self.diagnostics["rejected_records"].append({"record": r, "reason": "INVALID_OR_MISSING_TIMESTAMP"})
                continue

            # Validate exact market identity
            id_res = validate_and_build_identity(r)
            if not id_res.get("is_valid"):
                reason = id_res.get("rejection_reason") or "INVALID_IDENTITY"
                if "CONFLICTING" in reason:
                    self.diagnostics["conflicting_identity_records"] += 1
                else:
                    self.diagnostics["missing_identity_records"] += 1
                self.diagnostics["missing_identity_by_reason"][reason] += 1
                self.diagnostics["rejected_records"].append({
                    "record": r,
                    "reason": reason,
                    "details": id_res,
                })
                continue

            canon_key = id_res["canonical_identity"]
            m_type = id_res["market_type"]

            # Validate price at detection time
            price = _clean_num(
                r.get("price_usd") or r.get("final_price_usd") or r.get("entry_price") or
                (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("price_at_detection_usd")
            )
            if price is None or price <= 0:
                self.diagnostics["missing_identity_by_reason"]["INVALID_OR_NONPOSITIVE_PRICE"] += 1
                self.diagnostics["rejected_records"].append({"record": r, "reason": "INVALID_OR_NONPOSITIVE_PRICE"})
                continue

            # Deduplication: prevent identical observation from inflating sample count
            dedup_key = (canon_key, str(t_str))
            if dedup_key in seen_keys:
                self.diagnostics["duplicate_records"] += 1
                self.diagnostics["rejected_records"].append({
                    "record": r,
                    "reason": "DUPLICATE_OBSERVATION",
                    "key": f"{canon_key}@{t_str}",
                })
                continue
            seen_keys.add(dedup_key)

            # Preserve immutable detection-time fields
            rec_id = r.get("snapshot_id") or r.get("forecast_id") or hashlib.sha256(f"{canon_key}:{t_str}".encode()).hexdigest()
            tp_price = _clean_num(
                r.get("target_price") or r.get("take_profit_price") or r.get("tp_price") or
                (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("target_price")
            )
            tp_pct = _clean_num(r.get("technical_tp_pct") or r.get("target_upside_pct") or r.get("tp_pct"))
            if tp_pct is None and tp_price is not None and price > 0:
                tp_pct = round(((tp_price / price) - 1.0) * 100.0, 4)

            score_val = _clean_num(
                r.get("early_breakout_score") or r.get("rank_score") or
                (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("signal_score_and_components", {}).get("early_breakout_score")
            ) or 0.0

            liq_val = _clean_num(
                r.get("liquidity_usd") or
                (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("liquidity_usd")
            ) or 0.0

            vol_val = _clean_num(
                r.get("volume_usd") or r.get("volume_24h_usd") or
                (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("volume_24h_usd")
            ) or 0.0

            age_val = _clean_num(
                r.get("age_hours") or
                (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("pool_age_hours")
            )

            valid.append({
                "record_id": rec_id,
                "timestamp": t,
                "timestamp_utc": t_str,
                "discovery_timestamp_utc": t_str,
                "market_type": m_type,
                "chain": id_res.get("chain"),
                "token": id_res.get("token"),
                "pool": id_res.get("pool"),
                "exchange": id_res.get("exchange"),
                "pair": id_res.get("pair"),
                "market_key": canon_key,
                "exact_identity": canon_key,
                "detection_price": price,
                "entry_price": price,
                "technical_tp_price": tp_price,
                "technical_tp_pct": tp_pct,
                "scanner_version": str(r.get("scanner_version") or r.get("version") or BACKTEST_VERSION),
                "config_fingerprint": str(r.get("config_fingerprint") or self.config_fingerprint),
                "score": score_val,
                "score_components": r.get("score_components") or (r.get("normalized_record", {}) if isinstance(r.get("normalized_record"), dict) else {}).get("signal_score_and_components") or {},
                "liquidity_usd": liq_val,
                "volume_usd": vol_val,
                "volume_24h_usd": vol_val,
                "token_age_hours": age_val,
                "age_hours": age_val,
                "security_evidence": r.get("security_evidence") or r.get("security_status") or r.get("quality_gate") or {},
                "discovery_source": r.get("source") or r.get("discovery_source") or r.get("venue") or "unknown",
                "provider_timestamps": {
                    "retrieved_utc": r.get("retrieved_utc"),
                    "data_timestamp_utc": r.get("data_timestamp_utc"),
                },
                "is_rejected": r.get("is_rejected_candidate", False),
                "raw_record": r,
            })

        # Sort strictly chronologically
        valid.sort(key=lambda item: item["timestamp"])
        self.diagnostics["valid_exact_identity_records"] = len(valid)
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
                "diagnostics": {
                    "total_historical_records": self.diagnostics.get("total_historical_records", len(self.records)),
                    "valid_exact_identity_records": 0,
                    "missing_identity_records": self.diagnostics.get("missing_identity_records", 0),
                    "missing_identity_by_reason": dict(self.diagnostics.get("missing_identity_by_reason", {})),
                    "conflicting_identity_records": self.diagnostics.get("conflicting_identity_records", 0),
                    "duplicate_records": self.diagnostics.get("duplicate_records", 0),
                    "successfully_matched_outcomes": 0,
                    "unresolved_outcomes": 0,
                    "outcome_identity_missing_records": self.diagnostics.get("outcome_identity_missing_records", 0),
                    "outcome_identity_invalid_records": self.diagnostics.get("outcome_identity_invalid_records", 0),
                    "outcome_identity_mismatch_records": self.diagnostics.get("outcome_identity_mismatch_records", 0),
                    "evidence_conflict_records": self.diagnostics.get("evidence_conflict_records", 0),
                    "target_outside_window_records": self.diagnostics.get("target_outside_window_records", 0),
                    "missing_touch_evidence_records": self.diagnostics.get("missing_touch_evidence_records", 0),
                    "missing_detection_time_records": self.diagnostics.get("missing_detection_time_records", 0),
                },
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
            "diagnostics": {
                "total_historical_records": self.diagnostics.get("total_historical_records", len(self.records)),
                "valid_exact_identity_records": len(dataset),
                "missing_identity_records": self.diagnostics.get("missing_identity_records", 0),
                "missing_identity_by_reason": dict(self.diagnostics.get("missing_identity_by_reason", {})),
                "conflicting_identity_records": self.diagnostics.get("conflicting_identity_records", 0),
                "duplicate_records": self.diagnostics.get("duplicate_records", 0),
                "successfully_matched_outcomes": self.diagnostics.get("successfully_matched_outcomes", 0),
                "unresolved_outcomes": self.diagnostics.get("unresolved_outcomes", 0),
                "outcome_identity_missing_records": self.diagnostics.get("outcome_identity_missing_records", 0),
                "outcome_identity_invalid_records": self.diagnostics.get("outcome_identity_invalid_records", 0),
                "outcome_identity_mismatch_records": self.diagnostics.get("outcome_identity_mismatch_records", 0),
                "evidence_conflict_records": self.diagnostics.get("evidence_conflict_records", 0),
                "target_outside_window_records": self.diagnostics.get("target_outside_window_records", 0),
                "missing_touch_evidence_records": self.diagnostics.get("missing_touch_evidence_records", 0),
                "missing_detection_time_records": self.diagnostics.get("missing_detection_time_records", 0),
            },
            "scientific_integrity_note": (
                "Zero synthetic labels permitted. Incomplete or unresolved observation windows are "
                "strictly counted and excluded from performance percentage denominators."
            ),
        }

    def _validate_outcome_identity(self, outcome_data: Any, forecast_canon_id: str) -> tuple[bool, dict[str, Any] | None]:
        """Validate that an outcome record (from lookup or embedded) has a valid canonical identity
        matching the forecast's canonical market_key.
        """
        if not isinstance(outcome_data, dict):
            self.diagnostics["outcome_identity_missing_records"] += 1
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "OUTCOME_IDENTITY_MISSING",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": None,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "OUTCOME_IDENTITY_MISSING: Outcome record is not a dictionary",
                "execution_status": "EXECUTION_UNVERIFIED",
                "execution_net_return_pct": None,
            }

        has_id_indicators = any(
            outcome_data.get(k) is not None and str(outcome_data.get(k)).strip() != ""
            for k in (
                "market_key", "exact_identity", "identity",
                "chain", "token_contract", "contract_address", "token_mint", "address",
                "pool_address", "pair_address", "gecko_pool_id",
                "exchange", "venue", "trading_pair", "pair", "venue_pair",
            )
        ) or (isinstance(outcome_data.get("normalized_record"), dict) and any(outcome_data["normalized_record"].values()))

        if not has_id_indicators:
            self.diagnostics["outcome_identity_missing_records"] += 1
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "OUTCOME_IDENTITY_MISSING",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": None,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "OUTCOME_IDENTITY_MISSING: Outcome record contains no market identity",
                "execution_status": "EXECUTION_UNVERIFIED",
                "execution_net_return_pct": None,
            }

        id_res = validate_and_build_identity(outcome_data)
        if not id_res.get("is_valid"):
            rej_reason = str(id_res.get("rejection_reason") or "INVALID_IDENTITY")
            if rej_reason.startswith("MISSING_"):
                self.diagnostics["outcome_identity_missing_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "OUTCOME_IDENTITY_MISSING",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": None,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"OUTCOME_IDENTITY_MISSING: {rej_reason}",
                    "execution_status": "EXECUTION_UNVERIFIED",
                    "execution_net_return_pct": None,
                }
            else:
                self.diagnostics["outcome_identity_invalid_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "OUTCOME_IDENTITY_INVALID",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": None,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"OUTCOME_IDENTITY_INVALID: {rej_reason}",
                    "execution_status": "EXECUTION_UNVERIFIED",
                    "execution_net_return_pct": None,
                }

        outcome_canon_id = id_res.get("canonical_identity")
        if outcome_canon_id != forecast_canon_id:
            self.diagnostics["outcome_identity_mismatch_records"] += 1
            self.diagnostics["conflicting_identity_records"] += 1
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "OUTCOME_IDENTITY_MISMATCH",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": None,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"OUTCOME_IDENTITY_MISMATCH: forecast={forecast_canon_id} outcome={outcome_canon_id}",
                "execution_status": "EXECUTION_UNVERIFIED",
                "execution_net_return_pct": None,
            }

        return True, None

    def _validate_candle_trace(
        self,
        outcome_data: dict[str, Any],
        forecast_canon_id: str,
        detection_dt: dt.datetime,
        outcome_status: str,
        mfe_pct: float | None,
        touch_time: float | None,
        touch_dt: dt.datetime | None,
        effective_target_pct: float,
        detection_price: float,
    ) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
        """Validate candle trace evidence provenance and completeness according to CANDLE_TRACE_SCHEMA.

        Enforces:
        - Recognized data provider/source.
        - Source reference or retrieval URL.
        - Exact canonical market identity match.
        - Declared candle interval in recognized formats.
        - Valid trace start and end timestamps.
        - Non-empty actual candle observations array (metadata-only traces are strictly rejected).
        - Valid, finite, internally consistent OHLC values for every candle.
        - Chronologically sorted observations without duplicate timestamps.
        - Candle spacing verification against declared interval with gap detection.
        - Complete window coverage from detection through required evaluation window (full 24h for losses).
        - Derivation and chronological verification of MFE and first target touch.
        """
        trace = outcome_data.get("candle_trace") or outcome_data.get("evidence")
        exec_status = str(outcome_data.get("execution_status") or "EXECUTION_UNVERIFIED")

        if not isinstance(trace, dict):
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "MISSING_CANDLE_TRACE_EVIDENCE",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "MISSING_CANDLE_TRACE_EVIDENCE: outcome requires verifiable candle trace evidence",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        # 1. Source Provenance
        raw_prov = (
            trace.get("provider")
            or trace.get("trace_provenance")
            or trace.get("source")
            or outcome_data.get("provider")
            or outcome_data.get("trace_provenance")
        )
        if not raw_prov or not str(raw_prov).strip():
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "MISSING_TRACE_PROVENANCE",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "MISSING_TRACE_PROVENANCE: candle trace lacks source provenance",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        norm_prov = normalize_trace_provider(raw_prov)
        if norm_prov is None or norm_prov not in RECOGNIZED_TRACE_PROVIDERS:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "UNRECOGNIZED_TRACE_PROVIDER",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"UNRECOGNIZED_TRACE_PROVIDER: provider '{raw_prov}' is not an exact recognized market data provider",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        # 2. Market Key Provenance (Exact equality with forecast)
        trace_mkey = str(trace.get("market_key") or trace.get("exact_identity") or "").strip()
        if not trace_mkey or trace_mkey != forecast_canon_id:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "PROVENANCE_MARKET_MISMATCH",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"PROVENANCE_MARKET_MISMATCH: trace market_key '{trace_mkey}' != forecast '{forecast_canon_id}'",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        # 3. Candle Interval
        candle_interval = str(trace.get("candle_interval") or trace.get("interval") or "").strip().lower()
        if not candle_interval:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "MISSING_CANDLE_INTERVAL",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "MISSING_CANDLE_INTERVAL: candle trace lacks candle interval",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        if candle_interval not in CANDLE_INTERVAL_SECONDS:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "UNRECOGNIZED_CANDLE_INTERVAL",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"UNRECOGNIZED_CANDLE_INTERVAL: interval '{candle_interval}' not supported",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        step_seconds = CANDLE_INTERVAL_SECONDS[candle_interval]

        # 4. Trace Start and End Timestamps
        trace_start_dt = _parse_utc(trace.get("trace_start_utc") or trace.get("start_utc"))
        trace_end_dt = _parse_utc(trace.get("trace_end_utc") or trace.get("end_utc"))
        if trace_start_dt is None or trace_end_dt is None:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "MISSING_TRACE_TIMESTAMPS",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "MISSING_TRACE_TIMESTAMPS: candle trace lacks valid start or end timestamp",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        if trace_end_dt < trace_start_dt:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "TRACE_END_BEFORE_START",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"TRACE_END_BEFORE_START: trace_end_utc {trace_end_dt.isoformat()} precedes trace_start_utc {trace_start_dt.isoformat()}",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        if trace_start_dt < (detection_dt - dt.timedelta(minutes=5)):
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "TRACE_PRECEDES_DETECTION",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"TRACE_PRECEDES_DETECTION: trace start {trace_start_dt} precedes detection {detection_dt}",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        # 5. Source Reference / Retrieval Evidence & Domain Validation
        source_ref = (
            trace.get("source_ref")
            or trace.get("source_url")
            or trace.get("source_id")
            or trace.get("query_url")
            or outcome_data.get("source_ref")
            or outcome_data.get("source_url")
        )
        if not source_ref or not str(source_ref).strip():
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "MISSING_SOURCE_REFERENCE",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "MISSING_SOURCE_REFERENCE: candle trace lacks source reference or retrieval URL",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        source_domain = extract_source_domain(source_ref)
        if not source_domain:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "UNAPPROVED_SOURCE_DOMAIN",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"UNAPPROVED_SOURCE_DOMAIN: source reference '{source_ref}' does not contain an approved domain",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        is_domain_valid, domain_status = validate_provider_source_domain(norm_prov, source_domain)
        if not is_domain_valid:
            self.diagnostics["unresolved_outcomes"] += 1
            if domain_status == "PROVIDER_DOMAIN_MISMATCH":
                return False, {
                    "outcome_classification": "PROVIDER_DOMAIN_MISMATCH",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"PROVIDER_DOMAIN_MISMATCH: provider '{norm_prov}' does not match source domain '{source_domain}'",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None
            else:
                return False, {
                    "outcome_classification": "UNAPPROVED_SOURCE_DOMAIN",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"UNAPPROVED_SOURCE_DOMAIN: domain '{source_domain}' is not approved for provider '{norm_prov}'",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

        # 6. Evidence Retrieval Timestamp
        raw_retrieved = (
            trace.get("retrieved_utc")
            or trace.get("retrieval_timestamp_utc")
            or trace.get("retrieved_at")
            or outcome_data.get("retrieved_utc")
        )
        retrieved_dt = _parse_utc(raw_retrieved)
        if retrieved_dt is None:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "MISSING_RETRIEVAL_TIMESTAMP",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "MISSING_RETRIEVAL_TIMESTAMP: candle trace lacks valid evidence retrieval timestamp",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        # 7. Require Non-Empty Actual Candle Observations Array
        # A trace containing only metadata and a claimed 24h end timestamp must NEVER qualify as verified evidence.
        # Self-reported coverage percentage is never trusted without actual candles.
        raw_candles = trace.get("candles")
        if not isinstance(raw_candles, list) or len(raw_candles) == 0:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "INCOMPLETE_OBSERVATION",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "MISSING_CANDLE_DATA: evaluated outcome requires non-empty candle observation array",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        # 8. Parse and Validate Every Individual Candle
        parsed_candles: list[dict[str, Any]] = []
        seen_timestamps: set[dt.datetime] = set()

        for idx, c in enumerate(raw_candles):
            if not isinstance(c, dict):
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "INCOMPLETE_OBSERVATION",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"INVALID_CANDLE_DATA: candle at index {idx} is not a dictionary",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

            c_ts_raw = c.get("timestamp_utc") or c.get("timestamp") or c.get("t") or c.get("time_open")
            c_dt = _parse_utc(c_ts_raw)
            if c_dt is None:
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "INCOMPLETE_OBSERVATION",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"INVALID_CANDLE_TIMESTAMP: candle at index {idx} has invalid or missing timestamp '{c_ts_raw}'",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

            # Boundary 1: No candle may precede detection
            if c_dt < detection_dt:
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "INCOMPLETE_OBSERVATION",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"CANDLE_PRECEDES_DETECTION: candle at index {idx} timestamp {c_dt.isoformat()} precedes detection {detection_dt.isoformat()}",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

            # Boundary 2: Reject future-dated candle relative to retrieval timestamp
            if c_dt > retrieved_dt:
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "INCOMPLETE_OBSERVATION",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"CANDLE_FUTURE_DATED: candle at index {idx} timestamp {c_dt.isoformat()} is future-dated relative to retrieval timestamp {retrieved_dt.isoformat()}",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

            # Detect duplicate timestamps
            if c_dt in seen_timestamps:
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "INCOMPLETE_OBSERVATION",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"DUPLICATE_CANDLE_TIMESTAMP: candle at index {idx} has duplicate timestamp {c_dt.isoformat()}",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None
            seen_timestamps.add(c_dt)

            # Check required OHLC fields existence: open, high, low, close
            ohlc_fields = [("open", ("open", "o")), ("high", ("high", "h")), ("low", ("low", "l")), ("close", ("close", "c"))]
            for field_name, aliases in ohlc_fields:
                val_present = any(a in c and c[a] is not None for a in aliases)
                if not val_present:
                    self.diagnostics["unresolved_outcomes"] += 1
                    return False, {
                        "outcome_classification": "INCOMPLETE_OBSERVATION",
                        "target_reached": None,
                        "market_target_reached": None,
                        "mfe_pct": mfe_pct,
                        "mae_pct": None,
                        "hours_to_target": None,
                        "is_evaluated": False,
                        "resolution_reason": f"MISSING_CANDLE_OHLC: candle at index {idx} missing required field '{field_name}'",
                        "execution_status": exec_status,
                        "execution_net_return_pct": None,
                    }, None

            c_open = _clean_strictly_positive_num(c.get("open") if "open" in c else c.get("o"))
            c_high = _clean_strictly_positive_num(c.get("high") if "high" in c else c.get("h"))
            c_low = _clean_strictly_positive_num(c.get("low") if "low" in c else c.get("l"))
            c_close = _clean_strictly_positive_num(c.get("close") if "close" in c else c.get("c"))

            if c_open is None or c_high is None or c_low is None or c_close is None:
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "INCOMPLETE_OBSERVATION",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"INVALID_CANDLE_OHLC: candle at index {idx} has non-numeric, NaN, infinite, zero, or negative OHLC values",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

            # Enforce Candle Geometry:
            # low <= min(open, close)
            # high >= max(open, close)
            # low <= high
            if c_low > c_high:
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "INCOMPLETE_OBSERVATION",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"INVALID_CANDLE_GEOMETRY: candle at index {idx} low {c_low} > high {c_high}",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

            if c_low > min(c_open, c_close):
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "INCOMPLETE_OBSERVATION",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"INVALID_CANDLE_GEOMETRY: candle at index {idx} low {c_low} > min(open, close) {min(c_open, c_close)}",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

            if c_high < max(c_open, c_close):
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "INCOMPLETE_OBSERVATION",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"INVALID_CANDLE_GEOMETRY: candle at index {idx} high {c_high} < max(open, close) {max(c_open, c_close)}",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

            parsed_candles.append({
                "dt": c_dt,
                "high": c_high,
                "open": c_open,
                "low": c_low,
                "close": c_close,
                "raw": c,
            })

        # 9. Chronological Sorting
        sorted_candles = sorted(parsed_candles, key=lambda x: x["dt"])

        # 10. Validate Spacing and Gap Detection
        for i in range(len(sorted_candles) - 1):
            delta_s = (sorted_candles[i + 1]["dt"] - sorted_candles[i]["dt"]).total_seconds()
            if delta_s < step_seconds * 0.5:
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "CANDLE_INTERVAL_MISMATCH",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": (
                        f"CANDLE_INTERVAL_MISMATCH: spacing between candles {sorted_candles[i]['dt']} and "
                        f"{sorted_candles[i + 1]['dt']} is {delta_s}s, mismatching declared {step_seconds}s"
                    ),
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None
            if delta_s > step_seconds * 1.5:
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "INCOMPLETE_OBSERVATION",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": (
                        f"CANDLE_GAP_DETECTED: gap of {delta_s}s between {sorted_candles[i]['dt']} and "
                        f"{sorted_candles[i + 1]['dt']} exceeds declared {step_seconds}s interval"
                    ),
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

        # Check explicit gap flags from trace / outcome_data
        has_flagged_gaps = (
            trace.get("has_gaps") is True
            or trace.get("gap_detected") is True
            or outcome_data.get("has_gaps") is True
            or outcome_data.get("candle_gap_detected") is True
        )
        if has_flagged_gaps:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "INCOMPLETE_OBSERVATION",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "CANDLE_GAP_DETECTED: candle trace has gaps or missing observations",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        # 11. Observation Window Coverage & Trace Metadata Agreement
        first_candle_dt = sorted_candles[0]["dt"]
        last_candle_dt = sorted_candles[-1]["dt"]
        candle_boundary_tolerance_s = max(60.0, step_seconds * 1.5)

        if first_candle_dt < (trace_start_dt - dt.timedelta(seconds=60)):
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "INCOMPLETE_OBSERVATION",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"TRACE_START_MISMATCH: actual first candle {first_candle_dt.isoformat()} precedes trace_start_utc {trace_start_dt.isoformat()}",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        if (first_candle_dt - trace_start_dt).total_seconds() > candle_boundary_tolerance_s:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "INCOMPLETE_OBSERVATION",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": (
                    f"TRACE_START_MISMATCH: actual first candle {first_candle_dt.isoformat()} starts "
                    f"{(first_candle_dt - trace_start_dt).total_seconds():.1f}s after trace_start_utc {trace_start_dt.isoformat()}, "
                    f"exceeding tolerance {candle_boundary_tolerance_s:.1f}s"
                ),
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        if last_candle_dt > (trace_end_dt + dt.timedelta(seconds=60)):
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "INCOMPLETE_OBSERVATION",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"TRACE_END_MISMATCH: actual last candle {last_candle_dt.isoformat()} exceeds trace_end_utc {trace_end_dt.isoformat()}",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        if (trace_end_dt - last_candle_dt).total_seconds() > candle_boundary_tolerance_s:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "INCOMPLETE_OBSERVATION",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": (
                    f"TRACE_END_MISMATCH: STALE_FINAL_CANDLE actual last candle {last_candle_dt.isoformat()} terminates "
                    f"{(trace_end_dt - last_candle_dt).total_seconds():.1f}s before declared trace_end_utc {trace_end_dt.isoformat()}, "
                    f"exceeding tolerance {candle_boundary_tolerance_s:.1f}s"
                ),
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        max_allowed_first_dt = detection_dt + dt.timedelta(seconds=step_seconds + 300)
        if first_candle_dt > max_allowed_first_dt:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "INCOMPLETE_OBSERVATION",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"OBSERVATION_STARTS_LATE: first candle at {first_candle_dt} starts later than detection {detection_dt}",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        # 12. For TARGET_NOT_REACHED: Enforce full 24-hour observation coverage
        if outcome_status == "TARGET_NOT_REACHED":
            elapsed_trace_hours = (trace_end_dt - detection_dt).total_seconds() / 3600.0
            if elapsed_trace_hours < 24.0:
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "INCOMPLETE_OBSERVATION",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"PARTIAL_OBSERVATION_WINDOW: observation window ended at {elapsed_trace_hours:.2f}h, minimum 24.0h required for loss",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

            full_24h_dt = detection_dt + dt.timedelta(hours=24)
            min_required_last_dt = full_24h_dt - dt.timedelta(seconds=step_seconds)
            if last_candle_dt < min_required_last_dt:
                elapsed_hours = (last_candle_dt - detection_dt).total_seconds() / 3600.0
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "INCOMPLETE_OBSERVATION",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": (
                        f"PARTIAL_OBSERVATION_WINDOW: STALE_FINAL_CANDLE final observation at {last_candle_dt} covers {elapsed_hours:.2f}h, "
                        f"incomplete for full 24.0h loss evaluation"
                    ),
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

        # 13. Verifiable Source-Response Evidence Reconciliation & Metadata Validation (Fail-Closed)
        captured_resp = (
            trace.get("captured_response")
            or trace.get("response_evidence")
            or trace.get("raw_response")
            or outcome_data.get("captured_response")
            or outcome_data.get("response_evidence")
        )
        if not isinstance(captured_resp, dict):
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "MISSING_RESPONSE_EVIDENCE",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "MISSING_RESPONSE_EVIDENCE: evaluated outcome requires captured provider HTTP response evidence",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        # 13a. Validate captured response HTTP status (must be explicit integer, strictly 200)
        raw_status = (
            captured_resp.get("http_status")
            if "http_status" in captured_resp
            else captured_resp.get("status_code")
        )
        if raw_status is None:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "INVALID_RESPONSE_HTTP_STATUS",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "INVALID_RESPONSE_HTTP_STATUS: MISSING_RESPONSE_HTTP_STATUS captured response lacks explicit integer http_status",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        if not isinstance(raw_status, int) or isinstance(raw_status, bool):
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "INVALID_RESPONSE_HTTP_STATUS",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"INVALID_RESPONSE_HTTP_STATUS: http_status must be an integer, got {type(raw_status).__name__} ({raw_status!r})",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        if raw_status != 200:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "INVALID_RESPONSE_HTTP_STATUS",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"INVALID_RESPONSE_HTTP_STATUS: HTTP {raw_status} rejected; only HTTP 200 is accepted",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        resp_status = raw_status

        # 13b. Validate captured request URL
        raw_req_url = (
            captured_resp.get("request_url")
            if "request_url" in captured_resp
            else captured_resp.get("url")
        )
        if not raw_req_url or not isinstance(raw_req_url, str) or not raw_req_url.strip():
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "MISSING_RESPONSE_REQUEST_URL",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "MISSING_RESPONSE_REQUEST_URL: captured response lacks explicit request_url",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        resp_url = raw_req_url.strip()
        is_url_valid, req_domain, url_diag = parse_and_validate_url(resp_url)
        if not is_url_valid or not req_domain:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "INVALID_RESPONSE_REQUEST_URL",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"INVALID_RESPONSE_REQUEST_URL: {url_diag} in request_url '{resp_url}'",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        is_req_domain_valid, req_domain_status = validate_provider_source_domain(norm_prov, req_domain)
        if not is_req_domain_valid:
            self.diagnostics["unresolved_outcomes"] += 1
            if req_domain_status == "PROVIDER_DOMAIN_MISMATCH":
                return False, {
                    "outcome_classification": "RESPONSE_URL_PROVIDER_MISMATCH",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"RESPONSE_URL_PROVIDER_MISMATCH: request_url domain '{req_domain}' belongs to a different provider than '{norm_prov}'",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None
            else:
                return False, {
                    "outcome_classification": "INVALID_RESPONSE_REQUEST_URL",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"INVALID_RESPONSE_REQUEST_URL: request_url domain '{req_domain}' not approved for provider '{norm_prov}'",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

        if req_domain != source_domain:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "RESPONSE_URL_DOMAIN_MISMATCH",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"RESPONSE_URL_DOMAIN_MISMATCH: request_url domain '{req_domain}' conflicts with source_ref domain '{source_domain}'",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        # 13c. Validate response retrieval time
        raw_resp_retrieved = (
            captured_resp.get("retrieval_timestamp_utc")
            if "retrieval_timestamp_utc" in captured_resp
            else (captured_resp.get("retrieved_utc") if "retrieved_utc" in captured_resp else captured_resp.get("retrieved_at"))
        )
        if not raw_resp_retrieved or not isinstance(raw_resp_retrieved, str) or not raw_resp_retrieved.strip():
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "MISSING_RESPONSE_RETRIEVAL_TIMESTAMP",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "MISSING_RESPONSE_RETRIEVAL_TIMESTAMP: captured response lacks retrieval_timestamp_utc",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        resp_retrieved_dt = _parse_utc(raw_resp_retrieved)
        if resp_retrieved_dt is None:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "INVALID_RESPONSE_RETRIEVAL_TIMESTAMP",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"INVALID_RESPONSE_RETRIEVAL_TIMESTAMP: retrieval_timestamp_utc '{raw_resp_retrieved}' is invalid or timezone-naive",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        now_utc = dt.datetime.now(dt.timezone.utc)
        if resp_retrieved_dt > (now_utc + dt.timedelta(minutes=15)):
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "FUTURE_RESPONSE_RETRIEVAL_TIMESTAMP",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"FUTURE_RESPONSE_RETRIEVAL_TIMESTAMP: retrieval timestamp {resp_retrieved_dt.isoformat()} is in the future relative to current time {now_utc.isoformat()}",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        retrieval_tolerance_s = 300.0  # Documented 5-minute tolerance
        if abs((resp_retrieved_dt - retrieved_dt).total_seconds()) > retrieval_tolerance_s:
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "RESPONSE_RETRIEVAL_TIME_MISMATCH",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": (
                    f"RESPONSE_RETRIEVAL_TIME_MISMATCH: captured response retrieval time {resp_retrieved_dt.isoformat()} "
                    f"differs from trace retrieval time {retrieved_dt.isoformat()} by "
                    f"{abs((resp_retrieved_dt - retrieved_dt).total_seconds()):.1f}s (tolerance: {retrieval_tolerance_s:.1f}s)"
                ),
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        if sorted_candles and sorted_candles[-1]["dt"] > resp_retrieved_dt:
            max_c_dt = sorted_candles[-1]["dt"]
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "CANDLE_TIMESTAMP_EXCEEDS_RETRIEVAL_TIME",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": (
                    f"CANDLE_TIMESTAMP_EXCEEDS_RETRIEVAL_TIME: candle at {max_c_dt.isoformat()} "
                    f"is later than captured response retrieval time {resp_retrieved_dt.isoformat()}"
                ),
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        # 13d. Reconcile captured response payload and content hash (fail-closed)
        raw_body = (
            captured_resp.get("raw_body")
            if "raw_body" in captured_resp
            else (captured_resp.get("body") if "body" in captured_resp else captured_resp.get("payload"))
        )
        declared_hash = (
            captured_resp.get("content_hash")
            or captured_resp.get("response_sha256")
            or captured_resp.get("hash")
        )

        if raw_body is None or declared_hash is None or not str(declared_hash).strip():
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "MISSING_RESPONSE_EVIDENCE",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "MISSING_RESPONSE_EVIDENCE: captured response missing raw_body or content_hash",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        # Verify content hash (fail-closed)
        if isinstance(raw_body, str):
            calc_hash = hashlib.sha256(raw_body.encode("utf-8")).hexdigest()
        else:
            calc_hash = hashlib.sha256(json.dumps(raw_body, sort_keys=True).encode("utf-8")).hexdigest()

        if calc_hash.lower() != str(declared_hash).strip().lower():
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "RESPONSE_CONTENT_HASH_MISMATCH",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"RESPONSE_CONTENT_HASH_MISMATCH: calculated sha256 {calc_hash} != declared {declared_hash}",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        # Parse raw response body
        if isinstance(raw_body, str):
            try:
                parsed_body = json.loads(raw_body)
            except Exception as exc:
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "UNPARSABLE_RESPONSE_EVIDENCE",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"UNPARSABLE_RESPONSE_EVIDENCE: failed to parse response JSON: {exc}",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None
        else:
            parsed_body = raw_body

        resp_candles_raw = extract_candles_from_response_payload(parsed_body)
        if resp_candles_raw is None or not isinstance(resp_candles_raw, list):
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "UNPARSABLE_RESPONSE_EVIDENCE",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "UNPARSABLE_RESPONSE_EVIDENCE: response does not contain recognizable candle observations",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        # Reconcile parsed response candles against sorted candles
        parsed_resp_candles = []
        for rc in resp_candles_raw:
            if isinstance(rc, dict):
                rc_ts = _parse_utc(rc.get("timestamp_utc") or rc.get("timestamp") or rc.get("t") or rc.get("time_open"))
                if rc_ts is not None:
                    parsed_resp_candles.append({
                        "dt": rc_ts,
                        "open": _clean_num(rc.get("open") if "open" in rc else rc.get("o")),
                        "high": _clean_num(rc.get("high") if "high" in rc else rc.get("h")),
                        "low": _clean_num(rc.get("low") if "low" in rc else rc.get("l")),
                        "close": _clean_num(rc.get("close") if "close" in rc else rc.get("c")),
                    })

        parsed_resp_candles.sort(key=lambda x: x["dt"])

        if len(parsed_resp_candles) != len(sorted_candles):
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "RESPONSE_CANDLE_COUNT_MISMATCH",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": (
                    f"RESPONSE_CANDLE_COUNT_MISMATCH: response contains {len(parsed_resp_candles)} candles, "
                    f"evaluator supplied {len(sorted_candles)} candles"
                ),
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        for c_idx in range(len(sorted_candles)):
            sc = sorted_candles[c_idx]
            rc = parsed_resp_candles[c_idx]

            # Reconcile timestamp
            if abs((sc["dt"] - rc["dt"]).total_seconds()) > 60.0:
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "RESPONSE_CANDLE_TIMESTAMP_MISMATCH",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": (
                        f"RESPONSE_CANDLE_TIMESTAMP_MISMATCH: candle at index {c_idx} timestamp {sc['dt'].isoformat()} "
                        f"conflicts with captured response timestamp {rc['dt'].isoformat()}"
                    ),
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

            # Reconcile OHLC values
            for ohlc_key in ("open", "high", "low", "close"):
                s_val = sc[ohlc_key]
                r_val = rc[ohlc_key]
                if s_val is None or r_val is None or abs(s_val - r_val) > 1e-4:
                    self.diagnostics["unresolved_outcomes"] += 1
                    return False, {
                        "outcome_classification": "RESPONSE_CANDLE_OHLC_MISMATCH",
                        "target_reached": None,
                        "market_target_reached": None,
                        "mfe_pct": mfe_pct,
                        "mae_pct": None,
                        "hours_to_target": None,
                        "is_evaluated": False,
                        "resolution_reason": (
                            f"RESPONSE_CANDLE_OHLC_MISMATCH: candle at index {c_idx} {ohlc_key} {s_val} "
                            f"conflicts with captured response {ohlc_key} {r_val}"
                        ),
                        "execution_status": exec_status,
                        "execution_net_return_pct": None,
                    }, None

        # 14. Derive and Verify MFE and First-Touch Chronology
        # Pre-detection candles must never contribute to MFE, MAE or target-touch calculations
        eval_candles = [c for c in sorted_candles if c["dt"] >= detection_dt]
        detection_price_val = detection_price if detection_price > 0 else 1.0
        max_high = max(c["high"] for c in eval_candles)
        derived_mfe = round(((max_high - detection_price_val) / detection_price_val) * 100.0, 2)

        if mfe_pct is not None and abs(derived_mfe - mfe_pct) > 1.0:
            self.diagnostics["evidence_conflict_records"] += 1
            self.diagnostics["unresolved_outcomes"] += 1
            return False, {
                "outcome_classification": "OUTCOME_EVIDENCE_CONFLICT",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": mfe_pct,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": f"OUTCOME_EVIDENCE_CONFLICT: derived candle MFE {derived_mfe:.2f}% conflicts with claimed MFE {mfe_pct:.2f}%",
                "execution_status": exec_status,
                "execution_net_return_pct": None,
            }, None

        target_price = detection_price_val * (1.0 + effective_target_pct / 100.0)
        touching_candles = [c for c in eval_candles if c["high"] >= target_price]

        derived_touch_hours: float | None = None
        derived_touch_dt: dt.datetime | None = None

        if touching_candles:
            first_touch_c = touching_candles[0]
            derived_touch_dt = first_touch_c["dt"]
            derived_touch_hours = round((derived_touch_dt - detection_dt).total_seconds() / 3600.0, 2)

        if outcome_status == "TARGET_NOT_REACHED":
            if touching_candles:
                self.diagnostics["evidence_conflict_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "OUTCOME_EVIDENCE_CONFLICT",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"OUTCOME_EVIDENCE_CONFLICT: status is TARGET_NOT_REACHED but candle trace touches target price {target_price}",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

        if outcome_status == "TARGET_REACHED":
            if not touching_candles or derived_touch_hours is None or derived_touch_dt is None:
                self.diagnostics["evidence_conflict_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "OUTCOME_EVIDENCE_CONFLICT",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"OUTCOME_EVIDENCE_CONFLICT: no candle in trace touches target price {target_price}",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

            # First touch must occur within the 24-hour observation window (0.0 to 24.0 hours)
            if derived_touch_hours < 0.0 or derived_touch_hours > 24.0:
                self.diagnostics["target_outside_window_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "TARGET_OUTSIDE_24H_WINDOW",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": derived_touch_hours,
                    "is_evaluated": False,
                    "resolution_reason": f"TARGET_OUTSIDE_24H_WINDOW: chronological first touch occurred at {derived_touch_hours:.2f}h",
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

            # Verify agreement with claimed touch_time if provided
            touch_tol_hours = max(1.0, step_seconds / 3600.0)
            if touch_time is not None and abs(derived_touch_hours - touch_time) > touch_tol_hours:
                self.diagnostics["evidence_conflict_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return False, {
                    "outcome_classification": "OUTCOME_EVIDENCE_CONFLICT",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": (
                        f"OUTCOME_EVIDENCE_CONFLICT: chronological first touch is at {derived_touch_hours:.2f}h, "
                        f"conflicting with claimed touch time {touch_time:.2f}h"
                    ),
                    "execution_status": exec_status,
                    "execution_net_return_pct": None,
                }, None

            # Verify agreement with claimed touch_dt if provided
            if touch_dt is not None:
                touch_delta_s = abs((derived_touch_dt - touch_dt).total_seconds())
                if touch_delta_s > max(3600.0, step_seconds):
                    self.diagnostics["evidence_conflict_records"] += 1
                    self.diagnostics["unresolved_outcomes"] += 1
                    return False, {
                        "outcome_classification": "OUTCOME_EVIDENCE_CONFLICT",
                        "target_reached": None,
                        "market_target_reached": None,
                        "mfe_pct": mfe_pct,
                        "mae_pct": None,
                        "hours_to_target": None,
                        "is_evaluated": False,
                        "resolution_reason": (
                            f"OUTCOME_EVIDENCE_CONFLICT: chronological first touch timestamp {derived_touch_dt.isoformat()} "
                            f"conflicts with claimed timestamp {touch_dt.isoformat()}"
                        ),
                        "execution_status": exec_status,
                        "execution_net_return_pct": None,
                    }, None

        return True, None, {
            "derived_mfe": derived_mfe,
            "derived_touch_hours": derived_touch_hours,
            "derived_touch_dt": derived_touch_dt,
            "candles_count": len(sorted_candles),
            "sorted_candles": sorted_candles,
            "provider": norm_prov,
            "source_ref": source_ref,
            "source_domain": source_domain,
            "request_url": resp_url,
            "http_status": resp_status,
            "content_hash": calc_hash,
            "content_hash_verified": True,
            "response_reconciliation_verified": True,
            "retrieved_utc": raw_retrieved,
            "response_retrieval_utc": raw_resp_retrieved,
            "provider_metadata": trace.get("provider_metadata") or trace.get("metadata") or trace.get("raw_response_metadata"),
        }

    def _resolve_outcome(self, item: dict[str, Any], lookup: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
        """Resolve exact-identity, verified 24-hour outcome without synthetic labels.

        Enforces:
        - Mandatory canonical outcome identity matching forecast market_key across ALL sources
          (both outcome lookup dictionaries and embedded records).
        - Strict rejection of missing, invalid, or mismatched outcome identities.
        - Non-contradictory classification: TARGET_NOT_REACHED can never produce a hit;
          disagreements between classification and candle evidence trigger OUTCOME_EVIDENCE_CONFLICT.
        - Required candle evidence of WHEN the target was reached (first-touch timestamp or elapsed hours).
        - Verification that detection timestamp exists and is timezone-aware.
        - Cross-verification of timestamp and elapsed time agreement within tolerance.
        - Verification against frozen detection-time technical target.
        - Strict separation between market target attainment and executable net profit.
        """
        forecast_canon_id = item.get("market_key")

        # 1. Resolve source outcome record and enforce identical validation across all sources
        if lookup is not None and item.get("record_id") in lookup:
            outcome_data = lookup[item["record_id"]]
            source_reason = "MATCHED_EXACT_IDENTITY_OUTCOME_LOOKUP"
            is_valid_id, rejection_dict = self._validate_outcome_identity(outcome_data, forecast_canon_id)
            if not is_valid_id:
                return rejection_dict  # type: ignore[return-value]
        else:
            raw = item.get("raw_record") or {}
            has_embedded_outcome_dict = isinstance(raw.get("outcome"), dict)
            has_flat_outcome_fields = (
                raw.get("outcome_classification") is not None
                or raw.get("mfe_pct") is not None
                or raw.get("target_reached") is not None
            )

            if not has_embedded_outcome_dict and not has_flat_outcome_fields:
                self.diagnostics["unresolved_outcomes"] += 1
                return {
                    "outcome_classification": "INCOMPLETE_OBSERVATION",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": None,
                    "mae_pct": None,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": "UNRESOLVED_INCOMPLETE_OR_CENSORED",
                    "execution_status": "EXECUTION_UNVERIFIED",
                    "execution_net_return_pct": None,
                }

            outcome_data = raw["outcome"] if has_embedded_outcome_dict else raw
            source_reason = "EMBEDDED_VERIFIED_OUTCOME"
            # Embedded outcomes MUST pass the exact same identity validation
            is_valid_id, rejection_dict = self._validate_outcome_identity(outcome_data, forecast_canon_id)
            if not is_valid_id:
                return rejection_dict  # type: ignore[return-value]

        # 2. Extract outcome fields
        outcome_status = outcome_data.get("outcome_classification")
        if not outcome_status:
            if outcome_data.get("target_reached") is True:
                outcome_status = "TARGET_REACHED"
            elif outcome_data.get("target_reached") is False:
                outcome_status = "TARGET_NOT_REACHED"
            else:
                outcome_status = "INCOMPLETE_OBSERVATION"

        mfe_pct = _clean_num(outcome_data.get("mfe_pct"))
        mae_pct = _clean_num(outcome_data.get("mae_pct"))
        claimed_target_reached = outcome_data.get("target_reached")
        touch_time = _clean_num(
            outcome_data.get("hours_to_target") or outcome_data.get("first_touch_hours") or outcome_data.get("target_touch_time_hours")
        )
        touch_dt = _parse_utc(
            outcome_data.get("touch_timestamp_utc") or outcome_data.get("target_touch_utc") or outcome_data.get("first_touch_utc")
        )

        exec_status = str(outcome_data.get("execution_status") or "EXECUTION_UNVERIFIED")
        exec_net_return = _clean_num(outcome_data.get("execution_net_return_pct") or outcome_data.get("net_pnl_pct"))

        # Frozen detection-time target threshold (minimum +40.0% single TP)
        frozen_tp_pct = _clean_num(item.get("technical_tp_pct"))
        effective_target_pct = max(MANDATORY_TARGET_PCT, frozen_tp_pct if frozen_tp_pct is not None else MANDATORY_TARGET_PCT)

        # 3. Validate detection timestamp exists and is timezone-aware
        detection_dt = item.get("timestamp")
        if detection_dt is None or not isinstance(detection_dt, dt.datetime) or detection_dt.tzinfo is None:
            self.diagnostics["missing_detection_time_records"] += 1
            self.diagnostics["unresolved_outcomes"] += 1
            return {
                "outcome_classification": "MISSING_DETECTION_TIME",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": None,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "MISSING_DETECTION_TIME: detection timestamp is missing or not timezone-aware",
                "execution_status": exec_status,
                "execution_net_return_pct": exec_net_return,
            }

        # 4. Handle unresolved, censored, or gap records
        if outcome_status not in {"TARGET_REACHED", "TARGET_NOT_REACHED"}:
            censored = bool(outcome_data.get("censored") or outcome_status in {"UNKNOWN_DUE_TO_GAP", "PRICE_SOURCE_CONFLICT", "EXPIRED"})
            classification = outcome_status or ("CENSORED" if censored else "INCOMPLETE_OBSERVATION")
            self.diagnostics["unresolved_outcomes"] += 1
            return {
                "outcome_classification": classification,
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": None,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "UNRESOLVED_INCOMPLETE_OR_CENSORED",
                "execution_status": exec_status,
                "execution_net_return_pct": exec_net_return,
            }

        # 5. Require verified candle evidence (real MFE)
        if mfe_pct is None:
            self.diagnostics["unresolved_outcomes"] += 1
            return {
                "outcome_classification": "INCOMPLETE_OBSERVATION",
                "target_reached": None,
                "market_target_reached": None,
                "mfe_pct": None,
                "mae_pct": None,
                "hours_to_target": None,
                "is_evaluated": False,
                "resolution_reason": "MISSING_MFE_EVIDENCE: verified candle trace missing mfe_pct",
                "execution_status": exec_status,
                "execution_net_return_pct": exec_net_return,
            }

        # 6. For TARGET_REACHED, validate touch timestamp and elapsed hours
        if outcome_status == "TARGET_REACHED":
            if touch_time is None and touch_dt is None:
                self.diagnostics["missing_touch_evidence_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return {
                    "outcome_classification": "MISSING_TARGET_TOUCH_EVIDENCE",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": mae_pct,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": "MISSING_TARGET_TOUCH_EVIDENCE: TARGET_REACHED requires verifiable touch timestamp or elapsed time",
                    "execution_status": exec_status,
                    "execution_net_return_pct": exec_net_return,
                }

            if touch_dt is not None:
                if touch_dt < detection_dt:
                    self.diagnostics["evidence_conflict_records"] += 1
                    self.diagnostics["unresolved_outcomes"] += 1
                    return {
                        "outcome_classification": "INVALID_TOUCH_TIMESTAMP",
                        "target_reached": None,
                        "market_target_reached": None,
                        "mfe_pct": mfe_pct,
                        "mae_pct": mae_pct,
                        "hours_to_target": None,
                        "is_evaluated": False,
                        "resolution_reason": f"INVALID_TOUCH_TIMESTAMP: touch timestamp {touch_dt} precedes detection {detection_dt}",
                        "execution_status": exec_status,
                        "execution_net_return_pct": exec_net_return,
                    }
                calculated_elapsed_hours = (touch_dt - detection_dt).total_seconds() / 3600.0
                if touch_time is not None and abs(calculated_elapsed_hours - touch_time) > 0.1:
                    self.diagnostics["evidence_conflict_records"] += 1
                    self.diagnostics["unresolved_outcomes"] += 1
                    return {
                        "outcome_classification": "OUTCOME_EVIDENCE_CONFLICT",
                        "target_reached": None,
                        "market_target_reached": None,
                        "mfe_pct": mfe_pct,
                        "mae_pct": mae_pct,
                        "hours_to_target": None,
                        "is_evaluated": False,
                        "resolution_reason": (
                            f"OUTCOME_EVIDENCE_CONFLICT: touch timestamp elapsed {calculated_elapsed_hours:.2f}h "
                            f"conflicts with claimed elapsed time {touch_time:.2f}h (tolerance 0.1h)"
                        ),
                        "execution_status": exec_status,
                        "execution_net_return_pct": exec_net_return,
                    }
                if touch_time is None:
                    touch_time = calculated_elapsed_hours

            if touch_time is not None:
                if touch_time < 0.0:
                    self.diagnostics["evidence_conflict_records"] += 1
                    self.diagnostics["unresolved_outcomes"] += 1
                    return {
                        "outcome_classification": "INVALID_TOUCH_TIMESTAMP",
                        "target_reached": None,
                        "market_target_reached": None,
                        "mfe_pct": mfe_pct,
                        "mae_pct": mae_pct,
                        "hours_to_target": touch_time,
                        "is_evaluated": False,
                        "resolution_reason": f"INVALID_TOUCH_TIMESTAMP: touch time {touch_time} is negative",
                        "execution_status": exec_status,
                        "execution_net_return_pct": exec_net_return,
                    }
                if touch_time > 24.0:
                    self.diagnostics["target_outside_window_records"] += 1
                    self.diagnostics["unresolved_outcomes"] += 1
                    return {
                        "outcome_classification": "TARGET_TOUCH_OUTSIDE_WINDOW",
                        "target_reached": None,
                        "market_target_reached": None,
                        "mfe_pct": mfe_pct,
                        "mae_pct": mae_pct,
                        "hours_to_target": touch_time,
                        "is_evaluated": False,
                        "resolution_reason": f"TARGET_TOUCH_OUTSIDE_WINDOW: touch time {touch_time:.2f}h occurs outside the 24-hour evaluation window",
                        "execution_status": exec_status,
                        "execution_net_return_pct": exec_net_return,
                    }

        # 7. Validate verifiable candle-trace evidence schema and provenance
        detection_price = _clean_num(item.get("detection_price")) or 1.0
        is_trace_valid, trace_rej, trace_info = self._validate_candle_trace(
            outcome_data=outcome_data,
            forecast_canon_id=forecast_canon_id,
            detection_dt=detection_dt,
            outcome_status=outcome_status,
            mfe_pct=mfe_pct,
            touch_time=touch_time,
            touch_dt=touch_dt,
            effective_target_pct=effective_target_pct,
            detection_price=detection_price,
        )
        if not is_trace_valid:
            return trace_rej  # type: ignore[return-value]

        if trace_info is not None and outcome_status == "TARGET_REACHED":
            if touch_time is None:
                touch_time = trace_info.get("derived_touch_hours")

        # 8. Check for contradictory outcome classification vs candle evidence
        # Case A: status is TARGET_NOT_REACHED, but evidence says target was hit
        if outcome_status == "TARGET_NOT_REACHED":
            if claimed_target_reached is True or mfe_pct >= effective_target_pct:
                self.diagnostics["evidence_conflict_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return {
                    "outcome_classification": "OUTCOME_EVIDENCE_CONFLICT",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": mae_pct,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"OUTCOME_EVIDENCE_CONFLICT: status is TARGET_NOT_REACHED but mfe_pct={mfe_pct}% >= {effective_target_pct}% or claimed target_reached=True",
                    "execution_status": exec_status,
                    "execution_net_return_pct": exec_net_return,
                }

        # Case B: status is TARGET_REACHED, but evidence says target was NOT hit
        if outcome_status == "TARGET_REACHED":
            if claimed_target_reached is False or mfe_pct < effective_target_pct:
                self.diagnostics["evidence_conflict_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return {
                    "outcome_classification": "OUTCOME_EVIDENCE_CONFLICT",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": mae_pct,
                    "hours_to_target": None,
                    "is_evaluated": False,
                    "resolution_reason": f"OUTCOME_EVIDENCE_CONFLICT: status is TARGET_REACHED but mfe_pct={mfe_pct}% < {effective_target_pct}% or claimed target_reached=False",
                    "execution_status": exec_status,
                    "execution_net_return_pct": exec_net_return,
                }
                if touch_time is None:
                    touch_time = calculated_elapsed_hours

            # 9. Validate observation window bounds (0.0 to 24.0 hours)
            if touch_time < 0.0:
                self.diagnostics["evidence_conflict_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return {
                    "outcome_classification": "INVALID_TOUCH_TIMESTAMP",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": mae_pct,
                    "hours_to_target": touch_time,
                    "is_evaluated": False,
                    "resolution_reason": f"INVALID_TOUCH_TIMESTAMP: touch time {touch_time} is negative",
                    "execution_status": exec_status,
                    "execution_net_return_pct": exec_net_return,
                }
            if touch_time > 24.0:
                self.diagnostics["target_outside_window_records"] += 1
                self.diagnostics["unresolved_outcomes"] += 1
                return {
                    "outcome_classification": "TARGET_TOUCH_OUTSIDE_WINDOW",
                    "target_reached": None,
                    "market_target_reached": None,
                    "mfe_pct": mfe_pct,
                    "mae_pct": mae_pct,
                    "hours_to_target": touch_time,
                    "is_evaluated": False,
                    "resolution_reason": f"TARGET_TOUCH_OUTSIDE_WINDOW: touch time {touch_time:.2f}h exceeds 24.0h observation window",
                    "execution_status": exec_status,
                    "execution_net_return_pct": exec_net_return,
                }

        # 10. Valid, non-contradictory verified outcome
        market_target_reached = bool(outcome_status == "TARGET_REACHED")
        self.diagnostics["successfully_matched_outcomes"] += 1
        evidence_audit = {
            "evidence_level": "PROVIDER_ATTRIBUTED_INGESTION_AUDITED",
            "provider": trace_info.get("provider") if trace_info else None,
            "source_ref": trace_info.get("source_ref") if trace_info else None,
            "source_domain": trace_info.get("source_domain") if trace_info else None,
            "request_url": trace_info.get("request_url") if trace_info else None,
            "http_status": trace_info.get("http_status") if trace_info else None,
            "content_hash": trace_info.get("content_hash") if trace_info else None,
            "content_hash_verified": trace_info.get("content_hash_verified") if trace_info else False,
            "response_reconciliation_verified": trace_info.get("response_reconciliation_verified") if trace_info else False,
            "retrieved_utc": trace_info.get("retrieved_utc") if trace_info else None,
            "response_retrieval_utc": trace_info.get("response_retrieval_utc") if trace_info else None,
            "provider_metadata": trace_info.get("provider_metadata") if trace_info else None,
            "authenticity_note": (
                "Evidence is provider-attributed and ingestion-audited via captured HTTP response "
                "reconciliation and SHA-256 content hashing. The SHA-256 digest proves content integrity "
                "relative to the captured body, not provider origin. It is not cryptographically signed."
            ),
        }
        return {
            "outcome_classification": outcome_status,
            "target_reached": market_target_reached,
            "market_target_reached": market_target_reached,
            "mfe_pct": mfe_pct,
            "mae_pct": mae_pct,
            "hours_to_target": touch_time if market_target_reached else None,
            "is_evaluated": True,
            "resolution_reason": source_reason,
            "execution_status": exec_status,
            "execution_net_return_pct": exec_net_return,
            "evidence_audit": evidence_audit,
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
