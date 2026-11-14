"""Pre-breakout order book depth and multi-size execution simulator.

Provides read-only simulation of market impact, slippage, and round-trip execution
costs across notional trade sizes ($100, $500, $1k, $5k). Distinguishes actual order
book depth walks, constant-product AMM simulations, and size-verified router quotes
from approximate linear liquidity heuristics.

Never enables automated execution, signing, private-key handling, or live orders.
Fails closed whenever quote freshness, identity, size, reserve, or fee evidence is unverified.
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
from datetime import datetime, timezone
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()
BACKTEST_VERSION = VERSION

try:
    from scripts.address_identity import same_address
except ImportError:
    try:
        from address_identity import same_address
    except ImportError:
        def same_address(chain: Any, left: Any, right: Any) -> bool:
            return bool(left and right and str(left).strip().lower() == str(right).strip().lower())


def validate_fee_bps(fee_bps: Any) -> float:
    """Validate trading fee in basis points (bps).

    Units: basis points (bps), where 1 bps = 0.01% (100 bps = 1.0%).
    Valid range: 0.0 <= fee_bps < 10000.0 (i.e. 0% to strictly less than 100%).
    Rejects None, booleans, non-numeric strings, negative values, NaNs, infinities,
    and out-of-range values (>= 10000.0 bps).
    Never clamps or silently defaults invalid fees.
    """
    if fee_bps is None or isinstance(fee_bps, bool):
        raise ValueError("INVALID_FEE_DATA: fee_bps cannot be None or boolean")
    try:
        f = float(fee_bps)
    except (TypeError, ValueError):
        raise ValueError(f"INVALID_FEE_DATA: fee_bps must be numeric, got {fee_bps!r}")
    if not math.isfinite(f):
        raise ValueError(f"INVALID_FEE_DATA: fee_bps must be finite, got {f}")
    if f < 0.0:
        raise ValueError(f"INVALID_FEE_DATA: fee_bps cannot be negative, got {f}")
    if f >= 10000.0:
        raise ValueError(f"INVALID_FEE_DATA: fee_bps must be less than 10000.0 bps (100%), got {f}")
    return f


def is_valid_address(chain: Any, address: Any) -> bool:
    """Validate address syntax for specified chain.

    Verifies structural format conformity against chain-specific encoding specifications:
    - EVM: strictly '0x' followed by exactly 40 hexadecimal characters (case-insensitive, 42 chars total).
      Non-hexadecimal characters and lengths other than 42 are strictly rejected.
    - Solana: strictly Base58 string of 32 to 44 characters ([1-9A-HJ-NP-Za-km-z]{32,44}).
    - Generic/other: strictly 32 to 128 alphanumeric characters ([0-9a-zA-Z_-]{32,128}).

    Rejects non-string, empty, whitespace-padded, whitespace-containing, separator-containing
    (:, /, \\, |, ?, #, ;, ,), non-hexadecimal EVM, or bare symbol inputs.

    Note: Address format validation verifies structural conformity against encoding
    specifications; it does NOT constitute cryptographic or on-chain proof that an address or
    contract currently exists or is deployed on-chain.
    """
    if not isinstance(address, str) or not address:
        return False
    if address.strip() != address:
        return False
    # Reject whitespace or separators/punctuation
    for ch in (" ", "\t", "\n", "\r", "|", ":", "/", "\\", "?", "#", ";", ","):
        if ch in address:
            return False

    chain_key = str(chain or "").strip().lower()
    if chain_key in {"solana", "sol"}:
        return bool(re.fullmatch(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", address))
    elif chain_key in {
        "ethereum", "eth", "base", "bsc", "bnb", "polygon", "polygon_pos",
        "arbitrum", "optimism", "avalanche", "avax", "fantom", "ftm",
        "linea", "scroll", "zksync", "mantle", "blast", "mode", "opbnb"
    } or address.startswith("0x"):
        # EVM: strictly 0x followed by exactly 40 hex digits (total length 42)
        if len(address) != 42 or not address.startswith("0x"):
            return False
        return bool(re.fullmatch(r"^0x[0-9a-fA-F]{40}$", address))
    else:
        # Non-EVM / generic
        return bool(re.fullmatch(r"^[0-9a-zA-Z_-]{32,128}$", address))


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


def simulate(
    book: dict[str, Any],
    buy_notional_usd: float,
    fee_bps: float = 0.0,
    *,
    quote_usd_conversion: dict[str, Any] | None = None,
    quote_usd_price: float | None = None,
    expected_chain: str | None = None,
    expected_quote_asset: str | None = None,
    as_of_utc: str | None = None,
    max_conversion_age_seconds: float = 300.0,
    require_quote_conversion: bool = False,
    require_quote_denomination: bool = False,
) -> dict[str, Any]:
    """Simulate order book walk for buy and immediate sell with quote-currency USD normalization."""
    try:
        buy_notional_usd = float(buy_notional_usd)
    except (TypeError, ValueError):
        raise ValueError("buy_notional_usd must be numeric")
    if not math.isfinite(buy_notional_usd) or buy_notional_usd <= 0:
        raise ValueError("buy_notional_usd must be positive and finite")

    fee_bps = validate_fee_bps(fee_bps)

    # Establish quote-currency denomination
    denom = (
        book.get("quote_currency") or
        book.get("denomination") or
        book.get("quote_asset") or
        book.get("quote_token") or
        book.get("quote_symbol")
    )
    if require_quote_denomination or require_quote_conversion or quote_usd_conversion is not None:
        if not denom:
            raise ValueError("MISSING_QUOTE_DENOMINATION: order-book snapshot missing quote-currency denomination")

    # Resolve conversion rate
    conv = quote_usd_conversion or (book.get("quote_usd_conversion") if isinstance(book, dict) else None)
    if require_quote_conversion and not conv:
        raise ValueError("MISSING_QUOTE_CONVERSION_EVIDENCE: quote-asset USD conversion evidence is required; bare numeric quote_usd_price cannot satisfy this requirement")

    if conv:
        ok, status, rate, details = validate_quote_usd_conversion(
            conv,
            expected_chain=expected_chain or book.get("chain"),
            expected_quote_asset=expected_quote_asset or book.get("quote_asset") or book.get("quote_token"),
            as_of_utc=as_of_utc,
            max_conversion_age_seconds=max_conversion_age_seconds,
        )
        if not ok:
            raise ValueError(f"{status}: {details.get('reason')}")
        eff_quote_price = rate
    elif quote_usd_price is not None:
        p = _clean_strictly_positive(quote_usd_price)
        if p is None:
            raise ValueError("INVALID_QUOTE_CONVERSION_PRICE: quote_usd_price must be positive and finite")
        eff_quote_price = p
    elif require_quote_conversion:
        raise ValueError("MISSING_QUOTE_CONVERSION_EVIDENCE: quote-asset USD conversion evidence is required")
    else:
        eff_quote_price = 1.0

    bids = _levels(book.get("bids"), reverse=True)
    asks = _levels(book.get("asks"), reverse=False)
    if not bids or not asks:
        raise ValueError("book must contain at least one valid bid and ask")
    best_bid, best_ask = bids[0][0], asks[0][0]
    if best_bid >= best_ask:
        raise ValueError("crossed or locked book; inspect snapshot before simulation")
    mid = (best_bid + best_ask) / 2.0

    # Measure quote depth within +/-2% of mid in quote tokens and USD
    bid_depth_2pct_tokens = sum(p * q for p, q in bids if p >= mid * 0.98)
    ask_depth_2pct_tokens = sum(p * q for p, q in asks if p <= mid * 1.02)
    bid_depth_2pct = bid_depth_2pct_tokens * eff_quote_price
    ask_depth_2pct = ask_depth_2pct_tokens * eff_quote_price

    # Convert USD trade notional into quote-token units
    buy_notional_quote_tokens = buy_notional_usd / eff_quote_price

    # Walk asks until requested quote spend is reached
    remaining_quote = buy_notional_quote_tokens
    buy_qty = buy_quote_tokens = 0.0
    buy_fills = []
    for price, available_qty in asks:
        if remaining_quote <= 1e-10:
            break
        take_qty = min(available_qty, remaining_quote / price)
        if take_qty > 0:
            spent = take_qty * price
            buy_fills.append({
                "price": price * eff_quote_price,
                "price_quote": price,
                "quantity": take_qty,
                "quote": spent * eff_quote_price,
                "quote_tokens": spent,
            })
            buy_qty += take_qty
            buy_quote_tokens += spent
            remaining_quote -= spent
    buy_complete = remaining_quote <= max(1e-8, buy_notional_quote_tokens * 1e-9)

    # Immediate sale of quantity purchased against bid side
    sell = _consume(bids, buy_qty)
    sell_complete = sell["remaining_quantity"] <= max(1e-12, buy_qty * 1e-9)
    sell_quote_tokens = sell["quote_notional"]

    # Convert back to USD
    buy_quote_usd = buy_quote_tokens * eff_quote_price
    sell_quote_usd = sell_quote_tokens * eff_quote_price
    buy_vwap_quote = buy_quote_tokens / buy_qty if buy_qty else None
    buy_vwap_usd = buy_vwap_quote * eff_quote_price if buy_vwap_quote else None
    sell_vwap_quote = sell["vwap"]
    sell_vwap_usd = sell_vwap_quote * eff_quote_price if sell_vwap_quote else None
    mid_usd = mid * eff_quote_price

    gross_pnl_usd = (sell_quote_usd - buy_quote_usd) if sell_vwap_quote is not None else None
    fee_cost_usd = (buy_quote_usd + sell_quote_usd) * fee_bps / 10000.0 if gross_pnl_usd is not None else None
    net_pnl_usd = gross_pnl_usd - fee_cost_usd if gross_pnl_usd is not None else None

    return {
        "schema": "crypto-prebreakout.execution-simulation.v1",
        "skill_version": VERSION,
        "status": "FULL_ROUND_TRIP_DEPTH_AVAILABLE" if buy_complete and sell_complete else "INSUFFICIENT_DISPLAYED_DEPTH",
        "execution_enabled": False,
        "snapshot": book.get("timestamp_utc") or book.get("timestamp") or "NOT PROVIDED",
        "quote_denomination": denom,
        "quote_usd_price": eff_quote_price,
        "mid_price": mid_usd,
        "mid_price_quote": mid,
        "best_bid": best_bid * eff_quote_price,
        "best_bid_quote": best_bid,
        "best_ask": best_ask * eff_quote_price,
        "best_ask_quote": best_ask,
        "spread_bps": (best_ask - best_bid) / mid * 10000.0,
        "requested_buy_notional_usd": buy_notional_usd,
        "buy": {
            "filled_quote_usd": buy_quote_usd,
            "filled_quote_tokens": buy_quote_tokens,
            "filled_quantity": buy_qty,
            "vwap": buy_vwap_usd,
            "vwap_quote": buy_vwap_quote,
            "remaining_quote_usd": max(0.0, remaining_quote * eff_quote_price),
            "complete": buy_complete,
            "impact_vs_best_ask_bps": ((buy_vwap_quote / best_ask) - 1.0) * 10000.0 if buy_vwap_quote else None,
            "slippage_vs_mid_bps": ((buy_vwap_quote / mid) - 1.0) * 10000.0 if buy_vwap_quote else None,
            "fills": buy_fills,
        },
        "immediate_exit": {
            "filled_quote_usd": sell_quote_usd,
            "filled_quote_tokens": sell_quote_tokens,
            "filled_quantity": sell["filled_quantity"],
            "vwap": sell_vwap_usd,
            "vwap_quote": sell_vwap_quote,
            "remaining_quantity": sell["remaining_quantity"],
            "complete": sell_complete,
            "impact_vs_best_bid_bps": ((best_bid / sell_vwap_quote) - 1.0) * 10000.0 if sell_vwap_quote else None,
            "slippage_vs_mid_bps": ((mid / sell_vwap_quote) - 1.0) * 10000.0 if sell_vwap_quote else None,
            "fills": sell["fills"],
        },
        "depth_within_2pct_usd": {
            "bids": bid_depth_2pct,
            "asks": ask_depth_2pct,
            "limiting_side": min(bid_depth_2pct, ask_depth_2pct),
        },
        "round_trip_estimate": {
            "fee_bps_each_side": fee_bps,
            "gross_pnl_usd": gross_pnl_usd,
            "estimated_fee_usd": fee_cost_usd,
            "net_pnl_usd": net_pnl_usd,
            "net_return_pct": (net_pnl_usd / buy_quote_usd * 100.0) if net_pnl_usd is not None and buy_quote_usd else None,
        },
        "limitations": [
            "single order-book snapshot; displayed orders may cancel or move",
            "immediate round-trip simulation is not a future exit-price forecast",
            "does not model hidden liquidity, queue priority, latency, adverse selection or dynamic impact",
            "fees are user-supplied and may differ by tier, venue, token or order type",
        ],
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

    if raw_stamp is None or raw_stamp == "":
        return False, "MISSING_EXECUTION_QUOTE_TIMESTAMP", None

    quote_dt = _parse_iso_utc(raw_stamp)
    if quote_dt is None:
        if isinstance(raw_stamp, str) and ("T" in raw_stamp or "-" in raw_stamp):
            try:
                naive = datetime.fromisoformat(raw_stamp)
                if naive.tzinfo is None:
                    return False, "NAIVE_TIMESTAMP_REJECTED", None
            except Exception:
                pass
        return False, "INVALID_EXECUTION_QUOTE_TIMESTAMP", None

    age_seconds = (as_of_dt - quote_dt).total_seconds()
    if age_seconds < -1e-6:
        return False, "FUTURE_DATED_QUOTE", age_seconds
    if age_seconds > max_quote_age_seconds:
        return False, "STALE_QUOTE", age_seconds

    return True, "FRESH", age_seconds


# Trusted Provider and Valuation Policy Registries
RECOGNIZED_VALUATION_PROVIDERS: dict[str, dict[str, Any]] = {
    "chainlink": {
        "name": "Chainlink Decentralized Oracle Network",
        "aliases": {"chainlink", "chainlink_oracle", "chainlink_feed", "chainlink_oracle_usd", "chainlink_oracle_eth_usd"},
        "requires_feed_provenance": True,
    },
    "pyth": {
        "name": "Pyth Network High-Fidelity On-Demand Oracle",
        "aliases": {"pyth", "pyth_network", "pyth_oracle"},
        "requires_feed_provenance": True,
    },
    "uniswap_v3_twap": {
        "name": "Uniswap v3 On-Chain TWAP Oracle",
        "aliases": {"uniswap_v3_twap", "uniswap_twap", "uni_v3_twap"},
        "requires_feed_provenance": True,
    },
    "defillama": {
        "name": "DefiLlama Multi-Chain Price API",
        "aliases": {"defillama", "defillama_prices", "defillama_api"},
        "requires_feed_provenance": True,
    },
    "geckoterminal": {
        "name": "GeckoTerminal Validated Token Price API",
        "aliases": {"geckoterminal", "geckoterminal_api", "geckoterminal_price"},
        "requires_feed_provenance": True,
    },
    "dexscreener": {
        "name": "DexScreener Multi-Chain Pair API",
        "aliases": {"dexscreener", "dexscreener_api", "dexscreener_price"},
        "requires_feed_provenance": True,
    },
    "coinbase": {
        "name": "Coinbase Institutional Public Market Reference",
        "aliases": {"coinbase", "coinbase_oracle", "coinbase_pro"},
        "requires_feed_provenance": True,
    },
    "binance": {
        "name": "Binance Public Market Reference Ticker",
        "aliases": {"binance", "binance_ticker", "binance_market"},
        "requires_feed_provenance": True,
    },
    "kraken": {
        "name": "Kraken Public Market Reference Ticker",
        "aliases": {"kraken", "kraken_ticker"},
        "requires_feed_provenance": True,
    },
}

ALL_RECOGNIZED_PROVIDER_ALIASES: set[str] = set()
for prov_key, prov_data in RECOGNIZED_VALUATION_PROVIDERS.items():
    ALL_RECOGNIZED_PROVIDER_ALIASES.add(prov_key)
    ALL_RECOGNIZED_PROVIDER_ALIASES.update(prov_data.get("aliases", set()))

RECOGNIZED_VALUATION_POLICIES: dict[str, str] = {
    "STABLECOIN_ORACLE_VERIFIED": "Empirical price reported by trusted oracle for a fiat-backed or multi-collateral stablecoin; never assumes hardcoded $1.00.",
    "CRYPTO_ORACLE_VERIFIED": "Empirical price reported by trusted oracle for a native/crypto quote asset (e.g. WETH, SOL).",
    "ON_CHAIN_TWAP_VERIFIED": "Time-weighted average price verified directly from on-chain liquidity pool reserves.",
    "DEX_AGGREGATOR_VERIFIED": "Multi-venue volume-weighted market reference from trusted indexer/aggregator.",
    "CEX_MARKET_TICKER_VERIFIED": "Consolidated public market reference from tier-1 centralized exchange ticker.",
    "STABLECOIN_DEVIATION_BAND_POLICY": "Empirical stablecoin valuation verified to reside within acceptable depeg tolerance bands without assuming constant $1.00.",
}


def validate_quote_usd_conversion(
    conversion: Any,
    *,
    expected_chain: str | None = None,
    expected_quote_asset: str | None = None,
    as_of_utc: str | None = None,
    max_conversion_age_seconds: float = 300.0,
) -> tuple[bool, str, float | None, dict[str, Any]]:
    """Validate quote-asset/USD conversion evidence and provenance.

    Requires:
    - conversion evidence dictionary with explicit price_usd, timestamp_utc, chain, and quote_asset.
    - price_usd must be positive and finite.
    - chain must match expected_chain.
    - quote_asset must match expected_quote_asset and have syntactically valid address format.
    - timestamp_utc must be timezone-aware UTC, not future-dated, and fresh relative to as_of_utc.
    - source/provider must identify a recognized trusted valuation provider; arbitrary strings rejected (UNVERIFIED_VALUATION_SOURCE).
    - valuation_policy must match an approved policy; arbitrary or self-declared pegs rejected (UNRECOGNIZED_VALUATION_POLICY / UNVERIFIED_VALUATION_SOURCE).
    - feed/provenance metadata must bind the price to the exact quote-token address and chain (MISSING_CONVERSION_PROVENANCE).
    - does not claim cryptographic authenticity unless verified.

    Returns:
    (is_valid, status, price_usd, details)
    """
    if not isinstance(conversion, dict) or not conversion:
        return False, "MISSING_QUOTE_CONVERSION_EVIDENCE", None, {
            "reason": "quote-asset USD conversion evidence is missing"
        }

    # 1. Price validation
    raw_price = conversion.get("price_usd")
    if raw_price is None and "rate" in conversion:
        raw_price = conversion["rate"]
    if raw_price is None and "quote_price_usd" in conversion:
        raw_price = conversion["quote_price_usd"]

    clean_price = _clean_strictly_positive(raw_price)
    if clean_price is None:
        return False, "INVALID_QUOTE_CONVERSION_PRICE", None, {
            "reason": f"quote-asset USD conversion price must be positive and finite, got {raw_price!r}"
        }

    # 2. Timestamp freshness validation
    stamp = conversion.get("timestamp_utc") or conversion.get("timestamp") or conversion.get("as_of_utc")
    fresh, fresh_status, age = validate_timestamp_freshness(stamp, as_of_utc, max_conversion_age_seconds)
    if not fresh:
        if fresh_status in ("FUTURE_DATED_QUOTE", "FUTURE_DATED"):
            status_code = "FUTURE_DATED_QUOTE_CONVERSION"
        elif fresh_status in ("STALE_QUOTE", "STALE"):
            status_code = "STALE_QUOTE_CONVERSION"
        else:
            status_code = "INVALID_QUOTE_CONVERSION_TIMESTAMP"
        return False, status_code, None, {
            "reason": f"quote-asset USD conversion timestamp verification failed: {fresh_status}",
            "age_seconds": age,
        }

    # 3. Chain validation
    conv_chain = conversion.get("chain")
    if expected_chain:
        if not conv_chain:
            return False, "MISSING_CONVERSION_CHAIN_IDENTITY", None, {
                "reason": "quote-asset USD conversion evidence missing chain identity"
            }
        if str(conv_chain).strip().lower() != str(expected_chain).strip().lower():
            return False, "QUOTE_CONVERSION_CHAIN_MISMATCH", None, {
                "reason": f"conversion chain ({conv_chain}) does not match expected ({expected_chain})",
                "actual": conv_chain,
                "expected": expected_chain,
            }

    # 4. Quote asset address validation
    conv_asset = (
        conversion.get("quote_asset") or
        conversion.get("quote_token") or
        conversion.get("token_address") or
        conversion.get("address")
    )
    effective_chain = expected_chain or conv_chain or "evm"
    if expected_quote_asset:
        if not conv_asset:
            return False, "MISSING_CONVERSION_ASSET_IDENTITY", None, {
                "reason": "quote-asset USD conversion evidence missing quote asset address"
            }
        if not is_valid_address(effective_chain, conv_asset):
            return False, "INVALID_TOKEN_ADDRESS", None, {
                "reason": f"conversion quote asset address format invalid: {conv_asset}"
            }
        if not same_address(effective_chain, conv_asset, expected_quote_asset):
            return False, "QUOTE_CONVERSION_ASSET_MISMATCH", None, {
                "reason": f"conversion quote asset ({conv_asset}) does not match expected ({expected_quote_asset})",
                "actual": conv_asset,
                "expected": expected_quote_asset,
            }
    elif conv_asset:
        if not is_valid_address(effective_chain, conv_asset):
            return False, "INVALID_TOKEN_ADDRESS", None, {
                "reason": f"conversion quote asset address format invalid: {conv_asset}"
            }

    # 5. Valuation source / provider validation
    raw_source = conversion.get("source")
    raw_provider = conversion.get("provider")
    raw_oracle = conversion.get("oracle")

    if not raw_source and not raw_provider and not raw_oracle:
        return False, "UNVERIFIED_VALUATION_SOURCE", None, {
            "reason": "quote-asset USD conversion lacks explicit valuation source or provider"
        }

    if raw_source:
        norm_source = str(raw_source).strip().lower()
        if norm_source not in ALL_RECOGNIZED_PROVIDER_ALIASES:
            return False, "UNVERIFIED_VALUATION_SOURCE", None, {
                "reason": f"arbitrary or unrecognized valuation source: {raw_source!r}"
            }

    if raw_provider:
        norm_provider = str(raw_provider).strip().lower()
        if norm_provider not in ALL_RECOGNIZED_PROVIDER_ALIASES:
            return False, "UNVERIFIED_VALUATION_SOURCE", None, {
                "reason": f"arbitrary or unrecognized valuation provider: {raw_provider!r}"
            }

    if raw_oracle:
        norm_oracle = str(raw_oracle).strip().lower()
        if norm_oracle not in ALL_RECOGNIZED_PROVIDER_ALIASES:
            return False, "UNVERIFIED_VALUATION_SOURCE", None, {
                "reason": f"arbitrary or unrecognized valuation oracle: {raw_oracle!r}"
            }

    # 6. Valuation policy validation
    raw_policy = conversion.get("valuation_policy") or conversion.get("policy")
    if not raw_policy:
        return False, "UNRECOGNIZED_VALUATION_POLICY", None, {
            "reason": "quote-asset USD conversion lacks explicit valuation policy"
        }
    norm_policy = str(raw_policy).strip().upper()
    if norm_policy not in RECOGNIZED_VALUATION_POLICIES:
        return False, "UNRECOGNIZED_VALUATION_POLICY", None, {
            "reason": f"unrecognized valuation policy: {raw_policy!r}"
        }

    # Reject self-declared peg or assumed constant valuations
    if conversion.get("self_declared_peg") is True or conversion.get("assumed_peg") is True or norm_policy == "SELF_DECLARED_PEG":
        return False, "UNVERIFIED_VALUATION_SOURCE", None, {
            "reason": "self-declared stablecoin peg rejected; empirical oracle valuation required"
        }

    # 7. Feed / Provenance metadata binding
    provenance_id = (
        conversion.get("feed_address") or
        conversion.get("feed_id") or
        conversion.get("aggregator_address") or
        conversion.get("oracle_address") or
        conversion.get("pool_id") or
        conversion.get("pair_id") or
        conversion.get("pair_address") or
        conversion.get("market_id") or
        conversion.get("source_url") or
        conversion.get("query_id")
    )
    if not provenance_id or not str(provenance_id).strip():
        return False, "MISSING_CONVERSION_PROVENANCE", None, {
            "reason": "missing valuation provenance: feed_address, feed_id, pair_id, pool_id, or source_url required to bind USD price to asset"
        }
    prov_str = str(provenance_id).strip()

    if "feed_address" in conversion or "aggregator_address" in conversion or "oracle_address" in conversion:
        addr_val = str(conversion.get("feed_address") or conversion.get("aggregator_address") or conversion.get("oracle_address")).strip()
        if not is_valid_address(effective_chain, addr_val):
            return False, "INVALID_TOKEN_ADDRESS", None, {
                "reason": f"invalid oracle feed address format for chain {effective_chain}: {addr_val}"
            }

    return True, "CONVERSION_VERIFIED", clean_price, {
        "price_usd": clean_price,
        "chain": conv_chain,
        "quote_asset": conv_asset,
        "source": str(raw_source or raw_provider or raw_oracle).strip(),
        "provider": str(raw_provider or raw_source or raw_oracle).strip(),
        "valuation_policy": norm_policy,
        "provenance_id": prov_str,
        "age_seconds": round(age, 1) if age is not None else None,
        "verification_method": "TRUSTED_PROVIDER_FEED_METADATA_AND_FRESHNESS_VERIFIED",
        "cryptographic_authenticity_verified": False,
    }


def validate_two_way_quotes(
    buy_q: Any,
    sell_q: Any,
    *,
    expected_token_address: str | None = None,
    expected_chain: str | None = None,
    expected_pool: str | None = None,
    expected_quote_asset: str | None = None,
    require_pool_identity: bool = False,
    require_quote_asset: bool = False,
    quote_usd_price: float | None = None,
    target_size_usd: float | None = None,
    size_tolerance_pct: float = 1.0,
    quantity_tolerance_pct: float = 0.1,
    quote_meta: dict[str, Any] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Validate both sides of a router trade quote.

    Enforces:
    - Explicit 'BUY' direction on buy quote and 'SELL' direction on sell quote.
    - Explicit chain identity on both legs matching expected_chain.
    - Explicit pool identity on both legs where required/supplied matching expected_pool.
    - Explicit token addresses on both legs, verified swap directions:
        buy: expected quote asset -> candidate token
        sell: candidate token -> expected quote asset
    - Candidate token != quote asset.
    - Positive finite input and output amounts on both quote legs.
    - Round-trip token quantity reconciliation: buy output tokens must match sell input tokens
      within quantity_tolerance_pct (default 0.1%).
    - Both buy notional size and sell notional size verified against target_size_usd within tolerance.
    """
    if not isinstance(buy_q, dict) or not buy_q:
        return False, "MISSING_BUY_ROUTE", {"reason": "missing buy quote"}
    if not isinstance(sell_q, dict) or not sell_q:
        return False, "MISSING_SELL_ROUTE", {"reason": "missing sell quote"}

    # 1. Explicit swap direction on both legs
    buy_dir = str(buy_q.get("direction") or "").strip().upper()
    if buy_dir != "BUY":
        return False, "DIRECTION_MISMATCH", {"reason": f"buy_quote direction must be explicitly 'BUY', got {buy_q.get('direction')!r}"}

    sell_dir = str(sell_q.get("direction") or "").strip().upper()
    if sell_dir != "SELL":
        return False, "DIRECTION_MISMATCH", {"reason": f"sell_quote direction must be explicitly 'SELL', got {sell_q.get('direction')!r}"}

    # 2. Chain validation on both legs
    buy_chain = buy_q.get("chain")
    sell_chain = sell_q.get("chain")
    if not buy_chain or not sell_chain:
        return False, "MISSING_CHAIN_IDENTITY", {
            "reason": "missing explicit chain identity on quote leg",
            "buy_chain": buy_chain,
            "sell_chain": sell_chain,
        }
    if str(buy_chain).strip().lower() != str(sell_chain).strip().lower():
        return False, "CHAIN_MISMATCH", {
            "reason": "buy and sell quotes specify different chains",
            "buy_chain": buy_chain,
            "sell_chain": sell_chain,
        }
    effective_chain = str(buy_chain).strip().lower()
    if expected_chain and effective_chain != str(expected_chain).strip().lower():
        return False, "CHAIN_MISMATCH", {
            "reason": "quote chain does not match expected_chain",
            "expected": expected_chain,
            "actual": effective_chain,
        }

    # 3. Expected token address validation
    if not expected_token_address:
        return False, "MISSING_TOKEN_ADDRESS", {"reason": "expected_token_address is required for route verification"}
    if not is_valid_address(effective_chain, expected_token_address):
        return False, "INVALID_TOKEN_ADDRESS", {"reason": f"invalid expected token address format: {expected_token_address}"}

    # 4. Pool validation
    buy_pool = buy_q.get("pool_address") or buy_q.get("pool") or buy_q.get("venue")
    sell_pool = sell_q.get("pool_address") or sell_q.get("pool") or sell_q.get("venue")
    if require_pool_identity or expected_pool:
        if not buy_pool or not sell_pool:
            return False, "MISSING_POOL_IDENTITY", {
                "reason": "missing pool identity on quote leg",
                "buy_pool": buy_pool,
                "sell_pool": sell_pool,
            }
        if not is_valid_address(effective_chain, buy_pool):
            return False, "INVALID_TOKEN_ADDRESS", {"reason": f"invalid buy pool address format: {buy_pool}"}
        if not is_valid_address(effective_chain, sell_pool):
            return False, "INVALID_TOKEN_ADDRESS", {"reason": f"invalid sell pool address format: {sell_pool}"}

    if buy_pool and sell_pool:
        if not same_address(effective_chain, buy_pool, sell_pool):
            return False, "POOL_MISMATCH", {
                "reason": "buy and sell quotes specify different pools",
                "buy_pool": buy_pool,
                "sell_pool": sell_pool,
            }
    if expected_pool:
        if buy_pool and not same_address(effective_chain, buy_pool, expected_pool):
            return False, "POOL_MISMATCH", {
                "reason": "buy quote pool does not match expected_pool",
                "expected": expected_pool,
                "actual": buy_pool,
            }
        if sell_pool and not same_address(effective_chain, sell_pool, expected_pool):
            return False, "POOL_MISMATCH", {
                "reason": "sell quote pool does not match expected_pool",
                "expected": expected_pool,
                "actual": sell_pool,
            }

    # 5. Token addresses on both legs
    buy_in_tok = buy_q.get("in_token") or buy_q.get("input_token")
    buy_out_tok = buy_q.get("out_token") or buy_q.get("output_token")
    sell_in_tok = sell_q.get("in_token") or sell_q.get("input_token")
    sell_out_tok = sell_q.get("out_token") or sell_q.get("output_token")

    if not buy_in_tok or not buy_out_tok:
        return False, "MISSING_TOKEN_ADDRESS", {"reason": "buy quote missing in_token or out_token"}
    if not sell_in_tok or not sell_out_tok:
        return False, "MISSING_TOKEN_ADDRESS", {"reason": "sell quote missing in_token or out_token"}

    for addr_label, addr_val in (("buy in_token", buy_in_tok), ("buy out_token", buy_out_tok),
                                 ("sell in_token", sell_in_tok), ("sell out_token", sell_out_tok)):
        if not is_valid_address(effective_chain, addr_val):
            return False, "INVALID_TOKEN_ADDRESS", {"reason": f"invalid token address format for {addr_label}: {addr_val}"}

    # Validate candidate token binding
    if not same_address(effective_chain, buy_out_tok, expected_token_address):
        return False, "TOKEN_ADDRESS_MISMATCH", {
            "reason": "buy out_token does not match candidate token",
            "expected": expected_token_address,
            "actual": buy_out_tok,
        }
    if not same_address(effective_chain, sell_in_tok, expected_token_address):
        return False, "TOKEN_ADDRESS_MISMATCH", {
            "reason": "sell in_token does not match candidate token",
            "expected": expected_token_address,
            "actual": sell_in_tok,
        }

    # Validate quote asset consistency
    if not same_address(effective_chain, buy_in_tok, sell_out_tok):
        return False, "QUOTE_ASSET_MISMATCH", {
            "reason": "buy in_token and sell out_token quote assets differ",
            "buy_in": buy_in_tok,
            "sell_out": sell_out_tok,
        }
    if expected_quote_asset:
        if not is_valid_address(effective_chain, expected_quote_asset):
            return False, "INVALID_TOKEN_ADDRESS", {"reason": f"invalid expected quote asset format: {expected_quote_asset}"}
        if not same_address(effective_chain, buy_in_tok, expected_quote_asset):
            return False, "QUOTE_ASSET_MISMATCH", {
                "reason": "buy in_token does not match expected_quote_asset",
                "expected": expected_quote_asset,
                "actual": buy_in_tok,
            }
        if not same_address(effective_chain, sell_out_tok, expected_quote_asset):
            return False, "QUOTE_ASSET_MISMATCH", {
                "reason": "sell out_token does not match expected_quote_asset",
                "expected": expected_quote_asset,
                "actual": sell_out_tok,
            }
    elif require_quote_asset:
        return False, "MISSING_QUOTE_ASSET_EVIDENCE", {"reason": "expected quote asset is required but was not provided"}

    # Candidate token cannot be the quote asset
    if same_address(effective_chain, buy_out_tok, buy_in_tok):
        return False, "INVALID_TOKEN_ADDRESS", {"reason": "candidate token and quote asset cannot be the same address"}

    # 6. Input and output amounts on both quotes
    def _extract_amount(q: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for k in keys:
            if k in q:
                return q[k]
        return None

    buy_in = _clean_strictly_positive(_extract_amount(buy_q, ("in_amount", "amount_in", "input_amount")))
    buy_out = _clean_strictly_positive(_extract_amount(buy_q, ("out_amount", "amount_out", "output_amount", "filled_quantity")))
    if buy_in is None or buy_out is None:
        return False, "INVALID_BUY_QUOTE_AMOUNTS", {"reason": "buy quote in_amount and out_amount must be positive and finite"}

    sell_in = _clean_strictly_positive(_extract_amount(sell_q, ("in_amount", "amount_in", "input_amount", "filled_quantity")))
    sell_out = _clean_strictly_positive(_extract_amount(sell_q, ("out_amount", "amount_out", "output_amount", "filled_quote_usd")))
    if sell_in is None or sell_out is None:
        return False, "INVALID_SELL_QUOTE_AMOUNTS", {"reason": "sell quote in_amount and out_amount must be positive and finite"}

    # 7. Round-trip token quantity reconciliation: buy output tokens must match sell input tokens
    qty_diff = abs(buy_out - sell_in) / buy_out
    qty_tol = (quantity_tolerance_pct / 100.0) + 1e-9
    if qty_diff > qty_tol:
        return False, "QUANTITY_MISMATCH", {
            "reason": f"buy output quantity ({buy_out}) does not match sell input quantity ({sell_in}) within {quantity_tolerance_pct}% tolerance",
            "buy_out": buy_out,
            "sell_in": sell_in,
            "diff_pct": qty_diff * 100.0,
            "tolerance_pct": quantity_tolerance_pct,
        }

    # 8. Quoted notional size verification on BOTH legs
    if buy_q.get("notional_size_usd") is not None:
        buy_size = _clean_strictly_positive(buy_q.get("notional_size_usd"))
    elif quote_usd_price is not None and buy_in is not None:
        buy_size = buy_in * quote_usd_price
    else:
        buy_size = _clean_strictly_positive(buy_q.get("in_amount"))

    if sell_q.get("notional_size_usd") is not None:
        sell_size = _clean_strictly_positive(sell_q.get("notional_size_usd"))
    elif quote_usd_price is not None and sell_out is not None:
        sell_size = sell_out * quote_usd_price
    else:
        sell_size = _clean_strictly_positive(sell_q.get("out_amount") or sell_q.get("output_amount"))

    if target_size_usd is not None:
        if buy_size is None or sell_size is None:
            return False, "ORDER_SIZE_UNVERIFIED", {
                "size_bound": False,
                "buy_size": buy_size,
                "sell_size": sell_size,
                "target_size": target_size_usd,
                "reason": "buy or sell quote lacks verified notional size",
            }

        buy_rel_diff = abs(buy_size - target_size_usd) / target_size_usd
        sell_rel_diff = abs(sell_size - target_size_usd) / target_size_usd
        tolerance = (size_tolerance_pct / 100.0) + 1e-9

        if buy_rel_diff > tolerance:
            return False, "ORDER_SIZE_MISMATCH", {
                "size_bound": False,
                "quoted_size": buy_size,
                "buy_size": buy_size,
                "sell_size": sell_size,
                "target_size": target_size_usd,
                "rel_diff_pct": buy_rel_diff * 100.0,
                "tolerance_pct": size_tolerance_pct,
                "leg": "buy",
            }
        if sell_rel_diff > tolerance:
            return False, "ORDER_SIZE_MISMATCH", {
                "size_bound": False,
                "quoted_size": sell_size,
                "buy_size": buy_size,
                "sell_size": sell_size,
                "target_size": target_size_usd,
                "rel_diff_pct": sell_rel_diff * 100.0,
                "tolerance_pct": size_tolerance_pct,
                "leg": "sell",
            }

    quoted_size = buy_size or sell_size
    return True, "TWO_WAY_QUOTE_VERIFIED", {
        "size_bound": (buy_size is not None and sell_size is not None),
        "buy_size": buy_size,
        "sell_size": sell_size,
        "quoted_size": quoted_size,
        "buy_out": buy_out,
        "sell_out": sell_out,
    }


def simulate_constant_product_amm(
    reserve_base: float | None = None,
    reserve_quote: float | None = None,
    buy_notional_usd: float = 0.0,
    fee_bps: float = 30.0,
    *,
    reserve0: float | None = None,
    reserve1: float | None = None,
    chain: str | None = None,
    pool_address: str | None = None,
    token0: str | None = None,
    token1: str | None = None,
    timestamp_utc: str | None = None,
    expected_chain: str | None = None,
    expected_pool: str | None = None,
    expected_token_address: str | None = None,
    expected_quote_asset: str | None = None,
    require_reserve_identity: bool = True,
    require_quote_conversion: bool = False,
    quote_usd_conversion: dict[str, Any] | None = None,
    quote_usd_price: float | None = None,
    as_of_utc: str | None = None,
    max_conversion_age_seconds: float = 300.0,
) -> dict[str, Any]:
    """Exact constant-product AMM (x * y = k) simulation with reserve tracking, fees, and identity verification.

    Associates reserve0 exclusively with token0 and reserve1 exclusively with token1.
    Determines whether candidate token is token0 or token1 to select the correct base and quote reserves.
    Normalizes USD trade sizes into quote-token quantities using verified quote-asset USD conversion.
    Rejects missing, zero, negative, non-finite, or inconsistent reserve data.
    Fails closed on any incomplete identity, orientation ambiguity, or unverified conversion.
    """
    fee = validate_fee_bps(fee_bps)

    buy_usd = _clean_strictly_positive(buy_notional_usd)
    if buy_usd is None:
        raise ValueError("buy_notional_usd must be positive and finite")

    def _check_reserve(val: Any, name: str) -> float:
        if val is None:
            raise ValueError(f"MISSING_AMM_RESERVES: {name} is missing")
        num = _clean_strictly_positive(val)
        if num is None:
            raise ValueError(f"INVALID_AMM_RESERVES: {name} must be positive and finite, got {val!r}")
        return num

    token0_is_candidate = None
    effective_base_reserve = None
    effective_quote_reserve = None
    r0 = None
    r1 = None

    # Step 1: Validate identity and determine token orientation
    if require_reserve_identity:
        if not chain:
            raise ValueError("MISSING_AMM_CHAIN_IDENTITY: reserve snapshot missing chain identity")
        if not pool_address:
            raise ValueError("MISSING_AMM_POOL_IDENTITY: reserve snapshot missing pool identity")
        if not token0 or not token1:
            raise ValueError("MISSING_AMM_TOKEN_IDENTITY: reserve snapshot missing token0 or token1")

        if not is_valid_address(chain, pool_address):
            raise ValueError(f"INVALID_TOKEN_ADDRESS: invalid AMM pool address format: {pool_address}")
        if not is_valid_address(chain, token0):
            raise ValueError(f"INVALID_TOKEN_ADDRESS: invalid AMM token0 address format: {token0}")
        if not is_valid_address(chain, token1):
            raise ValueError(f"INVALID_TOKEN_ADDRESS: invalid AMM token1 address format: {token1}")

        if same_address(chain, token0, token1):
            raise ValueError("INVALID_AMM_RESERVES: token0 and token1 cannot be the same address")

        if expected_chain and str(chain).strip().lower() != str(expected_chain).strip().lower():
            raise ValueError("AMM_CHAIN_MISMATCH: reserve snapshot chain does not match expected chain")
        if expected_pool and not same_address(expected_chain or chain, pool_address, expected_pool):
            raise ValueError("AMM_POOL_MISMATCH: reserve pool does not match expected pool")

        if not expected_token_address:
            raise ValueError("MISSING_TOKEN_ADDRESS: expected_token_address is required to determine reserve orientation")
        if not is_valid_address(expected_chain or chain, expected_token_address):
            raise ValueError(f"INVALID_TOKEN_ADDRESS: invalid expected candidate token address format: {expected_token_address}")

        match0 = same_address(expected_chain or chain, token0, expected_token_address)
        match1 = same_address(expected_chain or chain, token1, expected_token_address)
        if not match0 and not match1:
            raise ValueError("AMM_TOKEN_MISMATCH: neither reserve token matches expected candidate token")
        if match0 and match1:
            raise ValueError("INVALID_AMM_RESERVES: candidate token matches both token0 and token1")

        token0_is_candidate = match0
        pool_quote_token = token1 if token0_is_candidate else token0

        if expected_quote_asset:
            if not is_valid_address(expected_chain or chain, expected_quote_asset):
                raise ValueError(f"INVALID_TOKEN_ADDRESS: invalid expected quote asset format: {expected_quote_asset}")
            if not same_address(expected_chain or chain, pool_quote_token, expected_quote_asset):
                raise ValueError("QUOTE_ASSET_MISMATCH: reserve pool quote token does not match expected quote asset")
    else:
        if token0 and token1 and expected_token_address:
            eff_chain = chain or "evm"
            if same_address(eff_chain, token0, token1):
                raise ValueError("INVALID_AMM_RESERVES: token0 and token1 cannot be the same address")
            match0 = same_address(eff_chain, token0, expected_token_address)
            match1 = same_address(eff_chain, token1, expected_token_address)
            if not match0 and not match1:
                raise ValueError("AMM_TOKEN_MISMATCH: neither reserve token matches expected candidate token")
            token0_is_candidate = match0
        pool_quote_token = (token1 if token0_is_candidate else token0) if token0_is_candidate is not None else expected_quote_asset

    # Step 2: Validate and associate reserves
    has_r0_r1 = (reserve0 is not None or reserve1 is not None)
    has_rb_rq = (reserve_base is not None or reserve_quote is not None)

    if not has_r0_r1 and not has_rb_rq:
        raise ValueError("MISSING_AMM_RESERVES: AMM reserve evidence missing")

    if has_r0_r1:
        if reserve0 is None or reserve1 is None:
            raise ValueError("INVALID_AMM_RESERVES: both reserve0 and reserve1 must be provided")
        r0 = _check_reserve(reserve0, "reserve0")
        r1 = _check_reserve(reserve1, "reserve1")

        if token0_is_candidate is None:
            raise ValueError("MISSING_AMM_TOKEN_IDENTITY: cannot associate reserve0/reserve1 without token orientation evidence")

        # Associate reserve0 exclusively with token0 and reserve1 exclusively with token1
        if token0_is_candidate:
            effective_base_reserve = r0
            effective_quote_reserve = r1
        else:
            effective_base_reserve = r1
            effective_quote_reserve = r0

    if has_rb_rq:
        if reserve_base is None or reserve_quote is None:
            raise ValueError("INVALID_AMM_RESERVES: both reserve_base and reserve_quote must be provided")
        rb_val = _check_reserve(reserve_base, "reserve_base")
        rq_val = _check_reserve(reserve_quote, "reserve_quote")

        if has_r0_r1:
            tol = 1e-6
            if abs(rb_val - effective_base_reserve) > tol * max(rb_val, effective_base_reserve):
                raise ValueError(
                    f"INCONSISTENT_AMM_RESERVES: reserve_base ({rb_val}) does not match oriented candidate reserve ({effective_base_reserve})"
                )
            if abs(rq_val - effective_quote_reserve) > tol * max(rq_val, effective_quote_reserve):
                raise ValueError(
                    f"INCONSISTENT_AMM_RESERVES: reserve_quote ({rq_val}) does not match oriented quote reserve ({effective_quote_reserve})"
                )
        else:
            effective_base_reserve = rb_val
            effective_quote_reserve = rq_val
            if token0_is_candidate is True:
                r0 = rb_val
                r1 = rq_val
            elif token0_is_candidate is False:
                r0 = rq_val
                r1 = rb_val

    if effective_base_reserve is None or effective_quote_reserve is None:
        raise ValueError("INVALID_AMM_RESERVES: could not determine effective base and quote reserves")

    # Step 3: Resolve quote-asset USD conversion rate
    conv = quote_usd_conversion
    if require_quote_conversion and not conv:
        raise ValueError("MISSING_QUOTE_CONVERSION_EVIDENCE: quote-asset USD conversion evidence is required; bare numeric quote_usd_price cannot satisfy this requirement")

    if conv:
        ok, status, rate, details = validate_quote_usd_conversion(
            conv,
            expected_chain=expected_chain or chain,
            expected_quote_asset=pool_quote_token or expected_quote_asset,
            as_of_utc=as_of_utc or timestamp_utc,
            max_conversion_age_seconds=max_conversion_age_seconds,
        )
        if not ok:
            raise ValueError(f"{status}: {details.get('reason')}")
        eff_quote_price = rate
    elif quote_usd_price is not None:
        p = _clean_strictly_positive(quote_usd_price)
        if p is None:
            raise ValueError("INVALID_QUOTE_CONVERSION_PRICE: quote_usd_price must be positive and finite")
        eff_quote_price = p
    elif require_quote_conversion:
        raise ValueError("MISSING_QUOTE_CONVERSION_EVIDENCE: quote-asset USD conversion evidence is required")
    else:
        eff_quote_price = 1.0

    rb = effective_base_reserve
    rq = effective_quote_reserve
    mid_price_quote = rq / rb
    mid_price_usd = mid_price_quote * eff_quote_price
    fee_factor = 1.0 - (fee / 10000.0)

    # Convert USD trade size into quote-token units
    buy_quote_tokens = buy_usd / eff_quote_price

    # Buy swap: spend buy_quote_tokens quote tokens to get base tokens
    delta_in_eff = buy_quote_tokens * fee_factor
    delta_base_out = (rb * delta_in_eff) / (rq + delta_in_eff)

    if delta_base_out <= 0 or delta_base_out >= rb:
        return {
            "status": "INSUFFICIENT_LIQUIDITY",
            "route_available": False,
            "simulation_method": "CONSTANT_PRODUCT_AMM_SIMULATION",
            "reserve_base": rb,
            "reserve_quote": rq,
            "reserve0": r0,
            "reserve1": r1,
            "token0": token0,
            "token1": token1,
            "token0_is_candidate": token0_is_candidate,
            "quote_usd_price": eff_quote_price,
            "mid_price": mid_price_usd,
            "mid_price_quote": mid_price_quote,
            "buy": {"complete": False, "filled_quote_usd": 0.0, "filled_quote_tokens": 0.0, "filled_quantity": 0.0, "vwap": None},
            "immediate_exit": {"complete": False, "filled_quote_usd": 0.0, "filled_quote_tokens": 0.0, "filled_quantity": 0.0, "vwap": None},
            "round_trip_estimate": {"fee_bps_each_side": fee, "net_pnl_usd": None, "net_return_pct": None},
        }

    rb_post = rb - delta_base_out
    rq_post = rq + buy_quote_tokens
    buy_vwap_quote = buy_quote_tokens / delta_base_out
    buy_vwap_usd = buy_vwap_quote * eff_quote_price
    buy_impact_bps = ((buy_vwap_quote / mid_price_quote) - 1.0) * 10000.0

    # Sell swap: sell delta_base_out back into the pool to get quote tokens
    delta_base_in_eff = delta_base_out * fee_factor
    delta_quote_out_tokens = (rq_post * delta_base_in_eff) / (rb_post + delta_base_in_eff)
    sell_quote_usd = delta_quote_out_tokens * eff_quote_price
    sell_vwap_quote = delta_quote_out_tokens / delta_base_out
    sell_vwap_usd = sell_vwap_quote * eff_quote_price
    sell_impact_bps = ((mid_price_quote / sell_vwap_quote) - 1.0) * 10000.0

    gross_pnl_usd = sell_quote_usd - buy_usd
    net_return_pct = (gross_pnl_usd / buy_usd) * 100.0

    return {
        "status": "FULL_ROUND_TRIP_AMM_LIQUIDITY_AVAILABLE",
        "route_available": True,
        "simulation_method": "CONSTANT_PRODUCT_AMM_SIMULATION",
        "reserve_base": rb,
        "reserve_quote": rq,
        "reserve0": r0,
        "reserve1": r1,
        "token0": token0,
        "token1": token1,
        "token0_is_candidate": token0_is_candidate,
        "quote_usd_price": eff_quote_price,
        "mid_price": mid_price_usd,
        "mid_price_quote": mid_price_quote,
        "buy": {
            "complete": True,
            "filled_quote_usd": buy_usd,
            "filled_quote_tokens": buy_quote_tokens,
            "filled_quantity": delta_base_out,
            "vwap": buy_vwap_usd,
            "vwap_quote": buy_vwap_quote,
            "price_impact_bps": buy_impact_bps,
        },
        "immediate_exit": {
            "complete": True,
            "filled_quote_usd": sell_quote_usd,
            "filled_quote_tokens": delta_quote_out_tokens,
            "filled_quantity": delta_base_out,
            "vwap": sell_vwap_usd,
            "vwap_quote": sell_vwap_quote,
            "price_impact_bps": sell_impact_bps,
        },
        "round_trip_estimate": {
            "fee_bps_each_side": fee,
            "net_pnl_usd": gross_pnl_usd,
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
    expected_quote_asset: str | None = None,
    require_pool_identity: bool = False,
    require_reserve_identity: bool = True,
    require_quote_asset: bool = False,
    require_quote_conversion: bool = False,
    require_quote_denomination: bool = False,
    quote_usd_conversion: dict[str, Any] | None = None,
    max_conversion_age_seconds: float = 300.0,
    size_tolerance_pct: float = 1.0,
    quantity_tolerance_pct: float = 0.1,
) -> dict[str, Any]:
    """Evaluate multi-size read-only execution scenarios ($100, $500, $1k, $5k).

    Distinguishes actual order book depth walks, constant-product AMM simulations,
    and size-verified router quotes from approximate linear liquidity heuristics.
    Never assumes the same token has identical execution cost across sizes.
    Never assumes a buy quote proves a sell route is possible.
    Enforces quote freshness verification, size-bound quote validation, AMM reserve identity,
    order-book identity, expected quote-asset verification, and pre-simulation fee validation.
    Fails closed on any unverified condition.
    """
    liquidity_usd = None
    if isinstance(source, dict):
        liquidity_usd = _clean_numeric(source.get("liquidity_usd"))

    clean_sizes: list[float] = []
    raw_sizes = sizes_usd if isinstance(sizes_usd, (list, tuple, set)) else DEFAULT_RESEARCH_NOTIONAL_SIZES_USD
    for s in raw_sizes:
        num = _clean_strictly_positive(s)
        if num is not None:
            clean_sizes.append(num)
    if not clean_sizes:
        clean_sizes = list(DEFAULT_RESEARCH_NOTIONAL_SIZES_USD)

    gas_usd_clean = _clean_numeric(gas_usd)
    gas_usd = max(0.0, gas_usd_clean) if gas_usd_clean is not None else None

    target_upside_pct = _clean_numeric(target_upside_pct)

    max_age_clean = _clean_numeric(max_quote_age_seconds)
    max_quote_age_seconds = max(1.0, max_age_clean) if max_age_clean is not None else 300.0

    # 1. Fee validation before simulation begins
    try:
        validated_fee_bps = validate_fee_bps(fee_bps)
    except (ValueError, TypeError) as fee_err:
        size_evaluations = {}
        for s in clean_sizes:
            size_key = f"${int(s) if s.is_integer() else s}"
            size_evaluations[size_key] = {
                "size_usd": s,
                "status": "INVALID_FEE_DATA",
                "simulation_method": "INVALID_FEE_REJECTED",
                "route_available": False,
                "quote_verified_for_size": False,
                "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                "quote_freshness": "UNVERIFIED",
                "buy": {"status": "UNAVAILABLE", "reason": f"Invalid fee: {fee_err}"},
                "sell": {"status": "UNAVAILABLE", "reason": f"Invalid fee: {fee_err}"},
                "estimated_round_trip_cost_pct": None,
                "execution_adjusted_return_pct": None,
            }
        return {
            "schema": "crypto-prebreakout.multi-size-execution-simulation.v1",
            "simulator_version": BACKTEST_VERSION if "BACKTEST_VERSION" in globals() else "4.30.0",
            "execution_enabled": False,
            "status": "INVALID_FEE_DATA",
            "available_liquidity_usd": liquidity_usd,
            "fee_bps": None,
            "gas_usd": gas_usd,
            "target_upside_pct": target_upside_pct,
            "size_evaluations": size_evaluations,
            "research_disclaimer": "Read-only simulation. Evaluates hypothetical size-dependent price impact and execution drag.",
        }

    book = _find_book(source)
    quotes = source.get("dex_execution_quotes") if isinstance(source.get("dex_execution_quotes"), dict) else (
        source if ("buy_quote" in source or "sell_quote" in source) else None
    )
    amm_reserves = source.get("amm_reserves") if isinstance(source.get("amm_reserves"), dict) else (
        source.get("reserves") if isinstance(source.get("reserves"), dict) else None
    )

    conv_data = quote_usd_conversion
    if not conv_data and isinstance(source, dict):
        conv_data = (
            source.get("quote_usd_conversion") or
            (book.get("quote_usd_conversion") if isinstance(book, dict) else None) or
            (amm_reserves.get("quote_usd_conversion") if isinstance(amm_reserves, dict) else None) or
            (quotes.get("quote_usd_conversion") if isinstance(quotes, dict) else None)
        )

    target_chain = (
        expected_chain or
        (book.get("chain") if isinstance(book, dict) else None) or
        (amm_reserves.get("chain") if isinstance(amm_reserves, dict) else None) or
        (quotes.get("chain") if isinstance(quotes, dict) else None)
    )

    pool_quote_asset = None
    if amm_reserves and expected_token_address and target_chain:
        r_t0 = amm_reserves.get("token0")
        r_t1 = amm_reserves.get("token1")
        if r_t0 and same_address(target_chain, r_t0, expected_token_address):
            pool_quote_asset = r_t1
        elif r_t1 and same_address(target_chain, r_t1, expected_token_address):
            pool_quote_asset = r_t0

    target_quote_asset = (
        expected_quote_asset or
        (book.get("quote_asset") or book.get("quote_token") if isinstance(book, dict) else None) or
        pool_quote_asset or
        (quotes.get("quote_asset") if isinstance(quotes, dict) else None)
    )

    if require_quote_conversion and not conv_data:
        size_evaluations = {}
        for s in clean_sizes:
            size_key = f"${int(s) if s.is_integer() else s}"
            size_evaluations[size_key] = {
                "size_usd": s,
                "status": "MISSING_QUOTE_CONVERSION_EVIDENCE",
                "simulation_method": "QUOTE_CONVERSION_REJECTED",
                "route_available": False,
                "quote_verified_for_size": False,
                "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                "quote_freshness": "UNVERIFIED",
                "reason": "quote-asset USD conversion evidence is required",
                "buy": {"status": "UNAVAILABLE", "reason": "Missing quote-asset USD conversion evidence"},
                "sell": {"status": "UNAVAILABLE", "reason": "Missing quote-asset USD conversion evidence"},
                "estimated_round_trip_cost_pct": None,
                "execution_adjusted_return_pct": None,
            }
        return {
            "schema": "crypto-prebreakout.multi-size-execution-simulation.v1",
            "simulator_version": BACKTEST_VERSION if "BACKTEST_VERSION" in globals() else "4.30.0",
            "execution_enabled": False,
            "status": "MISSING_QUOTE_CONVERSION_EVIDENCE",
            "available_liquidity_usd": liquidity_usd,
            "fee_bps": validated_fee_bps,
            "gas_usd": gas_usd,
            "target_upside_pct": target_upside_pct,
            "size_evaluations": size_evaluations,
            "research_disclaimer": "Read-only simulation. Evaluates hypothetical size-dependent price impact and execution drag.",
        }

    effective_quote_usd_price = 1.0
    if conv_data:
        ok_conv, conv_status, conv_price, conv_details = validate_quote_usd_conversion(
            conv_data,
            expected_chain=target_chain,
            expected_quote_asset=target_quote_asset,
            as_of_utc=as_of_utc,
            max_conversion_age_seconds=max_conversion_age_seconds,
        )
        if not ok_conv:
            size_evaluations = {}
            for s in clean_sizes:
                size_key = f"${int(s) if s.is_integer() else s}"
                size_evaluations[size_key] = {
                    "size_usd": s,
                    "status": conv_status,
                    "simulation_method": "QUOTE_CONVERSION_REJECTED",
                    "route_available": False,
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "quote_freshness": "UNVERIFIED",
                    "reason": conv_details.get("reason"),
                    "buy": {"status": "UNAVAILABLE", "reason": conv_details.get("reason")},
                    "sell": {"status": "UNAVAILABLE", "reason": conv_details.get("reason")},
                    "estimated_round_trip_cost_pct": None,
                    "execution_adjusted_return_pct": None,
                }
            return {
                "schema": "crypto-prebreakout.multi-size-execution-simulation.v1",
                "simulator_version": BACKTEST_VERSION if "BACKTEST_VERSION" in globals() else "4.30.0",
                "execution_enabled": False,
                "status": conv_status,
                "available_liquidity_usd": liquidity_usd,
                "fee_bps": validated_fee_bps,
                "gas_usd": gas_usd,
                "target_upside_pct": target_upside_pct,
                "size_evaluations": size_evaluations,
                "research_disclaimer": "Read-only simulation. Evaluates hypothetical size-dependent price impact and execution drag.",
            }
        effective_quote_usd_price = conv_price

    size_evaluations: dict[str, Any] = {}

    for size in clean_sizes:
        size_key = f"${int(size) if size.is_integer() else size}"

        # Case A: Order book depth simulation
        if book:
            book_stamp = book.get("timestamp_utc") or book.get("timestamp")
            book_fresh, book_fresh_status, book_age = validate_timestamp_freshness(
                book_stamp, as_of_utc, max_quote_age_seconds
            )
            if not book_fresh:
                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": book_fresh_status,
                    "route_available": False,
                    "quote_freshness": book_fresh_status,
                    "simulation_method": f"{book_fresh_status}_REJECTED",
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "quote_age_seconds": round(book_age, 1) if book_age is not None else None,
                    "buy": {"status": "UNAVAILABLE", "reason": f"Freshness verification failed: {book_fresh_status}"},
                    "sell": {"status": "UNAVAILABLE", "reason": f"Freshness verification failed: {book_fresh_status}"},
                    "estimated_round_trip_cost_pct": None,
                    "execution_adjusted_return_pct": None,
                }
                continue

            # Order book identity validation
            book_chain = book.get("chain")
            book_pool = book.get("pool_address") or book.get("venue") or book.get("pool")
            book_token = book.get("token_address") or book.get("contract_address") or book.get("base_token")
            book_quote = book.get("quote_asset") or book.get("quote_token") or book.get("quote_address")

            book_ident_status = None
            book_ident_reason = None
            if not book_chain:
                book_ident_status = "MISSING_BOOK_CHAIN_IDENTITY"
                book_ident_reason = "order book evidence missing chain identity"
            elif not book_pool:
                book_ident_status = "MISSING_BOOK_POOL_IDENTITY"
                book_ident_reason = "order book evidence missing pool or venue identity"
            elif not book_token:
                book_ident_status = "MISSING_BOOK_TOKEN_IDENTITY"
                book_ident_reason = "order book evidence missing token contract identity"
            elif not is_valid_address(book_chain, book_token):
                book_ident_status = "INVALID_TOKEN_ADDRESS"
                book_ident_reason = f"order book token address format invalid: {book_token}"
            elif expected_chain and str(book_chain).strip().lower() != str(expected_chain).strip().lower():
                book_ident_status = "BOOK_CHAIN_MISMATCH"
                book_ident_reason = f"order book chain ({book_chain}) does not match expected ({expected_chain})"
            elif expected_pool and not same_address(expected_chain or book_chain, book_pool, expected_pool):
                book_ident_status = "BOOK_POOL_MISMATCH"
                book_ident_reason = f"order book pool/venue ({book_pool}) does not match expected ({expected_pool})"
            elif expected_token_address and not same_address(expected_chain or book_chain, book_token, expected_token_address):
                book_ident_status = "BOOK_TOKEN_MISMATCH"
                book_ident_reason = f"order book token ({book_token}) does not match expected ({expected_token_address})"
            elif require_quote_asset and not book_quote:
                book_ident_status = "MISSING_QUOTE_ASSET_EVIDENCE"
                book_ident_reason = "order book evidence missing explicit quote asset identity"
            elif expected_quote_asset and not book_quote:
                book_ident_status = "MISSING_QUOTE_ASSET_EVIDENCE"
                book_ident_reason = "order book evidence missing quote asset identity to verify against expected quote asset"
            elif book_quote and not is_valid_address(book_chain, book_quote):
                book_ident_status = "INVALID_TOKEN_ADDRESS"
                book_ident_reason = f"order book quote asset address format invalid: {book_quote}"
            elif book_quote and book_token and same_address(book_chain, book_token, book_quote):
                book_ident_status = "INVALID_TOKEN_ADDRESS"
                book_ident_reason = "order book token and quote asset cannot be the same address"
            elif expected_quote_asset and book_quote and not same_address(expected_chain or book_chain, book_quote, expected_quote_asset):
                book_ident_status = "QUOTE_ASSET_MISMATCH"
                book_ident_reason = f"order book quote asset ({book_quote}) does not match expected ({expected_quote_asset})"

            if book_ident_status:
                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": book_ident_status,
                    "route_available": False,
                    "quote_freshness": "FRESH",
                    "simulation_method": "ORDER_BOOK_IDENTITY_REJECTED",
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "quote_age_seconds": round(book_age, 1) if book_age is not None else None,
                    "reason": book_ident_reason,
                    "buy": {"status": "UNAVAILABLE", "reason": book_ident_reason},
                    "sell": {"status": "UNAVAILABLE", "reason": book_ident_reason},
                    "estimated_round_trip_cost_pct": None,
                    "execution_adjusted_return_pct": None,
                }
                continue

            try:
                sim = simulate(
                    book,
                    buy_notional_usd=size,
                    fee_bps=validated_fee_bps,
                    quote_usd_conversion=conv_data,
                    quote_usd_price=effective_quote_usd_price,
                    expected_chain=expected_chain,
                    expected_quote_asset=expected_quote_asset,
                    as_of_utc=as_of_utc,
                    max_conversion_age_seconds=max_conversion_age_seconds,
                    require_quote_conversion=require_quote_conversion,
                    require_quote_denomination=require_quote_denomination,
                )
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
                    "quote_age_seconds": round(book_age, 1) if book_age is not None else None,
                    "available_liquidity_usd": liquidity_usd,
                    "quote_usd_price": sim.get("quote_usd_price"),
                    "buy": {
                        "status": "AVAILABLE" if buy_complete else "PARTIAL_DEPTH_ONLY",
                        "filled_quote_usd": sim["buy"]["filled_quote_usd"],
                        "filled_quote_tokens": sim["buy"].get("filled_quote_tokens"),
                        "vwap": sim["buy"]["vwap"],
                        "vwap_quote": sim["buy"].get("vwap_quote"),
                        "price_impact_bps": buy_impact_bps,
                        "slippage_bps": buy_slip_bps,
                        "complete": buy_complete,
                    },
                    "sell": {
                        "status": "AVAILABLE" if sell_complete else "PARTIAL_DEPTH_ONLY",
                        "filled_quote_usd": sim["immediate_exit"]["filled_quote_usd"],
                        "filled_quote_tokens": sim["immediate_exit"].get("filled_quote_tokens"),
                        "vwap": sim["immediate_exit"]["vwap"],
                        "vwap_quote": sim["immediate_exit"].get("vwap_quote"),
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
                err_msg = str(e)
                status_code = "UNAVAILABLE"
                for code in (
                    "MISSING_QUOTE_DENOMINATION",
                    "MISSING_QUOTE_CONVERSION_EVIDENCE",
                    "INVALID_QUOTE_CONVERSION_PRICE",
                    "STALE_QUOTE_CONVERSION",
                    "FUTURE_DATED_QUOTE_CONVERSION",
                    "INVALID_QUOTE_CONVERSION_TIMESTAMP",
                    "QUOTE_CONVERSION_CHAIN_MISMATCH",
                    "QUOTE_CONVERSION_ASSET_MISMATCH",
                    "UNVERIFIED_VALUATION_SOURCE",
                    "UNRECOGNIZED_VALUATION_POLICY",
                    "MISSING_CONVERSION_PROVENANCE",
                ):
                    if code in err_msg:
                        status_code = code
                        break
                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": status_code,
                    "simulation_method": "ORDER_BOOK_DEPTH_WALK",
                    "is_depth_walk": True,
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "route_available": False,
                    "reason": err_msg,
                    "buy": {"status": "UNAVAILABLE", "reason": err_msg},
                    "sell": {"status": "UNAVAILABLE", "reason": err_msg},
                    "estimated_round_trip_cost_pct": None,
                    "execution_adjusted_return_pct": None,
                }

        # Case B: Constant-product AMM simulation
        elif amm_reserves:
            res_stamp = amm_reserves.get("timestamp_utc") or amm_reserves.get("timestamp")
            res_fresh, res_fresh_status, res_age = validate_timestamp_freshness(
                res_stamp, as_of_utc, max_quote_age_seconds
            )
            if not res_fresh:
                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": res_fresh_status,
                    "route_available": False,
                    "quote_freshness": res_fresh_status,
                    "simulation_method": f"{res_fresh_status}_REJECTED",
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "quote_age_seconds": round(res_age, 1) if res_age is not None else None,
                    "buy": {"status": "UNAVAILABLE", "reason": f"Freshness verification failed: {res_fresh_status}"},
                    "sell": {"status": "UNAVAILABLE", "reason": f"Freshness verification failed: {res_fresh_status}"},
                    "estimated_round_trip_cost_pct": None,
                    "execution_adjusted_return_pct": None,
                }
                continue

            # DO NOT inherit missing identity from parent candidate! Must be provided by reserve snapshot itself.
            res_chain = amm_reserves.get("chain")
            res_pool = amm_reserves.get("pool_address") or amm_reserves.get("pool")
            res_t0 = amm_reserves.get("token0")
            res_t1 = amm_reserves.get("token1")

            if not res_chain:
                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": "MISSING_AMM_CHAIN_IDENTITY",
                    "route_available": False,
                    "quote_freshness": "FRESH",
                    "simulation_method": "CONSTANT_PRODUCT_AMM_SIMULATION",
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "quote_age_seconds": round(res_age, 1) if res_age is not None else None,
                    "reason": "reserve snapshot missing chain identity",
                    "buy": {"status": "UNAVAILABLE", "reason": "Missing AMM chain identity"},
                    "sell": {"status": "UNAVAILABLE", "reason": "Missing AMM chain identity"},
                }
                continue

            if not res_pool:
                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": "MISSING_AMM_POOL_IDENTITY",
                    "route_available": False,
                    "quote_freshness": "FRESH",
                    "simulation_method": "CONSTANT_PRODUCT_AMM_SIMULATION",
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "quote_age_seconds": round(res_age, 1) if res_age is not None else None,
                    "reason": "reserve snapshot missing pool identity",
                    "buy": {"status": "UNAVAILABLE", "reason": "Missing AMM pool identity"},
                    "sell": {"status": "UNAVAILABLE", "reason": "Missing AMM pool identity"},
                }
                continue

            if not res_t0 or not res_t1:
                has_aliases = bool(amm_reserves.get("base_token") or amm_reserves.get("quote_token"))
                reason = (
                    "reserve snapshot uses ambiguous base_token/quote_token aliases without explicit token0/token1 pool ordering"
                    if has_aliases else
                    "reserve snapshot missing token0 or token1 identity"
                )
                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": "MISSING_AMM_TOKEN_IDENTITY",
                    "route_available": False,
                    "quote_freshness": "FRESH",
                    "simulation_method": "CONSTANT_PRODUCT_AMM_SIMULATION",
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "quote_age_seconds": round(res_age, 1) if res_age is not None else None,
                    "reason": reason,
                    "buy": {"status": "UNAVAILABLE", "reason": reason},
                    "sell": {"status": "UNAVAILABLE", "reason": reason},
                }
                continue

            try:
                r0 = amm_reserves.get("reserve0")
                r1 = amm_reserves.get("reserve1")
                rb = amm_reserves.get("reserve_base") or amm_reserves.get("reserve_token") or amm_reserves.get("base_reserve")
                rq = amm_reserves.get("reserve_quote") or amm_reserves.get("reserve_usd") or amm_reserves.get("quote_reserve")

                amm_sim = simulate_constant_product_amm(
                    reserve_base=rb,
                    reserve_quote=rq,
                    buy_notional_usd=size,
                    fee_bps=validated_fee_bps,
                    reserve0=r0,
                    reserve1=r1,
                    chain=res_chain,
                    pool_address=res_pool,
                    token0=res_t0,
                    token1=res_t1,
                    timestamp_utc=res_stamp,
                    expected_chain=expected_chain,
                    expected_pool=expected_pool,
                    expected_token_address=expected_token_address,
                    expected_quote_asset=expected_quote_asset,
                    require_reserve_identity=require_reserve_identity,
                    require_quote_conversion=require_quote_conversion,
                    quote_usd_conversion=conv_data,
                    quote_usd_price=effective_quote_usd_price,
                    as_of_utc=as_of_utc,
                    max_conversion_age_seconds=max_conversion_age_seconds,
                )

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
                    "quote_age_seconds": round(res_age, 1) if res_age is not None else None,
                    "available_liquidity_usd": liquidity_usd,
                    "reserve_base": amm_sim.get("reserve_base"),
                    "reserve_quote": amm_sim.get("reserve_quote"),
                    "reserve0": amm_sim.get("reserve0"),
                    "reserve1": amm_sim.get("reserve1"),
                    "token0_is_candidate": amm_sim.get("token0_is_candidate"),
                    "quote_usd_price": amm_sim.get("quote_usd_price"),
                    "buy": {
                        "status": "AVAILABLE" if buy_ok else "UNAVAILABLE",
                        "filled_quote_usd": amm_sim["buy"]["filled_quote_usd"],
                        "filled_quote_tokens": amm_sim["buy"].get("filled_quote_tokens"),
                        "filled_quantity": amm_sim["buy"]["filled_quantity"],
                        "vwap": amm_sim["buy"]["vwap"],
                        "vwap_quote": amm_sim["buy"].get("vwap_quote"),
                        "price_impact_bps": amm_sim["buy"].get("price_impact_bps"),
                        "complete": buy_ok,
                    },
                    "sell": {
                        "status": "AVAILABLE" if sell_ok else "UNAVAILABLE",
                        "filled_quote_usd": amm_sim["immediate_exit"]["filled_quote_usd"],
                        "filled_quote_tokens": amm_sim["immediate_exit"].get("filled_quote_tokens"),
                        "filled_quantity": amm_sim["immediate_exit"]["filled_quantity"],
                        "vwap": amm_sim["immediate_exit"]["vwap"],
                        "vwap_quote": amm_sim["immediate_exit"].get("vwap_quote"),
                        "price_impact_bps": amm_sim["immediate_exit"].get("price_impact_bps"),
                        "complete": sell_ok,
                    },
                    "round_trip_cost_usd": abs(amm_sim["round_trip_estimate"]["net_pnl_usd"]) if amm_sim["round_trip_estimate"]["net_pnl_usd"] is not None else None,
                    "estimated_round_trip_cost_pct": round(total_drag_pct, 2) if total_drag_pct is not None else None,
                    "execution_adjusted_return_pct": round(adj_return, 2) if adj_return is not None else None,
                }
            except ValueError as e:
                err_msg = str(e)
                status_code = "INVALID_AMM_RESERVES"
                for code in (
                    "MISSING_QUOTE_CONVERSION_EVIDENCE",
                    "INVALID_QUOTE_CONVERSION_PRICE",
                    "STALE_QUOTE_CONVERSION",
                    "FUTURE_DATED_QUOTE_CONVERSION",
                    "INVALID_QUOTE_CONVERSION_TIMESTAMP",
                    "QUOTE_CONVERSION_CHAIN_MISMATCH",
                    "QUOTE_CONVERSION_ASSET_MISMATCH",
                    "UNVERIFIED_VALUATION_SOURCE",
                    "UNRECOGNIZED_VALUATION_POLICY",
                    "MISSING_CONVERSION_PROVENANCE",
                    "MISSING_AMM_POOL_IDENTITY", "AMM_POOL_MISMATCH",
                    "MISSING_AMM_CHAIN_IDENTITY", "AMM_CHAIN_MISMATCH",
                    "MISSING_AMM_TOKEN_IDENTITY", "AMM_TOKEN_MISMATCH",
                    "QUOTE_ASSET_MISMATCH", "INVALID_TOKEN_ADDRESS",
                    "INCONSISTENT_AMM_RESERVES", "MISSING_AMM_RESERVES", "INVALID_AMM_RESERVES",
                ):
                    if code in err_msg:
                        status_code = code
                        break

                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": status_code,
                    "simulation_method": "CONSTANT_PRODUCT_AMM_SIMULATION",
                    "is_depth_walk": False,
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "route_available": False,
                    "quote_freshness": "FRESH",
                    "quote_age_seconds": round(res_age, 1) if res_age is not None else None,
                    "reason": err_msg,
                    "buy": {"status": "UNAVAILABLE", "reason": err_msg},
                    "sell": {"status": "UNAVAILABLE", "reason": err_msg},
                    "estimated_round_trip_cost_pct": None,
                    "execution_adjusted_return_pct": None,
                }

        # Case C: Router quotes evaluation
        elif quotes:
            buy_q = quotes.get("buy_quote")
            sell_q = quotes.get("sell_quote")

            if not isinstance(buy_q, dict) or not buy_q:
                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": "MISSING_BUY_ROUTE",
                    "route_available": False,
                    "quote_freshness": "UNVERIFIED",
                    "simulation_method": "MISSING_BUY_ROUTE_REJECTED",
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "quote_age_seconds": None,
                    "buy": {"status": "UNAVAILABLE", "reason": "Missing buy quote route"},
                    "sell": {"status": "UNAVAILABLE", "reason": "Missing buy quote route"},
                    "estimated_round_trip_cost_pct": None,
                    "execution_adjusted_return_pct": None,
                }
                continue

            if not isinstance(sell_q, dict) or not sell_q:
                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": "MISSING_SELL_ROUTE",
                    "route_available": False,
                    "quote_freshness": "UNVERIFIED",
                    "simulation_method": "MISSING_SELL_ROUTE_REJECTED",
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "quote_age_seconds": None,
                    "buy": {"status": "AVAILABLE" if buy_q else "UNAVAILABLE"},
                    "sell": {"status": "UNAVAILABLE", "reason": "Missing sell quote route"},
                    "estimated_round_trip_cost_pct": None,
                    "execution_adjusted_return_pct": None,
                }
                continue

            # Validate buy and sell timestamps independently; final_price_timestamp_utc NEVER substitutes!
            buy_stamp = buy_q.get("timestamp_utc") or buy_q.get("timestamp")
            sell_stamp = sell_q.get("timestamp_utc") or sell_q.get("timestamp")

            buy_fresh, buy_fresh_status, buy_age = validate_timestamp_freshness(
                buy_stamp, as_of_utc, max_quote_age_seconds
            )
            sell_fresh, sell_fresh_status, sell_age = validate_timestamp_freshness(
                sell_stamp, as_of_utc, max_quote_age_seconds
            )

            if not buy_fresh:
                quote_freshness_status = buy_fresh_status
                quote_age_seconds = buy_age
                is_fresh = False
            elif not sell_fresh:
                quote_freshness_status = sell_fresh_status
                quote_age_seconds = sell_age
                is_fresh = False
            else:
                quote_freshness_status = "FRESH"
                quote_age_seconds = max(buy_age or 0.0, sell_age or 0.0)
                is_fresh = True

            if not is_fresh:
                size_evaluations[size_key] = {
                    "size_usd": size,
                    "status": quote_freshness_status,
                    "route_available": False,
                    "quote_freshness": quote_freshness_status,
                    "simulation_method": f"{quote_freshness_status}_REJECTED",
                    "quote_verified_for_size": False,
                    "size_verification_status": "ORDER_SIZE_UNVERIFIED_ESTIMATE",
                    "quote_age_seconds": round(quote_age_seconds, 1) if quote_age_seconds is not None else None,
                    "buy": {"status": "UNAVAILABLE", "reason": f"Freshness verification failed: {quote_freshness_status}"},
                    "sell": {"status": "UNAVAILABLE", "reason": f"Freshness verification failed: {quote_freshness_status}"},
                    "estimated_round_trip_cost_pct": None,
                    "execution_adjusted_return_pct": None,
                }
                continue

            valid_route, route_reason, route_info = validate_two_way_quotes(
                buy_q,
                sell_q,
                expected_token_address=expected_token_address,
                expected_chain=expected_chain,
                expected_pool=expected_pool,
                expected_quote_asset=expected_quote_asset,
                require_pool_identity=require_pool_identity,
                require_quote_asset=require_quote_asset,
                target_size_usd=size,
                size_tolerance_pct=size_tolerance_pct,
                quantity_tolerance_pct=quantity_tolerance_pct,
                quote_meta=quotes,
                quote_usd_price=effective_quote_usd_price,
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

            fee_cost = (size * 2.0) * (validated_fee_bps / 10000.0)
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
                    "status": "AVAILABLE" if (isinstance(buy_q, dict) and buy_q and valid_route) else "UNAVAILABLE",
                    "provider": quotes.get("provider"),
                    "estimated_price_impact_bps": impact_bps,
                    "reason": route_info.get("reason") if not valid_route else None,
                },
                "sell": {
                    "status": "AVAILABLE" if (isinstance(sell_q, dict) and sell_q and valid_route) else "UNAVAILABLE",
                    "provider": quotes.get("provider"),
                    "estimated_price_impact_bps": impact_bps,
                    "sell_route_independently_verified": bool(isinstance(sell_q, dict) and sell_q and valid_route),
                    "reason": route_info.get("reason") if not valid_route else None,
                },
                "trading_fee_usd": round(fee_cost, 2),
                "network_gas_usd": gas_usd,
                "estimated_round_trip_cost_pct": round(total_drag_pct, 2) if total_drag_pct is not None else None,
                "execution_adjusted_return_pct": round(adj_return, 2) if adj_return is not None else None,
            }

        # Case D: Only liquidity or no depth data (fail closed)
        else:
            if liquidity_usd and liquidity_usd > 0:
                impact_pct = (size / (liquidity_usd / 2.0)) * 100.0
                impact_bps = round(impact_pct * 100.0, 1)
                fee_cost = (size * 2.0) * (validated_fee_bps / 10000.0)
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
                    "quote_freshness": "UNVERIFIED_TIMESTAMP",
                    "quote_age_seconds": None,
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
                    "quote_freshness": "UNVERIFIED_TIMESTAMP",
                    "quote_age_seconds": None,
                    "buy": {"status": "UNAVAILABLE", "reason": "No order book depth or route quotes provided"},
                    "sell": {"status": "UNAVAILABLE", "reason": "No order book depth or route quotes provided"},
                    "estimated_round_trip_cost_pct": None,
                    "execution_adjusted_return_pct": None,
                }

    return {
        "schema": "crypto-prebreakout.multi-size-execution-simulation.v1",
        "simulator_version": BACKTEST_VERSION if "BACKTEST_VERSION" in globals() else "4.30.0",
        "execution_enabled": False,
        "available_liquidity_usd": liquidity_usd,
        "fee_bps": validated_fee_bps,
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
