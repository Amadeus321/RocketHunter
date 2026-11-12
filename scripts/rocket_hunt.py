#!/usr/bin/env python3
"""Broad blockchain/Web3 rocket discovery across CEX, DEX and aggregators.

Read-only discovery. The raw-mover boards are sorted by observed return and do
not discard thin/new assets; tradeability and security are separate labels.
The script cannot enumerate every token on every chain. It reports exact
source coverage and never calls a partial scan a complete global universe.
"""
import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import venues  # noqa: E402

VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()
DEFAULT_VENUES = ("binance", "bybit", "okx", "mexc", "bitget", "gate", "kucoin")
# GeckoTerminal slugs are API identifiers, not chain names. These 24 IDs are
# drawn from the live /networks registry; the scan validates them dynamically
# before requesting per-network pools.
DEFAULT_NETWORKS = ("solana", "base", "bsc", "eth", "avax", "sui-network", "ton",
                    "arbitrum", "polygon_pos", "optimism", "aptos", "ftm",
                    "cro", "linea", "scroll", "zksync", "mantle", "blast",
                    "mode", "sei-network", "opbnb", "shibarium",
                    "hedera-hashgraph", "filecoin")
GECKO_NETWORK_ALIASES = {"avalanche": "avax", "sui": "sui-network", "fantom": "ftm",
                         "cronos": "cro", "sei": "sei-network"}
GECKO_NETWORK_REGISTRY_PAGES = 3
DEFAULT_DEX_SEARCH_QUERIES = ("meme", "solana", "base", "pump", "AI", "DeFi",
                               "gaming", "RWA", "DePIN", "launch", "new token", "web3",
                               "ethereum", "bsc", "arbitrum", "polygon", "avalanche", "optimism",
                               "sui", "ton", "tron", "blast", "linea", "fantom", "mantle", "zksync",
                               "aptos", "scroll", "sei", "mode", "pulsechain", "berachain", "hyperliquid", "monad")
DEX_ROOT = "https://api.dexscreener.com"
GECKO_ROOT = "https://api.geckoterminal.com/api/v2"
COINGECKO_ROOT = "https://api.coingecko.com/api/v3"


def utc_now():
    return datetime.now(timezone.utc)


def utc_iso():
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def n(value):
    try:
        if value is None or value == "":
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def first_num(*values):
    for value in values:
        x = n(value)
        if x is not None:
            return x
    return None


def _items(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "result", "coins", "markets", "tickers", "pairs"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _pct_map(container, *keys):
    if not isinstance(container, dict):
        return None
    for key in keys:
        if key in container:
            return n(container.get(key))
    return None


def _canonical_chain_address(chain, address):
    value = str(address or "").strip()
    evm = {"ethereum", "eth", "base", "bsc", "bnb", "polygon", "polygon_pos",
           "arbitrum", "optimism", "avalanche", "avax", "fantom", "ftm",
           "linea", "scroll", "zksync", "mantle", "blast", "mode", "opbnb"}
    if str(chain or "").lower() in evm and re.fullmatch(r"0x[0-9a-fA-F]+", value):
        return value.lower()
    return value


def _age_hours(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            ts = float(value)
            if ts > 10_000_000_000:
                ts /= 1000.0
        else:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        return max(0.0, (time.time() - ts) / 3600.0)
    except Exception:
        return None


def _base_record(*, identity, asset, symbol, source, market_type,
                 change_24h=None, change_6h=None, change_1h=None, change_5m=None,
                 price_usd=None, volume_24h_usd=None, volume_6h_usd=None,
                 volume_1h_usd=None, liquidity_usd=None, market_cap_usd=None,
                 fdv_usd=None, chain=None, address=None, venue=None, pair=None,
                 pair_url=None, age_hours=None, txns_24h=None, buys_24h=None,
                 sells_24h=None, promo_flags=None, extra=None, retrieved_utc=None):
    row = {
        "identity": identity, "asset": asset or symbol or "UNKNOWN",
        "symbol": (symbol or "").upper(), "source": source,
        "market_type": market_type, "spot_available": market_type == "cex_spot",
        "dex_available": market_type == "dex_pool", "chain": chain,
        "contract_address": address, "venue": venue, "pair": pair, "pair_url": pair_url,
        "price_usd": price_usd, "change_5m_pct": change_5m,
        "change_1h_pct": change_1h, "change_6h_pct": change_6h,
        "change_24h_pct": change_24h, "volume_1h_usd": volume_1h_usd,
        "volume_6h_usd": volume_6h_usd, "volume_24h_usd": volume_24h_usd,
        "liquidity_usd": liquidity_usd, "market_cap_usd": market_cap_usd,
        "fdv_usd": fdv_usd, "age_hours": age_hours,
        "txns_24h": txns_24h, "buys_24h": buys_24h, "sells_24h": sells_24h,
        "promo_flags": sorted(set(promo_flags or [])),
        "retrieved_utc": retrieved_utc or utc_iso(), "rank_score": None,
        "risk_tier": None, "risk_flags": []
    }
    if isinstance(extra, dict):
        row.update(extra)
    return row


def parse_gecko_pool(pool, included_by_id, source, retrieved_utc):
    attrs = pool.get("attributes") or {}
    rel = pool.get("relationships") or {}
    base_data = ((rel.get("base_token") or {}).get("data") or {})
    quote_data = ((rel.get("quote_token") or {}).get("data") or {})
    base_token = included_by_id.get(base_data.get("id"), {})
    quote_token = included_by_id.get(quote_data.get("id"), {})
    ba = base_token.get("attributes") or {}
    qa = quote_token.get("attributes") or {}
    pool_id = str(pool.get("id") or "")
    network = str(pool_id.split("_", 1)[0] or attrs.get("network") or "unknown")
    base_id = str(base_data.get("id") or "")
    address = ba.get("address") or base_id.split("_", 1)[-1] or None
    symbol = ba.get("symbol")
    name = ba.get("name")
    change = attrs.get("price_change_percentage") or {}
    volume = attrs.get("volume_usd") or {}
    txns = attrs.get("transactions") or {}
    h24tx = txns.get("h24") or {}
    age = _age_hours(attrs.get("pool_created_at"))
    pool_address = attrs.get("address") or pool_id.split("_", 1)[-1]
    pool_name = attrs.get("name") or ""
    liquidity = first_num(attrs.get("reserve_in_usd"), attrs.get("total_reserve_in_usd"))
    volume24 = first_num(volume.get("h24"))
    return _base_record(
        identity="dex:%s:%s" % (network.lower(), _canonical_chain_address(network, address or pool_address)),
        asset=name or pool_name or symbol, symbol=symbol or (pool_name.split("/")[0].strip() if pool_name else ""),
        source=source, market_type="dex_pool", chain=network, address=address,
        pair=pool_name, pair_url=("https://www.geckoterminal.com/%s/pools/%s" % (network, pool_address)) if pool_address else None,
        price_usd=first_num(attrs.get("base_token_price_usd")),
        change_5m=_pct_map(change, "m5", "5m"), change_1h=_pct_map(change, "h1", "1h"),
        change_6h=_pct_map(change, "h6", "6h"), change_24h=_pct_map(change, "h24", "24h"),
        volume_1h_usd=first_num(volume.get("h1")), volume_6h_usd=first_num(volume.get("h6")),
        volume_24h_usd=volume24, liquidity_usd=liquidity,
        market_cap_usd=first_num(attrs.get("market_cap_usd"), attrs.get("market_cap")),
        fdv_usd=n(attrs.get("fdv_usd")), age_hours=age,
        txns_24h=(n(h24tx.get("buys")) or 0) + (n(h24tx.get("sells")) or 0),
        buys_24h=n(h24tx.get("buys")), sells_24h=n(h24tx.get("sells")),
        retrieved_utc=retrieved_utc,
        extra={"pool_address": pool_address, "quote_symbol": qa.get("symbol"),
               "pool_created_at": attrs.get("pool_created_at"), "gecko_pool_id": pool.get("id")}
    )


def parse_dexscreener_pair(pair, chain, address, flags, retrieved_utc):
    base = pair.get("baseToken") or {}
    quote_token = pair.get("quoteToken") or {}
    if _canonical_chain_address(chain, base.get("address")) != _canonical_chain_address(chain, address):
        return None
    pc = pair.get("priceChange") or {}
    vol = pair.get("volume") or {}
    liq = pair.get("liquidity") or {}
    txn = pair.get("txns") or {}
    h24txn = txn.get("h24") or {}
    age = _age_hours(pair.get("pairCreatedAt"))
    pair_url = pair.get("url")
    pair_id = pair.get("pairAddress")
    return _base_record(
        identity="dex:%s:%s" % (chain.lower(), _canonical_chain_address(chain, address)),
        asset=base.get("name"), symbol=base.get("symbol"), source="dexscreener",
        market_type="dex_pool", chain=chain, address=address,
        venue=pair.get("dexId"), pair="%s/%s" % (base.get("symbol", "?"), quote_token.get("symbol", "?")),
        pair_url=pair_url, price_usd=n(pair.get("priceUsd")),
        change_5m=_pct_map(pc, "m5"), change_1h=_pct_map(pc, "h1"),
        change_6h=_pct_map(pc, "h6"), change_24h=_pct_map(pc, "h24"),
        volume_1h_usd=n(vol.get("h1")), volume_6h_usd=n(vol.get("h6")),
        volume_24h_usd=n(vol.get("h24")), liquidity_usd=n(liq.get("usd")),
        market_cap_usd=n(pair.get("marketCap")), fdv_usd=n(pair.get("fdv")),
        age_hours=age, txns_24h=(n(h24txn.get("buys")) or 0)+(n(h24txn.get("sells")) or 0),
        buys_24h=n(h24txn.get("buys")), sells_24h=n(h24txn.get("sells")),
        promo_flags=flags, retrieved_utc=retrieved_utc,
        extra={"pool_address": pair_id, "quote_symbol": quote_token.get("symbol"),
               "pair_created_at_ms": pair.get("pairCreatedAt"),
               "dex_boosts_active": n((pair.get("boosts") or {}).get("active"))}
    )


def score_candidate(row):
    """Transparent prioritisation heuristic; deliberately not a probability."""
    p24 = max(0.0, n(row.get("change_24h_pct")) or 0.0)
    p6 = max(0.0, n(row.get("change_6h_pct")) or 0.0)
    p1 = max(0.0, n(row.get("change_1h_pct")) or 0.0)
    vol = max(0.0, n(row.get("volume_24h_usd")) or 0.0)
    liq = max(0.0, n(row.get("liquidity_usd")) or 0.0)
    txns = max(0.0, n(row.get("txns_24h")) or 0.0)
    momentum = min(p24, 1000.0) / 1000.0 * 32
    momentum += min(p6, 300.0) / 300.0 * 17
    momentum += min(p1, 150.0) / 150.0 * 14
    volume_score = min(math.log10(vol + 1) / 8.0, 1.0) * 12
    liquidity_score = min(math.log10(liq + 1) / 7.0, 1.0) * 14
    activity_score = min(math.log10(txns + 1) / 5.0, 1.0) * 6
    source_score = min(len(row.get("corroborating_sources") or [row.get("source")]) / 3.0, 1.0) * 5
    penalty = 0.0
    if any(flag in (row.get("promo_flags") or []) for flag in ("paid_boost", "paid_ad")):
        penalty += 5.0
    if liq and liq < 10_000:
        penalty += 8.0
    elif liq and liq < 30_000:
        penalty += 4.0
    if row.get("age_hours") is not None and row["age_hours"] < 2:
        penalty += 3.0
    if p24 >= 500 and p1 is not None and p1 < 0:
        penalty += 4.0
    return round(max(0.0, min(100.0, momentum + volume_score + liquidity_score + activity_score + source_score - penalty)), 2)


def set_risk_tier(row):
    liq = n(row.get("liquidity_usd"))
    qv = n(row.get("volume_24h_usd"))
    age = n(row.get("age_hours"))
    flags = list(row.get("risk_flags") or [])
    promo = set(row.get("promo_flags") or [])
    if promo & {"paid_boost", "paid_ad"}:
        flags.append("PAID_PROMOTION_SIGNAL — not independent demand proof")
    if "community_takeover" in promo:
        flags.append("COMMUNITY_TAKEOVER_LEAD — independently verify")
    if age is not None and age < 24:
        flags.append("NEW_POOL_UNDER_24H")
    elif age is not None and age < 24 * 14:
        flags.append("NEW_POOL_UNDER_14D")
    if liq is not None:
        if liq < 5_000:
            tier = "DUST / EXTREME EXECUTION RISK"
            flags.append("LIQUIDITY_UNDER_5000_USD")
        elif liq < 25_000:
            tier = "ULTRA-THIN MICROCAP"
            flags.append("LIQUIDITY_UNDER_25000_USD")
        elif liq < 100_000:
            tier = "HIGH-RISK MICROCAP"
            flags.append("LIQUIDITY_UNDER_100000_USD")
        else:
            tier = "LIQUIDITY SCREEN PASSED — deeper checks required"
    elif row.get("market_type") == "cex_spot":
        tier = "CEX SPOT — execution check required"
    else:
        tier = "LIQUIDITY UNVERIFIED"
        flags.append("NO_LIQUIDITY_DATA")
    if qv is not None and liq is not None and qv > max(liq * 50, 1_000_000):
        flags.append("VOLUME_TO_LIQUIDITY_EXTREME — verify wash/turnover quality")
    change24 = n(row.get("change_24h_pct"))
    change1 = n(row.get("change_1h_pct"))
    txns = n(row.get("txns_24h"))
    buys = n(row.get("buys_24h"))
    sells = n(row.get("sells_24h"))
    if change24 is not None and abs(change24) >= 10_000:
        flags.append("EXTREME_24H_RETURN_OVER_10000_PERCENT — independently verify price series")
    if age is not None and age > 180 * 24 and change24 is not None and abs(change24) >= 500:
        flags.append("OLD_POOL_WITH_EXTREME_REPORTED_RETURN — verify update freshness")
    if change24 is not None and abs(change24) >= 100 and txns is not None and txns < 10:
        flags.append("LOW_TXN_COUNT_FOR_REPORTED_MOVE")
    if change24 is not None and change24 >= 100 and buys is not None and buys >= 10 and (sells or 0) == 0:
        flags.append("NO_RECORDED_SELLS_DURING_REPORTED_MOVE")
    if change24 is not None and change24 >= 100 and change1 is not None and change1 <= -50:
        flags.append("SEVERE_1H_REVERSAL_AFTER_24H_SPIKE")
    row["risk_tier"] = tier
    row["risk_flags"] = sorted(set(flags))
    row["rank_score"] = score_candidate(row)
    return row


def quality_gate(row):
    """Conservative follow-up screen; deliberately does not imply execution readiness."""
    reasons = []
    flags = set(row.get("risk_flags") or [])
    hard_quality_flags = {
        "VOLUME_TO_LIQUIDITY_EXTREME — verify wash/turnover quality",
        "OLD_POOL_WITH_EXTREME_REPORTED_RETURN — verify update freshness",
        "LOW_TXN_COUNT_FOR_REPORTED_MOVE",
        "NO_RECORDED_SELLS_DURING_REPORTED_MOVE",
        "SEVERE_1H_REVERSAL_AFTER_24H_SPIKE",
        "EXTREME_24H_RETURN_OVER_10000_PERCENT — independently verify price series",
    }
    if row.get("market_type") == "dex_pool":
        liq, vol = n(row.get("liquidity_usd")), n(row.get("volume_24h_usd"))
        age, txns = n(row.get("age_hours")), n(row.get("txns_24h"))
        change = n(row.get("change_24h_pct"))
        if liq is None or liq < 100_000: reasons.append("DEX liquidity below $100k or unavailable")
        if vol is None or vol < 250_000: reasons.append("DEX 24h volume below $250k or unavailable")
        if age is None or age > 365 * 24: reasons.append("Pool age missing or older than 365 days")
        if txns is None or txns < 25: reasons.append("Fewer than 25 reported 24h transactions")
        if change is None or change <= 0 or change > 2_500: reasons.append("24h return outside 0% to 2,500% screen")
        if flags & hard_quality_flags: reasons.extend(sorted(flags & hard_quality_flags))
    elif row.get("market_type") == "cex_spot":
        vol = n(row.get("volume_24h_usd")); cross = int(n(row.get("cross_venue_count")) or 0)
        change = n(row.get("change_24h_pct"))
        if vol is None or vol < 1_000_000: reasons.append("CEX 24h quote volume below $1m or unavailable")
        if cross < 2: reasons.append("No independent second-venue price confirmation")
        if change is None or change < 2 or change > 300: reasons.append("24h return outside 2% to 300% screen")
        if "TICKER_COLLISION_OR_CROSS_VENUE_PRICE_DIVERGENCE" in flags:
            reasons.append("Cross-venue ticker collision or price divergence")
    else:
        reasons.append("Not a DEX pool or CEX spot ticker")
    return {"status": "FOLLOW_UP_SCREEN_PASSED_NOT_EXECUTION_READY" if not reasons else "NOT_SHORTLISTED",
            "reasons": reasons, "execution_ready": False}


class RocketHunter:
    def __init__(self, timeout=8, retries=2, workers=8, checkpoint_dir=None,
                 resume=False, resume_max_age_seconds=900, progress=True):
        self.fx = venues.Fetcher(timeout=timeout, retries=retries, pause=0.02)
        self.workers = workers
        self.source_status = []
        self.candidates = []
        self.discovery_leads = []
        self.web3_trending_leads = []
        # Public API limits fluctuate. 12 seconds keeps the sustained cadence
        # around 5 requests/minute, leaving headroom for dynamic registry reads.
        self.gecko_request_interval = 12.0
        self._gecko_last_request = 0.0
        self._gecko_lock = threading.Lock()
        self._checkpoint_lock = threading.RLock()
        self.checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else None
        self.resume = bool(resume)
        self.resume_max_age_seconds = max(0, int(resume_max_age_seconds))
        self.progress = bool(progress)
        if self.checkpoint_dir:
            (self.checkpoint_dir / "responses").mkdir(parents=True, exist_ok=True)

    def _cache_path(self, source, url):
        if not self.checkpoint_dir:
            return None
        key = hashlib.sha256((str(source) + "\0" + str(url)).encode("utf-8")).hexdigest()
        return self.checkpoint_dir / "responses" / (key + ".json")

    def _write_progress(self, last_source=None):
        if not self.checkpoint_dir:
            return
        with self._checkpoint_lock:
            manifest = {
                "skill_version": VERSION,
                "updated_utc": utc_iso(),
                "last_source": last_source,
                "source_count": len(self.source_status),
                "source_status": list(self.source_status),
                "successful_cached_responses": sum(1 for x in self.source_status if x.get("status") == "CACHED"),
            }
            target = self.checkpoint_dir / "progress.json"
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(str(tmp), str(target))

    def _record_source(self, row):
        with self._checkpoint_lock:
            self.source_status.append(row)
            self._write_progress(last_source=row.get("source"))
        if self.progress:
            state = row.get("status", "UNKNOWN")
            count = row.get("row_count", "—")
            suffix = " (cached)" if state == "CACHED" else ""
            print("[%s] %s: %s rows%s" % (state, row.get("source"), count, suffix), flush=True)

    def _fetch_cached(self, source, url):
        """Load a recent successful response on an explicit resume run."""
        path = self._cache_path(source, url)
        if not self.resume or not path or not path.exists():
            return False, None
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            retrieved = saved.get("retrieved_utc")
            age = _age_hours(retrieved)
            age_seconds = age * 3600 if age is not None else float("inf")
            if age_seconds > self.resume_max_age_seconds:
                return False, None
            payload = saved.get("payload")
            if payload is None:
                return False, None
            self._record_source({"source": source, "url": url, "status": "CACHED",
                                 "row_count": len(_items(payload)), "retrieved_utc": retrieved,
                                 "cache_age_seconds": round(age_seconds, 1), "error": None})
            self.fx.log.append({"method": "GET", "url": url, "ok": True, "status": None,
                                "error": None, "retrieved_utc": retrieved, "cached": True})
            return True, payload
        except (OSError, ValueError, TypeError):
            return False, None

    def _save_cached(self, source, url, payload, retrieved_utc):
        path = self._cache_path(source, url)
        if not path or payload is None:
            return
        with self._checkpoint_lock:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"source": source, "url": url,
                                       "retrieved_utc": retrieved_utc, "payload": payload},
                                      separators=(",", ":")), encoding="utf-8")
            os.replace(str(tmp), str(path))

    def _fetch_json(self, source, url):
        hit, cached_payload = self._fetch_cached(source, url)
        if hit:
            return cached_payload
        payload = self.fx.get(url)
        ok = payload is not None
        log_row = next((x for x in reversed(self.fx.log) if x.get("url") == url), {})
        retrieved = log_row.get("retrieved_utc", utc_iso())
        row = {"source": source, "url": url, "status": "OK" if ok else "FAILED",
               "row_count": len(_items(payload)) if ok else 0,
               "retrieved_utc": retrieved, "error": log_row.get("error")}
        self._record_source(row)
        if ok:
            self._save_cached(source, url, payload, retrieved)
        return payload

    def _fetch_gecko_json(self, source, url):
        """Serialize live GeckoTerminal requests to stay below its current ~30/min public-tier limit.

        Successful cached responses on --resume bypass pacing; only live calls
        consume the public API request budget.
        """
        hit, cached_payload = self._fetch_cached(source, url)
        if hit:
            return cached_payload
        with self._gecko_lock:
            wait = self.gecko_request_interval - (time.monotonic() - self._gecko_last_request)
            if self._gecko_last_request and wait > 0:
                time.sleep(wait)
            self._gecko_last_request = time.monotonic()
            return self._fetch_json(source, url)

    def cex_boards(self, venue_names):
        def one(name):
            venue = venues.VENUES[name]
            local_fx = venues.Fetcher(timeout=self.fx.timeout, retries=2, pause=0)
            rows, url = venue.tickers(local_fx)
            audit = getattr(venue, "last_ticker_audit", {}) or {}
            return name, rows, url, audit, local_fx.log
        with ThreadPoolExecutor(max_workers=min(self.workers, len(venue_names) or 1)) as pool:
            futures = [pool.submit(one, name) for name in venue_names]
            for fut in as_completed(futures):
                name, rows, url, audit, log = fut.result()
                ok = isinstance(rows, list)
                last_log = log[-1] if log else {}
                self.source_status.append({"source": "cex_spot:%s" % name, "url": url,
                                           "status": "OK" if ok else "FAILED",
                                           "http_status": last_log.get("status"),
                                           "error": None if ok else last_log.get("error"),
                                           "retrieved_utc": utc_iso(), "row_count": len(rows or []),
                                           "ticker_audit": audit, "request_log": log})
                if not ok:
                    continue
                for t in rows:
                    self.candidates.append(_base_record(
                        identity="cex:%s" % t["base"].upper(), asset=t["base"], symbol=t["base"],
                        source="cex:%s" % name, market_type="cex_spot", venue=name,
                        pair=t.get("pair"), price_usd=n(t.get("last")),
                        change_24h=n(t.get("change_pct")), volume_24h_usd=n(t.get("quote_vol")),
                        retrieved_utc=utc_iso(),
                        extra={"ticker_close_ms": t.get("ticker_close_ms"),
                               "ticker_age_ms": t.get("ticker_age_ms"),
                               "trade_count_24h": t.get("trade_count"), "asset_base": t.get("base")}
                    ))

    def discover_gecko_networks(self, preferred_networks=None, limit=24):
        """Validate preferred chain slugs against GeckoTerminal's live registry.

        The registry is authoritative for API slugs. If it is unavailable, retain
        the explicit preferred list but record the fallback and leave coverage
        partial rather than silently claiming the network list was verified.
        """
        preferred = [GECKO_NETWORK_ALIASES.get(str(x).lower(), str(x).lower())
                     for x in (preferred_networks or DEFAULT_NETWORKS)]
        supported_order = []
        registry_pages_ok = 0
        registry_errors = []
        for page in range(1, GECKO_NETWORK_REGISTRY_PAGES + 1):
            url = "%s/networks?%s" % (GECKO_ROOT, urlencode({"page": page}))
            payload = self._fetch_gecko_json("gecko_network_registry_page_%d" % page, url)
            items = _items(payload)
            ids = [str(x.get("id")) for x in items if isinstance(x, dict) and x.get("id")]
            if not ids:
                registry_errors.append("page %d returned no network identifiers" % page)
                continue
            registry_pages_ok += 1
            for network_id in ids:
                if network_id not in supported_order:
                    supported_order.append(network_id)
        if not supported_order:
            selected = list(dict.fromkeys(preferred))[:max(0, int(limit))]
            return selected, {"registry_complete": False, "registry_pages_ok": 0,
                              "registry_pages_requested": GECKO_NETWORK_REGISTRY_PAGES,
                              "supported_network_count": 0, "missing_preferred": [],
                              "fallback_used": True, "errors": registry_errors or ["registry unavailable"]}
        supported = set(supported_order)
        selected = [x for x in preferred if x in supported]
        missing = [x for x in preferred if x not in supported]
        # If one or more of the curated preferred chains have disappeared from
        # the provider's current registry, fill to the requested count using
        # other live network IDs (excluding testnets) rather than bad slugs.
        target = max(0, int(limit))
        if len(selected) < target:
            for network_id in supported_order:
                if network_id in selected or "testnet" in network_id.lower():
                    continue
                selected.append(network_id)
                if len(selected) >= target:
                    break
        selected = selected[:target]
        return selected, {"registry_complete": registry_pages_ok == GECKO_NETWORK_REGISTRY_PAGES,
                          "registry_pages_ok": registry_pages_ok,
                          "registry_pages_requested": GECKO_NETWORK_REGISTRY_PAGES,
                          "supported_network_count": len(supported_order),
                          "missing_preferred": missing, "fallback_used": False,
                          "errors": registry_errors, "selection_source": "live_geckoterminal_network_registry"}

    def gecko_global(self, pages=3, network_names=None, per_network=0):
        base_routes = []
        # Multi-timeframe global trending and global new-pool pagination are
        # important: page 1 alone can hide extreme movers in broad universes.
        for page in range(1, max(1, min(10, pages)) + 1):
            for duration in ("1h", "6h", "24h"):
                base_routes.append(("gecko_global_trending_%s_page_%s" % (duration, page),
                                    "%s/networks/trending_pools?%s" % (GECKO_ROOT, urlencode({"page":page,"duration":duration,"include":"base_token,quote_token,dex"}))))
            base_routes.append(("gecko_global_new_pools_page_%s" % page,
                                "%s/networks/new_pools?%s" % (GECKO_ROOT, urlencode({"page":page,"include":"base_token,quote_token,dex"}))))
        routes = list({url: (name, url) for name, url in base_routes}.values())
        if network_names and per_network > 0:
            for network in network_names:
                for endpoint in ("trending_pools", "new_pools", "pools"):
                    query = {"page": 1, "include": "base_token,quote_token,dex"}
                    if endpoint == "trending_pools":
                        query["duration"] = "24h"
                    elif endpoint == "pools":
                        query["sort"] = "h24_volume_usd_desc"
                    routes.append(("gecko:%s:%s" % (network, endpoint),
                                   "%s/networks/%s/%s?%s" % (GECKO_ROOT, quote(network, safe=""), endpoint, urlencode(query))))
        # Bound request count; global pages + top-volume/trending/new-pool network sweep.
        routes = routes[:max(4, len(base_routes) + (len(network_names or []) * 3 if per_network else 0))]
        def request(item):
            name, url = item
            return name, url, self._fetch_gecko_json(name, url)
        included_by_id = {}
        batches = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for result in pool.map(request, routes):
                batches.append(result)
        for source, url, payload in batches:
            if not payload:
                continue
            included = payload.get("included", []) if isinstance(payload, dict) else []
            included_by_id.update({x.get("id"): x for x in included if isinstance(x, dict) and x.get("id")})
        for source, url, payload in batches:
            if not payload:
                continue
            rows = _items(payload)
            stamp = utc_iso()
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("attributes"), dict):
                    continue
                candidate = parse_gecko_pool(row, included_by_id, source, stamp)
                candidate["source_url"] = url
                candidate["discovery_lane"] = ("new_pool" if "new_pools" in source else
                                               "top_volume_pool" if source.endswith(":pools") else "trending_pool")
                self.candidates.append(candidate)

    def dexscreener_search(self, queries=None):
        """Search broad pair/category terms to augment profile and promotion leads."""
        queries = tuple(queries or getattr(self, "dex_search_queries", DEFAULT_DEX_SEARCH_QUERIES))
        def request(query):
            url = DEX_ROOT + "/latest/dex/search?" + urlencode({"q": query})
            payload = self._fetch_json("dexscreener_search:%s" % query, url)
            rows = []
            for pair in _items(payload):
                base = pair.get("baseToken") or {}
                chain = pair.get("chainId")
                address = base.get("address")
                if not chain or not address:
                    continue
                row = parse_dexscreener_pair(pair, chain, address, set(), utc_iso())
                if row:
                    row["source"] = "dexscreener_search"
                    row["discovery_lane"] = "broad_pair_search"
                    row["source_url"] = url
                    row["search_query"] = query
                    rows.append(row)
            return rows
        with ThreadPoolExecutor(max_workers=min(self.workers, max(1, len(queries)))) as pool:
            for batch in pool.map(request, queries):
                self.candidates.extend(batch)

    def dexscreener_discovery(self, max_token_lookups=80):
        paths = [
            ("dexscreener_profiles", "/token-profiles/latest/v1", "latest_profile_lead"),
            ("dexscreener_boosts_latest", "/token-boosts/latest/v1", "paid_boost"),
            ("dexscreener_boosts_top", "/token-boosts/top/v1", "paid_boost"),
            ("dexscreener_community_takeovers", "/community-takeovers/latest/v1", "community_takeover"),
            ("dexscreener_ads", "/ads/latest/v1", "paid_ad")
        ]
        leads = {}
        # Multi-lane DEX Screener Discovery integration
        try:
            from scripts.dexscreener_discovery import DexScreenerDiscoveryEngine
            ds_engine = DexScreenerDiscoveryEngine(timeout=self.timeout, max_workers=self.workers)
            full_candidates = ds_engine.run_full_discovery()
            if full_candidates:
                existing_keys = {(str(c.get("chain", "")).lower(), str(c.get("pool_address", "")).lower()) for c in self.candidates}
                for c in full_candidates:
                    ckey = (str(c.get("chain", "")).lower(), str(c.get("pool_address", "")).lower())
                    if ckey not in existing_keys:
                        self.candidates.append(c)
                        existing_keys.add(ckey)
                self.discovery_leads = [
                    {"chain": x["chain"], "address": x["token_address"],
                     "sources": sorted(list(x.get("sources", []))), "promo_flags": sorted(list(x.get("flags", []))),
                     "boost_amount": x.get("boost_amount"), "boost_total": x.get("boost_total")}
                    for x in ds_engine.discovery_leads.values()
                ]
                self.dexscreener_stats = dict(ds_engine.stats)
                return
        except Exception:
            pass

        for source, path, flag in paths:
            url = DEX_ROOT + path
            payload = self._fetch_json(source, url)
            for row in _items(payload):
                chain = row.get("chainId")
                address = row.get("tokenAddress")
                if not chain or not address:
                    continue
                key = (str(chain).lower(), str(address).lower())
                leads.setdefault(key, {"chain": chain, "address": address, "flags": set(), "sources": set()})
                leads[key]["flags"].add(flag)
                leads[key]["sources"].add(source)
                leads[key]["boost_amount"] = max(n(leads[key].get("boost_amount")) or 0, n(row.get("amount")) or 0)
                leads[key]["boost_total"] = max(n(leads[key].get("boost_total")) or 0, n(row.get("totalAmount")) or 0)
        self.discovery_leads = [{"chain": x["chain"], "address": x["address"],
                                 "sources": sorted(x["sources"]), "promo_flags": sorted(x["flags"]),
                                 "boost_amount": x.get("boost_amount"), "boost_total": x.get("boost_total")}
                                for x in leads.values()]
        leads_list = list(leads.values())[:max_token_lookups]
        def lookup_single(item):
            chain, address = item["chain"], item["address"]
            url = "%s/token-pairs/v1/%s/%s" % (DEX_ROOT, quote(str(chain), safe=""), quote(str(address), safe=""))
            payload = self._fetch_json("dexscreener_token_pairs", url)
            pairs = _items(payload)
            candidates = []
            for pair in pairs:
                row = parse_dexscreener_pair(pair, chain, address, item["flags"], utc_iso())
                if row:
                    row["source_url"] = url
                    row["promo_sources"] = sorted(item["sources"])
                    row["boost_amount"] = item.get("boost_amount")
                    row["boost_total"] = item.get("boost_total")
                    candidates.append(row)
            return candidates

        # Batch lookup in slices of up to 30 addresses for high throughput and rate-limit safety
        addresses = [x["address"] for x in leads_list if x.get("address")]
        chunks = [addresses[i : i + 30] for i in range(0, len(addresses), 30)]

        def lookup_batch(chunk):
            addrs_str = ",".join(chunk)
            url = "%s/latest/dex/tokens/%s" % (DEX_ROOT, addrs_str)
            payload = self._fetch_json("dexscreener_batch_tokens", url)
            pairs = _items(payload)
            candidates = []
            matched_addrs = set()
            for pair in pairs:
                base = pair.get("baseToken") or {}
                addr = base.get("address")
                chain = pair.get("chainId")
                if not addr or not chain:
                    continue
                matched_addrs.add(str(addr).lower())
                lead = leads.get((str(chain).lower(), str(addr).lower()), {})
                row = parse_dexscreener_pair(pair, chain, addr, lead.get("flags", set()), utc_iso())
                if row:
                    row["source_url"] = url
                    row["promo_sources"] = sorted(lead.get("sources", []))
                    row["boost_amount"] = lead.get("boost_amount")
                    row["boost_total"] = lead.get("boost_total")
                    candidates.append(row)
            # Fall back to single lookup for missing tokens
            missing = [a for a in chunk if str(a).lower() not in matched_addrs]
            for a in missing:
                for item in leads_list:
                    if str(item.get("address", "")).lower() == str(a).lower():
                        candidates.extend(lookup_single(item))
                        break
            return candidates

        if chunks:
            with ThreadPoolExecutor(max_workers=min(self.workers, max(1, len(chunks)))) as pool:
                for batch in pool.map(lookup_batch, chunks):
                    self.candidates.extend(batch)
        else:
            with ThreadPoolExecutor(max_workers=min(self.workers, max(1, len(leads_list)))) as pool:
                for batch in pool.map(lookup_single, leads_list):
                    self.candidates.extend(batch)


    def coingecko_trending(self, max_coins=30):
        """Collect CoinGecko trending coins/NFTs/categories when an API key exists."""
        pro_key = os.environ.get("COINGECKO_API_KEY")
        demo_key = os.environ.get("COINGECKO_DEMO_API_KEY")
        api_key = pro_key or demo_key
        source = "coingecko_trending_leads"
        if not api_key:
            self.source_status.append({"source": source, "status": "SKIPPED",
                                       "reason": "optional COINGECKO_API_KEY / COINGECKO_DEMO_API_KEY not configured"})
            return
        import urllib.request
        endpoint = "https://pro-api.coingecko.com/api/v3/search/trending" if pro_key else "https://api.coingecko.com/api/v3/search/trending"
        key_header = "x-cg-pro-api-key" if pro_key else "x-cg-demo-api-key"
        req = urllib.request.Request(endpoint, headers={"User-Agent": venues.USER_AGENT,
                                                         "Accept": "application/json", key_header: api_key})
        try:
            with urllib.request.urlopen(req, timeout=self.fx.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.source_status.append({"source": source, "url": endpoint, "status": "OK",
                                       "retrieved_utc": utc_iso(), "row_count": len(payload.get("coins", [])) +
                                       len(payload.get("nfts", [])) + len(payload.get("categories", []))})
        except Exception as exc:
            self.source_status.append({"source": source, "url": endpoint, "status": "FAILED",
                                       "retrieved_utc": utc_iso(), "error": "%s: %s" % (type(exc).__name__, exc)})
            return

        existing = {row.get("identity") for row in self.candidates if str(row.get("identity", "")).startswith("coingecko:")}
        coin_leads = []
        for rank, row in enumerate(payload.get("coins", [])[:max_coins], 1):
            item = row.get("item") or {}
            coin_id = item.get("id")
            if not coin_id:
                continue
            detail = item.get("data") or {}
            change = detail.get("price_change_percentage_24h") or {}
            price_change = change.get("usd") if isinstance(change, dict) else None
            if "coingecko:%s" % coin_id not in existing:
                self.candidates.append(_base_record(
                    identity="coingecko:%s" % coin_id, asset=item.get("name"), symbol=item.get("symbol"),
                    source="coingecko_trending", market_type="aggregator_asset",
                    price_usd=n(detail.get("price")), change_24h=n(price_change),
                    volume_24h_usd=self._parse_usd(detail.get("total_volume")),
                    market_cap_usd=self._parse_usd(detail.get("market_cap")),
                    retrieved_utc=utc_iso(), extra={"coingecko_id": coin_id,
                    "market_cap_rank": item.get("market_cap_rank"), "trending_rank": rank,
                    "category": "global trending search", "discovery_lane": "aggregator_trending"}
                ))
                existing.add("coingecko:%s" % coin_id)
            coin_leads.append({"type": "coin", "rank": rank, "id": coin_id,
                               "name": item.get("name"), "symbol": item.get("symbol"),
                               "market_cap_rank": item.get("market_cap_rank"),
                               "price_change_24h_pct": n(price_change)})
        self.web3_trending_leads = coin_leads
        for rank, row in enumerate(payload.get("nfts", []), 1):
            detail = row.get("data") or {}
            self.web3_trending_leads.append({
                "type": "nft_collection", "rank": rank, "id": row.get("id"),
                "name": row.get("name"), "symbol": row.get("symbol"),
                "floor_price": row.get("floor_price_in_native_currency"),
                "native_currency": row.get("native_currency_symbol"),
                "floor_price_24h_change_pct": n(row.get("floor_price_24h_percentage_change")),
                "volume_24h_native": detail.get("h24_volume")
            })
        for rank, row in enumerate(payload.get("categories", []), 1):
            change = (row.get("data") or {}).get("market_cap_change_percentage_24h") or {}
            self.web3_trending_leads.append({
                "type": "category", "rank": rank, "id": row.get("id"),
                "name": row.get("name"), "slug": row.get("slug"),
                "coins_count": row.get("coins_count"),
                "market_cap_24h_change_pct": n(change.get("usd") if isinstance(change, dict) else None),
                "total_volume_usd": n((row.get("data") or {}).get("total_volume"))
            })

    @staticmethod
    def _parse_usd(value):
        if isinstance(value, str):
            value = value.replace("$", "").replace(",", "").strip()
        return n(value)

    def coingecko_markets(self, pages=2, per_page=250):
        for page in range(1, pages + 1):
            params = {"vs_currency":"usd", "order":"volume_desc", "per_page":per_page,
                      "page":page, "sparkline":"false", "price_change_percentage":"1h,24h,7d"}
            url = COINGECKO_ROOT + "/coins/markets?" + urlencode(params)
            payload = self._fetch_json("coingecko_markets_page_%s" % page, url)
            for row in _items(payload):
                self.candidates.append(_base_record(
                    identity="coingecko:%s" % row.get("id"), asset=row.get("name"), symbol=row.get("symbol"),
                    source="coingecko", market_type="aggregator_asset",
                    price_usd=n(row.get("current_price")), change_24h=n(row.get("price_change_percentage_24h")),
                    change_1h=n(row.get("price_change_percentage_1h_in_currency")),
                    change_6h=None, volume_24h_usd=n(row.get("total_volume")),
                    market_cap_usd=n(row.get("market_cap")), fdv_usd=n(row.get("fully_diluted_valuation")),
                    retrieved_utc=utc_iso(), extra={"coingecko_id":row.get("id"),
                    "market_cap_rank":row.get("market_cap_rank"), "circulating_supply":row.get("circulating_supply"),
                    "category":"listed fungible token"}
                ))

    def coinpaprika_markets(self, limit=5000):
        url = "https://api.coinpaprika.com/v1/tickers?" + urlencode({"quotes":"USD", "limit":limit})
        payload = self._fetch_json("coinpaprika_tickers", url)
        for row in _items(payload):
            usd = ((row.get("quotes") or {}).get("USD") or {})
            self.candidates.append(_base_record(
                identity="coinpaprika:%s" % row.get("id"), asset=row.get("name"), symbol=row.get("symbol"),
                source="coinpaprika", market_type="aggregator_asset", price_usd=n(usd.get("price")),
                change_24h=n(usd.get("percent_change_24h")), change_1h=n(usd.get("percent_change_1h")),
                change_6h=n(usd.get("percent_change_6h")), volume_24h_usd=n(usd.get("volume_24h")),
                market_cap_usd=n(usd.get("market_cap")), retrieved_utc=utc_iso(),
                extra={"coinpaprika_id":row.get("id"), "rank":row.get("rank"), "category":"listed fungible token"}
            ))

    def add_optional_birdeye(self, api_key, limit=100):
        if not api_key:
            self.source_status.append({"source":"birdeye_solana_trending", "status":"SKIPPED",
                                       "reason":"BIRDEYE_API_KEY not configured; optional paid/keyed source"})
            return
        url = "https://public-api.birdeye.so/defi/token_trending?" + urlencode({"sort_by":"rank", "sort_type":"asc", "offset":0, "limit":limit})
        headers_fx = venues.Fetcher(timeout=self.fx.timeout, retries=0, pause=0)
        # Birdeye requires a key and x-chain. Use a small dedicated request rather
        # than altering the generic no-key public-source fetcher.
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent":venues.USER_AGENT, "Accept":"application/json",
                                                   "X-API-KEY":api_key, "x-chain":"solana"})
        try:
            with urllib.request.urlopen(req, timeout=self.fx.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            self.source_status.append({"source":"birdeye_solana_trending", "status":"OK", "url":url,
                                       "retrieved_utc":utc_iso(), "row_count":len(_items(payload))})
            for row in _items(payload):
                self.candidates.append(_base_record(
                    identity="birdeye:solana:%s" % (row.get("address") or row.get("token_address")),
                    asset=row.get("name"), symbol=row.get("symbol"), source="birdeye", market_type="dex_token",
                    chain="solana", address=row.get("address") or row.get("token_address"),
                    price_usd=n(row.get("price")), change_24h=n(row.get("price_24h_change_percent")),
                    volume_24h_usd=n(row.get("volume_24h_usd")), market_cap_usd=n(row.get("market_cap")),
                    retrieved_utc=utc_iso(), extra={"category":"solana trending", "birdeye_rank":row.get("rank")}
                ))
        except Exception as exc:
            self.source_status.append({"source":"birdeye_solana_trending", "status":"FAILED", "url":url,
                                       "retrieved_utc":utc_iso(), "error":"%s: %s" % (type(exc).__name__, exc)})

    def run(self, venues_to_scan, include_coinpaprika=True, include_coingecko=True,
            include_dex=True, include_gecko=True, networks=None,
            gecko_network_sweep=None, dex_lookup_limit=160, coingecko_pages=2,
            include_birdeye=False, gecko_pages=3):
        # Freeze the actual launch timestamp before preflighted discovery work.
        # The forward forecast window must start here, not when report rendering ends.
        self.scan_start_utc = utc_iso()
        # Programmatic callers get the same broad defaults as the CLI: every
        # configured GeckoTerminal network is swept unless explicitly limited.
        networks = list(DEFAULT_NETWORKS if networks is None else networks)
        if gecko_network_sweep is None:
            gecko_network_sweep = len(networks)
        requested_networks = [GECKO_NETWORK_ALIASES.get(str(x).lower(), str(x).lower()) for x in networks]
        registry_audit = {"registry_complete": False, "registry_pages_ok": 0,
                          "registry_pages_requested": 0, "supported_network_count": 0,
                          "missing_preferred": [], "fallback_used": False}
        selected_networks = requested_networks
        if include_gecko and gecko_network_sweep:
            selected_networks, registry_audit = self.discover_gecko_networks(
                requested_networks, limit=gecko_network_sweep)
        # Public API calls include 3 live registry pages, global pages and the
        # three per-network pool families. Pacing estimate is conservative.
        gecko_global_calls = max(1, min(10, gecko_pages)) * 4 if include_gecko else 0
        gecko_network_calls = len(selected_networks) * 3 if include_gecko and gecko_network_sweep else 0
        gecko_registry_calls = registry_audit.get("registry_pages_ok", 0) if include_gecko else 0
        gecko_calls_planned = gecko_global_calls + gecko_network_calls + gecko_registry_calls
        self.discovery_config = {
            "gecko_pages": max(1, min(10, gecko_pages)),
            "gecko_networks_requested": list(requested_networks) if include_gecko else [],
            "gecko_networks_selected": list(selected_networks) if include_gecko else [],
            "gecko_network_registry": registry_audit,
            "gecko_request_interval_seconds": self.gecko_request_interval,
            "gecko_calls_planned": gecko_calls_planned,
            "gecko_pacing_seconds_estimate": max(0, gecko_calls_planned - 1) * self.gecko_request_interval,
        }
        # DEX-first is enforced in executable call order: all configured DEX
        # discovery families are attempted and logged before any CEX ticker.
        dex_first_sources = []
        if include_gecko:
            self.gecko_global(pages=gecko_pages, network_names=selected_networks, per_network=len(selected_networks))
            dex_first_sources.append("geckoterminal")
        if include_dex:
            self.dexscreener_discovery(max_token_lookups=dex_lookup_limit)
            self.dexscreener_search()
            dex_first_sources.append("dexscreener")
        if include_birdeye:
            self.add_optional_birdeye(os.environ.get("BIRDEYE_API_KEY"))
            dex_first_sources.append("birdeye_solana")

        # Secondary pass: CEX listings are discovered only after DEX discovery.
        self.cex_boards(venues_to_scan)
        if include_coingecko:
            self.coingecko_trending()
            self.coingecko_markets(pages=coingecko_pages)
        if include_coinpaprika:
            self.coinpaprika_markets()
        data = self.build_output()
        data["coverage"]["scan_order"] = dex_first_sources + ["cex_spot"] + (
            ["coingecko"] if include_coingecko else []) + (["coinpaprika"] if include_coinpaprika else [])
        data["coverage"]["dex_first_enforced"] = True
        data["coverage"]["discovery_config"] = getattr(self, "discovery_config", {})
        return data

    def build_output(self):
        # Coalesce CEX listings into one asset row while retaining venue-level data.
        cex_groups = {}
        others = []
        for row in self.candidates:
            if row.get("market_type") == "cex_spot":
                cex_groups.setdefault(row["identity"], []).append(row)
            else:
                others.append(row)
        cex_merged = []
        for identity, rows in cex_groups.items():
            # Ticker symbols are not globally unique. Merge venue rows only when
            # price and return agree; divergent clusters remain separate and get
            # a visible ticker-collision warning instead of a false consensus.
            clusters = []
            for row in sorted(rows, key=lambda x: n(x.get("volume_24h_usd")) or 0, reverse=True):
                price = n(row.get("price_usd")); change = n(row.get("change_24h_pct"))
                found = None
                for cluster in clusters:
                    ref_price = n(cluster[0].get("price_usd")); ref_change = n(cluster[0].get("change_24h_pct"))
                    price_ok = price is not None and ref_price is not None and abs(price / ref_price - 1) <= 0.03
                    change_ok = change is not None and ref_change is not None and abs(change - ref_change) <= 15.0
                    if price_ok and change_ok:
                        found = cluster; break
                if found is None:
                    clusters.append([row])
                else:
                    found.append(row)
            for cluster_index, cluster in enumerate(clusters, 1):
                cluster = sorted(cluster, key=lambda x: n(x.get("volume_24h_usd")) or 0, reverse=True)
                weights = [(n(x.get("change_24h_pct")), n(x.get("volume_24h_usd")) or 0) for x in cluster]
                denom = sum(w for _, w in weights)
                weighted = (sum((pct or 0) * weight for pct, weight in weights) / denom) if denom else n(cluster[0].get("change_24h_pct"))
                chosen = dict(cluster[0])
                chosen["change_24h_pct"] = weighted
                chosen["venue"] = cluster[0].get("venue")
                chosen["venues"] = [x.get("venue") for x in cluster]
                chosen["cross_venue_count"] = len(cluster)
                chosen["cross_venue_returns_pct"] = {x.get("venue"): x.get("change_24h_pct") for x in cluster}
                chosen["corroborating_sources"] = [x.get("source") for x in cluster]
                chosen["source"] = "cex_multi_venue" if len(cluster) > 1 else cluster[0].get("source")
                if len(clusters) > 1:
                    chosen["identity"] = "%s:ambiguous_cluster_%d" % (identity, cluster_index)
                    chosen.setdefault("risk_flags", []).append("TICKER_COLLISION_OR_CROSS_VENUE_PRICE_DIVERGENCE")
                if len(cluster) == 1:
                    chosen.setdefault("risk_flags", []).append("SINGLE_VENUE_ONLY — no cross-venue price confirmation")
                cex_merged.append(chosen)
        # DEX same-contract pools are aggregated into one token identity. Choose
        # the most useful pool by 24h volume, then liquidity; keep pool count.
        dex_groups = {}
        aggregators = []
        for row in others:
            if row.get("identity", "").startswith("dex:") and row.get("contract_address"):
                dex_groups.setdefault(row["identity"], []).append(row)
            else:
                aggregators.append(row)
        dex_merged = []
        for identity, rows in dex_groups.items():
            rows = sorted(rows, key=lambda x: ((n(x.get("volume_24h_usd")) or 0), (n(x.get("liquidity_usd")) or 0)), reverse=True)
            chosen = dict(rows[0])
            chosen["pool_count_observed"] = len(rows)
            chosen["corroborating_sources"] = sorted(set(x.get("source") for x in rows))
            chosen["best_liquidity_pool_usd"] = max((n(x.get("liquidity_usd")) or 0 for x in rows), default=0)
            flags = set(flag for x in rows for flag in (x.get("promo_flags") or []))
            chosen["promo_flags"] = sorted(flags)
            dex_merged.append(chosen)
        # DEX identities are retained separately and placed first in the
        # combined candidate list; raw-return boards remain sorted by observed
        # return, not by venue preference.
        rows = dex_merged + cex_merged + aggregators
        # A symbol/name appearing in more than one independent source is a
        # corroboration hint only; it is not contract-level identity proof.
        symbol_sources = {}
        for row in rows:
            sym = (row.get("symbol") or "").upper()
            if sym:
                symbol_sources.setdefault(sym, set()).add(row.get("source"))
        for row in rows:
            sym = (row.get("symbol") or "").upper()
            row["symbol_source_count"] = len(symbol_sources.get(sym, set()))
            set_risk_tier(row)
        rows = [r for r in rows if n(r.get("change_24h_pct")) is not None and n(r.get("price_usd")) is not None and n(r.get("price_usd")) > 0]
        def ranked(field):
            return sorted(rows, key=lambda x: (n(x.get(field)) if n(x.get(field)) is not None else -1e99,
                                                 n(x.get("rank_score")) or 0), reverse=True)
        top_24h = ranked("change_24h_pct")
        top_24h_gainers = [x for x in top_24h if (n(x.get("change_24h_pct")) or 0) > 0]
        top_24h_losers = sorted([x for x in rows if (n(x.get("change_24h_pct")) or 0) < 0],
                                key=lambda x: n(x.get("change_24h_pct")) or 0)
        top_1h = [x for x in ranked("change_1h_pct") if n(x.get("change_1h_pct")) is not None]
        top_6h = [x for x in ranked("change_6h_pct") if n(x.get("change_6h_pct")) is not None]
        new_pools_24h = sorted([x for x in rows if x.get("market_type") in ("dex_pool", "dex_token") and
                                x.get("age_hours") is not None and x["age_hours"] <= 24],
                               key=lambda x: (n(x.get("change_24h_pct")) or -1e99, n(x.get("rank_score")) or 0), reverse=True)
        new_launches = sorted([x for x in rows if x.get("age_hours") is not None and x["age_hours"] <= 24*14],
                              key=lambda x: (n(x.get("change_24h_pct")) or -1e99, n(x.get("rank_score")) or 0), reverse=True)
        extreme_100 = [x for x in top_24h if (n(x.get("change_24h_pct")) or 0) >= 100]
        extreme_400 = [x for x in top_24h if (n(x.get("change_24h_pct")) or 0) >= 400]
        extreme_1000 = [x for x in top_24h if (n(x.get("change_24h_pct")) or 0) >= 1000]
        moonshots = sorted([x for x in rows if x.get("market_type") in ("dex_pool", "dex_token") and
                            (n(x.get("change_24h_pct")) or 0) > 20],
                           key=lambda x: (n(x.get("change_24h_pct")) or 0, n(x.get("rank_score")) or 0), reverse=True)
        liquid = sorted([x for x in rows if (n(x.get("liquidity_usd")) or 0) >= 100_000 or
                         (x.get("market_type") == "cex_spot" and (n(x.get("volume_24h_usd")) or 0) >= 250_000)],
                        key=lambda x: (n(x.get("rank_score")) or 0, n(x.get("change_24h_pct")) or 0), reverse=True)
        for row in rows:
            row["quality_gate"] = quality_gate(row)
        follow_up = sorted([x for x in rows if x["quality_gate"]["status"] == "FOLLOW_UP_SCREEN_PASSED_NOT_EXECUTION_READY"],
                           key=lambda x: (n(x.get("rank_score")) or 0, n(x.get("volume_24h_usd")) or 0), reverse=True)
        scan_ok = sum(1 for x in self.source_status if x.get("status") in ("OK", "CACHED"))
        scan_cached = sum(1 for x in self.source_status if x.get("status") == "CACHED")
        scan_failed = sum(1 for x in self.source_status if x.get("status") == "FAILED")
        scan_skipped = sum(1 for x in self.source_status if x.get("status") == "SKIPPED")
        return {
            "skill_version": VERSION, "tool": "rocket_hunt.py", "scan_start_utc": getattr(self, "scan_start_utc", utc_iso()),
            "created_utc": utc_iso(),
            "coverage": {"status": "PARTIAL" if scan_failed else ("MULTI_SOURCE_SAMPLED" if scan_ok else "NO_DATA"),
                         "sources_attempted": len(self.source_status), "sources_ok": scan_ok,
                         "sources_cached": scan_cached, "sources_skipped": scan_skipped,
                         "sources_failed": scan_failed, "raw_candidate_rows": len(self.candidates),
                         "unique_asset_rows": len(rows), "cex_venues_requested": [],
                         "scan_order": ["geckoterminal", "dexscreener", "optional_birdeye_solana", "cex_spot", "aggregators"],
                         "dex_first_enforced": True,
                         "checkpoint_dir": str(self.checkpoint_dir) if self.checkpoint_dir else None,
                         "resume_enabled": self.resume,
                         "resume_max_age_seconds": self.resume_max_age_seconds if self.resume else None,
                         "universe_note": "Sampled public DEX-first, CEX-second and aggregator feeds; not every asset, chain, exchange, pool or NFT collection on the internet. GeckoTerminal/DexScreener and optional Birdeye are attempted before CEX adapters. CEX adapters are currently primarily USDT-quoted spot tickers. A partial feed cannot establish the global #1 mover."},
            "ranking_note": "Raw 24h/6h/1h boards sort by provider-reported percent change and may contain anomalous marks. rank_score is a transparent heuristic, not a probability or backtested expectancy. The separate execution_candidate_shortlist is only a conservative follow-up screen and is never execution-ready without fresh sell-side route, security and depth checks.",
            "top_24h_movers": top_24h[:100], "top_24h_gainers": top_24h_gainers[:100],
            "top_24h_losers": top_24h_losers[:100], "top_6h_movers": top_6h[:100],
            "top_1h_movers": top_1h[:100], "new_pools_under_24h": new_pools_24h[:100],
            "new_launch_movers": new_launches[:100],
            "extreme_movers_100pct_plus": extreme_100[:100],
            "extreme_movers_400pct_plus": extreme_400[:100],
            "extreme_movers_1000pct_plus": extreme_1000[:100],
            "high_risk_moonshot_scouts": moonshots[:100], "most_actionable_liquidity_screen": liquid[:100],
            "execution_candidate_shortlist": follow_up[:50],
            "candidate_quality_note": "Shortlist thresholds: DEX liquidity >= $100k, 24h volume >= $250k, >=25 reported 24h transactions, pool age <=365 days, 24h return 0-2500%, no material volume/liquidity or activity anomaly; or CEX volume >=$1m, >=2 corroborating venues, 24h return 2-300%. This is a follow-up research screen, not a buy instruction or execution/safety pass.",
            "dex_discovery_leads": self.discovery_leads,
            "web3_trending_leads": self.web3_trending_leads,
            "source_status": self.source_status, "http_request_log": self.fx.log
        }


def render_markdown(data, limit=30):
    cov = data.get("coverage") or {}
    out = ["# Rocket Hunt — broad blockchain/Web3 mover board", "",
           "**Snapshot:** %s  |  **Coverage:** %s  |  **Rows:** %s candidates / %s deduplicated assets" %
           (data.get("created_utc"), cov.get("status"), cov.get("raw_candidate_rows"), cov.get("unique_asset_rows")), "",
           "> This is a sampled public-data universe, not every asset on every chain or venue. Rankings are observed price moves, not probability of future return. Paid boosts, thin pools and new launches remain visible but are risk-flagged.", ""]
    boards = [("Top 24h gainers", "top_24h_gainers", "change_24h_pct"),
              ("Top 24h movers (gainers and losers)", "top_24h_movers", "change_24h_pct"),
              ("Top 6h movers", "top_6h_movers", "change_6h_pct"),
              ("Top 1h movers", "top_1h_movers", "change_1h_pct"),
              ("Pools launched in the last 24h", "new_pools_under_24h", "change_24h_pct"),
              ("New-launch movers (last 14d)", "new_launch_movers", "change_24h_pct"),
              ("High-risk moonshot scouts", "high_risk_moonshot_scouts", "change_24h_pct"),
              ("Most actionable liquidity screen", "most_actionable_liquidity_screen", "change_24h_pct")]
    for title, key, field in boards:
        out += ["## %s" % title, "", "| # | Asset | Move | Price USD | Price timestamp (UTC) | Pool / pair address | Token contract | 24h volume | Liquidity | Source / venue | Risk tier / flags |", "|---:|---|---:|---:|---|---|---|---:|---:|---|---|"]
        for i, r in enumerate(data.get(key, [])[:limit], 1):
            move = n(r.get(field)); move_s = "n/a" if move is None else "%+.1f%%" % move
            price = n(r.get("price_usd")); price_s = "n/a" if price is None else "%.12g" % price
            vol = n(r.get("volume_24h_usd")); vol_s = "n/a" if vol is None else "$%s" % format(vol, ",.0f")
            liq = n(r.get("liquidity_usd")); liq_s = "n/a" if liq is None else "$%s" % format(liq, ",.0f")
            source = "%s / %s" % (r.get("source"), r.get("venue") or r.get("chain") or "aggregator")
            flags = "; ".join((r.get("risk_flags") or [])[:2]) or "none reported"
            pool_address = r.get("pool_address") or "—"
            contract_address = r.get("contract_address") or "—"
            retrieved = r.get("retrieved_utc") or "—"
            out.append("| %d | **%s** (`%s`) | %s | %s | %s | `%s` | `%s` | %s | %s | %s | %s |" %
                       (i, r.get("asset"), r.get("symbol"), move_s, price_s, retrieved,
                        pool_address, contract_address, vol_s, liq_s, source, flags))
        if not data.get(key):
            out.append("| — | No rows returned by accessible feeds | — | — | — | — | — | — | — | — | — |")
        out.append("")
    nft_rows = [x for x in data.get("web3_trending_leads", []) if x.get("type") == "nft_collection"]
    nft_rows.sort(key=lambda x: n(x.get("floor_price_24h_change_pct")) if n(x.get("floor_price_24h_change_pct")) is not None else -1e99, reverse=True)
    out += ["## Trending NFT collection movers (separate from fungible tokens)", "",
            "| # | Collection | Floor 24h change | Floor price | Volume |", "|---:|---|---:|---:|---:|"]
    for i, row in enumerate(nft_rows[:limit], 1):
        move = n(row.get("floor_price_24h_change_pct")); move_s = "n/a" if move is None else "%+.1f%%" % move
        floor = "%s %s" % (row.get("floor_price", "n/a"), row.get("native_currency", ""))
        out.append("| %d | %s (`%s`) | %s | %s | %s |" %
                   (i, row.get("name") or "Unknown", row.get("symbol") or "—", move_s, floor, row.get("volume_24h_native") or "n/a"))
    if not nft_rows:
        out.append("| — | No authenticated NFT trending feed returned | — | — | — |")
    category_rows = [x for x in data.get("web3_trending_leads", []) if x.get("type") == "category"]
    category_rows.sort(key=lambda x: n(x.get("market_cap_24h_change_pct")) if n(x.get("market_cap_24h_change_pct")) is not None else -1e99, reverse=True)
    out += ["", "## Trending Web3 / blockchain categories", "", "| # | Category | 24h market-cap change | Coins |", "|---:|---|---:|---:|"]
    for i, row in enumerate(category_rows[:limit], 1):
        move = n(row.get("market_cap_24h_change_pct")); move_s = "n/a" if move is None else "%+.2f%%" % move
        out.append("| %d | %s | %s | %s |" % (i, row.get("name") or "Unknown", move_s, row.get("coins_count") or "n/a"))
    if not category_rows:
        out.append("| — | No authenticated category trending feed returned | — | — |")
    out.append("")
    out += ["## Source coverage", "", "| Source | Status | Rows | URL / reason |", "|---|---|---:|---|"]
    for s in data.get("source_status", []):
        out.append("| %s | %s | %s | %s |" % (s.get("source"), s.get("status"), s.get("row_count", "—"), s.get("url") or s.get("reason") or s.get("error") or "—"))
    out += ["", "## Interpretation", "", "- The raw 24h board is the answer to ‘what already moved the most?’; it is **not** the buy signal.",
            "- The liquidity screen and deep finalist process are the answer to ‘what can I plausibly enter now?’.",
            "- DEX pool age, liquidity, transaction activity, paid promotion, holder concentration and sellability must be checked before calling a microcap executable.",
            "- A 1,000% quote from a tiny pool may be an unfillable mark, a tax trap, wash activity or a real early rocket. The board retains it and makes the uncertainty visible.",
            "- No score is a probability. No outcome is guaranteed.\n"]
    return "\n".join(out)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--venue", action="append", choices=tuple(venues.VENUES), help="CEX spot venue; repeatable (default: all supported)")
    p.add_argument("--out", default="rocket-run/rocket-hunt.json")
    p.add_argument("--md", default="rocket-run/rocket-hunt.md")
    p.add_argument("--limit", type=int, default=30, help="rows per Markdown leaderboard")
    p.add_argument("--timeout", type=int, default=8,
                   help="per HTTP request timeout in seconds; not a total scan deadline")
    p.add_argument("--checkpoint-dir", default="rocket-run/checkpoints",
                   help="directory for successful raw API response checkpoints")
    p.add_argument("--resume", action="store_true",
                   help="reuse successful checkpoint responses within the freshness window")
    p.add_argument("--resume-max-age-seconds", type=int, default=900,
                   help="maximum age of a saved response accepted by --resume (default 900)")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--dex-lookup-limit", type=int, default=160)
    p.add_argument("--dex-search-limit", type=int, default=12, help="broad DexScreener pair/category queries (1-12)")
    p.add_argument("--coingecko-pages", type=int, default=2)
    p.add_argument("--no-coinpaprika", action="store_true")
    p.add_argument("--no-coingecko", action="store_true")
    p.add_argument("--no-dex", action="store_true")
    p.add_argument("--no-gecko", action="store_true")
    p.add_argument("--birdeye", action="store_true", help="use optional BIRDEYE_API_KEY for Solana trending")
    p.add_argument("--network", action="append", default=[], help="extra GeckoTerminal network to sweep")
    p.add_argument("--network-sweep-limit", type=int, default=len(DEFAULT_NETWORKS), help="request trending/new/top-volume pools for the first N configured chains (default: all %d configured networks; set 0 to skip per-network sweep)" % len(DEFAULT_NETWORKS))
    p.add_argument("--gecko-pages", type=int, default=3, help="global GeckoTerminal pages per timeframe/list (1-10)")
    p.add_argument("--gecko-request-interval", type=float, default=12.0,
                   help="seconds between public GeckoTerminal calls (default 12; increase if 429 responses persist)")
    p.add_argument("--retries", type=int, default=2, help="HTTP retry attempts for transient failures and rate limits (default 2)")
    a = p.parse_args(argv)
    if a.retries < 0 or a.retries > 5:
        p.error("--retries must be between 0 and 5")
    hunter = RocketHunter(timeout=a.timeout, retries=a.retries, workers=max(1, a.workers),
                          checkpoint_dir=a.checkpoint_dir, resume=a.resume,
                          resume_max_age_seconds=a.resume_max_age_seconds)
    hunter.gecko_request_interval = max(0.0, a.gecko_request_interval)
    if not 1 <= a.dex_search_limit <= len(DEFAULT_DEX_SEARCH_QUERIES):
        p.error("--dex-search-limit must be between 1 and %d" % len(DEFAULT_DEX_SEARCH_QUERIES))
    total_q = len(DEFAULT_DEX_SEARCH_QUERIES)
    q_offset = int(datetime.now(timezone.utc).timestamp() // 3600) % total_q
    rotated_queries = list(DEFAULT_DEX_SEARCH_QUERIES[q_offset:]) + list(DEFAULT_DEX_SEARCH_QUERIES[:q_offset])
    search_queries = tuple(rotated_queries[:a.dex_search_limit])
    default_networks = list(DEFAULT_NETWORKS)
    extra_networks = [x for x in a.network if x not in default_networks]

    # Integrate DefiLlama Flow Engine for dynamic ecosystem discovery allocation
    try:
        from scripts.defillama_flow_engine import DefiLlamaFlowEngine
        flow_engine = DefiLlamaFlowEngine(timeout=min(6, a.timeout))
        priority_chains = flow_engine.get_priority_networks(top_n=len(default_networks))
        reordered = [c for c in priority_chains if c in default_networks]
        remaining = [c for c in default_networks if c not in reordered]
        default_networks = reordered + remaining
    except Exception:
        pass

    if a.network_sweep_limit > 0:
        networks = default_networks[:a.network_sweep_limit] + extra_networks
    else:
        networks = []
    network_sweep_count = len(networks)
    hunter.dex_search_queries = search_queries
    data = hunter.run(a.venue or list(DEFAULT_VENUES), include_coinpaprika=not a.no_coinpaprika,
                      include_coingecko=not a.no_coingecko, include_dex=not a.no_dex,
                      include_gecko=not a.no_gecko, networks=networks,
                      gecko_network_sweep=network_sweep_count,
                      dex_lookup_limit=max(0, a.dex_lookup_limit),
                      coingecko_pages=max(1, min(5, a.coingecko_pages)), include_birdeye=a.birdeye,
                      gecko_pages=max(1, min(10, a.gecko_pages)))
    data["coverage"]["cex_venues_requested"] = a.venue or list(DEFAULT_VENUES)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    Path(a.md).parent.mkdir(parents=True, exist_ok=True)
    Path(a.md).write_text(render_markdown(data, limit=max(1, a.limit)), encoding="utf-8")
    pacing = data.get("coverage", {}).get("discovery_config", {}).get("gecko_pacing_seconds_estimate", 0)
    print("Rocket Hunt %s: %s assets, %s sources OK / %s failed (%s cached). Wrote %s and %s" %
          (VERSION, data["coverage"]["unique_asset_rows"], data["coverage"]["sources_ok"],
           data["coverage"]["sources_failed"], data["coverage"].get("sources_cached", 0), a.out, a.md))
    if pacing:
        print("GeckoTerminal pacing budget: approximately %.1f minutes before network/processing overhead." % (pacing / 60.0))
    return 0 if data["coverage"]["sources_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
