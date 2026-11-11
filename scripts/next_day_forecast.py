#!/usr/bin/env python3
"""Rank likely next-day upside leaders and evaluate an immediate spot entry.

This is a transparent heuristic ranking, not a calibrated probability model.
It never places orders. Missing inputs reduce evidence coverage and change the action tier; they are never
silently treated as favorable. The system always preserves a ranked Top Rocket.
It does not use reward/risk ratios or an arbitrary minimum target percentage.
"""
import argparse
import json
import math
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

try:
    from scripts.tp_gate import canonical_tp_upside
    from scripts.address_identity import same_address
except ImportError:  # direct ``python scripts/next_day_forecast.py`` execution
    from tp_gate import canonical_tp_upside
    from address_identity import same_address

VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()
PROVENANCE_FIELDS = ("price", "volume_15m", "volume_1h", "spot_trades", "spot_cvd", "derivatives", "order_book", "route_quotes", "safety", "single_take_profit")

WEIGHTS = {
    "momentum_relative_strength": 15.0,
    "volume_acceleration": 20.0,
    "spot_flow": 15.0,
    "structure_timing": 15.0,
    "catalyst": 10.0,
    "liquidity": 15.0,
    "derivatives_context": 5.0,
    "supply_safety": 5.0,
}


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, float(x)))


def positive_scale(value, full_at):
    return clamp(float(value) / full_at) if full_at > 0 else 0.0


def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _add(details, factor, points, weight, supported, note):
    details.append({"factor": factor, "points": round(points, 2), "weight": weight,
                    "supported": bool(supported), "note": note})


def evaluate(candidate, snapshot_utc, max_age_minutes=30, require_field_provenance=False,
             require_chase_diagnostics=False, market_context=None, sector_peer_count=1):
    details = []
    score = 0.0
    supported_weight = 0.0
    returns = candidate.get("returns_pct") or {}
    rs = candidate.get("relative_strength_pct") or {}
    vols = candidate.get("volume_ratios") or {}
    price = _num(candidate.get("price"))
    timestamp = parse_time(candidate.get("data_timestamp_utc"))
    snapshot = parse_time(snapshot_utc)
    age_minutes = ((snapshot - timestamp).total_seconds() / 60.0) if timestamp and snapshot else None
    fresh = age_minutes is not None and -2 <= age_minutes <= max_age_minutes
    market_context = market_context or {}

    # Strict live runs validate the timestamp/source of each independently sampled field.
    evidence_map = candidate.get("field_evidence") or candidate.get("source_refs") or {}
    provenance_problems = []
    provenance_ages = {}
    for field in PROVENANCE_FIELDS:
        evidence = evidence_map.get(field) if isinstance(evidence_map, dict) else None
        if not isinstance(evidence, dict):
            provenance_problems.append("%s: source/timestamp missing" % field)
            continue
        source = evidence.get("source") or evidence.get("venue") or evidence.get("source_ref")
        url = evidence.get("url") or evidence.get("source_url")
        stamp = parse_time(evidence.get("timestamp_utc") or evidence.get("data_timestamp_utc"))
        age = ((snapshot - stamp).total_seconds() / 60.0) if stamp and snapshot else None
        if not source and not url:
            provenance_problems.append("%s: source identity missing" % field)
        if stamp is None:
            provenance_problems.append("%s: valid timestamp missing" % field)
        elif age is None or age < -2 or age > max_age_minutes:
            provenance_problems.append("%s: stale/future field timestamp" % field)
        else:
            provenance_ages[field] = round(age, 1)

    def add(factor, points, weight, supported, note, covered=None):
        nonlocal score, supported_weight
        if supported:
            score += points
        if covered is None:
            covered = weight if supported else 0.0
        supported_weight += clamp(covered, 0.0, weight)
        _add(details, factor, points if supported else 0.0, weight, supported, note)

    # Momentum and cross-asset strength: bounded so an old pump cannot dominate.
    momentum_items = [
        ("1h", returns.get("1h"), 3.0, 3.0),
        ("4h", returns.get("4h"), 3.0, 6.0),
        ("24h", returns.get("24h"), 2.5, 15.0),
        ("7d", returns.get("7d"), 1.5, 30.0),
        ("vs BTC 24h", rs.get("btc_24h"), 2.5, 8.0),
        ("vs sector 24h", rs.get("sector_24h"), 2.5, 8.0),
    ]
    m_points = 0.0
    m_weight = 0.0
    m_notes = []
    for label, value, weight, scale in momentum_items:
        if _num(value) is None:
            continue
        m_weight += weight
        p = weight * positive_scale(value, scale)
        m_points += p
        m_notes.append("%s %.2f%%" % (label, float(value)))
    add("momentum_relative_strength", m_points, WEIGHTS["momentum_relative_strength"],
        m_weight > 0, "; ".join(m_notes) if m_notes else "missing momentum/relative-strength data", covered=m_weight)

    # Volume ratios are matched interval-to-interval against the historical median.
    v_weights = {"15m": 7.0, "1h": 8.0, "4h": 5.0}
    v_points = 0.0
    v_weight = 0.0
    v_notes = []
    for interval, weight in v_weights.items():
        ratio = _num(vols.get(interval))
        if ratio is None or ratio <= 0:
            continue
        v_weight += weight
        points = weight * clamp((math.log(ratio, 2) + 2.0) / 4.0)
        v_points += points
        v_notes.append("%s %.2fx" % (interval, ratio))
    add("volume_acceleration", v_points, WEIGHTS["volume_acceleration"],
        v_weight > 0, "; ".join(v_notes) if v_notes else "missing same-interval volume baselines", covered=v_weight)

    buyer = _num(candidate.get("spot_buyer_share_pct"))
    spot_cvd = _num(candidate.get("spot_cvd_change_1h_usd"))
    spot_cvd_ts = parse_time(candidate.get("spot_cvd_timestamp_utc"))
    spot_cvd_age = ((snapshot - spot_cvd_ts).total_seconds() / 60.0) if snapshot and spot_cvd_ts else None
    spot_cvd_fresh = spot_cvd_age is not None and -2 <= spot_cvd_age <= 15
    spot_cvd_checked = (candidate.get("spot_cvd_checked") is True and
                        str(candidate.get("spot_cvd_method") or "").lower() in ("trade_level", "decoded_swaps") and
                        spot_cvd is not None and spot_cvd_fresh)
    if not spot_cvd_checked:
        spot_cvd = None
    spot_cvd_ratio = _num(candidate.get("spot_cvd_ratio_1h_pct"))
    spot_cvd_turnover = _num(candidate.get("spot_quote_volume_1h_usd"))
    if spot_cvd_ratio is None and spot_cvd is not None and spot_cvd_turnover is not None and spot_cvd_turnover > 0:
        spot_cvd_ratio = spot_cvd / spot_cvd_turnover * 100.0
    if not spot_cvd_checked:
        spot_cvd_ratio = None
    flow_points = 0.0
    flow_weight = 0.0
    flow_notes = []
    if buyer is not None:
        flow_weight += 7.5
        flow_points += 7.5 * clamp((buyer - 40.0) / 25.0)
        flow_notes.append("buyer-initiated notional share %.1f%%" % buyer)
    if spot_cvd_ratio is not None:
        flow_weight += 7.5
        flow_points += 7.5 * clamp((spot_cvd_ratio + 5.0) / 20.0)
        flow_notes.append("1h spot CVD %.2f%% of quote turnover" % spot_cvd_ratio)
    add("spot_flow", flow_points, WEIGHTS["spot_flow"], flow_weight > 0,
        "; ".join(flow_notes) if flow_notes else "missing buyer-side flow and signed spot CVD", covered=flow_weight)

    structure_points = 0.0
    structure_weight = 0.0
    structure_notes = []
    d_high = _num(candidate.get("distance_to_24h_high_pct"))
    if d_high is not None:
        structure_weight += 6.0
        points = 6.0 if d_high <= 2 else 5.0 if d_high <= 5 else 3.0 if d_high <= 10 else 0.0
        structure_points += points
        structure_notes.append("%.2f%% below 24h high" % d_high)
    compression = _num(candidate.get("range_compression_pctile"))
    if compression is not None:
        structure_weight += 5.0
        points = 5.0 if compression <= 20 else 4.0 if compression <= 40 else 2.0 if compression <= 60 else 0.0
        structure_points += points
        structure_notes.append("range-compression percentile %.1f" % compression)
    if isinstance(candidate.get("higher_lows"), bool):
        structure_weight += 2.0
        structure_points += 2.0 if candidate["higher_lows"] else 0.0
        structure_notes.append("higher lows" if candidate["higher_lows"] else "no higher-low structure")
    if isinstance(candidate.get("fresh_impulse"), bool):
        structure_weight += 2.0
        structure_points += 2.0 if candidate["fresh_impulse"] else 0.0
        structure_notes.append("fresh impulse" if candidate["fresh_impulse"] else "no fresh impulse")
    add("structure_timing", structure_points, WEIGHTS["structure_timing"],
        structure_weight > 0, "; ".join(structure_notes) if structure_notes else "missing structure data", covered=structure_weight)

    catalyst_checked = candidate.get("catalyst_checked") is True
    catalyst_quality = _num(candidate.get("catalyst_quality_0_5"))
    catalyst_ok = catalyst_checked and catalyst_quality is not None and 0 <= catalyst_quality <= 5
    catalyst_ts = parse_time(candidate.get("catalyst_timestamp_utc"))
    catalyst_age_hours = ((snapshot - catalyst_ts).total_seconds() / 3600.0) if catalyst_ts and snapshot else None
    catalyst_half_life = _num(candidate.get("catalyst_half_life_hours")) or 24.0
    catalyst_decay = (0.5 ** (max(0.0, catalyst_age_hours) / catalyst_half_life)) if catalyst_age_hours is not None and catalyst_half_life > 0 else 1.0
    catalyst_decay = clamp(catalyst_decay)
    catalyst_adjusted_quality = catalyst_quality * catalyst_decay if catalyst_ok else None
    add("catalyst", 2.0 * catalyst_adjusted_quality if catalyst_ok else 0.0,
        WEIGHTS["catalyst"], catalyst_ok,
        ("verified catalyst %.1f/5; age %.1fh; decay %.2f" % (catalyst_quality, catalyst_age_hours, catalyst_decay))
        if catalyst_ok and catalyst_age_hours is not None else
        ("verified catalyst %.1f/5; timestamp missing, decay not applied" % catalyst_quality if catalyst_ok else "catalyst not source-checked"))

    turnover = _num(candidate.get("quote_turnover_24h_usd"))
    spread = _num(candidate.get("spread_bps"))
    bid_depth = _num(candidate.get("bid_depth_2pct_usd"))
    ask_depth = _num(candidate.get("ask_depth_2pct_usd"))
    order_notional = _num(candidate.get("order_notional_usd"))
    liq_points = 0.0
    liq_weight = 0.0
    liq_notes = []
    if turnover is not None:
        liq_weight += 5.0
        liq_points += 5.0 if turnover >= 2_500_000 else 4.0 if turnover >= 1_000_000 else 2.5 if turnover >= 250_000 else 0.5 if turnover >= 100_000 else 0.0
        liq_notes.append("24h quote turnover $%.0f" % turnover)
    if spread is not None:
        liq_weight += 5.0
        liq_points += 5.0 if spread <= 5 else 4.0 if spread <= 15 else 3.0 if spread <= 30 else 1.5 if spread <= 50 else 0.0
        liq_notes.append("spread %.1f bps" % spread)
    if bid_depth is not None and ask_depth is not None:
        liq_weight += 5.0
        two_sided = min(bid_depth, ask_depth)
        required = max(5_000.0, 20.0 * order_notional) if order_notional is not None else None
        if required is not None:
            liq_points += 5.0 if two_sided >= required else 3.0 if two_sided >= required * 0.5 else 1.0 if two_sided >= required * 0.2 else 0.0
            liq_notes.append("weaker-side 2%% depth $%.0f vs required $%.0f" % (two_sided, required))
        else:
            liq_notes.append("weaker-side 2%% depth $%.0f; intended order notional missing" % two_sided)
    add("liquidity", liq_points, WEIGHTS["liquidity"], liq_weight > 0,
        "; ".join(liq_notes) if liq_notes else "missing venue-specific book/turnover data", covered=liq_weight)

    derivatives_checked = candidate.get("derivatives_checked") is True
    derivatives_audit_status = str(candidate.get("derivatives_audit_status") or "UNVERIFIED").upper()
    derivatives_market_status = str(candidate.get("derivatives_market_status") or "").upper()
    derivatives_ts = parse_time(candidate.get("derivatives_timestamp_utc"))
    derivatives_age_min = ((snapshot - derivatives_ts).total_seconds() / 60.0) if snapshot and derivatives_ts else None
    derivatives_fresh = derivatives_age_min is not None and -5 <= derivatives_age_min <= 120
    derivatives_no_market = derivatives_checked and derivatives_market_status == "NO_MARKET_FOUND" and derivatives_fresh
    oi_change = _num(candidate.get("oi_change_4h_pct"))
    taker_ratio = _num(candidate.get("futures_taker_buy_sell_ratio"))
    funding = _num(candidate.get("funding_8h_pct"))
    deriv_values = [x for x in (oi_change, taker_ratio, funding) if x is not None]
    deriv_points = 0.0
    deriv_ok = derivatives_checked and derivatives_fresh and (bool(deriv_values) or derivatives_no_market)
    if deriv_ok:
        if oi_change is not None:
            deriv_points += 2.0 * clamp((oi_change + 2.0) / 10.0)
        if taker_ratio is not None:
            deriv_points += 2.0 * clamp((taker_ratio - 0.8) / 0.8)
        if funding is not None:
            # Mildly negative to modestly positive is acceptable; penalize crowded positive funding.
            deriv_points += 1.0 if -0.03 <= funding <= 0.03 else 0.4 if funding < 0.08 else 0.0
    add("derivatives_context", deriv_points, WEIGHTS["derivatives_context"], deriv_ok,
        ("no derivatives market found in checked venues" if derivatives_no_market else
         "OI/taker/funding fields checked" if deriv_ok else
         "derivatives positioning coverage incomplete or stale"))

    safety = str(candidate.get("safety_status", "UNVERIFIED")).upper()
    safety_checked = candidate.get("safety_checked") is True
    unlock = _num(candidate.get("unlock_72h_pct_circ"))
    safety_points = 3.0 if safety_checked and safety == "PASS" else 1.0 if safety_checked and safety == "UNVERIFIED" else 0.0
    if unlock is not None:
        safety_points += 2.0 if unlock < 0.25 else 1.0 if unlock < 1.0 else 0.0
    supply_covered = (3.0 if safety_checked else 0.0) + (2.0 if unlock is not None else 0.0)
    add("supply_safety", safety_points, WEIGHTS["supply_safety"], supply_covered > 0,
        "safety %s%s" % (safety, "; next-72h unlock %.3f%% of circulating supply" % unlock if unlock is not None else "; unlock/supply window unverified"), covered=supply_covered)

    penalties = []
    r24 = _num(returns.get("24h"))
    r7d = _num(returns.get("7d"))
    r1 = _num(returns.get("1h"))
    r4 = _num(returns.get("4h"))
    if r24 is not None and r24 > 20:
        penalties.append((min(6.0, (r24 - 20.0) * 0.2), "24h move already >20%"))
    if r7d is not None and r7d > 50:
        penalties.append((min(6.0, (r7d - 50.0) * 0.1), "7d move already >50%"))
    if buyer is not None and buyer < 45:
        penalties.append((min(5.0, (45.0 - buyer) * 0.5), "seller-dominated spot flow"))
    if r1 is not None and r4 is not None and r1 < 0 and r4 < 0:
        penalties.append((3.0, "short-term momentum fading"))
    funding_annualized = funding * 3.0 * 365.0 if funding is not None else None
    if funding is not None and funding > 0.08:
        penalties.append((3.0, "crowded positive funding"))
    oi_velocity = _num(candidate.get("oi_velocity_1h_pct"))
    if oi_velocity is None:
        oi_velocity = oi_change
    if funding_annualized is not None and funding_annualized > 30.0 and oi_velocity is not None and oi_velocity <= 0:
        penalties.append((4.0, "annualized funding >30% while OI velocity is flat/falling"))
    perp_cvd = _num(candidate.get("perp_cvd_change_1h_usd"))
    if oi_change is not None and oi_change > 0 and spot_cvd is not None and spot_cvd <= 0:
        penalties.append((4.0, "OI rising without positive spot CVD — possible leverage-led move"))
    if spot_cvd is not None and perp_cvd is not None and spot_cvd < 0 < perp_cvd:
        penalties.append((3.0, "perpetual buying diverges from negative spot CVD"))
    if unlock is not None and unlock >= 1.0:
        penalties.append((5.0, "material unlock within 72h"))

    # Measured anti-chase diagnostics. Missing values are reported, never imputed.
    ext_vwap = _num(candidate.get("extension_from_vwap_pct"))
    ext_base = _num(candidate.get("distance_above_last_consolidation_pct"))
    last_hour_share = _num(candidate.get("share_of_24h_move_last_hour_pct"))
    wick15 = _num(candidate.get("upper_wick_15m_pct"))
    wick1h = _num(candidate.get("upper_wick_1h_pct"))
    chase_flags = []
    if ext_vwap is not None:
        if ext_vwap > 5:
            penalties.append((min(8.0, (ext_vwap - 5.0) * 0.8), "extended %.1f%% above VWAP" % ext_vwap))
        if ext_vwap > 10:
            chase_flags.append("EXTREME_VWAP_EXTENSION")
    if ext_base is not None:
        if ext_base > 8:
            penalties.append((min(7.0, (ext_base - 8.0) * 0.5), "%.1f%% above last consolidation" % ext_base))
        if ext_base > 15:
            chase_flags.append("EXTREME_DISTANCE_FROM_BASE")
    if last_hour_share is not None and r24 is not None and r24 > 0:
        if last_hour_share > 50:
            penalties.append((min(8.0, (last_hour_share - 50.0) * 0.16), "%.1f%% of 24h move occurred in last hour" % last_hour_share))
        if last_hour_share > 85 and r24 >= 8:
            chase_flags.append("MOVE_CONCENTRATED_IN_LAST_HOUR")
    if wick15 is not None and wick15 > 50:
        penalties.append((min(5.0, (wick15 - 50.0) * 0.1), "15m upper-wick rejection %.1f%% of candle range" % wick15))
        if wick15 > 80:
            chase_flags.append("EXTREME_15M_UPPER_WICK")
    if wick1h is not None and wick1h > 60:
        penalties.append((min(5.0, (wick1h - 60.0) * 0.1), "1h upper-wick rejection %.1f%% of candle range" % wick1h))
        if wick1h > 85:
            chase_flags.append("EXTREME_1H_UPPER_WICK")

    catalyst_z = _num(candidate.get("distance_from_consolidation_z"))
    if catalyst_age_hours is not None and catalyst_age_hours > 48 and catalyst_z is not None and catalyst_z > 1.5:
        penalties.append((4.0, "catalyst is older than 48h and price is >1.5σ above consolidation"))

    btc_returns = market_context.get("btc_returns_pct") if isinstance(market_context.get("btc_returns_pct"), dict) else {}
    btc_1h = _num(btc_returns.get("1h"))
    btc_4h = _num(btc_returns.get("4h"))
    btc_dominance_change = _num(market_context.get("btc_dominance_change_24h_pct"))
    alt_breadth = _num(market_context.get("alt_breadth_pct"))
    alt_volatility_pctile = _num(market_context.get("alt_volatility_percentile"))
    dominance_rotation = (btc_dominance_change is not None and btc_dominance_change >= 0.5 and
                          ((alt_breadth is not None and alt_breadth <= 40) or
                           (alt_volatility_pctile is not None and alt_volatility_pctile <= 25)))
    declared_regime = str(market_context.get("label") or "").upper()
    risk_off = (market_context.get("risk_off") is True or declared_regime in ("DEFENSIVE", "RISK_OFF") or
                (btc_4h is not None and btc_4h <= -2.0 and (btc_1h is None or btc_1h < 0)) or dominance_rotation)
    if risk_off:
        penalties.append((4.0, "BTC/market risk-off regime — stricter long-entry bar applied"))
    if sector_peer_count > 1:
        penalties.append((min(4.0, (sector_peer_count - 1) * 1.5),
                          "sector crowding: %d candidates in same mapped sector; correlated signals are not independent" % sector_peer_count))

    penalty_total = min(35.0, sum(p[0] for p in penalties))
    final_score = round(clamp(score - penalty_total, 0.0, 100.0), 1)
    coverage = round(supported_weight, 1)
    hard_fail = candidate.get("hard_safety_fail") is True or safety == "FAIL"
    stale = not fresh

    # Preserve the highest-upside asset even when it is not yet executable.
    # CEX spot and DEX spot pools are both eligible research markets; futures-only
    # instruments remain visible but can never be labelled an immediate spot buy.
    market_type = str(candidate.get("market_type") or "cex_spot").lower()
    is_dex = market_type in ("dex", "dex_pool", "amm", "clmm")
    spot_verified = candidate.get("spot_available") is True
    dex_verified = (candidate.get("dex_available") is True and
                    bool(candidate.get("chain") or candidate.get("network")) and
                    bool(candidate.get("contract_address") or candidate.get("token_address")) and
                    bool(candidate.get("pool_address") or candidate.get("pair_address")) and
                    candidate.get("final_price_verified") is True and
                    candidate.get("final_quote_token_orientation_verified") is True and
                    str(candidate.get("final_pool_address") or "") == str(candidate.get("pool_address") or candidate.get("pair_address") or "") and
                    same_address(candidate.get("chain") or candidate.get("network"),
                                 candidate.get("final_token_contract"),
                                 candidate.get("contract_address") or candidate.get("token_address")) and
                    str(candidate.get("final_quote_chain") or "").lower() ==
                    str(candidate.get("chain") or candidate.get("network") or "").lower())
    market_verified = spot_verified or dex_verified
    if is_dex and not (candidate.get("chain") or candidate.get("network")):
        market_verified = False
    if is_dex and not (candidate.get("contract_address") or candidate.get("token_address")):
        market_verified = False

    # A CEX book must be explicitly verified or have a complete, fresh two-sided
    # spread/depth snapshot. A DEX pool needs both buy and sell router quotes.
    book_ts = parse_time(candidate.get("order_book_timestamp_utc"))
    book_age = ((snapshot - book_ts).total_seconds() / 60.0) if snapshot and book_ts else None
    book_fresh = book_age is not None and -2 <= book_age <= max_age_minutes
    cex_book_verified = (spot_verified and book_fresh and
                         (candidate.get("order_book_verified") is True or
                          (spread is not None and bid_depth is not None and ask_depth is not None)))
    dex_quote_ts = parse_time(candidate.get("dex_quote_timestamp_utc"))
    dex_quote_age = ((snapshot - dex_quote_ts).total_seconds() / 60.0) if snapshot and dex_quote_ts else None
    # Router quotes are deliberately much stricter than ordinary price/book freshness:
    # both sides must be refreshed within 60 seconds and describe the same net quantity.
    dex_quotes_fresh = dex_quote_age is not None and -0.1 <= dex_quote_age <= 1.0
    dex_buy_notional = _num(candidate.get("dex_buy_quote_input_usd"))
    dex_buy_token_out = _num(candidate.get("dex_buy_base_output_amount"))
    dex_sell_token_in = _num(candidate.get("dex_sell_base_input_amount"))
    dex_route_size_ok = (
        order_notional is not None and dex_buy_notional is not None and
        abs(dex_buy_notional - order_notional) <= max(0.01, order_notional * 0.01) and
        dex_buy_token_out is not None and dex_sell_token_in is not None and dex_buy_token_out > 0 and
        abs(dex_sell_token_in - dex_buy_token_out) <= max(1e-12, dex_buy_token_out * 0.001)
    )
    guard = candidate.get("dex_loss_guard_evidence") if isinstance(candidate.get("dex_loss_guard_evidence"), dict) else {}
    guard_checks = guard.get("checks") if isinstance(guard.get("checks"), dict) else {}
    guard_evidence_pass = (
        guard.get("status") == "PASS" and all(guard_checks.get(key) is True for key in (
            "exact_token_and_pool_identity", "launch_wallet_analysis", "lp_position_audit",
            "token_security_audit", "two_way_route_quotes"))
    )
    route_evidence = candidate.get("dex_execution_quotes") if isinstance(candidate.get("dex_execution_quotes"), dict) else {}
    route_timestamp = parse_time(route_evidence.get("timestamp_utc"))
    route_age_seconds = (snapshot - route_timestamp).total_seconds() if snapshot and route_timestamp else None
    route_payload_complete = bool(
        route_evidence.get("provider") and route_evidence.get("buy_quote") and route_evidence.get("sell_quote") and
        route_age_seconds is not None and -5 <= route_age_seconds <= 300
    )
    dex_route_verified = (candidate.get("dex_buy_quote_available") is True and
                          candidate.get("dex_sell_quote_available") is True and dex_quotes_fresh and
                          dex_route_size_ok and guard_evidence_pass and route_payload_complete)
    route_verified = bool((is_dex and dex_route_verified) or
                          (not is_dex and cex_book_verified))

    target = _num(candidate.get("target_price"))
    tp_upside_pct = canonical_tp_upside(price, target)
    verification_gaps = []
    if stale:
        verification_gaps.append("price snapshot stale/unknown (> %d min or timestamp missing)" % max_age_minutes)
    if not market_verified:
        verification_gaps.append("exact DEX chain, pool, token contract, and refreshed price identity not verified")
    if is_dex and candidate.get("dex_loss_guard_status") != "PASS":
        verification_gaps.append("independent DEX identity, safety, and two-way execution guard not PASS")
    if not route_verified:
        verification_gaps.append("executable buy AND sell route not verified")
    if not spot_cvd_checked:
        verification_gaps.append("fresh trade-level spot CVD not verified (DEX swaps/CEX aggressor-side trades)")
    if not (deriv_ok and (derivatives_audit_status == "COMPLETE" or derivatives_no_market)):
        verification_gaps.append("derivatives positioning audit incomplete/stale; check OI, funding, liquidations and long/short positioning")
    if not (safety_checked and safety == "PASS"):
        verification_gaps.append("full token/contract safety checks not independently verified PASS")
    mandatory_evidence_fields = {"spot_cvd", "derivatives", "safety", "single_take_profit", "route_quotes"}
    mandatory_provenance_problems = [problem for problem in provenance_problems
                                     if problem.split(":", 1)[0] in mandatory_evidence_fields]
    if mandatory_provenance_problems:
        verification_gaps.append("mandatory pre-pick source/timestamp evidence incomplete: " +
                                 "; ".join(mandatory_provenance_problems))
    if target is None or price is None or target <= price:
        verification_gaps.append("one source-backed single take-profit price not available")
    elif tp_upside_pct is None or tp_upside_pct < 40.0:
        verification_gaps.append("single take-profit upside below mandatory +40.00% threshold")
    if require_field_provenance and provenance_problems:
        verification_gaps.append("field-level provenance incomplete/stale: " + "; ".join(provenance_problems))

    # The following are risk disclosures, not rank-erasing filters. The user asked
    # for the biggest opportunities, including microcaps; preserve them and make
    # the risk tier explicit. No R:R or fixed target-distance veto is applied.
    risk_flags = [p[1] for p in penalties]
    if safety != "PASS" or candidate.get("safety_checked") is not True:
        risk_flags.append("contract/token safety is not independently verified PASS — speculative classification")
    if require_chase_diagnostics and any(x is None for x in (ext_vwap, ext_base, last_hour_share, wick15, wick1h)):
        risk_flags.append("anti-chase diagnostics incomplete (VWAP/base extension, last-hour move share, 15m/1h upper wicks)")
    volume_floor = 2.0 if risk_off else 1.5
    buyer_floor = 58.0 if risk_off else 53.0
    spread_ceiling = 20.0 if risk_off else 30.0
    if _num(vols.get("15m")) is None or float(vols.get("15m", 0)) < volume_floor:
        risk_flags.append("15m volume acceleration below %.1fx or missing" % volume_floor)
    if _num(vols.get("1h")) is None or float(vols.get("1h", 0)) < volume_floor:
        risk_flags.append("1h volume acceleration below %.1fx or missing" % volume_floor)
    if buyer is None or buyer < buyer_floor:
        risk_flags.append("spot buyer share below %.0f%% or missing" % buyer_floor)
    if spread is None or spread > spread_ceiling:
        risk_flags.append("spread above %.0f bps or missing" % spread_ceiling)
    if order_notional is None or order_notional <= 0:
        order_notional = 100.0  # research-only quote benchmark, not user capital
        liq_notes.append("$100 benchmark only; not user position size")
    required_depth = max(5_000.0, 20.0 * order_notional)
    if bid_depth is None or ask_depth is None or min(bid_depth, ask_depth) < required_depth:
        risk_flags.append("two-sided 2%% depth below $%.0f research benchmark" % required_depth)
    if risk_off:
        btc_rs = _num(rs.get("btc_24h"))
        if btc_rs is None or btc_rs < 2.0:
            risk_flags.append("BTC risk-off: relative strength below +2pp or unavailable")
        if market_context.get("btc_breakdown_confirmed") is True:
            risk_flags.append("confirmed BTC 4h breakdown — extreme long-entry risk")
    if turnover is None or turnover < 250_000:
        risk_flags.append("24h spot turnover below $250k or missing")
    if r1 is None or r4 is None or (r1 <= 0 and r4 <= 0):
        risk_flags.append("no positive 1h/4h spot momentum")
    if candidate.get("high_risk_scout") is True:
        risk_flags.append("high-risk scout: microcap/age/float/contract/liquidity risk")
    if chase_flags:
        risk_flags.extend("late-chase warning: " + flag for flag in chase_flags)
    if not require_field_provenance and not evidence_map:
        provenance_problems.append("field-level source/timestamps not supplied; legacy snapshot timestamp only")

    if hard_fail:
        risk_flags.append("confirmed hard safety/contract/market failure — do not enter")
        entry_status = "AVOID — HARD FAIL"
    elif verification_gaps:
        entry_status = "TOP ROCKET — EXECUTION UNVERIFIED"
    elif risk_flags:
        entry_status = "SPECULATIVE BUY NOW — HIGH RISK"
    else:
        entry_status = "BUY NOW — RESEARCH QUALIFIED"

    if not fresh:
        confidence = "STALE / UNKNOWN SNAPSHOT — relative confidence unavailable"
    elif coverage >= 85 and final_score >= 75:
        confidence = "HIGHER RELATIVE CONFIDENCE — still uncalibrated"
    elif coverage >= 70 and final_score >= 60:
        confidence = "MEDIUM RELATIVE CONFIDENCE — still uncalibrated"
    else:
        confidence = "LOW RELATIVE CONFIDENCE — still uncalibrated"

    entry_ready = entry_status in ("BUY NOW — RESEARCH QUALIFIED", "SPECULATIVE BUY NOW — HIGH RISK")
    return {
        "identity": candidate.get("identity"),
        "asset": candidate.get("asset", "UNKNOWN"),
        "pair": candidate.get("pair", "UNKNOWN"),
        "venue": candidate.get("venue", "UNKNOWN"),
        "market_type": market_type,
        "chain": candidate.get("chain") or candidate.get("network"),
        "contract_address": candidate.get("contract_address") or candidate.get("token_address"),
        "pool_address": candidate.get("pool_address"),
        "pair_url": candidate.get("pair_url"),
        "final_price_usd": candidate.get("final_price_usd"),
        "final_price_timestamp_utc": candidate.get("final_price_timestamp_utc"),
        "final_price_verified": candidate.get("final_price_verified") is True,
        "final_pool_address": candidate.get("final_pool_address"),
        "final_token_contract": candidate.get("final_token_contract"),
        "final_quote_chain": candidate.get("final_quote_chain"),
        "final_quote_token_orientation_verified": candidate.get("final_quote_token_orientation_verified") is True,
        "tracked_token_orientation_verified": candidate.get("tracked_token_orientation_verified") is True,
        "dex_loss_guard_status": candidate.get("dex_loss_guard_status"),
        "dex_execution_quotes": candidate.get("dex_execution_quotes"),
        "dex_loss_guard_evidence": candidate.get("dex_loss_guard_evidence"),
        "returns_pct": returns,
        "price": price,
        "canonical_tp_reference_price": price if tp_upside_pct is not None else None,
        "tp_upside_pct": round(tp_upside_pct, 8) if tp_upside_pct is not None else None,
        "forecast_score": final_score,
        "evidence_coverage_pct": coverage,
        "relative_confidence": confidence,
        "entry_decision": entry_status,
        "trade_plan": {
            "entry_price": price if entry_ready and route_verified and not hard_fail else None,
            "single_take_profit": target if price is not None and target is not None and target > price else None,
            "time_stop_24h": (snapshot + timedelta(hours=24)).replace(microsecond=0).isoformat().replace("+00:00", "Z") if snapshot else None,
        },
        "retest_required": False,
        "snapshot_age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
        "data_fresh": fresh,
        "market_verified": market_verified,
        "safety_verified": safety == "PASS" and candidate.get("safety_checked") is True,
        "executable_route_verified": route_verified,
        "order_book_age_minutes": round(book_age, 1) if book_age is not None else None,
        "dex_quote_age_minutes": round(dex_quote_age, 1) if dex_quote_age is not None else None,
        "risk_penalty": round(penalty_total, 1),
        "risk_flags": list(dict.fromkeys(risk_flags)),
        "late_chase_flags": chase_flags,
        "risk_off_regime": risk_off,
        "sector": candidate.get("sector"),
        "sector_peer_count": sector_peer_count,
        "field_provenance_status": "PASS" if evidence_map and not provenance_problems else ("FAIL" if require_field_provenance else "LEGACY / NOT FULLY VERIFIED"),
        "field_provenance_problems": provenance_problems,
        "field_provenance_ages_minutes": provenance_ages,
        "funding_annualized_pct_simple": round(funding_annualized, 3) if funding_annualized is not None else None,
        "catalyst_age_hours": round(catalyst_age_hours, 2) if catalyst_age_hours is not None else None,
        "catalyst_decay_factor": round(catalyst_decay, 4),
        "entry_blockers": verification_gaps,
        "pre_pick_audit_status": "COMPLETE" if not verification_gaps else "PARTIAL",
        "spot_cvd_verified": spot_cvd_checked,
        "derivatives_audit_complete": bool(deriv_ok and (derivatives_audit_status == "COMPLETE" or derivatives_no_market)),
        "factor_details": details,
        "note": "Always-ranked rocket shortlist. Action tier separates a qualified route from speculative risk and missing verification. No reward/risk ratio or fixed target-distance veto is used. A buy label is not an order and does not guarantee a fill or profit.",
    }


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def rank_document(doc, max_age_minutes=30):
    snapshot = doc.get("as_of_utc")
    if parse_time(snapshot) is None:
        raise ValueError("top-level as_of_utc must be an ISO-8601 UTC timestamp")
    candidates = doc.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("candidates must be a non-empty list")

    # Do not erase futures-only, newly launched or unverified rows from the raw
    # leaderboard. They remain visible, but cannot receive a spot BUY label.
    eligible = list(candidates)
    sector_counts = {}
    for row in eligible:
        sector = row.get("sector")
        if sector:
            sector_counts[sector] = sector_counts.get(sector, 0) + 1

    market_context = doc.get("market_context") or {}
    evaluated = [evaluate(
        c, snapshot, max_age_minutes,
        require_field_provenance=doc.get("require_field_provenance") is True,
        require_chase_diagnostics=doc.get("require_chase_diagnostics") is True,
        market_context=market_context,
        sector_peer_count=sector_counts.get(c.get("sector"), 1),
    ) for c in eligible]
    evaluated.sort(key=lambda x: (x["forecast_score"], x["evidence_coverage_pct"]), reverse=True)
    for i, row in enumerate(evaluated, 1):
        row["forecast_rank"] = i
    upside_leader = evaluated[0]
    buy_tiers = ("BUY NOW — RESEARCH QUALIFIED", "SPECULATIVE BUY NOW — HIGH RISK")
    buys = [r for r in evaluated if r["entry_decision"] in buy_tiers and
            r.get("market_type") == "dex_pool" and r.get("pool_address") and
            r.get("contract_address") and r.get("chain") and
            r.get("dex_loss_guard_status") == "PASS" and
            r.get("final_price_verified") is True and
            r.get("tracked_token_orientation_verified") is True]
    # Prefer the highest-ranked verified entry. If none is verifiable, still
    # return the highest-ranked raw mover as TOP ROCKET with its missing checks.
    top = buys[0] if buys else upside_leader
    if not buys and top["entry_decision"] in buy_tiers:
        top["entry_blockers"] = list(dict.fromkeys(top["entry_blockers"] + [
            "candidate does not satisfy the final DEX identity, +40% TP, safety, and two-way execution shortlist gate"
        ]))
        top["entry_decision"] = "TOP ROCKET — EXECUTION UNVERIFIED"
    misses = [r for r in evaluated if r not in buys]
    misses.sort(key=lambda x: (len(x["entry_blockers"]), -x["forecast_score"], -x["evidence_coverage_pct"]))
    closest_misses = [{
        "asset": r["asset"], "pair": r["pair"], "venue": r["venue"],
        "forecast_rank": r["forecast_rank"], "forecast_score": r["forecast_score"],
        "entry_decision": r["entry_decision"], "verification_gaps": r["entry_blockers"][:8],
        "risk_flags": r["risk_flags"][:5],
    } for r in misses[:5]]
    regime = "RISK_OFF" if any(r["risk_off_regime"] for r in evaluated) else str(market_context.get("label") or "UNVERIFIED")
    if top["entry_decision"] == "BUY NOW — RESEARCH QUALIFIED":
        summary = "TOP PICK — BUY NOW: %s (%s)" % (top["asset"], top["pair"])
    elif top["entry_decision"] == "SPECULATIVE BUY NOW — HIGH RISK":
        summary = "TOP PICK — SPECULATIVE BUY NOW / HIGH RISK: %s (%s)" % (top["asset"], top["pair"])
    elif top["entry_decision"] == "AVOID — HARD FAIL":
        summary = "TOP ROCKET LEADER IS HARD-FAILED: %s (%s); next viable ranked asset is shown below" % (top["asset"], top["pair"])
    else:
        summary = "TOP ROCKET — EXECUTION UNVERIFIED: %s (%s); see exact missing checks" % (top["asset"], top["pair"])
    # If the raw #1 is a hard-fail, promote the next non-hard-failed row as the
    # Top Pick, while keeping the raw #1 separately visible.
    if top["entry_decision"] == "AVOID — HARD FAIL":
        alternatives = [r for r in evaluated if r["entry_decision"] != "AVOID — HARD FAIL"]
        if alternatives:
            top = alternatives[0]
            if top["entry_decision"] in buy_tiers and top not in buys:
                top["entry_decision"] = "TOP ROCKET — EXECUTION UNVERIFIED"
                top["entry_blockers"] = list(dict.fromkeys(top["entry_blockers"] + [
                    "candidate does not satisfy the final DEX identity, +40% TP, safety, and two-way execution shortlist gate"
                ]))
            summary = "TOP PICK — %s: %s (%s)" % (top["entry_decision"], top["asset"], top["pair"])
    horizon_hours = float(doc.get("forecast_horizon_hours", 24))
    if horizon_hours <= 0:
        raise ValueError("forecast_horizon_hours must be > 0")
    # The data snapshot may be later than the scan launch (discovery/audits take
    # time). Keep freshness checks anchored to as_of_utc, but anchor the user's
    # forward forecast/time-stop to the frozen scan_start_utc when supplied.
    forecast_start = parse_time(doc.get("forecast_window_start_utc") or snapshot)
    if forecast_start is None:
        raise ValueError("forecast_window_start_utc must be a valid ISO-8601 timestamp")
    end_dt = forecast_start + timedelta(hours=horizon_hours)
    fmt_utc = lambda dt: dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    forecast_end = fmt_utc(end_dt)
    for row in evaluated:
        row["trade_plan"]["time_stop_24h"] = forecast_end
    top["trade_plan"]["time_stop_24h"] = forecast_end
    return {
        "schema": "crypto-prebreakout.next-day-forecast.v4",
        "skill_version": VERSION,
        "as_of_utc": snapshot,
        "forecast_window_start_utc": fmt_utc(forecast_start),
        "forecast_window_end_utc": forecast_end,
        "forecast_horizon_hours": horizon_hours,
        "universe": doc.get("universe", "USER-SUPPLIED / PARTIAL — declare exact universe"),
        "spot_only": doc.get("spot_only", True),
        "pre_pick_audit_status": top["pre_pick_audit_status"],
        "market_regime": regime,
        "market_context": market_context,
        "raw_upside_leader": upside_leader,
        "upside_leader": upside_leader,
        "top_rocket_pick": top,
        "forecast_leader": top,
        "best_immediate_entry": buys[0] if buys else None,
        "verified_actionable_opportunities": buys,
        "discovery_candidates": evaluated,
        "candidates": evaluated,
        "filtered_candidate_count": 0,
        "closest_misses": closest_misses,
        "summary": summary,
        "calibration": "No probabilities are emitted. Show resolved forecast count, Wilson interval and source-specific discovery baseline only from a frozen, time-separated ledger; before calibrated history exists, state that explicitly.",
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", help="JSON document with timestamped candidates")
    p.add_argument("--out", help="optional output JSON path")
    p.add_argument("--json", action="store_true", help="print full JSON (default summary)")
    p.add_argument("--max-age-minutes", type=int, default=30)
    market = p.add_mutually_exclusive_group()
    market.add_argument("--spot-only", dest="spot_only", action="store_true", help="rank only explicitly verified spot listings")
    market.add_argument("--include-nonspot", dest="spot_only", action="store_false", help="include non-spot rows for research context; this does not authorize futures entries")
    p.set_defaults(spot_only=None)
    args = p.parse_args(argv)
    if args.max_age_minutes < 1:
        p.error("--max-age-minutes must be >= 1")
    try:
        with open(args.input, encoding="utf-8") as fh:
            doc = json.load(fh)
        if args.spot_only is not None:
            doc["spot_only"] = args.spot_only
        result = rank_document(doc, args.max_age_minutes)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print("Forecast failed: %s" % exc, file=sys.stderr)
        return 2
    rendered = json.dumps(result, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(rendered + "\n")
    if args.json:
        print(rendered)
    else:
        print(result["summary"])
        print("As of: " + result["as_of_utc"])
        if result.get("raw_upside_leader"):
            raw = result["raw_upside_leader"]
            print("Raw upside leader: %s | score %s/100 | evidence %s%%" % (
                raw["pair"], raw["forecast_score"], raw["evidence_coverage_pct"]))
        if result.get("top_rocket_pick"):
            selected = result["top_rocket_pick"]
            print("TOP PICK: %s | %s | %s" % (selected["asset"], selected["pair"], selected["entry_decision"]))
            print("Price: %s | venue: %s | confidence: %s" % (selected["price"], selected["venue"], selected["relative_confidence"]))
            for item in selected["entry_blockers"]:
                print("  Verification gap: " + item)
            for item in selected["risk_flags"][:8]:
                print("  Risk: " + item)
        if result["best_immediate_entry"]:
            b = result["best_immediate_entry"]
            print("TRADE PLAN: Entry price: %s | Single take-profit: %s | Time stop (24h): %s" % (
                b["trade_plan"]["entry_price"], b["trade_plan"]["single_take_profit"], b["trade_plan"]["time_stop_24h"]))
        else:
            print("No execution-verified entry is present in this input; the Top Pick remains ranked above.")
        print("Retest required: NO (entry status reflects current evidence and execution verification)")
        print(result["calibration"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
