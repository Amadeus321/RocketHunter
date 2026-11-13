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

    def test_multi_size_simulation_scales_price_impact_across_notional_sizes(self):
        from scripts.execution_simulator import simulate_multi_size
        book = {
            "timestamp_utc": "2026-11-13T12:00:00Z",
            # Book has 2 units ($202) at best ask, so $100 stays at best ask, but $500 and $5,000 walk the book
            "bids": [[99.0, 2.0], [98.0, 20.0], [95.0, 30.0]],
            "asks": [[101.0, 2.0], [102.0, 20.0], [105.0, 30.0]],
        }
        res = simulate_multi_size(book, sizes_usd=[100.0, 500.0, 5000.0], as_of_utc="2026-11-13T12:01:00Z")
        evals = res["size_evaluations"]

        assert "$100" in evals
        assert "$500" in evals
        assert "$5000" in evals

        # $100 fills completely with zero impact vs best ask
        assert evals["$100"]["status"] == "EXECUTABLE_DEPTH_AVAILABLE"
        assert evals["$100"]["buy"]["price_impact_bps"] == 0.0

        # $500 fills completely but walks book to 102.0
        assert evals["$500"]["status"] == "EXECUTABLE_DEPTH_AVAILABLE"
        assert evals["$500"]["buy"]["price_impact_bps"] > 0.0

        # $5,000 walks book even further and has higher price impact than $500
        assert evals["$5000"]["buy"]["price_impact_bps"] > evals["$500"]["buy"]["price_impact_bps"]

    def test_multi_size_detects_stale_quotes(self):
        from scripts.execution_simulator import simulate_multi_size
        stale_source = {
            "dex_execution_quotes": {
                "timestamp_utc": "2026-11-13T11:00:00Z",  # 1 hour old!
                "buy_quote": {"amount_out": 100},
                "sell_quote": {"amount_out": 95},
            },
            "liquidity_usd": 100_000,
        }
        res = simulate_multi_size(stale_source, sizes_usd=[100.0], as_of_utc="2026-11-13T12:05:00Z", max_quote_age_seconds=300)
        assert res["size_evaluations"]["$100"]["status"] == "STALE_QUOTE"
        assert res["size_evaluations"]["$100"]["route_available"] is False

    def test_buy_quote_does_not_prove_sellability(self):
        from scripts.execution_simulator import simulate_multi_size
        honeypot_source = {
            "dex_execution_quotes": {
                "timestamp_utc": "2026-11-13T12:00:00Z",
                "buy_quote": {"amount_out": 1000},
                "sell_quote": None,  # Honeypot: buy is possible, sell route missing!
            },
            "liquidity_usd": 100_000,
        }
        res = simulate_multi_size(honeypot_source, sizes_usd=[100.0], as_of_utc="2026-11-13T12:01:00Z")
        assert res["size_evaluations"]["$100"]["status"] == "MISSING_SELL_ROUTE"
        assert res["size_evaluations"]["$100"]["route_available"] is False
        assert res["size_evaluations"]["$100"]["sell"]["status"] == "UNAVAILABLE"

    def test_insufficient_liquidity_blocks_large_notional_size(self):
        from scripts.execution_simulator import simulate_multi_size
        thin_liquidity_source = {
            "dex_execution_quotes": {
                "timestamp_utc": "2026-11-13T12:00:00Z",
                "buy_quote": {"amount_out": 100},
                "sell_quote": {"amount_out": 95},
            },
            "liquidity_usd": 2_000,  # Only $2,000 total liquidity
        }
        res = simulate_multi_size(thin_liquidity_source, sizes_usd=[100.0, 5000.0], as_of_utc="2026-11-13T12:01:00Z")
        # $100 is < half liquidity
        assert res["size_evaluations"]["$100"]["status"] == "EXECUTABLE_ROUTE_AVAILABLE"
        # $5,000 exceeds available pool liquidity!
        assert res["size_evaluations"]["$5000"]["status"] == "INSUFFICIENT_LIQUIDITY"
        assert res["size_evaluations"]["$5000"]["route_available"] is False


if __name__ == "__main__":
    unittest.main()
