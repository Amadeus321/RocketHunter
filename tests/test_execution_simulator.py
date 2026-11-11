import unittest

from scripts.execution_simulator import simulate


class ExecutionSimulatorTests(unittest.TestCase):
    def test_simulates_round_trip_without_enabling_execution(self):
        result = simulate({
            "timestamp_utc": "2026-11-09T12:00:00Z",
            "bids": [[99.0, 2.0], [98.0, 2.0]],
            "asks": [[101.0, 2.0], [102.0, 2.0]],
        }, buy_notional_usd=100.0, fee_bps=10.0)

        self.assertEqual(result["status"], "FULL_ROUND_TRIP_DEPTH_AVAILABLE")
        self.assertFalse(result["execution_enabled"])
        self.assertEqual(result["snapshot"], "2026-11-09T12:00:00Z")
        self.assertTrue(result["buy"]["complete"])
        self.assertTrue(result["immediate_exit"]["complete"])
        self.assertLess(
            result["round_trip_estimate"]["net_pnl_usd"],
            result["round_trip_estimate"]["gross_pnl_usd"],
        )

    def test_reports_insufficient_displayed_depth(self):
        result = simulate({
            "bids": [[99.0, 0.1]],
            "asks": [[101.0, 0.25]],
        }, buy_notional_usd=100.0)

        self.assertEqual(result["status"], "INSUFFICIENT_DISPLAYED_DEPTH")
        self.assertFalse(result["buy"]["complete"])
        self.assertFalse(result["immediate_exit"]["complete"])
        self.assertEqual(result["immediate_exit"]["remaining_quantity"], 0.15)

    def test_rejects_invalid_notional_and_crossed_books(self):
        book = {"bids": [[100.0, 1.0]], "asks": [[101.0, 1.0]]}
        with self.assertRaisesRegex(ValueError, "must be positive and finite"):
            simulate(book, buy_notional_usd=0)

        with self.assertRaisesRegex(ValueError, "must be non-negative and finite"):
            simulate(book, buy_notional_usd=10, fee_bps=-1)

        with self.assertRaisesRegex(ValueError, "crossed or locked book"):
            simulate({"bids": [[101.0, 1.0]], "asks": [[101.0, 1.0]]}, 10)


if __name__ == "__main__":
    unittest.main()
