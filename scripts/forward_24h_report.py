#!/usr/bin/env python3
"""Build the scan-start-anchored forward-24h Top Rocket and single-TP report.

Research-only. It fetches OHLCV for shortlisted markets, derives one explicitly
labelled technical target, and feeds the timestamped data to next_day_forecast.
Missing execution/safety/CVD/derivatives data remains UNVERIFIED.
"""
import argparse
import json
import math
import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import next_day_forecast  # noqa: E402
from address_identity import address_key, same_address  # noqa: E402
from tp_gate import canonical_tp_upside  # noqa: E402
import venues  # noqa: E402

GECKO = "https://api.geckoterminal.com/api/v2"
GECKO_ALIASES = {"avalanche": "avax", "sui": "sui-network", "fantom": "ftm",
                 "cronos": "cro", "sei": "sei-network"}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_dt(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc))
    except (TypeError, ValueError):
        return None


def num(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={
        "Accept": "application/json;version=20230203",
        "User-Agent": "crypto-prebreakout-forward-24h/4.28.0 (read-only research)"
    })
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8")), utc_now()


def pick_candidates(report, dex_limit=140, cex_limit=25):
    # Merge exact DEX pools across full-universe, short-window, extreme-gainer
    # and fresh-launch boards; retain CEX spot markets as a parallel research lane.
    selected_by_id = {}
    boards = (
        "universe_rows_v419", "discovery_rows_v421",
        "execution_candidate_shortlist", "most_actionable_liquidity_screen",
        "top_5m_movers", "top_1h_movers", "top_6h_movers",
        "top_24h_gainers", "top_24h_movers", "top_24h_volume",
        "extreme_movers_100pct_plus", "extreme_movers_400pct_plus",
        "extreme_movers_1000pct_plus", "new_launch_movers",
        "new_pools_24h", "new_pools_under_24h", "new_launch_discovery_board_v418",
        "early_breakout_leaders", "high_risk_moonshot_scouts",
    )
    for board in boards:
        rows = report.get(board) or []
        for row in rows:
            if not isinstance(row, dict) or row.get("market_type") not in ("dex_pool", "cex_spot"):
                continue
            kind = row.get("market_type")
            chain_or_venue = str(row.get("chain") or row.get("venue") or "").lower()
            pool_address = str(row.get("pool_address") or row.get("pair_address") or "").strip()
            original_identity = str(row.get("identity") or "")
            # Exact pool address is the DEX market identity. Providers sometimes
            # key a row by token contract and sometimes by pool address.
            ident = ("dex:%s:%s" % (chain_or_venue, pool_address.lower())
                     if kind == "dex_pool" and pool_address
                     else original_identity.lower())
            if not ident:
                ident = "%s:%s:%s" % (kind, chain_or_venue, row.get("contract_address") or row.get("pair"))
            existing = selected_by_id.get(ident)
            if existing is None:
                record = dict(row)
                if kind == "dex_pool" and pool_address:
                    record["source_identity"] = original_identity or None
                    record["identity"] = ident
                selected_by_id[ident] = record
                continue
            previous_score = num(existing.get("early_breakout_score"))
            incoming_score = num(row.get("early_breakout_score"))
            incoming_normalized = str(row.get("source") or "").startswith("dexpaprika_")
            existing_normalized = str(existing.get("source") or "").startswith("dexpaprika_")
            prefer_signal = incoming_score is not None and (
                previous_score is None or incoming_score > previous_score
            )
            prefer_token_mapping = incoming_normalized and not existing_normalized
            breakout_fields = {
                "change_5m_pct", "change_1h_pct", "change_6h_pct", "change_24h_pct",
                "price_usd", "liquidity_usd", "volume_24h_usd", "txns_24h",
                "age_hours", "market_cap_usd", "fdv_usd",
                "asset", "symbol", "pair", "contract_address",
                "quote_token_address", "quote_symbol",
                "dexpaprika_token_order_swapped_for_tracking",
                "source", "retrieved_utc", "source_url", "pair_url",
                "breakout_confluence_score", "breakout_engine_scores", "breakout_acceleration_signals",
                "probability_tp_hit_24h_pct", "expected_net_24h_return_pct", "potential_mfe_24h_pct",
                "potential_mae_24h_pct", "estimated_hours_to_target", "reversal_risk_score",
                "realistic_opportunity_rank",
            }
            if prefer_signal or prefer_token_mapping:
                # A normalized DEX-pool record is authoritative for which
                # non-quote token is being tracked, even if another board gave
                # the pool a higher opaque score. Keep the strongest score
                # bundle, but never let a quote asset become the tracked coin.
                for key, value in row.items():
                    if value is None:
                        continue
                    if key in breakout_fields or key.startswith("dexpaprika_"):
                        existing[key] = value
                    if prefer_signal and key.startswith("early_breakout_"):
                        existing[key] = value
            # Fill gaps only; do not replace a verified pool/token mapping.
            for key, value in row.items():
                if existing.get(key) is None and value is not None:
                    existing[key] = value
    selected = list(selected_by_id.values())
    dex = [r for r in selected if r.get("market_type") == "dex_pool"]
    cex = [r for r in selected if r.get("market_type") == "cex_spot"]

    def bounded_return(value, cap):
        value = num(value)
        return max(-cap, min(cap, value)) if value is not None else -cap

    sort_key = lambda r: (
        num(r.get("early_breakout_score")) or 0,
        bounded_return(r.get("change_5m_pct"), 500),
        bounded_return(r.get("change_1h_pct"), 5000),
        bounded_return(r.get("change_6h_pct"), 10000),
        bounded_return(r.get("change_24h_pct"), 50000),
        num(r.get("dexpaprika_1h_buy_usd")) or 0,
        num(r.get("liquidity_usd")) or 0,
        num(r.get("volume_24h_usd")) or 0,
        num(r.get("rank_score")) or 0,
    )
    dex.sort(key=sort_key, reverse=True)
    cex.sort(key=sort_key, reverse=True)
    return dex[:max(0, dex_limit)] + cex[:max(0, cex_limit)]


def _hourly_bars(candles, as_of_timestamp=None):
    """Return unique, structurally valid, completed hourly OHLCV bars."""
    as_of = parse_dt(as_of_timestamp)
    as_of_epoch = as_of.timestamp() if as_of else None
    by_time = {}
    invalid = 0
    for row in candles or []:
        if not isinstance(row, dict):
            invalid += 1
            continue
        values = {key: num(row.get(key)) for key in ("o", "h", "l", "c", "qv", "t")}
        if any(value is None for value in values.values()):
            invalid += 1
            continue
        stamp = values["t"]
        if stamp > 1_000_000_000_000:
            stamp /= 1000.0
        values["t"] = int(stamp)
        if (values["t"] <= 0 or min(values["o"], values["h"], values["l"], values["c"]) <= 0 or
                values["qv"] < 0 or values["l"] > min(values["o"], values["c"]) or
                values["h"] < max(values["o"], values["c"]) or values["h"] < values["l"]):
            invalid += 1
            continue
        if as_of_epoch is not None and values["t"] + 3600 > as_of_epoch:
            continue
        if values["t"] in by_time:
            invalid += 1
            continue
        by_time[values["t"]] = values
    return sorted(by_time.values(), key=lambda row: row["t"]), invalid


def _resistance_bars(bars):
    """Exclude isolated extreme upper wicks from technical resistance levels."""
    ranges = [row["h"] - row["l"] for row in bars]
    typical = statistics_median(ranges[-24:]) if ranges else 0.0
    if typical <= 0:
        return bars, 0
    clean = []
    excluded = 0
    for row in bars:
        span = row["h"] - row["l"]
        upper_wick = row["h"] - max(row["o"], row["c"])
        if span > 3.0 * typical and upper_wick >= 0.65 * span:
            excluded += 1
        else:
            clean.append(row)
    return clean, excluded


def derive_single_target(candles, current_price, as_of_timestamp=None):
    """Choose an observed swing/range resistance; projections stay explicitly non-technical."""
    bars, _ = _hourly_bars(candles, as_of_timestamp)
    if current_price is None or current_price <= 0 or len(bars) < 20:
        return None, "UNVERIFIED", "Insufficient completed OHLCV candles to derive one target."
    bars, _ = _resistance_bars(bars)
    if len(bars) < 20:
        return None, "UNVERIFIED", "Insufficient non-anomalous completed candles to derive resistance."
    highs = [float(x["h"]) for x in bars]
    pivots = []
    for i in range(2, len(bars) - 2):
        h = highs[i]
        if h > current_price * 1.005 and h >= max(highs[i-2:i]) and h > max(highs[i+1:i+3]):
            pivots.append(h)
    if pivots:
        level = min(pivots)
        target = level * 0.9975  # aim just below the nearest observed overhead swing high
        if target > current_price:
            return round(target, 12), "7D_SWING_RESISTANCE", "Single take-profit set 0.25% below the nearest confirmed 1h swing high in the available 7d candles."
    last24 = bars[-24:]
    hi24 = max(x["h"] for x in last24)
    if hi24 > current_price * 1.01:
        return round(hi24 * 0.9975, 12), "24H_RANGE_HIGH", "Single take-profit set 0.25% below the observed 24h candle high."
    tr = []
    for prev, cur in zip(bars[:-1], bars[1:]):
        tr.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"])))
    atr = sum(tr[-14:]) / min(14, len(tr)) if tr else 0
    range24 = hi24 - min(x["l"] for x in last24)
    projection = max(1.5 * atr, 0.5 * range24, current_price * 0.03)
    target = current_price + projection
    if math.isfinite(target) and target > current_price:
        return round(target, 12), "ATR_RANGE_PROJECTION", "No overhead swing high was found; single take-profit is a 24h volatility projection from 1h ATR/range, not a resistance level or calibrated probability."
    return None, "UNVERIFIED", "Could not derive a positive target from the candle history."


def candle_stats(candles, current_price, as_of_timestamp=None):
    bars, _ = _hourly_bars(candles, as_of_timestamp)
    if len(bars) < 8:
        return {}
    closes = [float(x["c"]) for x in bars]
    volumes = [float(x["qv"]) for x in bars]
    last = bars[-1]
    def ret_back(n):
        if len(closes) <= n or closes[-n-1] <= 0:
            return None
        return (current_price / closes[-n-1] - 1.0) * 100.0
    out = {"1h": ret_back(1), "4h": ret_back(4), "7d": ret_back(min(167, len(closes)-1))}
    one_hour_ratio = None
    if len(volumes) >= 25 and statistics_median(volumes[:-1]) > 0:
        one_hour_ratio = volumes[-1] / statistics_median(volumes[:-1])
    four_hour_ratio = None
    if len(volumes) >= 28:
        recent4 = sum(volumes[-4:])
        hist4 = [sum(volumes[i:i+4]) for i in range(0, len(volumes)-8, 4)]
        med4 = statistics_median(hist4) if hist4 else 0
        if med4 > 0:
            four_hour_ratio = recent4 / med4
    recent24 = bars[-24:]
    high24 = max(x["h"] for x in recent24)
    out["distance_to_24h_high_pct"] = max(0.0, (high24 / current_price - 1.0) * 100.0) if current_price > 0 else None
    lows = [x["l"] for x in bars[-6:]]
    out["higher_lows"] = len(lows) >= 4 and lows[-1] > lows[-3] and lows[-2] > lows[-4]
    out["fresh_impulse"] = bool(closes[-1] > closes[-2] and volumes[-1] >= (statistics_median(volumes[-25:-1]) if len(volumes) >= 25 else 0))
    out["volume_ratios"] = {"15m": None, "1h": one_hour_ratio, "4h": four_hour_ratio}
    out["candles"] = bars
    return out


def statistics_median(values):
    vals = sorted(float(x) for x in values if num(x) is not None)
    if not vals:
        return 0.0
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def project_24h_upside(candles, current_price, stats=None, as_of_timestamp=None):
    """Candle-backed upside scenario; explicitly not a calibrated expected return."""
    bars, _ = _hourly_bars(candles, as_of_timestamp)
    if current_price is None or current_price <= 0 or len(bars) < 20:
        return None, "INSUFFICIENT_1H_OHLCV"
    true_ranges = []
    for previous, current in zip(bars[:-1], bars[1:]):
        true_ranges.append(max(
            current["h"] - current["l"],
            abs(current["h"] - previous["c"]),
            abs(current["l"] - previous["c"]),
        ))
    atr = statistics_median(true_ranges[-14:]) if true_ranges else 0.0
    atr_pct = atr / current_price * 100.0
    recent = bars[-24:]
    range_pct = (max(x["h"] for x in recent) - min(x["l"] for x in recent)) / current_price * 100.0
    stats = stats or {}
    momentum_1h = num(stats.get("1h")) or 0.0
    momentum_4h = num(stats.get("4h")) or 0.0
    ratios = stats.get("volume_ratios") or {}
    ratio_1h = num(ratios.get("1h")) or 0.0
    trend_bonus = min(0.60, max(0.0, momentum_1h) / 100.0 * 0.45
                      + max(0.0, momentum_4h) / 100.0 * 0.20)
    volume_bonus = min(0.25, math.log1p(max(0.0, ratio_1h)) / math.log1p(5.0) * 0.25)
    structure_bonus = (0.10 if stats.get("higher_lows") is True else 0.0) + (
        0.10 if stats.get("fresh_impulse") is True else 0.0
    )
    baseline = max(atr_pct * math.sqrt(24.0), range_pct * 0.50, 3.0)
    projected = min(250.0, max(3.0, baseline * (1.0 + trend_bonus + volume_bonus + structure_bonus)))
    return round(projected, 2), "1H_ATR_RANGE_MOMENTUM_SCENARIO_NOT_CALIBRATED"


def derive_risk_metrics(candles, current_price, as_of_timestamp=None):
    """Derive a volatility-based 24h invalidation level from completed hourly candles."""
    bars, _ = _hourly_bars(candles, as_of_timestamp)
    if current_price is None or current_price <= 0 or len(bars) < 20:
        return {
            "atr_1h_usd": None, "atr_1h_pct": None, "initial_stop_price": None,
            "stop_loss_pct": None, "stop_method": "UNVERIFIED_INSUFFICIENT_HOURLY_CANDLES",
        }
    true_ranges = []
    for previous, current in zip(bars[:-1], bars[1:]):
        true_ranges.append(max(
            current["h"] - current["l"],
            abs(current["h"] - previous["c"]),
            abs(current["l"] - previous["c"]),
        ))
    atr = statistics_median(true_ranges[-14:])
    if atr <= 0:
        return {
            "atr_1h_usd": None, "atr_1h_pct": None, "initial_stop_price": None,
            "stop_loss_pct": None, "stop_method": "UNVERIFIED_ZERO_HOURLY_ATR",
        }
    atr_pct = atr / current_price * 100.0
    # 1.8x ATR, with a 1% minimum noise buffer and 25% maximum reference risk.
    risk_distance = min(current_price * 0.25, max(current_price * 0.01, 1.8 * atr))
    stop = current_price - risk_distance
    return {
        "atr_1h_usd": round(atr, 12),
        "atr_1h_pct": round(atr_pct, 4),
        "initial_stop_price": round(stop, 12),
        "stop_loss_pct": round((risk_distance / current_price) * 100.0, 4),
        "stop_method": "1.8X_1H_ATR_MIN_1PCT_MAX_25PCT_REFERENCE_RISK",
    }


def validate_technical_tp(candles, current_price, target, target_source, price_timestamp,
                          candle_timestamp, chain, pool_address, token_address,
                          token_orientation_verified, liquidity_usd, atr_1h_pct):
    """Validate an observed resistance against its exact, fresh, token-oriented candle set."""
    price_dt = parse_dt(price_timestamp)
    candle_dt = parse_dt(candle_timestamp)
    evidence_as_of = min(price_dt, candle_dt) if price_dt and candle_dt else None
    bars, invalid_count = _hourly_bars(
        candles, evidence_as_of.isoformat() if evidence_as_of else None)
    clean_bars, extreme_wicks = _resistance_bars(bars)
    blockers = []
    observed_sources = {
        "7D_SWING_RESISTANCE", "24H_RANGE_HIGH",
        "OBSERVED_SWING_RESISTANCE", "OBSERVED_RECENT_RANGE_HIGH",
    }
    if target_source not in observed_sources:
        blockers.append("target is not an observed resistance level")
    if len(bars) < 20 or len(clean_bars) < 20:
        blockers.append("fewer than 20 valid non-anomalous completed hourly candles")
    if invalid_count > max(1, int(len(candles or []) * 0.05)):
        blockers.append("candle history contains excessive malformed or duplicate bars")
    if price_dt is None or candle_dt is None or abs((candle_dt - price_dt).total_seconds()) > 900:
        blockers.append("price and candle retrieval timestamps differ by more than 15 minutes")
    if not token_orientation_verified:
        blockers.append("candle token orientation is not verified")
    if not chain or not pool_address or not token_address:
        blockers.append("exact chain, pool, or token identity is missing")

    if len(bars) >= 20 and price_dt is not None:
        recent = bars[-20:]
        timestamps = [row["t"] for row in recent]
        if timestamps[-1] - timestamps[0] < 19 * 3600 or any(
                later - earlier > 2 * 3600 for earlier, later in zip(timestamps, timestamps[1:])):
            blockers.append("recent hourly candle history is incomplete or too widely gapped")
        latest_age = price_dt.timestamp() - timestamps[-1]
        if latest_age < -3600 or latest_age > 7200:
            blockers.append("latest completed candle is not time-aligned with the reference price")

    liquidity = num(liquidity_usd)
    if liquidity is None or liquidity < 100_000:
        blockers.append("verified pool liquidity is below the existing $100,000 research floor")
    atr_pct = num(atr_1h_pct)
    if atr_pct is None or atr_pct <= 0:
        blockers.append("hourly volatility evidence is missing or invalid")
    target_value = num(target)
    price_value = num(current_price)
    upside = canonical_tp_upside(price_value, target_value)
    if upside is None or upside <= 0:
        blockers.append("technical target is not above the current reference price")
    elif atr_pct is not None and upside > max(40.0, 3.0 * atr_pct):
        blockers.append("technical target is more than three hourly ATRs from entry")
    if clean_bars and target_value is not None:
        resistance_targets = [row["h"] * 0.9975 for row in clean_bars]
        if min(abs(value - target_value) / max(target_value, 1e-12)
               for value in resistance_targets) > 0.01:
            blockers.append("target does not match an observed non-anomalous candle resistance")
    else:
        blockers.append("no non-anomalous candle resistance supports the target")
    return {
        "status": "PASS" if not blockers else "NOT_QUALIFIED",
        "blockers": blockers,
        "target_price_source": target_source,
        "target_price": target_value,
        "current_price": price_value,
        "technical_tp_upside_pct": round(upside, 8) if upside is not None else None,
        "atr_1h_pct": atr_pct,
        "maximum_tp_distance_pct": max(40.0, 3.0 * atr_pct) if atr_pct is not None and atr_pct > 0 else None,
        "liquidity_usd": liquidity,
        "valid_candle_count": len(bars),
        "resistance_candle_count": len(clean_bars),
        "excluded_extreme_upper_wicks": extreme_wicks,
        "invalid_candle_count": invalid_count,
        "price_timestamp_utc": price_timestamp,
        "candle_retrieved_utc": candle_timestamp,
        "chain": str(chain or "").lower(),
        "pool_address": pool_address,
        "token_address": token_address,
        "token_orientation_verified": bool(token_orientation_verified),
    }


def parse_hyperliquid_meta_and_asset_ctxs(payload):
    """Normalize Hyperliquid's public perp universe + aligned asset contexts."""
    if not isinstance(payload, list) or len(payload) < 2:
        return {}
    meta, contexts = payload[0], payload[1]
    universe = meta.get("universe") if isinstance(meta, dict) else None
    if not isinstance(universe, list) or not isinstance(contexts, list):
        return {}
    output = {}
    for market, context in zip(universe, contexts):
        if not isinstance(market, dict) or not isinstance(context, dict):
            continue
        symbol = str(market.get("name") or "").upper().strip()
        mark = num(context.get("markPx") or context.get("midPx"))
        oi = num(context.get("openInterest"))
        funding_hourly = num(context.get("funding"))
        previous_day = num(context.get("prevDayPx"))
        day_volume = num(context.get("dayNtlVlm"))
        if not symbol or mark is None or mark <= 0:
            continue
        output[symbol] = {
            "symbol": symbol,
            "mark_price_usd": mark,
            "open_interest_contracts": oi,
            "open_interest_usd": oi * mark if oi is not None else None,
            "funding_hourly_pct": funding_hourly * 100.0 if funding_hourly is not None else None,
            # Hyperliquid pays funding hourly; convert eight consecutive hourly
            # rates to the 8h-equivalent field used by the forward model.
            "funding_8h_pct": funding_hourly * 8.0 * 100.0 if funding_hourly is not None else None,
            "day_volume_usd": day_volume,
            "day_change_pct": (mark / previous_day - 1.0) * 100.0 if previous_day and previous_day > 0 else None,
        }
    return output


def fetch_hyperliquid_market_context(candidates, memory_dir=None, timeout=8, payload_override=None):
    """Fetch one public market-wide perp snapshot; map only symbol+price-confirmed spot assets."""
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if payload_override is None:
        request = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "metaAndAssetCtxs", "dex": ""}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "crypto-prebreakout-hyperliquid-context/4.28"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            return {"status": "UNAVAILABLE", "error": "%s: %s" % (type(exc).__name__, exc),
                    "matched_candidates": 0, "markets_received": 0}
    else:
        payload = payload_override

    markets = parse_hyperliquid_meta_and_asset_ctxs(payload)
    if not markets:
        return {"status": "EMPTY_OR_UNPARSEABLE", "matched_candidates": 0, "markets_received": 0}

    memory_path = None
    previous_state = {}
    if memory_dir:
        memory_path = Path(memory_dir) / "hyperliquid-market-state.json"
        try:
            previous_state = json.loads(memory_path.read_text(encoding="utf-8")) if memory_path.exists() else {}
        except Exception:
            previous_state = {}
    old_markets = previous_state.get("markets") if isinstance(previous_state.get("markets"), dict) else {}
    snapshots = {}
    for symbol, market in markets.items():
        old = old_markets.get(symbol) if isinstance(old_markets.get(symbol), dict) else {}
        old_ts = parse_dt(old.get("timestamp_utc"))
        old_oi = num(old.get("open_interest_usd"))
        new_oi = num(market.get("open_interest_usd"))
        elapsed = ((now_dt - old_ts).total_seconds() / 3600.0) if old_ts else None
        change_4h = None
        change_1h = None
        if old_oi and new_oi and old_oi > 0 and elapsed is not None and 0.25 <= elapsed <= 6.0:
            raw_change = (new_oi / old_oi - 1.0) * 100.0
            change_4h = raw_change * min(4.0 / elapsed, 4.0)
            change_1h = raw_change / elapsed
        snapshots[symbol] = {
            **market,
            "timestamp_utc": now_iso,
            "oi_change_4h_pct": round(change_4h, 4) if change_4h is not None else None,
            "oi_velocity_1h_pct": round(change_1h, 4) if change_1h is not None else None,
            "oi_observation_interval_hours": round(elapsed, 3) if elapsed is not None else None,
        }
    if memory_path:
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text(json.dumps({
            "schema": "crypto-prebreakout.hyperliquid-market-state.v1",
            "updated_utc": now_iso,
            "markets": snapshots,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    matched = 0
    mismatched = 0
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        symbol = str(candidate.get("symbol") or "").upper().strip()
        market = snapshots.get(symbol)
        spot_price = num(candidate.get("price"))
        if not market or spot_price is None or spot_price <= 0:
            continue
        mark_price = num(market.get("mark_price_usd"))
        if mark_price is None or mark_price <= 0:
            continue
        price_difference = abs(spot_price / mark_price - 1.0) * 100.0
        candidate["hyperliquid_context_available"] = True
        candidate["hyperliquid_mark_price_usd"] = mark_price
        candidate["hyperliquid_price_difference_pct"] = round(price_difference, 4)
        candidate["hyperliquid_funding_hourly_pct"] = market.get("funding_hourly_pct")
        candidate["hyperliquid_open_interest_usd"] = market.get("open_interest_usd")
        candidate["hyperliquid_day_volume_usd"] = market.get("day_volume_usd")
        if price_difference > 10.0:
            mismatched += 1
            candidate["hyperliquid_match_status"] = "SYMBOL_MATCH_PRICE_DIVERGENCE"
            continue
        matched += 1
        candidate.update({
            "derivatives_checked": True,
            "derivatives_audit_status": "PARTIAL_HYPERLIQUID_PUBLIC_PERP_CONTEXT",
            "derivatives_market_status": "HYPERLIQUID_PERP_PRICE_CONFIRMED",
            "derivatives_timestamp_utc": now_iso,
            "funding_8h_pct": market.get("funding_8h_pct"),
            "oi_change_4h_pct": market.get("oi_change_4h_pct"),
            "oi_velocity_1h_pct": market.get("oi_velocity_1h_pct"),
            "hyperliquid_match_status": "SYMBOL_AND_PRICE_CROSSCHECK_WITHIN_10PCT",
        })
    return {
        "status": "OK",
        "provider": "Hyperliquid public info API — metaAndAssetCtxs",
        "markets_received": len(markets),
        "matched_candidates": matched,
        "symbol_matches_price_divergent": mismatched,
        "funding_interval": "hourly; funding_8h_pct is the sum of eight current-rate equivalents",
        "open_interest_change": "4h-normalized from persisted prior snapshots when the sample interval is 0.25–6h",
        "snapshot_utc": now_iso,
    }


def fetch_btc_macro_context(cex_fx, timeout=8):
    """Get a fresh BTC 1h/4h regime anchor from Binance spot candles."""
    adapter = venues.VENUES.get("binance")
    if adapter is None:
        return {"status": "UNAVAILABLE", "error": "Binance spot adapter unavailable"}
    try:
        bars, url = adapter.candles(cex_fx, "BTCUSDT", "1h", limit=8)
    except Exception as exc:
        return {"status": "UNAVAILABLE", "error": "%s: %s" % (type(exc).__name__, exc)}
    bars = sorted((bar for bar in (bars or []) if num(bar.get("c")) is not None),
                  key=lambda bar: num(bar.get("t")) or 0)
    if len(bars) < 5:
        return {"status": "UNAVAILABLE", "url": url, "bars_received": len(bars)}
    closes = [num(bar.get("c")) for bar in bars]
    if any(value is None or value <= 0 for value in closes[-5:]):
        return {"status": "UNAVAILABLE", "url": url, "bars_received": len(bars)}
    return {
        "status": "OK",
        "source": "Binance public spot BTCUSDT 1h klines",
        "url": url,
        "timestamp_utc": utc_now(),
        "returns_pct": {
            "1h": round((closes[-1] / closes[-2] - 1.0) * 100.0, 4),
            "4h": round((closes[-1] / closes[-5] - 1.0) * 100.0, 4),
        },
    }


def build_market_context(report, candidates, btc_macro=None):
    """Estimate BTC regime and alt breadth from sampled DEX/CEX spot markets."""
    market_rows = []
    seen = set()
    for board in ("universe_rows_v419", "discovery_rows_v421", "top_1h_movers", "top_6h_movers"):
        for row in report.get(board) or []:
            if not isinstance(row, dict):
                continue
            contract = str(row.get("contract_address") or "").lower()
            chain = str(row.get("chain") or "").lower()
            symbol = str(row.get("symbol") or "").upper()
            key = (
                "%s:%s" % (chain, contract) if contract else
                "%s:%s" % (str(row.get("venue") or "").lower(), symbol)
                if row.get("market_type") == "cex_spot" and symbol else
                str(row.get("identity") or "")
            )
            if key and key not in seen:
                seen.add(key)
                market_rows.append(row)
    candidate_rows = [row for row in (candidates or []) if isinstance(row, dict)]
    btc_candidates = [row for row in candidate_rows if str(row.get("symbol") or "").upper() == "BTC"]
    btc = btc_candidates[0] if btc_candidates else None
    btc_returns = (btc.get("returns_pct") or {}) if btc else {}
    macro_returns = (btc_macro or {}).get("returns_pct") if isinstance(btc_macro, dict) else None
    btc_1h = num((macro_returns or {}).get("1h")) if macro_returns else num(btc_returns.get("1h"))
    btc_4h = num((macro_returns or {}).get("4h")) if macro_returns else num(btc_returns.get("4h"))
    stable_symbols = {"USDC", "USDT", "DAI", "BUSD", "FDUSD", "USDE", "USDS", "TUSD", "USDD"}
    alt_changes = []
    for row in market_rows:
        symbol = str(row.get("symbol") or "").upper()
        change = num(row.get("change_1h_pct"))
        if not symbol or symbol in stable_symbols or symbol in ("BTC", "ETH", "WBTC", "WETH") or change is None:
            continue
        alt_changes.append(change)
    for row in candidate_rows:
        symbol = str(row.get("symbol") or "").upper()
        returns = row.get("returns_pct") if isinstance(row.get("returns_pct"), dict) else {}
        change = num(returns.get("1h"))
        contract = str(row.get("contract_address") or "").lower()
        chain = str(row.get("chain") or "").lower()
        venue = str(row.get("venue") or "").lower()
        key = (
            "%s:%s" % (chain, contract) if row.get("market_type") == "dex_pool" and contract else
            "%s:%s" % (venue, symbol) if row.get("market_type") == "cex_spot" and venue and symbol else
            str(row.get("identity") or "")
        )
        if (symbol and symbol not in stable_symbols and symbol not in ("BTC", "ETH", "WBTC", "WETH")
                and change is not None and key and key not in seen):
            seen.add(key)
            alt_changes.append(change)
    breadth = (sum(1 for value in alt_changes if value > 0) / len(alt_changes) * 100.0) if alt_changes else None
    if btc_4h is not None and btc_4h <= -2.0 and (btc_1h is None or btc_1h < 0):
        regime = "RISK_OFF"
    elif breadth is not None and breadth <= 35 and btc_1h is not None and btc_1h < 0:
        regime = "RISK_OFF"
    elif btc_4h is not None and btc_4h >= 1.0 and breadth is not None and breadth >= 60:
        regime = "RISK_ON"
    else:
        regime = "MIXED_OR_UNVERIFIED"
    return {
        "label": regime,
        "risk_off": regime == "RISK_OFF",
        "btc_returns_pct": {"1h": btc_1h, "4h": btc_4h},
        "btc_source": (btc_macro or {}).get("source") if isinstance(btc_macro, dict) else "candidate 1h/4h candles",
        "btc_macro_snapshot": btc_macro,
        "alt_breadth_pct": round(breadth, 2) if breadth is not None else None,
        "alt_volatility_percentile": None,
        "breadth_sample_count": len(alt_changes),
        "btc_context_available": btc_1h is not None and btc_4h is not None,
        "method": "Sampled full-universe/discovery and evaluated spot candidates cross-sectional 1h breadth, with fresh Binance BTCUSDT 1h/4h anchor; BTC dominance is not attached",
    }

class GeckoThrottle:
    def __init__(self, interval=12.0):
        self.interval = interval
        self.last = 0.0
        self.rate_limited = False
        self.rate_limit_reason = None

    def get(self, url, timeout=10):
        if self.rate_limited:
            raise urllib.error.HTTPError(
                url, 429, f"Too Many Requests (cached rate limit: {self.rate_limit_reason})", {}, None
            )
        if self.last:
            wait = self.interval - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
        self.last = time.monotonic()
        try:
            return get_json(url, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (418, 429):
                self.rate_limited = True
                self.rate_limit_reason = f"HTTP {exc.code} rate limit from provider: {exc}"
            raise


def audit_indexes(report):
    post = report.get("post_scan_audit") or {}
    cvd = {}
    for item in ((post.get("gecko_spot_cvd_audit") or {}).get("audited") or []):
        chain = str(item.get("chain") or "").lower()
        cvd[(chain, address_key(chain, item.get("contract_address")))] = item
    books = {}
    for item in ((post.get("cex_orderbook_audit") or {}).get("book_snapshots") or []):
        books[(str(item.get("identity") or "").lower(), str(item.get("venue") or "").lower())] = item
    security = {}
    for item in ((post.get("goplus_security_audit") or {}).get("audited") or []):
        chain = str(item.get("chain") or "").lower()
        security[(chain, address_key(chain, item.get("contract_address")))] = item
    routes = {}
    for audit_key in ("dex_router_quote_audit", "solana_router_quote_audit"):
        for item in ((post.get(audit_key) or {}).get("quotes") or []):
            chain = str(item.get("chain") or "").lower()
            routes[(chain, address_key(chain, item.get("contract_address")))] = item
    loss_guards = {}
    for item in ((post.get("dex_loss_guard_audit") or {}).get("audited") or []):
        chain = str(item.get("chain") or "").lower()
        loss_guards[(chain, address_key(chain, item.get("contract_address")))] = item
    return cvd, books, security, routes, loss_guards


def gecko_network(chain):
    return GECKO_ALIASES.get(str(chain or "").lower(), str(chain or "").lower())


def _gecko_token_address(payload, relationship_name):
    relationship = ((((payload or {}).get("data") or {}).get("relationships") or {}).get(relationship_name) or {})
    ref = relationship.get("data") if isinstance(relationship, dict) else None
    ref_id = ref.get("id") if isinstance(ref, dict) else None
    if not ref_id:
        return None
    for item in (payload or {}).get("included") or []:
        if isinstance(item, dict) and str(item.get("id") or "") == str(ref_id):
            attrs = item.get("attributes") or {}
            address = attrs.get("address") or attrs.get("token_address")
            if address:
                return str(address)
    text_id = str(ref_id)
    return text_id.split("_", 1)[1] if "_" in text_id else text_id


def resolve_gecko_token_orientation(payload, tracked_token_address, fallback_swapped=False, chain=None):
    base = _gecko_token_address(payload, "base_token")
    quote = _gecko_token_address(payload, "quote_token")
    if same_address(chain, tracked_token_address, base):
        return "base", True, "GECKOTERMINAL_TOKEN_ADDRESS_MATCH"
    if same_address(chain, tracked_token_address, quote):
        return "quote", True, "GECKOTERMINAL_TOKEN_ADDRESS_MATCH"
    return (
        "quote" if fallback_swapped else "base",
        False,
        "DEXPAPRIKA_TOKEN_ORDER_FALLBACK",
    )


def fetch_dex_market(row, throttle, timeout=10, candle_cache=None):
    network = gecko_network(row.get("chain"))
    pool = str(row.get("pool_address") or "").strip()
    tracked_token = str(row.get("contract_address") or "").strip()
    if not network or not pool:
        return None, [], None, "pool or network identity missing"
    if not tracked_token:
        return None, [], None, "tracked token contract/mint missing"
    cache_key = f"{network}:{pool.lower()}:{tracked_token.lower()}"
    if candle_cache is not None and cache_key in candle_cache:
        cached = candle_cache[cache_key]
        return (
            cached.get("price"),
            cached.get("candles", []),
            cached.get("meta"),
            cached.get("error"),
        )
    pool_url = "%s/networks/%s/pools/%s?include=base_token,quote_token" % (
        GECKO, urllib.parse.quote(network, safe=""), urllib.parse.quote(pool, safe=""))
    orientation = "token-address"
    orientation_verified = False
    orientation_method = "GECKOTERMINAL_EXACT_TOKEN_ADDRESS"
    try:
        payload, price_ts = throttle.get(pool_url, timeout)
        matched_orientation, matched, matched_method = resolve_gecko_token_orientation(
            payload, tracked_token,
            bool(row.get("dexpaprika_token_order_swapped_for_tracking")),
            row.get("chain"),
        )
        orientation = matched_orientation if matched else "token-address"
        orientation_verified = bool(matched)
        orientation_method = matched_method if matched else "GECKOTERMINAL_EXACT_TOKEN_ADDRESS"
        attrs = ((payload.get("data") or {}).get("attributes") or {}) if isinstance(payload, dict) else {}
        price_key = (
            "base_token_price_usd" if matched_orientation == "base" else
            "quote_token_price_usd" if matched_orientation == "quote" else None
        )
        price = num(attrs.get(price_key)) if price_key else None
        price_url = pool_url
    except Exception as exc:
        price, price_ts, price_url = None, None, pool_url
        price_error = "%s: %s" % (type(exc).__name__, exc)
        orientation = "token-address"
        orientation_verified = False
        orientation_method = "GECKOTERMINAL_EXACT_TOKEN_ADDRESS"
    else:
        price_error = None
    # The current GeckoTerminal contract explicitly supports a token address
    # here, not just 'base'/'quote'. This prevents candle inversion errors even
    # when pool relationship metadata omits the included token resources.
    params = urllib.parse.urlencode({
        "aggregate": 1, "limit": 168, "currency": "usd", "token": tracked_token,
    })
    candle_url = "%s/networks/%s/pools/%s/ohlcv/hour?%s" % (
        GECKO, urllib.parse.quote(network, safe=""), urllib.parse.quote(pool, safe=""), params)
    try:
        candle_payload, candle_ts = throttle.get(candle_url, timeout)
        raw = (((candle_payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
        candles = [{"t": int(x[0]), "o": float(x[1]), "h": float(x[2]), "l": float(x[3]),
                    "c": float(x[4]), "qv": float(x[5])} for x in raw if isinstance(x, list) and len(x) >= 6]
    except Exception as exc:
        candles, candle_ts = [], None
        candle_error = "%s: %s" % (type(exc).__name__, exc)
    else:
        candle_error = None
    if price is None and candles:
        # Exact-token USD OHLCV close is the correct-token fallback reference.
        price = num(candles[-1].get("c"))
        if price is not None:
            price_ts = candle_ts
            price_url = candle_url
    if candles:
        orientation_verified = True
        orientation_method = (
            "GECKOTERMINAL_TOKEN_ADDRESS_RELATIONSHIP_MATCH"
            if orientation in ("base", "quote")
            else "GECKOTERMINAL_EXACT_TOKEN_ADDRESS_OHLCV"
        )
    res = (price, candles, {
        "price_url": price_url, "price_timestamp_utc": price_ts,
        "candle_url": candle_url, "candle_timestamp_utc": candle_ts,
        "tracked_token_orientation": orientation,
        "tracked_token_orientation_verified": orientation_verified,
        "tracked_token_orientation_method": orientation_method,
        "price_error": price_error, "candle_error": candle_error,
    }, None)
    if candle_cache is not None and (candles or price is not None):
        candle_cache[cache_key] = {
            "price": res[0], "candles": res[1], "meta": res[2], "error": res[3]
        }
    return res


def make_candidate(row, scan_ts, cvd_index, books_index, security_index, routes_index,
                   gecko_throttle, cex_fx, timeout=10, loss_guard_index=None, candle_cache=None):
    kind = row.get("market_type")
    chain = str(row.get("chain") or "").lower() if kind == "dex_pool" else None
    contract = str(row.get("contract_address") or "")
    ident = str(row.get("identity") or "")
    price = num(row.get("price_usd"))
    # Never manufacture freshness from scan_start; missing retrieval timestamps remain missing.
    price_ts = row.get("retrieved_utc")
    price_source = row.get("source") or "scanner snapshot"
    price_url = row.get("source_url") or row.get("pair_url")
    candles, candle_url, candle_ts = [], None, None
    price_refresh_note = None
    token_orientation = None
    token_orientation_verified = False
    token_orientation_method = None
    book = None
    cex_venue_pair_symbol = None
    contract_key = address_key(chain, contract)
    cvd_item = cvd_index.get((chain or "", contract_key)) if kind == "dex_pool" else None
    security_item = security_index.get((chain or "", contract_key)) if kind == "dex_pool" else None
    route_item = routes_index.get((chain or "", contract_key)) if kind == "dex_pool" else None
    loss_guard_item = (loss_guard_index or {}).get((chain or "", contract_key)) if kind == "dex_pool" else None
    loss_guard_status = ((loss_guard_item or {}).get("status") or "BLOCKED_UNVERIFIED_OR_RISK") if kind == "dex_pool" else "NOT_APPLICABLE"
    loss_guard_blockers = ((loss_guard_item or {}).get("blockers") or ["loss-pattern guard evidence missing"]) if kind == "dex_pool" else []
    book_item = None
    if kind == "dex_pool":
        refreshed, candles, meta, error = fetch_dex_market(row, gecko_throttle, timeout, candle_cache=candle_cache)
        if refreshed is not None and refreshed > 0:
            price, price_ts, price_url = refreshed, meta.get("price_timestamp_utc"), meta.get("price_url")
            price_source = (
                "GeckoTerminal exact-token USD OHLCV close"
                if meta.get("tracked_token_orientation_method") == "GECKOTERMINAL_EXACT_TOKEN_ADDRESS_OHLCV"
                else "GeckoTerminal pool snapshot"
            )
        candle_url = (meta or {}).get("candle_url")
        candle_ts = (meta or {}).get("candle_timestamp_utc")
        token_orientation = (meta or {}).get("tracked_token_orientation")
        token_orientation_verified = bool((meta or {}).get("tracked_token_orientation_verified"))
        token_orientation_method = (meta or {}).get("tracked_token_orientation_method")
        if not candles:
            price_refresh_note = error or (meta or {}).get("candle_error") or "No OHLCV candles returned"
    else:
        venue_name = str(row.get("venue") or ((row.get("venues") or [None])[0]) or "").lower()
        adapter = venues.VENUES.get(venue_name)
        if adapter:
            symbol = adapter.pair_to_symbol(str(row.get("asset") or row.get("symbol") or "").upper())
            cex_venue_pair_symbol = symbol
            cex_key = f"cex:{venue_name}:{symbol}"
            if candle_cache is not None and cex_key in candle_cache:
                cached_cex = candle_cache[cex_key]
                candles, candle_url, candle_ts = (
                    cached_cex.get("candles", []),
                    cached_cex.get("url"),
                    cached_cex.get("ts"),
                )
            else:
                try:
                    candles, candle_url = adapter.candles(cex_fx, symbol, "1h", limit=168)
                    candle_ts = utc_now()
                    if candle_cache is not None and candles:
                        candle_cache[cex_key] = {"candles": candles, "url": candle_url, "ts": candle_ts}
                except Exception as exc:
                    candles = []
                    price_refresh_note = "%s: %s" % (type(exc).__name__, exc)
            book_item = books_index.get((ident.lower(), venue_name))
            sim = (book_item or {}).get("simulation") or {}
            if sim.get("mid_price") is not None:
                price = num(sim.get("mid_price"))
                price_ts = (book_item or {}).get("retrieved_utc") or utc_now()
                price_source = "CEX order-book midpoint snapshot"
                price_url = (book_item or {}).get("url")
    # Venue adapters may return None rather than an empty list for missing OHLCV.
    # Normalize here so the report can continue with explicit missing evidence.
    candles = candles or []
    price_dt = parse_dt(price_ts)
    candle_dt = parse_dt(candle_ts)
    evidence_as_of = min(price_dt, candle_dt) if price_dt and candle_dt else None
    evidence_timestamp = evidence_as_of.isoformat() if evidence_as_of else None
    stats = candle_stats(candles, price, evidence_timestamp) if candles and price else {}
    if not stats:
        price_refresh_note = price_refresh_note or "Insufficient 1h candle history"
    target, target_method, target_note = (
        derive_single_target(candles, price, evidence_timestamp) if stats
        else (None, "UNVERIFIED", "Candle history unavailable.")
    )
    projected_24h_upside_pct, upside_projection_method = project_24h_upside(
        candles, price, stats, evidence_timestamp)
    risk_metrics = derive_risk_metrics(candles, price, evidence_timestamp)
    technical_tp_validation = validate_technical_tp(
        candles, price, target, target_method, price_ts, candle_ts, chain,
        row.get("pool_address") or row.get("pair_address"), contract,
        token_orientation_verified, row.get("liquidity_usd"),
        risk_metrics.get("atr_1h_pct"),
    ) if kind == "dex_pool" else {
        "status": "NOT_QUALIFIED",
        "blockers": ["CEX candle targets are research-only and cannot enter the DEX actionable lane"],
        "target_price_source": target_method,
        "target_price": target,
    }
    scan_return24 = num(row.get("change_24h_pct"))
    ret24 = (price / stats["candles"][-25]["c"] - 1) * 100 if stats and len(stats["candles"]) >= 25 and stats["candles"][-25]["c"] > 0 else scan_return24
    ret1 = stats.get("1h") if stats else num(row.get("change_1h_pct"))
    ret4 = stats.get("4h") if stats else None
    ret7 = stats.get("7d") if stats else None
    volume_ratios = stats.get("volume_ratios") if stats else {"15m": None, "1h": None, "4h": None}
    cvd1 = ((cvd_item or {}).get("cvd_by_window") or {}).get("1h") or {}
    cvd6 = ((cvd_item or {}).get("cvd_by_window") or {}).get("6h") or {}
    cvd_checked = bool(cvd_item and cvd_item.get("status") == "SAMPLED_CVD_AVAILABLE" and cvd1.get("valid_trades_sampled", 0) > 0)
    cvd_ts = (cvd_item or {}).get("last_trade_utc")
    buyer_share = None
    cvd_ratio = num(cvd1.get("cvd_ratio_pct"))
    quote_turnover_1h = None
    if cvd_checked:
        total_flow = num(cvd1.get("buy_volume_usd")) + num(cvd1.get("sell_volume_usd"))
        quote_turnover_1h = total_flow
        if total_flow > 0:
            buyer_share = num(cvd1.get("buy_volume_usd")) / total_flow * 100.0
    book_sim = ((book_item or {}).get("simulation") or {})
    depth = book_sim.get("depth_within_2pct_usd") or {}
    spread = num(book_sim.get("spread_bps"))
    bid_depth = num(depth.get("bids"))
    ask_depth = num(depth.get("asks"))
    order_book_verified = bool(book_item and book_item.get("status") == "FULL_ROUND_TRIP_DEPTH_AVAILABLE")
    order_book_ts = (book_item or {}).get("retrieved_utc")
    safety_status = "UNVERIFIED"
    safety_checked = False
    hard_fail = False
    if security_item:
        audit = security_item.get("audit") or {}
        flags = audit.get("risk_flags") or []
        safety_checked = security_item.get("status") in ("FLAGGED_REVIEW", "PROVIDER_CHECK_RETURNED_REVIEW_REQUIRED")
        hard_fail = any(x in flags for x in ("HONEYPOT", "CANNOT_BUY", "CANNOT_SELL_ALL"))
        safety_status = "FAIL" if hard_fail else "UNVERIFIED"
    route_ts = (route_item or {}).get("timestamp_utc")
    firm_or_route_ok = bool(route_item and (
        route_item.get("status") == "BUY_AND_SELL_FIRM_QUOTES_RETURNED" or
        (str(route_item.get("provider", "")).lower().startswith("jupiter") and route_item.get("status") == "BUY_AND_SELL_ROUTES_RETURNED")))
    buy_q = (route_item or {}).get("buy_quote") or {}
    sell_q = (route_item or {}).get("sell_quote") or {}
    input_usd = num((route_item or {}).get("notional_usd"))
    token_out = num(buy_q.get("buy_amount_token_raw") or buy_q.get("output_amount_token_raw"))
    token_in = num(sell_q.get("sell_amount_token_raw") or sell_q.get("input_amount_token_raw"))
    candidate = {
        "asset": row.get("asset") or row.get("symbol"), "symbol": row.get("symbol"),
        "pair": row.get("pair"), "venue": row.get("venue") or row.get("chain"),
        "market_type": kind, "chain": chain, "contract_address": contract,
        "venue_pair_symbol": cex_venue_pair_symbol,
        "pool_address": row.get("pool_address"), "pair_address": row.get("pool_address"),
        "pair_url": row.get("pair_url"),
        "identity": ident, "source": price_source, "source_url": price_url,
        "price": price, "data_timestamp_utc": price_ts,
        "spot_available": kind == "cex_spot", "dex_available": kind == "dex_pool",
        "returns_pct": {"1h": ret1, "4h": ret4, "24h": ret24, "7d": ret7},
        "relative_strength_pct": {}, "volume_ratios": volume_ratios,
        "spot_buyer_share_pct": buyer_share,
        "spot_cvd_change_1h_usd": num(cvd1.get("cvd_usd")) if cvd_checked else None,
        "spot_cvd_ratio_1h_pct": cvd_ratio if cvd_checked else None,
        "spot_quote_volume_1h_usd": quote_turnover_1h,
        "spot_cvd_checked": cvd_checked, "spot_cvd_method": "trade_level" if cvd_checked else "UNVERIFIED",
        "spot_cvd_timestamp_utc": cvd_ts,
        "distance_to_24h_high_pct": stats.get("distance_to_24h_high_pct") if stats else None,
        "higher_lows": stats.get("higher_lows") if stats else None,
        "fresh_impulse": stats.get("fresh_impulse") if stats else None,
        "catalyst_checked": False,
        "quote_turnover_24h_usd": num(row.get("volume_24h_usd")),
        "volume_24h_usd": num(row.get("volume_24h_usd")),
        "liquidity_usd": num(row.get("liquidity_usd")),
        "market_cap_usd": num(row.get("market_cap_usd")),
        "fdv_usd": num(row.get("fdv_usd")),
        "age_hours": num(row.get("age_hours")),
        "txns_24h": num(row.get("txns_24h")),
        "change_5m_pct": num(row.get("change_5m_pct")),
        "change_1h_pct": num(row.get("change_1h_pct")),
        "change_6h_pct": num(row.get("change_6h_pct")),
        "change_24h_pct": num(row.get("change_24h_pct")),
        "projected_24h_upside_pct": projected_24h_upside_pct,
        "upside_projection_method": upside_projection_method,
        "upside_projection_confidence": "CANDLE_BACKED_HEURISTIC_NOT_CALIBRATED" if projected_24h_upside_pct is not None else "MOMENTUM_ONLY_NO_CANDLE_PROJECTION",
        **risk_metrics,
        "tracked_token_orientation": token_orientation,
        "tracked_token_orientation_verified": token_orientation_verified,
        "tracked_token_orientation_method": token_orientation_method,
        "spread_bps": spread, "bid_depth_2pct_usd": bid_depth, "ask_depth_2pct_usd": ask_depth,
        "order_notional_usd": 100.0,
        "order_book_verified": order_book_verified, "order_book_timestamp_utc": order_book_ts,
        "derivatives_checked": False, "derivatives_audit_status": "UNVERIFIED",
        "safety_checked": safety_checked, "safety_status": safety_status, "hard_safety_fail": hard_fail,
        "dex_loss_guard_status": loss_guard_status,
        "dex_loss_guard_blockers": loss_guard_blockers,
        "target_price": target, "target_price_source": target_method, "target_price_note": target_note,
        "technical_tp_validation": technical_tp_validation,
        "dex_buy_quote_available": bool(firm_or_route_ok), "dex_sell_quote_available": bool(firm_or_route_ok),
        "dex_quote_timestamp_utc": route_ts, "dex_buy_quote_input_usd": input_usd,
        "dex_buy_base_output_amount": token_out, "dex_sell_base_input_amount": token_in,
        "dex_execution_quotes": ({
            "provider": (route_item or {}).get("provider"),
            "status": (route_item or {}).get("status"),
            "timestamp_utc": route_ts,
            "input_notional_usd": input_usd,
            "slippage_bps": (route_item or {}).get("slippage_bps"),
            "fee_usd": (route_item or {}).get("fee_usd"),
            "route": (route_item or {}).get("route"),
            "buy_quote": buy_q,
            "sell_quote": sell_q,
        } if route_item else None),
        "dex_loss_guard_evidence": loss_guard_item,
        "field_evidence": {
            "price": {"source": price_source, "url": price_url, "timestamp_utc": price_ts},
            "volume_1h": {"source": "1h OHLCV", "url": candle_url, "timestamp_utc": candle_ts} if candle_url and candle_ts else None,
            "spot_trades": {"source": "GeckoTerminal recent trades sample", "url": "https://api.geckoterminal.com/api/v2/networks/%s/pools/%s/trades" % (chain, row.get("pool_address")), "timestamp_utc": cvd_ts} if cvd_checked else None,
            "spot_cvd": {"source": "GeckoTerminal sampled DEX swaps", "url": "https://api.geckoterminal.com/api/v2/networks/%s/pools/%s/trades" % (chain, row.get("pool_address")), "timestamp_utc": cvd_ts} if cvd_checked else None,
            "order_book": {"source": "CEX order book snapshot", "url": (book_item or {}).get("url"), "timestamp_utc": order_book_ts} if book_item and order_book_ts else None,
            "safety": {"source": "GoPlus", "url": None, "timestamp_utc": security_item.get("retrieved_utc")} if security_item else None,
            "single_take_profit": {"source": "GeckoTerminal / CEX historical 1h OHLCV", "url": candle_url, "timestamp_utc": candle_ts, "method": target_method} if target is not None and candle_ts else None,
            "route_quotes": {"source": (route_item or {}).get("provider"), "url": None, "timestamp_utc": route_ts} if route_item and route_ts else None,
        },
        "early_breakout_score": num(row.get("early_breakout_score")),
        "early_breakout_tier": row.get("early_breakout_tier"),
        "early_breakout_components": row.get("early_breakout_components") or {},
        "early_breakout_risk_flags": row.get("early_breakout_risk_flags") or [],
        "dexpaprika_family": row.get("dexpaprika_family"),
        "dexpaprika_rank": row.get("dexpaprika_rank"),
        "quote_symbol": row.get("quote_symbol"),
        "dexpaprika_token_order_swapped_for_tracking": bool(row.get("dexpaprika_token_order_swapped_for_tracking")),
        "dexpaprika_1h_buy_usd": num(row.get("dexpaprika_1h_buy_usd")),
        "dexpaprika_1h_sell_usd": num(row.get("dexpaprika_1h_sell_usd")),
        "dexpaprika_5m_buy_usd": num(row.get("dexpaprika_5m_buy_usd")),
        "dexpaprika_5m_sell_usd": num(row.get("dexpaprika_5m_sell_usd")),
        "dexpaprika_1h_volume_usd": num(row.get("dexpaprika_1h_volume_usd")),
        "dexpaprika_5m_volume_usd": num(row.get("dexpaprika_5m_volume_usd")),
        "dexpaprika_1h_txns": num(row.get("dexpaprika_1h_txns")),
        "dexpaprika_5m_txns": num(row.get("dexpaprika_5m_txns")),
        "high_risk_scout": bool(row.get("risk_tier") in ("DUST / EXTREME EXECUTION RISK", "ULTRA-THIN MICROCAP", "HIGH-RISK MICROCAP")),
        "source_refs": {"ticker": price_url, "candles": candle_url, "pool": row.get("pair_url")},
        "_scan_row": row, "_candles_count": len(candles), "_candle_url": candle_url,
        "_target_note": target_note, "_price_refresh_note": price_refresh_note,
    }
    if kind == "dex_pool":
        candidate.update({
            "final_price_usd": price,
            "final_price_timestamp_utc": price_ts,
            "final_price_source": price_source,
            "final_price_url": price_url,
            "final_pool_address": row.get("pool_address"),
            "final_token_contract": contract,
            "final_quote_chain": chain,
            "final_quote_token_orientation_verified": token_orientation_verified,
            "final_price_verified": bool(
                price is not None and price > 0 and price_ts and
                row.get("pool_address") and contract and token_orientation_verified
            ),
        })
    return candidate



def enforce_required_trade_fields(top, market_meta, forecast_end=None):
    """Require a fresh DEX price, candle-backed TP and exact pool/pair address.

    Entry price is deliberately a fresh pool spot *reference*, not a promise of
    an executable fill. Router availability remains a separate verification.
    Return (pick, missing_fields); never fabricate a missing value.
    """
    if not isinstance(top, dict) or not top:
        return None, ["ranked DEX pick"]
    meta = market_meta if isinstance(market_meta, dict) else {}
    trade = top.get("trade_plan") if isinstance(top.get("trade_plan"), dict) else {}
    pair_address = str(top.get("pool_address") or meta.get("pool_address") or "").strip()
    entry_price = num(top.get("price"))
    take_profit = num(trade.get("single_take_profit"))
    if take_profit is None:
        take_profit = num(meta.get("target_price"))
    target_method = str(meta.get("target_price_source") or top.get("target_price_source") or "")
    tp_validation = meta.get("technical_tp_validation")
    tp_validation = tp_validation if isinstance(tp_validation, dict) else {}
    missing = []
    if str(top.get("market_type") or "").lower() != "dex_pool":
        missing.append("DEX spot market")
    if not pair_address:
        missing.append("exact pool/pair address")
    if not str(top.get("contract_address") or meta.get("contract_address") or "").strip():
        missing.append("exact token contract/mint")
    if (meta.get("final_price_verified") is not True or
            str(meta.get("final_pool_address") or "") != pair_address or
            not same_address(top.get("chain"), meta.get("final_token_contract"),
                             top.get("contract_address") or meta.get("contract_address")) or
            str(meta.get("final_quote_chain") or "").lower() != str(top.get("chain") or "").lower() or
            meta.get("final_quote_token_orientation_verified") is not True):
        missing.append("independently refreshed exact-pool price, token identity, and orientation")
    if entry_price is None or entry_price <= 0:
        missing.append("positive current spot reference price")
    if top.get("data_fresh") is not True:
        missing.append("fresh price timestamp")
    if top.get("market_verified") is not True:
        missing.append("verified DEX market identity")
    if top.get("entry_decision") == "AVOID — HARD FAIL":
        missing.append("candidate has a hard safety/market failure")
    if int(meta.get("_candles_count") or 0) < 20:
        missing.append("at least 20 hourly candles")
    if take_profit is None or entry_price is None or take_profit <= entry_price:
        missing.append("candle-derived single take-profit above entry")
    tp_upside_pct = canonical_tp_upside(entry_price, take_profit)
    if tp_upside_pct is None or tp_upside_pct < 40.0:
        missing.append("single take-profit upside below mandatory +40.00% threshold")
    if not target_method or target_method == "UNVERIFIED":
        missing.append("verified take-profit derivation method")
    if tp_validation.get("status") != "PASS":
        missing.extend(tp_validation.get("blockers") or ["credible technical TP evidence did not pass"])
    if (num(tp_validation.get("target_price")) is None or take_profit is None or
            abs(num(tp_validation.get("target_price")) - take_profit) > max(abs(take_profit or 0) * 1e-8, 1e-12)):
        missing.append("technical TP evidence does not match the selected target")
    if missing:
        return None, missing

    basis = "FRESH DEX POOL SPOT REFERENCE — NOT A FIRM ROUTER/FILL QUOTE"
    trade["entry_price"] = entry_price
    trade["single_take_profit"] = take_profit
    trade["tp_upside_pct"] = round(tp_upside_pct, 8)
    trade["tp_reference_price"] = entry_price
    trade["pair_address"] = pair_address
    trade["entry_price_basis"] = basis
    trade["target_method"] = target_method
    if forecast_end:
        trade["time_stop_24h"] = forecast_end
    top["trade_plan"] = trade
    top["pair_address"] = pair_address
    top["pool_address"] = pair_address
    top["target_price"] = take_profit
    top["target_price_source"] = target_method
    top["entry_price_basis"] = basis
    return top, []


def enforce_dex_loss_guard(top, verified_plan, missing):
    """Keep a DEX reference plan research-only unless the loss-pattern gate explicitly passes."""
    blockers = list(missing or [])
    if isinstance(top, dict) and str(top.get("market_type") or "").lower() == "dex_pool":
        if top.get("dex_loss_guard_status") != "PASS":
            blockers.append("DEX loss-pattern guard PASS (launch-wallet graph, exact LP position, proxy review, security and size-bound two-way quotes)")
            return None, list(dict.fromkeys(blockers))
    return verified_plan, list(dict.fromkeys(blockers))


def render_markdown(doc, report):
    top = doc.get("top_rocket_pick") or {}
    raw = doc.get("raw_upside_leader") or {}
    raw_rows = report.get("top_24h_gainers") or []
    raw_asset = raw_rows[0] if raw_rows and isinstance(raw_rows[0], dict) else {}
    fmt = lambda x: "UNVERIFIED" if num(x) is None else ("$" + format(float(x), ".12g"))
    scan_start = doc.get("forecast_window_start_utc")
    forecast_end = doc.get("forecast_window_end_utc")
    trade = top.get("trade_plan") or {}
    plan_entry = trade.get("entry_price")
    target = trade.get("single_take_profit")
    candidate = top.get("_market_metadata") or {}
    run_url = doc.get("github_actions_run_url")
    clickable_run_url = ("[%s](%s)" % (run_url, run_url)) if run_url and str(run_url).startswith("http") else (run_url or "UNAVAILABLE")
    if not top:
        return "\n".join([
            "## Forward 24-hour rocket forecast", "",
            "**General version:** %s  |  **Scan finished (UTC):** %s  |  **GitHub Actions run ID:** %s  |  **Run URL:** %s" % (
                doc.get("general_version") or "v4.26.1", doc.get("scan_finished_utc") or "UNVERIFIED",
                doc.get("github_actions_run_id") or "UNAVAILABLE", clickable_run_url),
            "**Status: PICK WITHHELD — required evidence incomplete**", "",
            "**ZERO VERIFIED OPPORTUNITIES** passed every mandatory final gate.",
            "No candidate passed all mandatory evidence checks. This report intentionally does not publish an incomplete pick.",
            "A valid pick requires a fresh spot reference entry price, a candle-derived single take-profit above entry, and an exact DEX pool/pair address.",
            "The trailing raw 24h gainer board remains discovery-only and is not an entry recommendation.", "",
            "## Forward DEX watchlist", "",
            "DEX discoveries remain visible for research, but no candidate is entry-ready until the DEX loss-pattern guard has independently verified token/pool identity, launch-wallet history, exact LP position, proxy permissions, token security and fresh size-bound two-way quotes.", "",
        ])
    lines = ["## Forward 24-hour rocket forecast", "",
             "**General version:** %s  |  **Scan finished (UTC):** %s  |  **GitHub Actions run:** %s  |  **Run URL:** %s" % (
                 doc.get("general_version") or "v4.26.1", doc.get("scan_finished_utc") or "UNVERIFIED",
                 doc.get("github_actions_run_id") or "UNAVAILABLE", clickable_run_url),
             "**Verified actionable opportunities:** %d" % len(doc.get("verified_actionable_opportunities") or []),
             "**Forecast window:** %s → %s  |  **Data snapshot:** %s  |  **Coverage:** %s" % (
                 scan_start, forecast_end, doc.get("as_of_utc"), report.get("coverage", {}).get("status")),
             "**Collection order:** %s" % (" → ".join(report.get("coverage", {}).get("scan_order") or ["DEX sources before CEX, then aggregators"])), "",
             "### Trailing raw 24h champion (discovery only)", "",
             "**%s** (%s) — %s | current scan snapshot %s" % (
                 raw_asset.get("asset") or raw.get("asset") or "No qualifying row",
                 raw_asset.get("symbol") or raw.get("pair") or "—",
                 (("%+.2f%%" % num(raw_asset.get("change_24h_pct"))) if num(raw_asset.get("change_24h_pct")) is not None else "UNVERIFIED") if raw_asset else "—",
                 fmt(raw_asset.get("price_usd")) if raw_asset else "UNVERIFIED"), "",
             "### Forward 24h Top Pick", "",
             "**%s** — %s" % (top.get("asset", "UNAVAILABLE"), top.get("entry_decision", "UNVERIFIED")), "",
             "| Field | Value |", "|---|---|",
             "| Current price (USD) | %s |" % fmt(top.get("price")),
             "| Price source timestamp (UTC) | %s |" % (top.get("data_timestamp_utc") or "UNVERIFIED"),
             "| Pair | %s |" % (top.get("pair") or "UNVERIFIED"),
             "| Venue / chain | %s |" % (top.get("venue") or top.get("chain") or "UNVERIFIED"),
             "| Pair address (copy exactly) | `%s` |" % top.get("pair_address"),
             "| Token contract / mint (copy) | `%s` |" % (top.get("contract_address") or "UNVERIFIED"),
             "| Pair page | %s |" % (top.get("pair_url") or "UNVERIFIED"),
             "| Entry price | %s |" % fmt(plan_entry),
             "| Entry price basis | %s |" % (trade.get("entry_price_basis") or "UNVERIFIED"),
             "| Single take-profit | %s |" % fmt(target),
             "| Canonical TP upside from refreshed reference | %s |" % (
                 "UNVERIFIED" if num(trade.get("tp_upside_pct")) is None else "%+.2f%%" % num(trade.get("tp_upside_pct"))),
             "| Take-profit method | %s |" % (top.get("target_price_source") or "UNVERIFIED"),
             "| Projected 24h upside scenario | %s |" % (
                 "UNVERIFIED" if num(top.get("projected_24h_upside_pct")) is None
                 else "%+.2f%% (uncalibrated)" % num(top.get("projected_24h_upside_pct"))),
             "| Initial ATR stop | %s |" % fmt(top.get("initial_stop_price")),
             "| ATR downside | %s |" % (
                 "UNVERIFIED" if num(top.get("stop_loss_pct")) is None else "%.2f%%" % num(top.get("stop_loss_pct"))),
             "| Scenario reward/risk | %s |" % (
                 "UNVERIFIED" if num(top.get("scenario_reward_risk_ratio")) is None
                 else "%.2fx" % num(top.get("scenario_reward_risk_ratio"))),
             "| Market regime | %s |" % (doc.get("market_regime") or "UNVERIFIED"),
             "| Hyperliquid context | %s |" % (
                 (doc.get("hyperliquid_derivatives_context") or {}).get("status", "UNVERIFIED")),
             "| 24h time stop | %s |" % (trade.get("time_stop_24h") or forecast_end or "UNVERIFIED"),
             "| Forecast score | %s / 100 (heuristic, not probability) |" % top.get("forecast_score"),
             "| Relative confidence | %s |" % top.get("relative_confidence", "UNVERIFIED"),
             "| DEX loss-pattern guard | %s |" % top.get("dex_loss_guard_status", "UNVERIFIED"), "",
             "**Why it ranks:**"]
    for detail in (top.get("factor_details") or []):
        if detail.get("supported"):
            lines.append("- %s: %s" % (detail.get("factor"), detail.get("note")))
    lines += ["", "**Verification gaps:**"]
    for item in (top.get("entry_blockers") or []):
        lines.append("- " + item)
    if not top.get("entry_blockers"):
        lines.append("- None recorded by this heuristic gate; still not a fill guarantee.")
    if top.get("risk_flags"):
        lines += ["", "**Risk flags:**"]
        lines.extend("- " + x for x in top.get("risk_flags", [])[:12])
    lines += ["", "### Forward-24h ranking (sampled accessible universe)", "",
              "| Rank | Asset | Market | 1h move | 24h move | Projected 24h upside | ATR downside | Scenario R:R | Status | Pair address | Token contract |",
              "|---:|---|---|---:|---:|---:|---:|---:|---|---|---|"]
    for item in doc.get("candidates", [])[:25]:
        md = next((m for m in report.get("candidates", []) if m.get("identity") == item.get("identity")), {})
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | `%s` | `%s` |" % (
            item.get("forecast_rank"), item.get("asset"), item.get("market_type"),
            ("n/a" if num((item.get("returns_pct") or {}).get("1h")) is None else "%+.2f%%" % num((item.get("returns_pct") or {}).get("1h"))),
            ("n/a" if num((item.get("returns_pct") or {}).get("24h")) is None else "%+.2f%%" % num((item.get("returns_pct") or {}).get("24h"))),
            ("n/a" if num(item.get("projected_24h_upside_pct")) is None else "%+.2f%%" % num(item.get("projected_24h_upside_pct"))),
            ("n/a" if num(item.get("stop_loss_pct")) is None else "%.2f%%" % num(item.get("stop_loss_pct"))),
            ("n/a" if num(item.get("scenario_reward_risk_ratio")) is None else "%.2fx" % num(item.get("scenario_reward_risk_ratio"))),
            item.get("entry_decision"), md.get("pool_address") or "N/A", md.get("contract_address") or "—"))
    lines += ["", "**Important:** the forecast is a ranking heuristic, not a calibrated probability or a guarantee that this asset will be the actual best gainer. The trailing raw 24h champion and forward forecast are separate. DEX trade CVD remains capped to one pool/300 sampled trades; missing CEX CVD, derivatives and full safety evidence remain unverified.", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="rocket-run/rocket-hunt.json")
    parser.add_argument("--markdown", default="rocket-run/rocket-hunt.md")
    parser.add_argument("--out", default="rocket-run/forward-24h-rocket.json")
    parser.add_argument("--dex-limit", type=int, default=140)
    parser.add_argument("--cex-limit", type=int, default=25)
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--gecko-interval", type=float, default=12.0)
    args = parser.parse_args(argv)
    if args.dex_limit < 0 or args.cex_limit < 0 or args.timeout <= 0 or args.gecko_interval < 0:
        parser.error("limits must be non-negative and timeout positive")
    try:
        report_path = Path(args.report)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        scan_start = report.get("scan_start_utc") or report.get("created_utc")
        if parse_dt(scan_start) is None:
            raise ValueError("scanner output is missing a valid scan_start_utc")
        cvd_idx, books_idx, security_idx, routes_idx, loss_guard_idx = audit_indexes(report)
        selected = pick_candidates(report, args.dex_limit, args.cex_limit)
        cache_path = report_path.parent / ".forward-candles-cache.json"
        candle_cache = {}
        if cache_path.is_file():
            try:
                candle_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                candle_cache = {}
        throttle = GeckoThrottle(args.gecko_interval)
        cex_fx = venues.Fetcher(timeout=args.timeout, retries=2, pause=0)
        candidates = [make_candidate(row, scan_start, cvd_idx, books_idx, security_idx, routes_idx,
                                     throttle, cex_fx, args.timeout, loss_guard_index=loss_guard_idx,
                                     candle_cache=candle_cache) for row in selected]
        if candle_cache:
            try:
                cache_path.write_text(json.dumps(candle_cache, separators=(",", ":")), encoding="utf-8")
            except Exception:
                pass
        # The forward winner must have a timestamped current price and a candle-backed
        # single target. Keep non-qualifying discoveries in the raw scan boards, but
        # do not let a no-candle mark win a forward trade-plan ranking.
        # v4.25: rank every identity-bearing DEX-pool discovery, including young
        # pools and rows with missing OHLCV/TP. Missing evidence is a visible
        # confidence/risk penalty, never a reason to erase the candidate.
        forecast_candidates = [
            c for c in candidates
            if (
                c.get("market_type") == "dex_pool"
                and c.get("pool_address") and c.get("contract_address")
            ) or (
                c.get("market_type") == "cex_spot"
                and (c.get("symbol") or c.get("asset"))
                and (c.get("venue") or c.get("source"))
            )
        ]
        if not forecast_candidates:
            # Keep one DEX Top Rocket visible even when OHLCV is unavailable, but
            # do not invent a target or executable entry. The three-field plan
            # remains explicit and uses UNVERIFIED for the unsupported values.
            now = utc_now()
            scan_start_dt = parse_dt(scan_start)
            forecast_end = (scan_start_dt + timedelta(hours=24)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            raw_rows = report.get("top_24h_gainers") or []
            raw_top = raw_rows[0] if raw_rows else None
            # No incomplete Top Pick: missing required values are not replaced
            # with placeholders or estimates unsupported by the selected candles.
            top_pick = None
            no_pick = {"schema": "crypto-prebreakout.forward-24h-rocket.v1", "status": "NO_QUALIFYING_PICK_REQUIRED_FIELDS_NOT_VERIFIED",
                       "scan_start_utc": scan_start, "forecast_window_start_utc": scan_start,
                       "forecast_window_end_utc": forecast_end, "as_of_utc": now,
                       "universe": "SAMPLED ACCESSIBLE DEX DISCOVERY; 24 configured networks attempted, seven CEX boards run separately",
                       "reason": "No DEX candidate had all required fields: exact pair address, fresh spot reference entry price, at least 20 hourly candles and one candle-derived single take-profit above entry. No incomplete Top Pick was published.",
                       "required_fields": ["entry_price", "single_take_profit", "pair_address", "dex_loss_guard_status=PASS"],
                       "general_version": "v4.26.1", "scan_finished_utc": utc_now(),
                       "github_actions_run_id": os.environ.get("GITHUB_RUN_ID") or None,
                       "github_actions_run_url": (
                           "https://github.com/%s/actions/runs/%s" % (os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID"))
                           if os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_RUN_ID") else None
                       ),
                       "verified_actionable_opportunities": [], "best_immediate_entry": None,
                       "raw_trailing_24h_champion": raw_top, "top_rocket_pick": None,
                       "data_collection": {"selected_dex_candidates": sum(1 for x in candidates if x.get("market_type") == "dex_pool"),
                                           "selected_cex_candidates": sum(1 for x in candidates if x.get("market_type") == "cex_spot"),
                                           "forecast_eligible_candidates": 0,
                                           "ohlcv_candidates": sum(1 for x in candidates if x.get("_candles_count", 0) >= 20)}}
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(no_pick, indent=2, sort_keys=True), encoding="utf-8")
            raw_change = num((raw_top or {}).get("change_24h_pct"))
            raw_price = num((raw_top or {}).get("price_usd"))
            lines = ["## Forward 24-hour rocket forecast", "",
                     "**Status:** %s" % ("TOP ROCKET — EXECUTION UNVERIFIED" if top_pick else "NO DEX POOL WITH COPYABLE IDENTITY"), "",
                     "**Forecast window:** %s → %s | **Data snapshot:** %s" % (scan_start, forecast_end, now), "",
                     "No DEX candidate met every requirement for a fully source-backed forward trade plan: a copyable pool address, token contract, fresh price, sufficient hourly candle history and one candle-derived take-profit above current price.", "",
                     "No entry or target was invented. The raw trailing 24h champion is shown separately and is not the forward forecast.", "",
                     "**Trailing raw 24h champion:** %s (%s), observed move %s, price %s" % (
                         (raw_top or {}).get("asset", "None"), (raw_top or {}).get("symbol", "—"),
                         ("%+.2f%%" % raw_change) if raw_change is not None else "UNVERIFIED",
                         ("$" + format(raw_price, ".12g")) if raw_price is not None else "UNVERIFIED")]
            lines += ["", "### Forward 24h Top Pick", "",
                      "**NO QUALIFYING PICK — no complete three-field trade plan was supported by source data.**", "",
                      "Required fields: Entry price, Single take-profit, Pair address. The scanner will not invent any of them.",
                      "", "CEX discovery remains in the broad mover report. Any published Top Pick must be DEX-only with an exact on-chain pair address."]
            with Path(args.markdown).open("a", encoding="utf-8") as fh:
                fh.write("\n\n" + "\n".join(lines) + "\n")
            print("Forward 24h Top Rocket: %s — no source-backed TP was available." % (top_pick.get("asset") if top_pick else "NO DEX PICK"), flush=True)
            print("Forward candidates: discovered DEX=%d CEX=%d; OHLCV=%d; forecast-eligible=0" % (
                no_pick["data_collection"]["selected_dex_candidates"], no_pick["data_collection"]["selected_cex_candidates"],
                no_pick["data_collection"]["ohlcv_candidates"]), flush=True)
            return 0
        snapshot = utc_now()
        memory_dir = Path(os.environ.get("SCANNER_MEMORY_DIR") or (Path("/tmp") / "scanner-memory"))
        hyperliquid_coverage = fetch_hyperliquid_market_context(
            forecast_candidates, memory_dir=memory_dir, timeout=args.timeout
        )
        btc_macro_context = fetch_btc_macro_context(cex_fx, timeout=args.timeout)
        market_context = build_market_context(report, forecast_candidates, btc_macro=btc_macro_context)
        forecast_input = {"as_of_utc": snapshot, "forecast_window_start_utc": scan_start,
                          "forecast_horizon_hours": 24, "universe": "SAMPLED ACCESSIBLE WEB3: GeckoTerminal/DexScreener DEX + seven CEX spot adapters + aggregators + Hyperliquid derivatives context",
                          "spot_only": True, "require_field_provenance": True,
                          "require_chase_diagnostics": False, "candidates": forecast_candidates,
                          "market_context": market_context}
        doc = next_day_forecast.rank_document(forecast_input, max_age_minutes=45)
        doc["hyperliquid_derivatives_context"] = hyperliquid_coverage
        meta = {str(x.get("identity")): x for x in candidates}
        # The breakout score is a separate, explainable short-window signal.
        # It is blended with the existing market/quality score, not converted
        # into a probability. Strong early momentum therefore influences rank
        # without concealing missing safety or execution evidence.
        for ranked in doc.get("candidates", []):
            item_meta = meta.get(str(ranked.get("identity")), {})
            early = num(item_meta.get("early_breakout_score"))
            base_score = num(ranked.get("forecast_score")) or 0.0
            ranked["early_breakout_score"] = early
            ranked["early_breakout_tier"] = item_meta.get("early_breakout_tier")
            ranked["early_breakout_components"] = item_meta.get("early_breakout_components") or {}
            ranked["early_breakout_risk_flags"] = item_meta.get("early_breakout_risk_flags") or []
            ranked["dexpaprika_family"] = item_meta.get("dexpaprika_family")
            ranked["dexpaprika_rank"] = item_meta.get("dexpaprika_rank")
            ranked["dex_loss_guard_status"] = item_meta.get("dex_loss_guard_status") or "BLOCKED_UNVERIFIED_OR_RISK"
            ranked["dex_loss_guard_blockers"] = item_meta.get("dex_loss_guard_blockers") or ["loss-pattern guard evidence missing"]
            if ranked.get("market_type") == "dex_pool" and ranked["dex_loss_guard_status"] != "PASS":
                guard_blockers = ranked.get("entry_blockers") or []
                guard_blockers = list(guard_blockers) + list(ranked["dex_loss_guard_blockers"])
                ranked["entry_blockers"] = list(dict.fromkeys(guard_blockers))
                ranked["risk_flags"] = list(dict.fromkeys(list(ranked.get("risk_flags") or []) + ["DEX_LOSS_PATTERN_GUARD_NOT_PASSED"]))
                ranked["entry_decision"] = "TOP ROCKET — EXECUTION UNVERIFIED"
            ranked["projected_24h_upside_pct"] = item_meta.get("projected_24h_upside_pct")
            ranked["upside_projection_method"] = item_meta.get("upside_projection_method")
            ranked["upside_projection_confidence"] = item_meta.get("upside_projection_confidence")
            ranked["tracked_token_orientation"] = item_meta.get("tracked_token_orientation")
            ranked["tracked_token_orientation_verified"] = item_meta.get("tracked_token_orientation_verified")
            ranked["change_5m_pct"] = item_meta.get("change_5m_pct")
            ranked["change_1h_pct"] = item_meta.get("change_1h_pct")
            ranked["change_6h_pct"] = item_meta.get("change_6h_pct")
            ranked["change_24h_pct"] = item_meta.get("change_24h_pct")
            ranked["liquidity_usd"] = item_meta.get("liquidity_usd")
            ranked["volume_24h_usd"] = item_meta.get("volume_24h_usd")
            ranked["age_hours"] = item_meta.get("age_hours")
            for risk_field in ("atr_1h_usd", "atr_1h_pct", "initial_stop_price",
                               "stop_loss_pct", "stop_method", "scenario_reward_risk_ratio",
                               "scenario_reward_risk_penalty", "hyperliquid_context_available",
                               "hyperliquid_mark_price_usd", "hyperliquid_price_difference_pct",
                               "hyperliquid_funding_hourly_pct", "hyperliquid_open_interest_usd",
                               "hyperliquid_day_volume_usd", "hyperliquid_match_status",
                               "funding_8h_pct", "oi_change_4h_pct", "oi_velocity_1h_pct",
                               "derivatives_checked", "derivatives_audit_status",
                               "derivatives_market_status", "derivatives_timestamp_utc",
                               "breakout_confluence_score", "breakout_engine_scores",
                               "breakout_acceleration_signals", "probability_tp_hit_24h_pct",
                               "expected_net_24h_return_pct", "potential_mfe_24h_pct",
                               "potential_mae_24h_pct", "estimated_hours_to_target",
                               "reversal_risk_score", "realistic_opportunity_rank"):
                if risk_field in item_meta:
                    ranked[risk_field] = item_meta.get(risk_field)
            projected = num(ranked.get("projected_24h_upside_pct"))
            stop_risk_pct = num(ranked.get("stop_loss_pct"))
            scenario_reward_risk = (
                projected / stop_risk_pct
                if projected is not None and projected > 0 and stop_risk_pct is not None and stop_risk_pct > 0
                else None
            )
            ranked["scenario_reward_risk_ratio"] = round(scenario_reward_risk, 3) if scenario_reward_risk is not None else None
            risk_reward_penalty = (
                min(8.0, max(0.0, 1.0 - scenario_reward_risk) * 8.0)
                if scenario_reward_risk is not None and scenario_reward_risk < 1.0 else 0.0
            )
            ranked["scenario_reward_risk_penalty"] = round(risk_reward_penalty, 2)
            projection_score = (
                100.0 * min(1.0, math.log1p(max(0.0, projected)) / math.log1p(250.0))
                if projected is not None and projected > 0 else None
            )
            opp_rank = num(ranked.get("realistic_opportunity_rank"))
            if opp_rank is not None and early is not None:
                ranked["ranking_score_24h"] = round(max(0.0, 0.45 * opp_rank + 0.35 * early + 0.20 * base_score - risk_reward_penalty), 2)
                ranked["ranking_model_24h"] = "45% realistic opportunity (win prob + net return) + 35% breakout confluence + 20% base research - downside penalty"
            elif early is not None and projection_score is not None:
                ranked["ranking_score_24h"] = round(max(0.0, 0.55 * early + 0.20 * base_score + 0.25 * projection_score - risk_reward_penalty), 2)
                ranked["ranking_model_24h"] = "55% early-breakout + 20% base research + 25% candle-backed upside scenario - downside/R:R penalty"
            elif early is not None:
                ranked["ranking_score_24h"] = round(max(0.0, 0.70 * early + 0.30 * base_score - risk_reward_penalty), 2)
                ranked["ranking_model_24h"] = "70% early-breakout + 30% base research - downside/R:R penalty; candle projection unavailable"
            else:
                ranked["ranking_score_24h"] = round(max(0.0, base_score - risk_reward_penalty), 2)
                ranked["ranking_model_24h"] = "base research score - downside/R:R penalty; early-breakout provider signal unavailable"
        doc["candidates"].sort(
            key=lambda row: (num(row.get("ranking_score_24h")) or 0.0,
                             num(row.get("evidence_coverage_pct")) or 0.0),
            reverse=True,
        )
        for rank, ranked in enumerate(doc["candidates"], 1):
            ranked["forecast_rank"] = rank
        if doc.get("candidates"):
            # Preserve the old model's leader for comparison, but make all
            # headline leader fields follow the new blended breakout ranking.
            doc["base_research_leader"] = doc.get("raw_upside_leader")
            doc["raw_upside_leader"] = doc["candidates"][0]
            doc["upside_leader"] = doc["candidates"][0]
            # Top Rocket is the highest-ranked upside research candidate.
            # Execution-qualified entry remains separately available below.
            doc["top_rocket_pick"] = doc["candidates"][0]
            doc["forecast_leader"] = doc["candidates"][0]
            doc["summary"] = "TOP 24H ROCKET LEADER — %s (%s)" % (
                doc["candidates"][0].get("asset"), doc["candidates"][0].get("pair")
            )
            buy_tiers = ("BUY NOW — RESEARCH QUALIFIED", "SPECULATIVE BUY NOW — HIGH RISK")
            doc["best_immediate_entry"] = next(
                (row for row in doc["candidates"] if row.get("entry_decision") in buy_tiers),
                None,
            )
            doc["top_speculative_upside_leader"] = next(
                (row for row in doc["candidates"] if row.get("opportunity_tier") in ("TOP_BREAKOUT_POTENTIAL", "HIGH_UPSIDE_EARLY")),
                doc["candidates"][0],
            )
            doc["ranking_policy"] = (
                "Ranked for relative 24h upside potential using short-window acceleration plus "
                "existing research evidence. This score is a heuristic, not a return forecast or probability."
            )
        for item in doc.get("candidates", []):
            item_meta = meta.get(str(item.get("identity")), {})
            item.update({"pool_address": item_meta.get("pool_address"), "pair_url": item_meta.get("pair_url"),
                         "final_price_usd": item_meta.get("final_price_usd"),
                         "final_price_timestamp_utc": item_meta.get("final_price_timestamp_utc"),
                         "final_price_source": item_meta.get("final_price_source"),
                         "final_price_url": item_meta.get("final_price_url"),
                         "final_pool_address": item_meta.get("final_pool_address"),
                         "final_token_contract": item_meta.get("final_token_contract"),
                         "final_quote_chain": item_meta.get("final_quote_chain"),
                         "final_quote_token_orientation_verified": item_meta.get("final_quote_token_orientation_verified"),
                         "final_price_verified": item_meta.get("final_price_verified"),
                         "dex_execution_quotes": item_meta.get("dex_execution_quotes"),
                         "dex_loss_guard_evidence": item_meta.get("dex_loss_guard_evidence"),
                         "data_timestamp_utc": item_meta.get("data_timestamp_utc"),
                         "target_price": item_meta.get("target_price"),
                         "target_price_source": item_meta.get("target_price_source"),
                         "target_price_note": item_meta.get("target_price_note"),
                         "technical_tp_validation": item_meta.get("technical_tp_validation"),
                         "age_hours": item_meta.get("age_hours"),
                         "liquidity_usd": item_meta.get("liquidity_usd"),
                         "early_breakout_score": item_meta.get("early_breakout_score"),
                         "early_breakout_tier": item_meta.get("early_breakout_tier"),
                         "early_breakout_components": item_meta.get("early_breakout_components") or {},
                         "early_breakout_risk_flags": item_meta.get("early_breakout_risk_flags") or [],
                         "dexpaprika_family": item_meta.get("dexpaprika_family"),
                         "dexpaprika_1h_volume_usd": item_meta.get("dexpaprika_1h_volume_usd"),
                         "dexpaprika_5m_volume_usd": item_meta.get("dexpaprika_5m_volume_usd"),
                         "projected_24h_upside_pct": item_meta.get("projected_24h_upside_pct"),
                         "upside_projection_method": item_meta.get("upside_projection_method"),
                         "upside_projection_confidence": item_meta.get("upside_projection_confidence"),
                         "tracked_token_orientation": item_meta.get("tracked_token_orientation"),
                         "tracked_token_orientation_verified": item_meta.get("tracked_token_orientation_verified"),
                         "dex_loss_guard_status": item_meta.get("dex_loss_guard_status") or "BLOCKED_UNVERIFIED_OR_RISK",
                         "dex_loss_guard_blockers": item_meta.get("dex_loss_guard_blockers") or ["loss-pattern guard evidence missing"]})
        top = doc.get("top_rocket_pick") or {}
        top_meta = meta.get(str(top.get("identity")), {})
        top.update({"pool_address": top_meta.get("pool_address"),
                    "pair_address": top_meta.get("pool_address"), "pair_url": top_meta.get("pair_url"),
                    "final_price_usd": top_meta.get("final_price_usd"),
                    "final_price_timestamp_utc": top_meta.get("final_price_timestamp_utc"),
                    "final_price_source": top_meta.get("final_price_source"),
                    "final_price_url": top_meta.get("final_price_url"),
                    "final_pool_address": top_meta.get("final_pool_address"),
                    "final_token_contract": top_meta.get("final_token_contract"),
                    "final_quote_chain": top_meta.get("final_quote_chain"),
                    "final_quote_token_orientation_verified": top_meta.get("final_quote_token_orientation_verified"),
                    "final_price_verified": top_meta.get("final_price_verified"),
                    "dex_execution_quotes": top_meta.get("dex_execution_quotes"),
                    "dex_loss_guard_evidence": top_meta.get("dex_loss_guard_evidence"),
                    "data_timestamp_utc": top_meta.get("data_timestamp_utc"),
                    "target_price": top_meta.get("target_price"),
                    "target_price_source": top_meta.get("target_price_source"),
                    "target_price_note": top_meta.get("target_price_note"),
                    "technical_tp_validation": top_meta.get("technical_tp_validation"),
                    "projected_24h_upside_pct": top_meta.get("projected_24h_upside_pct"),
                    "upside_projection_method": top_meta.get("upside_projection_method"),
                    "upside_projection_confidence": top_meta.get("upside_projection_confidence"),
                    "tracked_token_orientation": top_meta.get("tracked_token_orientation"),
                    "tracked_token_orientation_verified": top_meta.get("tracked_token_orientation_verified"),
                    "dex_loss_guard_status": top_meta.get("dex_loss_guard_status") or "BLOCKED_UNVERIFIED_OR_RISK",
                    "dex_loss_guard_blockers": top_meta.get("dex_loss_guard_blockers") or ["loss-pattern guard evidence missing"]})
        verified_plan, missing_required = enforce_required_trade_fields(top, top_meta, doc.get("forecast_window_end_utc"))
        verified_plan, missing_required = enforce_dex_loss_guard(top, verified_plan, missing_required)
        # Only the independently checked DEX plan can populate the actionable lane.
        # The model's preliminary CEX/DEX BUY tier remains research metadata.
        doc["best_immediate_entry"] = None
        if verified_plan is not None:
            top.update({"trade_plan": verified_plan.get("trade_plan")})
        doc["forward_research_leader"] = top or None
        doc["top_rocket_pick"] = None
        doc["forecast_leader"] = None
        doc["status"] = "RESEARCH_RANKED_AWAITING_FINAL_QUALIFICATION"
        doc["required_fields"] = ["entry_price", "single_take_profit", "pair_address", "dex_loss_guard_status=PASS"]
        doc["required_field_gaps"] = missing_required
        doc["top_rocket_execution_ready"] = False
        doc["top_opportunities"] = []
        doc["verified_actionable_opportunities"] = []
        doc["discovery_candidates"] = doc.get("candidates", [])
        doc["best_immediate_entry_note"] = (
            "A separately ranked candidate may have a complete source-backed research reference plan; "
            "this does not represent a live execution fill."
        )
        doc["scan_start_utc"] = scan_start
        doc["trailing_raw_24h_champion"] = (report.get("top_24h_gainers") or [None])[0]
        doc["forecast_generation_utc"] = snapshot
        doc["general_version"] = "v4.26.1"
        doc["scan_finished_utc"] = utc_now()
        doc["github_actions_run_id"] = os.environ.get("GITHUB_RUN_ID") or None
        repository = os.environ.get("GITHUB_REPOSITORY")
        run_id = doc["github_actions_run_id"]
        doc["github_actions_run_url"] = (
            "https://github.com/%s/actions/runs/%s" % (repository, run_id)
            if repository and run_id else None
        )
        doc["universe"] = "SAMPLED ACCESSIBLE WEB3/DEFI: multi-chain DEX pool discovery plus configured CEX spot venues; not an exhaustive census"
        doc["data_collection"] = {"selected_dex_candidates": sum(1 for x in candidates if x.get("market_type") == "dex_pool"),
                                  "selected_cex_candidates": sum(1 for x in candidates if x.get("market_type") == "cex_spot"),
                                  "forecast_eligible_candidates": len(forecast_candidates),
                                  "ohlcv_candidates": sum(1 for x in candidates if x.get("_candles_count", 0) >= 20),
                                  "target_method_counts": {method: sum(1 for x in candidates if x.get("target_price_source") == method)
                                                           for method in sorted(set(x.get("target_price_source") for x in candidates))}}
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        md = render_markdown(doc, report)
        existing_md = Path(args.markdown).read_text(encoding="utf-8") if Path(args.markdown).exists() else ""
        prefix = (existing_md + "\n\n") if existing_md else ""
        Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown).write_text(prefix + md + "\n", encoding="utf-8")
        top_asset_safe = str(top.get("asset") or "").encode("ascii", "replace").decode("ascii")
        print("Forward 24h Top Rocket: %s | status=%s | current=%s | single TP=%s | time stop=%s" % (
            top_asset_safe, top.get("entry_decision"), top.get("price"),
            (top.get("trade_plan") or {}).get("single_take_profit"), (top.get("trade_plan") or {}).get("time_stop_24h")), flush=True)
        print("Forward candidates: discovered DEX=%d CEX=%d; forecast-eligible=%d; OHLCV=%d" % (
            doc["data_collection"]["selected_dex_candidates"], doc["data_collection"]["selected_cex_candidates"],
            doc["data_collection"]["forecast_eligible_candidates"], doc["data_collection"]["ohlcv_candidates"]), flush=True)
        return 0
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print("Forward 24h report failed: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
