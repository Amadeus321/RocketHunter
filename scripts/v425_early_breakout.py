#!/usr/bin/env python3
"""v4.25.1 — free cross-chain early-breakout discovery and 24h peak monitoring.

Read-only research. Uses DexPaprika's keyless public REST tier only; no key,
paid service, wallet, trade execution, or paid-plan fallback is used.
"""
from __future__ import annotations

import argparse
import copy
import email.utils
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

VERSION = "4.29.0"
EARLY_BREAKOUT_SIGNAL_VERSION = "v4.25-early-breakout.v1"
EARLY_BREAKOUT_MODEL_ID = "v4.25-early-breakout"
API_ROOT = "https://api.dexpaprika.com"
REGISTRY_TTL_SECONDS = 7 * 24 * 60 * 60
# Latest verified DexPaprika agent skill (2026-11-05): keyless allowance
# 10,000 credits over a rolling 30 days and 15 requests/minute per IP.
# Six discovery queries + at most twelve open-position OHLCV calls every two hours
# use about 6,480 request reservations per 30 days. This scanner reserves a 3,000-credit
# buffer for other keyless clients sharing the runner's public IP.
SCANNER_ROLLING_REQUEST_CEILING = 7000
MAX_MONITORED_POSITIONS = 12
MAX_NEW_POSITIONS_PER_SCAN = 1
LEGACY_STOP_FIELDS = (
    "initial_stop_price", "stop_loss_pct", "stop_method", "trailing_stop_price",
    "trailing_stop_armed", "first_protective_stop_touch_candle_utc",
    "possible_protective_stop_touch_candle_utc", "protective_exit_type",
    "protective_exit_reference_price", "intrahour_exit_order_ambiguous",
    "scenario_reward_risk_ratio", "scenario_reward_risk_penalty",
    "latest_scenario_reward_risk_ratio",
)
LEGACY_EXTRA_TARGET_FIELDS = (
    "take_profit_1", "take_profit_2", "take_profit_3",
    "target_1", "target_2", "target_3",
    "tp1", "tp2", "tp3", "tp1_price", "tp2_price", "tp3_price",
)
REQUEST_INTERVAL_SECONDS = 4.2
GECKO_POSITION_ROOT = "https://api.geckoterminal.com/api/v2"
GECKO_POSITION_INTERVAL_SECONDS = 6.2
GECKO_MAX_RETRY_AFTER_SECONDS = 7 * 24 * 60 * 60
GECKO_POSITION_ALIASES = {
    "avalanche": "avax", "sui": "sui-network", "fantom": "ftm",
    "cronos": "cro", "sei": "sei-network", "polygon": "polygon_pos",
    "ethereum": "eth", "binance-smart-chain": "bsc",
}
MAX_SEARCH_ROWS = 100
OHLCV_INTERVAL = "1h"
OHLCV_LIMIT = 30
USER_AGENT = "crypto-prebreakout-research/v4.29.0 (read-only research)"
EARLY_FIELDS = (
    "early_breakout_score", "early_breakout_tier", "early_breakout_components",
    "early_breakout_risk_flags", "early_breakout_signal_version",
    "early_breakout_model_id",
    # 9-Engine Imminent Breakout and 24h Probabilistic Outcomes
    "breakout_confluence_score", "breakout_engine_scores", "breakout_acceleration_signals",
    "engine_evidence_status", "evidence_coverage_pct", "breakout_evidence_eligible",
    "probability_tp_hit_24h_pct", "expected_net_24h_return_pct", "potential_mfe_24h_pct",
    "potential_mae_24h_pct", "estimated_hours_to_target", "reversal_risk_score",
    "realistic_opportunity_rank",
    # Carry the actual observations used by the score into the final 24h board.
    # Previously only the opaque score survived compaction, hiding momentum,
    # volume and liquidity evidence from the ranking output.
    "change_5m_pct", "change_1h_pct", "change_6h_pct", "change_24h_pct",
    "price_usd", "liquidity_usd", "volume_24h_usd", "txns_24h",
    "age_hours", "market_cap_usd", "fdv_usd",
    "dexpaprika_family", "dexpaprika_rank",
    "dexpaprika_network", "dexpaprika_pool_id", "dexpaprika_created_at",
    "dexpaprika_1h_buy_usd", "dexpaprika_1h_sell_usd",
    "dexpaprika_5m_buy_usd", "dexpaprika_5m_sell_usd",
    "dexpaprika_1h_volume_usd", "dexpaprika_5m_volume_usd",
    "dexpaprika_1h_txns", "dexpaprika_5m_txns",
    "dexpaprika_provider_timestamp", "quote_token_address",
    "quote_symbol", "dexpaprika_token_order_swapped_for_tracking",
)
POOL_QUERIES = (
    ("pool_momentum_5m", {"order_by": "price_change_percentage_5m", "sort": "desc"}),
    ("pool_momentum_1h", {"order_by": "price_change_percentage_1h", "sort": "desc"}),
    ("pool_momentum_6h", {"order_by": "price_change_percentage_6h", "sort": "desc"}),
    ("pool_momentum_24h", {"order_by": "price_change_percentage_24h", "sort": "desc"}),
    ("pool_volume_24h", {"order_by": "volume_usd_24h", "sort": "desc"}),
    ("new_pools_24h", {"created_after": "-24h", "order_by": "created_at", "sort": "desc"}),
)
STABLE_SYMBOLS = {
    "USDC", "USDT", "DAI", "BUSD", "FDUSD", "USDE", "USDS", "TUSD",
    "USDD", "USDP", "FRAX", "LUSD", "PYUSD", "USD0", "USDBC", "USDC.E",
}
WRAPPED_SYMBOLS = {
    "WETH", "WBNB", "WSOL", "WMATIC", "WAVAX", "WFTM", "WONE",
    "WGLMR", "WROSE", "WSEI", "WTON", "WTRX",
}
# Resolve native/wrapped quote assets by canonical contract or mint address,
# never by a bare ticker that a meme token can spoof.
CHAIN_QUOTE_ADDRESSES = {
    "solana": {"so11111111111111111111111111111111111111112"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _memory_root(hunter: Any = None, explicit: Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    configured = os.environ.get("SCANNER_MEMORY_DIR")
    if configured:
        return Path(configured)
    checkpoint = getattr(hunter, "checkpoint_dir", None) if hunter is not None else None
    if checkpoint:
        return Path(checkpoint) / "provider-memory"
    return Path("rocket-run/provider-memory")


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (OSError, ValueError, TypeError):
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def _rolling_ledger(memory: Path) -> dict[str, Any]:
    path = memory / "dexpaprika-request-ledger.json"
    ledger = _load_json(path, {})
    if not isinstance(ledger, dict):
        ledger = {}
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    requests = ledger.get("requests")
    if not isinstance(requests, list):
        requests = []
    retained = []
    for row in requests:
        if not isinstance(row, dict):
            continue
        stamp = parse_time(row.get("reserved_utc"))
        if stamp is not None and stamp >= cutoff:
            retained.append(row)
    return {
        "schema": "crypto-prebreakout.dexpaprika-request-ledger.v1",
        "provider_version": VERSION,
        "rolling_window_days": 30,
        "scanner_request_ceiling": SCANNER_ROLLING_REQUEST_CEILING,
        "requests": retained,
        "last_observed_credits_remaining": ledger.get("last_observed_credits_remaining"),
        "last_observed_credits_utc": ledger.get("last_observed_credits_utc"),
        "last_observed_plan": ledger.get("last_observed_plan"),
    }


def _reserve_request(memory: Path, family: str) -> tuple[bool, int]:
    memory.mkdir(parents=True, exist_ok=True)
    shared = Path(os.environ.get("ROCKET_HUNTER_SHARED_MEMORY_DIR") or memory).resolve()
    shared.mkdir(parents=True, exist_ok=True)
    try:
        from scripts.file_lock import locked_file
    except ImportError:
        from file_lock import locked_file
    with locked_file(shared / ".dexpaprika-request.lock"):
        ledger = _rolling_ledger(shared)
        used = len(ledger["requests"])
        observed_remaining = _num(ledger.get("last_observed_credits_remaining"))
        # The provider exposes a rolling credit header. Stop early when the
        # remaining account/IP quota approaches the 3,000-credit safety reserve.
        if observed_remaining is not None and observed_remaining <= 3000:
            return False, used
        if used >= SCANNER_ROLLING_REQUEST_CEILING:
            return False, used
        ledger["requests"].append({"reserved_utc": utc_now(), "family": family, "request_reservation": 1})
        _write_json(shared / "dexpaprika-request-ledger.json", ledger)
        _write_json(memory / "dexpaprika-request-ledger.json", ledger)
        return True, len(ledger["requests"])


def _store_quota_headers(memory: Path, headers: dict[str, str]) -> None:
    remaining = _num(headers.get("x-credits-remaining"))
    if remaining is None:
        return
    shared = Path(os.environ.get("ROCKET_HUNTER_SHARED_MEMORY_DIR") or memory).resolve()
    shared.mkdir(parents=True, exist_ok=True)
    try:
        from scripts.file_lock import locked_file
    except ImportError:
        from file_lock import locked_file
    with locked_file(shared / ".dexpaprika-request.lock"):
        ledger = _rolling_ledger(shared)
        ledger["last_observed_credits_remaining"] = int(remaining)
        ledger["last_observed_credits_utc"] = utc_now()
        ledger["last_observed_plan"] = headers.get("x-api-plan")
        _write_json(shared / "dexpaprika-request-ledger.json", ledger)
        _write_json(memory / "dexpaprika-request-ledger.json", ledger)


class DexPaprikaClient:
    """Single-process, keyless client with a conservative 15-request/minute pace."""

    def __init__(self, memory: Path, timeout: int = 8, interval: float = REQUEST_INTERVAL_SECONDS):
        self.memory = Path(memory)
        self.timeout = max(2, int(timeout))
        self.interval = max(4.0, float(interval))
        self.last_request = 0.0
        self.request_count = 0
        self.last_plan_header = None

    def get(self, path: str, params: dict[str, Any] | None = None,
            family: str = "unspecified") -> tuple[Any, dict[str, str], str | None]:
        allowed, reserved = _reserve_request(self.memory, family)
        if not allowed:
            return None, {}, "scanner rolling request ceiling reached (%s/%s)" % (
                reserved, SCANNER_ROLLING_REQUEST_CEILING)
        try:
            from scripts.file_lock import shared_rate_limit
        except ImportError:
            from file_lock import shared_rate_limit
        shared = Path(os.environ.get("ROCKET_HUNTER_SHARED_MEMORY_DIR") or self.memory).resolve()
        shared_rate_limit(shared / ".dexpaprika-request-rate.json", self.interval)
        wait = self.interval - (time.monotonic() - self.last_request)
        if self.last_request and wait > 0:
            time.sleep(wait)
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = API_ROOT + path + (("?" + query) if query else "")
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }, method="GET")
        self.last_request = time.monotonic()
        self.request_count += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
                self.last_plan_header = headers.get("x-api-plan")
                payload = json.loads(response.read().decode("utf-8"))
            _store_quota_headers(self.memory, headers)
            return payload, headers, None
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(800).decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            headers = {str(k).lower(): str(v) for k, v in exc.headers.items()} if exc.headers else {}
            _store_quota_headers(self.memory, headers)
            if exc.code == 429:
                retry_after_raw = headers.get("retry-after")
                # Do not interpret Retry-After: 0 as unrestricted permission to immediately repeat
                try:
                    retry_seconds = float(retry_after_raw.strip()) if retry_after_raw else 30.0
                    if retry_seconds <= 0.0:
                        retry_seconds = 15.0  # Enforce minimum 15s backoff
                except (ValueError, TypeError):
                    retry_seconds = 30.0
                return None, headers, "HTTP 429 Rate limited (Retry-After: %ss backoff enforced): %s" % (retry_seconds, detail)
            return None, headers, "HTTP %s %s" % (exc.code, detail)
        except Exception as exc:
            return None, {}, "%s: %s" % (type(exc).__name__, exc)


def _record_status(hunter: Any, source: str, status: str, url: str | None,
                   row_count: int | None = None, error: str | None = None,
                   extra: dict[str, Any] | None = None) -> None:
    row = {
        "source": source,
        "status": status,
        "url": url,
        "row_count": row_count,
        "retrieved_utc": utc_now(),
        "error": error,
    }
    if extra:
        row.update(extra)
    hunter.source_status.append(row)


def _network_rows(payload: Any) -> list[dict[str, str]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("networks")
        if not isinstance(rows, list):
            data = payload.get("data")
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict):
                rows = data.get("networks") or data.get("items") or data.get("results") or []
            else:
                rows = payload.get("items") or payload.get("results") or []
    else:
        rows = []
    out = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        ident = str(row.get("id") or row.get("chain") or "").strip()
        if not ident or ident.lower() in seen:
            continue
        seen.add(ident.lower())
        out.append({
            "id": ident,
            "display_name": str(row.get("display_name") or row.get("name") or ident),
        })
    return out


def _load_registry(client: DexPaprikaClient) -> tuple[list[dict[str, str]], dict[str, Any]]:
    memory = client.memory
    path = memory / "provider-registries" / "dexpaprika-networks.json"
    saved = _load_json(path, {})
    networks = saved.get("networks") if isinstance(saved, dict) else None
    fetched = parse_time(saved.get("fetched_utc")) if isinstance(saved, dict) else None
    age = (datetime.now(timezone.utc) - fetched).total_seconds() if fetched else None
    if isinstance(networks, list) and networks and age is not None and age <= REGISTRY_TTL_SECONDS:
        return networks, {
            "registry_available": True, "registry_cached": True,
            "registry_network_count": len(networks),
            "registry_age_seconds": round(age, 1),
        }
    payload, headers, error = client.get("/networks", family="network_registry")
    networks = _network_rows(payload)
    if networks:
        _write_json(path, {
            "provider": "dexpaprika",
            "provider_version": VERSION,
            "fetched_utc": utc_now(),
            "networks": networks,
            "registry_scope": "DexPaprika supported-network registry; not a census of every chain or every on-chain market.",
        })
    return networks, {
        "registry_available": bool(networks),
        "registry_cached": False,
        "registry_network_count": len(networks),
        "registry_error": error,
        "api_plan_header": headers.get("x-api-plan"),
    }


def _token_address(token: dict[str, Any]) -> str | None:
    value = token.get("id") or token.get("address") or token.get("token_address")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _token_symbol(token: dict[str, Any]) -> str:
    return str(token.get("symbol") or token.get("name") or "").strip().upper()


def _is_quote_token(token: dict[str, Any], chain: str | None = None) -> bool:
    symbol = _token_symbol(token)
    if symbol in STABLE_SYMBOLS or symbol in WRAPPED_SYMBOLS:
        return True
    address = (_token_address(token) or "").lower()
    return address in CHAIN_QUOTE_ADDRESSES.get(str(chain or "").strip().lower(), set())


def _created_age_hours(value: Any) -> float | None:
    stamp = parse_time(value)
    if stamp is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0)


def score_candidate(row: dict[str, Any]) -> dict[str, Any]:
    """Transparent 0–100 relative early-breakout score, never a probability."""
    p5 = _num(row.get("change_5m_pct"))
    p1 = _num(row.get("change_1h_pct"))
    p6 = _num(row.get("change_6h_pct"))
    p24 = _num(row.get("change_24h_pct"))
    liquidity = _num(row.get("liquidity_usd") or row.get("best_liquidity_pool_usd"))
    volume = _num(row.get("volume_24h_usd"))
    txns = _num(row.get("txns_24h"))
    age = _num(row.get("age_hours"))
    buy_usd = _num(row.get("dexpaprika_1h_buy_usd"))
    sell_usd = _num(row.get("dexpaprika_1h_sell_usd"))
    components: dict[str, float] = {}
    components["5m_impulse"] = 10.0 * min(1.0, max(0.0, p5 or 0.0) / 15.0)
    components["1h_momentum"] = 10.0 * min(1.0, max(0.0, p1 or 0.0) / 35.0)
    components["6h_trend"] = 6.0 * min(1.0, max(0.0, p6 or 0.0) / 60.0)
    # Long-window gains matter, but get less weight so late pumps do not
    # automatically outrank a fresh acceleration signal.
    components["24h_context"] = 4.0 * min(1.0, max(0.0, p24 or 0.0) / 150.0)
    acceleration = 0.0
    if p1 is not None and p6 is not None:
        try:
            recent_rate = math.log1p(max(-95.0, p1) / 100.0)
            six_hour_rate = math.log1p(max(-95.0, p6) / 100.0) / 6.0
            acceleration = max(0.0, recent_rate - six_hour_rate) * 60.0
        except (ValueError, OverflowError):
            acceleration = 0.0
    components["momentum_acceleration"] = min(10.0, acceleration * 8.0)
    components["transaction_activity"] = (
        8.0 * min(1.0, math.log1p(max(0.0, txns or 0.0)) / math.log1p(3000.0))
    )
    turnover = (volume / liquidity) if volume is not None and liquidity is not None and liquidity > 0 else None
    components["volume_turnover"] = (
        8.0 * min(1.0, math.log1p(max(0.0, turnover or 0.0)) / math.log1p(12.0))
    )
    components["liquidity_depth"] = (
        10.0 * min(1.0, math.log1p(max(0.0, liquidity or 0.0)) / math.log1p(5_000_000.0))
    )
    buy_share = None
    if buy_usd is not None and sell_usd is not None and buy_usd + sell_usd > 0:
        buy_share = buy_usd / (buy_usd + sell_usd) * 100.0
    components["buy_pressure"] = (
        8.0 * min(1.0, max(0.0, ((buy_share or 50.0) - 40.0) / 35.0))
        if buy_share is not None else 0.0
    )
    # Compare the latest 5m turnover with one twelfth of the 1h turnover.
    # This captures a real-time volume burst rather than only a high 24h ratio.
    volume_5m = _num(row.get("dexpaprika_5m_volume_usd"))
    volume_1h = _num(row.get("dexpaprika_1h_volume_usd"))
    volume_acceleration_ratio = (
        (volume_5m * 12.0 / volume_1h)
        if volume_5m is not None and volume_1h is not None and volume_1h > 0
        else None
    )
    components["intraday_volume_acceleration"] = (
        8.0 * min(1.0, max(0.0, math.log1p(volume_acceleration_ratio) / math.log1p(5.0)))
        if volume_acceleration_ratio is not None else 0.0
    )
    if age is not None:
        components["pool_novelty"] = 6.0 if age <= 6 else 4.0 if age <= 24 else 2.0 if age <= 72 else 0.0
    else:
        components["pool_novelty"] = 0.0
    aligned = sum(value is not None and value > 0 for value in (p5, p1, p6))
    components["multi_window_alignment"] = 5.0 * aligned / 3.0
    raw = sum(components.values())
    score = round(max(0.0, min(100.0, raw / 93.0 * 100.0)), 2)
    risks = []
    if liquidity is None:
        risks.append("LIQUIDITY_UNVERIFIED")
    elif liquidity < 5_000:
        risks.append("EXTREME_THIN_LIQUIDITY")
    elif liquidity < 25_000:
        risks.append("THIN_LIQUIDITY_MICROCAP")
    if age is not None and age < 1:
        risks.append("POOL_LESS_THAN_ONE_HOUR_OLD")
    if turnover is not None and turnover > 20:
        risks.append("EXTREME_VOLUME_TO_LIQUIDITY_RATIO_CHECK_FOR_WASH_OR_ROTATION")
    if p24 is not None and p24 > 150:
        risks.append("LATE_CHASE_RISK_24H_MOVE_OVER_150_PERCENT")
    if p5 is not None and p5 > 25:
        risks.append("LATE_CHASE_RISK_5M_MOVE_OVER_25_PERCENT")
    if p1 is not None and p1 > 75:
        risks.append("LATE_CHASE_RISK_1H_MOVE_OVER_75_PERCENT")
    if p6 is not None and p6 > 150:
        risks.append("LATE_CHASE_RISK_6H_MOVE_OVER_150_PERCENT")
    if ((p5 is not None and p5 > 500) or
            (p1 is not None and p1 > 5000) or
            (p6 is not None and p6 > 10000) or
            (p24 is not None and p24 > 50000)):
        risks.append("EXTREME_RETURN_REQUIRES_OHLCV_CONFIRMATION")
    if p6 is not None and p6 > 80:
        risks.append("LATE_CHASE_RISK_6H_MOVE_OVER_80_PERCENT")
    short_rate = (p5 * 12.0) if p5 is not None else None
    if short_rate is not None and p1 is not None and short_rate > 100 and short_rate - p1 > 75:
        risks.append("ABNORMAL_SHORT_WINDOW_MOMENTUM_ACCELERATION")
    distance_to_high = _num(row.get("distance_to_24h_high_pct"))
    if distance_to_high is not None and distance_to_high <= 2.0 and (
            (p6 is not None and p6 >= 40) or (p24 is not None and p24 >= 100)):
        risks.append("AT_24H_LOCAL_HIGH_AFTER_STRONG_RUNUP")
    if p5 is not None and p5 < 0 and p1 is not None and p1 > 0:
        risks.append("VERY_SHORT_TERM_REVERSAL")
    if buy_share is not None and buy_share < 42:
        risks.append("SELL_SIDE_FLOW_DOMINANT")
    if _num(row.get("price_usd")) is None or (_num(row.get("price_usd")) or 0) <= 0:
        risks.append("CURRENT_PRICE_UNVERIFIED")
    if score >= 75 and (liquidity is None or liquidity < 25_000):
        tier = "HIGH_UPSIDE_SPECULATIVE_MICROCAP"
    elif score >= 75:
        tier = "EARLY_BREAKOUT_LEADER"
    elif score >= 60:
        tier = "ACCELERATING_WATCHLIST"
    elif score >= 40:
        tier = "DEVELOPING_MOMENTUM"
    else:
        tier = "EARLY_DISCOVERY_OR_WEAK_SIGNAL"

    # Evaluate 9 independent signal engines and 24h expected return model
    signal_engine_executed = False
    expected_return_engine_executed = False
    engine_failure_module = None
    engine_exception_category = None
    engine_failure_diagnostic = None
    engine_result_status = "SUCCESS"
    expected_return_status = "SUCCESS"
    engines_res = {}
    expected_res = {}

    try:
        try:
            from scripts.breakout_signal_engines import BreakoutSignalEngines
            from scripts.expected_return_engine import ExpectedReturnEngine
        except ImportError:
            from breakout_signal_engines import BreakoutSignalEngines
            from expected_return_engine import ExpectedReturnEngine
    except Exception as exc:
        engine_failure_module = "import"
        engine_exception_category = type(exc).__name__
        engine_failure_diagnostic = f"Engine module import failed: {type(exc).__name__}"
        engine_result_status = "SOFTWARE_FAILURE"
        expected_return_status = "SOFTWARE_FAILURE"

    if engine_failure_module is None:
        try:
            engines_res = BreakoutSignalEngines.evaluate_all_engines(row)
            signal_engine_executed = True
            if engines_res.get("breakout_evidence_eligible"):
                engine_result_status = "SUCCESS"
            else:
                engine_result_status = "DATA_UNAVAILABLE"
        except Exception as exc:
            signal_engine_executed = False
            engine_failure_module = "breakout_signal_engines"
            engine_exception_category = type(exc).__name__
            engine_failure_diagnostic = f"BreakoutSignalEngines failed: {type(exc).__name__}"
            engine_result_status = "SOFTWARE_FAILURE"
            engines_res = {
                "breakout_confluence_score": None,
                "engine_scores": {},
                "acceleration_signals": {},
                "engine_evidence_status": {},
                "evidence_coverage_pct": 0.0,
                "breakout_evidence_eligible": False,
                "evidence_status": "SOFTWARE_FAILURE",
            }

        try:
            expected_res = ExpectedReturnEngine.estimate_outcomes(row, engines_res)
            expected_return_engine_executed = True
            expected_return_status = expected_res.get("forecast_status", "SUCCESS")
        except Exception as exc:
            expected_return_engine_executed = False
            if engine_failure_module is None:
                engine_failure_module = "expected_return_engine"
                engine_exception_category = type(exc).__name__
                engine_failure_diagnostic = f"ExpectedReturnEngine failed: {type(exc).__name__}"
            expected_return_status = "SOFTWARE_FAILURE"
            expected_res = {
                "probability_tp_hit_24h_pct": None,
                "expected_net_24h_return_pct": None,
                "potential_mfe_24h_pct": None,
                "potential_mae_24h_pct": None,
                "estimated_hours_to_target": None,
                "reversal_risk_score": None,
                "realistic_opportunity_rank": None,
                "forecast_status": "SOFTWARE_FAILURE",
            }

    if engine_failure_module is not None:
        risks.append("SOFTWARE_ERROR_SIGNAL_ENGINES_INCOMPLETE")
        risks.append("UNVERIFIED_DUE_TO_ENGINE_FAILURE")

    return {
        "score": score,
        "signal_version": EARLY_BREAKOUT_SIGNAL_VERSION,
        "model_id": EARLY_BREAKOUT_MODEL_ID,
        "tier": tier,
        "components": {key: round(value, 2) for key, value in components.items()},
        "risk_flags": risks,
        "buy_pressure_pct": round(buy_share, 2) if buy_share is not None else None,
        "volume_to_liquidity": round(turnover, 3) if turnover is not None else None,
        "intraday_volume_acceleration_ratio": round(volume_acceleration_ratio, 3) if volume_acceleration_ratio is not None else None,
        "signal_engine_executed": signal_engine_executed,
        "expected_return_engine_executed": expected_return_engine_executed,
        "engine_failure_module": engine_failure_module,
        "engine_exception_category": engine_exception_category,
        "engine_failure_diagnostic": engine_failure_diagnostic,
        "engine_result_status": engine_result_status,
        "expected_return_status": expected_return_status,
        "breakout_confluence_score": engines_res.get("breakout_confluence_score"),
        "breakout_engine_scores": engines_res.get("engine_scores", {}),
        "breakout_acceleration_signals": engines_res.get("acceleration_signals", {}),
        "engine_evidence_status": engines_res.get("engine_evidence_status", {}),
        "evidence_coverage_pct": engines_res.get("evidence_coverage_pct", 0.0),
        "breakout_evidence_eligible": engines_res.get("breakout_evidence_eligible", False),
        "probability_tp_hit_24h_pct": expected_res.get("probability_tp_hit_24h_pct"),
        "expected_net_24h_return_pct": expected_res.get("expected_net_24h_return_pct"),
        "potential_mfe_24h_pct": expected_res.get("potential_mfe_24h_pct"),
        "potential_mae_24h_pct": expected_res.get("potential_mae_24h_pct"),
        "estimated_hours_to_target": expected_res.get("estimated_hours_to_target"),
        "reversal_risk_score": expected_res.get("reversal_risk_score"),
        "realistic_opportunity_rank": expected_res.get("realistic_opportunity_rank"),
        "method": "Relative heuristic using 5m/1h/6h momentum, acceleration, intraday volume acceleration, transaction activity, turnover, liquidity, new-pool age and buy pressure. 9 independent signal engines with 24h expected return projection.",
    }


def _canonical_chain_address(chain: str, address: Any) -> str:
    value = str(address or "").strip()
    evm = {"ethereum", "eth", "base", "bsc", "bnb", "polygon", "polygon_pos",
           "arbitrum", "optimism", "avalanche", "avax", "fantom", "ftm",
           "linea", "scroll", "zksync", "mantle", "blast", "mode", "opbnb"}
    if str(chain or "").lower() in evm and value.startswith("0x") and all(c in "0123456789abcdefABCDEF" for c in value[2:]):
        return value.lower()
    return value


def _normalize_pool(row: dict[str, Any], family: str, rank: int,
                    base_record_fn) -> dict[str, Any] | None:
    tokens = row.get("tokens") if isinstance(row.get("tokens"), list) else []
    token_rows = [item for item in tokens if isinstance(item, dict)]
    if not token_rows:
        token_rows = []
    network = str(row.get("chain") or "").strip()
    first = token_rows[0] if token_rows else {}
    second = token_rows[1] if len(token_rows) > 1 else {}
    # Track the non-quote token. Native SOL was previously missed because the
    # provider sometimes labels it SOL rather than WSOL, causing the report to
    # price and rank the quote asset instead of the actual traded token.
    if first and second and _is_quote_token(first, network) and not _is_quote_token(second, network):
        base, quote = second, first
        swapped = True
    else:
        base, quote = first, second
        swapped = False
    pool = str(row.get("id") or row.get("pool_address") or "").strip()
    if not network or not pool:
        return None
    base_address = _token_address(base) if base else None
    quote_address = _token_address(quote) if quote else None
    base_symbol = _token_symbol(base) if base else ""
    quote_symbol = _token_symbol(quote) if quote else ""
    pair = "%s/%s" % (base_symbol or "UNKNOWN", quote_symbol or "UNKNOWN")
    summary = base.get("summary") if isinstance(base.get("summary"), dict) else {}
    base_1h = base.get("1h") if isinstance(base.get("1h"), dict) else {}
    base_5m = base.get("5m") if isinstance(base.get("5m"), dict) else {}
    base_6h = base.get("6h") if isinstance(base.get("6h"), dict) else {}
    price = _num(base.get("summary", {}).get("price_usd") if isinstance(base.get("summary"), dict) else None)
    if price is None and not swapped:
        price = _num(row.get("price_usd"))
    fdv = _num(base.get("fdv"))
    if fdv is None:
        fdv = _num(summary.get("fdv"))
    change_5m = _num(row.get("price_change_percentage_5m"))
    change_1h = _num(row.get("price_change_percentage_1h"))
    change_6h = _num(row.get("price_change_percentage_6h"))
    change_24h = _num(row.get("price_change_percentage_24h"))
    created = row.get("created_at")
    age = _created_age_hours(created)
    liquidity = _num(row.get("liquidity_usd"))
    volume24 = _num(row.get("volume_usd_24h"))
    txns24 = _num(row.get("transactions_24h"))
    extra = {
        "pool_address": pool,
        "pair_address": pool,
        "quote_token_address": quote_address,
        "quote_symbol": quote_symbol,
        "dexpaprika_token_order_swapped_for_tracking": swapped,
        "dexpaprika_family": family,
        "dexpaprika_rank": rank,
        "dexpaprika_network": network,
        "dexpaprika_pool_id": pool,
        "dexpaprika_created_at": created,
        "dexpaprika_1h_buy_usd": _num(base_1h.get("buy_usd")),
        "dexpaprika_1h_sell_usd": _num(base_1h.get("sell_usd")),
        "dexpaprika_5m_buy_usd": _num(base_5m.get("buy_usd")),
        "dexpaprika_5m_sell_usd": _num(base_5m.get("sell_usd")),
        "dexpaprika_1h_volume_usd": _num(base_1h.get("volume_usd")),
        "dexpaprika_5m_volume_usd": _num(base_5m.get("volume_usd")),
        "dexpaprika_1h_txns": _num(base_1h.get("txns")),
        "dexpaprika_5m_txns": _num(base_5m.get("txns")),
        "dexpaprika_6h_buy_usd": _num(base_6h.get("buy_usd")),
        "dexpaprika_6h_sell_usd": _num(base_6h.get("sell_usd")),
        "dexpaprika_provider_timestamp": row.get("last_updated") or row.get("created_at"),
        "discovery_lane": "dexpaprika_%s" % family,
        "discovery_evidence_status": "POOL_SEARCH_ROW; FIRST_PAGE_SAMPLE; NO_COMPLETENESS_CLAIM",
        "dexpaprika_token_order_swapped_for_tracking": swapped,
    }
    record = base_record_fn(
        identity="dex:%s:%s" % (network.lower(), _canonical_chain_address(network, pool)),
        asset=(base.get("name") if base else None) or base_symbol or pair,
        symbol=base_symbol,
        source="dexpaprika_%s" % family,
        market_type="dex_pool",
        change_5m=change_5m,
        change_1h=change_1h,
        change_6h=change_6h,
        change_24h=change_24h,
        price_usd=price,
        volume_24h_usd=volume24,
        liquidity_usd=liquidity,
        market_cap_usd=None,
        fdv_usd=fdv,
        chain=network,
        address=base_address,
        venue=row.get("dex_name") or row.get("dex_id"),
        pair=pair,
        pair_url=None,
        age_hours=age,
        txns_24h=txns24,
        buys_24h=None,
        sells_24h=None,
        retrieved_utc=utc_now(),
        extra=extra,
    )
    score = score_candidate(record)
    record.update({
        "early_breakout_score": score["score"],
        "early_breakout_signal_version": score["signal_version"],
        "early_breakout_model_id": score["model_id"],
        "early_breakout_tier": score["tier"],
        "early_breakout_components": score["components"],
        "early_breakout_risk_flags": score["risk_flags"],
        "breakout_confluence_score": score.get("breakout_confluence_score"),
        "breakout_engine_scores": score.get("breakout_engine_scores"),
        "breakout_acceleration_signals": score.get("breakout_acceleration_signals"),
        "engine_evidence_status": score.get("engine_evidence_status"),
        "evidence_coverage_pct": score.get("evidence_coverage_pct"),
        "breakout_evidence_eligible": score.get("breakout_evidence_eligible"),
        "probability_tp_hit_24h_pct": score.get("probability_tp_hit_24h_pct"),
        "expected_net_24h_return_pct": score.get("expected_net_24h_return_pct"),
        "potential_mfe_24h_pct": score.get("potential_mfe_24h_pct"),
        "potential_mae_24h_pct": score.get("potential_mae_24h_pct"),
        "estimated_hours_to_target": score.get("estimated_hours_to_target"),
        "reversal_risk_score": score.get("reversal_risk_score"),
        "realistic_opportunity_rank": score.get("realistic_opportunity_rank"),
        "signal_engine_executed": score.get("signal_engine_executed"),
        "expected_return_engine_executed": score.get("expected_return_engine_executed"),
        "engine_failure_module": score.get("engine_failure_module"),
        "engine_exception_category": score.get("engine_exception_category"),
        "engine_failure_diagnostic": score.get("engine_failure_diagnostic"),
        "engine_result_status": score.get("engine_result_status"),
        "expected_return_status": score.get("expected_return_status"),
    })
    return record


def _collect_dexpaprika(hunter: Any, mod: Any) -> dict[str, Any]:
    memory = _memory_root(hunter)
    client = DexPaprikaClient(memory, timeout=getattr(getattr(hunter, "fx", None), "timeout", 8))
    networks, registry_meta = _load_registry(client)
    supported = {item["id"].lower() for item in networks}
    base_record_fn = getattr(mod, "_base_record")
    family_counts: dict[str, int] = {}
    errors = []
    requests = []
    total_added = 0
    pages_retrieved = 0
    records_discovered = 0
    for family, filters in POOL_QUERIES:
        family_added = 0
        family_page = 1
        has_next = True
        while has_next and family_page <= 2 and client.request_count < SCANNER_ROLLING_REQUEST_CEILING:
            params = {"limit": MAX_SEARCH_ROWS, "detailed": "true"}
            params.update(filters)
            if family_page > 1:
                params["page"] = family_page
            path = "/pools/search"
            payload, headers, error = client.get(path, params=params, family=family)
            pages_retrieved += 1
            status = "OK" if isinstance(payload, dict) and isinstance(payload.get("results"), list) else "FAILED"
            rows = payload.get("results") if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                rows = []
            records_discovered += len(rows)
            added = 0
            for rank, item in enumerate(rows, 1):
                if not isinstance(item, dict):
                    continue
                record = _normalize_pool(item, family, (family_page - 1) * MAX_SEARCH_ROWS + rank, base_record_fn)
                if record is None:
                    continue
                hunter.candidates.append(record)
                added += 1
            family_added += added
            total_added += added
            has_next = bool(payload.get("has_next_page")) if isinstance(payload, dict) else False
            requests.append({
                "family": family, "page": family_page, "status": status, "rows_received": len(rows),
                "records_added": added, "has_next_page": has_next,
                "api_plan_header": headers.get("x-api-plan"),
            })
            if error:
                errors.append({"family": family, "page": family_page, "error": error})
                break  # Stop paginating this family on error
            family_page += 1

        family_counts[family] = family_added
        _record_status(
            hunter, "dexpaprika_%s" % family, "OK" if family_added > 0 else "EMPTY_OR_FAILED", API_ROOT + "/pools/search",
            family_added, errors[-1]["error"] if errors and errors[-1]["family"] == family else None,
            extra={
                "records_added": family_added,
                "pages_retrieved": family_page - 1,
                "sort_order": filters.get("order_by"),
                "all_networks_query": True,
                "has_next_page": has_next,
                "provider_network_registry_count": len(networks),
                "api_plan_header": headers.get("x-api-plan") if "headers" in locals() else None,
            },
        )
    return {
        "provider_version": VERSION,
        "provider": "DexPaprika keyless public REST API",
        "api_key_required": False,
        "api_plan": "keyless_free_only",
        "coverage_status": "PARTIAL",
        "registry_available": registry_meta.get("registry_available", False),
        "registry_cached": registry_meta.get("registry_cached", False),
        "network_count": len(networks),
        "network_ids": [item["id"] for item in networks],
        "provider_scope": "Provider-documented 36-chain DEX dataset; cross-network global pool searches without a chains filter. Six independent rankings cover 5m, 1h, 6h, 24h momentum, 24h volume and pools created in the last 24h.",
        "coverage_boundary": "Endpoint returns ranked sampled pages up to provider quota limits; this is a broad ranked research sample, never an exhaustive census of all Web3 assets.",
        "family_counts": family_counts,
        "requests": requests,
        "pages_retrieved": pages_retrieved,
        "records_discovered": records_discovered,
        "records_rejected": max(0, records_discovered - total_added),
        "rate_limit_failures": len([e for e in errors if "429" in str(e.get("error", ""))]),
        "request_count_this_run": client.request_count,
        "rolling_request_ceiling": SCANNER_ROLLING_REQUEST_CEILING,
        "estimated_rolling_requests_reserved": len(_rolling_ledger(memory)["requests"]),
        "records_added": total_added,
        "work_left_for_future_rotations": any(r.get("has_next_page") for r in requests),
        "errors": errors,
        "registry_meta": registry_meta,
    }


def patch_v419_compactor(module: Any) -> None:
    """Preserve v4.25 source signals even when v4.19 is loaded under an alias."""
    module.UNIVERSE_COMPACT_FIELDS = tuple(dict.fromkeys(
        tuple(getattr(module, "UNIVERSE_COMPACT_FIELDS", ())) + EARLY_FIELDS
    ))
    if getattr(module, "_V425_COMPACTOR_PATCHED", False):
        return
    original_compact = module.compact_universe_row

    def compact_with_breakout_fields(row, _original=original_compact):
        compact = _original(row)
        for field in EARLY_FIELDS:
            value = row.get(field)
            if value is not None:
                compact[field] = copy.deepcopy(value)
        return compact

    module.compact_universe_row = compact_with_breakout_fields
    module._V425_COMPACTOR_PATCHED = True


def install_early_breakout_hooks(mod: Any) -> None:
    if getattr(mod, "_V425_EARLY_BREAKOUT_HOOKS_INSTALLED", False):
        return
    hunter_cls = getattr(mod, "RocketHunter", None)
    if hunter_cls is None:
        return
    # v4.19's collector compacts rows before they are persisted. Direct
    # execution can load that module under either "v419_upgrade" or
    # "scripts.v419_upgrade". Patch every loaded alias, including the actual
    # module object captured by v4.19's build_output closure.
    v419_modules = []
    for module_name in ("v419_upgrade", "scripts.v419_upgrade"):
        module = sys.modules.get(module_name)
        if module is not None and module not in v419_modules:
            v419_modules.append(module)
    if not v419_modules:
        try:
            from scripts import v419_upgrade as _v419
        except ImportError:
            try:
                import v419_upgrade as _v419
            except ImportError:
                _v419 = None
        if _v419 is not None:
            v419_modules.append(_v419)

    for _v419 in v419_modules:
        patch_v419_compactor(_v419)

    original_add = getattr(hunter_cls, "add_optional_birdeye", None)
    if original_add is None:
        raise RuntimeError("v4.24 Birdeye hook must be installed before v4.25")

    def patched_add_optional_birdeye(self, api_key, limit=100):
        # Preserve the optional Birdeye free-tier request, then collect the
        # independent keyless DexPaprika cross-chain pool rankings.
        original_add(self, api_key, limit=limit)
        if getattr(self, "dexpaprika_coverage_v425", None) is None:
            self.dexpaprika_coverage_v425 = _collect_dexpaprika(self, mod)

    hunter_cls.add_optional_birdeye = patched_add_optional_birdeye
    original_build = hunter_cls.build_output

    def patched_build_output(self):
        if getattr(self, "dexpaprika_coverage_v425", None) is None:
            self.dexpaprika_coverage_v425 = _collect_dexpaprika(self, mod)
        data = original_build(self)
        details = getattr(self, "dexpaprika_coverage_v425", None)
        if isinstance(details, dict):
            coverage = data.setdefault("coverage", {})
            coverage["dexpaprika_discovery_v425"] = {
                key: value for key, value in details.items() if key != "network_ids"
            }
            registries = coverage.setdefault("provider_registries_v425", {})
            registries["dexpaprika"] = {
                "network_ids": details.get("network_ids") or [],
                "network_count": details.get("network_count", 0),
                "registry_available": details.get("registry_available", False),
                "registry_cached": details.get("registry_cached", False),
                "scope": details.get("provider_scope"),
                "coverage_boundary": details.get("coverage_boundary"),
            }
            coverage["provider_roles_v425"] = coverage.get("provider_roles_v425") or {}
            coverage["provider_roles_v425"]["dexpaprika"] = (
                "keyless free all-network pool searches: 5m/1h/6h/24h momentum, "
                "24h volume and new pools; 100 first-page rows per family"
            )
            data["dexpaprika_discovery_v425"] = coverage["dexpaprika_discovery_v425"]
        return data

    hunter_cls.build_output = patched_build_output
    mod._V425_EARLY_BREAKOUT_HOOKS_INSTALLED = True


def _bar_time(bar: dict[str, Any], field: str) -> datetime | None:
    return parse_time(bar.get(field))


def _get_ohlcv(client: DexPaprikaClient, network: str, pool: str, position_inversed: bool = False) -> tuple[list[dict[str, Any]], str | None, dict[str, str]]:
    path = "/networks/%s/pools/%s/ohlcv" % (
        urllib.parse.quote(network, safe=""),
        urllib.parse.quote(pool, safe=""),
    )
    payload, headers, error = client.get(
        path,
        params={"start": "-24h", "interval": OHLCV_INTERVAL, "limit": OHLCV_LIMIT,
                "inversed": str(bool(position_inversed)).lower()},
        family="position_ohlcv_1h",
    )
    bars = payload if isinstance(payload, list) else []
    return [bar for bar in bars if isinstance(bar, dict)], error, headers


def _position_key(row: dict[str, Any]) -> str:
    return str(row.get("identity") or (
        "dex:%s:%s" % (str(row.get("chain") or "").lower(), _canonical_chain_address(row.get("chain"), row.get("pool_address")))
    ))


def _hourly_bars_to_position_bars(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Convert venue OHLCV rows to complete UTC hourly bars; ignore the forming hour."""
    output = []
    for row in rows or []:
        stamp = _num(row.get("t"))
        high, low, close = (_num(row.get(key)) for key in ("h", "l", "c"))
        if stamp is None or high is None or low is None or close is None:
            continue
        if stamp > 10_000_000_000:
            stamp /= 1000.0
        opened = datetime.fromtimestamp(stamp, timezone.utc)
        closed = opened + timedelta(hours=1)
        if closed > now:
            continue
        output.append({
            "time_open": opened.isoformat().replace("+00:00", "Z"),
            "time_close": closed.isoformat().replace("+00:00", "Z"),
            "high": high, "low": low, "close": close,
        })
    output.sort(key=lambda item: item["time_open"])
    return output


def _retry_after_metadata(value: Any, now: datetime | None = None) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        seconds = float(raw)
        if not math.isfinite(seconds) or seconds < 0:
            return None
        delay = min(seconds, GECKO_MAX_RETRY_AFTER_SECONDS)
        clamped = delay != seconds
        retry_at = current + timedelta(seconds=delay)
    except ValueError:
        try:
            retry_at = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        retry_at = retry_at.astimezone(timezone.utc)
        delay = max(0.0, (retry_at - current).total_seconds())
        if delay > GECKO_MAX_RETRY_AFTER_SECONDS:
            delay = GECKO_MAX_RETRY_AFTER_SECONDS
            retry_at = current + timedelta(seconds=delay)
            clamped = True
        else:
            clamped = False
    return {
        "retry_after_seconds": round(delay, 3),
        "cooldown_until_utc": retry_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "retry_after_clamped": clamped,
    }


def _response_header_value(headers: Any, name: str) -> Any:
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except (AttributeError, TypeError):
        value = None
    if value is not None:
        return value
    try:
        for key, candidate in headers.items():
            if str(key).lower() == name.lower():
                return candidate
    except (AttributeError, TypeError):
        return None
    return None


def _safe_retry_after_header(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 128 or any(not char.isprintable() for char in value):
        return None
    return value


def _fetch_gecko_position_bars_with_metadata(
    position: dict[str, Any], timeout: int = 8
) -> tuple[list[dict[str, Any]], str | None, str | None, dict[str, Any] | None]:
    """Fetch USD hourly OHLCV for the exact tracked DEX token, regardless of quote asset."""
    chain = GECKO_POSITION_ALIASES.get(
        str(position.get("chain") or "").lower(),
        str(position.get("chain") or "").lower(),
    )
    pool = str(position.get("pool_address") or "").strip()
    token = str(position.get("contract_address") or "").strip()
    if not chain or not pool or not token:
        return [], None, "DEX chain, pool or token mint/address missing", None
    params = urllib.parse.urlencode({
        "aggregate": 1, "limit": 40, "currency": "usd", "token": token,
    })
    url = "%s/networks/%s/pools/%s/ohlcv/hour?%s" % (
        GECKO_POSITION_ROOT, urllib.parse.quote(chain, safe=""),
        urllib.parse.quote(pool, safe=""), params,
    )
    headers = {"Accept": "application/json;version=20230203", "User-Agent": USER_AGENT}
    demo_key = os.environ.get("COINGECKO_DEMO_API_KEY", "").strip()
    if demo_key:
        headers["x-cg-demo-api-key"] = demo_key
    pro_key = os.environ.get("COINGECKO_API_KEY", "").strip()
    if pro_key:
        headers["x-cg-pro-api-key"] = pro_key
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        raw = (((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
        now = datetime.now(timezone.utc)
        bars = []
        for item in raw:
            if not isinstance(item, list) or len(item) < 6:
                continue
            try:
                opened = datetime.fromtimestamp(int(item[0]), timezone.utc)
                high, low, close = float(item[2]), float(item[3]), float(item[4])
            except (TypeError, ValueError, OverflowError):
                continue
            closed = opened + timedelta(hours=1)
            if closed > now:
                continue
            bars.append({
                "time_open": opened.isoformat().replace("+00:00", "Z"),
                "time_close": closed.isoformat().replace("+00:00", "Z"),
                "high": high, "low": low, "close": close,
            })
        bars.sort(key=lambda item: item["time_open"])
        return bars, url, None, None
    except urllib.error.HTTPError as exc:
        metadata = {
            "provider": "geckoterminal",
            "http_status": exc.code,
            "retry_after_available": False,
            "cooldown_status": "retry_after_unavailable",
        }
        retry_after_header = _safe_retry_after_header(
            _response_header_value(exc.headers, "Retry-After")
        )
        if retry_after_header is not None:
            metadata["retry_after_header"] = retry_after_header
        retry_after = _retry_after_metadata(
            retry_after_header
        )
        if retry_after is not None:
            metadata.update(retry_after)
            metadata["retry_after_available"] = True
            metadata["cooldown_status"] = "bounded_retry_after"
        return [], url, "%s: %s" % (type(exc).__name__, exc), metadata
    except Exception as exc:
        return [], url, "%s: %s" % (type(exc).__name__, exc), None


def _fetch_gecko_position_bars(
    position: dict[str, Any], timeout: int = 8
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    bars, url, error, _ = _fetch_gecko_position_bars_with_metadata(position, timeout)
    return bars, url, error


def _fetch_cex_position_bars(position: dict[str, Any], adapters, fx, now: datetime) -> tuple[list[dict[str, Any]], str | None, str | None]:
    venue = str(position.get("venue") or "").lower()
    symbol = str(position.get("venue_pair") or position.get("pair") or "").strip()
    adapter = adapters.VENUES.get(venue)
    if adapter is None or not symbol:
        return [], None, "CEX venue adapter or venue pair symbol missing"
    try:
        rows, url = adapter.candles(fx, symbol, "1h", limit=40)
        if rows is None:
            return [], url, "CEX hourly OHLCV unavailable"
        return _hourly_bars_to_position_bars(rows, now), url, None
    except Exception as exc:
        return [], None, "%s: %s" % (type(exc).__name__, exc)


def _report_discovery_index(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index full and compact report boards, preferring rows with breakout signals."""
    index: dict[str, dict[str, Any]] = {}
    boards = (
        "universe_rows_v419", "discovery_rows_v421", "dex_discovery_leads",
        "execution_candidate_shortlist", "most_actionable_liquidity_screen",
        "top_5m_movers", "top_1h_movers", "top_6h_movers",
        "top_24h_gainers", "top_24h_movers", "top_24h_volume",
        "extreme_movers_100pct_plus", "extreme_movers_400pct_plus",
        "extreme_movers_1000pct_plus", "new_launch_movers",
        "new_pools_24h", "new_pools_under_24h",
        "high_risk_moonshot_scouts", "new_launch_discovery_board_v418",
    )
    for board in boards:
        rows = report.get(board)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("identity") or "").strip()
            if not key:
                chain = str(row.get("chain") or "").strip().lower()
                pool = str(row.get("pool_address") or row.get("pair_address") or "").strip().lower()
                if chain and pool:
                    key = "dex:%s:%s" % (chain, pool)
            if not key:
                continue
            existing = index.setdefault(key, {})
            incoming_score = _num(row.get("early_breakout_score"))
            existing_score = _num(existing.get("early_breakout_score"))
            # Always fill gaps. For the score/component bundle, prefer the row
            # with the strongest observed early-breakout signal across families.
            prefer_signal_bundle = incoming_score is not None and (
                existing_score is None or incoming_score > existing_score
            )
            for field, value in row.items():
                if value is None:
                    continue
                if field in EARLY_FIELDS and not prefer_signal_bundle and existing.get(field) is not None:
                    continue
                if existing.get(field) is None or field in EARLY_FIELDS and prefer_signal_bundle:
                    existing[field] = copy.deepcopy(value)
    return index


def _fallback_market_mover_candidates(report: dict[str, Any], limit: int = 500) -> list[dict[str, Any]]:
    """Keep a ranked discovery-only board when detailed OHLCV eligibility fails."""
    boards = (
        "universe_rows_v419", "discovery_rows_v421",
        "top_1h_movers", "top_6h_movers", "top_24h_gainers",
        "top_24h_movers", "top_24h_volume",
        "extreme_movers_100pct_plus", "extreme_movers_400pct_plus",
        "extreme_movers_1000pct_plus", "new_launch_movers",
        "new_pools_24h", "new_pools_under_24h",
        "high_risk_moonshot_scouts", "new_launch_discovery_board_v418",
    )
    by_market: dict[str, dict[str, Any]] = {}
    for board in boards:
        rows = report.get(board)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("market_type") not in ("dex_pool", "cex_spot"):
                continue
            kind = row.get("market_type")
            chain = str(row.get("chain") or "").strip().lower()
            pool = str(row.get("pool_address") or row.get("pair_address") or "").strip()
            source_identity = str(row.get("identity") or "").strip()
            key = (
                "dex:%s:%s" % (chain, pool.lower()) if kind == "dex_pool" and pool else
                source_identity.lower() or
                "%s:%s:%s" % (kind, row.get("venue") or chain, row.get("contract_address") or row.get("symbol"))
            )
            existing = by_market.get(key)
            incoming = copy.deepcopy(row)
            if kind == "dex_pool" and pool:
                incoming["identity"] = key
                incoming["pool_address"] = pool
                incoming["pair_address"] = incoming.get("pair_address") or pool
            incoming_score = _num(incoming.get("early_breakout_score"))
            if incoming_score is None:
                signal = score_candidate(incoming)
                incoming["early_breakout_score"] = signal["score"]
                incoming["early_breakout_signal_version"] = signal["signal_version"]
                incoming["early_breakout_model_id"] = signal["model_id"]
                incoming["early_breakout_tier"] = signal["tier"]
                incoming["early_breakout_components"] = signal["components"]
                incoming["early_breakout_risk_flags"] = signal["risk_flags"]
                incoming["breakout_confluence_score"] = signal.get("breakout_confluence_score")
                incoming["breakout_engine_scores"] = signal.get("breakout_engine_scores")
                incoming["breakout_acceleration_signals"] = signal.get("breakout_acceleration_signals")
                incoming["engine_evidence_status"] = signal.get("engine_evidence_status")
                incoming["evidence_coverage_pct"] = signal.get("evidence_coverage_pct")
                incoming["breakout_evidence_eligible"] = signal.get("breakout_evidence_eligible")
                incoming["probability_tp_hit_24h_pct"] = signal.get("probability_tp_hit_24h_pct")
                incoming["expected_net_24h_return_pct"] = signal.get("expected_net_24h_return_pct")
                incoming["potential_mfe_24h_pct"] = signal.get("potential_mfe_24h_pct")
                incoming["potential_mae_24h_pct"] = signal.get("potential_mae_24h_pct")
                incoming["estimated_hours_to_target"] = signal.get("estimated_hours_to_target")
                incoming["reversal_risk_score"] = signal.get("reversal_risk_score")
                incoming["realistic_opportunity_rank"] = signal.get("realistic_opportunity_rank")
                incoming["signal_engine_executed"] = signal.get("signal_engine_executed")
                incoming["expected_return_engine_executed"] = signal.get("expected_return_engine_executed")
                incoming["engine_failure_module"] = signal.get("engine_failure_module")
                incoming["engine_exception_category"] = signal.get("engine_exception_category")
                incoming["engine_failure_diagnostic"] = signal.get("engine_failure_diagnostic")
                incoming["engine_result_status"] = signal.get("engine_result_status")
                incoming["expected_return_status"] = signal.get("expected_return_status")
            if existing is None:
                by_market[key] = incoming
                continue
            prior_score = _num(existing.get("early_breakout_score")) or 0.0
            this_score = _num(incoming.get("early_breakout_score")) or 0.0
            source_is_normalized = str(incoming.get("source") or "").startswith("dexpaprika_")
            old_is_normalized = str(existing.get("source") or "").startswith("dexpaprika_")
            if this_score > prior_score or (source_is_normalized and not old_is_normalized):
                for field, value in incoming.items():
                    if value is not None:
                        existing[field] = value
            else:
                for field, value in incoming.items():
                    if existing.get(field) is None and value is not None:
                        existing[field] = value
    rows = list(by_market.values())
    def bounded(value, cap):
        value = _num(value)
        return max(-cap, min(cap, value)) if value is not None else -cap
    rows.sort(key=lambda row: (
        _num(row.get("early_breakout_score")) or 0.0,
        bounded(row.get("change_5m_pct"), 500),
        bounded(row.get("change_1h_pct"), 5000),
        bounded(row.get("change_6h_pct"), 10000),
        bounded(row.get("change_24h_pct"), 50000),
        _num(row.get("volume_24h_usd")) or 0.0,
        _num(row.get("liquidity_usd")) or 0.0,
    ), reverse=True)
    output = []
    for row in rows[:max(0, limit)]:
        output.append({
            "identity": row.get("identity"),
            "asset": row.get("asset") or row.get("symbol") or "Unknown",
            "symbol": row.get("symbol"),
            "pair": row.get("pair") or row.get("symbol") or row.get("asset") or "Unknown market",
            "market_type": row.get("market_type"),
            "chain": row.get("chain"),
            "venue": row.get("venue"),
            "contract_address": row.get("contract_address"),
            "pool_address": row.get("pool_address") or row.get("pair_address"),
            "pair_address": row.get("pair_address") or row.get("pool_address"),
            "pair_url": row.get("pair_url"),
            "source_url": row.get("source_url"),
            "source": row.get("source"),
            "price": _num(row.get("price_usd")),
            "price_usd": _num(row.get("price_usd")),
            "data_timestamp_utc": row.get("retrieved_utc") or row.get("dexpaprika_provider_timestamp"),
            "change_5m_pct": _num(row.get("change_5m_pct")),
            "change_1h_pct": _num(row.get("change_1h_pct")),
            "change_6h_pct": _num(row.get("change_6h_pct")),
            "change_24h_pct": _num(row.get("change_24h_pct")),
            "returns_pct": {
                "1h": _num(row.get("change_1h_pct")),
                "4h": None,
                "24h": _num(row.get("change_24h_pct")),
                "6h": _num(row.get("change_6h_pct")),
            },
            "liquidity_usd": _num(row.get("liquidity_usd")),
            "volume_24h_usd": _num(row.get("volume_24h_usd")),
            "market_cap_usd": _num(row.get("market_cap_usd")),
            "fdv_usd": _num(row.get("fdv_usd")),
            "age_hours": _num(row.get("age_hours")),
            "txns_24h": _num(row.get("txns_24h")),
            "early_breakout_score": _num(row.get("early_breakout_score")),
            "early_breakout_tier": row.get("early_breakout_tier"),
            "early_breakout_components": row.get("early_breakout_components") or {},
            "early_breakout_risk_flags": row.get("early_breakout_risk_flags") or [],
            "risk_flags": row.get("risk_flags") or [],
            "source_forecast_score_pre_age_guard": _num(row.get("rank_score")) or 0.0,
            "return_24h_comparable": row.get("return_24h_comparable") is True,
            "historical_24h_comparable": row.get("return_24h_comparable") is True,
            "trade_plan": {},
            "target_price": None,
            "target_price_source": "UNVERIFIED",
            "quality_gate": {
                "status": "DISCOVERY_ONLY_OHLCV_NOT_AUDITED",
                "execution_ready": False,
                "reasons": ["Detailed OHLCV, price refresh, route and safety checks were not completed for this fallback lead."],
            },
            "entry_decision": "DISCOVERY ONLY — ranked from the broad mover boards; not execution-ready",
        })
    return output


def apply_forward_opportunity_ranking(forward: dict[str, Any],
                                      report: dict[str, Any]) -> dict[str, Any]:
    """Restore the next-24h opportunity ranking without weakening historical-age checks.

    The v4.18 guard checks whether a *past* 24h return is comparable. That is
    distinct from a new 24h forward holding window. Young pools remain visible
    and rankable for the forward window, but retain historical_24h_comparable=False.
    """
    candidates = forward.get("candidates")
    if not isinstance(candidates, list):
        candidates = []
    fallback_discovery_used = False
    if not candidates:
        candidates = _fallback_market_mover_candidates(report, limit=500)
        fallback_discovery_used = bool(candidates)
    index = _report_discovery_index(report)
    enriched = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        item = copy.deepcopy(candidate)
        for field in LEGACY_STOP_FIELDS + LEGACY_EXTRA_TARGET_FIELDS:
            item.pop(field, None)
        trade_plan = item.get("trade_plan")
        if isinstance(trade_plan, dict):
            for field in LEGACY_STOP_FIELDS + LEGACY_EXTRA_TARGET_FIELDS:
                trade_plan.pop(field, None)
        key = str(item.get("identity") or "").strip()
        meta = index.get(key, {})
        for field in EARLY_FIELDS:
            value = meta.get(field)
            if value is not None:
                item[field] = copy.deepcopy(value)
        # A v4.18 age-guarded score is deliberately kept in source_* for audit.
        base_score = _num(item.get("source_forecast_score_pre_age_guard"))
        if base_score is None:
            base_score = _num(item.get("forecast_score"))
        if base_score is None:
            base_score = 0.0
        early_score = _num(item.get("early_breakout_score"))
        risk_flags = item.get("early_breakout_risk_flags")
        risk_flags = set(risk_flags if isinstance(risk_flags, list) else [])
        p5 = _num(item.get("change_5m_pct"))
        p1 = _num(item.get("change_1h_pct"))
        p6 = _num(item.get("change_6h_pct"))
        p24 = _num(item.get("change_24h_pct"))
        distance_to_high = _num(item.get("distance_to_24h_high_pct"))
        if p5 is not None and p5 > 25:
            risk_flags.add("LATE_CHASE_RISK_5M_MOVE_OVER_25_PERCENT")
        if p1 is not None and p1 > 75:
            risk_flags.add("LATE_CHASE_RISK_1H_MOVE_OVER_75_PERCENT")
        if p6 is not None and p6 > 150:
            risk_flags.add("LATE_CHASE_RISK_6H_MOVE_OVER_150_PERCENT")
        if p24 is not None and p24 > 150:
            risk_flags.add("LATE_CHASE_RISK_24H_MOVE_OVER_150_PERCENT")
        if p5 is not None and p1 is not None and p5 * 12.0 > 100 and p5 * 12.0 - p1 > 75:
            risk_flags.add("ABNORMAL_SHORT_WINDOW_MOMENTUM_ACCELERATION")
        if distance_to_high is not None and distance_to_high <= 2.0 and (
                (p6 is not None and p6 >= 40) or (p24 is not None and p24 >= 100)):
            risk_flags.add("AT_24H_LOCAL_HIGH_AFTER_STRONG_RUNUP")
        item["late_chase_risk_flags"] = sorted(
            flag for flag in risk_flags if flag.startswith(("LATE_CHASE_", "ABNORMAL_SHORT_WINDOW_", "AT_24H_"))
        )
        # Prefer acceleration and fresh breakout structure over the older,
        # more conservative base forecast. Keep late-chase/flow-reversal
        # penalties explicit instead of filtering risky high-upside microcaps.
        late_penalty = 0.0
        if "LATE_CHASE_RISK_5M_MOVE_OVER_25_PERCENT" in risk_flags:
            late_penalty += 6.0
        if "LATE_CHASE_RISK_1H_MOVE_OVER_75_PERCENT" in risk_flags:
            late_penalty += 7.0
        if "LATE_CHASE_RISK_24H_MOVE_OVER_150_PERCENT" in risk_flags:
            late_penalty += 10.0
        if "LATE_CHASE_RISK_6H_MOVE_OVER_150_PERCENT" in risk_flags:
            late_penalty += 8.0
        if "LATE_CHASE_RISK_6H_MOVE_OVER_80_PERCENT" in risk_flags:
            late_penalty += 7.0
        if "ABNORMAL_SHORT_WINDOW_MOMENTUM_ACCELERATION" in risk_flags:
            late_penalty += 8.0
        if "AT_24H_LOCAL_HIGH_AFTER_STRONG_RUNUP" in risk_flags:
            late_penalty += 6.0
        if "VERY_SHORT_TERM_REVERSAL" in risk_flags:
            late_penalty += 6.0
        if "SELL_SIDE_FLOW_DOMINANT" in risk_flags:
            late_penalty += 6.0
        if "EXTREME_VOLUME_TO_LIQUIDITY_RATIO_CHECK_FOR_WASH_OR_ROTATION" in risk_flags:
            late_penalty += 4.0
        if "EXTREME_RETURN_REQUIRES_OHLCV_CONFIRMATION" in risk_flags:
            late_penalty += 12.0
        if "CURRENT_PRICE_UNVERIFIED" in risk_flags:
            late_penalty += 8.0
        projected_upside = _num(item.get("projected_24h_upside_pct"))
        projection_score = (
            100.0 * min(1.0, math.log1p(max(0.0, projected_upside)) / math.log1p(250.0))
            if projected_upside is not None and projected_upside > 0 else None
        )
        if early_score is not None and projection_score is not None:
            early_contribution = 0.55 * early_score
            research_contribution = 0.20 * base_score
            projection_contribution = 0.25 * projection_score
            opportunity_score = max(0.0, min(100.0,
                early_contribution + research_contribution + projection_contribution - late_penalty))
            ranking_model = "55% early-breakout + 20% pre-age-guard research + 25% volatility/momentum upside projection - risk penalties"
        elif early_score is not None:
            early_contribution = 0.70 * early_score
            research_contribution = 0.30 * base_score
            projection_contribution = 0.0
            opportunity_score = max(0.0, min(100.0,
                early_contribution + research_contribution - late_penalty))
            ranking_model = "70% early-breakout + 30% pre-age-guard research - risk penalties; projection unavailable"
        elif projection_score is not None:
            early_contribution = 0.0
            research_contribution = 0.70 * base_score
            projection_contribution = 0.30 * projection_score
            opportunity_score = max(0.0, min(100.0,
                research_contribution + projection_contribution - late_penalty))
            ranking_model = "70% pre-age-guard research + 30% volatility/momentum upside projection - risk penalties"
        else:
            early_contribution = 0.0
            research_contribution = base_score
            projection_contribution = 0.0
            opportunity_score = max(0.0, min(100.0, base_score - late_penalty))
            ranking_model = "pre-age-guard research score; breakout/projection signal unavailable; risk penalties applied"
        historical_comparable = item.get("return_24h_comparable") is True
        item["historical_24h_comparable"] = historical_comparable
        item["forward_window_hours"] = 24
        item["forward_opportunity_score"] = round(opportunity_score, 2)
        item["forward_opportunity_ranking_model"] = ranking_model
        item["late_chase_penalty"] = round(late_penalty, 2)
        item["forward_opportunity_score_breakdown"] = {
            "early_breakout_contribution": round(early_contribution, 2),
            "research_context_contribution": round(research_contribution, 2),
            "upside_projection_pct": projected_upside,
            "upside_projection_score": round(projection_score, 2) if projection_score is not None else None,
            "upside_projection_contribution": round(projection_contribution, 2),
            "late_chase_penalty": round(late_penalty, 2),
            "score_is_probability": False,
        }
        item["upside_projection_score"] = round(projection_score, 2) if projection_score is not None else None
        item["upside_projection_confidence"] = (
            "CANDLE_BACKED_HEURISTIC_NOT_CALIBRATED" if projection_score is not None else "MOMENTUM_ONLY_NO_CANDLE_PROJECTION"
        )
        item["opportunity_tier"] = (
            "TOP_BREAKOUT_POTENTIAL" if opportunity_score >= 80 else
            "HIGH_UPSIDE_EARLY" if opportunity_score >= 65 else
            "DEVELOPING_MOMENTUM" if opportunity_score >= 50 else
            "EARLY_DISCOVERY"
        )
        item["forward_opportunity_status"] = (
            "RANKED_NEXT_24H_HISTORICAL_24H_COMPARABLE"
            if historical_comparable else
            "RANKED_NEXT_24H_HISTORICAL_24H_NOT_COMPARABLE"
        )
        item["forward_opportunity_decision"] = (
            "RANKED FOR NEXT 24H — historical return comparable"
            if historical_comparable else
            "RANKED FOR NEXT 24H — historical 24h return not comparable; execution still unverified"
        )
        cand_pot = _num(item.get("tp_upside_pct"))
        if cand_pot is None and isinstance(item.get("trade_plan"), dict):
            cand_pot = _num(item["trade_plan"].get("tp_upside_pct"))
        if cand_pot is None and projected_upside is not None:
            cand_pot = projected_upside
        if cand_pot is not None and cand_pot < 40.0:
            item["forward_opportunity_disqualified"] = True
            item["forward_opportunity_disqualification_reason"] = "POTENTIAL_UNDER_40_PERCENT"
            continue
        enriched.append(item)

    enriched.sort(key=lambda row: (
        _num(row.get("forward_opportunity_score")) or 0.0,
        _num(row.get("early_breakout_score")) or 0.0,
        _num(row.get("evidence_coverage_pct")) or 0.0,
    ), reverse=True)
    for rank, item in enumerate(enriched, 1):
        item["forward_opportunity_rank"] = rank

    # Preserve the age-qualified historical cohort separately. Do not let the
    # historical-age gate erase the user-requested next-24h research leader.
    if "age_qualified_forecast_leader" not in forward:
        forward["age_qualified_forecast_leader"] = copy.deepcopy(forward.get("forecast_leader"))
    forward["candidates"] = enriched
    forward["forward_opportunity_candidates"] = enriched
    forward["top_opportunities"] = []
    forward["opportunity_board_version"] = "4.27.0"
    forward["opportunity_board_display_limit"] = 0
    forward["research_watchlist_display_limit"] = 25
    leader = enriched[0] if enriched else None
    forward["forward_opportunity_leader"] = None
    forward["forward_research_leader"] = copy.deepcopy(leader)
    forward["top_rocket_pick"] = None
    forward["forecast_leader"] = None
    forward["upside_leader"] = None
    forward["forward_watchlist_leader"] = None
    forward["top_speculative_upside_leader"] = None
    if leader:
        forward["summary"] = "TOP NEXT-24H RESEARCH OPPORTUNITY — %s (%s); execution and historical comparability remain separately labelled." % (
            leader.get("asset") or "Unknown", leader.get("pair") or "Unknown"
        )
        forward["status"] = "NEXT_24H_OPPORTUNITY_RANKED_EXECUTION_UNVERIFIED"
    else:
        forward["summary"] = "Market-mover discovery feed returned no usable rows; this is a data-coverage failure, not a conclusion that no gainers exist."
        forward["status"] = "MARKET_DISCOVERY_FEED_UNAVAILABLE"
    forward["forward_opportunity_ranking"] = {
        "method": "early-breakout and pre-age-guard research blend; when sufficient 1h OHLCV exists, add a volatility/momentum upside projection; explicit risk penalties; no calibrated probability",
        "objective": "rank early next-24h upside potential across sampled Web3/DeFi DEX pools and CEX spot markets; execution verification is a separate gate",
        "candidate_count": len(enriched),
        "published_top_n": 0,
        "research_watchlist_top_n": min(25, len(enriched)),
        "ranking_version": "4.27.0",
        "historical_24h_comparable_count": sum(1 for row in enriched if row.get("historical_24h_comparable")),
        "historical_24h_not_comparable_count": sum(1 for row in enriched if not row.get("historical_24h_comparable")),
        "historical_age_gate_applies_to": "comparing the source's trailing/past 24h return only",
        "forward_horizon": "a new 24h window from the reference entry; pool age under 24h does not exclude a candidate",
        "calibrated_probability": False,
        "research_only": True,
        "no_candidate_removed_for_historical_age": True,
        "fallback_market_mover_discovery_used": fallback_discovery_used,
        "candidate_source_scope": "full-universe, short-window, extreme-mover, and new-launch boards",
    }
    return forward


def _expected_hour_opens(entry_ts: datetime | None, end_ts: datetime | None) -> list[str]:
    if entry_ts is None or end_ts is None:
        return []
    cursor = entry_ts.replace(minute=0, second=0, microsecond=0)
    if cursor < entry_ts:
        cursor += timedelta(hours=1)
    expected = []
    while cursor + timedelta(hours=1) <= end_ts:
        expected.append(cursor.isoformat().replace("+00:00", "Z"))
        cursor += timedelta(hours=1)
    return expected


def _update_position(position: dict[str, Any], bars: list[dict[str, Any]],
                     now: datetime) -> dict[str, Any]:
    """Track one TP touch and price excursions for a 24-hour research window."""
    entry_ts = parse_time(position.get("entry_timestamp_utc"))
    end_ts = parse_time(position.get("time_stop_utc"))
    entry = _num(position.get("entry_price"))
    target = _num(position.get("single_take_profit"))
    best = _num(position.get("max_price_since_entry"))
    worst = _num(position.get("min_price_since_entry"))
    first_hit = parse_time(position.get("first_tp_touch_candle_utc"))
    possible_hit = parse_time(position.get("possible_tp_touch_candle_utc"))
    full_bars = 0
    ambiguous_partial_hit = False
    latest = _num(position.get("latest_price"))
    latest_ts = parse_time(position.get("latest_price_timestamp_utc"))
    observed_opens = set(position.get("observed_candle_open_times") or [])

    for legacy_field in LEGACY_STOP_FIELDS:
        position.pop(legacy_field, None)

    for bar in sorted(bars, key=lambda item: str(item.get("time_open") or "")):
        opened = _bar_time(bar, "time_open")
        closed = _bar_time(bar, "time_close")
        high = _num(bar.get("high"))
        low = _num(bar.get("low"))
        close = _num(bar.get("close"))
        if opened is None or high is None or low is None or close is None:
            continue
        if entry_ts is not None and end_ts is not None:
            if closed is not None and closed <= entry_ts:
                continue
            if opened >= end_ts:
                continue
            fully_inside = opened >= entry_ts and (closed is None or closed <= end_ts)
            if not fully_inside:
                if target is not None and first_hit is None and high >= target:
                    ambiguous_partial_hit = True
                    if possible_hit is None or opened < possible_hit:
                        possible_hit = opened
                continue

        full_bars += 1
        observed_opens.add(opened.replace(minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z"))
        new_target_touch = target is not None and first_hit is None and high >= target

        if new_target_touch:
            first_hit = opened

        best = high if best is None else max(best, high)
        worst = low if worst is None else min(worst, low)
        if closed is not None and (latest_ts is None or closed >= latest_ts):
            latest = close
            latest_ts = closed

    if entry is not None and entry > 0:
        mfe = ((best / entry) - 1.0) * 100.0 if best is not None else None
        mae = ((worst / entry) - 1.0) * 100.0 if worst is not None else None
        latest_return = ((latest / entry) - 1.0) * 100.0 if latest is not None else None
        retracement = ((latest / best) - 1.0) * 100.0 if latest is not None and best and best > 0 else None
    else:
        mfe = mae = latest_return = retracement = None
    hit = first_hit is not None
    expected_opens = _expected_hour_opens(entry_ts, end_ts)
    missing_opens = [stamp for stamp in expected_opens if stamp not in observed_opens]
    candle_coverage_complete = bool(expected_opens) and not missing_opens
    position.update({
        "observed_candle_open_times": sorted(observed_opens),
        "observed_candle_open_count": len(observed_opens),
        "expected_candle_open_count": len(expected_opens),
        "missing_candle_open_count": len(missing_opens),
        "candle_coverage_complete": candle_coverage_complete,
        "max_price_since_entry": best,
        "min_price_since_entry": worst,
        "latest_price": latest,
        "latest_price_timestamp_utc": latest_ts.isoformat().replace("+00:00", "Z") if latest_ts else position.get("latest_price_timestamp_utc"),
        "max_favorable_excursion_pct": round(mfe, 4) if mfe is not None else None,
        "max_adverse_excursion_pct": round(mae, 4) if mae is not None else None,
        "latest_return_from_entry_pct": round(latest_return, 4) if latest_return is not None else None,
        "retracement_from_peak_pct": round(retracement, 4) if retracement is not None else None,
        "candles_observed_this_check": full_bars,
        "last_checked_utc": utc_now(),
        "take_profit_hit": bool(hit or position.get("take_profit_hit")),
        "first_tp_touch_candle_utc": first_hit.isoformat().replace("+00:00", "Z") if first_hit else position.get("first_tp_touch_candle_utc"),
        "possible_tp_touch_candle_utc": possible_hit.isoformat().replace("+00:00", "Z") if possible_hit else position.get("possible_tp_touch_candle_utc"),
        "ohlcv_resolution": "1h",
    })

    if entry_ts and end_ts and now >= end_ts:
        position["window_status"] = "CLOSED_24H"
        position["exit_timestamp_utc"] = end_ts.isoformat().replace("+00:00", "Z")
        position["exit_reference_price"] = latest
        position["realized_reference_return_pct"] = latest_return
        if ambiguous_partial_hit:
            position["result_status"] = "TP_TOUCH_UNCONFIRMED"
            position["position_status"] = "24H WINDOW CLOSED — POSSIBLE TP TOUCH"
        elif hit:
            position["result_status"] = "TAKE_PROFIT_REACHED_WITHIN_24H"
            position["position_status"] = "24H WINDOW CLOSED — SINGLE TP TOUCH OBSERVED"
        elif target is None:
            position["result_status"] = "TARGET_UNVERIFIED"
            position["position_status"] = "24H WINDOW CLOSED — TARGET UNVERIFIED"
        else:
            position["result_status"] = "NO_TP_HIT_IN_24H" if candle_coverage_complete else "WINDOW_DATA_INCOMPLETE"
            position["position_status"] = (
                "24H WINDOW CLOSED — TP NOT HIT" if candle_coverage_complete
                else "24H WINDOW CLOSED — INCOMPLETE CANDLE COVERAGE"
            )
    else:
        position["window_status"] = "OPEN"
        if ambiguous_partial_hit:
            position["position_status"] = "POSSIBLE TP TOUCH — HOURLY CANDLE OVERLAPS WINDOW EDGE"
            position["result_status"] = "TP_TOUCH_UNCONFIRMED"
        elif hit:
            position["position_status"] = (
                "TP TOUCH OBSERVED — STILL AT/ABOVE TARGET" if latest is not None and latest >= target
                else "TP TOUCH OBSERVED — PULLBACK"
            )
            position["result_status"] = "TAKE_PROFIT_REACHED_WITHIN_24H"
        elif target is None:
            position["position_status"] = "TARGET FORMING — MFE TRACKED"
            position["result_status"] = "NO_VERIFIED_TARGET"
        else:
            position["position_status"] = "OPEN — SINGLE TP NOT YET OBSERVED"
            position["result_status"] = "OPEN"
    return position


def qualify_final_actionable_candidates(candidates: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Filter discovery rows through the deterministic production action gate."""
    try:
        from scripts.tp_gate import canonical_tp_upside
        from scripts.address_identity import same_address
    except ImportError:
        from tp_gate import canonical_tp_upside
        from address_identity import same_address
    actionable = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        plan = candidate.get("trade_plan") if isinstance(candidate.get("trade_plan"), dict) else {}
        tp_validation = candidate.get("technical_tp_validation")
        tp_validation = tp_validation if isinstance(tp_validation, dict) else {}
        reference = _num(candidate.get("final_price_usd"))
        price_stamp = parse_time(candidate.get("final_price_timestamp_utc"))
        age_seconds = (now - price_stamp).total_seconds() if price_stamp else None
        pool = str(candidate.get("pool_address") or candidate.get("pair_address") or "")
        contract = str(candidate.get("contract_address") or "")
        target = _num(plan.get("single_take_profit") or candidate.get("target_price"))
        declared_target = _num(candidate.get("target_price"))
        upside = canonical_tp_upside(reference, target)
        maximum_tp_distance = _num(tp_validation.get("maximum_tp_distance_pct"))
        technical_reference = _num(tp_validation.get("current_price"))
        technical_atr = _num(tp_validation.get("atr_1h_pct"))
        max_reference_drift = max(5.0, min(25.0, technical_atr)) if technical_atr is not None else None
        reference_drift_pct = (
            abs(reference / technical_reference - 1.0) * 100.0
            if reference is not None and technical_reference is not None and technical_reference > 0 else None
        )
        candidate["technical_tp_upside_pct"] = round(upside, 8) if upside is not None else None
        quotes = candidate.get("dex_execution_quotes") if isinstance(candidate.get("dex_execution_quotes"), dict) else {}
        quote_stamp = parse_time(quotes.get("timestamp_utc"))
        quote_age_seconds = (now - quote_stamp).total_seconds() if quote_stamp else None
        candle_stamp = parse_time(tp_validation.get("candle_retrieved_utc"))
        candle_price_delta = (
            abs((candle_stamp - price_stamp).total_seconds())
            if candle_stamp is not None and price_stamp is not None else None
        )
        guard = candidate.get("dex_loss_guard_evidence") if isinstance(candidate.get("dex_loss_guard_evidence"), dict) else {}
        guard_checks = guard.get("checks") if isinstance(guard.get("checks"), dict) else {}
        source = str(candidate.get("target_price_source") or "")
        observed_target = source in {
            "7D_SWING_RESISTANCE", "24H_RANGE_HIGH",
            "OBSERVED_SWING_RESISTANCE", "OBSERVED_RECENT_RANGE_HIGH",
        }
        validation_matches_identity = (
            str(tp_validation.get("chain") or "").lower() == str(candidate.get("chain") or "").lower() and
            str(tp_validation.get("pool_address") or "") == pool and
            same_address(candidate.get("chain"), tp_validation.get("token_address"), contract) and
            tp_validation.get("token_orientation_verified") is True
        )
        validation_matches_target = (
            declared_target is not None and target is not None and
            abs(declared_target - target) <= max(abs(target) * 1e-8, 1e-12) and
            _num(tp_validation.get("target_price")) is not None and
            abs(_num(tp_validation.get("target_price")) - target) <= max(abs(target) * 1e-8, 1e-12) and
            tp_validation.get("target_price_source") in {
                "7D_SWING_RESISTANCE", "24H_RANGE_HIGH",
                "OBSERVED_SWING_RESISTANCE", "OBSERVED_RECENT_RANGE_HIGH",
            }
        )
        tp_validation_pass = (
            tp_validation.get("status") == "PASS" and
            validation_matches_identity and validation_matches_target and
            maximum_tp_distance is not None and
            upside is not None and upside <= maximum_tp_distance and
            reference_drift_pct is not None and max_reference_drift is not None and
            reference_drift_pct <= max_reference_drift
        )
        software_failure = (
            candidate.get("signal_engine_executed") is False or
            candidate.get("expected_return_engine_executed") is False or
            candidate.get("engine_result_status") == "SOFTWARE_FAILURE" or
            candidate.get("expected_return_status") == "SOFTWARE_FAILURE" or
            "SOFTWARE_ERROR_SIGNAL_ENGINES_INCOMPLETE" in (candidate.get("early_breakout_risk_flags") or []) or
            "SOFTWARE_ERROR_SIGNAL_ENGINES_INCOMPLETE" in (candidate.get("risk_flags") or [])
        )
        complete = (
            not software_failure and
            str(candidate.get("market_type") or "").lower() == "dex_pool" and
            candidate.get("final_price_verified") is True and reference is not None and reference > 0 and
            age_seconds is not None and 0 <= age_seconds <= 300 and
            candle_price_delta is not None and candle_price_delta <= 900 and
            bool(pool and contract and candidate.get("chain")) and
            str(candidate.get("final_pool_address") or "") == pool and
            same_address(candidate.get("chain"), candidate.get("final_token_contract"), contract) and
            str(candidate.get("final_quote_chain") or "").lower() == str(candidate.get("chain") or "").lower() and
            candidate.get("final_quote_token_orientation_verified") is True and
            candidate.get("dex_loss_guard_status") == "PASS" and
            guard.get("status") == "PASS" and
            all(guard_checks.get(key) is True for key in (
                "exact_token_and_pool_identity", "launch_wallet_analysis", "lp_position_audit",
                "token_security_audit", "two_way_route_quotes")) and
            candidate.get("safety_verified") is True and
            candidate.get("executable_route_verified") is True and
            bool(quotes.get("provider") and isinstance(quotes.get("buy_quote"), dict) and quotes.get("buy_quote") and
                 isinstance(quotes.get("sell_quote"), dict) and quotes.get("sell_quote")) and
            quote_age_seconds is not None and 0 <= quote_age_seconds <= 300 and
            observed_target and tp_validation_pass and
            upside is not None and upside >= 40.0
        )
        blockers = []
        if software_failure:
            blockers.append("signal engine software execution failed or threw an exception")
        if str(candidate.get("market_type") or "").lower() != "dex_pool":
            blockers.append("only exact-identity DEX pools can enter the qualified lane")
        if not observed_target:
            blockers.append("technical target is not an observed candle resistance")
        if upside is None or upside < 40.0:
            blockers.append("technical single-TP upside is below +40.00%")
        if tp_validation.get("status") != "PASS":
            blockers.extend(tp_validation.get("blockers") or ["technical TP evidence is missing or failed"])
        if not validation_matches_identity:
            blockers.append("technical TP evidence identity or token orientation does not match")
        if not validation_matches_target:
            blockers.append("technical TP provenance does not match the candidate target")
        if (reference_drift_pct is None or max_reference_drift is None or
                reference_drift_pct > max_reference_drift):
            blockers.append("final price diverges excessively from the TP candle reference price")
        if candle_price_delta is None or candle_price_delta > 900:
            blockers.append("technical TP candles are not time-aligned with the final price")
        if age_seconds is None or not 0 <= age_seconds <= 300:
            blockers.append("final price is missing, future-dated, or older than 300 seconds")
        if not candidate.get("final_price_verified") or str(candidate.get("final_pool_address") or "") != pool:
            blockers.append("exact pool price verification failed")
        if (not pool or not contract or not candidate.get("chain") or
                not same_address(candidate.get("chain"), candidate.get("final_token_contract"), contract) or
                str(candidate.get("final_quote_chain") or "").lower() != str(candidate.get("chain") or "").lower() or
                candidate.get("final_quote_token_orientation_verified") is not True):
            blockers.append("final chain, token, pool, or price orientation identity did not pass")
        if not candidate.get("safety_verified") or candidate.get("dex_loss_guard_status") != "PASS":
            blockers.append("mandatory safety or loss-pattern gate did not pass")
        if guard.get("status") != "PASS" or not all(guard_checks.get(key) is True for key in (
                "exact_token_and_pool_identity", "launch_wallet_analysis", "lp_position_audit",
                "token_security_audit", "two_way_route_quotes")):
            blockers.append("loss-pattern evidence checks are incomplete")
        if candidate.get("executable_route_verified") is not True:
            blockers.append("execution-route verification did not pass")
        if quote_age_seconds is None or not 0 <= quote_age_seconds <= 300:
            blockers.append("buy/sell quotes are stale, future-dated, or missing a timestamp")
        if not (quotes.get("provider") and quotes.get("buy_quote") and quotes.get("sell_quote") and
                quote_age_seconds is not None and 0 <= quote_age_seconds <= 300):
            blockers.append("fresh two-way execution quotes are missing")
        candidate["qualification_blockers"] = list(dict.fromkeys(blockers))
        candidate["opportunity_status"] = "QUALIFIED" if complete else "NOT QUALIFIED"
        candidate["entry_decision"] = "QUALIFIED" if complete else "NOT QUALIFIED"
        if complete:
            candidate["output_category"] = "VERIFIED_ACTIONABLE_OPPORTUNITY"
            candidate["forward_opportunity_decision"] = "QUALIFIED"
            entry = dict(candidate)
            plan = dict(plan)
            plan.update({"entry_price": reference, "tp_reference_price": reference,
                         "tp_upside_pct": round(upside, 8), "single_take_profit": target,
                         "pair_address": pool})
            entry["trade_plan"] = plan
            entry["price"] = reference
            entry["tp_upside_pct"] = round(upside, 8)
            entry["verification_status"] = "PASS"
            entry["safety_status"] = "PASS"
            entry["execution_status"] = "PASS — independently sourced buy and sell quotes"
            actionable.append(entry)
    return actionable


def classify_final_candidates(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Unified ranking of qualifying opportunities with potential >= +40.00%.

    Do not split young DEX discovery leads from the mature research watchlist;
    all qualifying candidates with single-TP potential >= +40.00% are presented together.
    """
    research_watchlist = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("opportunity_status") == "QUALIFIED":
            continue
        # Minimum potential: +40.00%. Anything below +40% is irrelevant and excluded.
        plan = candidate.get("trade_plan") or {}
        entry = _num(candidate.get("final_price_usd"))
        if entry is None:
            entry = _num(candidate.get("price"))
        if entry is None and isinstance(plan, dict):
            entry = _num(plan.get("entry_price")) or _num(plan.get("tp_reference_price"))
        target = _num(plan.get("single_take_profit")) if isinstance(plan, dict) else None
        if target is None:
            target = _num(candidate.get("target_price"))
        upside = _num(candidate.get("tp_upside_pct"))
        if upside is None and entry and target and entry > 0:
            upside = round(((target - entry) / entry) * 100.0, 8)
        if upside is None or upside < 40.0:
            continue
        # Rule 5: No invented opportunities. Exclude dead/drained pools (liq < $15k), thin liquidity traps, inverted stablecoins, or corrupt wicks.
        liq = _num(candidate.get("liquidity_usd"))
        if liq is not None and liq < 15000.0:
            continue
        flags = set(candidate.get("early_breakout_risk_flags") or []) | set(candidate.get("risk_flags") or [])
        if "EXTREME_THIN_LIQUIDITY" in flags or "LIQUIDITY_UNDER_5000_USD" in flags:
            continue
        if upside > 1000.0 and (candidate.get("technical_tp_validation") or {}).get("status") != "PASS":
            continue
        pair_str = str(candidate.get("pair") or "").upper()
        if any(s in pair_str for s in ("USDT/USDC", "USDC/USDT", "DAI/USDC", "USDC/DAI")) and entry is not None:
            if entry < 0.8 or entry > 1.2:
                continue

        candidate["opportunity_status"] = "NOT QUALIFIED"
        candidate["output_category"] = "UNVERIFIED_RESEARCH_WATCHLIST"
        candidate["entry_decision"] = "NOT QUALIFIED"
        candidate["forward_opportunity_decision"] = "NOT QUALIFIED"
        research_watchlist.append(candidate)
    return research_watchlist, []


def update_position_monitor(outdir: Path, memory_dir: Path, run_id: str) -> dict[str, Any]:
    forward_path = outdir / "forward-24h-rocket.json"
    forward = _load_json(forward_path, {})
    report = _load_json(outdir / "rocket-hunt.json", {})
    if not isinstance(forward, dict):
        return {"status": "FORWARD_REPORT_MISSING"}
    forward = apply_forward_opportunity_ranking(forward, report)
    now = datetime.now(timezone.utc)
    client = DexPaprikaClient(memory_dir)
    networks, registry_meta = _load_registry(client)
    try:
        from scripts import venues as venue_adapters
        from scripts.tp_gate import canonical_tp_upside
    except ImportError:
        import venues as venue_adapters
        from tp_gate import canonical_tp_upside
    cex_fx = venue_adapters.Fetcher(timeout=8, retries=2, pause=0)

    state_path = memory_dir / "active-24h-watchlist.json"
    state = _load_json(state_path, {})
    positions = state.get("positions") if isinstance(state, dict) else []
    positions = positions if isinstance(positions, list) else []
    completed = _load_json(memory_dir / "completed-24h-watchlist.json", [])
    completed = completed if isinstance(completed, list) else []
    existing_by_key = {_position_key(item): item for item in positions if isinstance(item, dict)}
    candidates = forward.get("candidates") if isinstance(forward.get("candidates"), list) else []

    # Monitor existing DEX and CEX references first. GeckoTerminal is used for
    # exact-token USD candles on any DEX quote asset; CEX pairs use venue-specific
    # spot OHLCV. No quote-symbol whitelist can hide a good altcoin opportunity.
    updated = []
    gecko_last_request = 0.0
    request_count = 0
    monitor_rows = sorted(
        existing_by_key.values(),
        key=lambda item: (parse_time(item.get("time_stop_utc")) or now,
                          -(_num(item.get("early_breakout_score")) or 0.0)),
    )
    for position in monitor_rows:
        for legacy_field in LEGACY_STOP_FIELDS:
            position.pop(legacy_field, None)
        start_ts = parse_time(position.get("entry_timestamp_utc"))
        end_ts = parse_time(position.get("time_stop_utc"))
        if start_ts is None or end_ts is None:
            position["position_status"] = "DATA GAP — ENTRY WINDOW TIMESTAMP MISSING"
            updated.append(position)
            continue
        market_type = str(position.get("market_type") or ("dex_pool" if position.get("pool_address") else "cex_spot")).lower()
        if market_type == "dex_pool":
            wait = GECKO_POSITION_INTERVAL_SECONDS - (time.monotonic() - gecko_last_request) if gecko_last_request else 0
            if wait > 0:
                time.sleep(wait)
            bars, candle_url, error = _fetch_gecko_position_bars(position, timeout=8)
            gecko_last_request = time.monotonic()
            request_count += 1
            position["ohlcv_source"] = "GeckoTerminal exact tracked-token USD OHLCV"
        else:
            bars, candle_url, error = _fetch_cex_position_bars(position, venue_adapters, cex_fx, now)
            request_count += 1
            position["ohlcv_source"] = "CEX spot venue 1h OHLCV"
        position["ohlcv_url"] = candle_url
        position["ohlcv_timestamp_utc"] = utc_now() if not error else position.get("ohlcv_timestamp_utc")
        if error:
            position["last_monitor_error"] = error
        else:
            position.pop("last_monitor_error", None)
        _update_position(position, bars, now)
        if position.get("window_status") in ("CLOSED_24H", "CLOSED_EARLY"):
            completed.append(position)
        else:
            updated.append(position)

    by_key = {_position_key(item): item for item in updated}
    # Avoid duplicating a recently completed 24-hour observation window.
    reentry_cooldown = timedelta(hours=4)
    recent_closed_by_key = {}
    for item in completed:
        closed_at = parse_time(item.get("exit_timestamp_utc") or item.get("last_checked_utc"))
        if closed_at is not None:
            recent_closed_by_key[_position_key(item)] = max(
                closed_at, recent_closed_by_key.get(_position_key(item), closed_at)
            )
    new_positions = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        key = _position_key(candidate)
        existing = by_key.get(key)
        if existing is not None:
            existing["latest_early_breakout_score"] = candidate.get("early_breakout_score")
            existing["latest_forecast_rank"] = candidate.get("forecast_rank")
            existing["latest_forward_opportunity_rank"] = candidate.get("forward_opportunity_rank")
            continue
        if len(by_key) >= MAX_MONITORED_POSITIONS or new_positions >= MAX_NEW_POSITIONS_PER_SCAN:
            break
        market_type = str(candidate.get("market_type") or "").lower()
        entry = _num(candidate.get("price"))
        stamp = parse_time(candidate.get("data_timestamp_utc") or forward.get("as_of_utc"))
        if entry is None or entry <= 0 or stamp is None:
            continue
        last_closed = recent_closed_by_key.get(key)
        if last_closed is not None and stamp - last_closed < reentry_cooldown:
            continue
        if market_type == "dex_pool":
            chain = str(candidate.get("chain") or "").lower()
            pool = str(candidate.get("pool_address") or candidate.get("pair_address") or "").strip()
            contract = str(candidate.get("contract_address") or "").strip()
            if not chain or not pool or not contract:
                continue
            venue = None
            venue_pair = None
        elif market_type == "cex_spot":
            venue = str(candidate.get("venue") or "").lower()
            adapter = venue_adapters.VENUES.get(venue)
            if adapter is None:
                continue
            venue_pair = str(candidate.get("venue_pair_symbol") or "").strip()
            if not venue_pair:
                base_symbol = str(candidate.get("asset") or candidate.get("symbol") or "").upper().strip()
                if not base_symbol:
                    continue
                try:
                    venue_pair = adapter.pair_to_symbol(base_symbol)
                except Exception:
                    continue
            if not venue_pair:
                continue
            chain = None
            pool = None
            contract = None
        else:
            continue
        plan = candidate.get("trade_plan") if isinstance(candidate.get("trade_plan"), dict) else {}
        target = _num(plan.get("single_take_profit"))
        if target is None:
            target = _num(candidate.get("target_price"))
        tp_upside = canonical_tp_upside(entry, target)
        if tp_upside is None or tp_upside < 40.0:
            continue
        end = stamp + timedelta(hours=24)
        record = {
            "identity": key,
            "asset": candidate.get("asset"),
            "symbol": candidate.get("symbol"),
            "pair": candidate.get("pair"),
            "market_type": market_type,
            "chain": chain,
            "pool_address": pool,
            "contract_address": contract,
            "venue": venue,
            "venue_pair": venue_pair,
            "pair_url": candidate.get("pair_url"),
            "quote_symbol": candidate.get("quote_symbol"),
            "source": candidate.get("source") or "forward-watchlist",
            "entry_price": entry,
            "entry_timestamp_utc": stamp.isoformat().replace("+00:00", "Z"),
            "single_take_profit": target,
            "target_method": candidate.get("target_price_source"),
            "time_stop_utc": end.isoformat().replace("+00:00", "Z"),
            "early_breakout_score": candidate.get("early_breakout_score"),
            "early_breakout_tier": candidate.get("early_breakout_tier"),
            "forward_opportunity_rank": candidate.get("forward_opportunity_rank"),
            "latest_forecast_rank": candidate.get("forecast_rank"),
            "max_price_since_entry": entry,
            "min_price_since_entry": entry,
            "latest_price": entry,
            "latest_price_timestamp_utc": stamp.isoformat().replace("+00:00", "Z"),
            "take_profit_hit": False,
            "possible_tp_touch_candle_utc": None,
            "first_tp_touch_candle_utc": None,
            "window_status": "OPEN",
            "position_status": "OPEN — INITIAL REFERENCE",
            "research_only": True,
            "execution_status": "NO WALLET OR TRADE EXECUTION",
            "created_run_id": str(run_id),
        }
        by_key[key] = record
        new_positions += 1

    updated = list(by_key.values())
    updated.sort(key=lambda item: (
        parse_time(item.get("time_stop_utc")) or now,
        -(_num(item.get("early_breakout_score")) or 0.0),
    ))
    completed = completed[-500:]
    state_out = {
        "schema": "crypto-prebreakout.active-24h-watchlist.v2",
        "provider_version": VERSION,
        "updated_utc": utc_now(),
        "run_id": str(run_id),
        "max_positions": MAX_MONITORED_POSITIONS,
        "max_new_positions_per_scan": MAX_NEW_POSITIONS_PER_SCAN,
        "position_count": len(updated),
        "positions": updated,
        "policy": "Research-only 24h reference monitoring: exactly one single take-profit of at least +40.00%; no stop loss or trailing stop; no actual wallet trades or fills are known.",
    }
    _write_json(state_path, state_out)
    _write_json(memory_dir / "completed-24h-watchlist.json", completed)
    forward["position_monitor"] = {
        "status": "OK" if updated or completed else "NO_ELIGIBLE_REFERENCE_POSITIONS",
        "provider": "GeckoTerminal exact-token USD OHLCV for DEX; venue-specific spot OHLCV for CEX",
        "ohlcv_resolution": "1h; full candles only; DEX USD denomination is selected by exact token contract/mint",
        "management_policy": {
            "max_reference_window_hours": 24,
            "take_profit_count": 1,
            "take_profit_minimum_upside_pct": 40.0,
            "stop_loss": "NONE",
            "trailing_stop": "NONE",
            "window_close": "close the research observation window at 24h; this is not a stop-loss or an executable order",
        },
        "max_open_reference_positions": MAX_MONITORED_POSITIONS,
        "new_reference_positions_per_scan": MAX_NEW_POSITIONS_PER_SCAN,
        "active_positions": updated,
        "recently_completed_positions": completed[-20:],
        "gecko_requests_this_postscan": request_count,
        "gecko_request_interval_seconds": GECKO_POSITION_INTERVAL_SECONDS,
        "rolling_request_ceiling": SCANNER_ROLLING_REQUEST_CEILING,
        "estimated_rolling_requests_reserved": len(_rolling_ledger(memory_dir)["requests"]),
        "registry_network_count": len(networks),
        "registry_meta": registry_meta,
        "research_only": True,
        "new_reference_positions_this_scan": new_positions,
        "important_limit": "Hourly candles establish observed reference highs/lows, not actual executable fills. A price target touch is not a realized fill or profit.",
    }
    # Final production gate runs after all ranking/monitoring mutations. Discovery
    # remains intact; only the independently verified action lane is filtered.
    finalization_now = datetime.now(timezone.utc)
    actionable = qualify_final_actionable_candidates(candidates, finalization_now)
    forward["general_version"] = "v4.26.7"
    forward["scan_finished_utc"] = finalization_now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    forward["github_actions_run_id"] = str(run_id) if run_id else None
    repository = os.environ.get("GITHUB_REPOSITORY")
    forward["github_actions_run_url"] = (
        "https://github.com/%s/actions/runs/%s" % (repository, run_id)
        if repository and run_id else None
    )
    forward["verified_actionable_opportunities"] = actionable
    research_watchlist, discovery_only = classify_final_candidates(candidates)
    forward["top_opportunities"] = copy.deepcopy(actionable[:25])
    forward["opportunity_board_display_limit"] = len(forward["top_opportunities"])
    ranking = forward.get("forward_opportunity_ranking")
    if isinstance(ranking, dict):
        ranking["published_top_n"] = len(forward["top_opportunities"])
        ranking["research_watchlist_top_n"] = min(25, len(research_watchlist))
    forward["forward_opportunity_leader"] = copy.deepcopy(actionable[0]) if actionable else None
    forward["unverified_research_watchlist"] = research_watchlist
    forward["young_discovery_only_leads"] = discovery_only
    forward["forward_watchlist"] = research_watchlist
    forward["discovery_candidates"] = discovery_only
    forward["forward_research_leader"] = (
        copy.deepcopy(research_watchlist[0]) if research_watchlist else
        copy.deepcopy(discovery_only[0]) if discovery_only else None
    )
    forward["top_rocket_pick"] = copy.deepcopy(actionable[0]) if actionable else None
    forward["forecast_leader"] = copy.deepcopy(actionable[0]) if actionable else None
    forward["upside_leader"] = copy.deepcopy(actionable[0]) if actionable else None
    forward["forward_watchlist_leader"] = copy.deepcopy(research_watchlist[0]) if research_watchlist else None
    forward["best_immediate_entry"] = actionable[0] if actionable else None
    forward["status"] = "QUALIFIED_OPPORTUNITIES" if actionable else "ZERO_VERIFIED_OPPORTUNITIES"
    forward["summary"] = (
        "QUALIFIED OPPORTUNITY: %s (%s)" % (
            actionable[0].get("asset") or "Unknown",
            actionable[0].get("pair") or "Unknown",
        ) if actionable else
        "ZERO VERIFIED OPPORTUNITIES — all remaining candidates are NOT QUALIFIED research or discovery leads."
    )
    forward["actionable_gate"] = {
        "minimum_single_tp_upside_pct": 40.0,
        "target_provenance": "observed non-anomalous 1h candle resistance; hypothetical scenario excluded",
        "maximum_technical_tp_distance": "max(+40%, 3 x 1h ATR)",
        "minimum_pool_liquidity_usd": 100_000,
        "minimum_completed_hourly_candles": 20,
        "max_price_candle_timestamp_delta_seconds": 900,
        "qualified_count": len(actionable),
        "status": "PASS" if actionable else "ZERO_VERIFIED_OPPORTUNITIES",
        "price_max_age_seconds": 300,
        "note": "Only candidates passing the observed-resistance TP credibility checks, +40.00% technical TP threshold, final exact-pool identity, price freshness/orientation, safety, and fresh two-way execution quotes appear in the qualified lane. Other leads are explicitly NOT QUALIFIED and split by age.",
    }
    _write_json(forward_path, forward)
    main_coverage = report.get("coverage") if isinstance(report.get("coverage"), dict) else {}
    main_coverage["position_monitor_v428"] = {
        "active_positions": len(updated),
        "recently_completed": len(completed),
        "max_open_reference_positions": MAX_MONITORED_POSITIONS,
        "new_reference_positions_per_scan": MAX_NEW_POSITIONS_PER_SCAN,
        "ohlcv_resolution": "1h",
        "requests_this_postscan": request_count,
    }
    report["coverage"] = main_coverage
    _write_json(outdir / "rocket-hunt.json", report)
    return {
        "status": "OK",
        "active_positions": len(updated),
        "completed_positions": len(completed),
        "requests_this_postscan": request_count,
        "rolling_requests_reserved": len(_rolling_ledger(memory_dir)["requests"]),
        "run_id": str(run_id),
    }


def install_runtime_patches(repo_root: Path) -> dict[str, Any]:
    repo_root = Path(repo_root).resolve()
    target = repo_root / "scripts" / "rocket_hunt.py"
    if not target.exists():
        raise FileNotFoundError("scripts/rocket_hunt.py must be extracted before v4.25 install")
    source = target.read_text(encoding="utf-8")
    marker = 'if __name__ == "__main__":'
    hook = (
        "\ntry:\n"
        "    from scripts.v425_early_breakout import install_early_breakout_hooks as _v425_install\n"
        "except ImportError:\n"
        "    from v425_early_breakout import install_early_breakout_hooks as _v425_install\n"
        "import sys as _v425_sys\n"
        "_v425_install(_v425_sys.modules[__name__])\n"
    )
    if "_v425_install(__import__(__name__))" not in source:
        if marker not in source:
            raise RuntimeError("could not locate rocket_hunt.py main hook point")
        source = source.replace(marker, hook + "\n" + marker, 1)
        target.write_text(source, encoding="utf-8")
    return {
        "installed_version": VERSION,
        "hook": "after v4.24 Birdeye hook; DexPaprika pool discovery is called within DEX discovery pass",
        "keyless_only": True,
        "paid_fallback": False,
        "rolling_request_ceiling": SCANNER_ROLLING_REQUEST_CEILING,
        "max_monitored_positions": MAX_MONITORED_POSITIONS,
    }


def self_test() -> None:
    sample = {
        "chain": "base", "id": "0xpool", "price_usd": 0.01,
        "price_change_percentage_5m": 4.0,
        "price_change_percentage_1h": 22.0,
        "price_change_percentage_6h": 30.0,
        "price_change_percentage_24h": 31.0,
        "volume_usd_24h": 400000, "liquidity_usd": 90000,
        "transactions_24h": 1200, "created_at": utc_now(),
        "tokens": [
            {"id": "0xtoken", "name": "Alpha", "symbol": "ALPHA",
             "summary": {"price_usd": 0.01, "fdv": 1000000},
             "1h": {"buy_usd": 8000, "sell_usd": 2000, "volume_usd": 10000, "txns": 120}},
            {"id": "0xusdc", "name": "USD Coin", "symbol": "USDC"},
        ],
    }
    rec = _normalize_pool(sample, "pool_momentum_1h", 1,
                          lambda **kw: {**kw, **(kw.get("extra") or {}), "contract_address": kw.get("address")})
    assert rec and rec["identity"] == "dex:base:0xpool"
    assert rec["contract_address"] == "0xtoken"
    assert rec["quote_token_address"] == "0xusdc"
    assert rec["early_breakout_score"] > 0
    assert rec["early_breakout_tier"]
    assert score_candidate({"change_5m_pct": 5, "change_1h_pct": 20})["score"] > 0
    assert _network_rows([{"id": "base", "display_name": "Base"}, {"id": "base"}]) == [
        {"id": "base", "display_name": "Base"}
    ]
    print("v4.25.1 early-breakout self-test passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--post-scan", action="store_true")
    parser.add_argument("--outdir", type=Path, default=Path("rocket-run"))
    parser.add_argument("--memory-dir", type=Path, default=None)
    parser.add_argument("--run-id", type=str, default=os.environ.get("GITHUB_RUN_ID", "local-run"))
    args = parser.parse_args(argv)
    if args.install:
        print(json.dumps(install_runtime_patches(args.repo_root), sort_keys=True))
        return 0
    if args.self_test:
        self_test()
        return 0
    if args.post_scan:
        if args.memory_dir is None:
            parser.error("--memory-dir is required with --post-scan")
        result = update_position_monitor(args.outdir, args.memory_dir, args.run_id)
        print(json.dumps(result, sort_keys=True))
        return 0
    parser.error("choose --install, --self-test or --post-scan")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
