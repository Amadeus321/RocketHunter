#!/usr/bin/env python3
"""Estimate marketable spot entry/exit VWAP and book impact from a saved snapshot.

This is a read-only order-book simulation. It does not submit orders, predict fills,
or guarantee that displayed depth will remain available.
"""
import argparse
import json
import math
import sys
from pathlib import Path

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
