import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import next_day_forecast


class NextDayTP40GateTests(unittest.TestCase):
    def evaluate_target(self, target):
        stamp = "2026-11-09T10:00:00Z"
        row = {
            "market_type": "dex_pool", "asset": "Example", "pair": "EX/USDC",
            "chain": "ethereum", "contract_address": "0xtoken", "pool_address": "0xpool",
            "dex_available": True, "price": 100.0, "target_price": target,
            "data_timestamp_utc": stamp, "retrieved_utc": stamp,
        }
        return next_day_forecast.evaluate(row, stamp, max_age_minutes=45)

    def test_below_40_percent_is_never_a_buy_tier(self):
        result = self.evaluate_target(139.99)
        self.assertNotIn("BUY", result["entry_decision"])
        self.assertIn("single take-profit upside below mandatory +40.00% threshold", result["entry_blockers"])

    def test_exact_40_percent_passes_only_the_tp_gate(self):
        result = self.evaluate_target(140.0)
        self.assertNotIn("single take-profit upside below mandatory +40.00% threshold", result["entry_blockers"])
        self.assertEqual(result["tp_upside_pct"], 40.0)
        self.assertNotIn("BUY", result["entry_decision"])


if __name__ == "__main__":
    unittest.main()
