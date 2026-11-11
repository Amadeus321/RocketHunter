#!/usr/bin/env python3
"""Hard-bounded, read-only preflight for DEX discovery and seven CEX spot feeds.

Each endpoint probe runs in a child process so a blocked DNS resolver cannot
hold the whole scan beyond the configured timeout. This checks reachability,
not full scan completeness or execution-route availability.
"""
import argparse
import multiprocessing as mp
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import venues  # noqa: E402

CEX_PROBES = {
    "binance": [
        ("health", "https://api.binance.com/api/v3/ping"),
        ("market", "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
        ("market_alt", "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT"),
        ("market_alt", "https://api-gcp.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
        ("market_alt", "https://api1.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
        ("market_alt", "https://api2.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
        ("market_alt", "https://api3.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
        ("market_alt", "https://api4.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
        ("futures_market", "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT"),
    ],
    "mexc": [
        ("health", "https://api.mexc.com/api/v3/ping"),
        ("market", "https://api.mexc.com/api/v3/ticker/price?symbol=BTCUSDT"),
    ],
    "bybit": [
        ("health", "https://api.bybit.com/v5/market/time"),
        ("market", "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT"),
        ("market_alt", "https://api.bytick.com/v5/market/tickers?category=spot&symbol=BTCUSDT"),
    ],
    "okx": [
        ("health", "https://www.okx.com/api/v5/public/time"),
        ("market", "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT"),
    ],
    "bitget": [
        ("health", "https://api.bitget.com/api/v2/public/time"),
        ("market", "https://api.bitget.com/api/v2/spot/market/tickers?symbol=BTCUSDT"),
    ],
    "gate": [
        ("health", "https://api.gateio.ws/api/v4/spot/currency_pairs/BTC_USDT"),
        ("market", "https://api.gateio.ws/api/v4/spot/tickers?currency_pair=BTC_USDT"),
    ],
    "kucoin": [
        ("health", "https://api.kucoin.com/api/v1/timestamp"),
        ("market", "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol=BTC-USDT"),
    ],
}
DEX_PROBES = {
    "dexscreener": [("market", "https://api.dexscreener.com/latest/dex/search?q=ETH")],
    "geckoterminal": [("market", "https://api.geckoterminal.com/api/v2/networks/base/pools")],
    "coingecko": [("market", "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=volume_desc&per_page=1&page=1")],
    "coinpaprika": [("market", "https://api.coinpaprika.com/v1/tickers?quotes=USD")],
}
PROBES = dict(CEX_PROBES, **DEX_PROBES)


def _probe_direct(provider, kind, url, timeout):
    fx = venues.Fetcher(timeout=timeout, retries=0, pause=0)
    payload = fx.get(url)
    log = fx.log[-1] if fx.log else {}
    return {"venue": provider, "probe": kind, "ok": bool(log.get("ok")),
            "status": log.get("status"), "error": log.get("error"), "url": url,
            "retrieved_utc": log.get("retrieved_utc"),
            "response_type": type(payload).__name__ if payload is not None else None}


def _probe_child(conn, provider, kind, url, timeout):
    try:
        conn.send(_probe_direct(provider, kind, url, timeout))
    except BaseException as exc:
        try:
            conn.send({"venue": provider, "probe": kind, "ok": False, "status": None,
                       "error": "%s: %s" % (type(exc).__name__, exc), "url": url,
                       "retrieved_utc": None, "response_type": None})
        except Exception:
            pass
    finally:
        conn.close()


def _probe_bounded(provider, kind, url, timeout, context):
    recv_conn, send_conn = context.Pipe(duplex=False)
    proc = context.Process(target=_probe_child, args=(send_conn, provider, kind, url, timeout))
    proc.daemon = True
    try:
        proc.start()
        send_conn.close()
        proc.join(max(0.1, float(timeout)) + 1.0)
        if proc.is_alive():
            proc.terminate()
            proc.join(1.0)
            return {"venue": provider, "probe": kind, "ok": False, "status": None,
                    "error": "HARD_TIMEOUT after %.1fs (child process terminated)" % (float(timeout) + 1.0),
                    "url": url, "retrieved_utc": None, "response_type": None}
        if recv_conn.poll():
            return recv_conn.recv()
        return {"venue": provider, "probe": kind, "ok": False, "status": None,
                "error": "probe child exited without returning a result (exit=%s)" % proc.exitcode,
                "url": url, "retrieved_utc": None, "response_type": None}
    except Exception as exc:
        if proc.is_alive():
            proc.terminate()
            proc.join(1.0)
        return {"venue": provider, "probe": kind, "ok": False, "status": None,
                "error": "%s: %s" % (type(exc).__name__, exc), "url": url,
                "retrieved_utc": None, "response_type": None}
    finally:
        recv_conn.close()
        try:
            send_conn.close()
        except Exception:
            pass
        try:
            proc.close()
        except Exception:
            pass


def run(names=None, timeout=6, workers=16, include_dex=True):
    """Run parallel endpoint probes with a hard per-endpoint wall-clock bound."""
    names = list(names if names is not None else CEX_PROBES)
    providers = list(names) + (list(DEX_PROBES) if include_dex else [])
    if len(set(providers)) != len(providers):
        providers = list(dict.fromkeys(providers))
    checks = [(provider, kind, url) for provider in providers for kind, url in PROBES[provider]]
    workers = max(1, min(int(workers), len(checks) or 1))
    try:
        context = mp.get_context("fork")
    except ValueError:
        context = mp.get_context()

    rows = []
    for start in range(0, len(checks), workers):
        batch = checks[start:start + workers]
        # Launch a whole batch before joining so a DNS outage costs one timeout,
        # not N sequential timeouts.
        active = []
        for provider, kind, url in batch:
            recv_conn, send_conn = context.Pipe(duplex=False)
            proc = context.Process(target=_probe_child, args=(send_conn, provider, kind, url, timeout))
            proc.daemon = True
            proc.start()
            send_conn.close()
            active.append((provider, kind, url, proc, recv_conn))
        for provider, kind, url, proc, recv_conn in active:
            proc.join(max(0.1, float(timeout)) + 1.0)
            if proc.is_alive():
                proc.terminate()
                proc.join(1.0)
                row = {"venue": provider, "probe": kind, "ok": False, "status": None,
                       "error": "HARD_TIMEOUT after %.1fs (child process terminated)" % (float(timeout) + 1.0),
                       "url": url, "retrieved_utc": None, "response_type": None}
            elif recv_conn.poll():
                row = recv_conn.recv()
            else:
                row = {"venue": provider, "probe": kind, "ok": False, "status": None,
                       "error": "probe child exited without returning a result (exit=%s)" % proc.exitcode,
                       "url": url, "retrieved_utc": None, "response_type": None}
            rows.append(row)
            recv_conn.close()
            proc.close()

    summary = {}
    for provider in providers:
        provider_checks = [x for x in rows if x["venue"] == provider]
        summary[provider] = {
            "health_ok": any(x["probe"] == "health" and x["ok"] for x in provider_checks),
            "market_ok": any(x["probe"].startswith("market") and x["ok"] for x in provider_checks),
            "futures_market_ok": any(x["probe"] == "futures_market" and x["ok"] for x in provider_checks),
            # Readiness is based on an actual market-data response. A failed
            # primary hostname must not mask a working documented fallback.
            "ready": any(x["probe"].startswith("market") and x["ok"] for x in provider_checks),
            "market_probe_urls_ok": [x["url"] for x in provider_checks if x["probe"].startswith("market") and x["ok"]],
            "probes_ok": sum(1 for x in provider_checks if x["ok"]),
            "probes_attempted": len(provider_checks),
        }
    # Keep DEX discovery distinct from general aggregators: CoinGecko and
    # CoinPaprika are useful cross-checks, but they do not satisfy the DEX-first
    # discovery requirement by themselves.
    dex_market_ok = any(summary.get(x, {}).get("market_ok") for x in ("dexscreener", "geckoterminal"))
    aggregator_market_ok = any(summary.get(x, {}).get("market_ok") for x in ("coingecko", "coinpaprika"))
    cex_market_ok = any(summary.get(x, {}).get("market_ok") for x in CEX_PROBES if x in summary)
    cex_venues_market_ok = [x for x in CEX_PROBES if summary.get(x, {}).get("market_ok")]
    # An explicitly CEX-only preflight is allowed to recommend the reduced
    # scanner when a CEX market endpoint works. The full path requires both a
    # DEX discovery feed and a CEX market feed; aggregator-only is PARTIAL.
    scanner_ready = cex_market_ok and (dex_market_ok or not include_dex)
    any_market_ok = dex_market_ok or aggregator_market_ok or cex_market_ok
    return {"tool": "preflight.py", "results": rows, "summary": summary,
            "market_availability": {"dex_discovery_ok": dex_market_ok,
                                    "aggregator_ok": aggregator_market_ok,
                                    "cex_spot_ok": cex_market_ok,
                                    "cex_venues_market_ok": cex_venues_market_ok,
                                    "cex_venues_requested": [x for x in names if x in CEX_PROBES],
                                    "cex_venues_missing": [x for x in names if x in CEX_PROBES and not summary.get(x, {}).get("market_ok")]},
            "hard_timeout_seconds": float(timeout) + 1.0,
            "recommendation": "scanner" if scanner_ready else
                              "partial_scanner_or_connected_market_tools" if any_market_ok else
                              "network_egress_or_DNS_blocked; use connected market-data tools or run scanner outside this environment"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", action="append", choices=sorted(CEX_PROBES), help="repeat for CEX venues; defaults to all seven")
    parser.add_argument("--timeout", type=float, default=6, help="hard timeout per endpoint (seconds)")
    parser.add_argument("--workers", type=int, default=16, help="parallel probe processes (default: 16)")
    parser.add_argument("--no-dex", action="store_true", help="skip DEX/aggregator reachability checks")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    data = run(args.venue or sorted(CEX_PROBES), args.timeout, args.workers, include_dex=not args.no_dex)
    if args.out:
        import json
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    if args.json:
        import json
        print(json.dumps(data, indent=2))
    else:
        for provider, status in data["summary"].items():
            print("%-14s market=%s READY=%s (%s/%s probes)" %
                  (provider, status["market_ok"], status["ready"], status["probes_ok"], status["probes_attempted"]))
        print("Next path: " + data["recommendation"])
        for row in data["results"]:
            if not row["ok"]:
                print("  FAIL %s/%s: %s (%s)" % (row["venue"], row["probe"], row["error"], row["url"]))
    return 0 if "scanner" == data["recommendation"] else 2


if __name__ == "__main__":
    import json
    sys.exit(main())
