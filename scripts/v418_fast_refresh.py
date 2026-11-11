#!/usr/bin/env python3
"""Low-cost four-hour refresh using free public DEX, launch and aggregator feeds."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scripts.v418_pipeline import PIPELINE_VERSION, process_snapshot, iso_utc

GECKO_INTERVAL_SECONDS = 12.0
GECKO_ENDPOINTS = (
    ("gecko_global_trending_1h", "https://api.geckoterminal.com/api/v2/networks/trending_pools?page=1&duration=1h&include=base_token%2Cquote_token%2Cdex"),
    ("gecko_global_trending_6h", "https://api.geckoterminal.com/api/v2/networks/trending_pools?page=1&duration=6h&include=base_token%2Cquote_token%2Cdex"),
    ("gecko_global_new_pools", "https://api.geckoterminal.com/api/v2/networks/new_pools?page=1&include=base_token%2Cquote_token%2Cdex"),
)
DEXSCREENER_ENDPOINTS = (
    ("dexscreener_latest_profiles", "https://api.dexscreener.com/token-profiles/latest/v1"),
    ("dexscreener_latest_boosts", "https://api.dexscreener.com/token-boosts/latest/v1"),
)
COINPAPRIKA_ENDPOINT = ("coinpaprika_tickers", "https://api.coinpaprika.com/v1/tickers?quotes=USD")


def fetch_json(url: str, timeout: float = 12.0) -> tuple[Any, int | None, str | None]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": f"CryptoPreBreakout/{PIPELINE_VERSION} read-only"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(12 * 1024 * 1024 + 1)
            if len(raw) > 12 * 1024 * 1024:
                return None, response.status, "response exceeds 12 MiB safety limit"
            return json.loads(raw.decode("utf-8")), response.status, None
    except HTTPError as exc:
        return None, exc.code, f"HTTP {exc.code}"
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, getattr(exc, "code", None), f"{type(exc).__name__}: {exc}"


def number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def parse_gecko(payload: Any, lane: str, retrieved: str, endpoint: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    included = {}
    for item in payload.get("included", []) if isinstance(payload.get("included"), list) else []:
        if isinstance(item, dict) and item.get("id"):
            included[item["id"]] = item.get("attributes") or {}
    rows = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes") or {}
        rel = item.get("relationships") or {}
        base_ref = ((rel.get("base_token") or {}).get("data") or {}).get("id")
        base = included.get(base_ref, {})
        pool_id = str(item.get("id") or "")
        chain = pool_id.split("_", 1)[0] if "_" in pool_id else "unknown"
        pool_address = attrs.get("address") or (pool_id.split("_", 1)[1] if "_" in pool_id else None)
        contract = base.get("address") or (base_ref.split("_", 1)[1] if isinstance(base_ref, str) and "_" in base_ref else None)
        change = attrs.get("price_change_percentage") if isinstance(attrs.get("price_change_percentage"), dict) else {}
        volume = attrs.get("volume_usd") if isinstance(attrs.get("volume_usd"), dict) else {}
        transactions = attrs.get("transactions") if isinstance(attrs.get("transactions"), dict) else {}
        row = {
            "identity": f"dex:{chain}:{str(contract or pool_address or pool_id).lower()}",
            "asset": base.get("name") or (attrs.get("name") or "").split("/")[0].strip() or "Unknown token",
            "symbol": base.get("symbol"), "chain": chain,
            "market_type": "dex_pool", "contract_address": contract,
            "pool_address": pool_address, "gecko_pool_id": pool_id,
            "pair": attrs.get("name"), "venue": None,
            "price_usd": number(attrs.get("base_token_price_usd")),
            "change_5m_pct": number(change.get("m5")),
            "change_1h_pct": number(change.get("h1")),
            "change_6h_pct": number(change.get("h6")),
            "change_24h_pct": number(change.get("h24")),
            "volume_1h_usd": number(volume.get("h1")),
            "volume_6h_usd": number(volume.get("h6")),
            "volume_24h_usd": number(volume.get("h24")),
            "liquidity_usd": number(attrs.get("reserve_in_usd")),
            "pool_created_at": attrs.get("pool_created_at"),
            "retrieved_utc": retrieved, "source": lane,
            "source_url": endpoint,
            "pair_url": f"https://www.geckoterminal.com/{chain}/pools/{pool_address}" if pool_address else None,
            "discovery_lane": lane,
            "risk_flags": ["NEW_POOL_DISCOVERY_FEED"] if lane.endswith("new_pools") else [],
        }
        if isinstance(transactions.get("h24"), dict):
            tx24 = transactions["h24"]
            row["buys_24h"] = number(tx24.get("buys"))
            row["sells_24h"] = number(tx24.get("sells"))
        if row["price_usd"] is not None or row["contract_address"] or row["pool_address"]:
            rows.append(row)
    return rows


def parse_dexscreener(payload: Any, lane: str, retrieved: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        chain = str(item.get("chainId") or "unknown").lower()
        address = str(item.get("tokenAddress") or "").strip()
        if not address:
            continue
        flags = ["PROMOTIONAL_FEED_LEAD_NOT_INDEPENDENT_DEMAND"] if "boost" in lane else ["LATEST_TOKEN_PROFILE_LEAD"]
        rows.append({
            "identity": f"dex_token:{chain}:{address.lower()}",
            "asset": item.get("description") or item.get("name") or item.get("symbol") or address,
            "symbol": item.get("symbol"), "chain": chain,
            "market_type": "dex_token_lead", "contract_address": address,
            "price_usd": None, "liquidity_usd": None,
            "retrieved_utc": retrieved, "source": lane,
            "source_url": item.get("url"), "pair_url": item.get("url"),
            "discovery_lane": lane, "risk_flags": flags,
            "promo_flags": ["DEXSCREENER_BOOST_FEED"] if "boost" in lane else [],
            "boost_amount": number(item.get("amount")),
            "boost_total": number(item.get("totalAmount")),
        })
    return rows


def parse_coinpaprika(payload: Any, retrieved: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    rows = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        usd = (item.get("quotes") or {}).get("USD") or {}
        rows.append({
            "identity": f"coinpaprika:{item['id']}", "id": item["id"],
            "asset": item.get("name"), "symbol": item.get("symbol"),
            "market_type": "aggregator_asset", "market_cap_rank": item.get("rank"),
            "price_usd": number(usd.get("price")),
            "change_24h_pct": number(usd.get("percent_change_24h")),
            "volume_24h_usd": number(usd.get("volume_24h")),
            "market_cap_usd": number(usd.get("market_cap")),
            "retrieved_utc": retrieved, "source": "coinpaprika",
            "source_url": f"https://coinpaprika.com/coin/{item['id']}/",
            "discovery_lane": "coinpaprika_market_catalogue", "risk_flags": [],
        })
    return rows


def run_fast(outdir: Path, memory_dir: Path, run_id: str, no_network: bool = False) -> dict[str, Any]:
    outdir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    retrieved = iso_utc(now)
    source_status = []
    lanes: dict[str, list[dict[str, Any]]] = {"top_1h_movers": [], "top_6h_movers": [], "new_pools_under_24h": [], "dex_discovery_leads": [], "web3_trending_leads": []}
    raw_row_count = 0

    if no_network:
        payloads = {name: None for name, _ in GECKO_ENDPOINTS + DEXSCREENER_ENDPOINTS + (COINPAPRIKA_ENDPOINT,)}
        for name in payloads:
            source_status.append({"source": name, "status": "SKIPPED_TEST_MODE", "retrieved_utc": retrieved, "row_count": 0, "error": None})
    else:
        previous_gecko = None
        for name, url in GECKO_ENDPOINTS:
            if previous_gecko is not None:
                elapsed = time.monotonic() - previous_gecko
                if elapsed < GECKO_INTERVAL_SECONDS:
                    time.sleep(GECKO_INTERVAL_SECONDS - elapsed)
            payload, status, error = fetch_json(url)
            previous_gecko = time.monotonic()
            rows = parse_gecko(payload, name, retrieved, url) if error is None else []
            raw_row_count += len(rows)
            target = "top_1h_movers" if "trending_1h" in name else "top_6h_movers" if "trending_6h" in name else "new_pools_under_24h"
            lanes[target].extend(rows)
            source_status.append({"source": name, "status": "OK" if error is None else "FAILED", "http_status": status, "retrieved_utc": retrieved, "row_count": len(rows), "error": error, "url": url})
        for name, url in DEXSCREENER_ENDPOINTS:
            payload, status, error = fetch_json(url)
            rows = parse_dexscreener(payload, name, retrieved) if error is None else []
            raw_row_count += len(rows)
            lanes["dex_discovery_leads"].extend(rows)
            source_status.append({"source": name, "status": "OK" if error is None else "FAILED", "http_status": status, "retrieved_utc": retrieved, "row_count": len(rows), "error": error, "url": url})
        name, url = COINPAPRIKA_ENDPOINT
        payload, status, error = fetch_json(url, timeout=20.0)
        rows = parse_coinpaprika(payload, retrieved) if error is None else []
        raw_row_count += len(rows)
        lanes["web3_trending_leads"] = rows
        source_status.append({"source": name, "status": "OK" if error is None else "FAILED", "http_status": status, "retrieved_utc": retrieved, "row_count": len(rows), "error": error, "url": url})

    gainers = sorted((r for r in lanes["web3_trending_leads"] if number(r.get("change_24h_pct")) is not None), key=lambda r: number(r.get("change_24h_pct")) or -1e99, reverse=True)
    losers = sorted((r for r in lanes["web3_trending_leads"] if number(r.get("change_24h_pct")) is not None), key=lambda r: number(r.get("change_24h_pct")) or 0)
    main = {
        "skill_version": "4.15.3", "pipeline_version": PIPELINE_VERSION,
        "tool": "v418-free-fast-refresh", "created_utc": retrieved, "scan_start_utc": retrieved,
        "coverage": {
            "status": "PARTIAL_FAST_REFRESH", "raw_candidate_rows": raw_row_count,
            "unique_asset_rows": len({r.get("identity") or r.get("contract_address") for values in lanes.values() for r in values if r.get("identity") or r.get("contract_address")}),
            "sources_attempted": len(source_status),
            "sources_ok": sum(1 for s in source_status if s["status"] == "OK"),
            "sources_failed": sum(1 for s in source_status if s["status"] == "FAILED"),
            "market_scope": "sampled global trending/new-pool feeds + latest token profiles/boosts + CoinPaprika catalogue; not exhaustive",
        },
        "source_status": source_status,
        "top_24h_gainers": gainers[:100], "top_24h_losers": losers[:100],
        "top_24h_movers": (gainers[:50] + losers[:50]),
        **lanes,
    }
    (outdir / "rocket-hunt.json").write_text(json.dumps(main, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (outdir / "rocket-hunt.md").write_text(
        f"# v4.18 Free Fast Refresh\n\n**Snapshot:** {retrieved} | **Pipeline:** {PIPELINE_VERSION} | **Coverage:** PARTIAL_FAST_REFRESH\n\n"
        f"This lightweight run samples public new-pool/trending feeds, latest token profiles/boosts and CoinPaprika tickers. It is not a full scan and does not claim exhaustive Web3 coverage.\n\n"
        f"- Candidate rows received: {raw_row_count}\n- Sources OK: {main['coverage']['sources_ok']} / {len(source_status)}\n\n",
        encoding="utf-8",
    )
    audit = process_snapshot(outdir / "rocket-hunt.json", None, outdir, memory_dir, run_id, now_override=now)
    return audit


def main_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", type=Path, default=Path("fast-run"))
    parser.add_argument("--memory-dir", type=Path, required=True)
    parser.add_argument("--run-id", default="manual-fast-refresh")
    parser.add_argument("--no-network", action="store_true", help="run the parser/control path without calling providers")
    args = parser.parse_args(argv)
    summary = run_fast(args.outdir, args.memory_dir, args.run_id, args.no_network)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
