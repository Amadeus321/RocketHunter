#!/usr/bin/env python3
"""Read-only post-scan audits for CEX book depth, token security and DEX quotes.

No wallet signing, transaction submission, exchange trading or approvals occur.
Provider responses are snapshots; missing providers/credentials are explicitly
reported as SKIPPED or UNVERIFIED rather than inferred to be safe/tradable.
"""
import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import venues  # noqa: E402
import execution_simulator  # noqa: E402
import dex_loss_guard  # noqa: E402
import dex_onchain_evidence  # noqa: E402
from address_identity import address_key  # noqa: E402

GOPLUS_ROOT = "https://api.gopluslabs.io/api/v1"
ZEROX_PRICE_ROOT = "https://api.0x.org/swap/allowance-holder/price"
ZEROX_QUOTE_ROOT = "https://api.0x.org/swap/allowance-holder/quote"
JUPITER_QUOTE_ROOT = "https://api.jup.ag/swap/v1/quote"
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Native USDC contracts (6 decimals) on chains with verified 0x EVM quote support.
# Other networks remain explicitly UNSUPPORTED until a stablecoin/token route is
# independently configured; wrapped/bridged look-alikes are not substituted.
EVM_USDC = {
    "eth": {"chain_id": 1, "usdc": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
    "ethereum": {"chain_id": 1, "usdc": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
    "base": {"chain_id": 8453, "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
    "arbitrum": {"chain_id": 42161, "usdc": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"},
    "optimism": {"chain_id": 10, "usdc": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"},
    "polygon_pos": {"chain_id": 137, "usdc": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"},
    "polygon": {"chain_id": 137, "usdc": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"},
    "avax": {"chain_id": 43114, "usdc": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E"},
    "avalanche": {"chain_id": 43114, "usdc": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E"},
}

GOPLUS_NAME_ALIASES = {
    "eth": "eth", "ethereum": "eth", "bsc": "bsc", "polygon_pos": "polygon",
    "polygon": "polygon", "arbitrum": "arbitrum", "avax": "avalanche",
    "avalanche": "avalanche", "optimism": "optimism", "ftm": "fantom",
    "fantom": "fantom", "base": "base", "cro": "cronos", "cronos": "cronos",
    "opbnb": "opbnb", "linea": "linea", "scroll": "scroll", "zksync": "zksync",
    "blast": "blast", "mantle": "mantle", "mode": "mode",
}


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _json_get(url, headers=None, timeout=10, retries=2):
    VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()
    req_headers = {"User-Agent": "crypto-prebreakout-research-post-audit/%s" % VERSION,
                   "Accept": "application/json"}
    req_headers.update(headers or {})
    attempts = 0
    while True:
        attempts += 1
        req = urllib.request.Request(url, headers=req_headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body), {"ok": True, "status": response.status, "error": None,
                                          "url": url, "attempts": attempts, "retrieved_utc": utc_now()}
        except urllib.error.HTTPError as exc:
            retryable = exc.code in (418, 429) or 500 <= exc.code <= 599
            if retryable and attempts <= retries:
                delay = None
                try:
                    delay = float(exc.headers.get("Retry-After")) if exc.headers else None
                except (TypeError, ValueError):
                    delay = None
                if delay is None:
                    delay = min(15.0, 2.0 ** attempts * 2.0)
                time.sleep(max(0.0, min(60.0, delay)))
                continue
            return None, {"ok": False, "status": exc.code, "error": "HTTP %s" % exc.code,
                          "url": url, "attempts": attempts, "retrieved_utc": utc_now()}
        except Exception as exc:
            return None, {"ok": False, "status": None, "error": "%s: %s" % (type(exc).__name__, exc),
                          "url": url, "attempts": attempts, "retrieved_utc": utc_now()}


def _is_evm_address(value):
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", str(value or "")))


def _provider_code_ok(payload):
    if not isinstance(payload, dict):
        return False
    try:
        return int(payload.get("code", 0) or 0) == 1
    except (TypeError, ValueError):
        return False


def _parse_security(raw):
    """Return high-signal GoPlus flags; never collapse a missing field to safe."""
    if not isinstance(raw, dict):
        return {"status": "UNVERIFIED", "reason": "Provider returned no token record."}
    fields = ("is_honeypot", "cannot_buy", "cannot_sell_all", "is_open_source", "is_proxy",
              "is_mintable", "hidden_owner", "can_take_back_ownership", "owner_change_balance",
              "selfdestruct", "transfer_pausable", "is_blacklisted", "buy_tax", "sell_tax",
              "holder_count", "lp_holder_count", "creator_address")
    selected = {key: raw.get(key) for key in fields if key in raw}
    flags = []
    for key, label in (("is_honeypot", "HONEYPOT"), ("cannot_buy", "CANNOT_BUY"),
                       ("cannot_sell_all", "CANNOT_SELL_ALL"), ("is_proxy", "PROXY_IMPLEMENTATION_REVIEW_REQUIRED"),
                       ("hidden_owner", "HIDDEN_OWNER"),
                       ("can_take_back_ownership", "OWNERSHIP_RECLAIMABLE"),
                       ("owner_change_balance", "OWNER_CAN_CHANGE_BALANCES"),
                       ("selfdestruct", "SELFDESTRUCT_PRESENT"),
                       ("is_mintable", "MINTABLE"), ("transfer_pausable", "TRANSFER_PAUSABLE"),
                       ("is_blacklisted", "BLACKLIST_FUNCTION")):
        if str(raw.get(key, "0")) == "1":
            flags.append(label)
    if str(raw.get("is_open_source", "1")) == "0":
        flags.append("SOURCE_NOT_VERIFIED")
    for key in ("buy_tax", "sell_tax"):
        tax = _num(raw.get(key))
        if tax is not None and tax > 0.10:
            flags.append(key.upper() + "_OVER_10_PERCENT")
    # No hard flag is not a SAFE verdict: some fields can be absent and this is
    # a provider scan, not a bytecode audit or a real transaction simulation.
    return {"status": "FLAGGED_REVIEW" if flags else "PROVIDER_CHECK_RETURNED_REVIEW_REQUIRED",
            "risk_flags": flags, "fields_returned": sorted(selected), "data": selected,
            "limitations": ["Provider coverage and fields may be incomplete",
                            "No on-chain simulation or independent source-code audit",
                            "Holder/LP data may omit labels, vesting and locked supply"]}


def _select_dex_candidates(report, limit):
    rows = []
    seen = set()
    for board in ("execution_candidate_shortlist", "most_actionable_liquidity_screen", "top_24h_movers", "new_pools_under_24h"):
        for row in report.get(board, []) or []:
            if not isinstance(row, dict) or row.get("market_type") != "dex_pool":
                continue
            address = row.get("contract_address")
            chain = str(row.get("chain") or "").lower()
            identity = (chain, str(address or "").lower())
            if not address or identity in seen:
                continue
            seen.add(identity)
            rows.append(row)
    rows.sort(key=lambda x: (
        (x.get("quality_gate") or {}).get("status") == "FOLLOW_UP_SCREEN_PASSED_NOT_EXECUTION_READY",
        _num(x.get("rank_score")) or 0,
        _num(x.get("liquidity_usd")) or 0,
        _num(x.get("volume_24h_usd")) or 0), reverse=True)
    return rows[:max(0, limit)]


def _parse_utc(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _trade_cvd_window(trades, now, seconds):
    buy_usd = sell_usd = 0.0
    buys = sells = 0
    buyers, sellers = set(), set()
    valid = 0
    anomalous = 0
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        timestamp = _parse_utc(trade.get("block_timestamp"))
        if timestamp is None or (now - timestamp).total_seconds() < 0 or (now - timestamp).total_seconds() > seconds:
            continue
        kind = str(trade.get("kind") or "").lower()
        volume = _num(trade.get("volume_in_usd"))
        if volume is None or volume < 0 or kind not in ("buy", "sell"):
            continue
        valid += 1
        if str(trade.get("is_anomalous", "false")).lower() in ("true", "1", "yes"):
            anomalous += 1
        address = str(trade.get("tx_from_address") or "").lower()
        if kind == "buy":
            buys += 1
            buy_usd += volume
            if address:
                buyers.add(address)
        else:
            sells += 1
            sell_usd += volume
            if address:
                sellers.add(address)
    total = buy_usd + sell_usd
    cvd = buy_usd - sell_usd
    return {"buy_volume_usd": round(buy_usd, 6), "sell_volume_usd": round(sell_usd, 6),
            "cvd_usd": round(cvd, 6), "cvd_ratio_pct": round(cvd / total * 100.0, 4) if total else None,
            "buy_trades": buys, "sell_trades": sells, "unique_buyers_sampled": len(buyers),
            "unique_sellers_sampled": len(sellers), "valid_trades_sampled": valid,
            "anomalous_trades_flagged": anomalous}


def audit_gecko_spot_cvd(report, limit=8, timeout=10, interval_seconds=12.0):
    """Sample the latest GeckoTerminal pool trades and calculate USD buy/sell delta."""
    candidates = _select_dex_candidates(report, limit)
    if not candidates:
        return {"provider": "GeckoTerminal public on-chain trades", "status": "NO_CANDIDATES",
                "reason": "No DEX candidates with exact pool identity were present in the sampled report.",
                "audited": [], "candidate_count": 0}
    network_aliases = {"avalanche": "avax", "sui": "sui-network", "fantom": "ftm",
                       "cronos": "cro", "sei": "sei-network"}
    audited, logs = [], []
    last_completed = None
    for row in candidates:
        network = str(row.get("chain") or "").lower()
        network = network_aliases.get(network, network)
        pool = str(row.get("pool_address") or "").strip()
        item = {"chain": network, "symbol": row.get("symbol"),
                "contract_address": row.get("contract_address"), "pool_address": pool,
                "quality_gate": (row.get("quality_gate") or {}).get("status"),
                "retrieved_utc": utc_now()}
        if not network or not pool:
            item.update({"status": "UNVERIFIED", "reason": "Exact network slug or pool address is missing."})
            audited.append(item)
            continue
        if last_completed is not None and interval_seconds > 0:
            wait = interval_seconds - (time.monotonic() - last_completed)
            if wait > 0:
                time.sleep(wait)
        params = urllib.parse.urlencode({"trading_period": "1d", "per_page": 300})
        url = "%s/networks/%s/pools/%s/trades?%s" % (
            "https://api.geckoterminal.com/api/v2", urllib.parse.quote(network, safe=""),
            urllib.parse.quote(pool, safe=""), params)
        payload, log = _json_get(url, timeout=timeout, retries=2)
        last_completed = time.monotonic()
        logs.append(log)
        trades = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(trades, list) or not trades:
            item.update({"status": "UNVERIFIED", "reason": log.get("error") or "No recent trade data returned.",
                         "http_status": log.get("status"), "sampled_trade_count": 0})
            audited.append(item)
            continue
        raw = [x.get("attributes", {}) for x in trades if isinstance(x, dict)]
        now = datetime.now(timezone.utc)
        windows = {"1h": 3600, "6h": 21600, "24h": 86400}
        item.update({"status": "SAMPLED_CVD_AVAILABLE", "sampled_trade_count": len(raw),
                     "sample_limit": 300, "sample_limit_reached": len(raw) >= 300,
                     "cvd_by_window": {label: _trade_cvd_window(raw, now, seconds)
                                       for label, seconds in windows.items()},
                     "first_trade_utc": min((_parse_utc(t.get("block_timestamp")) for t in raw
                                             if _parse_utc(t.get("block_timestamp")) is not None), default=None),
                     "last_trade_utc": max((_parse_utc(t.get("block_timestamp")) for t in raw
                                            if _parse_utc(t.get("block_timestamp")) is not None), default=None),
                     "note": "Sampled latest trades from one pool only. A 300-trade cap can truncate busy pools; this is not full-token multi-pool CVD or CEX CVD."})
        # Convert datetimes to ISO strings for JSON serialization.
        for field in ("first_trade_utc", "last_trade_utc"):
            if isinstance(item.get(field), datetime):
                item[field] = item[field].strftime("%Y-%m-%dT%H:%M:%SZ")
        audited.append(item)
    successful = sum(1 for item in audited if item.get("status") == "SAMPLED_CVD_AVAILABLE")
    status = "COMPLETED_WITH_GAPS" if successful != len(audited) else "COMPLETED_SAMPLED_REVIEW_REQUIRED"
    return {"provider": "GeckoTerminal public on-chain trades", "status": status,
            "audited": audited, "audit_log": logs, "candidate_count": len(candidates),
            "successful_pools": successful, "request_interval_seconds": interval_seconds,
            "note": "Buy/sell USD delta from the sampled recent trades endpoint. The endpoint returns at most 300 trades per selected pool in this scan; it is not a full tape or proof of future price direction."}


def audit_goplus(report, limit=12, timeout=10):
    token = os.environ.get("GOPLUS_ACCESS_TOKEN", "").strip()
    if token.startswith("http"):
        token = ""
    candidates = _select_dex_candidates(report, limit)
    if not candidates:
        return {"provider": "GoPlus", "status": "NO_CANDIDATES",
                "reason": "No DEX token contracts with chain identity were present in the sampled report.",
                "audited": [], "candidate_count": 0}
    headers = {"Authorization": "Bearer " + token} if token else {}
    supported, log = _json_get(GOPLUS_ROOT + "/supported_chains", headers=headers, timeout=timeout)
    if not _provider_code_ok(supported):
        return {"provider": "GoPlus", "status": "FAILED", "reason": log.get("error") or "supported_chains failed",
                "audit_log": [log], "audited": [], "candidate_count": len(candidates)}
    chain_ids = {}
    for item in supported.get("result", []) or []:
        if isinstance(item, dict) and item.get("id") and item.get("name"):
            chain_ids[str(item["name"]).lower()] = str(item["id"])
    audited, logs = [], [log]
    for row in candidates:
        chain = str(row.get("chain") or "").lower()
        chain_name = GOPLUS_NAME_ALIASES.get(chain, chain)
        chain_id = chain_ids.get(chain_name)
        contract = str(row.get("contract_address") or "").lower()
        item = {"chain": chain, "chain_id": chain_id, "symbol": row.get("symbol"),
                "contract_address": contract, "pool_address": row.get("pool_address"),
                "source_price_usd": row.get("price_usd"), "liquidity_usd": row.get("liquidity_usd"),
                "source": "GoPlus", "retrieved_utc": utc_now()}
        if not _is_evm_address(contract):
            item.update({"status": "UNVERIFIED", "reason": "Not an EVM contract address; separate chain security provider is required."})
        elif not chain_id:
            item.update({"status": "UNVERIFIED", "reason": "GoPlus does not list a supported chain id for this network."})
        else:
            url = "%s/token_security/%s?%s" % (GOPLUS_ROOT, chain_id,
                                                   urllib.parse.urlencode({"contract_addresses": contract}))
            data, entry_log = _json_get(url, headers=headers, timeout=timeout)
            logs.append(entry_log)
            result = data.get("result", {}) if _provider_code_ok(data) else {}
            raw = result.get(contract) or result.get(contract.lower()) if isinstance(result, dict) else None
            item["audit"] = _parse_security(raw)
            item["status"] = item["audit"].get("status", "UNVERIFIED")
            if not raw:
                item["reason"] = entry_log.get("error") or (data.get("message") if isinstance(data, dict) else "No security record returned")
        audited.append(item)
    return {"provider": "GoPlus", "status": "COMPLETED_WITH_UNVERIFIED_ITEMS" if any(x.get("status") == "UNVERIFIED" for x in audited) else "COMPLETED_REVIEW_REQUIRED",
            "audited": audited, "audit_log": logs, "candidate_count": len(candidates),
            "note": "No candidate is labelled SAFE; risk-free status cannot be inferred from absent flags."}


def audit_0x_quotes(report, limit=10, notional_usd=100.0, timeout=12):
    """Read-only EVM route prices; use indicative /price without a wallet address.

    A public taker address upgrades the request to the firm /quote endpoint.
    The API key is never accepted as a wallet key and no transaction is signed.
    """
    DEFAULT_TAKER = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    api_key = os.environ.get("ZEROX_API_KEY", "").strip()
    taker = os.environ.get("ROCKET_TAKER_ADDRESS", "").strip()
    if not taker or not _is_evm_address(taker):
        taker = DEFAULT_TAKER
    candidates = _select_dex_candidates(report, limit)
    if not api_key:
        return {"provider": "0x Swap API", "status": "SKIPPED",
                "reason": "ZEROX_API_KEY is not configured; EVM DEX route prices cannot be retrieved.",
                "missing_configuration": ["ZEROX_API_KEY"],
                "required_secrets": ["ZEROX_API_KEY", "ROCKET_TAKER_ADDRESS (optional; firm quote only)"],
                "quotes": [], "candidate_count": len(candidates)}
    if not candidates:
        return {"provider": "0x Swap API", "status": "NO_CANDIDATES",
                "reason": "No DEX token contracts with chain identity were present in the sampled report.",
                "quotes": [], "candidate_count": 0}
    try:
        notional_usd = float(notional_usd)
    except (TypeError, ValueError):
        notional_usd = float("nan")
    if not math.isfinite(notional_usd) or notional_usd <= 0:
        return {"provider": "0x Swap API", "status": "FAILED", "reason": "Quote notional must be positive and finite.",
                "quotes": [], "candidate_count": len(candidates)}
    headers = {"0x-api-key": api_key, "0x-version": "v2"}
    endpoint = ZEROX_QUOTE_ROOT if taker else ZEROX_PRICE_ROOT
    quote_mode = "firm_quote_snapshot" if taker else "indicative_price_snapshot"
    stable_amount = str(int(round(notional_usd * 1_000_000)))
    quotes, logs = [], []
    for row in candidates:
        chain = str(row.get("chain") or "").lower()
        config = EVM_USDC.get(chain)
        token = str(row.get("contract_address") or "").strip()
        item = {"chain": chain, "symbol": row.get("symbol"), "contract_address": token,
                "notional_usd": notional_usd, "timestamp_utc": utc_now(), "quote_type": quote_mode}
        if not config or not _is_evm_address(token):
            item.update({"status": "UNSUPPORTED", "reason": "No verified chain-native USDC mapping or valid EVM token address."})
            quotes.append(item)
            continue
        params = {"chainId": str(config["chain_id"]), "sellToken": config["usdc"],
                  "buyToken": token, "sellAmount": stable_amount}
        if taker:
            params["taker"] = taker
        buy_url = endpoint + "?" + urllib.parse.urlencode(params)
        buy, buy_log = _json_get(buy_url, headers=headers, timeout=timeout)
        logs.append(buy_log)
        if not isinstance(buy, dict) or not buy.get("buyAmount"):
            item.update({"status": "NO_BUY_ROUTE", "reason": buy_log.get("error") or (buy.get("reason") if isinstance(buy, dict) else "0x returned no buy amount"),
                         "buy_http_status": buy_log.get("status")})
            quotes.append(item)
            continue
        try:
            bought_raw = str(int(buy["buyAmount"]))
        except (TypeError, ValueError):
            item.update({"status": "UNVERIFIED", "reason": "0x buyAmount was not an integer base-unit amount."})
            quotes.append(item)
            continue
        sell_params = {"chainId": str(config["chain_id"]), "sellToken": token,
                       "buyToken": config["usdc"], "sellAmount": bought_raw}
        if taker:
            sell_params["taker"] = taker
        sell_url = endpoint + "?" + urllib.parse.urlencode(sell_params)
        sell, sell_log = _json_get(sell_url, headers=headers, timeout=timeout)
        logs.append(sell_log)
        item["buy_quote"] = {"sell_amount_usdc_raw": stable_amount,
                             "buy_amount_token_raw": bought_raw,
                             "price": buy.get("price"),
                             "estimated_price_impact": buy.get("estimatedPriceImpact"),
                             "issues": buy.get("issues"), "gas": buy.get("gas"),
                             "gas_price": buy.get("gasPrice"),
                             "liquidity_available": buy.get("liquidityAvailable")}
        if isinstance(sell, dict) and sell.get("buyAmount"):
            try:
                exit_usd = int(sell["buyAmount"]) / 1_000_000.0
                roundtrip_pct = (exit_usd / notional_usd - 1.0) * 100.0
            except (TypeError, ValueError, ZeroDivisionError):
                exit_usd, roundtrip_pct = None, None
            item.update({"status": "BUY_AND_SELL_FIRM_QUOTES_RETURNED" if taker else "BUY_AND_SELL_INDICATIVE_PRICES_RETURNED",
                         "sell_quote": {"sell_amount_token_raw": bought_raw,
                                        "buy_amount_usdc_raw": sell.get("buyAmount"),
                                        "price": sell.get("price"),
                                        "estimated_price_impact": sell.get("estimatedPriceImpact"),
                                        "issues": sell.get("issues"), "gas": sell.get("gas"),
                                        "gas_price": sell.get("gasPrice")},
                         "round_trip_exit_usd_estimate": exit_usd,
                         "round_trip_return_pct_estimate": roundtrip_pct,
                         "important": "Timestamped provider snapshot; indicative prices are not firm orders. Even a firm quote may expire. Network fees, token taxes, approvals and wallet conditions can change realized fills."})
        else:
            item.update({"status": "BUY_ROUTE_ONLY", "sell_route_error": sell_log.get("error") or "No sell amount returned",
                         "sell_http_status": sell_log.get("status"),
                         "important": "A buy route alone is not evidence that the token can be sold."})
        quotes.append(item)
    both = sum(1 for x in quotes if x.get("status") in ("BUY_AND_SELL_FIRM_QUOTES_RETURNED", "BUY_AND_SELL_INDICATIVE_PRICES_RETURNED"))
    return {"provider": "0x Swap API", "status": "COMPLETED_WITH_GAPS" if any(x.get("status") not in ("BUY_AND_SELL_FIRM_QUOTES_RETURNED", "BUY_AND_SELL_INDICATIVE_PRICES_RETURNED") for x in quotes) else "COMPLETED_REVIEW_REQUIRED",
            "quotes": quotes, "audit_log": logs, "candidate_count": len(candidates),
            "route_pairs_with_buy_and_sell": both, "quote_mode": quote_mode,
            "taker_address_configured": bool(taker),
            "note": "No approvals, signatures or transactions were created or submitted."}


def audit_jupiter_quotes(report, limit=10, notional_usd=100.0, timeout=12):
    """Read-only Solana USDC-to-token and token-to-USDC route quote snapshots."""
    api_key = os.environ.get("JUPITER_API_KEY", "").strip()
    candidates = [x for x in _select_dex_candidates(report, limit * 3)
                  if str(x.get("chain") or "").lower() == "solana"][:max(0, limit)]
    if not api_key:
        return {"provider": "Jupiter Swap API", "status": "SKIPPED",
                "reason": "JUPITER_API_KEY is not configured; Solana route quotes cannot be retrieved.",
                "missing_configuration": ["JUPITER_API_KEY"], "quotes": [],
                "candidate_count": len(candidates)}
    if not candidates:
        return {"provider": "Jupiter Swap API", "status": "NO_CANDIDATES",
                "reason": "No Solana DEX token mints with chain identity were present in the sampled report.",
                "quotes": [], "candidate_count": 0}
    try:
        notional_usd = float(notional_usd)
    except (TypeError, ValueError):
        notional_usd = float("nan")
    if not math.isfinite(notional_usd) or notional_usd <= 0:
        return {"provider": "Jupiter Swap API", "status": "FAILED", "reason": "Quote notional must be positive and finite.",
                "quotes": [], "candidate_count": len(candidates)}
    headers = {"x-api-key": api_key}
    stable_amount = str(int(round(notional_usd * 1_000_000)))
    quotes, logs = [], []
    for row in candidates:
        mint = str(row.get("contract_address") or "").strip()
        item = {"chain": "solana", "symbol": row.get("symbol"), "contract_address": mint,
                "notional_usd": notional_usd, "timestamp_utc": utc_now(),
                "quote_type": "Jupiter read-only quote endpoint; no swap transaction requested"}
        if not mint or mint == SOLANA_USDC:
            item.update({"status": "UNSUPPORTED", "reason": "Missing or invalid token mint."})
            quotes.append(item)
            continue
        buy_params = {"inputMint": SOLANA_USDC, "outputMint": mint, "amount": stable_amount,
                      "slippageBps": "100", "swapMode": "ExactIn"}
        buy_url = JUPITER_QUOTE_ROOT + "?" + urllib.parse.urlencode(buy_params)
        buy, buy_log = _json_get(buy_url, headers=headers, timeout=timeout)
        logs.append(buy_log)
        if not isinstance(buy, dict) or not buy.get("outAmount"):
            item.update({"status": "NO_BUY_ROUTE", "reason": buy_log.get("error") or (buy.get("error") if isinstance(buy, dict) else "Jupiter returned no outAmount"),
                         "buy_http_status": buy_log.get("status")})
            quotes.append(item)
            continue
        try:
            bought_raw = str(int(buy["outAmount"]))
        except (TypeError, ValueError):
            item.update({"status": "UNVERIFIED", "reason": "Jupiter outAmount was not an integer base-unit amount."})
            quotes.append(item)
            continue
        sell_params = {"inputMint": mint, "outputMint": SOLANA_USDC, "amount": bought_raw,
                       "slippageBps": "100", "swapMode": "ExactIn"}
        sell_url = JUPITER_QUOTE_ROOT + "?" + urllib.parse.urlencode(sell_params)
        sell, sell_log = _json_get(sell_url, headers=headers, timeout=timeout)
        logs.append(sell_log)
        item["buy_quote"] = {"input_amount_usdc_raw": stable_amount, "output_amount_token_raw": bought_raw,
                             "price_impact_pct": buy.get("priceImpactPct"),
                             "other_amount_threshold": buy.get("otherAmountThreshold"),
                             "route_hops": len(buy.get("routePlan") or []), "context_slot": buy.get("contextSlot")}
        if isinstance(sell, dict) and sell.get("outAmount"):
            try:
                exit_usd = int(sell["outAmount"]) / 1_000_000.0
                roundtrip_pct = (exit_usd / notional_usd - 1.0) * 100.0
            except (TypeError, ValueError, ZeroDivisionError):
                exit_usd, roundtrip_pct = None, None
            item.update({"status": "BUY_AND_SELL_ROUTES_RETURNED",
                         "sell_quote": {"input_amount_token_raw": bought_raw,
                                        "output_amount_usdc_raw": sell.get("outAmount"),
                                        "price_impact_pct": sell.get("priceImpactPct"),
                                        "other_amount_threshold": sell.get("otherAmountThreshold"),
                                        "route_hops": len(sell.get("routePlan") or []), "context_slot": sell.get("contextSlot")},
                         "round_trip_exit_usd_estimate": exit_usd,
                         "round_trip_return_pct_estimate": roundtrip_pct,
                         "important": "Provider snapshot only. The API quote is not a signed transaction and may change before execution; network priority fees and token-specific risk are not fully modeled."})
        else:
            item.update({"status": "BUY_ROUTE_ONLY", "sell_route_error": sell_log.get("error") or "No sell route returned",
                         "sell_http_status": sell_log.get("status")})
        quotes.append(item)
    both = sum(1 for x in quotes if x.get("status") == "BUY_AND_SELL_ROUTES_RETURNED")
    return {"provider": "Jupiter Swap API", "status": "COMPLETED_WITH_GAPS" if any(x.get("status") != "BUY_AND_SELL_ROUTES_RETURNED" for x in quotes) else "COMPLETED_REVIEW_REQUIRED",
            "quotes": quotes, "audit_log": logs, "candidate_count": len(candidates),
            "route_pairs_with_buy_and_sell": both, "taker_address_required": False,
            "note": "No swap transaction was constructed, signed or submitted."}

def audit_cex_books(report, limit=10, notional_usd=100.0, depth=100, timeout=8, fee_bps=0.0):
    rows, seen = [], set()
    for board in ("execution_candidate_shortlist", "top_24h_movers", "most_actionable_liquidity_screen"):
        for row in report.get(board, []) or []:
            if not isinstance(row, dict) or row.get("market_type") != "cex_spot":
                continue
            key = str(row.get("identity") or row.get("symbol") or "").upper()
            if key and key not in seen:
                seen.add(key)
                rows.append(row)
    rows.sort(key=lambda x: (
        (x.get("quality_gate") or {}).get("status") == "FOLLOW_UP_SCREEN_PASSED_NOT_EXECUTION_READY",
        _num(x.get("rank_score")) or 0,
        _num(x.get("volume_24h_usd")) or 0), reverse=True)
    rows = rows[:max(0, limit)]
    tasks = []
    for row in rows:
        base = str(row.get("asset_base") or row.get("symbol") or "").upper()
        venues_for_row = row.get("venues") or ([row.get("venue")] if row.get("venue") else [])
        for venue_name in venues_for_row:
            if venue_name in venues.VENUES:
                tasks.append((row, base, venue_name))

    def one(task):
        row, base, venue_name = task
        fx = venues.Fetcher(timeout=timeout, retries=1, pause=0)
        adapter = venues.VENUES[venue_name]
        symbol = adapter.pair_to_symbol(base)
        book, url = adapter.book(fx, symbol, depth=depth)
        record = {"identity": row.get("identity"), "symbol": base, "venue": venue_name,
                  "pair": symbol, "url": url, "retrieved_utc": utc_now(), "status": "FAILED"}
        if isinstance(book, dict) and book.get("bids") and book.get("asks"):
            book["timestamp_utc"] = record["retrieved_utc"]
            try:
                simulated = execution_simulator.simulate(book, notional_usd, fee_bps)
                record.update({"status": simulated.get("status"), "simulation": simulated,
                               "limitations": simulated.get("limitations")})
            except ValueError as exc:
                record["reason"] = str(exc)
        else:
            log = fx.log[-1] if fx.log else {}
            record["reason"] = log.get("error") or "No order book returned"
            record["http_status"] = log.get("status")
        return record

    audits = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(tasks)))) as pool:
        futures = [pool.submit(one, task) for task in tasks]
        for future in as_completed(futures):
            try:
                audits.append(future.result())
            except Exception as exc:
                audits.append({"status": "FAILED", "reason": "%s: %s" % (type(exc).__name__, exc)})
    audits.sort(key=lambda x: (str(x.get("identity")), str(x.get("venue"))))
    ok = sum(1 for x in audits if x.get("status") == "FULL_ROUND_TRIP_DEPTH_AVAILABLE")
    if not rows:
        audit_status = "NO_CANDIDATES"
    else:
        audit_status = "COMPLETED_WITH_GAPS" if any(x.get("status") != "FULL_ROUND_TRIP_DEPTH_AVAILABLE" for x in audits) else "COMPLETED_REVIEW_REQUIRED"
    return {"status": audit_status,
            "requested_candidates": len(rows), "book_snapshots": audits,
            "full_depth_round_trips": ok, "notional_usd": float(notional_usd),
            "fee_bps_each_side_assumed": float(fee_bps),
            "note": "Order-book depth is cancellable and snapshot-based; fees default to zero unless configured and immediate round trips are not future exit forecasts."}


def audit_dex_loss_guard(report, audit, limit=12, timeout=8):
    """Collect source-attributed launch-wallet and exact LP evidence, then fail closed."""
    candidates = _select_dex_candidates(report, limit)
    security_index = {}
    for item in ((audit.get("goplus_security_audit") or {}).get("audited") or []):
        chain = str(item.get("chain") or "").lower()
        security_index[(chain, address_key(chain, item.get("contract_address")))] = item
    route_index = {}
    for key in ("dex_router_quote_audit", "solana_router_quote_audit"):
        for item in ((audit.get(key) or {}).get("quotes") or []):
            chain = str(item.get("chain") or "").lower()
            route_index[(chain, address_key(chain, item.get("contract_address")))] = item
    audited = []
    for row in candidates:
        chain = str(row.get("chain") or "").lower()
        contract = address_key(chain, row.get("contract_address"))
        key = (chain, contract)
        route_item = route_index.get(key)
        enrichment = dex_onchain_evidence.enrich_candidate(
            row, route_item=route_item, timeout=timeout)
        evidence = enrichment.get("loss_guard_evidence") or {}
        if evidence:
            row["loss_guard_evidence"] = evidence
        enrichment_security = enrichment.get("security_item")
        if chain in {"solana", "sol"}:
            # GoPlus's unsupported-chain placeholder must never mask RugCheck's
            # chain-native Solana authorities/risks result.
            security_item = enrichment_security
        else:
            security_item = security_index.get(key) or enrichment_security
        decision = dex_loss_guard.evaluate_dex_candidate(
            row, security_item, route_item)
        decision["evidence_enrichment"] = enrichment.get("summary") or {
            "status": "UNVERIFIED", "reason": "Evidence provider returned no summary"
        }
        decision.update({
            "asset": row.get("asset") or row.get("symbol"),
            "symbol": row.get("symbol"),
            "chain": chain,
            "contract_address": contract,
            "pool_address": row.get("pool_address") or row.get("pair_address"),
            "retrieved_utc": utc_now(),
        })
        audited.append(decision)
    blocked = sum(1 for item in audited if item.get("status") != "PASS")
    return {
        "provider": "DEX loss-pattern evidence gate",
        "status": "NO_CANDIDATES" if not audited else (
            "COMPLETED_WITH_BLOCKED_ITEMS" if blocked else "COMPLETED_ALL_ITEMS_PASSED"),
        "candidate_count": len(audited),
        "entry_ready_count": len(audited) - blocked,
        "blocked_count": blocked,
        "audited": audited,
        "evidence_providers": ["RugCheck + Solana mainnet RPC + Raydium API v3 where applicable",
                               "Blockscout v2 + read-only EVM JSON-RPC"],
        "note": "A research rank is not an entry approval. Incomplete or truncated holder history, missing initial mint allocation, unmatched pool identity, unsupported LP position models, missing proxy review, security or size-bound two-way quote evidence blocks DEX entry readiness.",
    }


def append_markdown(path, audit):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n\n## Post-scan execution and safety audit\n\n")
        for key, title in (("gecko_spot_cvd_audit", "GeckoTerminal sampled DEX trade CVD"),
                           ("cex_orderbook_audit", "CEX order-book snapshots"),
                           ("goplus_security_audit", "GoPlus token-security checks"),
                           ("dex_router_quote_audit", "0x EVM route prices/quotes"),
                           ("solana_router_quote_audit", "Jupiter Solana route quotes"),
                           ("dex_loss_guard_audit", "DEX loss-pattern entry guard")):
            block = audit.get(key) or {}
            fh.write("### %s\n\nStatus: **%s**\n\n" % (title, block.get("status", "UNKNOWN")))
            if block.get("reason"):
                fh.write(block["reason"] + "\n\n")
            if block.get("missing_configuration"):
                fh.write("Missing configuration: " + ", ".join(block["missing_configuration"]) + "\n\n")
            if key == "gecko_spot_cvd_audit":
                audited = block.get("audited", [])
                fh.write("Pools sampled: %d; pools with trade CVD: %d. The latest-trade response is capped at 300 per pool.\n\n" % (
                    len(audited), block.get("successful_pools", 0)))
            elif key == "cex_orderbook_audit":
                snapshots = block.get("book_snapshots", [])
                fh.write("Snapshots: %d; full displayed-depth round trips: %d.\n\n" % (
                    len(snapshots), block.get("full_depth_round_trips", 0)))
            elif key == "goplus_security_audit":
                fh.write("Candidate contracts reviewed: %d. No token is promoted to SAFE by this provider check.\n\n" % len(block.get("audited", [])))
            else:
                quotes = block.get("quotes", [])
                fh.write("Candidate routes reviewed: %d; both directions returned: %d.\n\n" % (
                    len(quotes), block.get("route_pairs_with_buy_and_sell", 0)))
        fh.write("No exchange orders, on-chain approvals, signatures or transactions were created or submitted.\n")


def preserve_covered_audit_results(previous, current, attempt_utc):
    """Keep same-run CVD/book evidence when a later disabled/empty pass has no replacement."""
    merged = dict(current or {})
    previous = previous if isinstance(previous, dict) else {}
    for key, items_key in (
        ("gecko_spot_cvd_audit", "audited"),
        ("cex_orderbook_audit", "book_snapshots"),
    ):
        fresh = merged.get(key)
        old = previous.get(key)
        if not isinstance(fresh, dict) or not isinstance(old, dict):
            continue
        fresh_items = fresh.get(items_key)
        old_items = old.get(items_key)
        if (fresh.get("status") in {"NO_CANDIDATES", "SKIPPED", "DISABLED"} or
                not isinstance(fresh_items, list) or not fresh_items):
            if isinstance(old_items, list) and old_items:
                retained = dict(old)
                retained["last_empty_attempt_utc"] = attempt_utc
                retained["last_empty_attempt_status"] = fresh.get("status") or "NO_RESULTS"
                merged[key] = retained
    return merged


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="rocket-run/rocket-hunt.json")
    parser.add_argument("--markdown", default="rocket-run/rocket-hunt.md")
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--gecko-cvd-limit", type=int, default=8)
    parser.add_argument("--gecko-cvd-interval", type=float, default=12.0)
    parser.add_argument("--book-limit", type=int, default=10)
    parser.add_argument("--book-depth", type=int, default=100)
    parser.add_argument("--book-notional-usd", type=float, default=100.0)
    parser.add_argument("--fee-bps", type=float, default=0.0)
    parser.add_argument("--security-limit", type=int, default=12)
    parser.add_argument("--router-limit", type=int, default=10)
    parser.add_argument("--router-notional-usd", type=float, default=100.0)
    args = parser.parse_args(argv)
    try:
        report_path = Path(args.report)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        audit = {
            "created_utc": utc_now(), "read_only": True,
            "gecko_spot_cvd_audit": audit_gecko_spot_cvd(report, args.gecko_cvd_limit, args.timeout, args.gecko_cvd_interval),
            "cex_orderbook_audit": audit_cex_books(report, args.book_limit, args.book_notional_usd,
                                                     args.book_depth, args.timeout, args.fee_bps),
            "goplus_security_audit": audit_goplus(report, args.security_limit, args.timeout),
            "dex_router_quote_audit": audit_0x_quotes(report, args.router_limit, args.router_notional_usd, args.timeout),
            "solana_router_quote_audit": audit_jupiter_quotes(report, args.router_limit, args.router_notional_usd, args.timeout),
        }
        audit["dex_loss_guard_audit"] = audit_dex_loss_guard(
            report, audit, max(args.security_limit, args.router_limit), args.timeout)
        audit = preserve_covered_audit_results(
            report.get("post_scan_audit"), audit, audit.get("created_utc"))
        report["post_scan_audit"] = audit
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        markdown = Path(args.markdown)
        if markdown.exists():
            append_markdown(markdown, audit)
        print("Post-scan audit: DEX CVD=%s; %d CEX books; GoPlus=%s; 0x=%s; Jupiter=%s" % (
            audit["gecko_spot_cvd_audit"].get("status"),
            len(audit["cex_orderbook_audit"].get("book_snapshots", [])),
            audit["goplus_security_audit"].get("status"),
            audit["dex_router_quote_audit"].get("status"),
            audit["solana_router_quote_audit"].get("status")), flush=True)
        print("DEX loss-pattern guard: %d/%d entry-ready; blocked=%d." % (
            audit["dex_loss_guard_audit"].get("entry_ready_count", 0),
            audit["dex_loss_guard_audit"].get("candidate_count", 0),
            audit["dex_loss_guard_audit"].get("blocked_count", 0)), flush=True)
        return 0
    except Exception as exc:
        print("Post-scan audit failed: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
