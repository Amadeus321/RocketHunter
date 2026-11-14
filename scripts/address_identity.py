"""Chain-aware address comparison for EVM and case-sensitive address formats."""
from __future__ import annotations

import re
from typing import Any

EVM_CHAINS = {
    "ethereum", "eth", "base", "bsc", "bnb", "polygon", "polygon_pos",
    "arbitrum", "optimism", "avalanche", "avax", "fantom", "ftm",
    "linea", "scroll", "zksync", "mantle", "blast", "mode", "opbnb",
}


def address_key(chain: Any, address: Any) -> str:
    value = str(address or "").strip()
    chain_key = str(chain or "").strip().lower()
    if chain_key in EVM_CHAINS and re.fullmatch(r"0x[0-9a-fA-F]+", value):
        return value.lower()
    return value


def same_address(chain: Any, left: Any, right: Any) -> bool:
    left_key = address_key(chain, left)
    right_key = address_key(chain, right)
    return bool(left_key and right_key and left_key == right_key)


def normalize_chain(chain: Any) -> str:
    return str(chain or "").strip().lower()


def is_evm_chain(chain: Any) -> bool:
    return normalize_chain(chain) in EVM_CHAINS


def build_canonical_dex_identity(chain: Any, token_contract_or_mint: Any, pool_address: Any) -> str | None:
    """Build canonical DEX identity: dex:<chain>:<token_address>:<pool_address>.

    Rejects missing chain, token, or pool address.
    Normalizes EVM hex addresses to lowercase while strictly preserving case for Solana/non-EVM.
    """
    chain_key = normalize_chain(chain)
    token_str = str(token_contract_or_mint or "").strip()
    pool_str = str(pool_address or "").strip()
    if not chain_key or not token_str or not pool_str:
        return None
    token_key = address_key(chain_key, token_str)
    pool_key = address_key(chain_key, pool_str)
    if not token_key or not pool_key:
        return None
    return f"dex:{chain_key}:{token_key}:{pool_key}"


CEX_MARKET_TYPES = {"cex_spot", "cex_perp", "spot", "perpetual"}
DEX_MARKET_TYPES = {"dex_pool", "dex", "amm", "clmm", "pool", "dex_token"}
KNOWN_CEX_VENUES = {
    "binance", "coinbase", "kraken", "kucoin", "okx", "bybit",
    "gateio", "mexc", "bitget", "hyperliquid", "bitfinex", "huobi", "htx"
}


def build_canonical_cex_identity(exchange_or_venue: Any, market_type: Any, trading_pair: Any) -> str | None:
    """Build canonical CEX identity: cex:<exchange>:<market_type>:<trading_pair>.

    Distinct from DEX identity structure to prevent combining CEX and DEX observations.
    """
    ex_key = str(exchange_or_venue or "").strip().lower()
    mtype_key = str(market_type or "spot").strip().lower()
    pair_str = str(trading_pair or "").strip().upper().replace("/", "-")
    if not ex_key or not pair_str:
        return None
    return f"cex:{ex_key}:{mtype_key}:{pair_str}"


def validate_and_build_identity(record: dict[str, Any]) -> dict[str, Any]:
    """Validate market identity completeness and return canonical identity or rejection diagnostic.

    Enforces mandatory pool address for all DEX observations, distinguishes CEX from DEX
    using structural and explicit discriminators, and flags ambiguous or conflicting representations.
    A DEX record missing a pool address is never converted into a CEX market.
    """
    if not isinstance(record, dict):
        return {
            "is_valid": False,
            "market_type": "unknown",
            "canonical_identity": None,
            "rejection_reason": "NOT_A_DICTIONARY",
        }

    market_type = str(record.get("market_type") or record.get("type") or "").strip().lower()
    identity_str = str(record.get("identity") or record.get("market_key") or record.get("exact_identity") or "").strip()
    norm_rec = record.get("normalized_record") if isinstance(record.get("normalized_record"), dict) else {}

    # Decompose canonical string if present
    dex_parts = identity_str.split(":") if identity_str.lower().startswith("dex:") else []
    cex_parts = identity_str.split(":") if identity_str.lower().startswith("cex:") else []

    # Check structural DEX indicators
    chain_val = record.get("chain") or norm_rec.get("chain") or (dex_parts[1].strip() if len(dex_parts) >= 2 else None)
    token_val = (
        record.get("contract_address") or record.get("token_mint") or record.get("address")
        or norm_rec.get("token_contract") or (dex_parts[2].strip() if len(dex_parts) >= 3 else None)
    )
    pool_val = (
        record.get("pool_address") or record.get("pair_address") or record.get("gecko_pool_id")
        or norm_rec.get("pool_address") or (dex_parts[3].strip() if len(dex_parts) >= 4 else None)
    )

    is_explicit_dex = (
        market_type in DEX_MARKET_TYPES
        or bool(dex_parts)
        or bool(chain_val)
        or bool(token_val)
        or bool(pool_val)
    )

    # Check structural CEX indicators
    venue_val = record.get("venue") or record.get("exchange") or (cex_parts[1].strip() if len(cex_parts) >= 2 else None)
    venue_str = str(venue_val or "").strip().lower()
    pair_val = record.get("venue_pair") or record.get("pair") or record.get("trading_pair") or (cex_parts[3].strip() if len(cex_parts) >= 4 else None)
    if not market_type and len(cex_parts) >= 3:
        market_type = cex_parts[2].strip().lower()

    is_explicit_cex = (
        market_type in CEX_MARKET_TYPES
        or bool(cex_parts)
        or (venue_str in KNOWN_CEX_VENUES and not is_explicit_dex)
    )

    # Reject contradictory market type indicators
    if is_explicit_dex and market_type in CEX_MARKET_TYPES:
        return {
            "is_valid": False,
            "market_type": "ambiguous",
            "canonical_identity": None,
            "rejection_reason": "AMBIGUOUS_MARKET_TYPE",
        }

    # 1. DEX Market Evaluation
    if is_explicit_dex:
        if not chain_val or not str(chain_val).strip():
            if identity_str.lower().startswith("dex:"):
                parts = identity_str.split(":")
                if len(parts) >= 2 and parts[1].strip():
                    chain_val = parts[1].strip()
            if not chain_val or not str(chain_val).strip():
                return {
                    "is_valid": False,
                    "market_type": "dex",
                    "canonical_identity": None,
                    "rejection_reason": "MISSING_CHAIN",
                }
        norm_chain = normalize_chain(chain_val)

        token_top = record.get("contract_address") or record.get("token_mint") or record.get("address")
        token_norm = norm_rec.get("token_contract")
        if token_top and token_norm and not same_address(norm_chain, token_top, token_norm):
            return {
                "is_valid": False,
                "market_type": "dex",
                "canonical_identity": None,
                "rejection_reason": "CONFLICTING_TOKEN_IDENTITY",
                "token_top": str(token_top),
                "token_norm": str(token_norm),
            }
        token = token_top or token_norm or token_val
        if not token or not str(token).strip():
            return {
                "is_valid": False,
                "market_type": "dex",
                "canonical_identity": None,
                "rejection_reason": "MISSING_TOKEN",
            }

        pool_top = record.get("pool_address") or record.get("pair_address") or record.get("gecko_pool_id")
        pool_norm = norm_rec.get("pool_address")
        if pool_top and pool_norm and not same_address(norm_chain, pool_top, pool_norm):
            return {
                "is_valid": False,
                "market_type": "dex",
                "canonical_identity": None,
                "rejection_reason": "CONFLICTING_POOL_IDENTITY",
                "pool_top": str(pool_top),
                "pool_norm": str(pool_norm),
            }
        pool = pool_top or pool_norm or pool_val
        if not pool or not str(pool).strip():
            # A missing DEX pool address MUST be rejected as MISSING_POOL (never CEX)
            return {
                "is_valid": False,
                "market_type": "dex",
                "canonical_identity": None,
                "rejection_reason": "MISSING_POOL",
            }

        dex_id = build_canonical_dex_identity(norm_chain, token, pool)
        if not dex_id:
            return {
                "is_valid": False,
                "market_type": "dex",
                "canonical_identity": None,
                "rejection_reason": "INVALID_DEX_IDENTITY",
            }

        return {
            "is_valid": True,
            "market_type": "dex",
            "canonical_identity": dex_id,
            "chain": norm_chain,
            "token": address_key(norm_chain, token),
            "pool": address_key(norm_chain, pool),
        }

    # 2. CEX Market Evaluation
    if is_explicit_cex:
        if not venue_val or not str(venue_val).strip() or not pair_val or not str(pair_val).strip():
            return {
                "is_valid": False,
                "market_type": "cex",
                "canonical_identity": None,
                "rejection_reason": "MISSING_CEX_FIELDS",
            }
        cex_id = build_canonical_cex_identity(venue_val, market_type or "cex_spot", pair_val)
        return {
            "is_valid": True,
            "market_type": "cex",
            "canonical_identity": cex_id,
            "exchange": str(venue_val).strip().lower(),
            "pair": str(pair_val).strip().upper().replace("/", "-"),
            "market_sub_type": market_type or "cex_spot",
        }

    # 3. Ambiguous Market Type
    return {
        "is_valid": False,
        "market_type": "ambiguous",
        "canonical_identity": None,
        "rejection_reason": "AMBIGUOUS_MARKET_TYPE",
    }
