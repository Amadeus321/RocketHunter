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

    Enforces mandatory pool address for all DEX observations, distinguishes CEX from DEX,
    and flags conflicting token or pool representations.
    """
    if not isinstance(record, dict):
        return {
            "is_valid": False,
            "market_type": "unknown",
            "canonical_identity": None,
            "rejection_reason": "NOT_A_DICTIONARY",
        }

    market_type = str(record.get("market_type") or record.get("type") or "").strip().lower()
    venue = record.get("venue") or record.get("exchange")
    pair = record.get("venue_pair") or record.get("pair") or record.get("trading_pair")
    pool_top = record.get("pool_address") or record.get("pair_address") or record.get("gecko_pool_id")

    # 1. CEX Market Classification
    is_cex = (market_type in {"cex_spot", "cex_perp", "spot", "perpetual"} or bool(venue)) and not bool(pool_top)
    if is_cex:
        if not venue or not pair:
            return {
                "is_valid": False,
                "market_type": "cex",
                "canonical_identity": None,
                "rejection_reason": "MISSING_CEX_FIELDS",
            }
        cex_id = build_canonical_cex_identity(venue, market_type or "cex_spot", pair)
        return {
            "is_valid": True,
            "market_type": "cex",
            "canonical_identity": cex_id,
            "exchange": str(venue).strip().lower(),
            "pair": str(pair).strip().upper().replace("/", "-"),
            "market_sub_type": market_type or "cex_spot",
        }

    # 2. DEX Market Classification
    chain = record.get("chain") or (
        record.get("normalized_record", {}) if isinstance(record.get("normalized_record"), dict) else {}
    ).get("chain")
    if not chain and str(record.get("identity") or "").startswith("dex:"):
        parts = str(record["identity"]).split(":")
        if len(parts) >= 2:
            chain = parts[1]

    if not chain or not str(chain).strip():
        return {
            "is_valid": False,
            "market_type": "dex",
            "canonical_identity": None,
            "rejection_reason": "MISSING_CHAIN",
        }
    norm_chain = normalize_chain(chain)

    token_top = record.get("contract_address") or record.get("token_mint") or record.get("address")
    token_norm = (
        record.get("normalized_record", {}) if isinstance(record.get("normalized_record"), dict) else {}
    ).get("token_contract")
    if token_top and token_norm and not same_address(norm_chain, token_top, token_norm):
        return {
            "is_valid": False,
            "market_type": "dex",
            "canonical_identity": None,
            "rejection_reason": "CONFLICTING_TOKEN_IDENTITY",
            "token_top": str(token_top),
            "token_norm": str(token_norm),
        }
    token = token_top or token_norm
    if not token or not str(token).strip():
        return {
            "is_valid": False,
            "market_type": "dex",
            "canonical_identity": None,
            "rejection_reason": "MISSING_TOKEN",
        }

    pool_norm = (
        record.get("normalized_record", {}) if isinstance(record.get("normalized_record"), dict) else {}
    ).get("pool_address")
    if pool_top and pool_norm and not same_address(norm_chain, pool_top, pool_norm):
        return {
            "is_valid": False,
            "market_type": "dex",
            "canonical_identity": None,
            "rejection_reason": "CONFLICTING_POOL_IDENTITY",
            "pool_top": str(pool_top),
            "pool_norm": str(pool_norm),
        }
    pool = pool_top or pool_norm
    if not pool or not str(pool).strip():
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
