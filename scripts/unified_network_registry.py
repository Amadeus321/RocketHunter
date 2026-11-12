#!/usr/bin/env python3
"""Unified Multi-Provider Web3 Network Registry for Rocket Hunter.

Combines, normalizes, and tracks network information across DEX Screener,
DefiLlama, GeckoTerminal, and DexPaprika. Maps canonical network identities
to provider-specific IDs and tracks per-provider query health, usable pool
counts, and discovery status.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any

# Canonical network mappings across major Web3 providers
CANONICAL_NETWORKS: dict[str, dict[str, Any]] = {
    "solana": {
        "display_name": "Solana",
        "family": "solana",
        "aliases": ["sol", "solana"],
        "provider_ids": {
            "dexscreener": "solana",
            "defillama": "Solana",
            "geckoterminal": "solana",
            "dexpaprika": "sol-solana",
        },
    },
    "ethereum": {
        "display_name": "Ethereum",
        "family": "evm",
        "aliases": ["eth", "ethereum", "mainnet"],
        "provider_ids": {
            "dexscreener": "ethereum",
            "defillama": "Ethereum",
            "geckoterminal": "eth",
            "dexpaprika": "eth-ethereum",
        },
    },
    "bsc": {
        "display_name": "BNB Smart Chain",
        "family": "evm",
        "aliases": ["bsc", "binance", "binance-smart-chain", "bnb", "wbnb"],
        "provider_ids": {
            "dexscreener": "bsc",
            "defillama": "BSC",
            "geckoterminal": "bsc",
            "dexpaprika": "bnb-binance-smart-chain",
        },
    },
    "base": {
        "display_name": "Base",
        "family": "evm",
        "aliases": ["base", "coinbase-base"],
        "provider_ids": {
            "dexscreener": "base",
            "defillama": "Base",
            "geckoterminal": "base",
            "dexpaprika": "base-base",
        },
    },
    "arbitrum": {
        "display_name": "Arbitrum One",
        "family": "evm",
        "aliases": ["arbitrum", "arbitrum-one", "arb", "arbitrum_one"],
        "provider_ids": {
            "dexscreener": "arbitrum",
            "defillama": "Arbitrum",
            "geckoterminal": "arbitrum",
            "dexpaprika": "arb-arbitrum",
        },
    },
    "polygon": {
        "display_name": "Polygon PoS",
        "family": "evm",
        "aliases": ["polygon", "matic", "polygon_pos"],
        "provider_ids": {
            "dexscreener": "polygon",
            "defillama": "Polygon",
            "geckoterminal": "polygon_pos",
            "dexpaprika": "matic-polygon",
        },
    },
    "avalanche": {
        "display_name": "Avalanche C-Chain",
        "family": "evm",
        "aliases": ["avalanche", "avax", "avalanche-c-chain"],
        "provider_ids": {
            "dexscreener": "avalanche",
            "defillama": "Avalanche",
            "geckoterminal": "avax",
            "dexpaprika": "avax-avalanche",
        },
    },
    "optimism": {
        "display_name": "OP Mainnet",
        "family": "evm",
        "aliases": ["optimism", "op", "optimistic-ethereum"],
        "provider_ids": {
            "dexscreener": "optimism",
            "defillama": "Optimism",
            "geckoterminal": "optimism",
            "dexpaprika": "op-optimism",
        },
    },
    "sui": {
        "display_name": "Sui",
        "family": "move",
        "aliases": ["sui", "sui-network"],
        "provider_ids": {
            "dexscreener": "sui",
            "defillama": "Sui",
            "geckoterminal": "sui-network",
            "dexpaprika": "sui-sui",
        },
    },
    "blast": {
        "display_name": "Blast",
        "family": "evm",
        "aliases": ["blast"],
        "provider_ids": {
            "dexscreener": "blast",
            "defillama": "Blast",
            "geckoterminal": "blast",
            "dexpaprika": "blast-blast",
        },
    },
    "linea": {
        "display_name": "Linea",
        "family": "evm",
        "aliases": ["linea"],
        "provider_ids": {
            "dexscreener": "linea",
            "defillama": "Linea",
            "geckoterminal": "linea",
            "dexpaprika": "linea-linea",
        },
    },
    "scroll": {
        "display_name": "Scroll",
        "family": "evm",
        "aliases": ["scroll"],
        "provider_ids": {
            "dexscreener": "scroll",
            "defillama": "Scroll",
            "geckoterminal": "scroll",
            "dexpaprika": "scroll-scroll",
        },
    },
    "fantom": {
        "display_name": "Fantom / Sonic",
        "family": "evm",
        "aliases": ["fantom", "ftm", "sonic"],
        "provider_ids": {
            "dexscreener": "fantom",
            "defillama": "Fantom",
            "geckoterminal": "ftm",
            "dexpaprika": "ftm-fantom",
        },
    },
    "mantle": {
        "display_name": "Mantle",
        "family": "evm",
        "aliases": ["mantle", "mnt"],
        "provider_ids": {
            "dexscreener": "mantle",
            "defillama": "Mantle",
            "geckoterminal": "mantle",
            "dexpaprika": "mnt-mantle",
        },
    },
    "zksync": {
        "display_name": "zkSync Era",
        "family": "evm",
        "aliases": ["zksync", "zksync-era", "era"],
        "provider_ids": {
            "dexscreener": "zksync",
            "defillama": "zkSync Era",
            "geckoterminal": "zksync",
            "dexpaprika": "zksync-zksync-era",
        },
    },
    "aptos": {
        "display_name": "Aptos",
        "family": "move",
        "aliases": ["aptos", "apt"],
        "provider_ids": {
            "dexscreener": "aptos",
            "defillama": "Aptos",
            "geckoterminal": "aptos",
            "dexpaprika": "apt-aptos",
        },
    },
    "sei": {
        "display_name": "Sei",
        "family": "cosmos/evm",
        "aliases": ["sei", "sei-network"],
        "provider_ids": {
            "dexscreener": "sei",
            "defillama": "Sei",
            "geckoterminal": "sei-network",
            "dexpaprika": "sei-sei",
        },
    },
    "mode": {
        "display_name": "Mode Network",
        "family": "evm",
        "aliases": ["mode"],
        "provider_ids": {
            "dexscreener": "mode",
            "defillama": "Mode",
            "geckoterminal": "mode",
            "dexpaprika": "mode-mode",
        },
    },
    "pulsechain": {
        "display_name": "PulseChain",
        "family": "evm",
        "aliases": ["pulsechain", "pls"],
        "provider_ids": {
            "dexscreener": "pulsechain",
            "defillama": "Pulse",
            "geckoterminal": "pulsechain",
            "dexpaprika": "pls-pulsechain",
        },
    },
    "ton": {
        "display_name": "The Open Network (TON)",
        "family": "ton",
        "aliases": ["ton", "the-open-network"],
        "provider_ids": {
            "dexscreener": "ton",
            "defillama": "TON",
            "geckoterminal": "ton",
            "dexpaprika": "ton-the-open-network",
        },
    },
    "tron": {
        "display_name": "TRON",
        "family": "tron",
        "aliases": ["tron", "trx"],
        "provider_ids": {
            "dexscreener": "tron",
            "defillama": "Tron",
            "geckoterminal": "tron",
            "dexpaprika": "trx-tron",
        },
    },
    "cronos": {
        "display_name": "Cronos",
        "family": "evm",
        "aliases": ["cronos", "cro"],
        "provider_ids": {
            "dexscreener": "cronos",
            "defillama": "Cronos",
            "geckoterminal": "cro",
            "dexpaprika": "cro-cronos",
        },
    },
    "celo": {
        "display_name": "Celo",
        "family": "evm",
        "aliases": ["celo"],
        "provider_ids": {
            "dexscreener": "celo",
            "defillama": "Celo",
            "geckoterminal": "celo",
            "dexpaprika": "celo-celo",
        },
    },
    "near": {
        "display_name": "NEAR Protocol",
        "family": "near",
        "aliases": ["near", "near-protocol"],
        "provider_ids": {
            "dexscreener": "near",
            "defillama": "Near",
            "geckoterminal": "near",
            "dexpaprika": "near-near-protocol",
        },
    },
}

# Build fast alias lookup index
_ALIAS_INDEX: dict[str, str] = {}
for canon, data in CANONICAL_NETWORKS.items():
    _ALIAS_INDEX[canon.lower()] = canon
    for alias in data.get("aliases", []):
        _ALIAS_INDEX[alias.lower()] = canon
    for prov, pid in data.get("provider_ids", {}).items():
        if pid:
            _ALIAS_INDEX[str(pid).lower()] = canon


def normalize_chain(name: str | None) -> str:
    """Normalize any raw chain identifier or alias to its canonical network ID."""
    if not name:
        return "unknown"
    clean = re.sub(r"[^a-zA-Z0-9_\-]", "", str(name).strip().lower())
    if clean in _ALIAS_INDEX:
        return _ALIAS_INDEX[clean]
    # Check without hyphens or underscores
    stripped = clean.replace("-", "").replace("_", "")
    for alias, canon in _ALIAS_INDEX.items():
        if alias.replace("-", "").replace("_", "") == stripped:
            return canon
    return clean or "unknown"


def get_provider_chain_id(canonical_id: str, provider: str) -> str | None:
    """Get the provider-specific chain identifier for a canonical network."""
    net = CANONICAL_NETWORKS.get(canonical_id)
    if not net:
        return None
    return net.get("provider_ids", {}).get(provider)


def is_provider_supported(canonical_id: str, provider: str) -> bool:
    """Check whether a given provider supports the specified canonical network."""
    return get_provider_chain_id(canonical_id, provider) is not None


class UnifiedNetworkRegistry:
    """Tracks discovery health, coverage, and usable results per network and provider."""

    def __init__(self) -> None:
        self.networks: dict[str, dict[str, Any]] = {}
        for canon, meta in CANONICAL_NETWORKS.items():
            self.networks[canon] = {
                "canonical_id": canon,
                "display_name": meta["display_name"],
                "family": meta["family"],
                "provider_ids": copy.deepcopy(meta["provider_ids"]),
                "providers": {
                    "dexscreener": {"supported": True, "status": "PENDING", "pools_count": 0, "error": None},
                    "defillama": {"supported": True, "status": "PENDING", "pools_count": 0, "error": None},
                    "geckoterminal": {"supported": True, "status": "PENDING", "pools_count": 0, "error": None},
                    "dexpaprika": {"supported": True, "status": "PENDING", "pools_count": 0, "error": None},
                },
                "ecosystem_flow_score": 1.0,
                "discovery_budget_weight": 1.0,
                "total_usable_pools": 0,
                "first_discovered_utc": None,
                "last_queried_utc": None,
            }

    def record_provider_result(
        self,
        chain: str,
        provider: str,
        status: str,
        pools_count: int = 0,
        error: str | None = None,
    ) -> None:
        """Record the outcome of a discovery query for a network and provider."""
        canon = normalize_chain(chain)
        if canon not in self.networks:
            # Register dynamically discovered network
            self.networks[canon] = {
                "canonical_id": canon,
                "display_name": chain.title(),
                "family": "evm" if canon.startswith("0x") else "other",
                "provider_ids": {provider: chain},
                "providers": {
                    p: {"supported": (p == provider), "status": "PENDING", "pools_count": 0, "error": None}
                    for p in ("dexscreener", "defillama", "geckoterminal", "dexpaprika")
                },
                "ecosystem_flow_score": 1.0,
                "discovery_budget_weight": 1.0,
                "total_usable_pools": 0,
                "first_discovered_utc": datetime.now(timezone.utc).isoformat(),
                "last_queried_utc": None,
            }

        net = self.networks[canon]
        prov_entry = net["providers"].setdefault(provider, {"supported": True, "status": "PENDING", "pools_count": 0, "error": None})
        prov_entry["status"] = status
        prov_entry["pools_count"] = max(prov_entry.get("pools_count", 0), pools_count)
        prov_entry["error"] = error
        net["last_queried_utc"] = datetime.now(timezone.utc).isoformat()
        net["total_usable_pools"] = sum(p.get("pools_count", 0) for p in net["providers"].values())

    def update_flow_weight(self, chain: str, flow_score: float, weight: float) -> None:
        """Update the ecosystem capital flow score and discovery weight for a network."""
        canon = normalize_chain(chain)
        if canon in self.networks:
            self.networks[canon]["ecosystem_flow_score"] = round(max(0.1, min(5.0, flow_score)), 3)
            self.networks[canon]["discovery_budget_weight"] = round(max(0.5, min(3.0, weight)), 3)

    def get_summary(self) -> dict[str, Any]:
        """Produce an exportable summary of multi-provider coverage and registry health."""
        active_chains = [c for c, data in self.networks.items() if data["total_usable_pools"] > 0]
        supported_counts = {
            p: sum(1 for data in self.networks.values() if data["providers"].get(p, {}).get("supported"))
            for p in ("dexscreener", "defillama", "geckoterminal", "dexpaprika")
        }
        usable_counts = {
            p: sum(1 for data in self.networks.values() if (data["providers"].get(p, {}).get("pools_count") or 0) > 0)
            for p in ("dexscreener", "defillama", "geckoterminal", "dexpaprika")
        }
        return {
            "total_registered_networks": len(self.networks),
            "networks_with_usable_pools": len(active_chains),
            "provider_network_coverage": supported_counts,
            "provider_usable_data_networks": usable_counts,
            "networks": self.networks,
        }
