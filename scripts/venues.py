#!/usr/bin/env python3
"""Public market-data adapters (read-only, no API keys, no orders).

Supported venues: mexc, binance, bybit, okx (spot, USDT quote).
Every adapter normalises to the same shapes:

  ticker  -> {"pair", "base", "last", "open", "high", "low", "change_pct", "quote_vol"}
  candle  -> {"t" (open time, ms), "o", "h", "l", "c", "qv" (quote volume)}  oldest first
  book    -> {"bids": [(price, qty)...], "asks": [(price, qty)...]}

IMPORTANT HONESTY NOTE
  Response layouts below follow each exchange's public documentation as the
  author understood it. They were exercised against synthetic fixtures in
  tests/, NOT against live endpoints from the authoring sandbox. Exchanges
  change fields and intervals; every fetch records its URL, HTTP status or
  error, and retrieval time so a failure is reported as a coverage gap,
  never silently turned into data.

Standard library only (Python 3.8+).
"""
import json
import re
import time
import email.utils
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()
USER_AGENT = "crypto-prebreakout-research/%s (read-only market data)" % VERSION
STABLES = {"USDT", "USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDE", "PYUSD", "USD1", "BUSD", "EUR", "EURI", "AEUR", "USDD", "USDX"}
LEVERAGED = re.compile(r"(\d+[LS]|BULL|BEAR)$")
INTERVAL_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
BINANCE_TICKER_MAX_AGE_MS = 30 * 60 * 1000  # short-horizon research freshness policy, not an exchange rule
BINANCE_TICKER_FUTURE_TOLERANCE_MS = 5 * 60 * 1000


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Fetcher:
    """HTTP client with per-host backoff, Retry-After handling and an audit log."""

    def __init__(self, timeout=15, retries=2, pause=0.15):
        self.timeout, self.retries, self.pause = timeout, retries, pause
        self.log = []
        self.host_backoff_until = {}

    @staticmethod
    def _retry_after_seconds(value):
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                dt = email.utils.parsedate_to_datetime(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0.0, (dt - utc_now()).total_seconds())
            except Exception:
                return None

    def _request(self, method, url, payload=None):
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        last_err, status = None, None
        for attempt in range(self.retries + 1):
            wait = max(0.0, self.host_backoff_until.get(host, 0.0) - time.monotonic())
            if wait:
                time.sleep(wait)
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
            body = None
            if payload is not None:
                headers["Content-Type"] = "application/json"
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status = resp.status
                    data = json.loads(resp.read().decode("utf-8"))
                self.log.append({"method": method, "url": url, "ok": True, "status": status,
                                 "error": None, "retrieved_utc": iso(utc_now()), "attempts": attempt + 1})
                if self.pause:
                    time.sleep(self.pause)
                return data
            except urllib.error.HTTPError as exc:
                status = exc.code
                retry_after = self._retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
                last_err = "HTTP %s" % exc.code
                if exc.code in (400, 401, 403, 404, 405, 410, 451):
                    break
                if exc.code == 429 or exc.code == 418:
                    # Public APIs may impose a full per-minute bucket reset. When the server
                    # omits Retry-After, back off conservatively instead of hammering
                    # the same route with 1s/2s retries.
                    delay = retry_after if retry_after is not None else min(60.0, 10.0 * (2 ** attempt))
                    self.host_backoff_until[host] = max(self.host_backoff_until.get(host, 0.0), time.monotonic() + delay)
                    if attempt >= self.retries:
                        last_err += " (rate limit; retry budget exhausted)"
                        break
                    continue
                if exc.code >= 500 and attempt < self.retries:
                    time.sleep(min(10.0, 0.5 * (2 ** attempt)))
                    continue
                break
            except Exception as exc:  # network blocked, timeout, invalid JSON
                last_err = "%s: %s" % (type(exc).__name__, exc)
                if attempt < self.retries:
                    time.sleep(min(5.0, 0.35 * (2 ** attempt)))
        self.log.append({"method": method, "url": url, "ok": False, "status": status,
                         "error": last_err, "retrieved_utc": iso(utc_now()), "attempts": attempt + 1})
        return None

    def get(self, url):
        return self._request("GET", url)

    def post(self, url, payload):
        return self._request("POST", url, payload)


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _keep(base):
    return bool(base) and base not in STABLES and not LEVERAGED.search(base)


def _pct(last, open_, fallback=None):
    if open_ and open_ > 0:
        return (last / open_ - 1) * 100
    return fallback


class _Binanceish:
    """Binance and MEXC spot v3 share the same layout (MEXC uses 60m for hourly)."""

    def __init__(self, name, base_url, interval_map, kline_limit):
        self.name, self.base_url, self.interval_map, self.kline_limit = name, base_url, interval_map, kline_limit
        self.age_check = kline_limit >= 340
        self.last_ticker_audit = {}

    def pair_to_symbol(self, base):
        return base + "USDT"

    def _market_hosts(self):
        if self.name == "binance":
            preferred = [self.base_url, "https://data-api.binance.vision", "https://api-gcp.binance.com",
                         "https://api1.binance.com", "https://api2.binance.com",
                         "https://api3.binance.com", "https://api4.binance.com"]
            return list(dict.fromkeys(preferred))
        return [self.base_url]

    def _get_market_with_fallback(self, fx, path, validator):
        attempts = []
        last_url = None
        for host in self._market_hosts():
            url = host + path
            payload = fx.get(url)
            ok = bool(validator(payload))
            fx_log = getattr(fx, "log", [])
            last_log = fx_log[-1] if fx_log else {}
            attempts.append({"url": url, "ok": ok, "http_status": last_log.get("status"),
                             "error": last_log.get("error")})
            last_url = url
            if ok:
                self.base_url = host
                self.last_market_audit = {"host_attempts": attempts, "selected_base_url": host}
                return payload, url
        self.last_market_audit = {"host_attempts": attempts, "selected_base_url": None}
        return None, last_url

    def tickers(self, fx):
        # Binance supports symbolStatus filtering on the 24h ticker endpoint.
        # MEXC shares the response layout but not necessarily this query parameter.
        # Binance documents several equivalent market-data hostnames. Try the
        # dedicated unauthenticated market-data host before declaring the venue
        # unavailable; do not use a proxy or a geographically restricted host.
        hosts = self._market_hosts()
        attempted = []
        data, url = None, None
        for host in hosts:
            candidate_url = host + "/api/v3/ticker/24hr"
            if self.name == "binance":
                candidate_url += "?symbolStatus=TRADING"
            candidate_data = fx.get(candidate_url)
            fx_log = getattr(fx, "log", [])
            last_log = fx_log[-1] if fx_log else {}
            attempted.append({"url": candidate_url, "ok": isinstance(candidate_data, list),
                              "error": last_log.get("error"), "status": last_log.get("status")})
            if isinstance(candidate_data, list):
                data, url = candidate_data, candidate_url
                self.base_url = host
                break
            url = candidate_url
        if not isinstance(data, list):
            self.last_ticker_audit = {"raw_rows": 0, "returned_rows": 0, "stale_rows_excluded": 0,
                                      "future_rows_excluded": 0, "unknown_time_rows_excluded": 0,
                                      "status_filter": "TRADING" if self.name == "binance" else "not requested",
                                      "host_attempts": attempted}
            return None, url
        out = []
        audit = {"raw_rows": len(data), "returned_rows": 0, "stale_rows_excluded": 0,
                 "future_rows_excluded": 0, "unknown_time_rows_excluded": 0,
                 "status_filter": "TRADING" if self.name == "binance" else "not requested",
                 "freshness_policy": "30m max age; 5m future tolerance" if self.name == "binance" else "not applied",
                 "host_attempts": attempted, "selected_base_url": self.base_url}
        now_ms = int(time.time() * 1000)
        for r in data:
            sym = str(r.get("symbol", ""))
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            if not _keep(base):
                continue
            ticker_close_ms = None
            ticker_age_ms = None
            if self.name == "binance":
                try:
                    ticker_close_ms = int(r.get("closeTime"))
                except (TypeError, ValueError):
                    ticker_close_ms = None
                if ticker_close_ms is None or ticker_close_ms <= 0:
                    audit["unknown_time_rows_excluded"] += 1
                    continue
                ticker_age_ms = now_ms - ticker_close_ms
                if ticker_age_ms > BINANCE_TICKER_MAX_AGE_MS:
                    audit["stale_rows_excluded"] += 1
                    continue
                if ticker_age_ms < -BINANCE_TICKER_FUTURE_TOLERANCE_MS:
                    audit["future_rows_excluded"] += 1
                    continue
                ticker_age_ms = max(0, ticker_age_ms)
            last, open_ = _f(r.get("lastPrice")), _f(r.get("openPrice"))
            chg = _pct(last, open_, _f(r.get("priceChangePercent"), None))
            quote_vol = _f(r.get("quoteVolume"))
            if last <= 0 or chg is None or quote_vol <= 0:
                continue
            item = {"pair": sym, "base": base, "last": last, "open": open_, "high": _f(r.get("highPrice")),
                    "low": _f(r.get("lowPrice")), "change_pct": chg, "quote_vol": quote_vol}
            if ticker_close_ms is not None:
                item.update({"ticker_close_ms": ticker_close_ms, "ticker_age_ms": ticker_age_ms,
                             "trade_count": int(_f(r.get("count")))})
            out.append(item)
        audit["returned_rows"] = len(out)
        self.last_ticker_audit = audit
        return out, url

    def candles(self, fx, symbol, interval, limit=None, start_ms=None, end_ms=None):
        limit = limit or self.kline_limit
        params = {"symbol": symbol, "interval": self.interval_map[interval], "limit": limit}
        if start_ms is not None: params["startTime"] = int(start_ms)
        if end_ms is not None: params["endTime"] = int(end_ms)
        path = "/api/v3/klines?" + urlencode(params)
        data, url = self._get_market_with_fallback(fx, path, lambda value: isinstance(value, list))
        if not isinstance(data, list):
            return None, url
        rows = []
        for k in data:
            if len(k) < 6:
                continue
            c = _f(k[4])
            qv = _f(k[7]) if len(k) > 7 else _f(k[5]) * c
            rows.append({"t": int(k[0]), "o": _f(k[1]), "h": _f(k[2]), "l": _f(k[3]), "c": c, "qv": qv})
        rows.sort(key=lambda r: r["t"])
        return rows, url

    def book(self, fx, symbol, depth=100):
        path = "/api/v3/depth?symbol=%s&limit=%d" % (symbol, depth)
        data, url = self._get_market_with_fallback(
            fx, path, lambda value: isinstance(value, dict) and isinstance(value.get("bids"), list)
            and isinstance(value.get("asks"), list))
        if not isinstance(data, dict) or "bids" not in data:
            return None, url
        return {"bids": [(_f(p), _f(q)) for p, q, *_ in data["bids"]],
                "asks": [(_f(p), _f(q)) for p, q, *_ in data["asks"]]}, url


class _Bybit:
    name = "bybit"
    base_url = "https://api.bybit.com"
    age_check = True
    kline_limit = 400
    interval_map = {"15m": "15", "1h": "60", "4h": "240", "1d": "D"}

    def pair_to_symbol(self, base):
        return base + "USDT"

    def tickers(self, fx):
        # Bybit documents api.bytick.com as an alternative mainnet REST host.
        # Some regions may still be disallowed by Bybit policy; a different host
        # is not treated as a way to bypass those restrictions.
        hosts = list(dict.fromkeys([self.base_url, "https://api.bytick.com"]))
        attempts = []
        data, rows, url = None, None, None
        for host in hosts:
            candidate_url = host + "/v5/market/tickers?category=spot"
            candidate = fx.get(candidate_url)
            try:
                candidate_rows = candidate["result"]["list"]
            except (TypeError, KeyError):
                candidate_rows = None
            fx_log = getattr(fx, "log", [])
            last_log = fx_log[-1] if fx_log else {}
            attempts.append({"url": candidate_url, "ok": isinstance(candidate_rows, list),
                             "error": last_log.get("error"), "status": last_log.get("status")})
            if isinstance(candidate_rows, list):
                data, rows, url = candidate, candidate_rows, candidate_url
                self.base_url = host
                break
            url = candidate_url
        self.last_ticker_audit = {"host_attempts": attempts, "selected_base_url": self.base_url}
        if not isinstance(rows, list):
            return None, url
        out = []
        for r in rows:
            sym = str(r.get("symbol", ""))
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            if not _keep(base):
                continue
            last = _f(r.get("lastPrice"))
            prev = _f(r.get("prevPrice24h"))
            chg = _pct(last, prev, _f(r.get("price24hPcnt"), 0.0) * 100)
            if last <= 0:
                continue
            out.append({"pair": sym, "base": base, "last": last, "open": prev, "high": _f(r.get("highPrice24h")),
                        "low": _f(r.get("lowPrice24h")), "change_pct": chg, "quote_vol": _f(r.get("turnover24h"))})
        return out, url

    def _get_market_with_fallback(self, fx, path, validator):
        hosts = list(dict.fromkeys([self.base_url, "https://api.bytick.com"]))
        attempts, last_url = [], None
        for host in hosts:
            url = host + path
            payload = fx.get(url)
            ok = bool(validator(payload))
            fx_log = getattr(fx, "log", [])
            last_log = fx_log[-1] if fx_log else {}
            attempts.append({"url": url, "ok": ok, "http_status": last_log.get("status"),
                             "error": last_log.get("error")})
            last_url = url
            if ok:
                self.base_url = host
                self.last_market_audit = {"host_attempts": attempts, "selected_base_url": host}
                return payload, url
        self.last_market_audit = {"host_attempts": attempts, "selected_base_url": None}
        return None, last_url

    def candles(self, fx, symbol, interval, limit=None):
        limit = limit or self.kline_limit
        path = "/v5/market/kline?category=spot&symbol=%s&interval=%s&limit=%d" % (symbol, self.interval_map[interval], limit)
        data, url = self._get_market_with_fallback(
            fx, path, lambda value: isinstance(value, dict) and value.get("retCode", 0) == 0
            and isinstance((value.get("result") or {}).get("list"), list))
        try:
            raw = data["result"]["list"]
        except (TypeError, KeyError):
            return None, url
        rows = [{"t": int(k[0]), "o": _f(k[1]), "h": _f(k[2]), "l": _f(k[3]), "c": _f(k[4]),
                 "qv": _f(k[6]) if len(k) > 6 else _f(k[5]) * _f(k[4])} for k in raw]
        rows.sort(key=lambda r: r["t"])  # Bybit returns newest first
        return rows, url

    def book(self, fx, symbol, depth=50):
        path = "/v5/market/orderbook?category=spot&symbol=%s&limit=%d" % (symbol, depth)
        data, url = self._get_market_with_fallback(
            fx, path, lambda value: isinstance(value, dict) and value.get("retCode", 0) == 0
            and isinstance((value.get("result") or {}).get("b"), list)
            and isinstance((value.get("result") or {}).get("a"), list))
        try:
            res = data["result"]
            return {"bids": [(_f(p), _f(q)) for p, q, *_ in res["b"]],
                    "asks": [(_f(p), _f(q)) for p, q, *_ in res["a"]]}, url
        except (TypeError, KeyError):
            return None, url


class _OKX:
    name = "okx"
    base_url = "https://www.okx.com"
    age_check = False  # candles endpoint caps at 300 bars (< 14 days of 1h)
    kline_limit = 300
    interval_map = {"15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}

    def pair_to_symbol(self, base):
        return base + "-USDT"

    def tickers(self, fx):
        url = self.base_url + "/api/v5/market/tickers?instType=SPOT"
        data = fx.get(url)
        try:
            rows = data["data"]
        except (TypeError, KeyError):
            return None, url
        out = []
        for r in rows:
            inst = str(r.get("instId", ""))
            if not inst.endswith("-USDT"):
                continue
            base = inst[:-5]
            if not _keep(base):
                continue
            last, open_ = _f(r.get("last")), _f(r.get("open24h"))
            if last <= 0 or open_ <= 0:
                continue
            out.append({"pair": inst, "base": base, "last": last, "open": open_, "high": _f(r.get("high24h")),
                        "low": _f(r.get("low24h")), "change_pct": (last / open_ - 1) * 100,
                        "quote_vol": _f(r.get("volCcy24h"))})
        return out, url

    def candles(self, fx, symbol, interval, limit=None):
        limit = min(limit or self.kline_limit, 300)
        url = "%s/api/v5/market/candles?instId=%s&bar=%s&limit=%d" % (self.base_url, symbol, self.interval_map[interval], limit)
        data = fx.get(url)
        try:
            raw = data["data"]
        except (TypeError, KeyError):
            return None, url
        rows = []
        for k in raw:
            if len(k) > 8 and str(k[8]) == "0":
                continue  # unconfirmed (still forming) candle
            c = _f(k[4])
            qv = _f(k[7]) if len(k) > 7 and _f(k[7]) > 0 else _f(k[5]) * c
            rows.append({"t": int(k[0]), "o": _f(k[1]), "h": _f(k[2]), "l": _f(k[3]), "c": c, "qv": qv})
        rows.sort(key=lambda r: r["t"])
        return rows, url

    def book(self, fx, symbol, depth=50):
        url = "%s/api/v5/market/books?instId=%s&sz=%d" % (self.base_url, symbol, depth)
        data = fx.get(url)
        try:
            b = data["data"][0]
            return {"bids": [(_f(r[0]), _f(r[1])) for r in b["bids"]],
                    "asks": [(_f(r[0]), _f(r[1])) for r in b["asks"]]}, url
        except (TypeError, KeyError, IndexError):
            return None, url


class _Bitget:
    name = "bitget"
    base_url = "https://api.bitget.com"
    age_check = False
    kline_limit = 200
    interval_map = {"15m": "15min", "1h": "1H", "4h": "4H", "1d": "1D"}

    def pair_to_symbol(self, base):
        return base + "USDT"

    def tickers(self, fx):
        url = self.base_url + "/api/v2/spot/market/tickers"
        data = fx.get(url)
        try:
            rows = data["data"]
        except (TypeError, KeyError):
            return None, url
        out = []
        for r in rows:
            sym = str(r.get("symbol", ""))
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            last, open_ = _f(r.get("lastPr")), _f(r.get("open"))
            if not _keep(base) or last <= 0 or open_ <= 0:
                continue
            out.append({"pair": sym, "base": base, "last": last, "open": open_,
                        "high": _f(r.get("high24h")), "low": _f(r.get("low24h")),
                        "change_pct": _pct(last, open_, _f(r.get("change24h"), 0) * 100),
                        "quote_vol": _f(r.get("usdtVolume")) or _f(r.get("quoteVolume"))})
        return out, url

    def candles(self, fx, symbol, interval, limit=None):
        limit = min(limit or self.kline_limit, 200)
        url = "%s/api/v2/spot/market/candles?symbol=%s&granularity=%s&limit=%d" % (
            self.base_url, symbol, self.interval_map[interval], limit)
        data = fx.get(url)
        try:
            raw = data["data"]
        except (TypeError, KeyError):
            return None, url
        rows = []
        for k in raw:
            if len(k) < 6:
                continue
            c = _f(k[4])
            qv = _f(k[6]) if len(k) > 6 else _f(k[5]) * c
            rows.append({"t": int(k[0]), "o": _f(k[1]), "h": _f(k[2]), "l": _f(k[3]), "c": c, "qv": qv})
        rows.sort(key=lambda r: r["t"])
        return rows, url

    def book(self, fx, symbol, depth=100):
        url = "%s/api/v2/spot/market/orderbook?symbol=%s&type=step0&limit=%d" % (self.base_url, symbol, depth)
        data = fx.get(url)
        try:
            b = data["data"]
            return {"bids": [(_f(x[0]), _f(x[1])) for x in b["bids"]],
                    "asks": [(_f(x[0]), _f(x[1])) for x in b["asks"]]}, url
        except (TypeError, KeyError):
            return None, url


class _Gate:
    name = "gate"
    base_url = "https://api.gateio.ws"
    age_check = False
    kline_limit = 200
    interval_map = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}

    def pair_to_symbol(self, base):
        return base + "_USDT"

    def tickers(self, fx):
        url = self.base_url + "/api/v4/spot/tickers"
        data = fx.get(url)
        if not isinstance(data, list):
            return None, url
        out = []
        for r in data:
            pair = str(r.get("currency_pair", ""))
            if not pair.endswith("_USDT"):
                continue
            base = pair[:-5]
            last, open_ = _f(r.get("last")), _f(r.get("last")) / (1 + _f(r.get("change_percentage")) / 100)
            if not _keep(base) or last <= 0 or open_ <= 0:
                continue
            out.append({"pair": pair, "base": base, "last": last, "open": open_,
                        "high": _f(r.get("high_24h")), "low": _f(r.get("low_24h")),
                        "change_pct": _f(r.get("change_percentage")), "quote_vol": _f(r.get("quote_volume"))})
        return out, url

    def candles(self, fx, symbol, interval, limit=None):
        limit = min(limit or self.kline_limit, 1000)
        url = "%s/api/v4/spot/candlesticks?currency_pair=%s&interval=%s&limit=%d" % (
            self.base_url, symbol, self.interval_map[interval], limit)
        data = fx.get(url)
        if not isinstance(data, list):
            return None, url
        rows = []
        # Gate arrays: [timestamp, quote_volume, close, high, low, open, base_volume].
        for k in data:
            if len(k) < 6:
                continue
            rows.append({"t": int(k[0]) * 1000, "o": _f(k[5]), "h": _f(k[3]),
                         "l": _f(k[4]), "c": _f(k[2]), "qv": _f(k[1])})
        rows.sort(key=lambda r: r["t"])
        return rows, url

    def book(self, fx, symbol, depth=100):
        url = "%s/api/v4/spot/order_book?currency_pair=%s&limit=%d" % (self.base_url, symbol, depth)
        data = fx.get(url)
        try:
            return {"bids": [(_f(x[0]), _f(x[1])) for x in data["bids"]],
                    "asks": [(_f(x[0]), _f(x[1])) for x in data["asks"]]}, url
        except (TypeError, KeyError):
            return None, url


class _Kucoin:
    name = "kucoin"
    base_url = "https://api.kucoin.com"
    age_check = False
    kline_limit = 150
    interval_map = {"15m": "15min", "1h": "1hour", "4h": "4hour", "1d": "1day"}

    def pair_to_symbol(self, base):
        return base + "-USDT"

    def tickers(self, fx):
        url = self.base_url + "/api/v1/market/allTickers"
        data = fx.get(url)
        try:
            rows = data["data"]["ticker"]
        except (TypeError, KeyError):
            return None, url
        out = []
        for r in rows:
            pair = str(r.get("symbol", ""))
            if not pair.endswith("-USDT"):
                continue
            base = pair[:-5]
            last, rate = _f(r.get("last")), _f(r.get("changeRate"))
            open_ = last / (1 + rate) if last > 0 and (1 + rate) > 0 else 0
            if not _keep(base) or last <= 0 or open_ <= 0:
                continue
            out.append({"pair": pair, "base": base, "last": last, "open": open_,
                        "high": _f(r.get("high")), "low": _f(r.get("low")),
                        "change_pct": rate * 100, "quote_vol": _f(r.get("volValue"))})
        return out, url

    def candles(self, fx, symbol, interval, limit=None):
        limit = min(limit or self.kline_limit, 1500)
        url = "%s/api/v1/market/candles?symbol=%s&type=%s" % (self.base_url, symbol, self.interval_map[interval])
        data = fx.get(url)
        try:
            raw = data["data"]
        except (TypeError, KeyError):
            return None, url
        rows = []
        # KuCoin: [time, open, close, high, low, volume, turnover].
        for k in raw[:limit]:
            if len(k) < 7:
                continue
            rows.append({"t": int(k[0]) * 1000, "o": _f(k[1]), "h": _f(k[3]),
                         "l": _f(k[4]), "c": _f(k[2]), "qv": _f(k[6])})
        rows.sort(key=lambda r: r["t"])
        return rows, url

    def book(self, fx, symbol, depth=100):
        url = self.base_url + "/api/v3/market/orderbook/level2_100?symbol=" + symbol
        data = fx.get(url)
        try:
            b = data["data"]
            return {"bids": [(_f(x[0]), _f(x[1])) for x in b["bids"]],
                    "asks": [(_f(x[0]), _f(x[1])) for x in b["asks"]]}, url
        except (TypeError, KeyError):
            return None, url


VENUES = {
    # MEXC spot v3 hourly interval is "60m" (the "Min60" form belongs to MEXC *futures*).
    "mexc": _Binanceish("mexc", "https://api.mexc.com", {"5m": "5m", "15m": "15m", "1h": "60m", "4h": "4h", "1d": "1d"}, 500),
    "binance": _Binanceish("binance", "https://api.binance.com", {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}, 500),
    "bybit": _Bybit(),
    "okx": _OKX(),
    "bitget": _Bitget(),
    "gate": _Gate(),
    "kucoin": _Kucoin(),
}


def completed(candles, interval, now_ms):
    """Drop any candle that has not finished by now_ms (venue-agnostic)."""
    step = INTERVAL_MS[interval]
    return [c for c in candles if c["t"] + step <= now_ms]


def depth_stats(book, pct=2.0):
    """Executable depth (USD) within pct% of mid and top-of-book spread in bps."""
    if not book or not book["bids"] or not book["asks"]:
        return None
    bids = sorted(book["bids"], reverse=True)
    asks = sorted(book["asks"])
    mid = (bids[0][0] + asks[0][0]) / 2
    if mid <= 0:
        return None
    lo, hi = mid * (1 - pct / 100), mid * (1 + pct / 100)
    return {"mid": mid, "spread_bps": (asks[0][0] - bids[0][0]) / mid * 1e4,
            "bid_usd": sum(p * q for p, q in bids if p >= lo),
            "ask_usd": sum(p * q for p, q in asks if p <= hi), "pct": pct,
            "levels": (len(bids), len(asks))}
