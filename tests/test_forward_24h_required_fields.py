import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import forward_24h_report as report
import venues


class _NoneCandlesAdapter:
    def pair_to_symbol(self, value):
        return value + "USDT"

    def candles(self, fetcher, symbol, interval, limit=168):
        return None, "https://example.invalid/candles"


class ForwardReportContractTests(unittest.TestCase):
    def test_none_from_cex_candle_adapter_is_normalized(self):
        old = venues.VENUES
        try:
            venues.VENUES = {"testvenue": _NoneCandlesAdapter()}
            row = {
                "market_type": "cex_spot", "venue": "testvenue", "asset": "TEST",
                "symbol": "TEST", "identity": "cex:testvenue:TESTUSDT", "price_usd": 1.0,
                "retrieved_utc": report.utc_now(), "pair": "TEST/USDT",
            }
            candidate = report.make_candidate(row, report.utc_now(), {}, {}, {}, {}, None, None)
            self.assertEqual(candidate["_candles_count"], 0)
            self.assertIsInstance(candidate["_scan_row"], dict)
        finally:
            venues.VENUES = old

    def test_complete_dex_pick_always_has_three_required_fields(self):
        top = {
            "market_type": "dex_pool", "price": 0.0125,
            "chain": "ethereum",
            "data_fresh": True, "market_verified": True,
            "tracked_token_orientation_verified": True,
            "entry_decision": "TOP ROCKET — EXECUTION UNVERIFIED",
            "pool_address": "0xpair123", "contract_address": "0xtoken123",
            "initial_stop_price": 0.011, "trailing_stop_price": 0.012,
            "trade_plan": {
                "single_take_profit": 0.0175, "stop_loss_price": 0.011,
                "trailing_stop_price": 0.012, "take_profit_2": 0.02,
            },
        }
        meta = {
            "final_price_verified": True, "final_pool_address": "0xpair123",
            "final_token_contract": "0xtoken123", "final_quote_chain": "ethereum",
            "final_quote_token_orientation_verified": True,
            "pool_address": "0xpair123", "contract_address": "0xtoken123", "_candles_count": 168,
            "target_price": 0.0175, "target_price_source": "7D_SWING_RESISTANCE",
            "technical_tp_validation": {
                "status": "PASS", "blockers": [],
                "target_price": 0.0175,
                "target_price_source": "7D_SWING_RESISTANCE",
            },
        }
        pick, gaps = report.enforce_required_trade_fields(top, meta, "2026-11-06T00:00:00Z")
        self.assertEqual(gaps, [])
        self.assertEqual(pick["trade_plan"]["entry_price"], 0.0125)
        self.assertEqual(pick["trade_plan"]["single_take_profit"], 0.0175)
        self.assertEqual(pick["trade_plan"]["tp_upside_pct"], 40.0)
        self.assertEqual(pick["pair_address"], "0xpair123")
        self.assertEqual(pick["trade_plan"]["pair_address"], "0xpair123")
        self.assertNotIn("initial_stop_price", pick)
        self.assertNotIn("trailing_stop_price", pick)
        self.assertNotIn("stop_loss_price", pick["trade_plan"])
        self.assertNotIn("trailing_stop_price", pick["trade_plan"])
        self.assertNotIn("take_profit_2", pick["trade_plan"])
        self.assertIn("NOT A FIRM ROUTER", pick["entry_price_basis"])

    def test_incomplete_dex_pick_is_rejected_not_published(self):
        top = {
            "market_type": "dex_pool", "price": 0.0125,
            "data_fresh": True, "market_verified": True,
            "pool_address": "0xpair123", "contract_address": "0xtoken123", "trade_plan": {"single_take_profit": None},
        }
        meta = {"pool_address": "0xpair123", "contract_address": "0xtoken123", "_candles_count": 4, "target_price_source": "UNVERIFIED"}
        pick, gaps = report.enforce_required_trade_fields(top, meta)
        self.assertIsNone(pick)
        self.assertTrue(gaps)
        self.assertIn("at least 20 hourly candles", gaps)

    def test_markdown_never_prints_incomplete_pick(self):
        md = report.render_markdown({"top_rocket_pick": None}, {"coverage": {}})
        self.assertIn("PICK WITHHELD", md)
        self.assertNotIn("Entry price | UNVERIFIED", md)
        self.assertNotIn("Single take-profit | UNVERIFIED", md)


if __name__ == "__main__":
    unittest.main()
