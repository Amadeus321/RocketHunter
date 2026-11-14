import copy
from datetime import datetime, timezone
import math
import unittest

from scripts.execution_simulator import (
    simulate,
    simulate_multi_size,
    simulate_constant_product_amm,
    validate_fee_bps,
    validate_two_way_quotes,
    validate_timestamp_freshness,
)


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
            "timestamp_utc": "2026-11-09T12:00:00Z",
            "bids": [[99.0, 0.1]],
            "asks": [[101.0, 0.25]],
        }, buy_notional_usd=100.0)

        self.assertEqual(result["status"], "INSUFFICIENT_DISPLAYED_DEPTH")
        self.assertFalse(result["buy"]["complete"])
        self.assertFalse(result["immediate_exit"]["complete"])
        self.assertEqual(result["immediate_exit"]["remaining_quantity"], 0.15)

    def test_rejects_invalid_notional_and_crossed_books(self):
        book = {"timestamp_utc": "2026-11-09T12:00:00Z", "bids": [[100.0, 1.0]], "asks": [[101.0, 1.0]]}
        with self.assertRaisesRegex(ValueError, "must be positive and finite"):
            simulate(book, buy_notional_usd=0)

        with self.assertRaises(ValueError):
            simulate(book, buy_notional_usd=10, fee_bps=-1)

        with self.assertRaisesRegex(ValueError, "crossed or locked book"):
            simulate({"timestamp_utc": "2026-11-09T12:00:00Z", "bids": [[101.0, 1.0]], "asks": [[101.0, 1.0]]}, 10)

    def test_multi_size_simulation_scales_price_impact_across_notional_sizes(self):
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
        stale_source = {
            "chain": "ethereum",
            "pool_address": "0xpool1111",
            "contract_address": "0xtoken2222",
            "dex_execution_quotes": {
                "timestamp_utc": "2026-11-13T11:00:00Z",  # 1 hour old
                "chain": "ethereum",
                "pool_address": "0xpool1111",
                "buy_quote": {
                    "direction": "BUY",
                    "chain": "ethereum",
                    "pool_address": "0xpool1111",
                    "timestamp_utc": "2026-11-13T11:00:00Z",
                    "in_token": "0xusdc",
                    "out_token": "0xtoken2222",
                    "in_amount": 100.0,
                    "amount_out": 100.0,
                    "notional_size_usd": 100.0,
                },
                "sell_quote": {
                    "direction": "SELL",
                    "chain": "ethereum",
                    "pool_address": "0xpool1111",
                    "timestamp_utc": "2026-11-13T11:00:00Z",
                    "in_token": "0xtoken2222",
                    "out_token": "0xusdc",
                    "in_amount": 100.0,
                    "amount_out": 95.0,
                    "notional_size_usd": 100.0,
                },
            },
            "liquidity_usd": 100_000,
        }
        res = simulate_multi_size(stale_source, sizes_usd=[100.0], as_of_utc="2026-11-13T12:05:00Z", max_quote_age_seconds=300)
        assert res["size_evaluations"]["$100"]["status"] == "STALE_QUOTE"
        assert res["size_evaluations"]["$100"]["route_available"] is False

    def test_buy_quote_does_not_prove_sellability(self):
        honeypot_source = {
            "chain": "ethereum",
            "pool_address": "0xpool1111",
            "contract_address": "0xtoken2222",
            "dex_execution_quotes": {
                "timestamp_utc": "2026-11-13T12:00:00Z",
                "chain": "ethereum",
                "pool_address": "0xpool1111",
                "buy_quote": {
                    "direction": "BUY",
                    "chain": "ethereum",
                    "pool_address": "0xpool1111",
                    "timestamp_utc": "2026-11-13T12:00:00Z",
                    "in_token": "0xusdc",
                    "out_token": "0xtoken2222",
                    "in_amount": 100.0,
                    "amount_out": 1000.0,
                    "notional_size_usd": 100.0,
                },
                "sell_quote": None,  # Honeypot: buy is possible, sell route missing!
            },
            "liquidity_usd": 100_000,
        }
        res = simulate_multi_size(honeypot_source, sizes_usd=[100.0], as_of_utc="2026-11-13T12:01:00Z")
        assert res["size_evaluations"]["$100"]["status"] == "MISSING_SELL_ROUTE"
        assert res["size_evaluations"]["$100"]["route_available"] is False
        assert res["size_evaluations"]["$100"]["sell"]["status"] == "UNAVAILABLE"

    def test_insufficient_liquidity_blocks_large_notional_size(self):
        thin_liquidity_source = {
            "chain": "ethereum",
            "pool_address": "0xpool1111",
            "contract_address": "0xtoken2222",
            "dex_execution_quotes": {
                "timestamp_utc": "2026-11-13T12:00:00Z",
                "chain": "ethereum",
                "pool_address": "0xpool1111",
                "notional_size_usd": 100.0,
                "buy_quote": {
                    "direction": "BUY",
                    "chain": "ethereum",
                    "pool_address": "0xpool1111",
                    "timestamp_utc": "2026-11-13T12:00:00Z",
                    "in_token": "0xusdc",
                    "out_token": "0xtoken2222",
                    "in_amount": 100.0,
                    "amount_out": 100.0,
                    "notional_size_usd": 100.0,
                },
                "sell_quote": {
                    "direction": "SELL",
                    "chain": "ethereum",
                    "pool_address": "0xpool1111",
                    "timestamp_utc": "2026-11-13T12:00:00Z",
                    "in_token": "0xtoken2222",
                    "out_token": "0xusdc",
                    "in_amount": 100.0,
                    "amount_out": 95.0,
                    "notional_size_usd": 100.0,
                },
            },
            "liquidity_usd": 2_000,  # Only $2,000 total liquidity
        }
        res = simulate_multi_size(thin_liquidity_source, sizes_usd=[100.0, 5000.0], as_of_utc="2026-11-13T12:01:00Z", expected_token_address="0xtoken2222")
        # $100 is < half liquidity
        assert res["size_evaluations"]["$100"]["status"] == "EXECUTABLE_ROUTE_AVAILABLE"
        # $5,000 exceeds available pool liquidity!
        assert res["size_evaluations"]["$5000"]["status"] == "INSUFFICIENT_LIQUIDITY"
        assert res["size_evaluations"]["$5000"]["route_available"] is False

    def test_distinguishes_depth_walk_from_approximate_liquidity_heuristic(self):
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

        # Only liquidity_usd supplied; no order book, no AMM reserves, no router quotes
        heuristic_source = {
            "liquidity_usd": 50_000,
        }
        res_heuristic = simulate_multi_size(heuristic_source, sizes_usd=[100.0], as_of_utc="2026-11-13T12:01:00Z")
        eval_quote = res_heuristic["size_evaluations"]["$100"]
        assert eval_quote["simulation_method"] == "APPROXIMATE_LIQUIDITY_HEURISTIC"
        assert eval_quote["is_depth_walk"] is False
        assert eval_quote["impact_model"] == "LINEAR_RESERVE_HEURISTIC_APPROXIMATION"
        assert "not a constant-product AMM simulation" in eval_quote["impact_model_disclaimer"]
        assert eval_quote["size_verification_status"] == "ORDER_SIZE_UNVERIFIED_ESTIMATE"
        assert eval_quote["quote_verified_for_size"] is False

    def test_malformed_and_negative_inputs_sanitized_safely(self):
        # Invalid fee fails closed immediately with INVALID_FEE_DATA
        quote_source = {
            "liquidity_usd": 50_000,
        }
        res_invalid_fee = simulate_multi_size(
            quote_source,
            sizes_usd=[250.0],
            fee_bps=-10.0,
            as_of_utc="2026-11-13T12:01:00Z",
        )
        assert res_invalid_fee["status"] == "INVALID_FEE_DATA"
        assert res_invalid_fee["fee_bps"] is None
        assert res_invalid_fee["size_evaluations"]["$250"]["route_available"] is False

        # When fee is valid, invalid sizes are sanitized and only valid positive size ($250) is evaluated
        res_valid_fee = simulate_multi_size(
            quote_source,
            sizes_usd=[-500.0, 0, "invalid", 250.0, float("nan")],
            fee_bps=30.0,
            gas_usd="not_a_number",
            as_of_utc="2026-11-13T12:01:00Z",
        )
        assert "$250" in res_valid_fee["size_evaluations"]
        assert len(res_valid_fee["size_evaluations"]) == 1
        assert res_valid_fee["fee_bps"] == 30.0
        assert res_valid_fee["gas_usd"] is None

    def test_missing_quote_timestamp_fails_freshness_verification(self):
        missing_timestamp_source = {
            "chain": "ethereum",
            "pool_address": "0xpool1111",
            "contract_address": "0xtoken2222",
            "dex_execution_quotes": {
                # No timestamps on quotes!
                "chain": "ethereum",
                "pool_address": "0xpool1111",
                "buy_quote": {"direction": "BUY", "chain": "ethereum", "pool_address": "0xpool1111", "in_token": "0xusdc", "out_token": "0xtoken2222", "in_amount": 100.0, "amount_out": 100.0, "notional_size_usd": 100.0},
                "sell_quote": {"direction": "SELL", "chain": "ethereum", "pool_address": "0xpool1111", "in_token": "0xtoken2222", "out_token": "0xusdc", "in_amount": 100.0, "amount_out": 95.0, "notional_size_usd": 100.0},
            },
            "liquidity_usd": 50_000,
        }
        res = simulate_multi_size(missing_timestamp_source, sizes_usd=[100.0], as_of_utc="2026-11-13T12:01:00Z", expected_token_address="0xtoken2222")
        eval100 = res["size_evaluations"]["$100"]
        assert eval100["status"] == "MISSING_EXECUTION_QUOTE_TIMESTAMP"
        assert eval100["quote_freshness"] == "MISSING_EXECUTION_QUOTE_TIMESTAMP"
        assert eval100["route_available"] is False


class AdversarialPhase2ExecutionTests(unittest.TestCase):
    """22 adversarial regression tests mandated for Rocket Hunter v4.30 Phase 2."""

    def setUp(self):
        self.as_of_utc = "2026-11-13T12:00:00Z"
        self.now_dt = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
        self.valid_quote_source = {
            "chain": "ethereum",
            "pool_address": "0xpool1111",
            "contract_address": "0xtoken2222",
            "intended_trade_size_usd": 100.0,
            "liquidity_usd": 100_000.0,
            "final_price_timestamp_utc": "2026-11-13T12:00:00Z",
            "dex_execution_quotes": {
                "provider": "uniswap_v3",
                "timestamp_utc": "2026-11-13T12:00:00Z",
                "chain": "ethereum",
                "pool_address": "0xpool1111",
                "notional_size_usd": 100.0,
                "buy_quote": {
                    "direction": "BUY",
                    "chain": "ethereum",
                    "pool_address": "0xpool1111",
                    "timestamp_utc": "2026-11-13T12:00:00Z",
                    "in_token": "0xusdc",
                    "out_token": "0xtoken2222",
                    "in_amount": 100.0,
                    "out_amount": 50.0,
                    "notional_size_usd": 100.0,
                },
                "sell_quote": {
                    "direction": "SELL",
                    "chain": "ethereum",
                    "pool_address": "0xpool1111",
                    "timestamp_utc": "2026-11-13T12:00:00Z",
                    "in_token": "0xtoken2222",
                    "out_token": "0xusdc",
                    "in_amount": 50.0,
                    "out_amount": 98.0,
                    "notional_size_usd": 100.0,
                },
            },
        }

    # 1. Missing buy execution-quote timestamp while final-price timestamp exists
    def test_01_missing_buy_execution_quote_timestamp_while_final_price_exists(self):
        src = copy.deepcopy(self.valid_quote_source)
        del src["dex_execution_quotes"]["buy_quote"]["timestamp_utc"]
        src["final_price_timestamp_utc"] = self.as_of_utc  # Present, but must never substitute!
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address="0xtoken2222")
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "MISSING_EXECUTION_QUOTE_TIMESTAMP")
        self.assertEqual(ev["quote_freshness"], "MISSING_EXECUTION_QUOTE_TIMESTAMP")

    # 2. Missing sell execution-quote timestamp while final-price timestamp exists
    def test_02_missing_sell_execution_quote_timestamp_while_final_price_exists(self):
        src = copy.deepcopy(self.valid_quote_source)
        del src["dex_execution_quotes"]["sell_quote"]["timestamp_utc"]
        src["final_price_timestamp_utc"] = self.as_of_utc  # Present, but must never substitute!
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address="0xtoken2222")
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "MISSING_EXECUTION_QUOTE_TIMESTAMP")
        self.assertEqual(ev["quote_freshness"], "MISSING_EXECUTION_QUOTE_TIMESTAMP")

    # 3. Buy and sell timestamps independently validated
    def test_03_buy_and_sell_timestamps_independently_validated(self):
        # Case A: Buy fresh, sell stale
        src_stale = copy.deepcopy(self.valid_quote_source)
        src_stale["dex_execution_quotes"]["sell_quote"]["timestamp_utc"] = "2026-11-13T11:50:00Z"  # 10 min old (> 300s)
        res_stale = simulate_multi_size(src_stale, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address="0xtoken2222")
        self.assertFalse(res_stale["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_stale["size_evaluations"]["$100"]["status"], "STALE_QUOTE")

        # Case B: Buy future-dated, sell fresh
        src_future = copy.deepcopy(self.valid_quote_source)
        src_future["dex_execution_quotes"]["buy_quote"]["timestamp_utc"] = "2026-11-13T12:05:00Z"  # 5 min in future
        res_future = simulate_multi_size(src_future, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address="0xtoken2222")
        self.assertFalse(res_future["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_future["size_evaluations"]["$100"]["status"], "FUTURE_DATED_QUOTE")

        # Case C: Buy naive without timezone, sell fresh
        src_naive = copy.deepcopy(self.valid_quote_source)
        src_naive["dex_execution_quotes"]["buy_quote"]["timestamp_utc"] = "2026-11-13 12:00:00"
        res_naive = simulate_multi_size(src_naive, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address="0xtoken2222")
        self.assertFalse(res_naive["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_naive["size_evaluations"]["$100"]["status"], "NAIVE_TIMESTAMP_REJECTED")

    # 4. Missing token address on each side of each quote
    def test_04_missing_token_address_on_each_side_of_each_quote(self):
        for leg, field in [
            ("buy_quote", "in_token"),
            ("buy_quote", "out_token"),
            ("sell_quote", "in_token"),
            ("sell_quote", "out_token"),
        ]:
            src = copy.deepcopy(self.valid_quote_source)
            del src["dex_execution_quotes"][leg][field]
            res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address="0xtoken2222")
            ev = res["size_evaluations"]["$100"]
            self.assertFalse(ev["route_available"])
            self.assertEqual(ev["status"], "MISSING_TOKEN_ADDRESS")

    # 5. Missing chain identity on either quote
    def test_05_missing_chain_identity_on_either_quote(self):
        for leg in ["buy_quote", "sell_quote"]:
            src = copy.deepcopy(self.valid_quote_source)
            del src["dex_execution_quotes"][leg]["chain"]
            res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address="0xtoken2222")
            ev = res["size_evaluations"]["$100"]
            self.assertFalse(ev["route_available"])
            self.assertEqual(ev["status"], "MISSING_CHAIN_IDENTITY")

    # 6. Missing pool identity on either quote where required
    def test_06_missing_pool_identity_on_either_quote_where_required(self):
        for leg in ["buy_quote", "sell_quote"]:
            src = copy.deepcopy(self.valid_quote_source)
            del src["dex_execution_quotes"][leg]["pool_address"]
            res = simulate_multi_size(
                src, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
                expected_token_address="0xtoken2222",
                expected_pool="0xpool1111",
                require_pool_identity=True,
            )
            ev = res["size_evaluations"]["$100"]
            self.assertFalse(ev["route_available"])
            self.assertEqual(ev["status"], "MISSING_POOL_IDENTITY")

    # 7. Buy and sell quotes with different chain identities
    def test_07_buy_and_sell_quotes_with_different_chain_identities(self):
        src = copy.deepcopy(self.valid_quote_source)
        src["dex_execution_quotes"]["buy_quote"]["chain"] = "ethereum"
        src["dex_execution_quotes"]["sell_quote"]["chain"] = "bsc"
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address="0xtoken2222")
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "CHAIN_MISMATCH")

    # 8. Buy and sell quotes with different pool identities
    def test_08_buy_and_sell_quotes_with_different_pool_identities(self):
        src = copy.deepcopy(self.valid_quote_source)
        src["dex_execution_quotes"]["buy_quote"]["pool_address"] = "0xpool1111"
        src["dex_execution_quotes"]["sell_quote"]["pool_address"] = "0xpool2222"
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address="0xtoken2222")
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "POOL_MISMATCH")

    # 9. Incorrect quote-asset address
    def test_09_incorrect_quote_asset_address(self):
        # Case A: Buy in_token != Sell out_token
        src_diff = copy.deepcopy(self.valid_quote_source)
        src_diff["dex_execution_quotes"]["sell_quote"]["out_token"] = "0xweth"
        res_diff = simulate_multi_size(src_diff, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address="0xtoken2222")
        self.assertFalse(res_diff["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_diff["size_evaluations"]["$100"]["status"], "QUOTE_ASSET_MISMATCH")

        # Case B: expected_quote_asset mismatch
        src_exp = copy.deepcopy(self.valid_quote_source)
        res_exp = simulate_multi_size(
            src_exp, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address="0xtoken2222",
            expected_quote_asset="0xusdt"
        )
        self.assertFalse(res_exp["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_exp["size_evaluations"]["$100"]["status"], "QUOTE_ASSET_MISMATCH")

    # 10. Incorrect token address despite matching symbol
    def test_10_incorrect_token_address_despite_matching_symbol(self):
        src = copy.deepcopy(self.valid_quote_source)
        src["symbol"] = "PEPE"
        src["dex_execution_quotes"]["symbol"] = "PEPE"
        src["dex_execution_quotes"]["buy_quote"]["symbol"] = "PEPE"
        # Wrong token address despite matching symbol "PEPE"
        src["dex_execution_quotes"]["buy_quote"]["out_token"] = "0xfakepepe"
        res = simulate_multi_size(
            src, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address="0xtoken2222"
        )
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "TOKEN_ADDRESS_MISMATCH")

    # 11. Invalid or malformed token addresses
    def test_11_invalid_or_malformed_token_addresses(self):
        for bad_addr in [" 0xtoken2222 ", "0xtoken 2222", "PEPE", "0xtoken:2222", "0xtoken/2222", ""]:
            src = copy.deepcopy(self.valid_quote_source)
            src["dex_execution_quotes"]["buy_quote"]["out_token"] = bad_addr
            res = simulate_multi_size(
                src, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
                expected_token_address="0xtoken2222"
            )
            ev = res["size_evaluations"]["$100"]
            self.assertFalse(ev["route_available"])
            self.assertIn(ev["status"], {"INVALID_TOKEN_ADDRESS", "MISSING_TOKEN_ADDRESS"})

    # 12. Missing configured intended trade size despite quote-size fields being present
    def test_12_missing_configured_intended_trade_size_despite_quote_size_present(self):
        from scripts import v425_early_breakout as v425
        cand = copy.deepcopy(self.valid_quote_source)
        # Delete configured trade size
        del cand["intended_trade_size_usd"]
        # Quote size is explicitly present in quotes
        cand["dex_execution_quotes"]["notional_size_usd"] = 100.0
        cand["dex_execution_quotes"]["buy_quote"]["notional_size_usd"] = 100.0
        cand["dex_execution_quotes"]["sell_quote"]["notional_size_usd"] = 100.0
        # Mandatory fields for candidate
        cand["market_type"] = "dex_pool"
        cand["final_price_usd"] = 100.0
        cand["target_price"] = 140.0
        cand["target_price_source"] = "7D_SWING_RESISTANCE"

        qualified = v425.qualify_final_actionable_candidates([cand], self.now_dt)
        self.assertEqual(len(qualified), 0)
        self.assertIn("intended trade size is missing or unconfigured for execution simulation", cand["qualification_blockers"])

    # 13. Zero, negative, NaN, and infinite intended trade sizes
    def test_13_zero_negative_nan_infinite_intended_trade_sizes(self):
        from scripts import v425_early_breakout as v425
        for invalid_size in [0.0, -100.0, float("nan"), float("inf"), -float("inf")]:
            cand = copy.deepcopy(self.valid_quote_source)
            cand["intended_trade_size_usd"] = invalid_size
            cand["market_type"] = "dex_pool"
            cand["final_price_usd"] = 100.0
            cand["target_price"] = 140.0
            cand["target_price_source"] = "7D_SWING_RESISTANCE"

            qualified = v425.qualify_final_actionable_candidates([cand], self.now_dt)
            self.assertEqual(len(qualified), 0)
            self.assertIn("intended trade size must be a positive finite number", cand["qualification_blockers"])

    # 14. Buy quote size matching but sell quote size mismatching
    def test_14_buy_quote_size_matching_but_sell_quote_size_mismatching(self):
        src = copy.deepcopy(self.valid_quote_source)
        src["dex_execution_quotes"]["buy_quote"]["notional_size_usd"] = 100.0  # Matches $100
        src["dex_execution_quotes"]["sell_quote"]["notional_size_usd"] = 50.0   # Mismatches $100
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address="0xtoken2222")
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertFalse(ev["quote_verified_for_size"])
        self.assertEqual(ev["status"], "ORDER_SIZE_MISMATCH")

    # 15. Exact size-tolerance boundary and values immediately outside it
    def test_15_exact_size_tolerance_boundary_and_values_immediately_outside(self):
        # 1.0% tolerance on $100 target: allowed range is exactly $99.00 to $101.00
        # Inside boundaries: $101.00 and $99.00
        src_101 = copy.deepcopy(self.valid_quote_source)
        src_101["dex_execution_quotes"]["buy_quote"]["notional_size_usd"] = 101.0
        src_101["dex_execution_quotes"]["sell_quote"]["notional_size_usd"] = 101.0
        res_101 = simulate_multi_size(src_101, sizes_usd=[100.0], as_of_utc=self.as_of_utc, size_tolerance_pct=1.0, expected_token_address="0xtoken2222")
        self.assertTrue(res_101["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_101["size_evaluations"]["$100"]["status"], "EXECUTABLE_ROUTE_AVAILABLE")

        src_99 = copy.deepcopy(self.valid_quote_source)
        src_99["dex_execution_quotes"]["buy_quote"]["notional_size_usd"] = 99.0
        src_99["dex_execution_quotes"]["sell_quote"]["notional_size_usd"] = 99.0
        res_99 = simulate_multi_size(src_99, sizes_usd=[100.0], as_of_utc=self.as_of_utc, size_tolerance_pct=1.0, expected_token_address="0xtoken2222")
        self.assertTrue(res_99["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_99["size_evaluations"]["$100"]["status"], "EXECUTABLE_ROUTE_AVAILABLE")

        # Outside boundaries: $101.01 and $98.99
        src_101_01 = copy.deepcopy(self.valid_quote_source)
        src_101_01["dex_execution_quotes"]["buy_quote"]["notional_size_usd"] = 101.01
        src_101_01["dex_execution_quotes"]["sell_quote"]["notional_size_usd"] = 101.01
        res_101_01 = simulate_multi_size(src_101_01, sizes_usd=[100.0], as_of_utc=self.as_of_utc, size_tolerance_pct=1.0, expected_token_address="0xtoken2222")
        self.assertFalse(res_101_01["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_101_01["size_evaluations"]["$100"]["status"], "ORDER_SIZE_MISMATCH")

        src_98_99 = copy.deepcopy(self.valid_quote_source)
        src_98_99["dex_execution_quotes"]["buy_quote"]["notional_size_usd"] = 98.99
        src_98_99["dex_execution_quotes"]["sell_quote"]["notional_size_usd"] = 98.99
        res_98_99 = simulate_multi_size(src_98_99, sizes_usd=[100.0], as_of_utc=self.as_of_utc, size_tolerance_pct=1.0, expected_token_address="0xtoken2222")
        self.assertFalse(res_98_99["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_98_99["size_evaluations"]["$100"]["status"], "ORDER_SIZE_MISMATCH")

    # 16. AMM reserves belonging to the wrong pool
    def test_16_amm_reserves_belonging_to_the_wrong_pool(self):
        src_amm = {
            "chain": "ethereum",
            "amm_reserves": {
                "chain": "ethereum",
                "pool_address": "0xWRONG_POOL",
                "token0": "0xtoken2222",
                "token1": "0xusdc",
                "reserve_base": 10000.0,
                "reserve_quote": 50000.0,
                "timestamp_utc": self.as_of_utc,
            },
        }
        res = simulate_multi_size(
            src_amm, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_pool="0xpool1111",
            expected_token_address="0xtoken2222",
            require_reserve_identity=True,
        )
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "AMM_POOL_MISMATCH")

    # 17. AMM reserves with wrong token identity or orientation
    def test_17_amm_reserves_with_wrong_token_identity_or_orientation(self):
        src_amm = {
            "chain": "ethereum",
            "amm_reserves": {
                "chain": "ethereum",
                "pool_address": "0xpool1111",
                "token0": "0xWRONG_TOKEN_A",
                "token1": "0xWRONG_TOKEN_B",
                "reserve_base": 10000.0,
                "reserve_quote": 50000.0,
                "timestamp_utc": self.as_of_utc,
            },
        }
        res = simulate_multi_size(
            src_amm, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_pool="0xpool1111",
            expected_token_address="0xtoken2222",
            require_reserve_identity=True,
        )
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "AMM_TOKEN_MISMATCH")

    # 18. AMM reserves with missing chain or pool evidence
    def test_18_amm_reserves_with_missing_chain_or_pool_evidence(self):
        # Missing pool
        src_no_pool = {
            "amm_reserves": {
                "chain": "ethereum",
                "token0": "0xtoken2222",
                "token1": "0xusdc",
                "reserve_base": 10000.0,
                "reserve_quote": 50000.0,
                "timestamp_utc": self.as_of_utc,
            },
        }
        res_no_pool = simulate_multi_size(
            src_no_pool, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_pool="0xpool1111",
            require_reserve_identity=True,
        )
        self.assertFalse(res_no_pool["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_no_pool["size_evaluations"]["$100"]["status"], "MISSING_AMM_POOL_IDENTITY")

        # Missing chain
        src_no_chain = {
            "amm_reserves": {
                "pool_address": "0xpool1111",
                "token0": "0xtoken2222",
                "token1": "0xusdc",
                "reserve_base": 10000.0,
                "reserve_quote": 50000.0,
                "timestamp_utc": self.as_of_utc,
            },
        }
        res_no_chain = simulate_multi_size(
            src_no_chain, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_chain="ethereum",
            require_reserve_identity=True,
        )
        self.assertFalse(res_no_chain["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_no_chain["size_evaluations"]["$100"]["status"], "MISSING_AMM_CHAIN_IDENTITY")

    # 19. Invalid fees passed through simulate_multi_size()
    def test_19_invalid_fees_passed_through_simulate_multi_size(self):
        for bad_fee in [-1.0, float("nan"), float("inf"), 10000.0, "not_numeric"]:
            res = simulate_multi_size(self.valid_quote_source, sizes_usd=[100.0], fee_bps=bad_fee, as_of_utc=self.as_of_utc)
            self.assertEqual(res["status"], "INVALID_FEE_DATA")
            self.assertIsNone(res["fee_bps"])
            ev = res["size_evaluations"]["$100"]
            self.assertFalse(ev["route_available"])
            self.assertEqual(ev["status"], "INVALID_FEE_DATA")

    # 20. Invalid fees never silently defaulted or clamped
    def test_20_invalid_fees_never_silently_defaulted_or_clamped(self):
        # Direct validate_fee_bps rejects
        with self.assertRaisesRegex(ValueError, "INVALID_FEE_DATA"):
            validate_fee_bps(-0.01)
        with self.assertRaisesRegex(ValueError, "INVALID_FEE_DATA"):
            validate_fee_bps(None)
        with self.assertRaisesRegex(ValueError, "INVALID_FEE_DATA"):
            validate_fee_bps(True)
        with self.assertRaisesRegex(ValueError, "INVALID_FEE_DATA"):
            validate_fee_bps(10000.0)

        # In simulate
        book = {"timestamp_utc": self.as_of_utc, "bids": [[99.0, 1.0]], "asks": [[101.0, 1.0]]}
        with self.assertRaisesRegex(ValueError, "INVALID_FEE_DATA"):
            simulate(book, buy_notional_usd=100.0, fee_bps=-5.0)

        # In simulate_constant_product_amm
        with self.assertRaisesRegex(ValueError, "INVALID_FEE_DATA"):
            simulate_constant_product_amm(1000.0, 1000.0, buy_notional_usd=100.0, fee_bps=-5.0)

    # 21. Production qualification rejects every unverified execution case
    def test_21_production_qualification_rejects_every_unverified_execution_case(self):
        from scripts import v425_early_breakout as v425

        base_cand = {
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
            "fee_bps": 30.0,
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
            "trade_plan": {"single_take_profit": 140.0, "intended_trade_size_usd": 100.0},
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
            "dex_execution_quotes": copy.deepcopy(self.valid_quote_source["dex_execution_quotes"]),
        }

        # Case A: Missing timestamp
        cand_no_stamp = copy.deepcopy(base_cand)
        del cand_no_stamp["dex_execution_quotes"]["buy_quote"]["timestamp_utc"]
        self.assertEqual(len(v425.qualify_final_actionable_candidates([cand_no_stamp], self.now_dt)), 0)
        self.assertIn("buy/sell quotes are stale, future-dated, or missing a timestamp", cand_no_stamp["qualification_blockers"])

        # Case B: Size mismatch ($500 quote vs $100 configured)
        cand_size_mismatch = copy.deepcopy(base_cand)
        cand_size_mismatch["dex_execution_quotes"]["buy_quote"]["notional_size_usd"] = 500.0
        cand_size_mismatch["dex_execution_quotes"]["sell_quote"]["notional_size_usd"] = 500.0
        self.assertEqual(len(v425.qualify_final_actionable_candidates([cand_size_mismatch], self.now_dt)), 0)
        self.assertIn("execution quote is not verified for intended trade size $100", cand_size_mismatch["qualification_blockers"])

        # Case C: Identity mismatch
        cand_ident_mismatch = copy.deepcopy(base_cand)
        cand_ident_mismatch["dex_execution_quotes"]["buy_quote"]["out_token"] = "0xwrongtoken"
        self.assertEqual(len(v425.qualify_final_actionable_candidates([cand_ident_mismatch], self.now_dt)), 0)
        self.assertTrue(any("execution identity verification failed" in b for b in cand_ident_mismatch["qualification_blockers"]))

        # Case D: Direction mismatch
        cand_dir = copy.deepcopy(base_cand)
        cand_dir["dex_execution_quotes"]["buy_quote"]["direction"] = "SELL"
        self.assertEqual(len(v425.qualify_final_actionable_candidates([cand_dir], self.now_dt)), 0)
        self.assertTrue(any("execution identity verification failed: DIRECTION_MISMATCH" in b for b in cand_dir["qualification_blockers"]))

        # Case E: Heuristic only (no book, no quotes, no AMM)
        cand_heur = copy.deepcopy(base_cand)
        del cand_heur["dex_execution_quotes"]
        self.assertEqual(len(v425.qualify_final_actionable_candidates([cand_heur], self.now_dt)), 0)
        self.assertTrue(any("approximate heuristic" in b or "fresh two-way execution quotes are missing" in b for b in cand_heur["qualification_blockers"]))

    # 22. Existing valid, fully evidenced scenarios continue to work
    def test_22_existing_valid_fully_evidenced_scenarios_continue_to_work(self):
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
            "fee_bps": 30.0,
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
            "trade_plan": {"single_take_profit": 140.0, "intended_trade_size_usd": 100.0},
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
            "dex_execution_quotes": copy.deepcopy(self.valid_quote_source["dex_execution_quotes"]),
        }

        qualified = v425.qualify_final_actionable_candidates([cand], self.now_dt)
        self.assertEqual(len(qualified), 1)
        self.assertEqual(qualified[0]["opportunity_status"], "QUALIFIED")
        self.assertEqual(qualified[0]["execution_simulation"]["status"], "EXECUTABLE_ROUTE_AVAILABLE")
        self.assertTrue(qualified[0]["execution_simulation"]["route_available"])
        self.assertTrue(qualified[0]["execution_simulation"]["quote_verified_for_size"])


if __name__ == "__main__":
    unittest.main()
