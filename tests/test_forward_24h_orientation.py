import unittest
from unittest.mock import patch

from scripts.forward_24h_report import (
    build_market_context,
    derive_risk_metrics,
    fetch_dex_market,
    resolve_gecko_token_orientation,
    fetch_btc_macro_context,
    fetch_hyperliquid_market_context,
    parse_hyperliquid_meta_and_asset_ctxs,
    pick_candidates,
)


class _FakeThrottle:
    def __init__(self, base_token, quote_token, base_price=12.5, quote_price=0.75):
        self.base_token = base_token
        self.quote_token = quote_token
        self.base_price = base_price
        self.quote_price = quote_price
        self.urls = []

    def get(self, url, timeout=10):
        self.urls.append(url)
        if "?include=" in url:
            return {
                "data": {
                    "attributes": {
                        "base_token_price_usd": str(self.base_price),
                        "quote_token_price_usd": str(self.quote_price),
                    },
                    "relationships": {
                        "base_token": {"data": {"id": "solana_" + self.base_token}},
                        "quote_token": {"data": {"id": "solana_" + self.quote_token}},
                    },
                },
            }, "2026-11-07T14:00:00Z"
        return {
            "data": {
                "attributes": {
                    "ohlcv_list": [[1794060000, 0.7, 0.8, 0.6, 0.75, 5000]],
                },
            },
        }, "2026-11-07T14:01:00Z"


class Forward24hOrientationTests(unittest.TestCase):

    def test_pool_token_matching_respects_case_sensitive_solana_mints(self):
        payload = {
            "data": {"relationships": {
                "base_token": {"data": {"id": "solana_MintAbC"}},
                "quote_token": {"data": {"id": "solana_Quote123"}},
            }},
        }
        orientation, verified, _ = resolve_gecko_token_orientation(
            payload, "mintabc", chain="solana")
        self.assertFalse(verified)
        self.assertEqual(orientation, "base")
        evm_payload = {"data": {"relationships": {
            "base_token": {"data": {"id": "ethereum_0xABCDEF0123456789012345678901234567890123"}},
            "quote_token": {"data": {"id": "ethereum_0x1234567890123456789012345678901234567890"}},
        }}}
        orientation, verified, _ = resolve_gecko_token_orientation(
            evm_payload, "0xabcdef0123456789012345678901234567890123", chain="ethereum")
        self.assertTrue(verified)
        self.assertEqual(orientation, "base")

    def test_hyperliquid_context_parses_hourly_funding_and_open_interest(self):
        payload = [
            {"universe": [{"name": "ETH"}, {"name": "SOL"}]},
            [
                {"markPx": "3000", "openInterest": "100", "funding": "0.0001",
                 "prevDayPx": "2900", "dayNtlVlm": "50000000"},
                {"markPx": "150", "openInterest": "2000", "funding": "-0.0002",
                 "prevDayPx": "140", "dayNtlVlm": "100000000"},
            ],
        ]

        markets = parse_hyperliquid_meta_and_asset_ctxs(payload)

        self.assertEqual(markets["ETH"]["open_interest_usd"], 300000.0)
        self.assertAlmostEqual(markets["ETH"]["funding_hourly_pct"], 0.01)
        self.assertAlmostEqual(markets["ETH"]["funding_8h_pct"], 0.08)
        self.assertAlmostEqual(markets["SOL"]["day_change_pct"], (150 / 140 - 1) * 100)

    def test_hyperliquid_derivatives_match_requires_spot_mark_price_confirmation(self):
        payload = [
            {"universe": [{"name": "ETH"}, {"name": "FAKE"}]},
            [
                {"markPx": "3000", "openInterest": "100", "funding": "0.0001",
                 "prevDayPx": "2900", "dayNtlVlm": "50000000"},
                {"markPx": "0.01", "openInterest": "1000000", "funding": "0.001",
                 "prevDayPx": "0.005", "dayNtlVlm": "500000"},
            ],
        ]
        candidates = [
            {"symbol": "ETH", "price": 3010.0},
            {"symbol": "FAKE", "price": 10.0},
        ]

        result = fetch_hyperliquid_market_context(candidates, payload_override=payload)

        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["matched_candidates"], 1)
        self.assertEqual(result["symbol_matches_price_divergent"], 1)
        self.assertTrue(candidates[0]["derivatives_checked"])
        self.assertEqual(candidates[0]["derivatives_market_status"], "HYPERLIQUID_PERP_PRICE_CONFIRMED")
        self.assertFalse(candidates[1].get("derivatives_checked", False))
        self.assertEqual(candidates[1]["hyperliquid_match_status"], "SYMBOL_MATCH_PRICE_DIVERGENCE")

    def test_market_context_flags_risk_off_from_btc_and_alt_breadth(self):
        candidates = [
            {"symbol": "BTC", "returns_pct": {"1h": -0.8, "4h": -3.0}},
        ]
        rows = [
            {"identity": "dex:%s" % i, "symbol": "ALT%s" % i, "change_1h_pct": -2.0}
            for i in range(20)
        ]
        context = build_market_context({"universe_rows_v419": rows}, candidates)

        self.assertEqual(context["label"], "RISK_OFF")
        self.assertTrue(context["risk_off"])
        self.assertEqual(context["breadth_sample_count"], 20)
        self.assertEqual(context["alt_breadth_pct"], 0.0)


    def test_btc_regime_anchor_uses_public_hourly_spot_candles(self):
        class FakeBinance:
            def candles(self, fx, symbol, interval, limit=None):
                self.request = (symbol, interval, limit)
                bars = [
                    {"t": i * 3600000, "c": float(100 + i)}
                    for i in range(8)
                ]
                return bars, "https://example.invalid/btc-klines"

        adapter = FakeBinance()
        with patch("scripts.forward_24h_report.venues.VENUES", {"binance": adapter}):
            result = fetch_btc_macro_context(object())

        self.assertEqual(result["status"], "OK")
        self.assertEqual(adapter.request, ("BTCUSDT", "1h", 8))
        self.assertAlmostEqual(result["returns_pct"]["1h"], (107 / 106 - 1) * 100, places=4)
        self.assertAlmostEqual(result["returns_pct"]["4h"], (107 / 103 - 1) * 100, places=4)

    def test_market_context_adds_candidate_candles_to_unique_alt_breadth(self):
        candidates = [
            {"identity": "cex:AAA", "market_type": "cex_spot", "venue": "binance",
             "symbol": "AAA", "returns_pct": {"1h": 3.0}},
            {"identity": "cex:BBB", "market_type": "cex_spot", "venue": "binance",
             "symbol": "BBB", "returns_pct": {"1h": 1.0}},
        ]
        context = build_market_context({}, candidates, btc_macro={
            "status": "OK", "source": "Binance BTCUSDT 1h candles",
            "returns_pct": {"1h": 1.0, "4h": 2.0},
        })

        self.assertEqual(context["label"], "RISK_ON")
        self.assertEqual(context["breadth_sample_count"], 2)
        self.assertEqual(context["alt_breadth_pct"], 100.0)
        self.assertEqual(context["btc_source"], "Binance BTCUSDT 1h candles")

    def test_atr_metrics_do_not_create_a_stop_loss(self):
        candles = []
        for hour in range(30):
            candles.append({
                "t": hour * 3600,
                "o": 100.0, "h": 102.0, "l": 98.0, "c": 100.0, "qv": 1000.0,
            })
        metrics = derive_risk_metrics(candles, 100.0)

        self.assertIsNotNone(metrics["atr_1h_usd"])
        self.assertIsNotNone(metrics["atr_1h_pct"])
        self.assertNotIn("initial_stop_price", metrics)
        self.assertNotIn("stop_loss_pct", metrics)

    def test_exact_tracked_token_is_used_when_it_is_quote(self):
        throttle = _FakeThrottle("So11111111111111111111111111111111111111112", "tracked-mint")
        row = {
            "chain": "solana",
            "pool_address": "pool-address",
            "contract_address": "tracked-mint",
        }

        price, candles, meta, error = fetch_dex_market(row, throttle)

        self.assertIsNone(error)
        self.assertEqual(price, 0.75)
        self.assertEqual(meta["tracked_token_orientation"], "quote")
        self.assertTrue(meta["tracked_token_orientation_verified"])
        self.assertEqual(len(candles), 1)
        self.assertIn("token=tracked-mint", throttle.urls[1])
        self.assertNotIn("token=base", throttle.urls[1])
        self.assertNotIn("token=quote", throttle.urls[1])


    def test_stronger_breakout_record_replaces_mis_oriented_pool_identity(self):
        full_universe = {
            "market_type": "dex_pool", "chain": "solana",
            "identity": "dex:solana:wrapped-sol-contract",
            "pool_address": "same-pool", "contract_address": "So11111111111111111111111111111111111111112",
            "asset": "Wrapped SOL", "symbol": "SOL", "pair": "SOL/ALPHA",
            "price_usd": 155.0, "liquidity_usd": 120000, "volume_24h_usd": 500000,
            "change_5m_pct": 1.0, "change_1h_pct": 2.0, "change_6h_pct": 3.0,
        }
        breakout_record = {
            "market_type": "dex_pool", "chain": "solana",
            "identity": "dex:solana:same-pool",
            "pool_address": "same-pool", "contract_address": "alpha-mint",
            "asset": "Alpha", "symbol": "ALPHA", "pair": "ALPHA/SOL",
            "price_usd": 0.00042, "liquidity_usd": 125000, "volume_24h_usd": 530000,
            "change_5m_pct": 7.0, "change_1h_pct": 31.0, "change_6h_pct": 45.0,
            "change_24h_pct": 52.0, "early_breakout_score": 91.0,
            "early_breakout_tier": "EARLY_BREAKOUT_LEADER",
        }
        picked = pick_candidates({
            "universe_rows_v419": [full_universe],
            "discovery_rows_v421": [breakout_record],
        }, dex_limit=1, cex_limit=0)

        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["identity"], "dex:solana:same-pool")
        self.assertEqual(picked[0]["contract_address"], "alpha-mint")
        self.assertEqual(picked[0]["asset"], "Alpha")
        self.assertEqual(picked[0]["pair"], "ALPHA/SOL")
        self.assertEqual(picked[0]["price_usd"], 0.00042)
        self.assertEqual(picked[0]["early_breakout_score"], 91.0)

    def test_exact_tracked_token_is_used_when_it_is_base(self):
        throttle = _FakeThrottle("tracked-mint", "So11111111111111111111111111111111111111112")
        row = {
            "chain": "solana",
            "pool_address": "pool-address",
            "contract_address": "tracked-mint",
        }

        price, candles, meta, error = fetch_dex_market(row, throttle)

        self.assertIsNone(error)
        self.assertEqual(price, 12.5)
        self.assertEqual(meta["tracked_token_orientation"], "base")
        self.assertTrue(meta["tracked_token_orientation_verified"])
        self.assertIn("token=tracked-mint", throttle.urls[1])


if __name__ == "__main__":
    unittest.main()
