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
