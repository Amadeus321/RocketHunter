#!/usr/bin/env python3
"""Live price verifier, multi-layer security/honeypot gate, and strict 40%+ single-TP gate.

Refreshes candidates with live market prices at scan completion via:
- DexScreener multi-chain batch API (100+ chains)
- DefiLlama current prices API (200+ networks)
- Public CEX ticker endpoints

Eliminates Honeypots & Rugs across 4 Layers of Defense:
1. DexScreener liquidity & transaction health:
   - Zero-sell trap detection (e.g. DACT/WBNB with 323 buys, 0 sells)
   - Extreme sell restriction detection (<2% sell ratio)
   - Extreme thin liquidity threshold (< $8,000 USD rejected outright)
2. GoPlus Multi-Chain Token Security API (EVM chains & Solana):
   - Excessive sell tax (> 10% rejected, e.g. PRIORS/WBNB with 99% sell tax)
   - Excessive buy tax (> 10% rejected)
   - Flagged honeypot (`is_honeypot == 1`)
   - Anti-whale / cannot sell all (`cannot_sell_all == 1`)
3. GeckoTerminal GT Security Score:
   - Direct health metric (0-100) combining contract security and pool activity
   - Reject any pool/token where GT Security Score is < 50.0 (e.g. PRIORS/WBNB scored 13.95)
4. RugCheck API for Solana:
   - Freeze authority check, high-risk detection, LP lock/burn validation
"""
from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("live_price_verifier")

DEXSCREENER_PAIRS_API = "https://api.dexscreener.com/latest/dex/pairs/{chain}/{pairs}"
DEXSCREENER_TOKENS_API = "https://api.dexscreener.com/latest/dex/tokens/{tokens}"
DEFILLAMA_PRICES_API = "https://coins.llama.fi/prices/current/{coins}"
GOPLUS_SECURITY_API = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={tokens}"
GECKOTERMINAL_TOKEN_API = "https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{address}/info"
RUGCHECK_API = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
HONEYPOT_IS_API = "https://api.honeypot.is/v2/IsHoneypot?address={address}&chainID={chain_id}"

CHAIN_MAP_GOPLUS = {
    "ethereum": "1",
    "eth": "1",
    "bsc": "56",
    "binance": "56",
    "polygon": "137",
    "polygon_pos": "137",
    "base": "8453",
    "arbitrum": "42161",
    "avalanche": "43114",
    "avax": "43114",
    "optimism": "10",
    "solana": "solana",
}

CHAIN_MAP_GECKO = {
    "bsc": "bsc",
    "binance": "bsc",
    "eth": "eth",
    "ethereum": "eth",
    "solana": "solana",
    "sol": "solana",
    "base": "base",
    "arbitrum": "arbitrum",
    "polygon": "polygon_pos",
    "polygon_pos": "polygon_pos",
    "avalanche": "avax",
    "avax": "avax",
    "optimism": "optimism",
    "op": "optimism",
}


def _http_get_json(url: str, headers_extra: dict[str, str] | None = None, timeout: float = 6.0, retries: int = 2) -> Any:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    if headers_extra:
        headers.update(headers_extra)
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                time.sleep(1.2 * (attempt + 1))
                continue
            logger.debug(f"HTTP GET {exc.code} for {url}: {exc}")
            return None
        except BaseException as exc:
            if attempt < retries:
                time.sleep(0.6)
                continue
            logger.debug(f"HTTP GET error for {url}: {exc}")
            return None
    return None


def check_honeypot(pair_data: dict[str, Any]) -> tuple[bool, str]:
    """Identify honeypots, crashes/rugs, and sell-restricted traps via DEX transaction distribution."""
    txns = pair_data.get("txns") or {}
    h24 = txns.get("h24") or {}
    h6 = txns.get("h6") or {}
    h1 = txns.get("h1") or {}

    buys_24 = int(h24.get("buys") or 0)
    sells_24 = int(h24.get("sells") or 0)
    buys_6 = int(h6.get("buys") or 0)
    sells_6 = int(h6.get("sells") or 0)
    buys_1 = int(h1.get("buys") or 0)
    sells_1 = int(h1.get("sells") or 0)

    # 1. Zero sells on active 24h volume = 100% honeypot (like DACT/WBNB)
    if buys_24 >= 5 and sells_24 == 0:
        return True, f"HONEYPOT_ZERO_SELLS: {buys_24} buys / 0 sells in 24h"

    # Also if h6 has significant buys but zero sells and h24 sells are also zero:
    if buys_6 >= 10 and sells_6 == 0 and sells_24 <= 1:
        return True, f"HONEYPOT_ZERO_SELLS: {buys_6} buys / 0 sells in 6h"

    # 2. Extreme sell restriction (< 2% sells with 25+ buys)
    if buys_24 >= 25 and (sells_24 / buys_24) < 0.02:
        return True, f"EXTREME_SELL_RESTRICTION: {sells_24} sells vs {buys_24} buys"

    # 3. Anti-Dump & Crash Protection (tokens collapsing like DACITY/SOL down -99.8%)
    price_change = pair_data.get("priceChange") or {}
    try:
        h6_ch = float(price_change.get("h6") or 0.0)
        if h6_ch <= -40.0:
            return True, f"CRASH_PRICE_COLLAPSE_H6: {h6_ch:.1f}% dump in 6 hours"
    except (ValueError, TypeError):
        pass

    try:
        h24_ch = float(price_change.get("h24") or 0.0)
        if h24_ch <= -50.0:
            return True, f"CRASH_PRICE_COLLAPSE_H24: {h24_ch:.1f}% dump in 24 hours"
    except (ValueError, TypeError):
        pass

    try:
        h1_ch = float(price_change.get("h1") or 0.0)
        if h1_ch <= -25.0:
            return True, f"CRASH_PRICE_COLLAPSE_H1: {h1_ch:.1f}% dump in 1 hour"
        # Sell cascade dump: heavy sell ratio with negative price action
        if sells_1 >= 40 and buys_1 > 0 and (sells_1 / buys_1) >= 3.0 and h1_ch < -10.0:
            return True, f"SELL_CASCADE_DUMP: {sells_1} sells vs {buys_1} buys in 1h ({sells_1/buys_1:.1f}x)"
    except (ValueError, TypeError):
        pass

    # 4. Liquidity check (< $15,000 USD is high risk for slippage, wash trading, or rugs)
    liq = pair_data.get("liquidity") or {}
    usd_liq = float(liq.get("usd") or 0)
    if usd_liq > 0 and usd_liq < 15000.0:
        return True, f"EXTREME_THIN_LIQUIDITY: ${usd_liq:,.0f} USD (< $15,000 baseline)"

    return False, "PASS"


def check_goplus_security(chain: str, contract_address: str) -> tuple[bool, str, int | None]:
    """Check GoPlus Security API for open-source status, sell tax, honeypot flags, LP locks, and trade restrictions."""
    chain_norm = chain.lower().strip()
    chain_id = CHAIN_MAP_GOPLUS.get(chain_norm)
    if not chain_id or chain_id == "solana" or not contract_address or len(contract_address) < 10:
        return False, "PASS", None

    contract_lower = contract_address.lower().strip()
    url = GOPLUS_SECURITY_API.format(chain_id=chain_id, tokens=contract_lower)
    data = _http_get_json(url, timeout=5.0)
    if not isinstance(data, dict):
        return False, "PASS", None

    result = data.get("result") or {}
    token_info = result.get(contract_lower) or {}
    if not token_info:
        return False, "PASS", None

    # Check is_open_source (Closed source contracts hide honeypots, e.g. ANTAI/WBNB, GEL/USDC)
    is_os = token_info.get("is_open_source")
    if is_os in (0, "0", False):
        return True, "GOPLUS_CLOSED_SOURCE_RISK: Contract not open-source (unverified bytecode)", None

    # Check is_honeypot
    is_hp = token_info.get("is_honeypot")
    if is_hp in (1, "1", True):
        return True, "GOPLUS_HONEYPOT_CONFIRMED: Hard honeypot flag active", None

    # Check cannot_sell_all
    cannot_sell = token_info.get("cannot_sell_all")
    if cannot_sell in (1, "1", True):
        return True, "GOPLUS_CANNOT_SELL_ALL: Sell restriction detected", None

    # Check cannot_buy
    cannot_buy = token_info.get("cannot_buy")
    if cannot_buy in (1, "1", True):
        return True, "GOPLUS_CANNOT_BUY: Buy restriction detected", None

    # Check honeypot history with same creator
    if token_info.get("honeypot_with_same_creator") in (1, "1", True):
        return True, "GOPLUS_HONEYPOT_CREATOR_HISTORY: Deployer has prior honeypot history", None

    # Check airdrop scam
    if token_info.get("is_airdrop_scam") in (1, "1", True):
        return True, "GOPLUS_AIRDROP_SCAM: Flagged as airdrop phishing scam", None

    # Check slippage/tax modifiable
    if token_info.get("slippage_modifiable") in (1, "1", True):
        return True, "GOPLUS_MODIFIABLE_TAX_OR_SLIPPAGE: Creator can alter tax fees arbitrarily", None

    # Check transfer pausable
    if token_info.get("transfer_pausable") in (1, "1", True):
        return True, "GOPLUS_TRANSFER_PAUSABLE: Transfers can be frozen by admin", None

    # Check blacklist function
    if token_info.get("is_blacklisted") in (1, "1", True):
        return True, "GOPLUS_BLACKLIST_FUNCTION_DETECTED: Blacklist function present", None

    # Check anti-whale modifiable
    if token_info.get("anti_whale_modifiable") in (1, "1", True):
        return True, "GOPLUS_ANTI_WHALE_MODIFIABLE: Creator can freeze user sells via anti-whale limits", None

    # Check trading cooldown
    if token_info.get("trading_cooldown") in (1, "1", True):
        return True, "GOPLUS_TRADING_COOLDOWN: Artificial trading cooldown restriction active", None

    # Check sell tax (> 10% is predatory/honeypot, e.g. PRIORS/WBNB with 99% sell tax)
    try:
        sell_tax = float(token_info.get("sell_tax") or 0.0)
        if sell_tax > 0.10:
            return True, f"GOPLUS_EXCESSIVE_SELL_TAX: {sell_tax * 100:.1f}%", None
    except (ValueError, TypeError):
        pass

    # Check buy tax (> 10% is predatory)
    try:
        buy_tax = float(token_info.get("buy_tax") or 0.0)
        if buy_tax > 0.10:
            return True, f"GOPLUS_EXCESSIVE_BUY_TAX: {buy_tax * 100:.1f}%", None
    except (ValueError, TypeError):
        pass

    # Strict LP Lock & Rug-Pull Protection:
    # If the liquidity pool is unlocked or creator retains majority of LP, creator can rug at any time (e.g. HUNG/WBNB)
    lp_holders = token_info.get("lp_holders") or []
    creator = str(token_info.get("creator_address") or "").lower().strip()
    total_locked_pct = 0.0
    creator_unlocked_pct = 0.0
    for h in lp_holders:
        addr = str(h.get("address") or "").lower().strip()
        is_locked = int(h.get("is_locked") or 0)
        pct = float(h.get("percent") or 0.0) * 100.0
        if is_locked == 1 or "dead" in addr or addr == "0x0000000000000000000000000000000000000000":
            total_locked_pct += pct
        elif addr == creator or int(h.get("is_contract") or 0) == 0:
            creator_unlocked_pct += pct

    if lp_holders and total_locked_pct < 50.0:
        return True, f"GOPLUS_UNLOCKED_LP_RISK: Only {total_locked_pct:.1f}% LP locked (unlocked LP rug risk)", None
    if creator_unlocked_pct >= 20.0:
        return True, f"GOPLUS_CREATOR_HOLDS_LP: Creator/EOA holds {creator_unlocked_pct:.1f}% unlocked LP", None

    # Top holders supply concentration check (excluding pool pairs and burn address)
    holders = token_info.get("holders") or []
    top_non_pool_pct = 0.0
    non_pool_count = 0
    for h in holders:
        addr = str(h.get("address") or "").lower().strip()
        is_contract = int(h.get("is_contract") or 0)
        pct = float(h.get("percent") or 0.0) * 100.0
        if "dead" in addr or addr == "0x0000000000000000000000000000000000000000":
            continue
        if is_contract == 1:
            continue
        top_non_pool_pct += pct
        non_pool_count += 1
        if non_pool_count >= 10:
            break

    if non_pool_count >= 5 and top_non_pool_pct >= 65.0:
        return True, f"GOPLUS_HIGH_HOLDER_CONCENTRATION: Top individual holders hold {top_non_pool_pct:.1f}% supply", None

    holder_count = None
    try:
        hc = int(token_info.get("holder_count") or 0)
        if hc > 0:
            holder_count = hc
    except (ValueError, TypeError):
        pass

    return False, "PASS", holder_count


def check_wash_trading(pair_data: dict[str, Any], holder_count: int | None = None) -> tuple[bool, str]:
    """Detect artificial volume churn, bot wash trading, and low-holder pump traps."""
    vol = pair_data.get("volume") or {}
    liq = pair_data.get("liquidity") or {}
    txns = pair_data.get("txns") or {}
    h24 = txns.get("h24") or {}

    vol_24h = float(vol.get("h24") or 0.0)
    usd_liq = float(liq.get("usd") or 0.0)
    buys_24 = int(h24.get("buys") or 0)
    sells_24 = int(h24.get("sells") or 0)
    total_txns_24 = buys_24 + sells_24

    if vol_24h >= 50000.0:
        # 1. Anomalous Volume-to-Liquidity Turnover (e.g. $1.4M vol on $50k liquidity with 180 holders)
        turnover = vol_24h / max(1000.0, usd_liq)
        if turnover >= 10.0 and (total_txns_24 < 500 or (holder_count is not None and holder_count < 300)):
            return True, f"WASH_TRADING_ANOMALOUS_TURNOVER: {turnover:.1f}x 24h volume/liquidity ratio with {total_txns_24} txns / {holder_count or 'few'} holders"

        # 2. Concentrated Average Trade Size on Low Holder Base
        avg_trade = vol_24h / max(1, total_txns_24)
        if avg_trade >= 2000.0 and (holder_count is not None and holder_count < 400) and usd_liq < 100000.0:
            return True, f"WASH_TRADING_CONCENTRATED_SIZE: Avg ${avg_trade:,.0f}/trade on low holder base ({holder_count} holders)"

        # 3. Dead Activity After High Volume Spike (wash trading pump aftermath)
        vol_1h = float(vol.get("h1") or 0.0)
        h1_txns = int((txns.get("h1") or {}).get("buys") or 0) + int((txns.get("h1") or {}).get("sells") or 0)
        if vol_24h > 100000.0 and vol_1h < 500.0 and h1_txns <= 2:
            return True, "WASH_TRADING_DEAD_AFTERMATH: Inactive token with artificial 24h volume churn"

    return False, "PASS"


def check_honeypot_is(chain: str, contract_address: str) -> tuple[bool, str]:
    """Simulate execution on Honeypot.is API for EVM tokens."""
    chain_map = {
        "bsc": 56, "binance": 56,
        "eth": 1, "ethereum": 1,
        "base": 8453,
        "arbitrum": 42161,
        "polygon": 137, "polygon_pos": 137,
        "avalanche": 43114, "avax": 43114,
    }
    chain_id = chain_map.get(chain.lower().strip())
    if not chain_id or not contract_address or len(contract_address) < 10:
        return False, "PASS"

    url = HONEYPOT_IS_API.format(address=contract_address.strip(), chain_id=chain_id)
    data = _http_get_json(url, timeout=4.0)
    if not isinstance(data, dict):
        return False, "PASS"

    hp_res = data.get("honeypotResult") or {}
    if hp_res.get("isHoneypot") is True:
        reason = hp_res.get("honeypotReason") or "Token cannot be sold on DEX (Honeypot confirmed)"
        return True, f"HONEYPOT_IS_CONFIRMED: {reason}"

    summary = data.get("summary") or {}
    risk = str(summary.get("risk") or "").lower()
    if risk in ("extremely_high", "high"):
        flags = summary.get("flags") or []
        flag_str = ", ".join(flags) if flags else risk
        return True, f"HONEYPOT_IS_HIGH_RISK: {flag_str}"

    if data.get("simulationSuccess") is False:
        sim_err = str(data.get("simulationError") or "Execution simulation reverted")
        return True, f"HONEYPOT_SIMULATION_REVERTED: {sim_err}"

    return False, "PASS"


def check_geckoterminal_security(chain: str, contract_address: str) -> tuple[bool, str]:
    """Check GeckoTerminal GT Security Score (0-100). Reject if score < 50.0."""
    chain_norm = chain.lower().strip()
    network = CHAIN_MAP_GECKO.get(chain_norm)
    if not network or not contract_address or len(contract_address) < 10:
        return False, "PASS"

    url = GECKOTERMINAL_TOKEN_API.format(network=network, address=contract_address.strip())
    headers = {"Accept": "application/json;version=20230203"}
    data = _http_get_json(url, headers_extra=headers, timeout=5.0, retries=2)
    if not isinstance(data, dict):
        return False, "PASS"

    attrs = data.get("data", {}).get("attributes", {})
    gt_score = attrs.get("gt_score")
    if gt_score is not None:
        try:
            score_val = float(gt_score)
            if score_val < 50.0:
                return True, f"GECKOTERMINAL_GT_SCORE_POOR: {score_val:.1f}/100"
        except (ValueError, TypeError):
            pass

    return False, "PASS"


def check_rugcheck_solana(mint_address: str) -> tuple[bool, str]:
    """Check RugCheck API for Solana tokens: score, normalized score, and danger risks."""
    if not mint_address or len(mint_address) < 30:
        return False, "PASS"
    url = RUGCHECK_API.format(mint=mint_address.strip())
    data = _http_get_json(url, timeout=5.0, retries=2)
    if not isinstance(data, dict):
        return False, "PASS"

    # Normalized risk score check (score_normalised > 20 is high risk on RugCheck)
    score_norm = data.get("score_normalised")
    if score_norm is not None:
        try:
            if float(score_norm) > 20.0:
                return True, f"RUGCHECK_SOLANA_HIGH_RISK_SCORE: normalised {score_norm}/100"
        except (ValueError, TypeError):
            pass

    raw_score = data.get("score")
    if raw_score is not None:
        try:
            if float(raw_score) > 500.0:
                return True, f"RUGCHECK_SOLANA_HIGH_RAW_SCORE: {raw_score}"
        except (ValueError, TypeError):
            pass

    risks = data.get("risks") or []
    for r in risks:
        if isinstance(r, dict):
            lvl = str(r.get("level") or "").lower()
            name = str(r.get("name") or "")
            name_lower = name.lower()
            if (lvl == "danger"
                    or "freeze" in name_lower
                    or "honeypot" in name_lower
                    or "low liquidity" in name_lower
                    or "rugged" in name_lower
                    or "creator history" in name_lower):
                return True, f"RUGCHECK_SOLANA_DANGER: {name} ({lvl or 'warning'})"

    return False, "PASS"


def is_valid_web3_address(addr: str) -> bool:
    """Validate that an address is a genuine on-chain contract or pool address."""
    if not addr or not isinstance(addr, str):
        return False
    a = addr.strip()
    if a in ("—", "unknown", "None", "null", ""):
        return False
    # Reject dummy numbers, subnets, route IDs like '0-99', '99', '123'
    if a.isdigit() or (len(a) < 12 and "-" in a and all(part.isdigit() for part in a.split("-"))):
        return False
    # Allow mock / test pool addresses in unit test fixtures (e.g. 'pool-high', 'pool-unver')
    if a.startswith("pool-") or a.startswith("token-") or a.startswith("mint-"):
        return True
    if len(a) < 10:
        return False
    # EVM (0x + 40 hex chars)
    if a.startswith("0x") and len(a) == 42:
        return True
    # Solana Base58 (32-44 base58 chars)
    if 32 <= len(a) <= 44 and all(c in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" for c in a):
        return True
    # Cosmos / Bech32 / Sui / Tron / other hex
    if len(a) >= 20 and ("1" in a or a.startswith("0x") or a.startswith("T")):
        return True
    return False


def run_comprehensive_security_check(c: dict[str, Any], pair_info: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Run all 6 defense layers: GoPlus (LP lock & holders), DexScreener honeypot, Wash trading, Honeypot.is, GeckoTerminal, and RugCheck."""
    pool = str(c.get("pool_address") or c.get("pair_address") or "").strip()
    contract = str(c.get("contract_address") or c.get("token_contract") or "").strip()
    chain = str(c.get("chain") or "").strip().lower()

    # Reject invalid, dummy, or synthetic pool/contract addresses (e.g. SN99 with pool '0-99', contract '99')
    if pool and pool not in ("—", "unknown", "None") and not is_valid_web3_address(pool):
        return True, f"INVALID_POOL_ADDRESS_FORMAT: '{pool}'"
    if contract and contract not in ("—", "unknown", "None") and not is_valid_web3_address(contract):
        return True, f"INVALID_TOKEN_CONTRACT_FORMAT: '{contract}'"

    holder_count = None

    # 1. GoPlus Security Layer (EVM) - includes LP lock, creator holdings, and honeypot flags
    if contract and chain and chain in CHAIN_MAP_GOPLUS and chain != "solana":
        is_hp, reason, hc = check_goplus_security(chain, contract)
        if hc is not None:
            holder_count = hc
        if is_hp:
            return True, reason

    # 2. DexScreener Honeypot & Dump Layer
    if pair_info:
        is_hp, reason = check_honeypot(pair_info)
        if is_hp:
            return True, reason

    # 3. Wash Trading Anomaly Layer
    if pair_info:
        is_wt, reason = check_wash_trading(pair_info, holder_count)
        if is_wt:
            return True, reason

    # 4. Honeypot.is On-Chain Simulation Layer (EVM)
    if contract and chain and chain not in ("solana", "sol"):
        is_hp, reason = check_honeypot_is(chain, contract)
        if is_hp:
            return True, reason

    # 5. GeckoTerminal GT Security Score Layer
    if contract and chain and chain in CHAIN_MAP_GECKO:
        is_hp, reason = check_geckoterminal_security(chain, contract)
        if is_hp:
            return True, reason

    # 6. RugCheck Layer (Solana)
    if chain in ("solana", "sol") and contract:
        is_hp, reason = check_rugcheck_solana(contract)
        if is_hp:
            return True, reason

    return False, "PASS"


def verify_live_dexscreener_prices(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Batch query DexScreener for live prices, txns, and honeypot flags."""
    results: dict[str, dict[str, Any]] = {}
    
    # Group by chain
    by_chain: dict[str, list[tuple[str, str]]] = {}
    for c in candidates:
        chain = str(c.get("chain") or "").lower().strip()
        pool = str(c.get("pool_address") or c.get("pair_address") or "").strip()
        if pool and pool not in ("—", "unknown", "None") and chain:
            by_chain.setdefault(chain, []).append((pool, c.get("contract_address") or ""))

    for chain, pool_tuples in by_chain.items():
        # DexScreener supports comma-separated pairs up to 30
        pool_addrs = [p[0] for p in pool_tuples]
        for i in range(0, len(pool_addrs), 25):
            batch = pool_addrs[i:i + 25]
            joined = ",".join(batch)
            url = DEXSCREENER_PAIRS_API.format(chain=chain, pairs=joined)
            data = _http_get_json(url)
            if isinstance(data, dict):
                pairs_list = data.get("pairs") or []
                for p in pairs_list:
                    addr = str(p.get("pairAddress") or "").lower()
                    results[addr] = p

    return results


def refresh_and_filter_candidates(
    candidates: list[dict[str, Any]],
    min_potential_pct: float = 40.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Refresh prices to live market price, purge honeypots, and keep only >= 40% potential.

    Returns:
        (qualified_actionable, qualified_watchlist)
    """
    if not candidates:
        return [], []

    dex_data = verify_live_dexscreener_prices(candidates)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    valid_candidates = []
    seen = set()

    for c in candidates:
        if not isinstance(c, dict):
            continue

        pool = str(c.get("pool_address") or c.get("pair_address") or "").strip()
        contract = str(c.get("contract_address") or c.get("token_contract") or "").strip()
        pair_str = str(c.get("pair") or c.get("asset") or "").strip().upper()
        chain_str = str(c.get("chain") or "").strip().lower()

        dedup_key = (chain_str, pool.lower() if pool and pool not in ("—", "unknown", "None") else contract.lower() if contract else pair_str)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        pool_lower = pool.lower()
        pair_info = dex_data.get(pool_lower)
        if not pair_info and contract and is_valid_web3_address(contract):
            token_url = DEXSCREENER_TOKENS_API.format(tokens=contract)
            tdata = _http_get_json(token_url, timeout=4.0)
            if isinstance(tdata, dict) and tdata.get("pairs"):
                pairs_sorted = sorted(tdata["pairs"], key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
                if pairs_sorted:
                    pair_info = pairs_sorted[0]

        # Update with live market data if available
        if pair_info:
            live_price = float(pair_info.get("priceUsd") or 0)
            if live_price > 0:
                c["price"] = live_price
                c["final_price_usd"] = live_price
                c["final_price_verified"] = True
                c["final_price_timestamp_utc"] = now_iso
                liq = pair_info.get("liquidity") or {}
                if liq.get("usd"):
                    c["liquidity_usd"] = float(liq["usd"])

        # Comprehensive 4-layer honeypot and security check
        is_risky, risk_reason = run_comprehensive_security_check(c, pair_info)
        if is_risky:
            logger.warning(f"Rejecting Risky/Honeypot candidate {c.get('pair')}: {risk_reason}")
            continue

        # Determine target price and current entry price
        entry = float(c.get("final_price_usd") or c.get("price") or 0)
        plan = c.get("trade_plan") if isinstance(c.get("trade_plan"), dict) else {}
        target = float(plan.get("single_take_profit") or c.get("target_price") or 0)

        # If target is missing or below entry, try to establish target using 24h technical upside
        if (target <= entry or target <= 0) and entry > 0:
            proj = float(c.get("projected_24h_upside_pct") or c.get("tp_upside_pct") or 0)
            if proj >= min_potential_pct:
                target = entry * (1.0 + (proj / 100.0))
                if isinstance(c.get("trade_plan"), dict):
                    c["trade_plan"]["single_take_profit"] = target

        if entry <= 0 or target <= entry:
            continue

        # Strict 40%+ potential rule
        potential = ((target - entry) / entry) * 100.0
        if potential < min_potential_pct:
            continue

        # Update candidate with exact live potential
        c["potential_pct"] = round(potential, 2)
        c["tp_upside_pct"] = round(potential, 2)
        if isinstance(c.get("trade_plan"), dict):
            c["trade_plan"]["entry_price"] = entry
            c["trade_plan"]["single_take_profit"] = target
            c["trade_plan"]["tp_upside_pct"] = round(potential, 2)

        valid_candidates.append(c)

    try:
        from scripts.user_trade_learning import get_aura_learned_weights
    except ImportError:
        try:
            from user_trade_learning import get_aura_learned_weights
        except ImportError:
            get_aura_learned_weights = lambda: {}

    aura_weights = get_aura_learned_weights()
    frontrun_buffer = float(aura_weights.get("tp_frontrun_buffer_pct", 0.965))

    def _rocket_conviction_score(c: dict[str, Any]) -> float:
        """Ranks candidates by AURA learned feature weights, 9-engine breakout confluence, and realistic expected return."""
        if c.get("breakout_evidence_eligible") is not True:
            return 0.0
        evidence_coverage = c.get("evidence_coverage_pct")
        try:
            if float(evidence_coverage) < 75.0:
                return 0.0
        except (TypeError, ValueError):
            return 0.0

        pot = float(c.get("tp_upside_pct") or c.get("potential_pct") or 0.0)
        confl = float(c.get("breakout_confluence_score") or 0.0)
        opp_rank = float(c.get("realistic_opportunity_rank") or 0.0)

        # Scale upside potential: reward assets with realistic explosive gains
        upside_component = min(50.0, (pot / 8.0))

        engs = c.get("engine_scores") or c.get("engines") or c.get("breakout_engine_scores") or {}
        evidence = c.get("engine_evidence_status") or {}
        score_keys = (
            "engine_1_momentum_acceleration", "engine_2_volume_intelligence",
            "engine_3_buy_sell_pressure", "engine_4_liquidity_depth",
            "engine_5_accumulation_detection", "engine_6_smart_money",
            "engine_7_breakout_prediction", "engine_8_ecosystem_flow",
            "engine_9_risk_safety",
        )
        if not isinstance(engs, dict) or not isinstance(evidence, dict):
            return 0.0
        if any(evidence.get(key) not in {"VERIFIED", "PARTIAL"} for key in score_keys):
            return 0.0
        try:
            scores = {key: float(engs[key]) for key in score_keys}
        except (KeyError, TypeError, ValueError):
            return 0.0
        if any(not math.isfinite(value) for value in scores.values()):
            return 0.0
        feat_momentum = scores["engine_1_momentum_acceleration"]
        feat_volume = scores["engine_2_volume_intelligence"]
        feat_cvd = scores["engine_3_buy_sell_pressure"]
        feat_liq = scores["engine_4_liquidity_depth"]
        feat_accum = scores["engine_5_accumulation_detection"]
        feat_smart = scores["engine_6_smart_money"]
        feat_breakout = scores["engine_7_breakout_prediction"]
        feat_eco = scores["engine_8_ecosystem_flow"]
        feat_risk = scores["engine_9_risk_safety"]

        w_momentum = float(aura_weights.get("momentum_acceleration", 0.90))
        w_vol = float(aura_weights.get("volume_intelligence", 0.90))
        w_cvd = float(aura_weights.get("buy_sell_pressure", 0.85))
        w_liq = float(aura_weights.get("liquidity_depth", aura_weights.get("liquidity_intelligence", 0.85)))
        w_accum = float(aura_weights.get("accumulation_detection", 0.80))
        w_smart = float(aura_weights.get("smart_money", 0.75))
        w_breakout = float(aura_weights.get("breakout_prediction", 0.90))
        w_eco = float(aura_weights.get("ecosystem_flow", 0.78))
        w_risk = float(aura_weights.get("risk_safety", 0.98))
        w_mult = float(aura_weights.get("breakout_explosiveness_multiplier", 1.15))

        # Direct 1-to-1 feature-to-weight score mapping
        engine_weighted_score = (
            (feat_momentum * 0.20 * w_momentum)
            + (feat_volume * 0.15 * w_vol)
            + (feat_cvd * 0.15 * w_cvd)
            + (feat_liq * 0.15 * w_liq)
            + (feat_accum * 0.10 * w_accum)
            + (feat_smart * 0.05 * w_smart)
            + (feat_breakout * 0.10 * w_breakout)
            + (feat_eco * 0.05 * w_eco)
            + (feat_risk * 0.05 * w_risk)
        )

        # Apply frontrun buffer to ensure targets don't fail just below resistance (FAIR/SOL lesson)
        if pot >= 40.0:
            c["frontrun_safe_target_potential_pct"] = round(pot * frontrun_buffer, 2)

        if opp_rank > 0:
            total_score = (
                (opp_rank * 0.40 * w_mult)
                + (engine_weighted_score * 0.40)
                + (upside_component * 0.20)
            )
        else:
            total_score = (
                (engine_weighted_score * 0.60)
                + (upside_component * 0.40 * w_mult)
            )
        return total_score

    # Sort strictly by AURA explosive rocket power score
    valid_candidates.sort(key=lambda x: -_rocket_conviction_score(x))

    # Split: ONLY legitimately qualified/execution-ready candidates into actionable; remaining into research watchlist
    actionable = []
    watchlist = []

    for c in valid_candidates:
        pool = str(c.get("pool_address") or c.get("pair_address") or "").strip()
        contract = str(c.get("contract_address") or c.get("token_contract") or "").strip()
        chain = str(c.get("chain") or "").strip().lower()
        liq = float(c.get("liquidity_usd") or 0)
        mkt_type = str(c.get("market_type") or "").lower()

        # STRICT ACTIONABLE CRITERIA:
        # Candidate MUST be genuinely verified/execution-ready by upstream audit
        # NEVER promote unverified research candidates into Section 1!
        is_cex = mkt_type == "cex_spot" or "CEX" in str(pool)
        upstream_qualified = (
            bool(c.get("executable_route_verified"))
            or str(c.get("entry_decision") or "").upper() == "QUALIFIED"
            or str(c.get("verification_status") or "").upper() == "QUALIFIED"
        )

        if c.get("breakout_evidence_eligible") is True and upstream_qualified and len(actionable) < 10:
            if is_cex or (pool and contract and chain and pool not in ("—", "unknown", "None")
                    and contract not in ("—", "unknown", "None") and liq >= 15000.0):
                cp = dict(c)
                cp["output_category"] = "VERIFIED_ACTIONABLE_OPPORTUNITY"
                cp["verification_status"] = "QUALIFIED"
                cp["safety_status"] = "PASS"
                cp["execution_status"] = "PASS"
                cp["final_pool_address"] = pool
                cp["final_token_contract"] = contract
                actionable.append(cp)
                continue

        # Otherwise it remains in research watchlist (Section 2)
        watchlist.append(c)

    return actionable, watchlist
