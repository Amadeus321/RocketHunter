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

    Enforces:
    - Independent parsing and validation of every supplied identity representation
      ('identity', 'market_key', 'exact_identity').
    - Strict rejection of legacy pipe-separated identity strings (LEGACY_IDENTITY_FORMAT).
    - Detection of conflicts between multiple supplied canonical strings (CONFLICTING_CANONICAL_STRINGS).
    - Cross-validation between top-level fields, normalized_record fields, and supplied canonical strings
      (CONFLICTING_CHAIN_IDENTITY, CONFLICTING_TOKEN_IDENTITY, CONFLICTING_POOL_IDENTITY).
    - Mandatory pool address for all DEX observations; a missing pool is never converted to CEX.
    - Full EVM hex case-normalization (0xabc) and case-preservation for non-EVM chains (Solana mints/pools).
    """
    if not isinstance(record, dict):
        return {
            "is_valid": False,
            "market_type": "unknown",
            "canonical_identity": None,
            "rejection_reason": "NOT_A_DICTIONARY",
        }

    # 1. Collect all supplied identity strings
    supplied_raw: dict[str, str] = {}
    for key in ("identity", "market_key", "exact_identity"):
        val = record.get(key)
        if val is not None and str(val).strip():
            supplied_raw[key] = str(val).strip()

    # 2. Reject legacy pipe-separated identities immediately
    for key, raw_str in supplied_raw.items():
        if "|" in raw_str or raw_str.lower().startswith("dex|") or raw_str.lower().startswith("cex|"):
            return {
                "is_valid": False,
                "market_type": "legacy",
                "canonical_identity": None,
                "rejection_reason": "LEGACY_IDENTITY_FORMAT",
                "field": key,
                "value": raw_str,
            }

    # 3. Independently parse and normalize each supplied canonical identity string
    parsed_supplied: dict[str, dict[str, Any]] = {}
    for key, raw_str in supplied_raw.items():
        if raw_str.lower().startswith("dex:"):
            parts = raw_str.split(":")
            if len(parts) not in (3, 4):
                return {
                    "is_valid": False,
                    "market_type": "dex",
                    "canonical_identity": None,
                    "rejection_reason": "INVALID_CANONICAL_IDENTITY_FORMAT",
                    "field": key,
                }
            norm_ch = normalize_chain(parts[1])
            if len(parts) == 4:
                norm_tok = address_key(norm_ch, parts[2])
                norm_pl = address_key(norm_ch, parts[3])
                canon_str = f"dex:{norm_ch}:{norm_tok}:{norm_pl}"
                parsed_supplied[key] = {
                    "market_type": "dex",
                    "canonical_identity": canon_str,
                    "chain": norm_ch,
                    "token": norm_tok,
                    "pool": norm_pl,
                    "is_full": True,
                }
            else:
                parsed_supplied[key] = {
                    "market_type": "dex",
                    "canonical_identity": None,
                    "chain": norm_ch,
                    "token": None,
                    "pool": None,
                    "is_full": False,
                }
        elif raw_str.lower().startswith("cex:"):
            parts = raw_str.split(":")
            if len(parts) != 4:
                return {
                    "is_valid": False,
                    "market_type": "cex",
                    "canonical_identity": None,
                    "rejection_reason": "INVALID_CANONICAL_IDENTITY_FORMAT",
                    "field": key,
                }
            ex_key = parts[1].strip().lower()
            mtype_key = parts[2].strip().lower()
            pair_key = parts[3].strip().upper().replace("/", "-")
            canon_str = f"cex:{ex_key}:{mtype_key}:{pair_key}"
            parsed_supplied[key] = {
                "market_type": "cex",
                "canonical_identity": canon_str,
                "exchange": ex_key,
                "market_sub_type": mtype_key,
                "pair": pair_key,
                "is_full": True,
            }
        else:
            return {
                "is_valid": False,
                "market_type": "unknown",
                "canonical_identity": None,
                "rejection_reason": "INVALID_CANONICAL_IDENTITY_FORMAT",
                "field": key,
            }

    # 4. Check for conflicts between multiple supplied canonical strings
    if len(parsed_supplied) > 1:
        chains = {info["chain"] for info in parsed_supplied.values() if info.get("chain")}
        if len(chains) > 1:
            return {
                "is_valid": False,
                "market_type": "ambiguous",
                "canonical_identity": None,
                "rejection_reason": "CONFLICTING_CANONICAL_STRINGS",
                "supplied": {k: v.get("canonical_identity") or v.get("chain") for k, v in parsed_supplied.items()},
            }
        tokens = {info["token"] for info in parsed_supplied.values() if info.get("token")}
        if len(tokens) > 1:
            return {
                "is_valid": False,
                "market_type": "ambiguous",
                "canonical_identity": None,
                "rejection_reason": "CONFLICTING_CANONICAL_STRINGS",
                "supplied": {k: v.get("canonical_identity") or v.get("token") for k, v in parsed_supplied.items()},
            }
        pools = {info["pool"] for info in parsed_supplied.values() if info.get("pool")}
        if len(pools) > 1:
            return {
                "is_valid": False,
                "market_type": "ambiguous",
                "canonical_identity": None,
                "rejection_reason": "CONFLICTING_CANONICAL_STRINGS",
                "supplied": {k: v.get("canonical_identity") or v.get("pool") for k, v in parsed_supplied.items()},
            }
        full_canons = {info["canonical_identity"] for info in parsed_supplied.values() if info.get("canonical_identity")}
        if len(full_canons) > 1:
            return {
                "is_valid": False,
                "market_type": "ambiguous",
                "canonical_identity": None,
                "rejection_reason": "CONFLICTING_CANONICAL_STRINGS",
                "supplied": {k: v["canonical_identity"] for k, v in parsed_supplied.items()},
            }

    # 5. Extract structural fields from top-level and normalized_record
    market_type = str(record.get("market_type") or record.get("type") or "").strip().lower()
    norm_rec = record.get("normalized_record") if isinstance(record.get("normalized_record"), dict) else {}

    top_chain = record.get("chain")
    norm_chain = norm_rec.get("chain")
    top_token = record.get("contract_address") or record.get("token_mint") or record.get("address")
    norm_token = norm_rec.get("token_contract")
    top_pool = record.get("pool_address") or record.get("pair_address") or record.get("gecko_pool_id")
    norm_pool = norm_rec.get("pool_address")

    venue_val = record.get("venue") or record.get("exchange")
    venue_str = str(venue_val or "").strip().lower()
    pair_val = record.get("venue_pair") or record.get("pair") or record.get("trading_pair")

    supplied_is_dex = any(info["market_type"] == "dex" for info in parsed_supplied.values())
    supplied_is_cex = any(info["market_type"] == "cex" for info in parsed_supplied.values())

    is_explicit_dex = (
        market_type in DEX_MARKET_TYPES
        or supplied_is_dex
        or bool(top_chain)
        or bool(top_token)
        or bool(top_pool)
        or bool(norm_chain)
        or bool(norm_token)
        or bool(norm_pool)
    )

    is_explicit_cex = (
        market_type in CEX_MARKET_TYPES
        or supplied_is_cex
        or (venue_str in KNOWN_CEX_VENUES and not is_explicit_dex)
    )

    # Contradictory market type indicators
    if is_explicit_dex and (market_type in CEX_MARKET_TYPES or supplied_is_cex):
        return {
            "is_valid": False,
            "market_type": "ambiguous",
            "canonical_identity": None,
            "rejection_reason": "AMBIGUOUS_MARKET_TYPE",
        }

    # 6. DEX Evaluation with thorough cross-validation
    if is_explicit_dex:
        # Cross-validate chain
        active_chain = top_chain or norm_chain
        if top_chain and norm_chain and normalize_chain(top_chain) != normalize_chain(norm_chain):
            return {
                "is_valid": False,
                "market_type": "dex",
                "canonical_identity": None,
                "rejection_reason": "CONFLICTING_CHAIN_IDENTITY",
                "top_chain": str(top_chain),
                "norm_chain": str(norm_chain),
            }

        # Check against supplied canonical strings chain
        if parsed_supplied:
            for info in parsed_supplied.values():
                supplied_chain = info.get("chain")
                if supplied_chain:
                    if top_chain and normalize_chain(top_chain) != supplied_chain:
                        return {
                            "is_valid": False,
                            "market_type": "dex",
                            "canonical_identity": None,
                            "rejection_reason": "CONFLICTING_CHAIN_IDENTITY",
                            "top_chain": str(top_chain),
                            "supplied_chain": str(supplied_chain),
                        }
                    if norm_chain and normalize_chain(norm_chain) != supplied_chain:
                        return {
                            "is_valid": False,
                            "market_type": "dex",
                            "canonical_identity": None,
                            "rejection_reason": "CONFLICTING_CHAIN_IDENTITY",
                            "norm_chain": str(norm_chain),
                            "supplied_chain": str(supplied_chain),
                        }
                    active_chain = active_chain or supplied_chain

        if not active_chain or not str(active_chain).strip():
            return {
                "is_valid": False,
                "market_type": "dex",
                "canonical_identity": None,
                "rejection_reason": "MISSING_CHAIN",
            }
        chain_norm = normalize_chain(active_chain)

        # Cross-validate token
        if top_token and norm_token and not same_address(chain_norm, top_token, norm_token):
            return {
                "is_valid": False,
                "market_type": "dex",
                "canonical_identity": None,
                "rejection_reason": "CONFLICTING_TOKEN_IDENTITY",
                "top_token": str(top_token),
                "norm_token": str(norm_token),
            }
        active_token = top_token or norm_token
        if parsed_supplied:
            for info in parsed_supplied.values():
                supplied_token = info.get("token")
                if supplied_token:
                    if top_token and not same_address(chain_norm, top_token, supplied_token):
                        return {
                            "is_valid": False,
                            "market_type": "dex",
                            "canonical_identity": None,
                            "rejection_reason": "CONFLICTING_TOKEN_IDENTITY",
                            "top_token": str(top_token),
                            "supplied_token": str(supplied_token),
                        }
                    if norm_token and not same_address(chain_norm, norm_token, supplied_token):
                        return {
                            "is_valid": False,
                            "market_type": "dex",
                            "canonical_identity": None,
                            "rejection_reason": "CONFLICTING_TOKEN_IDENTITY",
                            "norm_token": str(norm_token),
                            "supplied_token": str(supplied_token),
                        }
                    active_token = active_token or supplied_token

        if not active_token or not str(active_token).strip():
            return {
                "is_valid": False,
                "market_type": "dex",
                "canonical_identity": None,
                "rejection_reason": "MISSING_TOKEN",
            }

        # Cross-validate pool
        if top_pool and norm_pool and not same_address(chain_norm, top_pool, norm_pool):
            return {
                "is_valid": False,
                "market_type": "dex",
                "canonical_identity": None,
                "rejection_reason": "CONFLICTING_POOL_IDENTITY",
                "top_pool": str(top_pool),
                "norm_pool": str(norm_pool),
            }
        active_pool = top_pool or norm_pool
        if parsed_supplied:
            for info in parsed_supplied.values():
                supplied_pool = info.get("pool")
                if supplied_pool:
                    if top_pool and not same_address(chain_norm, top_pool, supplied_pool):
                        return {
                            "is_valid": False,
                            "market_type": "dex",
                            "canonical_identity": None,
                            "rejection_reason": "CONFLICTING_POOL_IDENTITY",
                            "top_pool": str(top_pool),
                            "supplied_pool": str(supplied_pool),
                        }
                    if norm_pool and not same_address(chain_norm, norm_pool, supplied_pool):
                        return {
                            "is_valid": False,
                            "market_type": "dex",
                            "canonical_identity": None,
                            "rejection_reason": "CONFLICTING_POOL_IDENTITY",
                            "norm_pool": str(norm_pool),
                            "supplied_pool": str(supplied_pool),
                        }
                    active_pool = active_pool or supplied_pool

        if not active_pool or not str(active_pool).strip():
            # A missing pool address must NEVER be silently converted to CEX
            return {
                "is_valid": False,
                "market_type": "dex",
                "canonical_identity": None,
                "rejection_reason": "MISSING_POOL",
            }

        dex_id = build_canonical_dex_identity(chain_norm, active_token, active_pool)
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
            "chain": chain_norm,
            "token": address_key(chain_norm, active_token),
            "pool": address_key(chain_norm, active_pool),
        }

    # 7. CEX Evaluation
    if is_explicit_cex:
        active_venue = venue_val
        active_pair = pair_val
        if parsed_supplied:
            first_parsed = next(iter(parsed_supplied.values()))
            if venue_val and str(venue_val).strip().lower() != first_parsed.get("exchange"):
                return {
                    "is_valid": False,
                    "market_type": "cex",
                    "canonical_identity": None,
                    "rejection_reason": "CONFLICTING_CEX_FIELDS",
                }
            if pair_val and str(pair_val).strip().upper().replace("/", "-") != first_parsed.get("pair"):
                return {
                    "is_valid": False,
                    "market_type": "cex",
                    "canonical_identity": None,
                    "rejection_reason": "CONFLICTING_CEX_FIELDS",
                }
            active_venue = active_venue or first_parsed.get("exchange")
            active_pair = active_pair or first_parsed.get("pair")
            if not market_type:
                market_type = first_parsed.get("market_sub_type", "cex_spot")

        if not active_venue or not str(active_venue).strip() or not active_pair or not str(active_pair).strip():
            return {
                "is_valid": False,
                "market_type": "cex",
                "canonical_identity": None,
                "rejection_reason": "MISSING_CEX_FIELDS",
            }

        cex_id = build_canonical_cex_identity(active_venue, market_type or "cex_spot", active_pair)
        return {
            "is_valid": True,
            "market_type": "cex",
            "canonical_identity": cex_id,
            "exchange": str(active_venue).strip().lower(),
            "pair": str(active_pair).strip().upper().replace("/", "-"),
            "market_sub_type": market_type or "cex_spot",
        }

    # 8. Ambiguous Market Type
    return {
        "is_valid": False,
        "market_type": "ambiguous",
        "canonical_identity": None,
        "rejection_reason": "AMBIGUOUS_MARKET_TYPE",
    }
