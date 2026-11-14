import copy
from datetime import datetime, timezone
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
                "notional_size_usd": 100.0,
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

    def test_distinguishes_depth_walk_from_approximate_liquidity_heuristic(self):
        from scripts.execution_simulator import simulate_multi_size
        book = {
            "timestamp_utc": "2026-11-13T12:00:00Z",
            "bids": [[99.0, 10.0]],
            "asks": [[101.0, 10.0]],
        }
        res_book = simulate_multi_size(book, sizes_usd=[100.0], as_of_utc="2026-11-13T12:01:00Z")
        eval_book = res_book["size_evaluations"]["$100"]
        assert eval_book["simulation_method"] == "ORDER_BOOK_DEPTH_WALK"
        assert eval_book["is_depth_walk"] is True
        assert eval_book["size_verification_status"] == "SIZE_BOUND_ORDER_BOOK_WALK"

        # Router quote without explicit depth
        quote_source = {
            "dex_execution_quotes": {
                "timestamp_utc": "2026-11-13T12:00:00Z",
                "buy_quote": {"amount_out": 100},
                "sell_quote": {"amount_out": 95},
            },
            "liquidity_usd": 50_000,
        }
        res_quote = simulate_multi_size(quote_source, sizes_usd=[100.0], as_of_utc="2026-11-13T12:01:00Z")
        eval_quote = res_quote["size_evaluations"]["$100"]
        assert eval_quote["simulation_method"] == "APPROXIMATE_LIQUIDITY_HEURISTIC"
        assert eval_quote["is_depth_walk"] is False
        assert eval_quote["impact_model"] == "LINEAR_RESERVE_HEURISTIC_APPROXIMATION"
        assert "not a constant-product AMM simulation" in eval_quote["impact_model_disclaimer"]
        assert eval_quote["size_verification_status"] == "ORDER_SIZE_UNVERIFIED_ESTIMATE"
        assert eval_quote["quote_verified_for_size"] is False

    def test_malformed_and_negative_inputs_sanitized_safely(self):
        from scripts.execution_simulator import simulate_multi_size
        quote_source = {
            "dex_execution_quotes": {
                "timestamp_utc": "2026-11-13T12:00:00Z",
                "buy_quote": {"amount_out": 100},
                "sell_quote": {"amount_out": 95},
            },
            "liquidity_usd": 50_000,
        }
        # Pass malformed, negative, NaN and string values
        res = simulate_multi_size(
            quote_source,
            sizes_usd=[-500.0, 0, "invalid", 250.0, float("nan")],
            fee_bps=-10.0,
            gas_usd="not_a_number",
            as_of_utc="2026-11-13T12:01:00Z",
        )
        # Should sanitize and evaluate only the valid positive size ($250)
        assert "$250" in res["size_evaluations"]
        assert res["fee_bps"] == 0.0
        assert res["gas_usd"] is None

    def test_missing_quote_timestamp_fails_freshness_verification(self):
        from scripts.execution_simulator import simulate_multi_size
        missing_timestamp_source = {
            "dex_execution_quotes": {
                # No timestamp!
                "buy_quote": {"amount_out": 100},
                "sell_quote": {"amount_out": 95},
            },
            "liquidity_usd": 50_000,
        }
        res = simulate_multi_size(missing_timestamp_source, sizes_usd=[100.0], as_of_utc="2026-11-13T12:01:00Z")
        eval100 = res["size_evaluations"]["$100"]
        assert eval100["status"] == "UNVERIFIED_TIMESTAMP"
        assert eval100["quote_freshness"] == "MISSING_TIMESTAMP_UNVERIFIED"
        assert eval100["route_available"] is False


class AdversarialPhase2ExecutionTests(unittest.TestCase):
    """20 adversarial regression tests mandated for Rocket Hunter v4.30 Phase 2."""

    def setUp(self):
        self.as_of_utc = "2026-11-13T12:00:00Z"
        self.now_dt = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
        self.valid_quote_source = {
            "chain": "ethereum",
            "pool_address": "0xpool1111",
            "contract_address": "0xtoken2222",
            "liquidity_usd": 100_000.0,
            "dex_execution_quotes": {
                "provider": "uniswap_v3",
                "timestamp_utc": "2026-11-13T12:00:00Z",
                "chain": "ethereum",
                "pool_address": "0xpool1111",
                "notional_size_usd": 100.0,
                "buy_quote": {
                    "direction": "BUY",
                    "in_token": "0xusdc",
                    "out_token": "0xtoken2222",
                    "in_amount": 100.0,
                    "out_amount": 50.0,
                    "notional_size_usd": 100.0,
                },
                "sell_quote": {
                    "direction": "SELL",
                    "in_token": "0xtoken2222",
                    "out_token": "0xusdc",
                    "in_amount": 50.0,
                    "out_amount": 98.0,
                    "notional_size_usd": 100.0,
                },
            },
        }

    # 1. Future-dated quotes
    def test_01_future_dated_quotes_rejected(self):
        from scripts.execution_simulator import simulate_multi_size
        src = copy.deepcopy(self.valid_quote_source)
        src["dex_execution_quotes"]["timestamp_utc"] = "2026-11-13T12:05:00Z"  # 5 min in future!
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc)
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["quote_freshness"], "FUTURE_DATED_QUOTE")
        self.assertEqual(ev["status"], "FUTURE_DATED_QUOTE")

    # 2. Missing timestamps
    def test_02_missing_timestamps_rejected(self):
        from scripts.execution_simulator import simulate_multi_size
        src = copy.deepcopy(self.valid_quote_source)
        del src["dex_execution_quotes"]["timestamp_utc"]
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc)
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["quote_freshness"], "MISSING_TIMESTAMP_UNVERIFIED")

        # Also test when as_of_utc is None (cannot verify freshness without evaluation timestamp)
        res_no_eval = simulate_multi_size(self.valid_quote_source, sizes_usd=[100.0], as_of_utc=None)
        ev_no_eval = res_no_eval["size_evaluations"]["$100"]
        self.assertFalse(ev_no_eval["route_available"])
        self.assertEqual(ev_no_eval["quote_freshness"], "INVALID_EVALUATION_TIMESTAMP")

    # 3. Invalid timestamps
    def test_03_invalid_timestamps_rejected(self):
        from scripts.execution_simulator import simulate_multi_size
        src = copy.deepcopy(self.valid_quote_source)
        src["dex_execution_quotes"]["timestamp_utc"] = "not-a-valid-timestamp"
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc)
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["quote_freshness"], "INVALID_QUOTE_TIMESTAMP")

    # 4. Naive timestamps without timezone
    def test_04_naive_timestamps_without_timezone_rejected(self):
        from scripts.execution_simulator import simulate_multi_size
        src = copy.deepcopy(self.valid_quote_source)
        src["dex_execution_quotes"]["timestamp_utc"] = "2026-11-13 12:00:00"  # Missing timezone offset or Z
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc)
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["quote_freshness"], "NAIVE_TIMESTAMP_REJECTED")

    # 5. Stale quotes
    def test_05_stale_quotes_rejected(self):
        from scripts.execution_simulator import simulate_multi_size
        src = copy.deepcopy(self.valid_quote_source)
        src["dex_execution_quotes"]["timestamp_utc"] = "2026-11-13T11:50:00Z"  # 10 min old (> 300s)
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, max_quote_age_seconds=300.0)
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["quote_freshness"], "STALE")
        self.assertEqual(ev["status"], "STALE_QUOTE")

    # 6. Buy quote without sell quote (honeypot pattern)
    def test_06_buy_quote_without_sell_quote_rejected(self):
        from scripts.execution_simulator import simulate_multi_size
        src = copy.deepcopy(self.valid_quote_source)
        src["dex_execution_quotes"]["sell_quote"] = None
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc)
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "MISSING_SELL_ROUTE")
        self.assertEqual(ev["sell"]["status"], "UNAVAILABLE")

    # 7. Empty sell quote
    def test_07_empty_sell_quote_rejected(self):
        from scripts.execution_simulator import simulate_multi_size
        src = copy.deepcopy(self.valid_quote_source)
        src["dex_execution_quotes"]["sell_quote"] = {}
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc)
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "MISSING_SELL_ROUTE")

    # 8. Zero or negative output amounts
    def test_08_zero_or_negative_output_amounts_rejected(self):
        from scripts.execution_simulator import simulate_multi_size
        # Zero buy out
        src_zero = copy.deepcopy(self.valid_quote_source)
        src_zero["dex_execution_quotes"]["buy_quote"]["out_amount"] = 0.0
        res_zero = simulate_multi_size(src_zero, sizes_usd=[100.0], as_of_utc=self.as_of_utc)
        self.assertFalse(res_zero["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_zero["size_evaluations"]["$100"]["status"], "INVALID_BUY_QUOTE_AMOUNTS")

        # Negative sell out
        src_neg = copy.deepcopy(self.valid_quote_source)
        src_neg["dex_execution_quotes"]["sell_quote"]["out_amount"] = -5.0
        res_neg = simulate_multi_size(src_neg, sizes_usd=[100.0], as_of_utc=self.as_of_utc)
        self.assertFalse(res_neg["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_neg["size_evaluations"]["$100"]["status"], "INVALID_SELL_QUOTE_AMOUNTS")

    # 9. NaN and infinite amounts
    def test_09_nan_and_infinite_amounts_rejected(self):
        from scripts.execution_simulator import simulate_multi_size
        src_nan = copy.deepcopy(self.valid_quote_source)
        src_nan["dex_execution_quotes"]["buy_quote"]["out_amount"] = float("nan")
        res_nan = simulate_multi_size(src_nan, sizes_usd=[100.0], as_of_utc=self.as_of_utc)
        self.assertFalse(res_nan["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_nan["size_evaluations"]["$100"]["status"], "INVALID_BUY_QUOTE_AMOUNTS")

        src_inf = copy.deepcopy(self.valid_quote_source)
        src_inf["dex_execution_quotes"]["sell_quote"]["out_amount"] = float("inf")
        res_inf = simulate_multi_size(src_inf, sizes_usd=[100.0], as_of_utc=self.as_of_utc)
        self.assertFalse(res_inf["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_inf["size_evaluations"]["$100"]["status"], "INVALID_SELL_QUOTE_AMOUNTS")

    # 10. Incorrect token addresses
    def test_10_incorrect_token_addresses_rejected(self):
        from scripts.execution_simulator import simulate_multi_size
        src = copy.deepcopy(self.valid_quote_source)
        src["dex_execution_quotes"]["buy_quote"]["out_token"] = "0xWRONG_TOKEN"
        res = simulate_multi_size(
            src, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address="0xtoken2222"
        )
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "TOKEN_ADDRESS_MISMATCH")

    # 11. Incorrect chain or pool identity
    def test_11_incorrect_chain_or_pool_identity_rejected(self):
        from scripts.execution_simulator import simulate_multi_size
        # Chain mismatch
        src_chain = copy.deepcopy(self.valid_quote_source)
        src_chain["dex_execution_quotes"]["chain"] = "bsc"
        res_chain = simulate_multi_size(
            src_chain, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_chain="ethereum"
        )
        self.assertFalse(res_chain["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_chain["size_evaluations"]["$100"]["status"], "CHAIN_MISMATCH")

        # Pool mismatch
        src_pool = copy.deepcopy(self.valid_quote_source)
        src_pool["dex_execution_quotes"]["pool_address"] = "0xWRONG_POOL"
        res_pool = simulate_multi_size(
            src_pool, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_pool="0xpool1111"
        )
        self.assertFalse(res_pool["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_pool["size_evaluations"]["$100"]["status"], "POOL_MISMATCH")

    # 12. Buy/sell direction mismatch
    def test_12_buy_sell_direction_mismatch_rejected(self):
        from scripts.execution_simulator import simulate_multi_size
        src = copy.deepcopy(self.valid_quote_source)
        src["dex_execution_quotes"]["buy_quote"]["direction"] = "SELL"  # Inverted direction
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc)
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "DIRECTION_MISMATCH")

    # 13. Quote-size mismatch
    def test_13_quote_size_mismatch_rejected(self):
        from scripts.execution_simulator import simulate_multi_size
        # Quoted for $100, but evaluated for $500
        src = copy.deepcopy(self.valid_quote_source)
        res = simulate_multi_size(src, sizes_usd=[100.0, 500.0], as_of_utc=self.as_of_utc)
        # $100 is verified
        self.assertTrue(res["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res["size_evaluations"]["$100"]["status"], "EXECUTABLE_ROUTE_AVAILABLE")
        # $500 is not verified
        self.assertFalse(res["size_evaluations"]["$500"]["route_available"])
        self.assertFalse(res["size_evaluations"]["$500"]["quote_verified_for_size"])
        self.assertNotEqual(res["size_evaluations"]["$500"]["status"], "EXECUTABLE_ROUTE_AVAILABLE")

    # 14. Quote-size tolerance boundary
    def test_14_quote_size_tolerance_boundary(self):
        from scripts.execution_simulator import simulate_multi_size
        # 1.0% boundary test
        src_boundary_in = copy.deepcopy(self.valid_quote_source)
        src_boundary_in["dex_execution_quotes"]["notional_size_usd"] = 100.9  # 0.9% error <= 1.0%
        src_boundary_in["dex_execution_quotes"]["buy_quote"]["notional_size_usd"] = 100.9
        src_boundary_in["dex_execution_quotes"]["buy_quote"]["in_amount"] = 100.9
        src_boundary_in["dex_execution_quotes"]["sell_quote"]["notional_size_usd"] = 100.9
        res_in = simulate_multi_size(src_boundary_in, sizes_usd=[100.0], as_of_utc=self.as_of_utc, size_tolerance_pct=1.0)
        self.assertTrue(res_in["size_evaluations"]["$100"]["route_available"])
        self.assertTrue(res_in["size_evaluations"]["$100"]["quote_verified_for_size"])

        src_boundary_out = copy.deepcopy(self.valid_quote_source)
        src_boundary_out["dex_execution_quotes"]["notional_size_usd"] = 101.5  # 1.5% error > 1.0%
        src_boundary_out["dex_execution_quotes"]["buy_quote"]["notional_size_usd"] = 101.5
        src_boundary_out["dex_execution_quotes"]["buy_quote"]["in_amount"] = 101.5
        src_boundary_out["dex_execution_quotes"]["sell_quote"]["notional_size_usd"] = 101.5
        res_out = simulate_multi_size(src_boundary_out, sizes_usd=[100.0], as_of_utc=self.as_of_utc, size_tolerance_pct=1.0)
        self.assertFalse(res_out["size_evaluations"]["$100"]["route_available"])
        self.assertFalse(res_out["size_evaluations"]["$100"]["quote_verified_for_size"])
        self.assertEqual(res_out["size_evaluations"]["$100"]["status"], "ORDER_SIZE_MISMATCH")

    # 15. Insufficient sell-side liquidity
    def test_15_insufficient_sell_side_liquidity(self):
        from scripts.execution_simulator import simulate
        book = {
            "timestamp_utc": self.as_of_utc,
            "bids": [[99.0, 0.1]],  # Only $9.90 of bid depth! Cannot absorb selling the purchased asset!
            "asks": [[101.0, 50.0]],  # Abundant ask depth ($5050)
        }
        res = simulate(book, buy_notional_usd=100.0)
        self.assertEqual(res["status"], "INSUFFICIENT_DISPLAYED_DEPTH")
        self.assertTrue(res["buy"]["complete"])
        self.assertFalse(res["immediate_exit"]["complete"])

    # 16. Insufficient buy-side liquidity
    def test_16_insufficient_buy_side_liquidity(self):
        from scripts.execution_simulator import simulate
        book = {
            "timestamp_utc": self.as_of_utc,
            "bids": [[99.0, 50.0]],  # Abundant bid depth
            "asks": [[101.0, 0.1]],  # Only $10.10 of ask depth! Cannot fill $100 buy!
        }
        res = simulate(book, buy_notional_usd=100.0)
        self.assertEqual(res["status"], "INSUFFICIENT_DISPLAYED_DEPTH")
        self.assertFalse(res["buy"]["complete"])

    # 17. Missing AMM reserves
    def test_17_missing_amm_reserves_fails_closed(self):
        from scripts.execution_simulator import simulate_constant_product_amm, simulate_multi_size
        with self.assertRaisesRegex(ValueError, "AMM reserves must be positive and finite"):
            simulate_constant_product_amm(reserve_base=0.0, reserve_quote=50000.0, buy_notional_usd=100.0)

        with self.assertRaisesRegex(ValueError, "AMM reserves must be positive and finite"):
            simulate_constant_product_amm(reserve_base=-100.0, reserve_quote=50000.0, buy_notional_usd=100.0)

        # In simulate_multi_size with invalid reserves
        src_amm = {
            "amm_reserves": {"reserve_base": 0.0, "reserve_quote": 50000.0, "timestamp_utc": self.as_of_utc},
        }
        res = simulate_multi_size(src_amm, sizes_usd=[100.0], as_of_utc=self.as_of_utc)
        self.assertFalse(res["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res["size_evaluations"]["$100"]["status"], "INVALID_AMM_RESERVES")

    # 18. Incorrect pool fee data
    def test_18_incorrect_pool_fee_data_rejected(self):
        from scripts.execution_simulator import simulate_constant_product_amm
        with self.assertRaisesRegex(ValueError, "fee_bps must be non-negative and finite"):
            simulate_constant_product_amm(reserve_base=10000.0, reserve_quote=50000.0, buy_notional_usd=100.0, fee_bps=-10.0)

        with self.assertRaisesRegex(ValueError, "fee_bps must be non-negative and finite"):
            simulate_constant_product_amm(reserve_base=10000.0, reserve_quote=50000.0, buy_notional_usd=100.0, fee_bps=float("nan"))

    # 19. Heuristic incorrectly labelled as verified prevented
    def test_19_heuristic_incorrectly_labelled_as_verified_prevented(self):
        from scripts.execution_simulator import simulate_multi_size
        # Only liquidity_usd supplied; no order book, no AMM reserves, no router quotes
        heuristic_src = {
            "liquidity_usd": 100_000.0,
            "final_price_timestamp_utc": self.as_of_utc,
        }
        res = simulate_multi_size(heuristic_src, sizes_usd=[100.0], as_of_utc=self.as_of_utc)
        ev = res["size_evaluations"]["$100"]
        self.assertEqual(ev["simulation_method"], "APPROXIMATE_LIQUIDITY_HEURISTIC")
        self.assertFalse(ev["route_available"])
        self.assertFalse(ev["quote_verified_for_size"])
        self.assertNotEqual(ev["status"], "EXECUTABLE_ROUTE_AVAILABLE")

    # 20. Production gate rejecting an unverified execution result
    def test_20_production_gate_rejects_unverified_execution(self):
        from scripts import v425_early_breakout as v425
        cand = {
            "market_type": "dex_pool",
            "chain": "ethereum",
            "pool_address": "0xpool1111",
            "contract_address": "0xtoken2222",
            "final_pool_address": "0xpool1111",
            "final_token_contract": "0xtoken2222",
            "final_quote_chain": "ethereum",
            "final_quote_token_orientation_verified": True,
            "final_price_usd": 100.0,
            "final_price_timestamp_utc": self.as_of_utc,
            "final_price_verified": True,
            "target_price": 140.0,
            "target_price_source": "7D_SWING_RESISTANCE",
            "intended_trade_size_usd": 100.0,
            "liquidity_usd": 100_000.0,
            "technical_tp_validation": {
                "status": "PASS",
                "chain": "ethereum",
                "pool_address": "0xpool1111",
                "token_address": "0xtoken2222",
                "token_orientation_verified": True,
                "target_price": 140.0,
                "target_price_source": "7D_SWING_RESISTANCE",
                "current_price": 100.0,
                "atr_1h_pct": 10.0,
                "maximum_tp_distance_pct": 40.0,
                "candle_retrieved_utc": self.as_of_utc,
            },
            "trade_plan": {"single_take_profit": 140.0},
            "dex_loss_guard_status": "PASS",
            "dex_loss_guard_evidence": {
                "status": "PASS",
                "checks": {
                    "exact_token_and_pool_identity": True,
                    "launch_wallet_analysis": True,
                    "lp_position_audit": True,
                    "token_security_audit": True,
                    "two_way_route_quotes": True,
                },
            },
            "safety_verified": True,
            "executable_route_verified": True,
            # Quote size mismatch: quote is for $500, but intended trade size is $100!
            "dex_execution_quotes": {
                "provider": "uniswap_v3",
                "notional_size_usd": 500.0,
                "timestamp_utc": self.as_of_utc,
                "buy_quote": {"amount_out": 5, "notional_size_usd": 500.0},
                "sell_quote": {"amount_out": 490, "notional_size_usd": 500.0},
            },
        }

        # Must fail closed due to size mismatch between quote and intended trade size
        qualified = v425.qualify_final_actionable_candidates([cand], self.now_dt)
        self.assertEqual(len(qualified), 0)
        self.assertIn("execution quote is not verified for intended trade size $100", cand["qualification_blockers"])


if __name__ == "__main__":
    unittest.main()
