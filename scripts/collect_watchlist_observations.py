#!/usr/bin/env python3
"""Foreground-only bounded collection of existing watchlist OHLCV observations."""
from __future__ import annotations

import argparse
import collections
import contextlib
import datetime as dt
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEMORY = ROOT / "rocket-hunter-data" / "scanner-memory"
DEFAULT_OUTPUT_ROOT = ROOT / "rocket-hunter-data" / "research-data" / "watchlist-observations"
DEFAULT_EXPORTS = ROOT / "rocket-hunter-data" / "research-exports"
DEFAULT_ANALYSIS = ROOT / "rocket-hunter-data" / "research-analysis"
DEFAULT_RUNS = ROOT / "rocket-runs"
OBSERVATION_SCHEMA = "crypto-prebreakout.market-observation.v1"
COLLECTOR_VERSION = "rocket-hunter-watchlist-collector.v1"
MIN_DURATION_MINUTES = 1
MAX_DURATION_MINUTES = 180
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 3600
MAX_CANDIDATES = 12
MAX_REQUESTS = 500
REQUEST_TIMEOUT_SECONDS = 8
PACING_SECONDS = 12.0
IDENTITY_FIELDS = (
    "identity", "market_key", "chain", "contract_address", "token_mint",
    "pool_address", "pair_address", "venue", "base_asset", "quote_asset",
    "quote_symbol", "pair", "pair_orientation",
)

try:
    from scripts import v425_early_breakout as monitor
    from scripts import venues
    from scripts.file_lock import shared_rate_limit
except ImportError:
    import v425_early_breakout as monitor
    import venues
    from file_lock import shared_rate_limit


class CollectorError(RuntimeError):
    """Raised when collection inputs or the separate research destination are unsafe."""


class _RequestLimitReached(RuntimeError):
    """Stops a CEX adapter before it sends beyond the collector's hard request cap."""


class _ProviderRateLimited(RuntimeError):
    """Prevents further CEX requests after the provider returns a rate-limit response."""


class _CollectionDeadlineReached(RuntimeError):
    """Stops a CEX adapter when the foreground session deadline expires."""


class _BoundedVenueFetcher(venues.Fetcher):
    """Use the existing venue client while pacing and counting every HTTP request."""

    def __init__(self, rate_path: Path, request_limit: int, rate_limit_fn,
                 deadline_monotonic: float):
        super().__init__(timeout=REQUEST_TIMEOUT_SECONDS, retries=0, pause=0)
        self.rate_path = rate_path
        self.request_limit = request_limit
        self.requests_before = 0
        self.rate_limit_fn = rate_limit_fn
        self.deadline_monotonic = deadline_monotonic
        self.requests_started = 0
        self.request_limit_reached = False
        self.provider_rate_limited = False
        self.duration_elapsed = False
        self.last_request_started_utc = None

    def get(self, url):
        if self.provider_rate_limited:
            raise _ProviderRateLimited("provider rate limit reached; collection stopped")
        if self.requests_before + self.requests_started >= self.request_limit:
            self.request_limit_reached = True
            raise _RequestLimitReached("collector request limit reached")
        if time.monotonic() >= self.deadline_monotonic:
            self.duration_elapsed = True
            raise _CollectionDeadlineReached("collector duration limit reached")
        self.rate_limit_fn(
            self.rate_path, max(PACING_SECONDS, monitor.GECKO_POSITION_INTERVAL_SECONDS)
        )
        remaining = self.deadline_monotonic - time.monotonic()
        if remaining <= 1.0:
            self.duration_elapsed = True
            raise _CollectionDeadlineReached("collector duration limit reached")
        self.timeout = max(1, min(REQUEST_TIMEOUT_SECONDS, int(remaining)))
        self.requests_started += 1
        self.last_request_started_utc = _utc_iso()
        result = super().get(url)
        if self.log and self.log[-1].get("status") in (418, 429):
            self.provider_rate_limited = True
        return result


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc_iso(value: dt.datetime | None = None) -> str:
    return (value or _utc_now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_provider_cooldown(path: Path, now: dt.datetime) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise CollectorError(f"provider cooldown state must be a regular file: {path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorError(f"cannot read provider cooldown state: {path}") from exc
    if not isinstance(state, dict) or state.get("provider") != "geckoterminal":
        raise CollectorError(f"provider cooldown state has an unsupported shape: {path}")
    until = _parse_utc(state.get("cooldown_until_utc"))
    if until is None:
        raise CollectorError(f"provider cooldown state has an invalid expiry: {path}")
    if until <= now:
        return None
    return state


def _parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(dt.timezone.utc)


def _write_provider_cooldown(path: Path, metadata: dict[str, Any], observed_utc: str) -> None:
    status = metadata.get("http_status")
    if metadata.get("provider") != "geckoterminal" or (
        status not in (418, 429)
        and not (status == 403 and metadata.get("retry_after_available") is True)
    ):
        return
    until = _parse_utc(metadata.get("cooldown_until_utc"))
    if until is None:
        return
    observed_at = _parse_utc(observed_utc) or _utc_now()
    previous = _read_provider_cooldown(path, observed_at)
    if previous is not None:
        previous_until = _parse_utc(previous.get("cooldown_until_utc"))
        if previous_until is not None and previous_until > until:
            return
    state = {
        "provider": "geckoterminal",
        "http_status": metadata["http_status"],
        "observed_utc": observed_utc,
        "retry_after_seconds": metadata.get("retry_after_seconds"),
        "cooldown_until_utc": metadata["cooldown_until_utc"],
        "retry_after_clamped": bool(metadata.get("retry_after_clamped", False)),
        "cooldown_status": metadata.get("cooldown_status", "bounded_retry_after"),
        "cooldown_source": "provider Retry-After response header",
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


@contextlib.contextmanager
def _locked_writer(path: Path, deadline_monotonic: float):
    """Acquire one cross-process writer lock, but never wait past the run deadline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    handle = path.open("r+b")
    acquired = False
    lock_deadline = min(deadline_monotonic, time.monotonic() + 1.0)
    try:
        if os.name == "nt":
            import msvcrt
            while not acquired:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    remaining = lock_deadline - time.monotonic()
                    if remaining <= 0:
                        raise CollectorError("another collector is already active")
                    time.sleep(min(0.1, remaining))
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
        else:
            import fcntl
            while not acquired:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    remaining = lock_deadline - time.monotonic()
                    if remaining <= 0:
                        raise CollectorError("another collector is already active")
                    time.sleep(min(0.1, remaining))
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _ensure_disjoint_paths(memory_dir: Path, output_root: Path) -> None:
    output = output_root.resolve()
    protected_roots = (
        memory_dir.resolve(), DEFAULT_EXPORTS.resolve(), DEFAULT_ANALYSIS.resolve(),
        DEFAULT_RUNS.resolve(),
    )
    for protected in protected_roots:
        if output == protected or output in protected.parents or protected in output.parents:
            raise CollectorError(
                "research output must be separate from scanner memory, exports, analysis, and scan outputs"
            )


def _load_watchlist(path: Path, max_candidates: int) -> tuple[list[dict[str, Any]], str]:
    if path.is_symlink() or not path.is_file():
        raise CollectorError(f"watchlist must be a regular file: {path}")
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorError(f"cannot read watchlist JSON: {path}") from exc
    if isinstance(document, dict):
        rows = document.get("positions")
    elif isinstance(document, list):
        rows = document
    else:
        rows = None
    if not isinstance(rows, list):
        raise CollectorError("watchlist must be a positions object or an array of candidate records")
    unique = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        identity = row.get("identity") or row.get("market_key")
        # Do not synthesize identity from a ticker, symbol, pool, or contract alone.
        if not isinstance(identity, str) or not identity.strip():
            unique.append(row)
            continue
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(row)
    return unique[:max_candidates], hashlib.sha256(raw).hexdigest()


def _configuration_fingerprint(args, watchlist_path: Path) -> str:
    config = {
        "collector_version": COLLECTOR_VERSION,
        "duration_minutes": args.duration_minutes,
        "interval_seconds": args.interval_seconds,
        "max_candidates": args.max_candidates,
        "max_requests": args.max_requests,
        "request_pacing_seconds": max(PACING_SECONDS, monitor.GECKO_POSITION_INTERVAL_SECONDS),
        "watchlist_path": str(watchlist_path.resolve()),
        "dry_run": args.dry_run,
        "once": args.once,
    }
    return hashlib.sha256(_json_text(config).encode("utf-8")).hexdigest()


def _identity_fields(position: dict[str, Any]) -> dict[str, Any]:
    return {field: position.get(field) for field in IDENTITY_FIELDS}


def _source_run_id(position: dict[str, Any]) -> str | None:
    value = position.get("created_run_id")
    return value if isinstance(value, str) and value else None


def _market_type(position: dict[str, Any]) -> str:
    value = str(position.get("market_type") or "").lower()
    if value:
        return value
    if position.get("pool_address") or position.get("pair_address"):
        return "dex_pool"
    if position.get("venue"):
        return "cex_spot"
    return ""


def _missing_fetch_fields(position: dict[str, Any]) -> list[str]:
    market_type = _market_type(position)
    if market_type == "dex_pool":
        required = ("chain", "pool_address", "contract_address")
    elif market_type == "cex_spot":
        required = ("venue", "venue_pair")
    else:
        return ["supported_market_type"]
    missing = [field for field in required if not isinstance(position.get(field), str)
               or not position[field].strip()]
    if not isinstance(position.get("identity") or position.get("market_key"), str):
        missing.append("identity_or_market_key")
    return missing


def _fetch_observation(position: dict[str, Any], cex_fetcher, request_time: dt.datetime):
    market_type = _market_type(position)
    if market_type == "dex_pool":
        bars, url, error, provider_metadata = monitor._fetch_gecko_position_bars_with_metadata(
            position, timeout=cex_fetcher.timeout
        )
        source = "GeckoTerminal exact-token USD 1h OHLCV"
    else:
        bars, url, error = monitor._fetch_cex_position_bars(
            position, venues, cex_fetcher, request_time
        )
        source = "existing CEX venue 1h OHLCV adapter"
        provider_metadata = None
    return bars, url, error, source, provider_metadata


def calculate_simulated_execution(
    entry_price: float | None,
    target_price: float | None,
    bars_5m: list[dict[str, Any]] | None = None,
    trades: list[dict[str, Any]] | None = None,
    liquidity_usd: float | None = None,
    fee_bps: float = 30.0,
) -> dict[str, Any]:
    """Calculates execution-aware net simulated return, slippage drag, and sequence analysis."""
    if entry_price is None or entry_price <= 0:
        return {"status": "UNVERIFIED", "reason": "Missing or zero entry price"}

    liq = liquidity_usd or 25_000.0
    slippage_drag_pct = 1.0 if liq >= 100_000 else (2.5 if liq >= 25_000 else 5.0)
    fee_drag_pct = (fee_bps / 100.0) * 2.0  # Entry + Exit fee

    high_water_mark = entry_price
    low_water_mark = entry_price
    target_hit = False

    bars = bars_5m or []
    for bar in bars:
        h = bar.get("high") or bar.get("h") or bar.get("close")
        l = bar.get("low") or bar.get("l") or bar.get("close")
        if h and h > high_water_mark:
            high_water_mark = h
        if l and l < low_water_mark:
            low_water_mark = l
        if target_price and h and h >= target_price:
            target_hit = True

    mfe_pct = round(((high_water_mark - entry_price) / entry_price) * 100.0, 2)
    mae_pct = round(((low_water_mark - entry_price) / entry_price) * 100.0, 2)
    net_return_pct = round(mfe_pct - slippage_drag_pct - fee_drag_pct, 2) if target_hit else round(mae_pct - slippage_drag_pct - fee_drag_pct, 2)

    return {
        "status": "SIMULATED",
        "entry_price": entry_price,
        "target_price": target_price,
        "liquidity_usd": liq,
        "fee_drag_pct": round(fee_drag_pct, 2),
        "slippage_drag_pct": round(slippage_drag_pct, 2),
        "target_hit": target_hit,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "net_simulated_return_pct": net_return_pct,
        "sequence_note": "Intrahour high/low sequence evaluated from 5m resolution candles.",
    }


def _is_rate_limit_error(error: str | None) -> bool:
    if not error:
        return False
    normalized = error.lower()
    return "http error 429" in normalized or "http error 418" in normalized


def _make_observation(
    position: dict[str, Any],
    *,
    run_id: str,
    sequence: int,
    scanner_version: str,
    configuration_fingerprint: str,
    watchlist_source: str,
    watchlist_sha256: str,
    source_status: str,
    source: str | None = None,
    source_url: str | None = None,
    error: str | None = None,
    bars: list[dict[str, Any]] | None = None,
    bars_5m: list[dict[str, Any]] | None = None,
    bars_1m: list[dict[str, Any]] | None = None,
    trades: list[dict[str, Any]] | None = None,
    liquidity_snapshot: dict[str, Any] | None = None,
    simulated_execution: dict[str, Any] | None = None,
    request_started_utc: str | None = None,
    source_received_utc: str | None = None,
    provider_rate_limit: dict[str, Any] | None = None,
    provider_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    collected = _utc_iso()
    record_id = hashlib.sha256(f"{run_id}:{sequence}".encode("utf-8")).hexdigest()
    return {
        "schema": OBSERVATION_SCHEMA,
        "record_id": record_id,
        "record_type": "watchlist_market_observation",
        "collection_run_id": run_id,
        "sequence": sequence,
        "collected_utc": collected,
        "ingested_utc": collected,
        "scanner_version": scanner_version,
        "source_scanner_run_id": _source_run_id(position),
        "configuration_fingerprint": configuration_fingerprint,
        "watchlist_source": {
            "path": watchlist_source,
            "sha256": watchlist_sha256,
        },
        "identity_fields": _identity_fields(position),
        "source": source,
        "source_url": source_url,
        "source_status": source_status,
        "request_started_utc": request_started_utc,
        "source_received_utc": source_received_utc,
        "provider_rate_limit": provider_rate_limit,
        "provider_response": provider_response,
        "error": error,
        "source_evidence": {
            "resolution": "1h",
            "bars": bars or [],
            "evidence_note": (
                "Existing provider adapters return normalized completed OHLCV bars; "
                "raw response headers/body are not retained by those adapters."
            ),
        },
        "enhanced_evidence": {
            "bars_5m_count": len(bars_5m or []),
            "bars_5m": bars_5m or [],
            "bars_1m_count": len(bars_1m or []),
            "bars_1m": bars_1m or [],
            "trades_count": len(trades or []),
            "trades": trades or [],
            "liquidity_snapshot": liquidity_snapshot,
            "simulated_execution": simulated_execution,
        },
    }


def _write_observation(stream, observation: dict[str, Any]) -> None:
    stream.write(_json_text(observation) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def _read_scanner_version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def collect(
    args,
    *,
    now_fn=_utc_now,
    sleep_fn=time.sleep,
    fetch_fn=None,
    rate_limit_fn=shared_rate_limit,
) -> Path | None:
    watchlist_path = Path(args.watchlist).absolute()
    output_root = Path(args.output_root)
    _ensure_disjoint_paths(Path(args.memory_dir), output_root)
    positions, watchlist_sha256 = _load_watchlist(watchlist_path, args.max_candidates)
    if args.dry_run:
        print(json.dumps({
            "mode": "dry-run",
            "network_requests": 0,
            "watchlist_records_selected": len(positions),
            "watchlist_sha256": watchlist_sha256,
            "candidate_statuses": [
                {"identity": row.get("identity") or row.get("market_key"),
                 "missing_fields": _missing_fetch_fields(row)}
                for row in positions
            ],
        }, indent=2, ensure_ascii=False))
        return None

    output_root.mkdir(parents=True, exist_ok=True)
    if output_root.is_symlink():
        raise CollectorError(f"research output root must not be a symlink: {output_root}")
    run_id = f"{now_fn().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    run_dir = output_root / f"collection-{run_id}"
    run_dir.mkdir(exist_ok=False)
    config_fingerprint = _configuration_fingerprint(args, watchlist_path)
    scanner_version = _read_scanner_version()
    started = now_fn()
    deadline = started.timestamp() + args.duration_minutes * 60
    deadline_monotonic = time.monotonic() + args.duration_minutes * 60
    counts: collections.Counter[str] = collections.Counter()
    request_count = 0
    sequence = 0
    stop_status = "COMPLETED"
    rate_path = output_root / ".gecko-cex-request-rate.json"
    cooldown_path = output_root / ".provider-cooldown.json"
    provider_rate_metadata = None
    provider_response_metadata = None
    cex_fetcher = _BoundedVenueFetcher(
        rate_path, args.max_requests, rate_limit_fn, deadline_monotonic
    )
    fetch = fetch_fn or _fetch_observation

    try:
        # One global lock serializes writers and provider polling across collector sessions.
        with _locked_writer(output_root / ".collector.lock", deadline_monotonic):
            provider_response_metadata = _read_provider_cooldown(cooldown_path, now_fn())
            if provider_response_metadata is not None:
                if provider_response_metadata.get("http_status") in (418, 429):
                    provider_rate_metadata = provider_response_metadata
                stop_status = "PROVIDER_COOLDOWN_ACTIVE"
            with (run_dir / "observations.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
                first_cycle = True
                while True:
                    if now_fn().timestamp() >= deadline:
                        stop_status = "DURATION_ELAPSED"
                        break
                    if not first_cycle:
                        if args.once:
                            stop_status = "ONE_CYCLE_COMPLETED"
                            break
                        remaining = deadline - now_fn().timestamp()
                        if remaining <= 0:
                            stop_status = "DURATION_ELAPSED"
                            break
                        sleep_fn(min(args.interval_seconds, remaining))
                        if now_fn().timestamp() >= deadline:
                            stop_status = "DURATION_ELAPSED"
                            break
                    first_cycle = False
                    cycle_requested = 0
                    for position in positions:
                        missing = _missing_fetch_fields(position)
                        if missing:
                            sequence += 1
                            observation = _make_observation(
                                position, run_id=run_id, sequence=sequence,
                                scanner_version=scanner_version,
                                configuration_fingerprint=config_fingerprint,
                                watchlist_source=str(watchlist_path),
                                watchlist_sha256=watchlist_sha256,
                                source_status="SKIPPED_INCOMPLETE_IDENTITY",
                                error="missing fields: " + ", ".join(missing),
                            )
                            _write_observation(stream, observation)
                            counts["skipped_incomplete_identity"] += 1
                            continue
                        if stop_status == "PROVIDER_COOLDOWN_ACTIVE":
                            sequence += 1
                            observation = _make_observation(
                                position, run_id=run_id, sequence=sequence,
                                scanner_version=scanner_version,
                                configuration_fingerprint=config_fingerprint,
                                watchlist_source=str(watchlist_path),
                                watchlist_sha256=watchlist_sha256,
                                source_status="SKIPPED_PROVIDER_COOLDOWN",
                                error="GeckoTerminal Retry-After cooldown is still active",
                                provider_rate_limit=provider_rate_metadata,
                                provider_response=provider_response_metadata,
                            )
                            _write_observation(stream, observation)
                            counts["skipped_provider_cooldown"] += 1
                            continue
                        if stop_status == "PROVIDER_RATE_LIMITED":
                            sequence += 1
                            observation = _make_observation(
                                position, run_id=run_id, sequence=sequence,
                                scanner_version=scanner_version,
                                configuration_fingerprint=config_fingerprint,
                                watchlist_source=str(watchlist_path),
                                watchlist_sha256=watchlist_sha256,
                                source_status="SKIPPED_PROVIDER_RATE_LIMIT",
                                error="a provider returned HTTP 418 or 429 earlier in this session",
                                provider_rate_limit=provider_rate_metadata,
                                provider_response=provider_response_metadata,
                            )
                            _write_observation(stream, observation)
                            counts["skipped_provider_rate_limit"] += 1
                            continue
                        if stop_status == "PROVIDER_ACCESS_DENIED":
                            sequence += 1
                            observation = _make_observation(
                                position, run_id=run_id, sequence=sequence,
                                scanner_version=scanner_version,
                                configuration_fingerprint=config_fingerprint,
                                watchlist_source=str(watchlist_path),
                                watchlist_sha256=watchlist_sha256,
                                source_status="SKIPPED_PROVIDER_ACCESS_DENIED",
                                error="GeckoTerminal returned HTTP 403 earlier in this session",
                                provider_response=provider_response_metadata,
                            )
                            _write_observation(stream, observation)
                            counts["skipped_provider_access_denied"] += 1
                            continue
                        if request_count >= args.max_requests:
                            stop_status = "REQUEST_LIMIT_REACHED"
                            sequence += 1
                            observation = _make_observation(
                                position, run_id=run_id, sequence=sequence,
                                scanner_version=scanner_version,
                                configuration_fingerprint=config_fingerprint,
                                watchlist_source=str(watchlist_path),
                                watchlist_sha256=watchlist_sha256,
                                source_status="SKIPPED_REQUEST_LIMIT",
                                error="collection request limit reached",
                            )
                            _write_observation(stream, observation)
                            counts["skipped_request_limit"] += 1
                            continue
                        if now_fn().timestamp() >= deadline:
                            stop_status = "DURATION_ELAPSED"
                            break
                        market_type = _market_type(position)
                        if market_type == "dex_pool":
                            rate_limit_fn(
                                rate_path, max(PACING_SECONDS, monitor.GECKO_POSITION_INTERVAL_SECONDS)
                            )
                            remaining = deadline_monotonic - time.monotonic()
                            if now_fn().timestamp() >= deadline or remaining <= 1.0:
                                stop_status = "DURATION_ELAPSED"
                                break
                            cex_fetcher.timeout = max(1, min(REQUEST_TIMEOUT_SECONDS, int(remaining)))
                            request_count += 1
                            cycle_requested += 1
                        else:
                            cex_fetcher.requests_before = request_count
                        previous_requests = cex_fetcher.requests_started
                        sequence += 1
                        request_started = _utc_iso(now_fn())
                        try:
                            fetch_result = fetch(position, cex_fetcher, now_fn())
                            if len(fetch_result) == 5:
                                bars, url, error, source, provider_metadata = fetch_result
                            else:
                                bars, url, error, source = fetch_result
                                provider_metadata = None
                        except Exception as exc:
                            bars, url, error, source, provider_metadata = (
                                [], None, f"{type(exc).__name__}: {exc}", None, None
                            )
                        if market_type == "cex_spot" and cex_fetcher.last_request_started_utc:
                            request_started = cex_fetcher.last_request_started_utc
                        actual_requests = cex_fetcher.requests_started - previous_requests
                        if market_type == "cex_spot":
                            request_count += actual_requests
                            cycle_requested += actual_requests
                        if cex_fetcher.provider_rate_limited:
                            stop_status = "PROVIDER_RATE_LIMITED"
                        elif cex_fetcher.request_limit_reached:
                            stop_status = "REQUEST_LIMIT_REACHED"
                        elif cex_fetcher.duration_elapsed:
                            stop_status = "DURATION_ELAPSED"
                        elif market_type == "dex_pool" and _is_rate_limit_error(error):
                            stop_status = "PROVIDER_RATE_LIMITED"
                        elif (
                            market_type == "dex_pool"
                            and isinstance(provider_metadata, dict)
                            and provider_metadata.get("http_status") == 403
                        ):
                            stop_status = "PROVIDER_ACCESS_DENIED"
                        if provider_metadata is not None:
                            provider_response_metadata = provider_metadata
                            if provider_metadata.get("http_status") in (418, 429):
                                provider_rate_metadata = provider_metadata
                            _write_provider_cooldown(
                                cooldown_path, provider_metadata, _utc_iso(now_fn())
                            )
                        received = _utc_iso(now_fn())
                        if stop_status == "PROVIDER_RATE_LIMITED":
                            status = "PROVIDER_RATE_LIMITED"
                            counts["provider_rate_limited"] += 1
                        elif stop_status == "PROVIDER_ACCESS_DENIED":
                            status = "PROVIDER_ACCESS_DENIED"
                            counts["provider_access_denied"] += 1
                        elif cex_fetcher.request_limit_reached:
                            status = "REQUEST_LIMIT_REACHED"
                            counts["request_limit_reached"] += 1
                        elif error:
                            status = "FETCH_FAILED"
                            counts["fetch_failed"] += 1
                        elif bars:
                            status = "OBSERVED"
                            counts["observed"] += 1
                        else:
                            status = "NO_COMPLETED_BARS"
                            counts["no_completed_bars"] += 1
                        entry_p = float(position.get("entry_price") or position.get("price_usd") or (bars[-1].get("close") if bars else 0.0) or 0.0)
                        target_p = float(position.get("single_take_profit") or position.get("target_price") or 0.0)
                        liq_p = float(position.get("liquidity_usd") or 0.0)
                        sim_exec = calculate_simulated_execution(
                            entry_price=entry_p if entry_p > 0 else None,
                            target_price=target_p if target_p > 0 else None,
                            bars_5m=bars,
                            liquidity_usd=liq_p if liq_p > 0 else None,
                        )
                        observation = _make_observation(
                            position, run_id=run_id, sequence=sequence,
                            scanner_version=scanner_version,
                            configuration_fingerprint=config_fingerprint,
                            watchlist_source=str(watchlist_path),
                            watchlist_sha256=watchlist_sha256,
                            source_status=status, source=source, source_url=url,
                            error=error, bars=bars, request_started_utc=request_started,
                            source_received_utc=received,
                            provider_rate_limit=provider_rate_metadata,
                            provider_response=provider_metadata,
                            simulated_execution=sim_exec,
                        )
                        _write_observation(stream, observation)
                    if stop_status in ("REQUEST_LIMIT_REACHED", "PROVIDER_RATE_LIMITED",
                                       "PROVIDER_ACCESS_DENIED",
                                       "DURATION_ELAPSED"):
                        break
                    counts["cycles"] += 1
                    if stop_status == "PROVIDER_COOLDOWN_ACTIVE":
                        break
                    if request_count >= args.max_requests:
                        stop_status = "REQUEST_LIMIT_REACHED"
                        break
                    if args.once or cycle_requested == 0:
                        stop_status = "ONE_CYCLE_COMPLETED" if args.once else "NO_REQUESTS_AVAILABLE"
                        break
    except KeyboardInterrupt:
        stop_status = "INTERRUPTED"
    except Exception:
        stop_status = "FAILED"
        raise
    finally:
        finished = now_fn()
        manifest = {
            "schema": "crypto-prebreakout.watchlist-observation-manifest.v1",
            "collector_version": COLLECTOR_VERSION,
            "status": stop_status,
            "collection_run_id": run_id,
            "started_utc": _utc_iso(started),
            "finished_utc": _utc_iso(finished),
            "duration_seconds": round((finished - started).total_seconds(), 3),
            "scanner_version": scanner_version,
            "configuration_fingerprint": config_fingerprint,
            "request_pacing_seconds": max(PACING_SECONDS, monitor.GECKO_POSITION_INTERVAL_SECONDS),
            "watchlist_source": str(watchlist_path),
            "watchlist_sha256": watchlist_sha256,
            "selected_candidate_count": len(positions),
            "requests_started": request_count,
            "record_count": sequence,
            "record_status_counts": dict(sorted(counts.items())),
            "source_access": "read-only",
            "provider_rate_limit": provider_rate_metadata,
            "provider_response": provider_response_metadata,
            "output_file": "observations.jsonl",
            "metrics_supported_by_complete_hourly_bars": [
                "candle-level observed target presence from high values, if forecast baseline and complete hourly coverage are available",
                "hourly-bar-resolution adverse excursion from low values, if complete coverage is available",
                "endpoint mark paths from close values and event-time timestamps",
            ],
            "metrics_still_unsupported": [
                "exact intrahour target-crossing time or ordering when multiple levels occur in one hourly candle",
                "target-touch claims across missing or incomplete candle coverage",
                "executable fills, fees, slippage, and quote-adjusted returns",
                "instantaneous price/liquidity at the collector's received timestamp; adapters return completed hourly bars",
            ],
        }
        temp_manifest = run_dir / "manifest.json.tmp"
        temp_manifest.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_manifest.replace(run_dir / "manifest.json")
    print(f"Collection {stop_status}: {request_count} requests, {sequence} observation records; {run_dir}")
    return run_dir


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY,
                        help="read-only source of active-24h-watchlist.json")
    parser.add_argument("--watchlist", type=Path, default=None,
                        help="read-only watchlist JSON; defaults to active-24h-watchlist.json")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--duration-minutes", type=int, default=60,
                        help=f"foreground session duration, {MIN_DURATION_MINUTES}-{MAX_DURATION_MINUTES} minutes")
    parser.add_argument("--interval-seconds", type=int, default=300,
                        help="delay between polling cycles, 60-3600 seconds")
    parser.add_argument("--max-candidates", type=int, default=MAX_CANDIDATES,
                        help=f"candidate cap, 1-{MAX_CANDIDATES}")
    parser.add_argument("--max-requests", type=int, default=120,
                        help=f"hard provider request cap, 1-{MAX_REQUESTS}")
    parser.add_argument("--once", action="store_true",
                        help="collect at most one bounded pass and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and show selected candidates without network or output writes")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.watchlist is None:
        args.watchlist = Path(args.memory_dir) / "active-24h-watchlist.json"
    if not MIN_DURATION_MINUTES <= args.duration_minutes <= MAX_DURATION_MINUTES:
        parser.error(f"--duration-minutes must be {MIN_DURATION_MINUTES}-{MAX_DURATION_MINUTES}")
    if not MIN_INTERVAL_SECONDS <= args.interval_seconds <= MAX_INTERVAL_SECONDS:
        parser.error(f"--interval-seconds must be {MIN_INTERVAL_SECONDS}-{MAX_INTERVAL_SECONDS}")
    if not 1 <= args.max_candidates <= MAX_CANDIDATES:
        parser.error(f"--max-candidates must be 1-{MAX_CANDIDATES}")
    if not 1 <= args.max_requests <= MAX_REQUESTS:
        parser.error(f"--max-requests must be 1-{MAX_REQUESTS}")
    try:
        collect(args)
    except CollectorError as exc:
        print(f"Watchlist collection failed: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Watchlist collection failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
