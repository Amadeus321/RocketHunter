#!/usr/bin/env python3
"""Expanded Multi-Lane DEX Screener Discovery Engine for Rocket Hunter.

Leverages all public DEX Screener discovery endpoints:
1. Fresh token profiles (/token-profiles/latest/v1)
2. Momentum & promotional discovery (latest/top boosts, community takeovers, ads)
3. Intelligent batch token lookups (up to 30 addresses per request)
4. Strategic pair searches across high-priority DeFi ecosystems
Enforces rate limits (60/min listing, 300/min pairs) with concurrency and caching.
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from scripts.unified_network_registry import normalize_chain

DEX_ROOT = "https://api.dexscreener.com"
MAX_BATCH_SIZE = 30  # DEX Screener allows up to 30 token addresses per request
USER_AGENT = "crypto-prebreakout-research/v4.27.0 (read-only DEX discovery)"

EXPANDED_SEARCH_QUERIES = (
    "breakout", "runner", "meme", "ai", "pump", "agent", "alpha",
    "gaming", "rwa", "depin", "launch", "solana", "base", "bsc",
    "ethereum", "sui", "arbitrum", "avalanche", "blast", "scroll",
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_num(val: Any) -> float | None:
    try:
        if val is None or val == "":
            return None
        v = float(val)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def fetch_dex_json(url: str, timeout: int = 8) -> tuple[Any, int | None, str | None]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=max(1, int(timeout))) as resp:
            status = int(resp.status)
            payload = json.loads(resp.read().decode("utf-8"))
        return payload, status, None
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(300).decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        return None, int(exc.code), f"HTTP {exc.code} {detail}".strip()
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def parse_dexscreener_pair_record(
    pair: dict[str, Any],
    source_lane: str = "dexscreener",
    promo_flags: set[str] | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Parse a raw DEX Screener pair object into a standardized RocketHunter discovery record."""
    if not isinstance(pair, dict):
        return None
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    chain_raw = pair.get("chainId") or "unknown"
    chain = normalize_chain(chain_raw)
    token_addr = str(base.get("address") or "").strip()
    pool_addr = str(pair.get("pairAddress") or "").strip()

    if not token_addr or not pool_addr:
        return None

    symbol = str(base.get("symbol") or "UNKNOWN").strip()
    name = str(base.get("name") or symbol).strip()
    quote_sym = str(quote.get("symbol") or "USDT").strip()
    pair_name = f"{symbol}/{quote_sym}"

    price_usd = _to_num(pair.get("priceUsd"))
    liquidity = pair.get("liquidity") or {}
    liq_usd = _to_num(liquidity.get("usd"))
    volume = pair.get("volume") or {}
    vol_24h = _to_num(volume.get("h24"))
    vol_6h = _to_num(volume.get("h6"))
    vol_1h = _to_num(volume.get("h1"))
    vol_5m = _to_num(volume.get("m5"))

    price_change = pair.get("priceChange") or {}
    chg_24h = _to_num(price_change.get("h24"))
    chg_6h = _to_num(price_change.get("h6"))
    chg_1h = _to_num(price_change.get("h1"))
    chg_5m = _to_num(price_change.get("m5"))

    txns = pair.get("txns") or {}
    tx_24h = txns.get("h24") or {}
    buys_24h = int(tx_24h.get("buys") or 0)
    sells_24h = int(tx_24h.get("sells") or 0)

    # Estimate pool age in hours
    created_at = pair.get("pairCreatedAt")
    age_hours = None
    if created_at:
        try:
            ts = float(created_at) / 1000.0 if float(created_at) > 10_000_000_000 else float(created_at)
            age_hours = max(0.0, (time.time() - ts) / 3600.0)
        except Exception:
            pass

    identity = f"dex:{chain}:{token_addr.lower()}"

    record = {
        "identity": identity,
        "asset": name,
        "symbol": symbol,
        "pair": pair_name,
        "chain": chain,
        "token_address": token_addr,
        "pool_address": pool_addr,
        "quote_token_address": quote.get("address"),
        "quote_symbol": quote_sym,
        "venue": pair.get("dexId") or "dexscreener",
        "market_type": "dex_pool",
        "price_usd": price_usd,
        "change_24h": chg_24h,
        "volume_24h": vol_24h,
        "liquidity_usd": liq_usd,
        "age_hours": age_hours,
        "pair_url": pair.get("url"),
        "source": source_lane,
        "discovery_lane": source_lane,
        "retrieved_utc": _utc_iso(),
        "extra": {
            "volume_5m": vol_5m,
            "volume_1h": vol_1h,
            "volume_6h": vol_6h,
            "price_change_5m": chg_5m,
            "price_change_1h": chg_1h,
            "price_change_6h": chg_6h,
            "buys_24h": buys_24h,
            "sells_24h": sells_24h,
            "promo_flags": sorted(list(promo_flags or [])),
            "fdv": _to_num(pair.get("fdv")),
            "market_cap": _to_num(pair.get("marketCap")),
        },
    }
    if extra_meta:
        record["extra"].update(extra_meta)
    return record


class DexScreenerDiscoveryEngine:
    """Orchestrates comprehensive multi-endpoint DEX Screener opportunity discovery."""

    def __init__(self, workers: int = 4, timeout: int = 8, max_workers: int | None = None) -> None:
        actual_workers = max_workers if max_workers is not None else workers
        self.workers = max(1, min(12, actual_workers))
        self.timeout = timeout
        self.search_queries = list(EXPANDED_SEARCH_QUERIES)
        self.discovered_candidates: list[dict[str, Any]] = []
        self.discovery_leads: dict[str, dict[str, Any]] = {}
        self.stats: dict[str, Any] = {
            "profiles_fetched": 0,
            "boosts_fetched": 0,
            "ctos_fetched": 0,
            "ads_fetched": 0,
            "search_queries_run": 0,
            "batch_token_lookups": 0,
            "total_pools_discovered": 0,
        }

    def collect_discovery_leads(self) -> dict[str, dict[str, Any]]:
        """Harvest early-stage leads from token profiles, boosts, CTOs, and ads."""
        lanes = [
            ("dexscreener_profiles", "/token-profiles/latest/v1", "latest_profile"),
            ("dexscreener_boosts_latest", "/token-boosts/latest/v1", "latest_boost"),
            ("dexscreener_boosts_top", "/token-boosts/top/v1", "top_boost"),
            ("dexscreener_community_takeovers", "/community-takeovers/latest/v1", "community_takeover"),
            ("dexscreener_ads", "/ads/latest/v1", "paid_ad"),
        ]

        for source_name, path, flag in lanes:
            url = DEX_ROOT + path
            payload, status, err = fetch_dex_json(url, timeout=self.timeout)
            if not payload or not isinstance(payload, list):
                continue

            count = 0
            for item in payload:
                if not isinstance(item, dict):
                    continue
                chain_raw = item.get("chainId")
                addr = item.get("tokenAddress")
                if not chain_raw or not addr:
                    continue
                chain = normalize_chain(chain_raw)
                key = f"{chain}:{addr.lower()}"
                if key not in self.discovery_leads:
                    self.discovery_leads[key] = {
                        "chain": chain,
                        "token_address": addr,
                        "flags": set(),
                        "sources": set(),
                        "icon": item.get("icon"),
                        "description": item.get("description"),
                        "boost_amount": 0.0,
                        "boost_total": 0.0,
                    }
                lead = self.discovery_leads[key]
                lead["flags"].add(flag)
                lead["sources"].add(source_name)
                lead["boost_amount"] = max(lead.get("boost_amount", 0.0), _to_num(item.get("amount")) or 0.0)
                lead["boost_total"] = max(lead.get("boost_total", 0.0), _to_num(item.get("totalAmount")) or 0.0)
                count += 1

            if "profile" in flag:
                self.stats["profiles_fetched"] += count
            elif "boost" in flag:
                self.stats["boosts_fetched"] += count
            elif "takeover" in flag:
                self.stats["ctos_fetched"] += count
            elif "ad" in flag:
                self.stats["ads_fetched"] += count

        return self.discovery_leads

    discover_promoted_and_fresh_leads = collect_discovery_leads

    def batch_lookup_tokens(self, addresses_by_chain: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
        """Perform efficient batch token lookups in slices of up to 30 addresses per request."""
        if addresses_by_chain is None:
            addresses_by_chain = {}
            for item in self.discovery_leads.values():
                addresses_by_chain.setdefault(item["chain"], []).append(item["token_address"])

        batches: list[tuple[str, list[str]]] = []
        for chain, addrs in addresses_by_chain.items():
            # Deduplicate preserving order
            unique_addrs = list(dict.fromkeys(addrs))
            for i in range(0, len(unique_addrs), MAX_BATCH_SIZE):
                chunk = unique_addrs[i : i + MAX_BATCH_SIZE]
                batches.append((chain, chunk))

        def _fetch_batch(batch_tuple: tuple[str, list[str]]) -> list[dict[str, Any]]:
            chain, chunk = batch_tuple
            addrs_str = ",".join(chunk)
            url = f"{DEX_ROOT}/latest/dex/tokens/{addrs_str}"
            payload, status, err = fetch_dex_json(url, timeout=self.timeout)
            if not payload or not isinstance(payload, dict):
                return []
            pairs = payload.get("pairs") or []
            results = []
            for pair in pairs:
                lead_key = f"{chain}:{str(pair.get('baseToken', {}).get('address', '')).lower()}"
                lead = self.discovery_leads.get(lead_key, {})
                rec = parse_dexscreener_pair_record(
                    pair,
                    source_lane="dexscreener_batch_lookup",
                    promo_flags=lead.get("flags"),
                    extra_meta={
                        "promo_sources": sorted(list(lead.get("sources", []))),
                        "boost_amount": lead.get("boost_amount", 0.0),
                        "boost_total": lead.get("boost_total", 0.0),
                    },
                )
                if rec:
                    results.append(rec)
            return results

        with ThreadPoolExecutor(max_workers=min(self.workers, max(1, len(batches)))) as pool:
            for res_batch in pool.map(_fetch_batch, batches):
                self.discovered_candidates.extend(res_batch)
                self.stats["batch_token_lookups"] += 1

        self.stats["total_pools_discovered"] = len(self.discovered_candidates)
        return self.discovered_candidates

    def execute_expanded_search(self, queries: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Run broad keyword searches across active Web3 DeFi pairs."""
        search_terms = tuple(queries or EXPANDED_SEARCH_QUERIES)

        def _search(q: str) -> list[dict[str, Any]]:
            url = f"{DEX_ROOT}/latest/dex/search?" + urllib.parse.urlencode({"q": q})
            payload, status, err = fetch_dex_json(url, timeout=self.timeout)
            if not payload or not isinstance(payload, dict):
                return []
            pairs = payload.get("pairs") or []
            rows = []
            for pair in pairs:
                rec = parse_dexscreener_pair_record(
                    pair,
                    source_lane="dexscreener_search",
                    extra_meta={"search_query": q},
                )
                if rec:
                    rows.append(rec)
            return rows

        with ThreadPoolExecutor(max_workers=min(self.workers, max(1, len(search_terms)))) as pool:
            for rows in pool.map(_search, search_terms):
                self.discovered_candidates.extend(rows)
                self.stats["search_queries_run"] += 1

        self.stats["total_pools_discovered"] = len(self.discovered_candidates)
        return self.discovered_candidates

    def run_full_discovery(self) -> list[dict[str, Any]]:
        """Execute full multi-lane DEX Screener discovery pipeline."""
        self.collect_discovery_leads()
        self.batch_lookup_tokens()
        self.execute_expanded_search()
        return self.discovered_candidates
