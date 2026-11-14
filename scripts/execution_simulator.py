#!/usr/bin/env python3
"""Estimate marketable spot entry/exit VWAP and book impact from a saved snapshot.

This is a read-only order-book simulation. It does not submit orders, predict fills,
or guarantee that displayed depth will remain available.
"""
from __future__ import annotations

import argparse
import datetime as _dt
from datetime import datetime, timezone
import json
import math
import sys
from pathlib import Path
from typing import Any

VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()
BACKTEST_VERSION = VERSION


def _find_book(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("bids"), list) and isinstance(obj.get("asks"), list):
            return obj
        for value in obj.values():
            found = _find_book(value)
            if found:
                return found
    return None


def _levels(raw, reverse=False):
    levels = []
    if not isinstance(raw, list):
        raise ValueError("order-book side must be a list")
    for item in raw:
        try:
            price, qty = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(price) and math.isfinite(qty) and price > 0 and qty > 0:
            levels.append((price, qty))
    return sorted(levels, key=lambda x: x[0], reverse=reverse)


def _consume(levels, quantity):
    remaining = quantity
    filled_qty = quote = 0.0
    fills = []
    for price, available in levels:
        if remaining <= 1e-15:
            break
        take = min(remaining, available)
        if take > 0:
            fills.append({"price": price, "quantity": take, "quote": price * take})
            filled_qty += take
            quote += price * take
            remaining -= take
    return {"filled_quantity": filled_qty, "quote_notional": quote,
            "remaining_quantity": max(0.0, remaining),
            "vwap": quote / filled_qty if filled_qty else None, "fills": fills}


def simulate(book, buy_notional_usd, fee_bps=0.0):
    try:
        buy_notional_usd = float(buy_notional_usd)
        fee_bps = float(fee_bps)
    except (TypeError, ValueError):
        raise ValueError("notional and fee_bps must be numeric")
    if not math.isfinite(buy_notional_usd) or buy_notional_usd <= 0:
        raise ValueError("buy_notional_usd must be positive and finite")
    if not math.isfinite(fee_bps) or fee_bps < 0:
        raise ValueError("fee_bps must be non-negative and finite")

    bids = _levels(book.get("bids"), reverse=True)
    asks = _levels(book.get("asks"), reverse=False)
    if not bids or not asks:
        raise ValueError("book must contain at least one valid bid and ask")
    best_bid, best_ask = bids[0][0], asks[0][0]
    if best_bid >= best_ask:
        raise ValueError("crossed or locked book; inspect snapshot before simulation")
    mid = (best_bid + best_ask) / 2.0

    # Measure quote depth within +/-2% of mid; depth is displayed, cancellable size.
    bid_depth_2pct = sum(p * q for p, q in bids if p >= mid * 0.98)
    ask_depth_2pct = sum(p * q for p, q in asks if p <= mid * 1.02)

    # Walk asks until the requested quote spend is reached.
    remaining_quote = buy_notional_usd
    buy_qty = buy_quote = 0.0
    buy_fills = []
    for price, available_qty in asks:
        if remaining_quote <= 1e-10:
            break
        take_qty = min(available_qty, remaining_quote / price)
        if take_qty > 0:
            spent = take_qty * price
            buy_fills.append({"price": price, "quantity": take_qty, "quote": spent})
            buy_qty += take_qty
            buy_quote += spent
            remaining_quote -= spent
    buy_complete = remaining_quote <= max(1e-8, buy_notional_usd * 1e-9)

    # Simulate an immediate sale of the quantity purchased against the bid side.
    sell = _consume(bids, buy_qty)
    sell_complete = sell["remaining_quantity"] <= max(1e-12, buy_qty * 1e-9)
    buy_vwap = buy_quote / buy_qty if buy_qty else None
    gross_pnl = (sell["quote_notional"] - buy_quote) if sell["vwap"] is not None else None
    fee_cost = (buy_quote + sell["quote_notional"]) * fee_bps / 10000.0 if gross_pnl is not None else None
    net_pnl = gross_pnl - fee_cost if gross_pnl is not None else None

    return {
        "schema": "crypto-prebreakout.execution-simulation.v1",
        "skill_version": VERSION,
        "status": "FULL_ROUND_TRIP_DEPTH_AVAILABLE" if buy_complete and sell_complete else "INSUFFICIENT_DISPLAYED_DEPTH",
        "execution_enabled": False,
        "snapshot": book.get("timestamp_utc") or book.get("timestamp") or "NOT PROVIDED",
        "mid_price": mid,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_bps": (best_ask - best_bid) / mid * 10000.0,
        "requested_buy_notional_usd": buy_notional_usd,
        "buy": {"filled_quote_usd": buy_quote, "filled_quantity": buy_qty, "vwap": buy_vwap,
                "remaining_quote_usd": max(0.0, remaining_quote), "complete": buy_complete,
                "impact_vs_best_ask_bps": ((buy_vwap / best_ask) - 1.0) * 10000.0 if buy_vwap else None,
                "slippage_vs_mid_bps": ((buy_vwap / mid) - 1.0) * 10000.0 if buy_vwap else None,
                "fills": buy_fills},
        "immediate_exit": {"filled_quote_usd": sell["quote_notional"], "filled_quantity": sell["filled_quantity"],
                           "vwap": sell["vwap"], "remaining_quantity": sell["remaining_quantity"],
                           "complete": sell_complete,
                           "impact_vs_best_bid_bps": ((best_bid / sell["vwap"]) - 1.0) * 10000.0 if sell["vwap"] else None,
                           "slippage_vs_mid_bps": ((mid / sell["vwap"]) - 1.0) * 10000.0 if sell["vwap"] else None,
                           "fills": sell["fills"]},
        "depth_within_2pct_usd": {"bids": bid_depth_2pct, "asks": ask_depth_2pct,
                                  "limiting_side": min(bid_depth_2pct, ask_depth_2pct)},
        "round_trip_estimate": {"fee_bps_each_side": fee_bps,
                                "gross_pnl_usd": gross_pnl, "estimated_fee_usd": fee_cost,
                                "net_pnl_usd": net_pnl,
                                "net_return_pct": (net_pnl / buy_quote * 100.0) if net_pnl is not None and buy_quote else None},
        "limitations": ["single order-book snapshot; displayed orders may cancel or move",
                        "immediate round-trip simulation is not a future exit-price forecast",
                        "does not model hidden liquidity, queue priority, latency, adverse selection or dynamic impact",
                        "fees are user-supplied and may differ by tier, venue, token or order type"],
    }


DEFAULT_RESEARCH_NOTIONAL_SIZES_USD = (100.0, 500.0, 1000.0, 5000.0)


def _clean_numeric(val: Any) -> float | None:
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def _clean_strictly_positive(val: Any) -> float | None:
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) and f > 0 else None
    except (ValueError, TypeError):
        return None


def _parse_iso_utc(val: Any) -> datetime | None:
    """Parse a timezone-aware ISO UTC timestamp. Reject naive or malformed timestamps."""
    if not isinstance(val, (str, datetime)):
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None or val.tzinfo.utcoffset(val) is None:
            return None
        return val.astimezone(timezone.utc)
    s = str(val).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        parsed = datetime.fromisoformat(s)
        if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def validate_timestamp_freshness(
    raw_stamp: Any,
    as_of_utc: Any,
    max_quote_age_seconds: float = 300.0,
) -> tuple[bool, str, float | None]:
    """Validate quote timestamp freshness against evaluation time as_of_utc.

    Returns:
        (is_fresh, status_str, age_seconds)
    """
    if as_of_utc is None:
        return False, "INVALID_EVALUATION_TIMESTAMP", None
    as_of_dt = _parse_iso_utc(as_of_utc)
    if as_of_dt is None:
        return False, "INVALID_EVALUATION_TIMESTAMP", None

    if raw_stamp is None:
        return False, "MISSING_TIMESTAMP", None

    quote_dt = _parse_iso_utc(raw_stamp)
    if quote_dt is None:
        if isinstance(raw_stamp, str) and ("T" in raw_stamp or "-" in raw_stamp):
            try:
                naive = datetime.fromisoformat(raw_stamp)
                if naive.tzinfo is None:
                    return False, "NAIVE_TIMESTAMP_REJECTED", None
            except Exception:
                pass
        return False, "INVALID_QUOTE_TIMESTAMP", None

    age_seconds = (as_of_dt - quote_dt).total_seconds()
    if age_seconds < -1e-6:
        return False, "FUTURE_DATED_QUOTE", age_seconds
    if age_seconds > max_quote_age_seconds:
        return False, "STALE_QUOTE", age_seconds

    return True, "FRESH", age_seconds


def validate_two_way_quotes(
    buy_q: Any,
    sell_q: Any,
    *,
    expected_token_address: str | None = None,
    expected_chain: str | None = None,
    expected_pool: str | None = None,
    target_size_usd: float | None = None,
    size_tolerance_pct: float = 1.0,
    quote_meta: dict[str, Any] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Validate both sides of a router trade quote.

    Ensures positive finite inputs/outputs, valid directions, matching contract/chain/pool,
    and binding to the requested notional size within tolerance.
    """
    if not isinstance(buy_q, dict) or not buy_q:
        return False, "MISSING_BUY_ROUTE", {}
    if not isinstance(sell_q, dict) or not sell_q:
        return False, "MISSING_SELL_ROUTE", {}

    # Buy direction
    buy_dir = str(buy_q.get("direction") or "").upper()
    if buy_dir in {"SELL", "EXACT_OUT", "SWAP_OUT"}:
        return False, "DIRECTION_MISMATCH", {"reason": "buy_quote has SELL direction"}

    # Sell direction
    sell_dir = str(sell_q.get("direction") or "").upper()
    if sell_dir in {"BUY", "EXACT_IN", "SWAP_IN"}:
        return False, "DIRECTION_MISMATCH", {"reason": "sell_quote has BUY direction"}

    # Input and output amounts
    buy_out = _clean_strictly_positive(
        buy_q.get("out_amount") or buy_q.get("amount_out") or
        buy_q.get("output_amount") or buy_q.get("filled_quantity")
    )
    if buy_out is None:
        return False, "INVALID_BUY_QUOTE_AMOUNTS", {"reason": "buy output amount must be positive and finite"}

    sell_out = _clean_strictly_positive(
        sell_q.get("out_amount") or sell_q.get("amount_out") or
        sell_q.get("output_amount") or sell_q.get("filled_quote_usd") or
        sell_q.get("notional_size_usd")
    )
    if sell_out is None:
        return False, "INVALID_SELL_QUOTE_AMOUNTS", {"reason": "sell output amount must be positive and finite"}

    # Check for non-finite or negative in amounts if explicitly specified
    for q_side, q_dict, err_name in (("buy", buy_q, "INVALID_BUY_QUOTE_AMOUNTS"), ("sell", sell_q, "INVALID_SELL_QUOTE_AMOUNTS")):
        for key in ("in_amount", "amount_in", "input_amount"):
            if key in q_dict:
                v = q_dict[key]
                cleaned = _clean_strictly_positive(v)
                if cleaned is None:
                    return False, err_name, {"reason": f"{q_side} {key} must be positive and finite"}

    # Address validation
    if expected_token_address:
        exp_addr = str(expected_token_address).lower()
        buy_out_tok = buy_q.get("out_token") or buy_q.get("token_address") or buy_q.get("contract_address")
        if buy_out_tok and str(buy_out_tok).lower() != exp_addr:
            return False, "TOKEN_ADDRESS_MISMATCH", {"expected": exp_addr, "actual": str(buy_out_tok)}
        sell_in_tok = sell_q.get("in_token") or sell_q.get("token_address") or sell_q.get("contract_address")
        if sell_in_tok and str(sell_in_tok).lower() != exp_addr:
            return False, "TOKEN_ADDRESS_MISMATCH", {"expected": exp_addr, "actual": str(sell_in_tok)}

    # Chain validation
    if expected_chain:
        exp_chain = str(expected_chain).lower()
        q_chain = (quote_meta or {}).get("chain") or buy_q.get("chain") or sell_q.get("chain")
        if q_chain and str(q_chain).lower() != exp_chain:
            return False, "CHAIN_MISMATCH", {"expected": exp_chain, "actual": str(q_chain)}

    # Pool validation
    if expected_pool:
        exp_pool = str(expected_pool).lower()
        q_pool = (quote_meta or {}).get("pool_address") or buy_q.get("pool_address") or sell_q.get("pool_address")
        if q_pool and str(q_pool).lower() != exp_pool:
            return False, "POOL_MISMATCH", {"expected": exp_pool, "actual": str(q_pool)}

    # Quoted notional size binding
    quoted_size = (
        _clean_strictly_positive(buy_q.get("notional_size_usd")) or
        _clean_strictly_positive(buy_q.get("in_amount")) or
        _clean_strictly_positive(sell_q.get("notional_size_usd")) or
        _clean_strictly_positive(sell_q.get("output_amount")) or
        _clean_strictly_positive((quote_meta or {}).get("notional_size_usd")) or
        _clean_strictly_positive((quote_meta or {}).get("quote_size_usd"))
    )

    if target_size_usd is not None:
        if quoted_size is None:
            return False, "ORDER_SIZE_UNVERIFIED", {"size_bound": False}
        rel_diff = abs(quoted_size - target_size_usd) / target_size_usd
        tolerance = size_tolerance_pct / 100.0
        if rel_diff > tolerance:
            return False, "ORDER_SIZE_MISMATCH", {
                "size_bound": False,
                "quoted_size": quoted_size,
                "target_size": target_size_usd,
                "rel_diff_pct": rel_diff * 100.0,
                "tolerance_pct": size_tolerance_pct,
            }

    return True, "TWO_WAY_QUOTE_VERIFIED", {
        "size_bound": quoted_size is not None,
        "quoted_size": quoted_size,
        "buy_out": buy_out,
        "sell_out": sell_out,
    }


def simulate_constant_product_amm(
    reserve_base: float,
    reserve_quote: float,
    buy_notional_usd: float,
    fee_bps: float = 30.0,
) -> dict[str, Any]:
    """Exact constant-product AMM (x * y = k) simulation with reserve tracking and fees."""
    rb = _clean_strictly_positive(reserve_base)
    rq = _clean_strictly_positive(reserve_quote)
    if rb is None or rq is None:
        raise ValueError("AMM reserves must be positive and finite")

    buy_usd = _clean_strictly_positive(buy_notional_usd)
    if buy_usd is None:
        raise ValueError("buy_notional_usd must be positive and finite")

    fee = _clean_numeric(fee_bps)
    if fee is None or fee < 0 or fee >= 10000:
        raise ValueError("fee_bps must be non-negative and finite")

    mid_price = rq / rb
    fee_factor = 1.0 - (fee / 10000.0)

    # Buy swap: spend buy_usd quote tokens to get base tokens
    delta_in_eff = buy_usd * fee_factor
    delta_base_out = (rb * delta_in_eff) / (rq + delta_in_eff)

    if delta_base_out <= 0 or delta_base_out >= rb:
        return {
            "status": "INSUFFICIENT_LIQUIDITY",
            "route_available": False,
            "simulation_method": "CONSTANT_PRODUCT_AMM_SIMULATION",
            "reserve_base": rb,
            "reserve_quote": rq,
            "mid_price": mid_price,
            "buy": {"complete": False, "filled_quote_usd": 0.0, "filled_quantity": 0.0, "vwap": None},
            "immediate_exit": {"complete": False, "filled_quote_usd": 0.0, "filled_quantity": 0.0, "vwap": None},
            "round_trip_estimate": {"fee_bps_each_side": fee, "net_pnl_usd": None, "net_return_pct": None},
        }

    rb_post = rb - delta_base_out
    rq_post = rq + buy_usd
    buy_vwap = buy_usd / delta_base_out
    buy_impact_bps = ((buy_vwap / mid_price) - 1.0) * 10000.0

    # Sell swap: sell delta_base_out back into the pool to get quote tokens
    delta_base_in_eff = delta_base_out * fee_factor
    delta_quote_out = (rq_post * delta_base_in_eff) / (rb_post + delta_base_in_eff)
    sell_vwap = delta_quote_out / delta_base_out
    sell_impact_bps = ((mid_price / sell_vwap) - 1.0) * 10000.0

    gross_pnl = delta_quote_out - buy_usd
    net_return_pct = (gross_pnl / buy_usd) * 100.0

    return {
        "status": "FULL_ROUND_TRIP_AMM_LIQUIDITY_AVAILABLE",
        "route_available": True,
        "simulation_method": "CONSTANT_PRODUCT_AMM_SIMULATION",
        "reserve_base": rb,
        "reserve_quote": rq,
        "mid_price": mid_price,
        "buy": {
            "complete": True,
            "filled_quote_usd": buy_usd,
            "filled_quantity": delta_base_out,
            "vwap": buy_vwap,
            "price_impact_bps": buy_impact_bps,
        },
        "immediate_exit": {
            "complete": True,
            "filled_quote_usd": delta_quote_out,
            "filled_quantity": delta_base_out,
            "vwap": sell_vwap,
            "price_impact_bps": sell_impact_bps,
        },
        "round_trip_estimate": {
            "fee_bps_each_side": fee,
            "net_pnl_usd": gross_pnl,
            "net_return_pct": net_return_pct,
        },
    }


def simulate_multi_size(
    source: dict[str, Any],
    *,
    sizes_usd: tuple[float, ...] | list[float] = DEFAULT_RESEARCH_NOTIONAL_SIZES_USD,
    fee_bps: float = 30.0,
    gas_usd: float | None = None,
    as_of_utc: str | None = None,
    max_quote_age_seconds: float = 300.0,
    target_upside_pct: float | None = None,
    expected_token_address: str | None = None,
    expected_chain: str | None = None,
    expected_pool: str | None = None,
    size_tolerance_pct: float = 1.0,
) -> dict[str, Any]:
    """Evaluate multi-size read-only execution scenarios ($100, $500, $1k, $5k).

    Distinguishes actual order book depth walks, constant-product AMM simulations,
    and size-verified router quotes from approximate linear liquidity heuristics.
    Never assumes the same token has identical execution cost across sizes.
    Never assumes a buy quote proves a sell route is possible.
    Enforces quote freshness verification and size-bound quote validation.
    """
    fee_bps_clean = _clean_numeric(fee_bps)
    fee_bps = max(0.0, fee_bps_clean) if fee_bps_clean is not None else 30.0

    gas_usd_clean = _clean_numeric(gas_usd)
    gas_usd = max(0.0, gas_usd_clean) if gas_usd_clean is not None else None

    target_upside_clean = _clean_numeric(target_upside_pct)
    target_upside_pct = target_upside_clean

    max_age_clean = _clean_numeric(max_quote_age_seconds)
    max_quote_age_seconds = max(1.0, max_age_clean) if max_age_clean is not None else 300.0

    clean_sizes: list[float] = []
    raw_sizes = sizes_usd if isinstance(sizes_usd, (list, tuple, set)) else DEFAULT_RESEARCH_NOTIONAL_SIZES_USD
    for s in raw_sizes:
        num = _clean_strictly_positive(s)
        if num is not None:
            clean_sizes.append(num)
    if not clean_sizes:
        clean_sizes = list(DEFAULT_RESEARCH_NOTIONAL_SIZES_USD)

    book = _find_book(source)
    quotes = source.get("dex_execution_quotes") if isinstance(source.get("dex_execution_quotes"), dict) else (
        source if ("buy_quote" in source or "sell_quote" in source) else None
    )
    amm_reserves = source.get("amm_reserves") if isinstance(source.get("amm_reserves"), dict) else (
        source.get("reserves") if isinstance(source.get("reserves"), dict) else None
    )

    liquidity_usd = None
    if isinstance(source, dict):
        liquidity_usd = _clean_numeric(source.get("liquidity_usd"))

    # Timestamp freshness evaluation
    raw_stamp = (
        (quotes.get("timestamp_utc") if quotes else None) or
        (book.get("timestamp_utc") if book else None) or
        (amm_reserves.get("timestamp_utc") if amm_reserves else None) or
        source.get("quote_timestamp_utc") or
        source.get("final_price_timestamp_utc")
    )

    is_fresh, freshness_status, quote_age_seconds = validate_timestamp_freshness(
        raw_stamp, as_of_utc, max_quote_age_seconds
    )

    size_evaluations: dict[str, Any] = {}

    for size in clean_sizes:
        size_key = f"${int(size) if size.is_integer() else size}"

        # If timestamp freshness verification failed, fail closed immediately
        if not is_fresh:
            status_map = {
                "STALE_QUOTE": "STALE_QUOTE",
                "FUTURE_DATED_QUOTE": "FUTURE_DATED_QUOTE",
                "MISSING_TIMESTAMP": "UNVERIFIED_TIMESTAMP",
                "NAIVE_TIMESTAMP_REJECTED": "UNVERIFIED_TIMESTAMP",
                "INVALID_EVALUATION_TIMESTAMP": "UNVERIFIED_TIMESTAMP",
                "INVALID_QUOTE_TIMESTAMP": "UNVERIFIED_TIMESTAMP",
            }
            status_label = status_map.get(freshness_status, "UNVERIFIED_TIMESTAMP")
            quote_freshness_label = (
                "STALE" if freshness_status == "STALE_QUOTE" else
                "MISSING_TIMESTAMP_UNVERIFIED" if freshness_status == "MISSING_TIMESTAMP" else
                freshness_status
            )
            size_evaluations[size_key] = {
                "size_usd": size,
                "status": status_label,
                "route_available": False,
                "quote_freshness": quote_freshness_label,
                "simulation_method": f"{freshness_status}_REJECTED",
                "quote_verified_for_size": False,
                "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                "quote_age_seconds": round(quote_age_seconds, 1) if quote_age_seconds is not None else None,
                "buy": {"status": "UNAVAILABLE", "reason": f"Freshness verification failed: {freshness_status}"},
                "sell": {"status": "UNAVAILABLE", "reason": f"Freshness verification failed: {freshness_status}"},
                "estimated_round_trip_cost_pct": None,
                "execution_adjusted_return_pct": None,
            }
            continue

        # Case A: Order book depth simulation
        if book:
            try:
                sim = simulate(book, buy_notional_usd=size, fee_bps=fee_bps)
                buy_complete = sim["buy"]["complete"]
                sell_complete = sim["immediate_exit"]["complete"]
                buy_impact_bps = sim["buy"]["impact_vs_best_ask_bps"]
                buy_slip_bps = sim["buy"]["slippage_vs_mid_bps"]
                sell_impact_bps = sim["immediate_exit"]["impact_vs_best_bid_bps"]
                sell_slip_bps = sim["immediate_exit"]["slippage_vs_mid_bps"]
                round_trip_pct = abs(sim["round_trip_estimate"]["net_return_pct"]) if sim["round_trip_estimate"]["net_return_pct"] is not None else None

                gas_adj = (gas_usd / size * 100.0) if gas_usd and size > 0 else 0.0
                total_drag_pct = (round_trip_pct + gas_adj) if round_trip_pct is not None else None
                adj_return = (target_upside_pct - total_drag_pct) if target_upside_pct is not None and total_drag_pct is not None else None

                status = (
                    "EXECUTABLE_DEPTH_AVAILABLE" if buy_complete and sell_complete else
                    "INSUFFICIENT_DISPLAYED_DEPTH"
                )

                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": status,
                    "simulation_method": "ORDER_BOOK_DEPTH_WALK",
                    "is_depth_walk": True,
                    "quote_verified_for_size": True,
                    "size_verification_status": "SIZE_BOUND_ORDER_BOOK_WALK",
                    "route_available": buy_complete and sell_complete,
                    "quote_freshness": "FRESH",
                    "quote_age_seconds": round(quote_age_seconds, 1) if quote_age_seconds is not None else None,
                    "available_liquidity_usd": liquidity_usd,
                    "buy": {
                        "status": "AVAILABLE" if buy_complete else "PARTIAL_DEPTH_ONLY",
                        "filled_quote_usd": sim["buy"]["filled_quote_usd"],
                        "vwap": sim["buy"]["vwap"],
                        "price_impact_bps": buy_impact_bps,
                        "slippage_bps": buy_slip_bps,
                        "complete": buy_complete,
                    },
                    "sell": {
                        "status": "AVAILABLE" if sell_complete else "PARTIAL_DEPTH_ONLY",
                        "filled_quote_usd": sim["immediate_exit"]["filled_quote_usd"],
                        "vwap": sim["immediate_exit"]["vwap"],
                        "price_impact_bps": sell_impact_bps,
                        "slippage_bps": sell_slip_bps,
                        "complete": sell_complete,
                    },
                    "trading_fee_usd": sim["round_trip_estimate"]["estimated_fee_usd"],
                    "network_gas_usd": gas_usd,
                    "round_trip_cost_usd": abs(sim["round_trip_estimate"]["net_pnl_usd"]) if sim["round_trip_estimate"]["net_pnl_usd"] is not None else None,
                    "estimated_round_trip_cost_pct": round(total_drag_pct, 2) if total_drag_pct is not None else None,
                    "execution_adjusted_return_pct": round(adj_return, 2) if adj_return is not None else None,
                }
            except ValueError as e:
                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": "UNAVAILABLE",
                    "simulation_method": "ORDER_BOOK_DEPTH_WALK",
                    "is_depth_walk": True,
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "route_available": False,
                    "reason": str(e),
                    "buy": {"status": "UNAVAILABLE"},
                    "sell": {"status": "UNAVAILABLE"},
                }

        # Case B: Constant-product AMM simulation
        elif amm_reserves:
            try:
                rb = amm_reserves.get("reserve_base") or amm_reserves.get("reserve_token") or amm_reserves.get("base_reserve")
                rq = amm_reserves.get("reserve_quote") or amm_reserves.get("reserve_usd") or amm_reserves.get("quote_reserve")
                amm_sim = simulate_constant_product_amm(rb, rq, buy_notional_usd=size, fee_bps=fee_bps)

                buy_ok = amm_sim["buy"]["complete"]
                sell_ok = amm_sim["immediate_exit"]["complete"]
                round_trip_pct = abs(amm_sim["round_trip_estimate"]["net_return_pct"]) if amm_sim["round_trip_estimate"]["net_return_pct"] is not None else None
                gas_adj = (gas_usd / size * 100.0) if gas_usd and size > 0 else 0.0
                total_drag_pct = (round_trip_pct + gas_adj) if round_trip_pct is not None else None
                adj_return = (target_upside_pct - total_drag_pct) if target_upside_pct is not None and total_drag_pct is not None else None

                route_avail = (amm_sim["status"] == "FULL_ROUND_TRIP_AMM_LIQUIDITY_AVAILABLE")
                status = "EXECUTABLE_AMM_ROUTE_AVAILABLE" if route_avail else amm_sim["status"]

                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": status,
                    "simulation_method": "CONSTANT_PRODUCT_AMM_SIMULATION",
                    "is_depth_walk": False,
                    "quote_verified_for_size": True,
                    "size_verification_status": "AMM_CONSTANT_PRODUCT_CALCULATED",
                    "route_available": route_avail,
                    "quote_freshness": "FRESH",
                    "quote_age_seconds": round(quote_age_seconds, 1) if quote_age_seconds is not None else None,
                    "available_liquidity_usd": liquidity_usd,
                    "buy": {
                        "status": "AVAILABLE" if buy_ok else "UNAVAILABLE",
                        "filled_quote_usd": amm_sim["buy"]["filled_quote_usd"],
                        "vwap": amm_sim["buy"]["vwap"],
                        "price_impact_bps": amm_sim["buy"].get("price_impact_bps"),
                        "complete": buy_ok,
                    },
                    "sell": {
                        "status": "AVAILABLE" if sell_ok else "UNAVAILABLE",
                        "filled_quote_usd": amm_sim["immediate_exit"]["filled_quote_usd"],
                        "vwap": amm_sim["immediate_exit"]["vwap"],
                        "price_impact_bps": amm_sim["immediate_exit"].get("price_impact_bps"),
                        "complete": sell_ok,
                    },
                    "round_trip_cost_usd": abs(amm_sim["round_trip_estimate"]["net_pnl_usd"]) if amm_sim["round_trip_estimate"]["net_pnl_usd"] is not None else None,
                    "estimated_round_trip_cost_pct": round(total_drag_pct, 2) if total_drag_pct is not None else None,
                    "execution_adjusted_return_pct": round(adj_return, 2) if adj_return is not None else None,
                }
            except ValueError as e:
                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": "INVALID_AMM_RESERVES",
                    "simulation_method": "CONSTANT_PRODUCT_AMM_SIMULATION",
                    "is_depth_walk": False,
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "route_available": False,
                    "reason": str(e),
                    "buy": {"status": "UNAVAILABLE"},
                    "sell": {"status": "UNAVAILABLE"},
                }

        # Case C: Router quotes evaluation
        elif quotes:
            buy_q = quotes.get("buy_quote")
            sell_q = quotes.get("sell_quote")

            valid_route, route_reason, route_info = validate_two_way_quotes(
                buy_q,
                sell_q,
                expected_token_address=expected_token_address,
                expected_chain=expected_chain,
                expected_pool=expected_pool,
                target_size_usd=size,
                size_tolerance_pct=size_tolerance_pct,
                quote_meta=quotes,
            )

            # Check if liquidity supports this size
            insufficient_liquidity = (liquidity_usd is not None and liquidity_usd < (size * 2.0))

            if insufficient_liquidity:
                status = "INSUFFICIENT_LIQUIDITY"
                route_available = False
            elif not valid_route:
                status = route_reason
                route_available = False
            else:
                status = "EXECUTABLE_ROUTE_AVAILABLE"
                route_available = True

            size_bound = bool(valid_route and route_info.get("size_bound", False))

            impact_bps = None
            if liquidity_usd and liquidity_usd > 0:
                impact_pct = (size / (liquidity_usd / 2.0)) * 100.0
                impact_bps = round(impact_pct * 100.0, 1)

            fee_cost = (size * 2.0) * (fee_bps / 10000.0)
            slip_pct = (impact_bps / 100.0) if impact_bps is not None else 1.5
            gas_adj = (gas_usd / size * 100.0) if gas_usd and size > 0 else 0.0
            total_drag_pct = slip_pct + (fee_cost / size * 100.0) + gas_adj
            adj_return = (target_upside_pct - total_drag_pct) if target_upside_pct is not None else None

            sim_method = (
                "ROUTER_TWO_WAY_QUOTE_VERIFIED" if size_bound and valid_route else
                "APPROXIMATE_LIQUIDITY_HEURISTIC"
            )

            size_evaluations[size_key] = {
                "size_usd": size,
                "status": status,
                "simulation_method": sim_method,
                "is_depth_walk": False,
                "impact_model": "LINEAR_RESERVE_HEURISTIC_APPROXIMATION",
                "impact_model_disclaimer": "Linear liquidity estimate (trade_size / half_liquidity); not a constant-product AMM simulation or order book depth walk.",
                "quote_verified_for_size": size_bound,
                "size_verification_status": "SIZE_BOUND_QUOTE_VERIFIED" if size_bound else "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                "size_warning": None if size_bound else f"Router quote evidence not independently size-bound to ${size:.0f}; using approximate liquidity heuristic.",
                "route_available": route_available,
                "quote_freshness": "FRESH",
                "quote_age_seconds": round(quote_age_seconds, 1) if quote_age_seconds is not None else None,
                "available_liquidity_usd": liquidity_usd,
                "buy": {
                    "status": "AVAILABLE" if (isinstance(buy_q, dict) and buy_q and status not in {"MISSING_BUY_ROUTE", "BUY_DIRECTION_MISMATCH", "INVALID_BUY_QUOTE_AMOUNTS"}) else "UNAVAILABLE",
                    "provider": quotes.get("provider"),
                    "estimated_price_impact_bps": impact_bps,
                    "reason": route_info.get("reason") if not valid_route else None,
                },
                "sell": {
                    "status": "AVAILABLE" if (isinstance(sell_q, dict) and sell_q and status not in {"MISSING_SELL_ROUTE", "SELL_DIRECTION_MISMATCH", "INVALID_SELL_QUOTE_AMOUNTS"}) else "UNAVAILABLE",
                    "provider": quotes.get("provider"),
                    "estimated_price_impact_bps": impact_bps,
                    "sell_route_independently_verified": bool(isinstance(sell_q, dict) and sell_q),
                    "reason": route_info.get("reason") if not valid_route else None,
                },
                "trading_fee_usd": round(fee_cost, 2),
                "network_gas_usd": gas_usd,
                "estimated_round_trip_cost_pct": round(total_drag_pct, 2) if total_drag_pct is not None else None,
                "execution_adjusted_return_pct": round(adj_return, 2) if adj_return is not None else None,
            }

        # Case D: Only liquidity or no depth data (fail closed!)
        else:
            if liquidity_usd and liquidity_usd > 0:
                impact_pct = (size / (liquidity_usd / 2.0)) * 100.0
                impact_bps = round(impact_pct * 100.0, 1)
                fee_cost = (size * 2.0) * (fee_bps / 10000.0)
                slip_pct = impact_bps / 100.0
                gas_adj = (gas_usd / size * 100.0) if gas_usd and size > 0 else 0.0
                total_drag_pct = slip_pct + (fee_cost / size * 100.0) + gas_adj
                adj_return = (target_upside_pct - total_drag_pct) if target_upside_pct is not None else None

                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": "APPROXIMATE_HEURISTIC_UNVERIFIED",
                    "simulation_method": "APPROXIMATE_LIQUIDITY_HEURISTIC",
                    "is_depth_walk": False,
                    "impact_model": "LINEAR_RESERVE_HEURISTIC_APPROXIMATION",
                    "impact_model_disclaimer": "Linear liquidity estimate (trade_size / half_liquidity); not a constant-product AMM simulation or order book depth walk.",
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "route_available": False,
                    "quote_freshness": "FRESH" if quote_age_seconds is not None else "UNVERIFIED_TIMESTAMP",
                    "quote_age_seconds": round(quote_age_seconds, 1) if quote_age_seconds is not None else None,
                    "available_liquidity_usd": liquidity_usd,
                    "buy": {"status": "UNAVAILABLE", "reason": "Approximate heuristic only; no verified route"},
                    "sell": {"status": "UNAVAILABLE", "reason": "Approximate heuristic only; no verified route"},
                    "estimated_round_trip_cost_pct": round(total_drag_pct, 2),
                    "execution_adjusted_return_pct": round(adj_return, 2) if adj_return is not None else None,
                }
            else:
                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": "NO_QUOTE_OR_BOOK_DATA",
                    "simulation_method": "NO_DEPTH_DATA",
                    "is_depth_walk": False,
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "route_available": False,
                    "buy": {"status": "UNAVAILABLE", "reason": "No order book depth or route quotes provided"},
                    "sell": {"status": "UNAVAILABLE", "reason": "No order book depth or route quotes provided"},
                }

    return {
        "schema": "crypto-prebreakout.multi-size-execution-simulation.v1",
        "simulator_version": BACKTEST_VERSION if "BACKTEST_VERSION" in globals() else "4.30.0",
        "execution_enabled": False,
        "available_liquidity_usd": liquidity_usd,
        "fee_bps": fee_bps,
        "gas_usd": gas_usd,
        "target_upside_pct": target_upside_pct,
        "size_evaluations": size_evaluations,
        "research_disclaimer": (
            "Read-only simulation. Evaluates hypothetical size-dependent price impact and execution drag. "
            "Displayed liquidity is subject to cancellation, front-running, and MEV sandwich attacks."
        ),
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("book_json", help="saved JSON order book with bids/asks")
    p.add_argument("--notional", type=float, required=True, help="requested buy quote notional")
    p.add_argument("--fee-bps", type=float, default=0.0, help="fee on each side in basis points")
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    try:
        with open(args.book_json, encoding="utf-8") as fh:
            doc = json.load(fh)
        book = _find_book(doc)
        if not book:
            raise ValueError("input JSON contains no bids/asks order book")
        result = simulate(book, args.notional, args.fee_bps)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print("Execution simulation failed: %s" % exc, file=sys.stderr)
        return 2
    rendered = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("book_json", help="saved JSON order book with bids/asks")
    p.add_argument("--notional", type=float, required=True, help="requested buy quote notional")
    p.add_argument("--fee-bps", type=float, default=0.0, help="fee on each side in basis points")
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    try:
        with open(args.book_json, encoding="utf-8") as fh:
            doc = json.load(fh)
        book = _find_book(doc)
        if not book:
            raise ValueError("input JSON contains no bids/asks order book")
        result = simulate(book, args.notional, args.fee_bps)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print("Execution simulation failed: %s" % exc, file=sys.stderr)
        return 2
    rendered = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
