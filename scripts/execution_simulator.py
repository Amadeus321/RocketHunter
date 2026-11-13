#!/usr/bin/env python3
"""Estimate marketable spot entry/exit VWAP and book impact from a saved snapshot.

This is a read-only order-book simulation. It does not submit orders, predict fills,
or guarantee that displayed depth will remain available.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()


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


def simulate_multi_size(
    source: dict[str, Any],
    *,
    sizes_usd: tuple[float, ...] | list[float] = DEFAULT_RESEARCH_NOTIONAL_SIZES_USD,
    fee_bps: float = 30.0,
    gas_usd: float | None = None,
    as_of_utc: str | None = None,
    max_quote_age_seconds: float = 300.0,
    target_upside_pct: float | None = None,
) -> dict[str, Any]:
    """Evaluate multi-size read-only execution scenarios ($100, $500, $1k, $5k).

    Never assumes the same token has identical execution cost across sizes.
    Never assumes a buy quote proves a sell route is possible.
    """
    now = None
    if as_of_utc:
        try:
            now = dt_parse = None
            import datetime as _dt
            if as_of_utc.endswith("Z"):
                now = _dt.datetime.fromisoformat(as_of_utc.replace("Z", "+00:00"))
            else:
                now = _dt.datetime.fromisoformat(as_of_utc)
        except Exception:
            now = None

    book = _find_book(source)
    quotes = source.get("dex_execution_quotes") if isinstance(source.get("dex_execution_quotes"), dict) else (
        source if "buy_quote" in source or "sell_quote" in source else None
    )

    size_evaluations: dict[str, Any] = {}
    liquidity_usd = None
    if isinstance(source, dict):
        liquidity_usd = (
            float(source.get("liquidity_usd"))
            if source.get("liquidity_usd") is not None and math.isfinite(float(source.get("liquidity_usd")))
            else None
        )

    # Check timestamp freshness if available
    quote_stamp = None
    quote_age_seconds = None
    raw_stamp = (
        (quotes.get("timestamp_utc") if quotes else None) or
        (book.get("timestamp_utc") if book else None) or
        source.get("quote_timestamp_utc") or
        source.get("final_price_timestamp_utc")
    )
    if raw_stamp and now:
        try:
            import datetime as _dt
            if str(raw_stamp).endswith("Z"):
                quote_dt = _dt.datetime.fromisoformat(str(raw_stamp).replace("Z", "+00:00"))
            else:
                quote_dt = _dt.datetime.fromisoformat(str(raw_stamp))
            quote_age_seconds = max(0.0, (now - quote_dt).total_seconds())
        except Exception:
            quote_age_seconds = None

    is_stale = quote_age_seconds is not None and quote_age_seconds > max_quote_age_seconds

    for size in sizes_usd:
        size_key = f"${int(size) if size.is_integer() else size}"
        if is_stale:
            size_evaluations[size_key] = {
                "size_usd": size,
                "status": "STALE_QUOTE",
                "route_available": False,
                "quote_freshness": "STALE",
                "quote_age_seconds": round(quote_age_seconds, 1) if quote_age_seconds else None,
                "buy": {"status": "UNAVAILABLE", "reason": "Quote older than maximum allowed age"},
                "sell": {"status": "UNAVAILABLE", "reason": "Quote older than maximum allowed age"},
                "estimated_round_trip_cost_pct": None,
                "execution_adjusted_return_pct": None,
            }
            continue

        if book:
            # Order book depth simulation
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
                    "route_available": buy_complete and sell_complete,
                    "quote_freshness": "FRESH" if quote_age_seconds is not None else "UNVERIFIED_TIMESTAMP",
                    "quote_age_seconds": round(quote_age_seconds, 1) if quote_age_seconds else None,
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
                    "route_available": False,
                    "reason": str(e),
                    "buy": {"status": "UNAVAILABLE"},
                    "sell": {"status": "UNAVAILABLE"},
                }
        elif quotes:
            # Router quote evidence evaluation
            buy_q = quotes.get("buy_quote")
            sell_q = quotes.get("sell_quote")
            has_buy = isinstance(buy_q, dict) and bool(buy_q)
            has_sell = isinstance(sell_q, dict) and bool(sell_q)

            if not has_buy:
                status = "MISSING_BUY_ROUTE"
            elif not has_sell:
                # Critical Web3 Rule: A buy quote never proves a sell route exists!
                status = "MISSING_SELL_ROUTE"
            elif liquidity_usd is not None and liquidity_usd < (size * 2.0):
                status = "INSUFFICIENT_LIQUIDITY"
            else:
                status = "EXECUTABLE_ROUTE_AVAILABLE"

            # Estimate AMM constant product impact for size relative to pool liquidity if modeled
            impact_bps = None
            if liquidity_usd and liquidity_usd > 0:
                # In x*y=k pool, price impact ~= trade_size / (pool_liquidity / 2)
                impact_pct = (size / (liquidity_usd / 2.0)) * 100.0
                impact_bps = round(impact_pct * 100.0, 1)

            fee_cost = (size * 2.0) * (fee_bps / 10000.0)
            slip_pct = (impact_bps / 100.0) if impact_bps is not None else 1.5
            gas_adj = (gas_usd / size * 100.0) if gas_usd and size > 0 else 0.0
            total_drag_pct = slip_pct + (fee_cost / size * 100.0) + gas_adj
            adj_return = (target_upside_pct - total_drag_pct) if target_upside_pct is not None else None

            size_evaluations[size_key] = {
                "size_usd": size,
                "status": status,
                "route_available": bool(has_buy and has_sell and status != "INSUFFICIENT_LIQUIDITY"),
                "quote_freshness": "FRESH" if quote_age_seconds is not None else "UNVERIFIED_TIMESTAMP",
                "quote_age_seconds": round(quote_age_seconds, 1) if quote_age_seconds else None,
                "available_liquidity_usd": liquidity_usd,
                "buy": {
                    "status": "AVAILABLE" if has_buy else "UNAVAILABLE",
                    "provider": quotes.get("provider"),
                    "estimated_price_impact_bps": impact_bps,
                },
                "sell": {
                    "status": "AVAILABLE" if has_sell else "UNAVAILABLE",
                    "provider": quotes.get("provider"),
                    "estimated_price_impact_bps": impact_bps,
                    "sell_route_independently_verified": has_sell,
                },
                "trading_fee_usd": round(fee_cost, 2),
                "network_gas_usd": gas_usd,
                "estimated_round_trip_cost_pct": round(total_drag_pct, 2) if total_drag_pct is not None else None,
                "execution_adjusted_return_pct": round(adj_return, 2) if adj_return is not None else None,
            }
        else:
            # Neither order book nor router quotes present
            size_evaluations[size_key] = {
                "size_usd": size,
                "status": "NO_QUOTE_OR_BOOK_DATA",
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
