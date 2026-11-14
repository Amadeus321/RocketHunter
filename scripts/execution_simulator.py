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
import hashlib
import hmac
import json
import math
import re
import secrets
import sys
import urllib.error
import urllib.request
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
      Note: This performs structural hexadecimal-format validation, not EIP-55 checksum validation.
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
            as_of_utc=as_of_utc or book.get("timestamp_utc"),
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

PROVIDER_ALIAS_MAP: dict[str, str] = {
    # Chainlink
    "chainlink": "chainlink",
    "chainlink_oracle": "chainlink",
    "chainlink_feed": "chainlink",
    "chainlink_oracle_usd": "chainlink",
    "chainlink_oracle_eth_usd": "chainlink",
    # Pyth
    "pyth": "pyth",
    "pyth_network": "pyth",
    "pyth_oracle": "pyth",
    # Uniswap v3 TWAP
    "uniswap_v3_twap": "uniswap_v3_twap",
    "uniswap_twap": "uniswap_v3_twap",
    "uni_v3_twap": "uniswap_v3_twap",
    "uniswap_v3": "uniswap_v3_twap",
    # DefiLlama
    "defillama": "defillama",
    "defillama_prices": "defillama",
    "defillama_api": "defillama",
    # GeckoTerminal
    "geckoterminal": "geckoterminal",
    "geckoterminal_api": "geckoterminal",
    "geckoterminal_price": "geckoterminal",
    # DexScreener
    "dexscreener": "dexscreener",
    "dexscreener_api": "dexscreener",
    "dexscreener_price": "dexscreener",
    # Coinbase
    "coinbase": "coinbase",
    "coinbase_oracle": "coinbase",
    "coinbase_pro": "coinbase",
    "coinbase_exchange": "coinbase",
    # Binance
    "binance": "binance",
    "binance_ticker": "binance",
    "binance_market": "binance",
    # Kraken
    "kraken": "kraken",
    "kraken_ticker": "kraken",
    "kraken_market": "kraken",
}

PROVIDER_ALLOWED_POLICIES: dict[str, set[str]] = {
    "chainlink": {"STABLECOIN_ORACLE_VERIFIED", "CRYPTO_ORACLE_VERIFIED", "STABLECOIN_DEVIATION_BAND_POLICY"},
    "pyth": {"STABLECOIN_ORACLE_VERIFIED", "CRYPTO_ORACLE_VERIFIED", "STABLECOIN_DEVIATION_BAND_POLICY"},
    "uniswap_v3_twap": {"ON_CHAIN_TWAP_VERIFIED"},
    "defillama": {"DEX_AGGREGATOR_VERIFIED"},
    "geckoterminal": {"DEX_AGGREGATOR_VERIFIED"},
    "dexscreener": {"DEX_AGGREGATOR_VERIFIED"},
    "coinbase": {"CEX_MARKET_TICKER_VERIFIED"},
    "binance": {"CEX_MARKET_TICKER_VERIFIED"},
    "kraken": {"CEX_MARKET_TICKER_VERIFIED"},
}

TRUSTED_CHAINLINK_FEEDS: dict[str, dict[str, Any]] = {
    # Ethereum Mainnet
    "0x8fa59a53f040d12d26f03bc9920373707c3c4004": {
        "chain": "ethereum",
        "chain_id": 1,
        "decimals": 8,
        "quote_asset": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        "symbol": "USDC/USD",
        "usd_reference": "USD",
        "allowed_policies": {"STABLECOIN_ORACLE_VERIFIED", "STABLECOIN_DEVIATION_BAND_POLICY"},
    },
    "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419": {
        "chain": "ethereum",
        "chain_id": 1,
        "decimals": 8,
        "quote_asset": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
        "symbol": "ETH/USD",
        "usd_reference": "USD",
        "allowed_policies": {"CRYPTO_ORACLE_VERIFIED"},
    },
    "0x3e7d1eab03480b83a7d35d7647822ec73a6858c6": {
        "chain": "ethereum",
        "chain_id": 1,
        "decimals": 8,
        "quote_asset": "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
        "symbol": "USDT/USD",
        "usd_reference": "USD",
        "allowed_policies": {"STABLECOIN_ORACLE_VERIFIED", "STABLECOIN_DEVIATION_BAND_POLICY"},
    },
    # Arbitrum
    "0x50834f3163758fcc1df9973b6e91f0f0f0434ac3": {
        "chain": "arbitrum",
        "chain_id": 42161,
        "decimals": 8,
        "quote_asset": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # USDC
        "symbol": "USDC/USD",
        "usd_reference": "USD",
        "allowed_policies": {"STABLECOIN_ORACLE_VERIFIED", "STABLECOIN_DEVIATION_BAND_POLICY"},
    },
    "0x639fe6ab55c921f74e7fac1ee960c0b6293ba612": {
        "chain": "arbitrum",
        "chain_id": 42161,
        "decimals": 8,
        "quote_asset": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",  # WETH
        "symbol": "ETH/USD",
        "usd_reference": "USD",
        "allowed_policies": {"CRYPTO_ORACLE_VERIFIED"},
    },
    # Base
    "0x7e860098f58bbfc8648a4311b374b1db673b2b82": {
        "chain": "base",
        "chain_id": 8453,
        "decimals": 8,
        "quote_asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "symbol": "USDC/USD",
        "usd_reference": "USD",
        "allowed_policies": {"STABLECOIN_ORACLE_VERIFIED", "STABLECOIN_DEVIATION_BAND_POLICY"},
    },
    "0x71041dddad3595f9ced3dccfbe3d1f4b0a16bb70": {
        "chain": "base",
        "chain_id": 8453,
        "decimals": 8,
        "quote_asset": "0x4200000000000000000000000000000000000006",  # WETH
        "symbol": "ETH/USD",
        "usd_reference": "USD",
        "allowed_policies": {"CRYPTO_ORACLE_VERIFIED"},
    },
    "0x3f3f5df88d6c13861d1e7883853244da4817e357": {
        "chain": "arbitrum",
        "chain_id": 42161,
        "decimals": 8,
        "quote_asset": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # USDT
        "symbol": "USDT/USD",
        "usd_reference": "USD",
        "allowed_policies": {"STABLECOIN_ORACLE_VERIFIED", "STABLECOIN_DEVIATION_BAND_POLICY"},
    },
}

TRUSTED_PYTH_FEEDS: dict[str, dict[str, Any]] = {
    # Pyth Solana USDC/USD
    "0xeaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a": {
        "chains": {"solana", "ethereum", "arbitrum", "base"},
        "expo": -8,
        "quote_asset": {
            "solana": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "ethereum": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "arbitrum": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
            "base": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        },
        "symbol": "Crypto.USDC/USD",
        "usd_reference": "USD",
        "allowed_policies": {"STABLECOIN_ORACLE_VERIFIED", "STABLECOIN_DEVIATION_BAND_POLICY"},
    },
    # Pyth SOL/USD
    "0xef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d": {
        "chains": {"solana"},
        "expo": -8,
        "quote_asset": {
            "solana": "So11111111111111111111111111111111111111112",
        },
        "symbol": "Crypto.SOL/USD",
        "usd_reference": "USD",
        "allowed_policies": {"CRYPTO_ORACLE_VERIFIED"},
    },
    # Pyth ETH/USD
    "0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace": {
        "chains": {"ethereum", "arbitrum", "base", "solana"},
        "expo": -8,
        "quote_asset": {
            "ethereum": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "arbitrum": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
            "base": "0x4200000000000000000000000000000000000006",
        },
        "symbol": "Crypto.ETH/USD",
        "usd_reference": "USD",
        "allowed_policies": {"CRYPTO_ORACLE_VERIFIED"},
    },
    # Pyth USDT/USD
    "0x2b89b9dc8fdf9f34709a5b106b472f0f39bb6ca9ce04b0fd7f2e971688e2e53b": {
        "chains": {"solana", "ethereum", "arbitrum", "base"},
        "expo": -8,
        "quote_asset": {
            "solana": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
            "ethereum": "0xdac17f958d2ee523a2206206994597c13d831ec7",
            "arbitrum": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
            "base": "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",
        },
        "symbol": "Crypto.USDT/USD",
        "usd_reference": "USD",
        "allowed_policies": {"STABLECOIN_ORACLE_VERIFIED", "STABLECOIN_DEVIATION_BAND_POLICY"},
    },
}

TRUSTED_UNISWAP_V3_POOLS: dict[str, dict[str, Any]] = {
    # Ethereum USDC / WETH 500 bps (0.05%)
    "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640": {
        "chain": "ethereum",
        "chain_id": 1,
        "token0": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        "token1": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
        "fee": 500,
        "fee_tier": 500,
        "usd_reference": "USDC",
        "intermediate_quote": "USDC",
        "requires_chained_usd_conversion": True,
        "allowed_assets": {
            "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        },
        "allowed_policies": {"ON_CHAIN_TWAP_VERIFIED"},
    },
    # Ethereum USDC / USDT 100 bps (0.01%)
    "0x3416cf6c708da44db26246036ddf2f3939040f43": {
        "chain": "ethereum",
        "chain_id": 1,
        "token0": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        "token1": "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
        "fee": 100,
        "fee_tier": 100,
        "usd_reference": "USDC",
        "intermediate_quote": "USDC",
        "requires_chained_usd_conversion": True,
        "allowed_assets": {
            "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "0xdac17f958d2ee523a2206206994597c13d831ec7",
        },
        "allowed_policies": {"ON_CHAIN_TWAP_VERIFIED"},
    },
    # Base WETH / USDC 500 bps (0.05%)
    "0xd0b53d9277642d899df5c87a3966a349a798f224": {
        "chain": "base",
        "chain_id": 8453,
        "token0": "0x4200000000000000000000000000000000000006",  # WETH
        "token1": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "fee": 500,
        "fee_tier": 500,
        "usd_reference": "USDC",
        "intermediate_quote": "USDC",
        "requires_chained_usd_conversion": True,
        "allowed_assets": {
            "0x4200000000000000000000000000000000000006",
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        },
        "allowed_policies": {"ON_CHAIN_TWAP_VERIFIED"},
    },
}

TRUSTED_CEX_DOMAINS: dict[str, set[str]] = {
    "coinbase": {"api.exchange.coinbase.com", "api.coinbase.com", "exchange.coinbase.com"},
    "binance": {"api.binance.com", "data-api.binance.vision", "api1.binance.com", "api2.binance.com", "api3.binance.com"},
    "kraken": {"api.kraken.com"},
}

TRUSTED_CEX_MARKETS: dict[str, dict[str, dict[str, Any]]] = {
    "coinbase": {
        "USDC-USD": {
            "symbol": "USDC",
            "usd_reference": "USD",
            "quote_currency": "USD",
            "allowed_assets": {
                "ethereum": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                "base": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                "arbitrum": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
                "solana": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            },
        },
        "ETH-USD": {
            "symbol": "WETH",
            "usd_reference": "USD",
            "quote_currency": "USD",
            "allowed_assets": {
                "ethereum": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
                "base": "0x4200000000000000000000000000000000000006",
                "arbitrum": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
            },
        },
        "SOL-USD": {
            "symbol": "SOL",
            "usd_reference": "USD",
            "quote_currency": "USD",
            "allowed_assets": {
                "solana": "So11111111111111111111111111111111111111112",
            },
        },
        "USDT-USD": {
            "symbol": "USDT",
            "usd_reference": "USD",
            "quote_currency": "USD",
            "allowed_assets": {
                "ethereum": "0xdac17f958d2ee523a2206206994597c13d831ec7",
            },
        },
    },
    "binance": {
        "USDCUSDT": {
            "symbol": "USDC",
            "usd_reference": "USDT",
            "quote_currency": "USDT",
            "requires_chained_usd_conversion": True,
            "allowed_assets": {
                "ethereum": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                "base": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                "arbitrum": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
                "solana": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            },
        },
        "ETHUSDT": {
            "symbol": "WETH",
            "usd_reference": "USDT",
            "quote_currency": "USDT",
            "requires_chained_usd_conversion": True,
            "allowed_assets": {
                "ethereum": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
                "base": "0x4200000000000000000000000000000000000006",
                "arbitrum": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
            },
        },
        "SOLUSDT": {
            "symbol": "SOL",
            "usd_reference": "USDT",
            "quote_currency": "USDT",
            "requires_chained_usd_conversion": True,
            "allowed_assets": {
                "solana": "So11111111111111111111111111111111111111112",
            },
        },
    },
    "kraken": {
        "USDCUSD": {
            "symbol": "USDC",
            "usd_reference": "USD",
            "quote_currency": "USD",
            "allowed_assets": {
                "ethereum": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                "base": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                "arbitrum": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
                "solana": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            },
        },
        "ETHUSD": {
            "symbol": "WETH",
            "usd_reference": "USD",
            "quote_currency": "USD",
            "allowed_assets": {
                "ethereum": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
                "base": "0x4200000000000000000000000000000000000006",
                "arbitrum": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
            },
        },
        "SOLUSD": {
            "symbol": "SOL",
            "usd_reference": "USD",
            "quote_currency": "USD",
            "allowed_assets": {
                "solana": "So11111111111111111111111111111111111111112",
            },
        },
        "USDTUSD": {
            "symbol": "USDT",
            "usd_reference": "USD",
            "quote_currency": "USD",
            "allowed_assets": {
                "ethereum": "0xdac17f958d2ee523a2206206994597c13d831ec7",
            },
        },
    },
}

TRUSTED_DEX_AGG_DOMAINS: dict[str, set[str]] = {
    "defillama": {"coins.llama.fi", "api.llama.fi"},
    "geckoterminal": {"api.geckoterminal.com"},
    "dexscreener": {"api.dexscreener.com"},
}

TRUSTED_DEX_AGG_PAIRS: dict[str, dict[str, dict[str, Any]]] = {
    "defillama": {
        "ethereum:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": {
            "chain": "ethereum",
            "quote_asset": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "symbol": "USDC",
            "usd_reference": "USDC",
            "intermediate_quote": "USDC",
            "requires_chained_usd_conversion": True,
        },
        "ethereum:0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": {
            "chain": "ethereum",
            "quote_asset": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "symbol": "WETH",
            "usd_reference": "USD",
        },
        "solana:EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": {
            "chain": "solana",
            "quote_asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "symbol": "USDC",
            "usd_reference": "USDC",
            "intermediate_quote": "USDC",
            "requires_chained_usd_conversion": True,
        },
        "solana:So11111111111111111111111111111111111111112": {
            "chain": "solana",
            "quote_asset": "So11111111111111111111111111111111111111112",
            "symbol": "SOL",
            "usd_reference": "USD",
        },
        "base:0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": {
            "chain": "base",
            "quote_asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "symbol": "USDC",
            "usd_reference": "USDC",
            "intermediate_quote": "USDC",
            "requires_chained_usd_conversion": True,
        },
        "arbitrum:0xaf88d065e77c8cc2239327c5edb3a432268e5831": {
            "chain": "arbitrum",
            "quote_asset": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
            "symbol": "USDC",
            "usd_reference": "USDC",
            "intermediate_quote": "USDC",
            "requires_chained_usd_conversion": True,
        },
    },
    "geckoterminal": {
        "eth_0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640": {
            "chain": "ethereum",
            "quote_asset": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "symbol": "USDC",
            "usd_reference": "USDC",
            "intermediate_quote": "USDC",
            "requires_chained_usd_conversion": True,
        },
        "base_0xd0b53d9277642d899df5c87a3966a349a798f224": {
            "chain": "base",
            "quote_asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "symbol": "USDC",
            "usd_reference": "USDC",
            "intermediate_quote": "USDC",
            "requires_chained_usd_conversion": True,
        },
    },
    "dexscreener": {
        "ethereum/0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640": {
            "chain": "ethereum",
            "quote_asset": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "symbol": "USDC",
            "usd_reference": "USDC",
            "intermediate_quote": "USDC",
            "requires_chained_usd_conversion": True,
        },
        "base/0xd0b53d9277642d899df5c87a3966a349a798f224": {
            "chain": "base",
            "quote_asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "symbol": "USDC",
            "usd_reference": "USDC",
            "intermediate_quote": "USDC",
            "requires_chained_usd_conversion": True,
        },
    },
}


def _extract_domain(url: Any) -> str:
    """Extract hostname/domain from an HTTP/HTTPS URL string."""
    if not isinstance(url, str):
        return ""
    m = re.search(r"https?://([^/:\?#]+)", url.strip().lower())
    return m.group(1) if m else ""


def _extract_cex_market_from_url(provider: str, url: str) -> str | None:
    """Extract recognized CEX market symbol from a URL path or query string."""
    u = url.upper()
    for m in TRUSTED_CEX_MARKETS.get(provider, {}):
        if m in u:
            return m
    return None

RECOGNIZED_VALUATION_POLICIES: dict[str, str] = {
    "STABLECOIN_ORACLE_VERIFIED": "Empirical price reported by trusted oracle for a fiat-backed or multi-collateral stablecoin; never assumes hardcoded $1.00.",
    "CRYPTO_ORACLE_VERIFIED": "Empirical price reported by trusted oracle for a native/crypto quote asset (e.g. WETH, SOL).",
    "ON_CHAIN_TWAP_VERIFIED": "Time-weighted average price verified directly from on-chain liquidity pool reserves.",
    "DEX_AGGREGATOR_VERIFIED": "Multi-venue volume-weighted market reference from trusted indexer/aggregator.",
    "CEX_MARKET_TICKER_VERIFIED": "Consolidated public market reference from tier-1 centralized exchange ticker.",
    "STABLECOIN_DEVIATION_BAND_POLICY": "Empirical stablecoin valuation verified to reside within acceptable depeg tolerance bands without assuming constant $1.00.",
}


def validate_observation_timestamp_freshness(
    raw_stamp: Any,
    as_of_utc: Any,
    max_age_seconds: float = 300.0,
    provider_name: str = "provider",
) -> tuple[bool, str, float | None]:
    """Validate provider observation timestamp freshness independently.

    Ensures that a fresh outer wrapper timestamp cannot hide a stale, naive,
    future-dated, or missing observation timestamp.
    """
    if as_of_utc is None:
        return False, "INVALID_EVALUATION_TIMESTAMP", None
    as_of_dt = _parse_iso_utc(as_of_utc)
    if as_of_dt is None:
        return False, "INVALID_EVALUATION_TIMESTAMP", None

    if raw_stamp is None or raw_stamp == "":
        return False, "MISSING_PROVIDER_OBSERVATION_TIMESTAMP", None

    obs_dt = None
    if isinstance(raw_stamp, (int, float)) and not isinstance(raw_stamp, bool):
        val = float(raw_stamp)
        if not math.isfinite(val) or val <= 0:
            return False, "INVALID_PROVIDER_OBSERVATION_TIMESTAMP", None
        if val > 1e11:  # milliseconds
            val = val / 1000.0
        if val < 1e8:
            return False, "INVALID_PROVIDER_OBSERVATION_TIMESTAMP", None
        try:
            obs_dt = datetime.fromtimestamp(val, tz=timezone.utc)
        except Exception:
            return False, "INVALID_PROVIDER_OBSERVATION_TIMESTAMP", None
    elif isinstance(raw_stamp, str):
        s = raw_stamp.strip()
        if not s:
            return False, "MISSING_PROVIDER_OBSERVATION_TIMESTAMP", None
        try:
            val = float(s)
            if not math.isfinite(val) or val <= 0:
                return False, "INVALID_PROVIDER_OBSERVATION_TIMESTAMP", None
            if val > 1e11:
                val = val / 1000.0
            if val < 1e8:
                return False, "INVALID_PROVIDER_OBSERVATION_TIMESTAMP", None
            obs_dt = datetime.fromtimestamp(val, tz=timezone.utc)
        except ValueError:
            # Check for naive ISO string
            if ("T" in s or "-" in s) and not s.endswith("Z") and "+" not in s[10:] and "-" not in s[10:]:
                return False, "INVALID_PROVIDER_OBSERVATION_TIMESTAMP", None
            obs_dt = _parse_iso_utc(s)
            if obs_dt is None:
                return False, "INVALID_PROVIDER_OBSERVATION_TIMESTAMP", None
    elif isinstance(raw_stamp, datetime):
        if raw_stamp.tzinfo is None or raw_stamp.tzinfo.utcoffset(raw_stamp) is None:
            return False, "INVALID_PROVIDER_OBSERVATION_TIMESTAMP", None
        obs_dt = raw_stamp.astimezone(timezone.utc)
    else:
        return False, "INVALID_PROVIDER_OBSERVATION_TIMESTAMP", None

    age_seconds = (as_of_dt - obs_dt).total_seconds()
    if age_seconds < -1e-6:
        return False, "FUTURE_PROVIDER_OBSERVATION", age_seconds
    if age_seconds > max_age_seconds:
        return False, "STALE_PROVIDER_OBSERVATION", age_seconds

    return True, "FRESH", age_seconds


_PROVENANCE_SECRET = secrets.token_bytes(32)
_ADAPTER_FACTORY_SECRET = object()


def _canonical_observation_payload(
    *,
    provider: str,
    provider_identifier: str,
    chain: str,
    chain_id: int | str,
    quote_asset: str,
    feed_or_pool_id: str,
    reference_asset: str,
    valuation_policy: str,
    price_usd: float,
    observation: dict[str, Any] | None,
    observation_timestamp: str,
    retrieval_timestamp_utc: str,
    http_status: int,
    rpc_method: str | None,
    raw_payload: str | None,
    response_sha256: str | None,
    transport_type: str,
    acquisition_method: str,
    request_url: str | None = None,
    cryptographic_signature_verified: bool = False,
    feed_id: str | None = None,
    market_id: str | None = None,
    pool_address: str | None = None,
) -> bytes:
    # 1. Exact finite numeric price representation without rounding
    if price_usd is None or isinstance(price_usd, bool) or not isinstance(price_usd, (int, float)):
        raise ValueError(f"price_usd must be a non-null numeric float or int, got {price_usd!r}")
    val_price = float(price_usd)
    if not math.isfinite(val_price) or val_price <= 0.0:
        raise ValueError(f"price_usd must be positive and finite, got {price_usd!r}")

    # 2. Strict HTTP status code validation (reject 0, None, < 100, > 599)
    if http_status is None or isinstance(http_status, bool) or not isinstance(http_status, int):
        raise ValueError(f"http_status must be a valid integer status code, got {http_status!r}")
    if http_status < 100 or http_status > 599:
        raise ValueError(f"http_status must be in the range [100, 599], got {http_status!r}")
    val_http_status = int(http_status)

    # 3. Explicit None and empty handling for required fields
    if acquisition_method is None or not str(acquisition_method).strip():
        raise ValueError("acquisition_method must not be None or empty")
    if chain is None or not str(chain).strip():
        raise ValueError("chain must not be None or empty")
    if chain_id is None or not str(chain_id).strip():
        raise ValueError("chain_id must not be None or empty")
    if feed_or_pool_id is None or not str(feed_or_pool_id).strip():
        raise ValueError("feed_or_pool_id must not be None or empty")
    if observation_timestamp is None or not str(observation_timestamp).strip():
        raise ValueError("observation_timestamp must not be None or empty")
    if provider is None or not str(provider).strip():
        raise ValueError("provider must not be None or empty")
    if provider_identifier is None or not str(provider_identifier).strip():
        raise ValueError("provider_identifier must not be None or empty")
    if quote_asset is None or not str(quote_asset).strip():
        raise ValueError("quote_asset must not be None or empty")
    if reference_asset is None or not str(reference_asset).strip():
        raise ValueError("reference_asset must not be None or empty")
    if retrieval_timestamp_utc is None or not str(retrieval_timestamp_utc).strip():
        raise ValueError("retrieval_timestamp_utc must not be None or empty")
    if transport_type is None or not str(transport_type).strip():
        raise ValueError("transport_type must not be None or empty")
    if valuation_policy is None or not str(valuation_policy).strip():
        raise ValueError("valuation_policy must not be None or empty")

    # 4. Deterministic observation data handling
    if observation is None:
        val_observation = {}
    elif isinstance(observation, dict):
        val_observation = copy.deepcopy(observation)
    else:
        raise ValueError(f"observation must be a dictionary or None, got {type(observation)}")

    canonical_dict = {
        "acquisition_method": str(acquisition_method).strip(),
        "chain": str(chain).strip().lower(),
        "chain_id": str(chain_id).strip(),
        "cryptographic_signature_verified": bool(cryptographic_signature_verified) if cryptographic_signature_verified is not None else False,
        "feed_id": str(feed_id).strip().lower() if feed_id is not None else "",
        "feed_or_pool_id": str(feed_or_pool_id).strip().lower(),
        "http_status": val_http_status,
        "market_id": str(market_id).strip() if market_id is not None else "",
        "observation": val_observation,
        "observation_timestamp": str(observation_timestamp).strip(),
        "pool_address": str(pool_address).strip().lower() if pool_address is not None else "",
        "price_usd": val_price,
        "provider": str(provider).strip().lower(),
        "provider_identifier": str(provider_identifier).strip().lower(),
        "quote_asset": str(quote_asset).strip().lower(),
        "raw_payload": str(raw_payload) if raw_payload is not None else "",
        "raw_payload_sha256": str(response_sha256).strip().lower() if response_sha256 is not None else "",
        "reference_asset": str(reference_asset).strip().upper(),
        "request_url": str(request_url).strip() if request_url is not None else "",
        "retrieval_timestamp_utc": str(retrieval_timestamp_utc).strip(),
        "rpc_method": str(rpc_method).strip() if rpc_method is not None else "",
        "transport_type": str(transport_type).strip(),
        "valuation_policy": str(valuation_policy).strip(),
    }
    canonical_json = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return canonical_json.encode("utf-8")


def _generate_provenance_token(payload_bytes: bytes) -> str:
    """Generate an internal HMAC-SHA256 authentication token for canonical observation bytes.

    SECURITY ARCHITECTURE & LIMITATION NOTE:
    This token detects field mutation and accidental in-memory tampering within a trusted
    Python process.

    CRITICAL SECURITY LIMITATIONS:
    1. In-process scope: HMAC detects mutations within the boundaries of a trusted, uncompromised
       Python runtime. It does NOT protect against arbitrary in-process code execution, process memory
       injection, or an adversary who has compromised the host runtime environment.
    2. No external provider proof: In-process HMAC does NOT independently prove the external provider's
       cryptographic origin (it is not an on-chain oracle signature or TLS notary proof from Chainlink/Pyth).
       External authenticity relies on genuine HTTP/RPC responses from trusted endpoints, while HMAC
       solely verifies that the in-memory representation has not been altered after acquisition.
    """
    return hmac.new(_PROVENANCE_SECRET, payload_bytes, hashlib.sha256).hexdigest()


def _verify_provenance_token(obs: Any) -> bool:
    """Verify internal HMAC-SHA256 authentication token for an AcquiredProviderObservation.

    SECURITY LIMITATION NOTE:
    Detects in-memory mutation within a trusted process. Does not protect against arbitrary code
    execution or a compromised runtime, and does not independently prove external provider origin.

    Validates that:
    1. The object is a genuine AcquiredProviderObservation instance.
    2. The object was sealed by the genuine adapter factory, not instantiated by caller code.
    3. The internal _auth_token is present.
    4. http_status is a valid integer HTTP status code [100, 599].
    5. price_usd is a positive finite numeric value.
    6. If raw_payload is present, its SHA-256 matches response_sha256.
    7. The canonical bytes of all 23 security-relevant fields match the HMAC token exactly.
    """
    if not isinstance(obs, AcquiredProviderObservation):
        return False
    if getattr(obs, "is_caller_created", False):
        return False
    if not getattr(obs, "_auth_token", None):
        return False
    if obs.http_status is None or isinstance(obs.http_status, bool) or not isinstance(obs.http_status, int):
        return False
    if obs.http_status < 100 or obs.http_status > 599:
        return False
    if obs.price_usd is None or isinstance(obs.price_usd, bool) or not isinstance(obs.price_usd, (int, float)):
        return False
    try:
        val_price = float(obs.price_usd)
        if not math.isfinite(val_price) or val_price <= 0.0:
            return False
    except (ValueError, TypeError):
        return False
    # Validate raw_payload sha256 consistency
    if obs.raw_payload:
        actual_hash = hashlib.sha256(obs.raw_payload.encode("utf-8")).hexdigest()
        if actual_hash.lower() != str(obs.response_sha256 or "").lower():
            return False
    try:
        canonical_bytes = obs.canonical_bytes()
    except (ValueError, TypeError):
        return False
    expected = _generate_provenance_token(canonical_bytes)
    return hmac.compare_digest(obs._auth_token, expected)


class ProviderUnavailableError(Exception):
    """Raised when an authentic read-only provider acquisition request fails."""
    pass


class AcquiredProviderObservation:
    """Internal provenance representation produced strictly by genuine provider acquisition adapters.

    Cannot be manufactured by copying ordinary dictionary fields or instantiating directly without
    the module-level authenticated HMAC signature.
    """

    def __init__(
        self,
        *,
        provider: str,
        chain: str,
        quote_asset: str,
        reference_asset: str,
        price_usd: float,
        retrieval_time_utc: str,
        request_url: str = "",
        rpc_method: str | None = None,
        http_status: int = 200,
        raw_payload: str = "",
        response_sha256: str = "",
        observation: dict[str, Any] | None = None,
        feed_id: str | None = None,
        market_id: str | None = None,
        pool_address: str | None = None,
        valuation_policy: str = "ORACLE_VERIFIED",
        transport_type: str = "LIVE_NETWORK_ACQUISITION",
        acquisition_method: str = "GENUINE_READ_ONLY_HTTP_RPC",
        chain_id: int | str = "",
        observation_timestamp: str = "",
        cryptographic_signature_verified: bool = False,
        _auth_token: str | None = None,
        _internal_seal: Any = None,
    ):
        self.provider = str(provider).strip().lower()
        self.chain = str(chain).strip().lower()
        self.chain_id = chain_id
        self.quote_asset = str(quote_asset).strip()
        self.reference_asset = str(reference_asset).strip().upper()
        self.price_usd = float(price_usd) if price_usd is not None and not isinstance(price_usd, bool) else price_usd
        self.retrieval_time_utc = str(retrieval_time_utc).strip() if retrieval_time_utc is not None else None
        self.request_url = str(request_url).strip() if request_url is not None else ""
        self.rpc_method = str(rpc_method).strip() if rpc_method is not None else None
        self.http_status = int(http_status) if isinstance(http_status, int) and not isinstance(http_status, bool) else http_status
        self.raw_payload = str(raw_payload) if raw_payload is not None else ""
        self.response_sha256 = str(response_sha256 or (hashlib.sha256(self.raw_payload.encode("utf-8")).hexdigest() if self.raw_payload else "")).lower().strip()
        self.observation = copy.deepcopy(observation) if observation is not None else {}
        self.feed_id = str(feed_id).strip() if feed_id else None
        self.market_id = str(market_id).strip() if market_id else None
        self.pool_address = str(pool_address).strip() if pool_address else None
        self.valuation_policy = str(valuation_policy).strip() if valuation_policy is not None else None
        self.transport_type = str(transport_type).strip() if transport_type is not None else None
        self.acquisition_method = str(acquisition_method).strip() if acquisition_method is not None else None
        self.observation_timestamp = str(observation_timestamp or retrieval_time_utc).strip() if (observation_timestamp or retrieval_time_utc) is not None else None
        self.cryptographic_signature_verified = bool(cryptographic_signature_verified) if cryptographic_signature_verified is not None else False

        # Reject caller-created observation objects that bypass genuine adapter factory
        if _internal_seal is not _ADAPTER_FACTORY_SECRET:
            self.is_caller_created = True
        else:
            self.is_caller_created = False

        if _auth_token is not None:
            self._auth_token = _auth_token
        else:
            try:
                self._auth_token = _generate_provenance_token(self.canonical_bytes())
            except Exception:
                self._auth_token = None

    @classmethod
    def _create(cls, **kwargs: Any) -> "AcquiredProviderObservation":
        return cls(_internal_seal=_ADAPTER_FACTORY_SECRET, **kwargs)

    @property
    def provider_identifier(self) -> str:
        return self.feed_id or self.market_id or self.pool_address or self.quote_asset

    def canonical_bytes(self) -> bytes:
        return _canonical_observation_payload(
            provider=self.provider,
            provider_identifier=self.provider_identifier,
            chain=self.chain,
            chain_id=self.chain_id,
            quote_asset=self.quote_asset,
            feed_or_pool_id=self.provider_identifier,
            reference_asset=self.reference_asset,
            valuation_policy=self.valuation_policy,
            price_usd=self.price_usd,
            observation=self.observation,
            observation_timestamp=self.observation_timestamp,
            retrieval_timestamp_utc=self.retrieval_time_utc,
            http_status=self.http_status,
            rpc_method=self.rpc_method,
            raw_payload=self.raw_payload,
            response_sha256=self.response_sha256,
            transport_type=self.transport_type,
            acquisition_method=self.acquisition_method,
            request_url=self.request_url,
            cryptographic_signature_verified=self.cryptographic_signature_verified,
            feed_id=self.feed_id,
            market_id=self.market_id,
            pool_address=self.pool_address,
        )

    def to_dict(self) -> dict[str, Any]:
        d = {
            "price_usd": self.price_usd,
            "chain": self.chain,
            "chain_id": self.chain_id,
            "quote_asset": self.quote_asset,
            "reference_asset": self.reference_asset,
            "timestamp_utc": self.retrieval_time_utc,
            "source": self.provider,
            "provider": self.provider,
            "provider_identifier": self.provider_identifier,
            "valuation_policy": self.valuation_policy,
            "request_url": self.request_url,
            "rpc_method": self.rpc_method,
            "http_status": self.http_status,
            "retrieval_time_utc": self.retrieval_time_utc,
            "observation_timestamp": self.observation_timestamp,
            "raw_payload": self.raw_payload,
            "response_sha256": self.response_sha256,
            "observation": copy.deepcopy(self.observation),
            "transport_type": self.transport_type,
            "acquisition_method": self.acquisition_method,
            "cryptographic_signature_verified": self.cryptographic_signature_verified,
            "provider_acquired": True,
            "_auth_token": self._auth_token,
            "__acquired_observation__": self,
        }
        if self.feed_id:
            d["feed_id"] = self.feed_id
            d["feed_address"] = self.feed_id
            d["pair_id"] = self.feed_id
        if self.request_url:
            d["source_url"] = self.request_url
        if self.market_id:
            d["market_id"] = self.market_id
        if self.pool_address:
            d["pool_address"] = self.pool_address
        return d


def _default_http_call(url: str, data: bytes | str | None = None, headers: dict[str, str] | None = None, timeout: float = 5.0) -> tuple[int, bytes]:
    req_headers = {"User-Agent": "RocketHunter/4.30"}
    if headers:
        req_headers.update(headers)
    req_data = data.encode("utf-8") if isinstance(data, str) else data
    req = urllib.request.Request(url, data=req_data, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        return e.code, body
    except Exception as e:
        raise ProviderUnavailableError(f"HTTP request to {url} failed: {e}") from e


class ChainlinkRpcAdapter:
    PUBLIC_RPCS = {
        "ethereum": "https://ethereum-rpc.publicnode.com",
        "arbitrum": "https://arbitrum-one-rpc.publicnode.com",
        "base": "https://base-rpc.publicnode.com",
    }

    @classmethod
    def fetch(
        cls,
        quote_asset: str,
        chain: str,
        *,
        feed_address: str | None = None,
        rpc_endpoint: str | None = None,
        as_of_utc: str | None = None,
        transport: Any = None,
    ) -> AcquiredProviderObservation:
        norm_ch = str(chain).strip().lower()
        feed_addr = str(feed_address or "").strip().lower()
        if not feed_addr:
            for addr, info in TRUSTED_CHAINLINK_FEEDS.items():
                if info["chain"] == norm_ch and same_address(norm_ch, info["quote_asset"], quote_asset):
                    feed_addr = addr
                    break
        if not feed_addr or feed_addr not in TRUSTED_CHAINLINK_FEEDS:
            raise ProviderUnavailableError(f"Chainlink feed not registered for {quote_asset} on {chain}")

        feed_info = TRUSTED_CHAINLINK_FEEDS[feed_addr]
        rpc_url = rpc_endpoint or cls.PUBLIC_RPCS.get(norm_ch, "https://ethereum-rpc.publicnode.com")
        call_transport = transport or _default_http_call
        transport_type = "MOCKED_TRANSPORT" if transport is not None else "LIVE_NETWORK_ACQUISITION"
        acquisition_method = "TEST_MOCKED_TRANSPORT" if transport is not None else "GENUINE_READ_ONLY_RPC"

        # 1. eth_chainId RPC call
        cid_payload = json.dumps({"jsonrpc": "2.0", "method": "eth_chainId", "params": [], "id": 1})
        st_cid, b_cid = call_transport(rpc_url, cid_payload, {"Content-Type": "application/json"})
        if st_cid != 200:
            raise ProviderUnavailableError(f"Chainlink eth_chainId RPC failed with HTTP {st_cid}")
        r_cid = json.loads(b_cid.decode("utf-8"))
        if "error" in r_cid or not r_cid.get("result") or r_cid["result"] == "0x":
            raise ProviderUnavailableError(f"Chainlink eth_chainId RPC call failed: {r_cid}")
        actual_chain_id = int(r_cid["result"], 16)
        expected_chain_id = feed_info.get("chain_id", 1)
        if actual_chain_id != expected_chain_id:
            raise ProviderUnavailableError(
                f"Chainlink RPC endpoint chain ID mismatch: got {actual_chain_id}, expected {expected_chain_id} for {norm_ch}"
            )

        # 2. decimals() eth_call (0x313ce567) - NO SILENT FALLBACK
        dec_payload = json.dumps({"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": feed_addr, "data": "0x313ce567"}, "latest"], "id": 2})
        status_dec, body_dec = call_transport(rpc_url, dec_payload, {"Content-Type": "application/json"})
        if status_dec != 200:
            raise ProviderUnavailableError(f"Chainlink decimals() RPC failed with HTTP {status_dec}")
        resp_dec = json.loads(body_dec.decode("utf-8"))
        if "error" in resp_dec or not resp_dec.get("result") or resp_dec["result"] == "0x":
            raise ProviderUnavailableError(f"Chainlink decimals() RPC call reverted or returned empty: {resp_dec}")
        decimals = int(resp_dec["result"], 16)
        if decimals <= 0 or decimals > 18:
            raise ValueError(f"Chainlink decimals returned invalid value: {decimals}")

        # 3. latestRoundData() eth_call (0xfeaf968e)
        round_payload = json.dumps({"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": feed_addr, "data": "0xfeaf968e"}, "latest"], "id": 3})
        status_rnd, body_rnd = call_transport(rpc_url, round_payload, {"Content-Type": "application/json"})
        if status_rnd != 200:
            raise ProviderUnavailableError(f"Chainlink latestRoundData() RPC failed with HTTP {status_rnd}")

        raw_payload_str = body_rnd.decode("utf-8")
        resp_rnd = json.loads(raw_payload_str)

        if "error" in resp_rnd or not resp_rnd.get("result") or resp_rnd["result"] == "0x":
            # Fallback for proxy architecture: latestAnswer (0x50d25bcd), latestRound (0x668a0f02), latestTimestamp (0x8205bf6a)
            ans_payload = json.dumps({"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": feed_addr, "data": "0x50d25bcd"}, "latest"], "id": 4})
            st_ans, b_ans = call_transport(rpc_url, ans_payload, {"Content-Type": "application/json"})
            if st_ans != 200:
                raise ProviderUnavailableError(f"Chainlink fallback latestAnswer() RPC failed with HTTP {st_ans}")
            r_ans = json.loads(b_ans.decode("utf-8"))
            if "error" in r_ans or not r_ans.get("result") or r_ans["result"] == "0x":
                raise ProviderUnavailableError(f"Chainlink fallback latestAnswer() RPC reverted or returned empty on {feed_addr}")
            ans_int = int(r_ans["result"], 16)
            answer = ans_int if ans_int < (1 << 255) else ans_int - (1 << 256)

            rnd_payload = json.dumps({"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": feed_addr, "data": "0x668a0f02"}, "latest"], "id": 5})
            st_rnd, b_rnd = call_transport(rpc_url, rnd_payload, {"Content-Type": "application/json"})
            if st_rnd != 200:
                raise ProviderUnavailableError(f"Chainlink fallback latestRound() RPC failed with HTTP {st_rnd}")
            r_rnd = json.loads(b_rnd.decode("utf-8"))
            if "error" in r_rnd or not r_rnd.get("result") or r_rnd["result"] == "0x":
                raise ProviderUnavailableError(f"Chainlink fallback latestRound() RPC reverted or returned empty on {feed_addr}")
            round_id = int(r_rnd["result"], 16)

            ts_payload = json.dumps({"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": feed_addr, "data": "0x8205bf6a"}, "latest"], "id": 6})
            st_ts, b_ts = call_transport(rpc_url, ts_payload, {"Content-Type": "application/json"})
            if st_ts != 200:
                raise ProviderUnavailableError(f"Chainlink fallback latestTimestamp() RPC failed with HTTP {st_ts}")
            r_ts = json.loads(b_ts.decode("utf-8"))
            if "error" in r_ts or not r_ts.get("result") or r_ts["result"] == "0x":
                raise ProviderUnavailableError(f"Chainlink fallback latestTimestamp() RPC reverted or returned empty on {feed_addr}")
            updated_at_ts = int(r_ts["result"], 16)

            answered_in_round = round_id
            started_at_ts = updated_at_ts
            raw_payload_str = b_ans.decode("utf-8")
        else:
            hex_data = resp_rnd["result"].lower().replace("0x", "")
            if len(hex_data) < 320:
                raise ProviderUnavailableError(f"Chainlink latestRoundData returned truncated hex: {hex_data}")
            round_id = int(hex_data[0:64], 16)
            ans_u = int(hex_data[64:128], 16)
            answer = ans_u if ans_u < (1 << 255) else ans_u - (1 << 256)
            started_at_ts = int(hex_data[128:192], 16)
            updated_at_ts = int(hex_data[192:256], 16)
            answered_in_round = int(hex_data[256:320], 16)

        if round_id <= 0:
            raise ValueError(f"Chainlink round_id must be strictly positive, got {round_id}")
        if answer <= 0:
            raise ValueError(f"Chainlink answer must be strictly positive, got {answer}")
        if answered_in_round < round_id:
            raise ValueError(f"Chainlink round incomplete: answeredInRound ({answered_in_round}) < roundId ({round_id})")
        if updated_at_ts <= 0:
            raise ValueError(f"Chainlink updatedAt timestamp must be strictly positive, got {updated_at_ts}")

        price_usd = answer / (10 ** decimals)
        retrieval_stamp = as_of_utc or datetime.now(timezone.utc).isoformat()
        updated_dt = datetime.fromtimestamp(updated_at_ts, tz=timezone.utc) if updated_at_ts else _parse_iso_utc(retrieval_stamp)
        updated_iso = updated_dt.isoformat().replace("+00:00", "Z")

        resp_hash = hashlib.sha256(raw_payload_str.encode("utf-8")).hexdigest()

        obs_dict = {
            "answer": answer,
            "decimals": decimals,
            "round_id": round_id,
            "answered_in_round": answered_in_round,
            "started_at": started_at_ts,
            "updated_at": updated_iso,
            "chain_id": actual_chain_id,
        }

        return AcquiredProviderObservation._create(
            provider="chainlink",
            chain=norm_ch,
            chain_id=actual_chain_id,
            quote_asset=feed_info["quote_asset"],
            reference_asset="USD",
            price_usd=price_usd,
            retrieval_time_utc=retrieval_stamp,
            request_url=rpc_url,
            rpc_method="eth_call:latestRoundData()",
            http_status=200,
            raw_payload=raw_payload_str,
            response_sha256=resp_hash,
            observation=obs_dict,
            feed_id=feed_addr,
            valuation_policy="STABLECOIN_ORACLE_VERIFIED" if price_usd <= 1.05 else "CRYPTO_ORACLE_VERIFIED",
            transport_type=transport_type,
            acquisition_method=acquisition_method,
            observation_timestamp=updated_iso,
        )


class PythHermesAdapter:
    DEFAULT_HERMES_URL = "https://hermes.pyth.network"

    @classmethod
    def fetch(
        cls,
        quote_asset: str,
        chain: str,
        *,
        feed_id: str | None = None,
        as_of_utc: str | None = None,
        transport: Any = None,
    ) -> AcquiredProviderObservation:
        norm_ch = str(chain).strip().lower()
        fid = str(feed_id or "").strip().lower()
        if not fid:
            for f_id, info in TRUSTED_PYTH_FEEDS.items():
                if norm_ch in info["chains"] and same_address(norm_ch, info["quote_asset"].get(norm_ch, ""), quote_asset):
                    fid = f_id
                    break
        if not fid or fid not in TRUSTED_PYTH_FEEDS:
            raise ProviderUnavailableError(f"Pyth feed not registered for {quote_asset} on {chain}")

        feed_info = TRUSTED_PYTH_FEEDS[fid]
        clean_fid = fid.lower().replace("0x", "")
        url = f"{cls.DEFAULT_HERMES_URL}/v2/updates/price/latest?ids[]={clean_fid}"
        call_transport = transport or _default_http_call
        transport_type = "MOCKED_TRANSPORT" if transport is not None else "LIVE_NETWORK_ACQUISITION"
        acquisition_method = "TEST_MOCKED_TRANSPORT" if transport is not None else "GENUINE_READ_ONLY_REST"

        status_code, body_bytes = call_transport(url, None, {})
        if status_code != 200:
            raise ProviderUnavailableError(f"Pyth Hermes API failed with HTTP {status_code}")

        raw_payload_str = body_bytes.decode("utf-8")
        data = json.loads(raw_payload_str)
        updates = data if isinstance(data, list) else data.get("parsed", [])
        if not updates or not isinstance(updates, list):
            raise ProviderUnavailableError("Pyth Hermes returned empty updates list")

        matching_items = []
        for item in updates:
            item_id = str(item.get("id", "")).lower().replace("0x", "")
            if not item_id:
                raise ProviderUnavailableError("Pyth update item missing 'id' field")
            if item_id == clean_fid:
                matching_items.append(item)

        if len(matching_items) == 0:
            raise ProviderUnavailableError(f"Pyth response missing requested feed ID {clean_fid}")
        if len(matching_items) > 1:
            raise ProviderUnavailableError(f"Pyth response contains duplicate entries for feed ID {clean_fid}")

        matched_item = matching_items[0]

        price_obj = matched_item.get("price")
        if not isinstance(price_obj, dict):
            raise ProviderUnavailableError("Pyth update missing 'price' object")
        if "price" not in price_obj or "expo" not in price_obj or "publish_time" not in price_obj:
            raise ProviderUnavailableError("Pyth update price object missing required fields (price, expo, publish_time)")

        raw_price = int(price_obj["price"])
        expo = int(price_obj["expo"])
        publish_time_ts = int(price_obj["publish_time"])

        if raw_price <= 0:
            raise ValueError(f"Pyth price must be strictly positive, got {raw_price}")
        if expo < -18 or expo > 0:
            raise ValueError(f"Pyth exponent out of valid range [-18, 0]: {expo}")
        if publish_time_ts <= 0:
            raise ValueError(f"Pyth publish_time must be positive timestamp, got {publish_time_ts}")

        price_usd = raw_price * (10 ** expo)
        retrieval_stamp = as_of_utc or datetime.now(timezone.utc).isoformat()
        pub_dt = datetime.fromtimestamp(publish_time_ts, tz=timezone.utc)
        pub_iso = pub_dt.isoformat().replace("+00:00", "Z")

        resp_hash = hashlib.sha256(raw_payload_str.encode("utf-8")).hexdigest()

        obs_dict = {
            "feed_id": fid,
            "price": raw_price,
            "expo": expo,
            "publish_time": pub_iso,
            "signature_status": "TLS_TRANSPORT_PROVIDER_DATA_WITHOUT_ON_CHAIN_SIGNATURE_VERIFICATION",
            "cryptographic_signature_verified": False,
        }

        return AcquiredProviderObservation._create(
            provider="pyth",
            chain=norm_ch,
            chain_id=norm_ch,
            quote_asset=quote_asset,
            reference_asset="USD",
            price_usd=price_usd,
            retrieval_time_utc=retrieval_stamp,
            request_url=url,
            http_status=status_code,
            raw_payload=raw_payload_str,
            response_sha256=resp_hash,
            observation=obs_dict,
            feed_id=fid,
            valuation_policy="STABLECOIN_ORACLE_VERIFIED" if price_usd <= 1.05 else "CRYPTO_ORACLE_VERIFIED",
            transport_type=transport_type,
            acquisition_method=acquisition_method,
            observation_timestamp=pub_iso,
        )


class CexTickerAdapter:
    URL_TEMPLATES = {
        "coinbase": "https://api.exchange.coinbase.com/products/{market_id}/ticker",
        "binance": "https://api.binance.com/api/v3/ticker/24hr?symbol={market_id}",
        "kraken": "https://api.kraken.com/0/public/Ticker?pair={market_id}",
    }

    @classmethod
    def fetch(
        cls,
        provider: str,
        quote_asset: str,
        chain: str,
        *,
        market_id: str | None = None,
        as_of_utc: str | None = None,
        transport: Any = None,
    ) -> AcquiredProviderObservation:
        canon_p = PROVIDER_ALIAS_MAP.get(str(provider).strip().lower(), str(provider).strip().lower())
        if canon_p not in cls.URL_TEMPLATES:
            raise ProviderUnavailableError(f"CEX provider '{provider}' not supported")
        norm_ch = str(chain).strip().lower()
        m_id = str(market_id or "").strip().upper()
        if not m_id:
            for mkt, minfo in TRUSTED_CEX_MARKETS.get(canon_p, {}).items():
                alloc = minfo.get("allowed_assets", {})
                if norm_ch in alloc and same_address(norm_ch, alloc[norm_ch], quote_asset):
                    m_id = mkt
                    break
        if not m_id or m_id not in TRUSTED_CEX_MARKETS.get(canon_p, {}):
            raise ProviderUnavailableError(f"Market '{m_id}' not registered in TRUSTED_CEX_MARKETS for {canon_p}")

        market_info = TRUSTED_CEX_MARKETS[canon_p][m_id]
        url = cls.URL_TEMPLATES[canon_p].format(market_id=m_id)
        call_transport = transport or _default_http_call
        transport_type = "MOCKED_TRANSPORT" if transport is not None else "LIVE_NETWORK_ACQUISITION"
        acquisition_method = "TEST_MOCKED_TRANSPORT" if transport is not None else "GENUINE_READ_ONLY_REST"

        status_code, body_bytes = call_transport(url, None, {})
        if status_code != 200:
            raise ProviderUnavailableError(f"{canon_p} API returned HTTP {status_code}")

        raw_payload_str = body_bytes.decode("utf-8")
        data = json.loads(raw_payload_str)

        retrieval_stamp = as_of_utc or datetime.now(timezone.utc).isoformat()
        quote_curr = market_info.get("quote_currency", "USD")

        if canon_p == "coinbase":
            lp = float(data.get("price", 0))
            bp = float(data.get("bid", 0))
            ap = float(data.get("ask", 0))
            vol = float(data.get("volume", 0))
            obs_stamp = data.get("time") or retrieval_stamp
        elif canon_p == "binance":
            lp = float(data.get("lastPrice", 0))
            bp = float(data.get("bidPrice", 0))
            ap = float(data.get("askPrice", 0))
            vol = float(data.get("volume", 0))
            close_time = data.get("closeTime")
            if close_time:
                obs_stamp = datetime.fromtimestamp(close_time / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            else:
                obs_stamp = retrieval_stamp
        else:  # kraken
            res_dict = data.get("result", {})
            mkt_data = res_dict.get(m_id) or next(iter(res_dict.values()), {})
            lp = float(mkt_data.get("c", [0])[0])
            bp = float(mkt_data.get("b", [0])[0])
            ap = float(mkt_data.get("a", [0])[0])
            v_val = mkt_data.get("v", [0, 0])
            vol = float(v_val[1] if isinstance(v_val, list) and len(v_val) > 1 else (v_val[0] if isinstance(v_val, list) and v_val else 0))
            obs_stamp = retrieval_stamp

        if not (bp <= lp <= ap):
            raise ValueError(f"CEX spread violation: requires bid ({bp}) <= last ({lp}) <= ask ({ap})")

        resp_hash = hashlib.sha256(raw_payload_str.encode("utf-8")).hexdigest()

        obs_dict = {
            "symbol": m_id,
            "last_price": lp,
            "bid_price": bp,
            "ask_price": ap,
            "volume_24h": vol,
            "timestamp": obs_stamp,
        }

        return AcquiredProviderObservation._create(
            provider=canon_p,
            chain=norm_ch,
            chain_id="CEX_OFF_CHAIN",
            quote_asset=quote_asset,
            reference_asset=quote_curr,
            price_usd=lp,
            retrieval_time_utc=retrieval_stamp,
            request_url=url,
            http_status=status_code,
            raw_payload=raw_payload_str,
            response_sha256=resp_hash,
            observation=obs_dict,
            market_id=m_id,
            valuation_policy="CEX_MARKET_TICKER_VERIFIED",
            transport_type=transport_type,
            acquisition_method=acquisition_method,
            observation_timestamp=obs_stamp,
        )


class UniswapV3TwapAdapter:
    PUBLIC_RPCS = {
        "ethereum": "https://ethereum-rpc.publicnode.com",
        "arbitrum": "https://arbitrum-one-rpc.publicnode.com",
        "base": "https://base-rpc.publicnode.com",
    }

    @classmethod
    def fetch(
        cls,
        quote_asset: str,
        chain: str,
        *,
        pool_address: str | None = None,
        twap_window_seconds: float = 180.0,
        rpc_endpoint: str | None = None,
        as_of_utc: str | None = None,
        transport: Any = None,
    ) -> AcquiredProviderObservation:
        norm_ch = str(chain).strip().lower()
        p_addr = str(pool_address or "").strip().lower()
        if not p_addr:
            for addr, info in TRUSTED_UNISWAP_V3_POOLS.items():
                if info["chain"] == norm_ch and (
                    same_address(norm_ch, info["token0"], quote_asset) or
                    same_address(norm_ch, info["token1"], quote_asset)
                ):
                    p_addr = addr
                    break
        if not p_addr or p_addr not in TRUSTED_UNISWAP_V3_POOLS:
            raise ProviderUnavailableError(f"Pool '{p_addr}' not registered in TRUSTED_UNISWAP_V3_POOLS")

        pool_info = TRUSTED_UNISWAP_V3_POOLS[p_addr]
        rpc_url = rpc_endpoint or cls.PUBLIC_RPCS.get(norm_ch, "https://ethereum-rpc.publicnode.com")
        call_transport = transport or _default_http_call
        transport_type = "MOCKED_TRANSPORT" if transport is not None else "LIVE_NETWORK_ACQUISITION"
        acquisition_method = "TEST_MOCKED_TRANSPORT" if transport is not None else "GENUINE_READ_ONLY_RPC"

        # 1. eth_chainId RPC call
        cid_payload = json.dumps({"jsonrpc": "2.0", "method": "eth_chainId", "params": [], "id": 1})
        st_cid, b_cid = call_transport(rpc_url, cid_payload, {"Content-Type": "application/json"})
        if st_cid != 200:
            raise ProviderUnavailableError(f"Uniswap v3 eth_chainId RPC failed with HTTP {st_cid}")
        r_cid = json.loads(b_cid.decode("utf-8"))
        if "error" in r_cid or not r_cid.get("result") or r_cid["result"] == "0x":
            raise ProviderUnavailableError(f"Uniswap v3 eth_chainId RPC call failed: {r_cid}")
        actual_chain_id = int(r_cid["result"], 16)
        expected_chain_id = pool_info.get("chain_id", 1)
        if actual_chain_id != expected_chain_id:
            raise ProviderUnavailableError(
                f"Uniswap v3 RPC endpoint chain ID mismatch: got {actual_chain_id}, expected {expected_chain_id} for {norm_ch}"
            )

        # 2. token0() eth_call (0x0dfe1681)
        t0_payload = json.dumps({"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": p_addr, "data": "0x0dfe1681"}, "latest"], "id": 2})
        st_t0, b_t0 = call_transport(rpc_url, t0_payload, {"Content-Type": "application/json"})
        if st_t0 != 200:
            raise ProviderUnavailableError(f"Uniswap v3 token0() RPC failed with HTTP {st_t0}")
        r_t0 = json.loads(b_t0.decode("utf-8"))
        if "error" in r_t0 or not r_t0.get("result") or len(r_t0["result"].replace("0x", "")) < 40:
            raise ProviderUnavailableError(f"Uniswap v3 token0() RPC call failed: {r_t0}")
        actual_t0 = "0x" + r_t0["result"].lower().replace("0x", "")[-40:]
        if not same_address(norm_ch, actual_t0, pool_info["token0"]):
            raise ProviderUnavailableError(
                f"Uniswap v3 pool token0 mismatch: got {actual_t0}, expected {pool_info['token0']}"
            )

        # 3. token1() eth_call (0xd21220a7)
        t1_payload = json.dumps({"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": p_addr, "data": "0xd21220a7"}, "latest"], "id": 3})
        st_t1, b_t1 = call_transport(rpc_url, t1_payload, {"Content-Type": "application/json"})
        if st_t1 != 200:
            raise ProviderUnavailableError(f"Uniswap v3 token1() RPC failed with HTTP {st_t1}")
        r_t1 = json.loads(b_t1.decode("utf-8"))
        if "error" in r_t1 or not r_t1.get("result") or len(r_t1["result"].replace("0x", "")) < 40:
            raise ProviderUnavailableError(f"Uniswap v3 token1() RPC call failed: {r_t1}")
        actual_t1 = "0x" + r_t1["result"].lower().replace("0x", "")[-40:]
        if not same_address(norm_ch, actual_t1, pool_info["token1"]):
            raise ProviderUnavailableError(
                f"Uniswap v3 pool token1 mismatch: got {actual_t1}, expected {pool_info['token1']}"
            )

        # 4. fee() eth_call (0xddca3f43)
        fee_payload = json.dumps({"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": p_addr, "data": "0xddca3f43"}, "latest"], "id": 4})
        st_fee, b_fee = call_transport(rpc_url, fee_payload, {"Content-Type": "application/json"})
        if st_fee != 200:
            raise ProviderUnavailableError(f"Uniswap v3 fee() RPC failed with HTTP {st_fee}")
        r_fee = json.loads(b_fee.decode("utf-8"))
        if "error" in r_fee or not r_fee.get("result") or r_fee["result"] == "0x":
            raise ProviderUnavailableError(f"Uniswap v3 fee() RPC call failed: {r_fee}")
        actual_fee = int(r_fee["result"], 16)
        expected_fee = pool_info.get("fee_tier", pool_info.get("fee", 3000))
        if actual_fee != expected_fee:
            raise ProviderUnavailableError(
                f"Uniswap v3 pool fee tier mismatch: got {actual_fee}, expected {expected_fee}"
            )

        # 5. observe(uint32[] secondsAgos) ABI encoding: selector 0x88316456
        win_int = int(twap_window_seconds)
        if win_int < 60:
            raise ValueError(f"Uniswap v3 TWAP window must be >= 60 seconds, got {win_int}")
        data_hex = "0x88316456" + "00" * 31 + "20" + "00" * 31 + "02" + hex(win_int)[2:].zfill(64) + "00" * 32
        rpc_payload = json.dumps({"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": p_addr, "data": data_hex}, "latest"], "id": 5})

        status_code, body_bytes = call_transport(rpc_url, rpc_payload, {"Content-Type": "application/json"})
        if status_code != 200:
            raise ProviderUnavailableError(f"Uniswap v3 observe() RPC failed with HTTP {status_code}")

        raw_payload_str = body_bytes.decode("utf-8")
        resp_json = json.loads(raw_payload_str)
        if "error" in resp_json or not resp_json.get("result") or resp_json["result"] == "0x":
            raise ProviderUnavailableError(f"Uniswap v3 observe() call reverted or returned empty: {resp_json}")

        res_hex = resp_json["result"].lower().replace("0x", "")
        if len(res_hex) < 64 * 5:
            raise ProviderUnavailableError(f"Truncated observe() response: {res_hex}")

        words = [res_hex[i:i+64] for i in range(0, len(res_hex), 64)]
        n_ticks = int(words[2], 16)
        if n_ticks < 2:
            raise ValueError("Uniswap v3 observe() returned fewer than 2 tickCumulatives")

        tick_cums = []
        for i in range(n_ticks):
            val_u = int(words[3 + i], 16)
            signed_val = val_u if val_u < (1 << 255) else val_u - (1 << 256)
            tick_cums.append(signed_val)

        delta_tick = tick_cums[1] - tick_cums[0]
        avg_tick = delta_tick / win_int
        calc_ratio = 1.0001 ** avg_tick

        if calc_ratio > 0:
            if calc_ratio < 1e-4:
                twap_price = 1.0 / (calc_ratio * 1e-12)
            else:
                twap_price = calc_ratio
        else:
            twap_price = 1.0

        retrieval_stamp = as_of_utc or datetime.now(timezone.utc).isoformat()
        resp_hash = hashlib.sha256(raw_payload_str.encode("utf-8")).hexdigest()

        obs_dict = {
            "twap_window_seconds": win_int,
            "twap_price": twap_price,
            "tick_cumulatives": [tick_cums[0], tick_cums[1]],
            "timestamp": retrieval_stamp,
            "chain_id": actual_chain_id,
            "token0": actual_t0,
            "token1": actual_t1,
            "fee": actual_fee,
        }

        return AcquiredProviderObservation._create(
            provider="uniswap_v3_twap",
            chain=norm_ch,
            chain_id=actual_chain_id,
            quote_asset=quote_asset,
            reference_asset=pool_info.get("intermediate_quote", "USDC"),
            price_usd=twap_price,
            retrieval_time_utc=retrieval_stamp,
            request_url=rpc_url,
            rpc_method="eth_call:observe(uint32[])",
            http_status=200,
            raw_payload=raw_payload_str,
            response_sha256=resp_hash,
            observation=obs_dict,
            pool_address=p_addr,
            valuation_policy="ON_CHAIN_TWAP_VERIFIED",
            transport_type=transport_type,
            acquisition_method=acquisition_method,
            observation_timestamp=retrieval_stamp,
        )


class DefiLlamaPricesAdapter:
    DEFAULT_BASE_URL = "https://coins.llama.fi/prices/current"

    @classmethod
    def fetch(
        cls,
        quote_asset: str,
        chain: str,
        *,
        as_of_utc: str | None = None,
        transport: Any = None,
    ) -> AcquiredProviderObservation:
        norm_ch = str(chain).strip().lower()
        token_key = f"{norm_ch}:{quote_asset}"
        url = f"{cls.DEFAULT_BASE_URL}/{token_key}"
        call_transport = transport or _default_http_call
        transport_type = "MOCKED_TRANSPORT" if transport is not None else "LIVE_NETWORK_ACQUISITION"
        acquisition_method = "TEST_MOCKED_TRANSPORT" if transport is not None else "GENUINE_READ_ONLY_REST"

        status_code, body_bytes = call_transport(url, None, {})
        if status_code != 200:
            raise ProviderUnavailableError(f"DefiLlama API failed with HTTP {status_code}")

        raw_payload_str = body_bytes.decode("utf-8")
        data = json.loads(raw_payload_str)
        coins = data.get("coins", {})
        token_data = coins.get(token_key) or coins.get(f"{norm_ch}:{quote_asset.lower()}")
        if not token_data:
            raise ProviderUnavailableError(f"DefiLlama returned no coin data for {token_key}")

        price_val = float(token_data.get("price", 0))
        if price_val <= 0:
            raise ValueError(f"DefiLlama price must be positive, got {price_val}")

        ts = int(token_data.get("timestamp", 0))
        retrieval_stamp = as_of_utc or datetime.now(timezone.utc).isoformat()
        obs_stamp = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z") if ts else retrieval_stamp

        resp_hash = hashlib.sha256(raw_payload_str.encode("utf-8")).hexdigest()

        obs_dict = {
            "price": price_val,
            "token": quote_asset,
            "chain": norm_ch,
            "timestamp": obs_stamp,
        }

        return AcquiredProviderObservation._create(
            provider="defillama",
            chain=norm_ch,
            chain_id=norm_ch,
            quote_asset=quote_asset,
            reference_asset="USD",
            price_usd=price_val,
            retrieval_time_utc=retrieval_stamp,
            request_url=url,
            http_status=status_code,
            raw_payload=raw_payload_str,
            response_sha256=resp_hash,
            observation=obs_dict,
            feed_id=token_key,
            valuation_policy="DEX_AGGREGATOR_VERIFIED",
            transport_type=transport_type,
            acquisition_method=acquisition_method,
            observation_timestamp=obs_stamp,
        )


def acquire_provider_observation(
    provider: str,
    quote_asset: str,
    chain: str,
    *,
    market_or_feed_id: str | None = None,
    as_of_utc: str | None = None,
    twap_window_seconds: float = 180.0,
    transport: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Genuine read-only provider acquisition layer.

    Dispatches to authentic provider adapters (Chainlink RPC, Pyth Hermes, CEX Ticker,
    Uniswap v3 TWAP, DefiLlama Prices).

    Never manufactures synthetic provider responses from caller-supplied prices.
    If real integration fails or is unavailable, returns PROVIDER_DATA_UNAVAILABLE and fails closed.
    """
    canon_p = PROVIDER_ALIAS_MAP.get(str(provider).strip().lower(), str(provider).strip().lower())
    norm_ch = str(chain).strip().lower()

    try:
        if canon_p == "chainlink":
            obs = ChainlinkRpcAdapter.fetch(
                quote_asset, norm_ch,
                feed_address=market_or_feed_id,
                as_of_utc=as_of_utc,
                transport=transport,
            )
        elif canon_p == "pyth":
            obs = PythHermesAdapter.fetch(
                quote_asset, norm_ch,
                feed_id=market_or_feed_id,
                as_of_utc=as_of_utc,
                transport=transport,
            )
        elif canon_p in {"coinbase", "binance", "kraken"}:
            obs = CexTickerAdapter.fetch(
                canon_p, quote_asset, norm_ch,
                market_id=market_or_feed_id,
                as_of_utc=as_of_utc,
                transport=transport,
            )
        elif canon_p == "uniswap_v3_twap":
            obs = UniswapV3TwapAdapter.fetch(
                quote_asset, norm_ch,
                pool_address=market_or_feed_id,
                twap_window_seconds=twap_window_seconds,
                as_of_utc=as_of_utc,
                transport=transport,
            )
        elif canon_p == "defillama":
            obs = DefiLlamaPricesAdapter.fetch(
                quote_asset, norm_ch,
                as_of_utc=as_of_utc,
                transport=transport,
            )
        else:
            return {
                "status": "PROVIDER_DATA_UNAVAILABLE",
                "provider": canon_p,
                "chain": norm_ch,
                "quote_asset": quote_asset,
                "price_usd": None,
                "reason": f"unsupported or unconfigured provider: '{provider}'",
            }
        return obs.to_dict()
    except Exception as e:
        return {
            "status": "PROVIDER_DATA_UNAVAILABLE",
            "provider": canon_p,
            "chain": norm_ch,
            "quote_asset": quote_asset,
            "price_usd": None,
            "reason": f"genuine provider acquisition failed: {e}",
        }



def validate_quote_usd_conversion(
    conversion: Any,
    *,
    expected_chain: str | None = None,
    expected_quote_asset: str | None = None,
    as_of_utc: str | None = None,
    max_conversion_age_seconds: float = 300.0,
    _recursion_depth: int = 0,
) -> tuple[bool, str, float | None, dict[str, Any]]:
    """Validate quote-asset/USD conversion evidence, observation authenticity, and freshness.

    Enforces:
    - Complete conversion dictionary with positive, finite conversion price.
    - Explicit, timezone-aware timestamp within max_conversion_age_seconds.
    - Explicit chain identity matching expected_chain.
    - Explicit quote asset address matching expected_quote_asset with valid format.
    - Explicit USD reference asset identity (e.g. 'USD', 'USDC', 'USDT').
    - Recognized valuation provider; conflicting provider evidence fails closed.
    - Recognized valuation policy strictly compatible with that provider and feed type.
    - Provider-specific feed registry binding:
        - Chainlink: feed address registered in TRUSTED_CHAINLINK_FEEDS matching chain & asset.
        - Pyth: feed ID registered in TRUSTED_PYTH_FEEDS matching chain & asset.
        - Uniswap v3 TWAP: pool address registered in TRUSTED_UNISWAP_V3_POOLS matching chain & asset.
        - CEX tickers (Coinbase, Binance, Kraken): registered market ID & trusted domain.
        - DEX aggregators (DefiLlama, GeckoTerminal, DexScreener): registered pair ID & trusted domain.
    - Independent observation timestamp freshness validation:
        - Chainlink: updatedAt validated independently against evaluation time.
        - Pyth: publish_time validated independently against evaluation time.
        - Uniswap v3 TWAP: observation timestamp validated independently.
        - CEX: observation timestamp required and validated independently.
        - DEX aggregators: observation timestamp required and validated independently.
        - Reject future-dated, stale, malformed, timezone-naive, or missing observation timestamps.
        - A fresh wrapper timestamp never makes an old provider observation appear fresh.
    - Provider-specific observation verification:
        - Chainlink: requires answer, decimals (8), round_id (>0), updated_at; checks answered_in_round and chain_id.
        - Pyth: requires price, expo, publish_time; validates feed-specific canonical expo.
        - Uniswap v3 TWAP: requires mandatory twap_price and twap_window_seconds (>= 60s); verifies on-chain tick cumulatives.
        - CEX: requires symbol, last_price, bid_price, ask_price, volume_24h (>0), bid_price <= last_price <= ask_price.
        - DEX aggregators: requires price, token, chain, timestamp.
    - Provider Acquisition Boundary & Payload Integrity:
        - Verifies request URL / RPC method against trusted provider endpoints.
        - Verifies response payload SHA-256 integrity hash where supplied.
        - Distinguishes authenticated provider acquisition from caller-supplied structural consistency.
    - Strict Chained USD Conversion & Multi-leg Path Resolution:
        - USDT is never treated as USD; reference_asset="USDT" requires verified chained USDT/USD conversion.
        - Markets quoting in USDT claiming USD reference asset fail with REFERENCE_CURRENCY_MISMATCH.
        - Uniswap v3 TWAP and DEX aggregator pairs represent token ratios, requiring verified usd_conversion_leg to USD.
        - Intermediate assets must explicitly match required identity across ALL candidate address fields.
        - Every conversion leg is validated independently for identity and freshness.
        - Chained conversion paths compound price: clean_price * chained_rate.

    Returns:
    (is_valid, status, price_usd, details)
    """
    if isinstance(conversion, AcquiredProviderObservation):
        conversion = conversion.to_dict()

    if not isinstance(conversion, dict) or not conversion:
        return False, "MISSING_QUOTE_CONVERSION_EVIDENCE", None, {
            "reason": "quote-asset USD conversion evidence is missing"
        }

    if conversion.get("status") == "PROVIDER_DATA_UNAVAILABLE":
        return False, "PROVIDER_DATA_UNAVAILABLE", None, {
            "reason": conversion.get("reason", "provider data unavailable"),
            "conversion_status": "PROVIDER_DATA_UNAVAILABLE",
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

    # Immediate Provenance & Tampering Integrity Verification
    actual_obs = conversion.get("__acquired_observation__") if isinstance(conversion, dict) else (
        conversion if isinstance(conversion, AcquiredProviderObservation) else None
    )
    if actual_obs is not None:
        if getattr(actual_obs, "is_caller_created", False):
            return False, "CALLER_CREATED_OBSERVATION_OBJECT", None, {
                "reason": "Caller-created AcquiredProviderObservation objects are rejected; must be acquired via genuine adapter"
            }
        if actual_obs.raw_payload:
            calc_sha = hashlib.sha256(actual_obs.raw_payload.encode("utf-8")).hexdigest()
            if str(actual_obs.response_sha256 or "").lower() != calc_sha.lower():
                return False, "PAYLOAD_HASH_MISMATCH", None, {
                    "reason": f"observation raw_payload hash ({calc_sha}) does not match response_sha256 ({actual_obs.response_sha256})"
                }
        if not _verify_provenance_token(actual_obs):
            return False, "INVALID_PROVENANCE_SIGNATURE", None, {
                "reason": "observation HMAC signature verification failed; observation data was tampered or mutated"
            }
        # Check dictionary mutations vs actual_obs
        if clean_price != actual_obs.price_usd:
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict price ({clean_price}) does not match authenticated observation ({actual_obs.price_usd})"
            }
        conv_chain_raw = conversion.get("chain")
        if conv_chain_raw and str(conv_chain_raw).strip().lower() != actual_obs.chain.lower():
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict chain ({conv_chain_raw}) does not match authenticated observation ({actual_obs.chain})"
            }
        for q_key in ("quote_asset", "token_address", "contract_address"):
            if q_key in conversion and conversion[q_key]:
                q_val = str(conversion[q_key]).strip()
                if not same_address(actual_obs.chain, q_val, actual_obs.quote_asset) and q_val.lower() != actual_obs.quote_asset.lower():
                    return False, "OBSERVATION_MUTATION_DETECTED", None, {
                        "reason": f"conversion dict {q_key} does not match authenticated observation"
                    }
        if "observation" in conversion and isinstance(conversion["observation"], dict) and actual_obs.observation:
            if conversion["observation"] != actual_obs.observation:
                return False, "OBSERVATION_MUTATION_DETECTED", None, {
                    "reason": "conversion dict observation payload does not match authenticated observation"
                }
        if "raw_payload" in conversion and str(conversion["raw_payload"]) != actual_obs.raw_payload:
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": "conversion dict raw_payload does not match authenticated observation"
            }
        if "response_sha256" in conversion and str(conversion["response_sha256"]).lower() != str(actual_obs.response_sha256 or "").lower():
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": "conversion dict response_sha256 does not match authenticated observation"
            }
        for id_key in ("feed_address", "feed_id", "market_id", "pool_address"):
            if id_key in conversion and conversion[id_key]:
                id_val = str(conversion[id_key]).strip()
                if actual_obs.provider_identifier and not same_address(actual_obs.chain, id_val, actual_obs.provider_identifier) and id_val.lower() != actual_obs.provider_identifier.lower():
                    return False, "OBSERVATION_MUTATION_DETECTED", None, {
                        "reason": f"conversion dict {id_key} ({id_val}) does not match authenticated observation ({actual_obs.provider_identifier})"
                    }
        req_url_candidate = conversion.get("request_url") or conversion.get("source_url")
        if req_url_candidate and str(req_url_candidate).strip() != actual_obs.request_url:
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict request_url ({req_url_candidate}) does not match authenticated observation ({actual_obs.request_url})"
            }
        if "rpc_method" in conversion and conversion.get("rpc_method") != actual_obs.rpc_method:
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict rpc_method ({conversion.get('rpc_method')}) does not match authenticated observation ({actual_obs.rpc_method})"
            }
        if "transport_type" in conversion and str(conversion.get("transport_type")).strip() != actual_obs.transport_type:
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict transport_type ({conversion.get('transport_type')}) does not match authenticated observation ({actual_obs.transport_type})"
            }
        cand_policy = conversion.get("valuation_policy") or conversion.get("policy")
        if cand_policy and str(cand_policy).strip().upper() != actual_obs.valuation_policy.upper():
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict valuation_policy ({cand_policy}) does not match authenticated observation ({actual_obs.valuation_policy})"
            }
        cand_ref = conversion.get("usd_reference_asset") or conversion.get("reference_asset") or conversion.get("usd_reference")
        if cand_ref and str(cand_ref).strip().upper() != actual_obs.reference_asset.upper():
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict reference_asset ({cand_ref}) does not match authenticated observation ({actual_obs.reference_asset})"
            }
        if "acquisition_method" in conversion and str(conversion.get("acquisition_method")).strip() != actual_obs.acquisition_method:
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict acquisition_method ({conversion.get('acquisition_method')}) does not match authenticated observation ({actual_obs.acquisition_method})"
            }
        if "cryptographic_signature_verified" in conversion and bool(conversion.get("cryptographic_signature_verified")) != bool(actual_obs.cryptographic_signature_verified):
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": "conversion dict cryptographic_signature_verified does not match authenticated observation"
            }
    else:
        if isinstance(conversion, dict) and conversion.get("_auth_token"):
            return False, "INVALID_PROVENANCE_SIGNATURE", None, {
                "reason": "dictionary contains unverified _auth_token without genuine AcquiredProviderObservation instance"
            }

    # 2. Outer timestamp freshness validation
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
    if not conv_chain or not str(conv_chain).strip():
        return False, "MISSING_CONVERSION_CHAIN_IDENTITY", None, {
            "reason": "quote-asset USD conversion evidence missing chain identity"
        }
    norm_chain = str(conv_chain).strip().lower()
    if expected_chain:
        if norm_chain != str(expected_chain).strip().lower():
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
    if not conv_asset or not str(conv_asset).strip():
        return False, "MISSING_CONVERSION_ASSET_IDENTITY", None, {
            "reason": "quote-asset USD conversion evidence missing quote asset address"
        }
    conv_asset_str = str(conv_asset).strip()
    if not is_valid_address(effective_chain, conv_asset_str):
        return False, "INVALID_TOKEN_ADDRESS", None, {
            "reason": f"conversion quote asset address format invalid: {conv_asset_str}"
        }
    if expected_quote_asset:
        if not same_address(effective_chain, conv_asset_str, expected_quote_asset):
            return False, "QUOTE_CONVERSION_ASSET_MISMATCH", None, {
                "reason": f"conversion quote asset ({conv_asset_str}) does not match expected ({expected_quote_asset})",
                "actual": conv_asset_str,
                "expected": expected_quote_asset,
            }

    # 5. USD reference asset validation
    raw_usd_ref = (
        conversion.get("usd_reference_asset") or
        conversion.get("reference_asset") or
        conversion.get("usd_reference") or
        conversion.get("quote_reference") or
        conversion.get("target_currency")
    )
    if not raw_usd_ref or not str(raw_usd_ref).strip():
        return False, "MISSING_USD_REFERENCE_IDENTITY", None, {
            "reason": "quote-asset USD conversion evidence missing USD reference asset identity (e.g. 'USD')"
        }
    norm_usd_ref = str(raw_usd_ref).strip().upper()
    if norm_usd_ref not in {"USD", "USDC", "USDT", "USD_UNIT", "USD_REFERENCE"}:
        return False, "MISMATCHED_USD_REFERENCE", None, {
            "reason": f"unsupported or non-USD reference asset identity: '{raw_usd_ref}'"
        }

    # 6. Valuation source / provider recognition & cross-validation
    raw_source = conversion.get("source")
    raw_provider = conversion.get("provider")
    raw_oracle = conversion.get("oracle")

    if not raw_source and not raw_provider and not raw_oracle:
        return False, "UNVERIFIED_VALUATION_SOURCE", None, {
            "reason": "quote-asset USD conversion lacks explicit valuation source or provider"
        }

    resolved_providers: set[str] = set()
    for field_name, raw_val in (("source", raw_source), ("provider", raw_provider), ("oracle", raw_oracle)):
        if raw_val:
            val_norm = str(raw_val).strip().lower()
            canonical_key = PROVIDER_ALIAS_MAP.get(val_norm)
            if not canonical_key:
                return False, "UNVERIFIED_VALUATION_SOURCE", None, {
                    "reason": f"arbitrary or unrecognized valuation {field_name}: {raw_val!r}"
                }
            resolved_providers.add(canonical_key)

    if len(resolved_providers) > 1:
        return False, "CONFLICTING_PROVIDER_EVIDENCE", None, {
            "reason": f"conflicting provider evidence: source '{raw_source}' vs provider '{raw_provider}'"
        }
    canonical_provider = next(iter(resolved_providers))

    # 7. Valuation policy validation
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

    # Verify policy compatibility with provider
    allowed_policies = PROVIDER_ALLOWED_POLICIES.get(canonical_provider, set())
    if norm_policy not in allowed_policies:
        return False, "INCOMPATIBLE_VALUATION_POLICY", None, {
            "reason": f"valuation policy '{norm_policy}' is incompatible with provider '{canonical_provider}'"
        }

    # 8. Provider-specific feed identity binding
    prov_str: str = ""
    market_info: dict[str, Any] = {}
    pool_info: dict[str, Any] = {}
    pair_info: dict[str, Any] = {}

    if canonical_provider == "chainlink":
        feed_addr_raw = conversion.get("feed_address") or conversion.get("aggregator_address") or conversion.get("oracle_address")
        if not feed_addr_raw or not str(feed_addr_raw).strip():
            return False, "MISSING_CONVERSION_PROVENANCE", None, {
                "reason": "missing valuation provenance: Chainlink conversion requires 'feed_address'"
            }
        feed_addr = str(feed_addr_raw).strip()
        if not is_valid_address(effective_chain, feed_addr):
            return False, "INVALID_TOKEN_ADDRESS", None, {
                "reason": f"invalid oracle feed address format for chain {effective_chain}: {feed_addr}"
            }
        feed_key = feed_addr.lower()
        if feed_key not in TRUSTED_CHAINLINK_FEEDS:
            return False, "UNRECOGNIZED_FEED_ADDRESS", None, {
                "reason": f"Chainlink feed address '{feed_addr}' is not registered in trusted feed registry"
            }
        feed_info = TRUSTED_CHAINLINK_FEEDS[feed_key]
        if str(feed_info["chain"]).lower() != norm_chain:
            return False, "QUOTE_CONVERSION_CHAIN_MISMATCH", None, {
                "reason": f"Chainlink feed '{feed_addr}' is registered on chain '{feed_info['chain']}', not '{norm_chain}'"
            }
        if not same_address(effective_chain, feed_info["quote_asset"], conv_asset_str):
            return False, "QUOTE_CONVERSION_ASSET_MISMATCH", None, {
                "reason": f"Chainlink feed '{feed_addr}' is registered for quote asset '{feed_info['quote_asset']}' ({feed_info['symbol']}), not '{conv_asset_str}'"
            }
        if norm_policy not in feed_info.get("allowed_policies", set()):
            return False, "INCOMPATIBLE_VALUATION_POLICY", None, {
                "reason": f"Chainlink feed '{feed_addr}' does not support policy '{norm_policy}'"
            }
        prov_str = feed_addr

    elif canonical_provider == "pyth":
        feed_id_raw = conversion.get("feed_id") or conversion.get("price_feed_id")
        if not feed_id_raw or not str(feed_id_raw).strip():
            return False, "MISSING_CONVERSION_PROVENANCE", None, {
                "reason": "missing valuation provenance: Pyth conversion requires 'feed_id'"
            }
        feed_id = str(feed_id_raw).strip().lower()
        if not feed_id.startswith("0x"):
            feed_id = "0x" + feed_id
        if feed_id not in TRUSTED_PYTH_FEEDS:
            return False, "UNRECOGNIZED_FEED_ID", None, {
                "reason": f"Pyth feed ID '{feed_id}' is not registered in trusted feed registry"
            }
        feed_info = TRUSTED_PYTH_FEEDS[feed_id]
        if norm_chain not in feed_info["chains"]:
            return False, "QUOTE_CONVERSION_CHAIN_MISMATCH", None, {
                "reason": f"Pyth feed '{feed_id}' ({feed_info['symbol']}) does not support chain '{norm_chain}'"
            }
        expected_asset_for_chain = feed_info["quote_asset"].get(norm_chain)
        if not expected_asset_for_chain or not same_address(norm_chain, expected_asset_for_chain, conv_asset_str):
            return False, "QUOTE_CONVERSION_ASSET_MISMATCH", None, {
                "reason": f"Pyth feed '{feed_id}' represents asset '{expected_asset_for_chain}', not '{conv_asset_str}'"
            }
        if norm_policy not in feed_info.get("allowed_policies", set()):
            return False, "INCOMPATIBLE_VALUATION_POLICY", None, {
                "reason": f"Pyth feed '{feed_id}' does not support policy '{norm_policy}'"
            }
        prov_str = feed_id

    elif canonical_provider == "uniswap_v3_twap":
        pool_raw = conversion.get("pool_id") or conversion.get("pool_address") or conversion.get("pair_address")
        if not pool_raw or not str(pool_raw).strip():
            return False, "MISSING_CONVERSION_PROVENANCE", None, {
                "reason": "missing valuation provenance: Uniswap v3 TWAP conversion requires 'pool_id' or 'pool_address'"
            }
        pool_addr = str(pool_raw).strip()
        if not is_valid_address(effective_chain, pool_addr):
            return False, "INVALID_TOKEN_ADDRESS", None, {
                "reason": f"invalid Uniswap v3 pool address format: {pool_addr}"
            }
        pool_key = pool_addr.lower()
        if pool_key not in TRUSTED_UNISWAP_V3_POOLS:
            return False, "UNRECOGNIZED_POOL_IDENTITY", None, {
                "reason": f"Uniswap v3 TWAP pool '{pool_addr}' is not registered in trusted TWAP pool registry"
            }
        pool_info = TRUSTED_UNISWAP_V3_POOLS[pool_key]
        if str(pool_info["chain"]).lower() != norm_chain:
            return False, "QUOTE_CONVERSION_CHAIN_MISMATCH", None, {
                "reason": f"Uniswap v3 TWAP pool belongs to chain '{pool_info['chain']}', not '{norm_chain}'"
            }
        if not any(same_address(effective_chain, a, conv_asset_str) for a in pool_info["allowed_assets"]):
            return False, "QUOTE_CONVERSION_ASSET_MISMATCH", None, {
                "reason": f"quote asset '{conv_asset_str}' is not an asset in Uniswap v3 TWAP pool '{pool_addr}'"
            }
        prov_str = pool_addr

    elif canonical_provider in {"coinbase", "binance", "kraken"}:
        market_id = conversion.get("market_id") or conversion.get("pair_id") or conversion.get("pair") or conversion.get("symbol")
        source_url = conversion.get("source_url")
        if not market_id and not source_url:
            return False, "MISSING_CONVERSION_PROVENANCE", None, {
                "reason": f"missing valuation provenance: {canonical_provider} conversion requires 'market_id' or 'source_url'"
            }
        if source_url:
            domain = _extract_domain(source_url)
            if domain not in TRUSTED_CEX_DOMAINS.get(canonical_provider, set()):
                return False, "UNTRUSTED_SOURCE_URL", None, {
                    "reason": f"untrusted source URL domain for {canonical_provider}: '{source_url}'"
                }
        market_key = str(market_id).strip().upper() if market_id else None
        if not market_key and source_url:
            market_key = _extract_cex_market_from_url(canonical_provider, source_url)
        if not market_key or market_key not in TRUSTED_CEX_MARKETS.get(canonical_provider, {}):
            return False, "UNRECOGNIZED_MARKET_IDENTITY", None, {
                "reason": f"unrecognized market identifier '{market_key or market_id}' for {canonical_provider}"
            }
        market_info = TRUSTED_CEX_MARKETS[canonical_provider][market_key]
        if market_info.get("quote_currency") == "USDT":
            if norm_usd_ref == "USD":
                return False, "REFERENCE_CURRENCY_MISMATCH", None, {
                    "reason": f"market '{market_key}' quotes in USDT, not USD; cannot label USDT as USD without verified chained conversion",
                    "market": market_key,
                    "actual_quote_currency": "USDT",
                }
        allowed_asset_for_chain = market_info["allowed_assets"].get(norm_chain)
        if not allowed_asset_for_chain or not same_address(norm_chain, allowed_asset_for_chain, conv_asset_str):
            return False, "QUOTE_CONVERSION_ASSET_MISMATCH", None, {
                "reason": f"market '{market_key}' on {canonical_provider} represents asset '{market_info['symbol']}', not quote asset '{conv_asset_str}'"
            }
        prov_str = market_key

    elif canonical_provider in {"defillama", "geckoterminal", "dexscreener"}:
        feed_id = conversion.get("feed_id") or conversion.get("pair_id") or conversion.get("pool_id")
        source_url = conversion.get("source_url")
        if not feed_id and not source_url:
            return False, "MISSING_CONVERSION_PROVENANCE", None, {
                "reason": f"missing valuation provenance: {canonical_provider} conversion requires feed/pair/pool id or source_url"
            }
        if source_url:
            domain = _extract_domain(source_url)
            if domain not in TRUSTED_DEX_AGG_DOMAINS.get(canonical_provider, set()):
                return False, "UNTRUSTED_SOURCE_URL", None, {
                    "reason": f"untrusted source URL domain for {canonical_provider}: '{source_url}'"
                }
        pair_key = str(feed_id).strip() if feed_id else ""
        if not pair_key and source_url:
            for registered_pair in TRUSTED_DEX_AGG_PAIRS.get(canonical_provider, {}):
                if registered_pair in source_url:
                    pair_key = registered_pair
                    break
        if not pair_key or pair_key not in TRUSTED_DEX_AGG_PAIRS.get(canonical_provider, {}):
            return False, "UNRECOGNIZED_MARKET_IDENTITY", None, {
                "reason": f"unrecognized market/pair identifier '{pair_key or feed_id}' for {canonical_provider}"
            }
        pair_info = TRUSTED_DEX_AGG_PAIRS[canonical_provider][pair_key]
        if str(pair_info["chain"]).lower() != norm_chain:
            return False, "QUOTE_CONVERSION_CHAIN_MISMATCH", None, {
                "reason": f"pair '{pair_key}' is on chain '{pair_info['chain']}', not '{norm_chain}'"
            }
        if not same_address(norm_chain, pair_info["quote_asset"], conv_asset_str):
            return False, "QUOTE_CONVERSION_ASSET_MISMATCH", None, {
                "reason": f"pair '{pair_key}' represents quote asset '{pair_info['quote_asset']}', not '{conv_asset_str}'"
            }
        prov_str = pair_key

    else:
        return False, "UNVERIFIED_VALUATION_SOURCE", None, {
            "reason": f"provider '{canonical_provider}' cannot be verified with available evidence"
        }

    # 9. Provider-specific observation verification & independent timestamp freshness
    obs = conversion.get("observation") if isinstance(conversion.get("observation"), dict) else conversion

    if canonical_provider == "chainlink":
        ans_raw = obs.get("answer") if obs.get("answer") is not None else obs.get("latest_answer")
        dec_raw = obs.get("decimals")
        rnd_raw = obs.get("round_id") if obs.get("round_id") is not None else obs.get("answered_in_round")
        upd_raw = obs.get("updated_at") or obs.get("updated_at_utc") or obs.get("updatedAt")

        if ans_raw is None or dec_raw is None or rnd_raw is None or upd_raw is None:
            return False, "MISSING_PROVIDER_OBSERVATION_FIELDS", None, {
                "reason": "missing required Chainlink observation fields: answer, decimals, round_id, updated_at",
                "provider": "chainlink",
            }

        # Independent observation timestamp freshness validation
        ok_ts, ts_status, ts_age = validate_observation_timestamp_freshness(upd_raw, as_of_utc, max_conversion_age_seconds, "chainlink")
        if not ok_ts:
            return False, ts_status, None, {
                "reason": f"Chainlink observation timestamp freshness failed: {ts_status}",
                "age_seconds": ts_age,
            }

        try:
            ans_val = float(ans_raw)
            dec_val = int(dec_raw)
            rnd_val = int(rnd_raw)
        except (ValueError, TypeError):
            return False, "INVALID_PROVIDER_OBSERVATION", None, {
                "reason": f"Chainlink observation values must be numeric: answer={ans_raw!r}, decimals={dec_raw!r}, round_id={rnd_raw!r}"
            }

        expected_decimals = feed_info.get("decimals", 8)
        if dec_val != expected_decimals:
            return False, "OBSERVATION_DECIMALS_MISMATCH", None, {
                "reason": f"Chainlink USD reference feeds must have {expected_decimals} decimals, got {dec_val}"
            }
        if rnd_val <= 0:
            return False, "INVALID_PROVIDER_OBSERVATION", None, {
                "reason": f"Chainlink round_id must be a strictly positive integer, got {rnd_val}"
            }
        if ans_val <= 0:
            return False, "INVALID_PROVIDER_OBSERVATION", None, {
                "reason": f"Chainlink answer must be strictly positive, got {ans_val}"
            }

        ans_round_raw = obs.get("answered_in_round") or obs.get("answeredInRound")
        if ans_round_raw is not None:
            try:
                ans_round = int(ans_round_raw)
                if ans_round < rnd_val:
                    return False, "INVALID_PROVIDER_OBSERVATION", None, {
                        "reason": f"Chainlink round not complete: answered_in_round ({ans_round}) < round_id ({rnd_val})"
                    }
            except (ValueError, TypeError):
                pass

        feed_chain_id = feed_info.get("chain_id")
        obs_chain_id = obs.get("chain_id") or conversion.get("chain_id")
        if obs_chain_id is not None and feed_chain_id is not None:
            try:
                if int(obs_chain_id) != int(feed_chain_id):
                    return False, "QUOTE_CONVERSION_CHAIN_MISMATCH", None, {
                        "reason": f"Chainlink observation chain_id ({obs_chain_id}) does not match registered feed chain_id ({feed_chain_id})"
                    }
            except (ValueError, TypeError):
                pass

        obs_price = ans_val / (10 ** dec_val)
        if abs(obs_price - clean_price) > max(clean_price * 1e-4, 1e-6):
            return False, "OBSERVATION_PRICE_DISCREPANCY", None, {
                "reason": f"Chainlink observation price ({obs_price}) does not match claimed price ({clean_price})",
                "observation_price": obs_price,
                "claimed_price": clean_price,
            }

    elif canonical_provider == "pyth":
        p_raw = obs.get("price") if obs.get("price") is not None else obs.get("price_val")
        e_raw = obs.get("expo") if obs.get("expo") is not None else obs.get("exponent")
        pub_raw = obs.get("publish_time") or obs.get("publishTime")

        if p_raw is None or e_raw is None or pub_raw is None:
            return False, "MISSING_PROVIDER_OBSERVATION_FIELDS", None, {
                "reason": "missing required Pyth observation fields: price, expo, publish_time",
                "provider": "pyth",
            }

        # Independent observation timestamp freshness validation
        ok_ts, ts_status, ts_age = validate_observation_timestamp_freshness(pub_raw, as_of_utc, max_conversion_age_seconds, "pyth")
        if not ok_ts:
            return False, ts_status, None, {
                "reason": f"Pyth observation timestamp freshness failed: {ts_status}",
                "age_seconds": ts_age,
            }

        try:
            p_val = float(p_raw)
            e_val = int(e_raw)
        except (ValueError, TypeError):
            return False, "INVALID_PROVIDER_OBSERVATION", None, {
                "reason": f"Pyth observation values must be numeric: price={p_raw!r}, expo={e_raw!r}"
            }

        expected_expo = feed_info.get("expo", -8)
        if e_val != expected_expo:
            return False, "OBSERVATION_DECIMALS_MISMATCH", None, {
                "reason": f"Pyth observation expo ({e_val}) does not match registered feed expo ({expected_expo})",
                "observation_expo": e_val,
                "registered_expo": expected_expo,
            }

        obs_fid = obs.get("feed_id") or obs.get("id")
        if obs_fid:
            norm_ofid = str(obs_fid).strip().lower()
            if not norm_ofid.startswith("0x"):
                norm_ofid = "0x" + norm_ofid
            if norm_ofid != feed_id:
                return False, "OBSERVATION_MARKET_MISMATCH", None, {
                    "reason": f"Pyth observation feed_id '{obs_fid}' does not match registered feed_id '{feed_id}'"
                }

        obs_price = p_val * (10 ** e_val)
        if obs_price <= 0:
            return False, "INVALID_PROVIDER_OBSERVATION", None, {
                "reason": f"Pyth observation price must be strictly positive, got {obs_price}"
            }
        if abs(obs_price - clean_price) > max(clean_price * 1e-4, 1e-6):
            return False, "OBSERVATION_PRICE_DISCREPANCY", None, {
                "reason": f"Pyth observation price ({obs_price}) does not match claimed price ({clean_price})",
                "observation_price": obs_price,
                "claimed_price": clean_price,
            }

    elif canonical_provider == "uniswap_v3_twap":
        win_raw = (
            obs.get("twap_window_seconds") if obs.get("twap_window_seconds") is not None else
            (obs.get("twap_window") if obs.get("twap_window") is not None else obs.get("window_seconds"))
        )
        if win_raw is None:
            return False, "MISSING_PROVIDER_OBSERVATION_FIELDS", None, {
                "reason": "missing required Uniswap v3 TWAP observation field: twap_window_seconds",
                "provider": "uniswap_v3_twap",
            }
        try:
            win_sec = float(win_raw)
        except (ValueError, TypeError):
            return False, "INVALID_TWAP_WINDOW", None, {
                "reason": f"invalid Uniswap v3 twap_window_seconds: {win_raw!r}"
            }
        if win_sec < 60.0:
            return False, "INVALID_TWAP_WINDOW", None, {
                "reason": f"Uniswap v3 TWAP window ({win_sec}s) must be at least 60 seconds",
                "window_seconds": win_sec,
            }

        # twap_price is MANDATORY; missing twap_price must fail closed
        twap_p = obs.get("twap_price")
        if twap_p is None:
            return False, "MISSING_PROVIDER_OBSERVATION_FIELDS", None, {
                "reason": "missing required Uniswap v3 TWAP observation field: twap_price",
                "provider": "uniswap_v3_twap",
            }
        try:
            tp_val = float(twap_p)
        except (ValueError, TypeError):
            return False, "INVALID_TWAP_OBSERVATION", None, {
                "reason": f"invalid twap_price in Uniswap v3 observation: {twap_p!r}"
            }
        if tp_val <= 0:
            return False, "INVALID_TWAP_OBSERVATION", None, {
                "reason": f"Uniswap v3 twap_price must be positive, got {tp_val}"
            }
        if abs(tp_val - clean_price) > max(clean_price * 1e-4, 1e-6):
            return False, "OBSERVATION_PRICE_DISCREPANCY", None, {
                "reason": f"Uniswap v3 TWAP observation price ({tp_val}) does not match claimed price ({clean_price})"
            }

        twap_ts = obs.get("timestamp") or obs.get("observation_timestamp") or obs.get("block_timestamp") or obs.get("timestamp_utc") or conversion.get("timestamp_utc")
        ok_ts, ts_status, ts_age = validate_observation_timestamp_freshness(twap_ts, as_of_utc, max_conversion_age_seconds, "uniswap_v3_twap")
        if not ok_ts:
            return False, ts_status, None, {
                "reason": f"Uniswap v3 TWAP observation timestamp verification failed: {ts_status}",
                "age_seconds": ts_age,
            }

        # On-chain observation calculation check - tick_cumulatives is MANDATORY
        tick_cumulatives = obs.get("tick_cumulatives") or obs.get("tickCumulatives")
        if tick_cumulatives is None:
            return False, "MISSING_PROVIDER_OBSERVATION_FIELDS", None, {
                "reason": "missing required Uniswap v3 TWAP observation field: tick_cumulatives",
                "provider": "uniswap_v3_twap",
            }
        if not isinstance(tick_cumulatives, (list, tuple)) or len(tick_cumulatives) != 2:
            return False, "INVALID_TWAP_OBSERVATION", None, {
                "reason": f"Uniswap v3 tick_cumulatives must be a 2-element list [start_tick, end_tick], got {tick_cumulatives!r}"
            }
        try:
            start_tick = int(tick_cumulatives[0])
            end_tick = int(tick_cumulatives[1])
            avg_tick = (end_tick - start_tick) / win_sec
            calc_ratio = 1.0001 ** avg_tick
            diff_direct = abs(calc_ratio - tp_val) / tp_val if tp_val > 0 else 1.0
            diff_inv = abs((1.0 / calc_ratio) - tp_val) / tp_val if calc_ratio > 0 else 1.0
            if min(diff_direct, diff_inv) > 0.05:
                return False, "INVALID_TWAP_OBSERVATION", None, {
                    "reason": f"Uniswap v3 on-chain tick cumulatives calculate to {calc_ratio:.4f}, which deviates from claimed twap_price {tp_val:.4f}",
                    "calculated_from_ticks": calc_ratio,
                    "claimed_twap_price": tp_val,
                }
        except Exception as e:
            return False, "INVALID_TWAP_OBSERVATION", None, {
                "reason": f"failed to calculate TWAP from tick_cumulatives: {e}"
            }

    elif canonical_provider in {"coinbase", "binance", "kraken"}:
        sym_raw = obs.get("symbol") or obs.get("market") or obs.get("pair")
        lp_raw = obs.get("last_price") if obs.get("last_price") is not None else obs.get("price")
        bp_raw = obs.get("bid_price") if obs.get("bid_price") is not None else obs.get("bid")
        ap_raw = obs.get("ask_price") if obs.get("ask_price") is not None else obs.get("ask")
        vol_raw = (
            obs.get("volume_24h") if obs.get("volume_24h") is not None else
            (obs.get("volume") if obs.get("volume") is not None else obs.get("base_volume"))
        )
        if sym_raw is None or lp_raw is None or bp_raw is None or ap_raw is None or vol_raw is None:
            return False, "MISSING_PROVIDER_OBSERVATION_FIELDS", None, {
                "reason": f"missing required {canonical_provider} observation fields (symbol, last_price, bid_price, ask_price, volume_24h)",
                "provider": canonical_provider,
            }

        # CEX observation timestamp validation
        cex_ts = obs.get("timestamp") or obs.get("observation_timestamp") or obs.get("server_time") or obs.get("time") or obs.get("timestamp_utc")
        if cex_ts is None:
            return False, "MISSING_PROVIDER_OBSERVATION_TIMESTAMP", None, {
                "reason": f"missing required {canonical_provider} observation timestamp",
                "provider": canonical_provider,
            }
        ok_ts, ts_status, ts_age = validate_observation_timestamp_freshness(cex_ts, as_of_utc, max_conversion_age_seconds, canonical_provider)
        if not ok_ts:
            return False, ts_status, None, {
                "reason": f"{canonical_provider} observation timestamp freshness failed: {ts_status}",
                "age_seconds": ts_age,
            }

        norm_obs_sym = str(sym_raw).strip().upper().replace("/", "").replace("-", "")
        norm_mkt_sym = market_key.replace("/", "").replace("-", "")
        if norm_obs_sym != norm_mkt_sym:
            return False, "OBSERVATION_MARKET_MISMATCH", None, {
                "reason": f"{canonical_provider} observation symbol '{sym_raw}' does not match registered market '{market_key}'",
                "observation_symbol": str(sym_raw),
                "registered_market": market_key,
            }
        lp = _clean_strictly_positive(lp_raw)
        bp = _clean_strictly_positive(bp_raw)
        ap = _clean_strictly_positive(ap_raw)
        vol = _clean_strictly_positive(vol_raw)
        if lp is None or bp is None or ap is None or vol is None or vol <= 0:
            return False, "INVALID_CEX_OBSERVATION", None, {
                "reason": f"{canonical_provider} observation fields must be positive finite numbers, volume > 0",
                "last_price": lp_raw,
                "bid_price": bp_raw,
                "ask_price": ap_raw,
                "volume_24h": vol_raw,
            }

        # Spread constraint: bid_price <= last_price <= ask_price
        if bp > ap:
            return False, "INVALID_CEX_OBSERVATION", None, {
                "reason": f"crossed/inverted {canonical_provider} ticker: bid_price ({bp}) > ask_price ({ap})",
                "bid_price": bp,
                "ask_price": ap,
            }
        if lp < bp or lp > ap:
            return False, "INVALID_CEX_OBSERVATION", None, {
                "reason": f"{canonical_provider} last_price ({lp}) outside bid/ask spread [{bp}, {ap}]: requires bid_price <= last_price <= ask_price",
                "last_price": lp,
                "bid_price": bp,
                "ask_price": ap,
            }
        if abs(lp - clean_price) > max(clean_price * 1e-4, 1e-6):
            return False, "OBSERVATION_PRICE_DISCREPANCY", None, {
                "reason": f"{canonical_provider} observation last_price ({lp}) does not match claimed price ({clean_price})",
                "observation_price": lp,
                "claimed_price": clean_price,
            }

    elif canonical_provider in {"defillama", "geckoterminal", "dexscreener"}:
        p_raw = obs.get("price") if obs.get("price") is not None else obs.get("price_usd")
        tok_raw = obs.get("token") or obs.get("token_address") or obs.get("address")
        ch_raw = obs.get("chain")
        ts_raw = obs.get("timestamp") or obs.get("observation_timestamp") or obs.get("timestamp_utc")

        if p_raw is None or tok_raw is None or ch_raw is None or ts_raw is None:
            return False, "MISSING_PROVIDER_OBSERVATION_FIELDS", None, {
                "reason": f"missing required {canonical_provider} observation fields (price, token, chain, timestamp)",
                "provider": canonical_provider,
            }

        # Independent observation timestamp freshness validation
        ok_ts, ts_status, ts_age = validate_observation_timestamp_freshness(ts_raw, as_of_utc, max_conversion_age_seconds, canonical_provider)
        if not ok_ts:
            return False, ts_status, None, {
                "reason": f"{canonical_provider} observation timestamp freshness failed: {ts_status}",
                "age_seconds": ts_age,
            }

        p_clean = _clean_strictly_positive(p_raw)
        if p_clean is None:
            return False, "INVALID_DEX_AGGREGATOR_PAYLOAD", None, {
                "reason": f"DEX aggregator price must be positive and finite, got {p_raw!r}"
            }
        if abs(p_clean - clean_price) > max(clean_price * 1e-4, 1e-6):
            return False, "OBSERVATION_PRICE_DISCREPANCY", None, {
                "reason": f"{canonical_provider} payload price ({p_clean}) does not match claimed price ({clean_price})",
                "observation_price": p_clean,
                "claimed_price": clean_price,
            }
        if not same_address(norm_chain, str(tok_raw).strip(), conv_asset_str):
            return False, "QUOTE_CONVERSION_ASSET_MISMATCH", None, {
                "reason": f"{canonical_provider} payload token ({tok_raw}) does not match conversion quote asset ({conv_asset_str})"
            }
        if str(ch_raw).strip().lower() != norm_chain:
            return False, "QUOTE_CONVERSION_CHAIN_MISMATCH", None, {
                "reason": f"{canonical_provider} payload chain ({ch_raw}) does not match conversion chain ({norm_chain})"
            }

    # 10. Provider Acquisition Provenance & Response Integrity Validation
    req_url = conversion.get("request_url") or obs.get("request_url") or conversion.get("source_url")
    rpc_method = conversion.get("rpc_method") or obs.get("rpc_method")
    raw_payload = conversion.get("raw_payload") or obs.get("raw_payload")
    resp_sha = conversion.get("response_sha256") or obs.get("response_sha256")
    is_acquired = bool(conversion.get("provider_acquired") or obs.get("provider_acquired"))

    if req_url:
        u_str = str(req_url).strip().lower()
        if not (u_str.startswith("http://") or u_str.startswith("https://")):
            return False, "UNTRUSTED_SOURCE_URL", None, {
                "reason": f"request_url must start with http:// or https://: {req_url}"
            }
        trusted_domains = TRUSTED_CEX_DOMAINS.get(canonical_provider, set()) | TRUSTED_DEX_AGG_DOMAINS.get(canonical_provider, set())
        if canonical_provider == "chainlink":
            trusted_domains = {"chainlink.org", "rpc.chainlink.org", "ethereum.org", "arbitrum.io", "base.org", "publicnode.com", "1rpc.io", "ankr.com", "llamarpc.com"}
        elif canonical_provider == "pyth":
            trusted_domains = {"hermes.pyth.network", "pyth.network"}
        elif canonical_provider == "uniswap_v3_twap":
            trusted_domains = {"uniswap.org", "ethereum.org", "arbitrum.io", "base.org", "publicnode.com", "1rpc.io", "ankr.com", "llamarpc.com"}

        m_dom = re.search(r"https?://([^/:\?#]+)", u_str)
        if m_dom:
            host = m_dom.group(1).lower()
            matched_dom = any(host == td or host.endswith("." + td) for td in trusted_domains) if trusted_domains else True
            if not matched_dom:
                return False, "UNTRUSTED_SOURCE_URL", None, {
                    "reason": f"untrusted source URL domain '{host}' for provider '{canonical_provider}'"
                }

    if raw_payload is not None and resp_sha is not None:
        raw_bytes = raw_payload.encode("utf-8") if isinstance(raw_payload, str) else json.dumps(raw_payload, sort_keys=True).encode("utf-8")
        calc_sha = hashlib.sha256(raw_bytes).hexdigest()
        if str(resp_sha).strip().lower() != calc_sha.lower():
            return False, "FABRICATED_PROVIDER_RESPONSE", None, {
                "reason": f"response payload SHA-256 mismatch: claimed '{resp_sha}', calculated '{calc_sha}'"
            }

    if raw_payload is not None and isinstance(raw_payload, (str, dict)):
        try:
            raw_data = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
            if isinstance(raw_data, dict):
                raw_p = raw_data.get("price") or raw_data.get("last") or raw_data.get("last_price") or raw_data.get("answer")
                if raw_p is not None:
                    raw_p_float = float(raw_p)
                    if canonical_provider == "chainlink":
                        raw_p_float = raw_p_float / (10 ** 8)
                    if abs(raw_p_float - clean_price) > max(clean_price * 0.05, 1e-4):
                        return False, "FABRICATED_PROVIDER_RESPONSE", None, {
                            "reason": f"raw provider payload price ({raw_p_float}) contradicts claimed price ({clean_price})"
                        }
        except Exception:
            pass

    # 11. Chained USD Conversion & Multi-leg Path Resolution
    requires_chained = False
    intermediate_asset_symbol = None

    if canonical_provider == "binance" and market_info.get("quote_currency") == "USDT":
        requires_chained = True
        intermediate_asset_symbol = "USDT"
    elif norm_usd_ref == "USDT":
        requires_chained = True
        intermediate_asset_symbol = "USDT"
    elif canonical_provider == "uniswap_v3_twap":
        requires_chained = True
        intermediate_asset_symbol = pool_info.get("intermediate_quote", "USDC")
    elif canonical_provider in {"defillama", "geckoterminal", "dexscreener"}:
        requires_chained = True
        intermediate_asset_symbol = pair_info.get("intermediate_quote", "USDC")

    chained_leg = conversion.get("chained_conversion") or conversion.get("usd_conversion_leg")

    if requires_chained and (not chained_leg or not isinstance(chained_leg, dict)):
        if intermediate_asset_symbol == "USDT" or norm_usd_ref == "USDT":
            return False, "MISSING_CHAINED_USD_CONVERSION", None, {
                "reason": "USDT-denominated valuation requires independently validated chained USDT/USD conversion; never assume a $1.00 peg"
            }
        else:
            return False, "UNVERIFIED_USD_CONVERSION_PATH", None, {
                "reason": f"{canonical_provider} pair reference represents an on-chain token ratio, not direct USD; requires verified usd_conversion_leg to USD"
            }

    effective_price_usd = clean_price
    chained_details_dict = None

    if chained_leg:
        if not isinstance(chained_leg, dict):
            return False, "INVALID_CHAINED_USD_CONVERSION", None, {
                "reason": "chained conversion evidence must be a dictionary"
            }
        if _recursion_depth >= 3:
            return False, "CONVERSION_CHAIN_TOO_DEEP", None, {
                "reason": "quote conversion chain exceeded maximum allowable depth"
            }

        ch_stamp = chained_leg.get("timestamp_utc") or chained_leg.get("timestamp") or chained_leg.get("as_of_utc")
        ch_fresh, ch_fresh_status, ch_age = validate_timestamp_freshness(ch_stamp, as_of_utc, max_conversion_age_seconds)
        if not ch_fresh:
            status_code = "STALE_CHAINED_USD_CONVERSION" if ch_fresh_status in ("STALE_QUOTE", "STALE") else "INVALID_QUOTE_CONVERSION_TIMESTAMP"
            return False, status_code, None, {
                "reason": f"chained conversion freshness verification failed: {ch_fresh_status}",
                "age_seconds": ch_age,
            }

        ch_chain = str(chained_leg.get("chain") or norm_chain).strip().lower()

        # Intermediate asset address binding - check across ALL candidate address fields
        expected_intermediate_token = None
        if intermediate_asset_symbol == "USDT":
            known_usdt = {
                "ethereum": "0xdac17f958d2ee523a2206206994597c13d831ec7",
                "arbitrum": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
                "base": "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",
                "solana": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
            }
            expected_intermediate_token = known_usdt.get(ch_chain)
        elif intermediate_asset_symbol == "USDC":
            known_usdc = {
                "ethereum": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                "arbitrum": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
                "base": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                "solana": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            }
            expected_intermediate_token = known_usdc.get(ch_chain)

        intermediate_found = False
        for addr_key in ("quote_asset", "token_address", "contract_address", "asset_address", "quote_token", "base_token", "address"):
            if addr_key in chained_leg and chained_leg[addr_key] is not None:
                addr_val = str(chained_leg[addr_key]).strip()
                if addr_val:
                    intermediate_found = True
                    if expected_intermediate_token and not same_address(ch_chain, addr_val, expected_intermediate_token):
                        return False, "QUOTE_CONVERSION_ASSET_MISMATCH", None, {
                            "reason": f"chained conversion address field '{addr_key}' ({addr_val}) does not match expected intermediate asset {intermediate_asset_symbol} ({expected_intermediate_token}) on chain {ch_chain}"
                        }
        if not intermediate_found and expected_intermediate_token:
            return False, "QUOTE_CONVERSION_ASSET_MISMATCH", None, {
                "reason": f"chained conversion missing intermediate asset address for {intermediate_asset_symbol}"
            }

        ch_ok, ch_status, ch_rate, ch_details = validate_quote_usd_conversion(
            chained_leg,
            expected_chain=chained_leg.get("chain"),
            as_of_utc=as_of_utc,
            max_conversion_age_seconds=max_conversion_age_seconds,
            _recursion_depth=_recursion_depth + 1,
        )
        if not ch_ok:
            return False, ch_status, None, {
                "reason": f"chained USD conversion validation failed: {ch_details.get('reason')}",
                "chained_details": ch_details,
            }

        ch_target = str(ch_details.get("usd_reference_asset") or "").strip().upper()
        if ch_target not in {"USD", "USD_UNIT", "USD_REFERENCE"}:
            return False, "UNVERIFIED_USD_CONVERSION_PATH", None, {
                "reason": f"chained conversion must terminate in direct USD, got '{ch_target}'"
            }

        effective_price_usd = clean_price * ch_rate
        chained_details_dict = ch_details

    # Provenance verification: authenticated via genuine adapter HMAC signature
    actual_obs = conversion.get("__acquired_observation__") if isinstance(conversion, dict) else (
        conversion if isinstance(conversion, AcquiredProviderObservation) else None
    )

    provenance_verified = False
    transport_type_detected = "UNSPECIFIED"
    verif_status = "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE"

    if actual_obs is not None:
        if getattr(actual_obs, "is_caller_created", False):
            return False, "CALLER_CREATED_OBSERVATION_OBJECT", None, {
                "reason": "Caller-created AcquiredProviderObservation objects are rejected; must be acquired via genuine adapter"
            }

        # Check raw payload sha256
        if actual_obs.raw_payload:
            calc_sha = hashlib.sha256(actual_obs.raw_payload.encode("utf-8")).hexdigest()
            if str(actual_obs.response_sha256 or "").lower() != calc_sha.lower():
                return False, "PAYLOAD_HASH_MISMATCH", None, {
                    "reason": f"observation raw_payload hash ({calc_sha}) does not match response_sha256 ({actual_obs.response_sha256})"
                }

        # Verify HMAC signature over full canonical payload
        if not _verify_provenance_token(actual_obs):
            return False, "INVALID_PROVENANCE_SIGNATURE", None, {
                "reason": "observation HMAC signature verification failed; observation data was tampered or mutated"
            }

        # Check for mutation between conversion dict and authenticated actual_obs
        if clean_price != actual_obs.price_usd:
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict price ({clean_price}) does not match authenticated observation ({actual_obs.price_usd})"
            }
        if norm_chain != actual_obs.chain:
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict chain ({norm_chain}) does not match authenticated observation ({actual_obs.chain})"
            }
        if not same_address(norm_chain, conv_asset_str, actual_obs.quote_asset) and conv_asset_str.lower() != actual_obs.quote_asset.lower():
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict quote_asset ({conv_asset_str}) does not match authenticated observation ({actual_obs.quote_asset})"
            }
        if prov_str and actual_obs.provider_identifier and not same_address(norm_chain, prov_str, actual_obs.provider_identifier) and prov_str.lower() != actual_obs.provider_identifier.lower():
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict identifier ({prov_str}) does not match authenticated observation ({actual_obs.provider_identifier})"
            }
        if isinstance(conversion, dict) and "observation" in conversion and isinstance(conversion["observation"], dict) and actual_obs.observation:
            if conversion["observation"] != actual_obs.observation:
                return False, "OBSERVATION_MUTATION_DETECTED", None, {
                    "reason": "conversion dict observation payload does not match authenticated observation"
                }
        if isinstance(conversion, dict) and "response_sha256" in conversion and str(conversion["response_sha256"]).lower() != str(actual_obs.response_sha256 or "").lower():
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": "conversion dict response_sha256 does not match authenticated observation"
            }
        if isinstance(conversion, dict) and "raw_payload" in conversion and str(conversion["raw_payload"]) != actual_obs.raw_payload:
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": "conversion dict raw_payload does not match authenticated observation"
            }
        req_url_candidate = conversion.get("request_url") or conversion.get("source_url")
        if req_url_candidate and str(req_url_candidate).strip() != actual_obs.request_url:
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict request_url ({req_url_candidate}) does not match authenticated observation ({actual_obs.request_url})"
            }
        if "rpc_method" in conversion and conversion.get("rpc_method") != actual_obs.rpc_method:
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict rpc_method ({conversion.get('rpc_method')}) does not match authenticated observation ({actual_obs.rpc_method})"
            }
        if "transport_type" in conversion and str(conversion.get("transport_type")).strip() != actual_obs.transport_type:
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict transport_type ({conversion.get('transport_type')}) does not match authenticated observation ({actual_obs.transport_type})"
            }
        cand_policy = conversion.get("valuation_policy") or conversion.get("policy")
        if cand_policy and str(cand_policy).strip().upper() != actual_obs.valuation_policy.upper():
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict valuation_policy ({cand_policy}) does not match authenticated observation ({actual_obs.valuation_policy})"
            }
        cand_ref = conversion.get("usd_reference_asset") or conversion.get("reference_asset") or conversion.get("usd_reference")
        if cand_ref and str(cand_ref).strip().upper() != actual_obs.reference_asset.upper():
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict reference_asset ({cand_ref}) does not match authenticated observation ({actual_obs.reference_asset})"
            }
        if "acquisition_method" in conversion and str(conversion.get("acquisition_method")).strip() != actual_obs.acquisition_method:
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": f"conversion dict acquisition_method ({conversion.get('acquisition_method')}) does not match authenticated observation ({actual_obs.acquisition_method})"
            }
        if "cryptographic_signature_verified" in conversion and bool(conversion.get("cryptographic_signature_verified")) != bool(actual_obs.cryptographic_signature_verified):
            return False, "OBSERVATION_MUTATION_DETECTED", None, {
                "reason": "conversion dict cryptographic_signature_verified does not match authenticated observation"
            }

        transport_type_detected = actual_obs.transport_type
        provenance_verified = True
        if actual_obs.transport_type == "MOCKED_TRANSPORT":
            verif_status = "TEST_MOCKED_OBSERVATION_VALIDATED"
        else:
            verif_status = "PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED"
    else:
        if isinstance(conversion, dict) and conversion.get("_auth_token"):
            return False, "INVALID_PROVENANCE_SIGNATURE", None, {
                "reason": "dictionary contains unverified _auth_token without genuine AcquiredProviderObservation instance"
            }

    # If chained conversion, verify intermediate leg was also acquired and validated
    if provenance_verified and requires_chained:
        if chained_details_dict and not chained_details_dict.get("provider_acquired", False):
            provenance_verified = False
            verif_status = "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE"
        elif chained_details_dict and chained_details_dict.get("transport_type") == "MOCKED_TRANSPORT":
            verif_status = "TEST_MOCKED_OBSERVATION_VALIDATED"
            transport_type_detected = "MOCKED_TRANSPORT"

    verif_method = (
        f"AUTHENTICATED_{canonical_provider.upper()}_ACQUISITION_VERIFIED"
        if provenance_verified else
        f"CALLER_SUPPLIED_{canonical_provider.upper()}_STRUCTURAL_CONSISTENCY_ONLY"
    )

    return True, verif_status, effective_price_usd, {
        "price_usd": effective_price_usd,
        "primary_price": clean_price,
        "chained_conversion": chained_details_dict,
        "chain": conv_chain,
        "quote_asset": conv_asset_str,
        "usd_reference_asset": norm_usd_ref,
        "source": str(raw_source or raw_provider or raw_oracle).strip(),
        "provider": canonical_provider,
        "valuation_policy": norm_policy,
        "provenance_id": prov_str,
        "age_seconds": round(age, 1) if age is not None else None,
        "verification_method": verif_method,
        "provider_acquired": provenance_verified,
        "transport_type": transport_type_detected,
        "conversion_status": verif_status,
        "cryptographic_authenticity_verified": getattr(actual_obs, "cryptographic_signature_verified", False) if actual_obs else False,
        "__acquired_observation__": actual_obs,
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

    if not expected_token_address and isinstance(source, dict):
        expected_token_address = source.get("contract_address") or source.get("token_address")

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
    conv_details = None
    if conv_data:
        ok_conv, conv_status, conv_price, conv_details = validate_quote_usd_conversion(
            conv_data,
            expected_chain=target_chain,
            expected_quote_asset=target_quote_asset,
            as_of_utc=as_of_utc,
            max_conversion_age_seconds=max_conversion_age_seconds,
        )
        if not ok_conv or conv_status not in {"PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "TEST_MOCKED_OBSERVATION_VALIDATED", "CONVERSION_VERIFIED"}:
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
                    "quote_conversion": conv_details,
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
                "quote_conversion": conv_details,
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
                    "INCOMPATIBLE_VALUATION_POLICY",
                    "MISSING_CONVERSION_PROVENANCE",
                    "MISSING_USD_REFERENCE_IDENTITY",
                    "MISMATCHED_USD_REFERENCE",
                    "REFERENCE_CURRENCY_MISMATCH",
                    "CANNOT_ASSUME_STABLECOIN_PEG",
                    "MISSING_CHAINED_USD_CONVERSION",
                    "STALE_CHAINED_USD_CONVERSION",
                    "UNVERIFIED_USD_CONVERSION_PATH",
                    "UNRECOGNIZED_FEED_ADDRESS",
                    "UNRECOGNIZED_FEED_ID",
                    "UNRECOGNIZED_POOL_IDENTITY",
                    "UNRECOGNIZED_MARKET_IDENTITY",
                    "UNTRUSTED_SOURCE_URL",
                    "MISSING_PROVIDER_OBSERVATION_FIELDS",
                    "MISSING_PROVIDER_OBSERVATION_TIMESTAMP",
                    "INVALID_PROVIDER_OBSERVATION_TIMESTAMP",
                    "STALE_PROVIDER_OBSERVATION",
                    "FUTURE_PROVIDER_OBSERVATION",
                    "OBSERVATION_PRICE_DISCREPANCY",
                    "OBSERVATION_DECIMALS_MISMATCH",
                    "INVALID_PROVIDER_OBSERVATION",
                    "INVALID_TWAP_WINDOW",
                    "INVALID_TWAP_OBSERVATION",
                    "INVALID_CEX_OBSERVATION",
                    "OBSERVATION_MARKET_MISMATCH",
                    "INVALID_DEX_AGGREGATOR_PAYLOAD",
                    "CONFLICTING_PROVIDER_EVIDENCE",
                    "FABRICATED_PROVIDER_RESPONSE",
                    "OBSERVATION_MUTATION_DETECTED",
                    "PAYLOAD_HASH_MISMATCH",
                    "CALLER_CREATED_OBSERVATION_OBJECT",
                    "INVALID_PROVENANCE_SIGNATURE",
                    "TEST_MOCKED_OBSERVATION_VALIDATED",
                    "UNVERIFIED_PROVIDER_OBSERVATION",
                    "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE",
                    "PROVIDER_DATA_UNAVAILABLE",
                    "PROVIDER_RESPONSE_INVALID",
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
                    "INCOMPATIBLE_VALUATION_POLICY",
                    "MISSING_CONVERSION_PROVENANCE",
                    "MISSING_USD_REFERENCE_IDENTITY",
                    "MISMATCHED_USD_REFERENCE",
                    "REFERENCE_CURRENCY_MISMATCH",
                    "CANNOT_ASSUME_STABLECOIN_PEG",
                    "MISSING_CHAINED_USD_CONVERSION",
                    "STALE_CHAINED_USD_CONVERSION",
                    "UNVERIFIED_USD_CONVERSION_PATH",
                    "UNRECOGNIZED_FEED_ADDRESS",
                    "UNRECOGNIZED_FEED_ID",
                    "UNRECOGNIZED_POOL_IDENTITY",
                    "UNRECOGNIZED_MARKET_IDENTITY",
                    "UNTRUSTED_SOURCE_URL",
                    "MISSING_PROVIDER_OBSERVATION_FIELDS",
                    "MISSING_PROVIDER_OBSERVATION_TIMESTAMP",
                    "INVALID_PROVIDER_OBSERVATION_TIMESTAMP",
                    "STALE_PROVIDER_OBSERVATION",
                    "FUTURE_PROVIDER_OBSERVATION",
                    "OBSERVATION_PRICE_DISCREPANCY",
                    "OBSERVATION_DECIMALS_MISMATCH",
                    "INVALID_PROVIDER_OBSERVATION",
                    "INVALID_TWAP_WINDOW",
                    "INVALID_TWAP_OBSERVATION",
                    "INVALID_CEX_OBSERVATION",
                    "OBSERVATION_MARKET_MISMATCH",
                    "INVALID_DEX_AGGREGATOR_PAYLOAD",
                    "CONFLICTING_PROVIDER_EVIDENCE",
                    "FABRICATED_PROVIDER_RESPONSE",
                    "OBSERVATION_MUTATION_DETECTED",
                    "PAYLOAD_HASH_MISMATCH",
                    "CALLER_CREATED_OBSERVATION_OBJECT",
                    "INVALID_PROVENANCE_SIGNATURE",
                    "TEST_MOCKED_OBSERVATION_VALIDATED",
                    "UNVERIFIED_PROVIDER_OBSERVATION",
                    "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE",
                    "PROVIDER_DATA_UNAVAILABLE",
                    "PROVIDER_RESPONSE_INVALID",
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
        "quote_conversion": conv_details,
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
