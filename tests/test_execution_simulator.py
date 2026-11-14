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
    is_valid_address,
)
from scripts import v425_early_breakout as v425

# Realistic, syntactically valid blockchain test addresses (42 chars, valid hexadecimal)
VALID_EVM_CHAIN = "ethereum"
VALID_POOL_ADDR = "0x1111111111111111111111111111111111111111"
VALID_TOKEN_ADDR = "0x2222222222222222222222222222222222222222"
VALID_QUOTE_ADDR = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"  # USDC on Ethereum
VALID_ALT_POOL = "0x9999999999999999999999999999999999999999"
VALID_ALT_TOKEN = "0x3333333333333333333333333333333333333333"
VALID_ALT_QUOTE = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"  # WETH on Ethereum

# Syntactically valid Solana test addresses (Base58, 32-44 characters)
VALID_SOLANA_CHAIN = "solana"
VALID_SOLANA_TOKEN = "So11111111111111111111111111111111111111112"
VALID_SOLANA_POOL = "7XawhbbxtsRcQA8KTkHT9f9nc6d69UwqCDh6U5EEbEmX"
VALID_SOLANA_QUOTE = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class ExecutionSimulatorTests(unittest.TestCase):
    def test_simulates_round_trip_without_enabling_execution(self):
        result = simulate({
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token_address": VALID_TOKEN_ADDR,
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
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token_address": VALID_TOKEN_ADDR,
            "timestamp_utc": "2026-11-09T12:00:00Z",
            "bids": [[99.0, 0.1]],
            "asks": [[101.0, 0.25]],
        }, buy_notional_usd=100.0)

        self.assertEqual(result["status"], "INSUFFICIENT_DISPLAYED_DEPTH")
        self.assertFalse(result["buy"]["complete"])
        self.assertFalse(result["immediate_exit"]["complete"])
        self.assertEqual(result["immediate_exit"]["remaining_quantity"], 0.15)

    def test_rejects_invalid_notional_and_crossed_books(self):
        book = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token_address": VALID_TOKEN_ADDR,
            "timestamp_utc": "2026-11-09T12:00:00Z",
            "bids": [[100.0, 1.0]],
            "asks": [[101.0, 1.0]],
        }
        with self.assertRaisesRegex(ValueError, "must be positive and finite"):
            simulate(book, buy_notional_usd=0)

        with self.assertRaises(ValueError):
            simulate(book, buy_notional_usd=10, fee_bps=-1)

        with self.assertRaisesRegex(ValueError, "crossed or locked book"):
            simulate({
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "token_address": VALID_TOKEN_ADDR,
                "timestamp_utc": "2026-11-09T12:00:00Z",
                "bids": [[101.0, 1.0]],
                "asks": [[101.0, 1.0]],
            }, 10)

    def test_multi_size_simulation_scales_price_impact_across_notional_sizes(self):
        book = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token_address": VALID_TOKEN_ADDR,
            "quote_asset": VALID_QUOTE_ADDR,
            "timestamp_utc": "2026-11-13T12:00:00Z",
            "bids": [[99.0, 2.0], [98.0, 20.0], [95.0, 30.0]],
            "asks": [[101.0, 2.0], [102.0, 20.0], [105.0, 30.0]],
        }
        res = simulate_multi_size(
            book,
            sizes_usd=[100.0, 500.0, 5000.0],
            as_of_utc="2026-11-13T12:01:00Z",
            expected_token_address=VALID_TOKEN_ADDR,
            expected_chain=VALID_EVM_CHAIN,
            expected_pool=VALID_POOL_ADDR,
            expected_quote_asset=VALID_QUOTE_ADDR,
        )
        evals = res["size_evaluations"]

        assert "$100" in evals
        assert "$500" in evals
        assert "$5000" in evals

        assert evals["$100"]["status"] == "EXECUTABLE_DEPTH_AVAILABLE"
        assert evals["$100"]["buy"]["price_impact_bps"] == 0.0

        assert evals["$500"]["status"] == "EXECUTABLE_DEPTH_AVAILABLE"
        assert evals["$500"]["buy"]["price_impact_bps"] > 0.0

        assert evals["$5000"]["buy"]["price_impact_bps"] > evals["$500"]["buy"]["price_impact_bps"]

    def test_multi_size_detects_stale_quotes(self):
        stale_source = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "dex_execution_quotes": {
                "timestamp_utc": "2026-11-13T11:00:00Z",  # 1 hour old
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "buy_quote": {
                    "direction": "BUY",
                    "chain": VALID_EVM_CHAIN,
                    "pool_address": VALID_POOL_ADDR,
                    "timestamp_utc": "2026-11-13T11:00:00Z",
                    "in_token": VALID_QUOTE_ADDR,
                    "out_token": VALID_TOKEN_ADDR,
                    "in_amount": 100.0,
                    "out_amount": 50.0,
                    "notional_size_usd": 100.0,
                },
                "sell_quote": {
                    "direction": "SELL",
                    "chain": VALID_EVM_CHAIN,
                    "pool_address": VALID_POOL_ADDR,
                    "timestamp_utc": "2026-11-13T11:00:00Z",
                    "in_token": VALID_TOKEN_ADDR,
                    "out_token": VALID_QUOTE_ADDR,
                    "in_amount": 50.0,
                    "out_amount": 95.0,
                    "notional_size_usd": 100.0,
                },
            },
            "liquidity_usd": 100_000,
        }
        res = simulate_multi_size(
            stale_source,
            sizes_usd=[100.0],
            as_of_utc="2026-11-13T12:05:00Z",
            max_quote_age_seconds=300,
            expected_token_address=VALID_TOKEN_ADDR,
            expected_chain=VALID_EVM_CHAIN,
            expected_pool=VALID_POOL_ADDR,
            expected_quote_asset=VALID_QUOTE_ADDR,
        )
        assert res["size_evaluations"]["$100"]["status"] == "STALE_QUOTE"
        assert res["size_evaluations"]["$100"]["route_available"] is False

    def test_buy_quote_does_not_prove_sellability(self):
        honeypot_source = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "dex_execution_quotes": {
                "timestamp_utc": "2026-11-13T12:00:00Z",
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "buy_quote": {
                    "direction": "BUY",
                    "chain": VALID_EVM_CHAIN,
                    "pool_address": VALID_POOL_ADDR,
                    "timestamp_utc": "2026-11-13T12:00:00Z",
                    "in_token": VALID_QUOTE_ADDR,
                    "out_token": VALID_TOKEN_ADDR,
                    "in_amount": 100.0,
                    "out_amount": 1000.0,
                    "notional_size_usd": 100.0,
                },
                "sell_quote": None,  # Honeypot: sell route missing!
            },
            "liquidity_usd": 100_000,
        }
        res = simulate_multi_size(
            honeypot_source,
            sizes_usd=[100.0],
            as_of_utc="2026-11-13T12:01:00Z",
            expected_token_address=VALID_TOKEN_ADDR,
            expected_chain=VALID_EVM_CHAIN,
            expected_pool=VALID_POOL_ADDR,
            expected_quote_asset=VALID_QUOTE_ADDR,
        )
        assert res["size_evaluations"]["$100"]["status"] == "MISSING_SELL_ROUTE"
        assert res["size_evaluations"]["$100"]["route_available"] is False
        assert res["size_evaluations"]["$100"]["sell"]["status"] == "UNAVAILABLE"

    def test_insufficient_liquidity_blocks_large_notional_size(self):
        thin_liquidity_source = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "dex_execution_quotes": {
                "timestamp_utc": "2026-11-13T12:00:00Z",
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "notional_size_usd": 100.0,
                "buy_quote": {
                    "direction": "BUY",
                    "chain": VALID_EVM_CHAIN,
                    "pool_address": VALID_POOL_ADDR,
                    "timestamp_utc": "2026-11-13T12:00:00Z",
                    "in_token": VALID_QUOTE_ADDR,
                    "out_token": VALID_TOKEN_ADDR,
                    "in_amount": 100.0,
                    "out_amount": 50.0,
                    "notional_size_usd": 100.0,
                },
                "sell_quote": {
                    "direction": "SELL",
                    "chain": VALID_EVM_CHAIN,
                    "pool_address": VALID_POOL_ADDR,
                    "timestamp_utc": "2026-11-13T12:00:00Z",
                    "in_token": VALID_TOKEN_ADDR,
                    "out_token": VALID_QUOTE_ADDR,
                    "in_amount": 50.0,
                    "out_amount": 95.0,
                    "notional_size_usd": 100.0,
                },
            },
            "liquidity_usd": 2_000,  # Only $2,000 total liquidity
        }
        res = simulate_multi_size(
            thin_liquidity_source,
            sizes_usd=[100.0, 5000.0],
            as_of_utc="2026-11-13T12:01:00Z",
            expected_token_address=VALID_TOKEN_ADDR,
            expected_chain=VALID_EVM_CHAIN,
            expected_pool=VALID_POOL_ADDR,
            expected_quote_asset=VALID_QUOTE_ADDR,
        )
        assert res["size_evaluations"]["$100"]["status"] == "EXECUTABLE_ROUTE_AVAILABLE"
        assert res["size_evaluations"]["$5000"]["status"] == "INSUFFICIENT_LIQUIDITY"
        assert res["size_evaluations"]["$5000"]["route_available"] is False

    def test_distinguishes_depth_walk_from_approximate_liquidity_heuristic(self):
        book = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token_address": VALID_TOKEN_ADDR,
            "quote_asset": VALID_QUOTE_ADDR,
            "timestamp_utc": "2026-11-13T12:00:00Z",
            "bids": [[99.0, 10.0]],
            "asks": [[101.0, 10.0]],
        }
        res_book = simulate_multi_size(
            book,
            sizes_usd=[100.0],
            as_of_utc="2026-11-13T12:01:00Z",
            expected_token_address=VALID_TOKEN_ADDR,
            expected_chain=VALID_EVM_CHAIN,
            expected_pool=VALID_POOL_ADDR,
            expected_quote_asset=VALID_QUOTE_ADDR,
        )
        eval_book = res_book["size_evaluations"]["$100"]
        assert eval_book["simulation_method"] == "ORDER_BOOK_DEPTH_WALK"
        assert eval_book["is_depth_walk"] is True
        assert eval_book["size_verification_status"] == "SIZE_BOUND_ORDER_BOOK_WALK"

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
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "dex_execution_quotes": {
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "buy_quote": {
                    "direction": "BUY", "chain": VALID_EVM_CHAIN, "pool_address": VALID_POOL_ADDR,
                    "in_token": VALID_QUOTE_ADDR, "out_token": VALID_TOKEN_ADDR,
                    "in_amount": 100.0, "out_amount": 50.0, "notional_size_usd": 100.0,
                },
                "sell_quote": {
                    "direction": "SELL", "chain": VALID_EVM_CHAIN, "pool_address": VALID_POOL_ADDR,
                    "in_token": VALID_TOKEN_ADDR, "out_token": VALID_QUOTE_ADDR,
                    "in_amount": 50.0, "out_amount": 95.0, "notional_size_usd": 100.0,
                },
            },
            "liquidity_usd": 50_000,
        }
        res = simulate_multi_size(
            missing_timestamp_source, sizes_usd=[100.0], as_of_utc="2026-11-13T12:01:00Z",
            expected_token_address=VALID_TOKEN_ADDR, expected_chain=VALID_EVM_CHAIN, expected_pool=VALID_POOL_ADDR,
            expected_quote_asset=VALID_QUOTE_ADDR,
        )
        eval100 = res["size_evaluations"]["$100"]
        assert eval100["status"] == "MISSING_EXECUTION_QUOTE_TIMESTAMP"
        assert eval100["quote_freshness"] == "MISSING_EXECUTION_QUOTE_TIMESTAMP"
        assert eval100["route_available"] is False


class AdversarialPhase2ExecutionTests(unittest.TestCase):
    """Adversarial regression tests covering all six Phase 2 execution realism requirements."""

    def setUp(self):
        self.as_of_utc = "2026-11-13T12:00:00Z"
        self.now_dt = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
        self.valid_quote_source = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "intended_trade_size_usd": 100.0,
            "quote_token_address": VALID_QUOTE_ADDR,
            "expected_quote_asset": VALID_QUOTE_ADDR,
            "liquidity_usd": 100_000.0,
            "final_price_timestamp_utc": "2026-11-13T12:00:00Z",
            "dex_execution_quotes": {
                "provider": "uniswap_v3",
                "timestamp_utc": "2026-11-13T12:00:00Z",
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "notional_size_usd": 100.0,
                "buy_quote": {
                    "direction": "BUY",
                    "chain": VALID_EVM_CHAIN,
                    "pool_address": VALID_POOL_ADDR,
                    "timestamp_utc": "2026-11-13T12:00:00Z",
                    "in_token": VALID_QUOTE_ADDR,
                    "out_token": VALID_TOKEN_ADDR,
                    "in_amount": 100.0,
                    "out_amount": 50.0,
                    "notional_size_usd": 100.0,
                },
                "sell_quote": {
                    "direction": "SELL",
                    "chain": VALID_EVM_CHAIN,
                    "pool_address": VALID_POOL_ADDR,
                    "timestamp_utc": "2026-11-13T12:00:00Z",
                    "in_token": VALID_TOKEN_ADDR,
                    "out_token": VALID_QUOTE_ADDR,
                    "in_amount": 50.0,  # Matches buy_quote out_amount exactly (50.0)
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
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "MISSING_EXECUTION_QUOTE_TIMESTAMP")
        self.assertEqual(ev["quote_freshness"], "MISSING_EXECUTION_QUOTE_TIMESTAMP")

    # 2. Missing sell execution-quote timestamp while final-price timestamp exists
    def test_02_missing_sell_execution_quote_timestamp_while_final_price_exists(self):
        src = copy.deepcopy(self.valid_quote_source)
        del src["dex_execution_quotes"]["sell_quote"]["timestamp_utc"]
        src["final_price_timestamp_utc"] = self.as_of_utc  # Present, but must never substitute!
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "MISSING_EXECUTION_QUOTE_TIMESTAMP")
        self.assertEqual(ev["quote_freshness"], "MISSING_EXECUTION_QUOTE_TIMESTAMP")

    # 3. Buy and sell timestamps independently validated
    def test_03_buy_and_sell_timestamps_independently_validated(self):
        # Case A: Buy fresh, sell stale
        src_stale = copy.deepcopy(self.valid_quote_source)
        src_stale["dex_execution_quotes"]["sell_quote"]["timestamp_utc"] = "2026-11-13T11:50:00Z"
        res_stale = simulate_multi_size(src_stale, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
        self.assertFalse(res_stale["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_stale["size_evaluations"]["$100"]["status"], "STALE_QUOTE")

        # Case B: Buy future-dated, sell fresh
        src_future = copy.deepcopy(self.valid_quote_source)
        src_future["dex_execution_quotes"]["buy_quote"]["timestamp_utc"] = "2026-11-13T12:05:00Z"
        res_future = simulate_multi_size(src_future, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
        self.assertFalse(res_future["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_future["size_evaluations"]["$100"]["status"], "FUTURE_DATED_QUOTE")

        # Case C: Buy naive without timezone, sell fresh
        src_naive = copy.deepcopy(self.valid_quote_source)
        src_naive["dex_execution_quotes"]["buy_quote"]["timestamp_utc"] = "2026-11-13 12:00:00"
        res_naive = simulate_multi_size(src_naive, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
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
            res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
            ev = res["size_evaluations"]["$100"]
            self.assertFalse(ev["route_available"])
            self.assertEqual(ev["status"], "MISSING_TOKEN_ADDRESS")

    # 5. Missing chain identity on either quote
    def test_05_missing_chain_identity_on_either_quote(self):
        for leg in ["buy_quote", "sell_quote"]:
            src = copy.deepcopy(self.valid_quote_source)
            del src["dex_execution_quotes"][leg]["chain"]
            res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
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
                expected_token_address=VALID_TOKEN_ADDR,
                expected_pool=VALID_POOL_ADDR,
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
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "CHAIN_MISMATCH")

    # 8. Buy and sell quotes with different pool identities
    def test_08_buy_and_sell_quotes_with_different_pool_identities(self):
        src = copy.deepcopy(self.valid_quote_source)
        src["dex_execution_quotes"]["buy_quote"]["pool_address"] = VALID_POOL_ADDR
        src["dex_execution_quotes"]["sell_quote"]["pool_address"] = VALID_ALT_POOL
        res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "POOL_MISMATCH")

    # 9. Expected quote asset validation and mismatch rejection
    def test_09_expected_quote_asset_validation_and_mismatch(self):
        # Case A: Buy in_token != Sell out_token
        src_diff = copy.deepcopy(self.valid_quote_source)
        src_diff["dex_execution_quotes"]["sell_quote"]["out_token"] = VALID_ALT_QUOTE
        res_diff = simulate_multi_size(src_diff, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
        self.assertFalse(res_diff["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_diff["size_evaluations"]["$100"]["status"], "QUOTE_ASSET_MISMATCH")

        # Case B: expected_quote_asset mismatch
        src_exp = copy.deepcopy(self.valid_quote_source)
        res_exp = simulate_multi_size(
            src_exp, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR,
            expected_quote_asset=VALID_ALT_QUOTE,
        )
        self.assertFalse(res_exp["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_exp["size_evaluations"]["$100"]["status"], "QUOTE_ASSET_MISMATCH")

        # Case C: Missing expected quote asset when required
        src_req = copy.deepcopy(self.valid_quote_source)
        res_req = simulate_multi_size(
            src_req, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR,
            expected_quote_asset=None,
            require_quote_asset=True,
        )
        self.assertFalse(res_req["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_req["size_evaluations"]["$100"]["status"], "MISSING_QUOTE_ASSET_EVIDENCE")

    # 10. Incorrect token address despite matching symbol
    def test_10_incorrect_token_address_despite_matching_symbol(self):
        src = copy.deepcopy(self.valid_quote_source)
        src["symbol"] = "PEPE"
        src["dex_execution_quotes"]["symbol"] = "PEPE"
        src["dex_execution_quotes"]["buy_quote"]["symbol"] = "PEPE"
        src["dex_execution_quotes"]["buy_quote"]["out_token"] = VALID_ALT_TOKEN  # Different valid hex address!
        res = simulate_multi_size(
            src, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR,
        )
        ev = res["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "TOKEN_ADDRESS_MISMATCH")

    # 11. Real blockchain address validation (reject non-hex EVM, invalid length, invalid formats)
    def test_11_real_blockchain_address_validation(self):
        # Non-hex EVM addresses
        self.assertFalse(is_valid_address("ethereum", "0xpool"))
        self.assertFalse(is_valid_address("ethereum", "0xtoken"))
        self.assertFalse(is_valid_address("ethereum", "0xtoken2222"))
        self.assertFalse(is_valid_address("ethereum", "0xpool1111"))
        self.assertFalse(is_valid_address("ethereum", "0xzzzz111111111111111111111111111111111111"))
        self.assertFalse(is_valid_address("ethereum", "PEPE"))
        # Invalid lengths
        self.assertFalse(is_valid_address("ethereum", "0x111111111111111111111111111111111111111"))  # 41 chars
        self.assertFalse(is_valid_address("ethereum", "0x11111111111111111111111111111111111111111"))  # 43 chars
        # Whitespace and punctuation
        self.assertFalse(is_valid_address("ethereum", f" {VALID_TOKEN_ADDR} "))
        self.assertFalse(is_valid_address("ethereum", f"{VALID_TOKEN_ADDR[:20]} {VALID_TOKEN_ADDR[20:]}"))
        self.assertFalse(is_valid_address("ethereum", f"{VALID_TOKEN_ADDR[:20]}:{VALID_TOKEN_ADDR[21:]}"))
        self.assertFalse(is_valid_address("ethereum", f"{VALID_TOKEN_ADDR[:20]}/{VALID_TOKEN_ADDR[21:]}"))

        # Valid EVM addresses
        self.assertTrue(is_valid_address("ethereum", VALID_TOKEN_ADDR))
        self.assertTrue(is_valid_address("base", VALID_QUOTE_ADDR))
        self.assertTrue(is_valid_address("arbitrum", VALID_POOL_ADDR))

        # Solana validation
        self.assertTrue(is_valid_address("solana", VALID_SOLANA_TOKEN))
        self.assertFalse(is_valid_address("solana", VALID_TOKEN_ADDR))  # EVM 0x rejected on Solana
        self.assertFalse(is_valid_address("solana", "too_short"))

    # 12. Mandatory explicit BUY and SELL direction
    def test_12_mandatory_explicit_swap_direction(self):
        # Non-BUY on buy quote
        for bad_dir in ["SELL", "EXACT_IN", "SWAP", "", None]:
            src = copy.deepcopy(self.valid_quote_source)
            src["dex_execution_quotes"]["buy_quote"]["direction"] = bad_dir
            res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
            ev = res["size_evaluations"]["$100"]
            self.assertFalse(ev["route_available"])
            self.assertEqual(ev["status"], "DIRECTION_MISMATCH")

        # Non-SELL on sell quote
        for bad_dir in ["BUY", "EXACT_OUT", "SWAP", "", None]:
            src = copy.deepcopy(self.valid_quote_source)
            src["dex_execution_quotes"]["sell_quote"]["direction"] = bad_dir
            res = simulate_multi_size(src, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
            ev = res["size_evaluations"]["$100"]
            self.assertFalse(ev["route_available"])
            self.assertEqual(ev["status"], "DIRECTION_MISMATCH")

    # 13. Mandatory positive and finite amounts on both quotes
    def test_13_mandatory_positive_finite_amounts(self):
        # Buy leg: zero, negative, NaN
        for bad_val in [0.0, -10.0, float("nan"), float("inf"), None]:
            src_in = copy.deepcopy(self.valid_quote_source)
            src_in["dex_execution_quotes"]["buy_quote"]["in_amount"] = bad_val
            res_in = simulate_multi_size(src_in, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
            self.assertFalse(res_in["size_evaluations"]["$100"]["route_available"])
            self.assertEqual(res_in["size_evaluations"]["$100"]["status"], "INVALID_BUY_QUOTE_AMOUNTS")

            src_out = copy.deepcopy(self.valid_quote_source)
            src_out["dex_execution_quotes"]["buy_quote"]["out_amount"] = bad_val
            res_out = simulate_multi_size(src_out, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
            self.assertFalse(res_out["size_evaluations"]["$100"]["route_available"])
            self.assertEqual(res_out["size_evaluations"]["$100"]["status"], "INVALID_BUY_QUOTE_AMOUNTS")

        # Sell leg: zero, negative, NaN
        for bad_val in [0.0, -10.0, float("nan"), float("inf"), None]:
            src_sell_in = copy.deepcopy(self.valid_quote_source)
            src_sell_in["dex_execution_quotes"]["sell_quote"]["in_amount"] = bad_val
            res_sell_in = simulate_multi_size(src_sell_in, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
            self.assertFalse(res_sell_in["size_evaluations"]["$100"]["route_available"])
            self.assertEqual(res_sell_in["size_evaluations"]["$100"]["status"], "INVALID_SELL_QUOTE_AMOUNTS")

            src_sell_out = copy.deepcopy(self.valid_quote_source)
            src_sell_out["dex_execution_quotes"]["sell_quote"]["out_amount"] = bad_val
            res_sell_out = simulate_multi_size(src_sell_out, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
            self.assertFalse(res_sell_out["size_evaluations"]["$100"]["route_available"])
            self.assertEqual(res_sell_out["size_evaluations"]["$100"]["status"], "INVALID_SELL_QUOTE_AMOUNTS")

    # 14. Round-trip token quantity reconciliation
    def test_14_round_trip_token_quantity_reconciliation(self):
        # Buy output quantity is 50.0 tokens. If sell input quantity is 10.0 (or 100.0), mismatch!
        src_mismatch = copy.deepcopy(self.valid_quote_source)
        src_mismatch["dex_execution_quotes"]["buy_quote"]["out_amount"] = 50.0
        src_mismatch["dex_execution_quotes"]["sell_quote"]["in_amount"] = 10.0
        res_mismatch = simulate_multi_size(src_mismatch, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
        ev = res_mismatch["size_evaluations"]["$100"]
        self.assertFalse(ev["route_available"])
        self.assertEqual(ev["status"], "QUANTITY_MISMATCH")

        # Exact boundary test for 0.1% quantity tolerance:
        # 50.0 * 0.001 = 0.05
        # 50.05 is exactly 0.1% -> passes!
        src_boundary_pass = copy.deepcopy(self.valid_quote_source)
        src_boundary_pass["dex_execution_quotes"]["buy_quote"]["out_amount"] = 50.0
        src_boundary_pass["dex_execution_quotes"]["sell_quote"]["in_amount"] = 50.05
        res_pass = simulate_multi_size(src_boundary_pass, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR, quantity_tolerance_pct=0.1)
        self.assertTrue(res_pass["size_evaluations"]["$100"]["route_available"])

        # 50.10 is 0.2% (> 0.1%) -> fails!
        src_boundary_fail = copy.deepcopy(self.valid_quote_source)
        src_boundary_fail["dex_execution_quotes"]["buy_quote"]["out_amount"] = 50.0
        src_boundary_fail["dex_execution_quotes"]["sell_quote"]["in_amount"] = 50.10
        res_fail = simulate_multi_size(src_boundary_fail, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR, quantity_tolerance_pct=0.1)
        self.assertFalse(res_fail["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_fail["size_evaluations"]["$100"]["status"], "QUANTITY_MISMATCH")

    # 15. Configured trade size enforcement & exact size-tolerance boundary
    def test_15_configured_trade_size_enforcement_and_boundary(self):
        # 1.0% tolerance on $100 target: allowed range is $99.00 to $101.00
        src_101 = copy.deepcopy(self.valid_quote_source)
        src_101["dex_execution_quotes"]["buy_quote"]["notional_size_usd"] = 101.0
        src_101["dex_execution_quotes"]["sell_quote"]["notional_size_usd"] = 101.0
        res_101 = simulate_multi_size(src_101, sizes_usd=[100.0], as_of_utc=self.as_of_utc, size_tolerance_pct=1.0, expected_token_address=VALID_TOKEN_ADDR)
        self.assertTrue(res_101["size_evaluations"]["$100"]["route_available"])

        src_99 = copy.deepcopy(self.valid_quote_source)
        src_99["dex_execution_quotes"]["buy_quote"]["notional_size_usd"] = 99.0
        src_99["dex_execution_quotes"]["sell_quote"]["notional_size_usd"] = 99.0
        res_99 = simulate_multi_size(src_99, sizes_usd=[100.0], as_of_utc=self.as_of_utc, size_tolerance_pct=1.0, expected_token_address=VALID_TOKEN_ADDR)
        self.assertTrue(res_99["size_evaluations"]["$100"]["route_available"])

        # Values immediately outside boundary
        src_101_01 = copy.deepcopy(self.valid_quote_source)
        src_101_01["dex_execution_quotes"]["buy_quote"]["notional_size_usd"] = 101.01
        src_101_01["dex_execution_quotes"]["sell_quote"]["notional_size_usd"] = 101.01
        res_101_01 = simulate_multi_size(src_101_01, sizes_usd=[100.0], as_of_utc=self.as_of_utc, size_tolerance_pct=1.0, expected_token_address=VALID_TOKEN_ADDR)
        self.assertFalse(res_101_01["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_101_01["size_evaluations"]["$100"]["status"], "ORDER_SIZE_MISMATCH")

        src_98_99 = copy.deepcopy(self.valid_quote_source)
        src_98_99["dex_execution_quotes"]["buy_quote"]["notional_size_usd"] = 98.99
        src_98_99["dex_execution_quotes"]["sell_quote"]["notional_size_usd"] = 98.99
        res_98_99 = simulate_multi_size(src_98_99, sizes_usd=[100.0], as_of_utc=self.as_of_utc, size_tolerance_pct=1.0, expected_token_address=VALID_TOKEN_ADDR)
        self.assertFalse(res_98_99["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_98_99["size_evaluations"]["$100"]["status"], "ORDER_SIZE_MISMATCH")

    # 16. AMM reserve identity on snapshot itself (no inheritance from candidate)
    def test_16_amm_reserve_identity_on_snapshot_itself(self):
        # Missing chain on reserve snapshot itself
        src_no_chain = {
            "chain": VALID_EVM_CHAIN,  # On parent, but must not be inherited!
            "amm_reserves": {
                "pool_address": VALID_POOL_ADDR,
                "token0": VALID_TOKEN_ADDR,
                "token1": VALID_QUOTE_ADDR,
                "reserve_base": 10000.0,
                "reserve_quote": 50000.0,
                "timestamp_utc": self.as_of_utc,
            },
        }
        res_chain = simulate_multi_size(
            src_no_chain, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_pool=VALID_POOL_ADDR, expected_token_address=VALID_TOKEN_ADDR,
        )
        self.assertFalse(res_chain["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_chain["size_evaluations"]["$100"]["status"], "MISSING_AMM_CHAIN_IDENTITY")

        # Missing pool on reserve snapshot itself
        src_no_pool = {
            "pool_address": VALID_POOL_ADDR,  # On parent, but must not be inherited!
            "amm_reserves": {
                "chain": VALID_EVM_CHAIN,
                "token0": VALID_TOKEN_ADDR,
                "token1": VALID_QUOTE_ADDR,
                "reserve_base": 10000.0,
                "reserve_quote": 50000.0,
                "timestamp_utc": self.as_of_utc,
            },
        }
        res_pool = simulate_multi_size(
            src_no_pool, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR,
        )
        self.assertFalse(res_pool["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_pool["size_evaluations"]["$100"]["status"], "MISSING_AMM_POOL_IDENTITY")

        # Missing token0/token1
        src_no_tokens = {
            "amm_reserves": {
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "reserve_base": 10000.0,
                "reserve_quote": 50000.0,
                "timestamp_utc": self.as_of_utc,
            },
        }
        res_tokens = simulate_multi_size(
            src_no_tokens, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR,
        )
        self.assertFalse(res_tokens["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_tokens["size_evaluations"]["$100"]["status"], "MISSING_AMM_TOKEN_IDENTITY")

    # 17. AMM reserve token orientation and expected quote asset
    def test_17_amm_reserve_token_orientation_and_quote_asset(self):
        # Tokens do not match candidate and quote asset
        src_wrong_tokens = {
            "amm_reserves": {
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "token0": VALID_ALT_TOKEN,
                "token1": VALID_ALT_QUOTE,
                "reserve_base": 10000.0,
                "reserve_quote": 50000.0,
                "timestamp_utc": self.as_of_utc,
            },
        }
        res_wrong = simulate_multi_size(
            src_wrong_tokens, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_pool=VALID_POOL_ADDR, expected_token_address=VALID_TOKEN_ADDR, expected_quote_asset=VALID_QUOTE_ADDR,
        )
        self.assertFalse(res_wrong["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_wrong["size_evaluations"]["$100"]["status"], "AMM_TOKEN_MISMATCH")

        # Candidate token matches, but quote asset does not match expected_quote_asset
        src_wrong_quote = {
            "amm_reserves": {
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "token0": VALID_TOKEN_ADDR,
                "token1": VALID_ALT_QUOTE,  # WETH instead of USDC
                "reserve_base": 10000.0,
                "reserve_quote": 50000.0,
                "timestamp_utc": self.as_of_utc,
            },
        }
        res_quote = simulate_multi_size(
            src_wrong_quote, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_pool=VALID_POOL_ADDR, expected_token_address=VALID_TOKEN_ADDR, expected_quote_asset=VALID_QUOTE_ADDR,
        )
        self.assertFalse(res_quote["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_quote["size_evaluations"]["$100"]["status"], "QUOTE_ASSET_MISMATCH")

    # 18. Order-book identity validation
    def test_18_order_book_identity_validation(self):
        base_book = {
            "timestamp_utc": self.as_of_utc,
            "bids": [[99.0, 10.0]],
            "asks": [[101.0, 10.0]],
        }

        # Missing chain
        book_no_chain = copy.deepcopy(base_book)
        book_no_chain["pool_address"] = VALID_POOL_ADDR
        book_no_chain["token_address"] = VALID_TOKEN_ADDR
        res = simulate_multi_size(book_no_chain, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
        self.assertFalse(res["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res["size_evaluations"]["$100"]["status"], "MISSING_BOOK_CHAIN_IDENTITY")

        # Missing pool
        book_no_pool = copy.deepcopy(base_book)
        book_no_pool["chain"] = VALID_EVM_CHAIN
        book_no_pool["token_address"] = VALID_TOKEN_ADDR
        res = simulate_multi_size(book_no_pool, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
        self.assertFalse(res["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res["size_evaluations"]["$100"]["status"], "MISSING_BOOK_POOL_IDENTITY")

        # Missing token
        book_no_token = copy.deepcopy(base_book)
        book_no_token["chain"] = VALID_EVM_CHAIN
        book_no_token["pool_address"] = VALID_POOL_ADDR
        res = simulate_multi_size(book_no_token, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
        self.assertFalse(res["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res["size_evaluations"]["$100"]["status"], "MISSING_BOOK_TOKEN_IDENTITY")

        # Token mismatch
        book_wrong_token = copy.deepcopy(base_book)
        book_wrong_token["chain"] = VALID_EVM_CHAIN
        book_wrong_token["pool_address"] = VALID_POOL_ADDR
        book_wrong_token["token_address"] = VALID_ALT_TOKEN
        res = simulate_multi_size(book_wrong_token, sizes_usd=[100.0], as_of_utc=self.as_of_utc, expected_token_address=VALID_TOKEN_ADDR)
        self.assertFalse(res["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res["size_evaluations"]["$100"]["status"], "BOOK_TOKEN_MISMATCH")

        # Fully evidenced valid order book passes
        valid_book = copy.deepcopy(base_book)
        valid_book["chain"] = VALID_EVM_CHAIN
        valid_book["pool_address"] = VALID_POOL_ADDR
        valid_book["token_address"] = VALID_TOKEN_ADDR
        valid_book["quote_asset"] = VALID_QUOTE_ADDR
        res_ok = simulate_multi_size(
            valid_book, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR, expected_chain=VALID_EVM_CHAIN,
            expected_pool=VALID_POOL_ADDR, expected_quote_asset=VALID_QUOTE_ADDR,
        )
        self.assertTrue(res_ok["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res_ok["size_evaluations"]["$100"]["status"], "EXECUTABLE_DEPTH_AVAILABLE")

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
        with self.assertRaisesRegex(ValueError, "INVALID_FEE_DATA"):
            validate_fee_bps(-0.01)
        with self.assertRaisesRegex(ValueError, "INVALID_FEE_DATA"):
            validate_fee_bps(None)
        with self.assertRaisesRegex(ValueError, "INVALID_FEE_DATA"):
            validate_fee_bps(True)
        with self.assertRaisesRegex(ValueError, "INVALID_FEE_DATA"):
            validate_fee_bps(10000.0)

        book = {
            "chain": VALID_EVM_CHAIN, "pool_address": VALID_POOL_ADDR, "token_address": VALID_TOKEN_ADDR,
            "timestamp_utc": self.as_of_utc, "bids": [[99.0, 1.0]], "asks": [[101.0, 1.0]],
        }
        with self.assertRaisesRegex(ValueError, "INVALID_FEE_DATA"):
            simulate(book, buy_notional_usd=100.0, fee_bps=-5.0)

        with self.assertRaisesRegex(ValueError, "INVALID_FEE_DATA"):
            simulate_constant_product_amm(1000.0, 1000.0, buy_notional_usd=100.0, fee_bps=-5.0, require_reserve_identity=False)

    # 21. Production qualification rejects every unverified execution case
    def test_21_production_qualification_rejects_every_unverified_execution_case(self):
        from scripts import v425_early_breakout as v425

        base_cand = {
            "market_type": "dex_pool",
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "final_pool_address": VALID_POOL_ADDR,
            "final_token_contract": VALID_TOKEN_ADDR,
            "final_quote_chain": VALID_EVM_CHAIN,
            "final_quote_token_orientation_verified": True,
            "final_price_usd": 100.0,
            "final_price_timestamp_utc": self.as_of_utc,
            "final_price_verified": True,
            "target_price": 140.0,
            "target_price_source": "7D_SWING_RESISTANCE",
            "intended_trade_size_usd": 100.0,
            "quote_token_address": VALID_QUOTE_ADDR,
            "expected_quote_asset": VALID_QUOTE_ADDR,
            "liquidity_usd": 100_000.0,
            "fee_bps": 30.0,
            "technical_tp_validation": {
                "status": "PASS",
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "token_address": VALID_TOKEN_ADDR,
                "token_orientation_verified": True,
                "target_price": 140.0,
                "target_price_source": "7D_SWING_RESISTANCE",
                "current_price": 100.0,
                "atr_1h_pct": 10.0,
                "maximum_tp_distance_pct": 40.0,
                "candle_retrieved_utc": self.as_of_utc,
            },
            "trade_plan": {"single_take_profit": 140.0, "intended_trade_size_usd": 100.0, "quote_token_address": VALID_QUOTE_ADDR},
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
        cand_ident_mismatch["dex_execution_quotes"]["buy_quote"]["out_token"] = VALID_ALT_TOKEN
        self.assertEqual(len(v425.qualify_final_actionable_candidates([cand_ident_mismatch], self.now_dt)), 0)
        self.assertTrue(any("execution identity verification failed" in b for b in cand_ident_mismatch["qualification_blockers"]))

        # Case D: Direction mismatch
        cand_dir = copy.deepcopy(base_cand)
        cand_dir["dex_execution_quotes"]["buy_quote"]["direction"] = "SELL"
        self.assertEqual(len(v425.qualify_final_actionable_candidates([cand_dir], self.now_dt)), 0)
        self.assertTrue(any("execution identity verification failed: DIRECTION_MISMATCH" in b for b in cand_dir["qualification_blockers"]))

        # Case E: Round-trip token quantity mismatch
        cand_qty = copy.deepcopy(base_cand)
        cand_qty["dex_execution_quotes"]["buy_quote"]["out_amount"] = 50.0
        cand_qty["dex_execution_quotes"]["sell_quote"]["in_amount"] = 10.0
        self.assertEqual(len(v425.qualify_final_actionable_candidates([cand_qty], self.now_dt)), 0)
        self.assertIn("execution simulation round-trip token quantity mismatch", cand_qty["qualification_blockers"])

        # Case F: Missing configured quote asset
        cand_no_quote_asset = copy.deepcopy(base_cand)
        del cand_no_quote_asset["quote_token_address"]
        del cand_no_quote_asset["expected_quote_asset"]
        del cand_no_quote_asset["trade_plan"]["quote_token_address"]
        self.assertEqual(len(v425.qualify_final_actionable_candidates([cand_no_quote_asset], self.now_dt)), 0)
        self.assertIn("expected quote asset is missing, unconfigured, or invalid", cand_no_quote_asset["qualification_blockers"])

    # 22. Existing valid, fully evidenced scenarios continue to work
    def test_22_existing_valid_fully_evidenced_scenarios_continue_to_work(self):
        from scripts import v425_early_breakout as v425
        cand = {
            "market_type": "dex_pool",
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "final_pool_address": VALID_POOL_ADDR,
            "final_token_contract": VALID_TOKEN_ADDR,
            "final_quote_chain": VALID_EVM_CHAIN,
            "final_quote_token_orientation_verified": True,
            "final_price_usd": 100.0,
            "final_price_timestamp_utc": self.as_of_utc,
            "final_price_verified": True,
            "target_price": 140.0,
            "target_price_source": "7D_SWING_RESISTANCE",
            "intended_trade_size_usd": 100.0,
            "quote_token_address": VALID_QUOTE_ADDR,
            "expected_quote_asset": VALID_QUOTE_ADDR,
            "liquidity_usd": 100_000.0,
            "fee_bps": 30.0,
            "technical_tp_validation": {
                "status": "PASS",
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "token_address": VALID_TOKEN_ADDR,
                "token_orientation_verified": True,
                "target_price": 140.0,
                "target_price_source": "7D_SWING_RESISTANCE",
                "current_price": 100.0,
                "atr_1h_pct": 10.0,
                "maximum_tp_distance_pct": 40.0,
                "candle_retrieved_utc": self.as_of_utc,
            },
            "trade_plan": {"single_take_profit": 140.0, "intended_trade_size_usd": 100.0, "quote_token_address": VALID_QUOTE_ADDR},
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

    # 23. AMM reserve orientation: candidate is token0, quote asset is token1
    def test_23_amm_orientation_candidate_is_token0(self):
        src = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "liquidity_usd": 100_000.0,
            "amm_reserves": {
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "token0": VALID_TOKEN_ADDR,   # Candidate is token0
                "token1": VALID_QUOTE_ADDR,   # Quote asset is token1
                "reserve0": 10_000.0,         # Candidate reserve
                "reserve1": 50_000.0,         # Quote asset reserve
                "timestamp_utc": self.as_of_utc,
            },
        }
        res = simulate_multi_size(
            src,
            sizes_usd=[100.0],
            as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR,
            expected_pool=VALID_POOL_ADDR,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
        )
        s100 = res["size_evaluations"]["$100"]
        self.assertTrue(s100["route_available"])
        self.assertEqual(s100["status"], "EXECUTABLE_AMM_ROUTE_AVAILABLE")
        self.assertTrue(s100["token0_is_candidate"])
        self.assertEqual(s100["reserve_base"], 10_000.0)
        self.assertEqual(s100["reserve_quote"], 50_000.0)
        self.assertAlmostEqual(s100["buy"]["filled_quote_usd"], 100.0, places=2)
        self.assertTrue(s100["buy"]["filled_quantity"] > 0)
        self.assertTrue(s100["sell"]["filled_quote_usd"] > 0)

    # 24. AMM reserve orientation: candidate is token1, quote asset is token0
    def test_24_amm_orientation_candidate_is_token1(self):
        src = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "liquidity_usd": 100_000.0,
            "amm_reserves": {
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "token0": VALID_QUOTE_ADDR,   # Quote asset is token0
                "token1": VALID_TOKEN_ADDR,   # Candidate is token1
                "reserve0": 50_000.0,         # Quote asset reserve
                "reserve1": 10_000.0,         # Candidate reserve
                "timestamp_utc": self.as_of_utc,
            },
        }
        res = simulate_multi_size(
            src,
            sizes_usd=[100.0],
            as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR,
            expected_pool=VALID_POOL_ADDR,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
        )
        s100 = res["size_evaluations"]["$100"]
        self.assertTrue(s100["route_available"])
        self.assertEqual(s100["status"], "EXECUTABLE_AMM_ROUTE_AVAILABLE")
        self.assertFalse(s100["token0_is_candidate"])
        self.assertEqual(s100["reserve_base"], 10_000.0)
        self.assertEqual(s100["reserve_quote"], 50_000.0)
        self.assertAlmostEqual(s100["buy"]["filled_quote_usd"], 100.0, places=2)
        self.assertTrue(s100["buy"]["filled_quantity"] > 0)
        self.assertTrue(s100["sell"]["filled_quote_usd"] > 0)

    # 25. Both orientations produce economically equivalent results for identical pool economics
    def test_25_amm_both_orientations_produce_economically_equivalent_results(self):
        # Pool A: token0 is candidate, token1 is quote
        src_a = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "liquidity_usd": 100_000.0,
            "amm_reserves": {
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "token0": VALID_TOKEN_ADDR,
                "token1": VALID_QUOTE_ADDR,
                "reserve0": 10_000.0,
                "reserve1": 50_000.0,
                "timestamp_utc": self.as_of_utc,
            },
        }

        # Pool B: token0 is quote, token1 is candidate (swapped ordering & swapped reserves)
        src_b = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "liquidity_usd": 100_000.0,
            "amm_reserves": {
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "token0": VALID_QUOTE_ADDR,
                "token1": VALID_TOKEN_ADDR,
                "reserve0": 50_000.0,
                "reserve1": 10_000.0,
                "timestamp_utc": self.as_of_utc,
            },
        }

        sizes = [100.0, 500.0, 1000.0]
        res_a = simulate_multi_size(
            src_a, sizes_usd=sizes, as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR, expected_pool=VALID_POOL_ADDR,
            expected_chain=VALID_EVM_CHAIN, expected_quote_asset=VALID_QUOTE_ADDR,
        )
        res_b = simulate_multi_size(
            src_b, sizes_usd=sizes, as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR, expected_pool=VALID_POOL_ADDR,
            expected_chain=VALID_EVM_CHAIN, expected_quote_asset=VALID_QUOTE_ADDR,
        )

        for s in ["$100", "$500", "$1000"]:
            eval_a = res_a["size_evaluations"][s]
            eval_b = res_b["size_evaluations"][s]
            self.assertTrue(eval_a["route_available"])
            self.assertTrue(eval_b["route_available"])
            self.assertAlmostEqual(eval_a["buy"]["filled_quantity"], eval_b["buy"]["filled_quantity"], places=9)
            self.assertAlmostEqual(eval_a["sell"]["filled_quote_usd"], eval_b["sell"]["filled_quote_usd"], places=9)
            self.assertAlmostEqual(eval_a["buy"]["vwap"], eval_b["buy"]["vwap"], places=9)
            self.assertAlmostEqual(eval_a["sell"]["vwap"], eval_b["sell"]["vwap"], places=9)
            self.assertAlmostEqual(eval_a["estimated_round_trip_cost_pct"], eval_b["estimated_round_trip_cost_pct"], places=4)
            self.assertAlmostEqual(eval_a["round_trip_cost_usd"], eval_b["round_trip_cost_usd"], places=4)

    # 26. Incorrect reserve orientation is rejected or fails closed
    def test_26_amm_incorrect_reserve_orientation_rejected(self):
        # Conflicting explicit reserve_base with oriented token reserve
        src_inconsistent = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "liquidity_usd": 100_000.0,
            "amm_reserves": {
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "token0": VALID_QUOTE_ADDR,   # token0 is quote asset
                "token1": VALID_TOKEN_ADDR,   # token1 is candidate token
                "reserve0": 50_000.0,         # quote reserve = 50,000
                "reserve1": 10_000.0,         # candidate reserve = 10,000
                "reserve_base": 50_000.0,     # Contradicts candidate reserve (claims base is 50,000)
                "reserve_quote": 10_000.0,
                "timestamp_utc": self.as_of_utc,
            },
        }
        res = simulate_multi_size(
            src_inconsistent, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR, expected_quote_asset=VALID_QUOTE_ADDR,
        )
        self.assertFalse(res["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res["size_evaluations"]["$100"]["status"], "INCONSISTENT_AMM_RESERVES")

    # 27. Missing token identity and invalid reserves fail closed
    def test_27_amm_missing_identity_and_invalid_reserves_fail_closed(self):
        # 1. Missing token identity when reserve0/reserve1 are provided
        with self.assertRaises(ValueError) as cm:
            simulate_constant_product_amm(
                buy_notional_usd=100.0,
                reserve0=10000.0,
                reserve1=50000.0,
                require_reserve_identity=False,
            )
        self.assertIn("MISSING_AMM_TOKEN_IDENTITY", str(cm.exception))

        # 2. Same address for token0 and token1
        with self.assertRaises(ValueError) as cm:
            simulate_constant_product_amm(
                buy_notional_usd=100.0,
                chain=VALID_EVM_CHAIN,
                pool_address=VALID_POOL_ADDR,
                token0=VALID_TOKEN_ADDR,
                token1=VALID_TOKEN_ADDR,
                expected_token_address=VALID_TOKEN_ADDR,
                reserve0=10000.0,
                reserve1=50000.0,
            )
        self.assertIn("INVALID_AMM_RESERVES", str(cm.exception))

        # 3. Negative, zero, NaN, Inf reserves
        for bad in [-100.0, 0.0, float("nan"), float("inf")]:
            with self.assertRaises(ValueError) as cm:
                simulate_constant_product_amm(
                    buy_notional_usd=100.0,
                    chain=VALID_EVM_CHAIN,
                    pool_address=VALID_POOL_ADDR,
                    token0=VALID_TOKEN_ADDR,
                    token1=VALID_QUOTE_ADDR,
                    expected_token_address=VALID_TOKEN_ADDR,
                    reserve0=bad,
                    reserve1=50000.0,
                )
            self.assertIn("INVALID_AMM_RESERVES", str(cm.exception))

        # 4. Partial reserves (reserve0 given without reserve1)
        with self.assertRaises(ValueError) as cm:
            simulate_constant_product_amm(
                buy_notional_usd=100.0,
                chain=VALID_EVM_CHAIN,
                pool_address=VALID_POOL_ADDR,
                token0=VALID_TOKEN_ADDR,
                token1=VALID_QUOTE_ADDR,
                expected_token_address=VALID_TOKEN_ADDR,
                reserve0=10000.0,
                reserve1=None,
            )
        self.assertIn("INVALID_AMM_RESERVES", str(cm.exception))

    # 28. Production gate correctly qualifies candidates with oriented AMM reserves
    def test_28_production_gate_with_oriented_reserves(self):
        cand_token1 = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "final_pool_address": VALID_POOL_ADDR,
            "final_token_contract": VALID_TOKEN_ADDR,
            "final_quote_chain": VALID_EVM_CHAIN,
            "final_quote_token_orientation_verified": True,
            "final_price_verified": True,
            "token_symbol": "ROCKET",
            "market_type": "dex_pool",
            "expected_quote_asset": VALID_QUOTE_ADDR,
            "quote_token_address": VALID_QUOTE_ADDR,
            "final_price_usd": 5.0,
            "target_price": 7.0,  # +40.0% upside
            "target_price_source": "7D_SWING_RESISTANCE",
            "final_price_timestamp_utc": self.as_of_utc,
            "intended_trade_size_usd": 100.0,
            "liquidity_usd": 100_000.0,
            "fee_bps": 30.0,
            "technical_tp_validation": {
                "status": "PASS",
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "token_address": VALID_TOKEN_ADDR,
                "token_orientation_verified": True,
                "target_price": 7.0,
                "target_price_source": "7D_SWING_RESISTANCE",
                "current_price": 5.0,
                "atr_1h_pct": 10.0,
                "maximum_tp_distance_pct": 40.0,
                "candle_retrieved_utc": self.as_of_utc,
            },
            "trade_plan": {"single_take_profit": 7.0, "intended_trade_size_usd": 100.0, "quote_token_address": VALID_QUOTE_ADDR},
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
            # AMM snapshot where candidate is token1 and quote asset is token0
            "amm_reserves": {
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "token0": VALID_QUOTE_ADDR,
                "token1": VALID_TOKEN_ADDR,
                "reserve0": 50_000.0,
                "reserve1": 10_000.0,
                "timestamp_utc": self.as_of_utc,
            },
        }

        qualified = v425.qualify_final_actionable_candidates([cand_token1], self.now_dt)
        self.assertEqual(len(qualified), 1)
        self.assertEqual(qualified[0]["opportunity_status"], "QUALIFIED")
        self.assertEqual(qualified[0]["execution_simulation"]["status"], "EXECUTABLE_AMM_ROUTE_AVAILABLE")
        self.assertTrue(qualified[0]["execution_simulation"]["route_available"])
        self.assertFalse(qualified[0]["execution_simulation"]["token0_is_candidate"])
        self.assertEqual(qualified[0]["execution_simulation"]["reserve_base"], 10_000.0)
        self.assertEqual(qualified[0]["execution_simulation"]["reserve_quote"], 50_000.0)

    # 29. AMM ambiguous base_token/quote_token aliases cannot silently establish orientation
    def test_29_amm_ambiguous_aliases_cannot_establish_orientation(self):
        src_aliases = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "liquidity_usd": 100_000.0,
            "amm_reserves": {
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "base_token": VALID_TOKEN_ADDR,   # Ambiguous alias without explicit token0
                "quote_token": VALID_QUOTE_ADDR,  # Ambiguous alias without explicit token1
                "reserve0": 10_000.0,
                "reserve1": 50_000.0,
                "timestamp_utc": self.as_of_utc,
            },
        }
        res = simulate_multi_size(
            src_aliases, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR, expected_quote_asset=VALID_QUOTE_ADDR,
        )
        s100 = res["size_evaluations"]["$100"]
        self.assertFalse(s100["route_available"])
        self.assertEqual(s100["status"], "MISSING_AMM_TOKEN_IDENTITY")
        self.assertIn("ambiguous base_token/quote_token aliases", s100["reason"])

    # 30. Missing explicit AMM token ordering fails closed
    def test_30_missing_explicit_amm_token_ordering_fails_closed(self):
        src_no_order = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "liquidity_usd": 100_000.0,
            "amm_reserves": {
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "reserve0": 10_000.0,
                "reserve1": 50_000.0,
                "timestamp_utc": self.as_of_utc,
            },
        }
        res = simulate_multi_size(
            src_no_order, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR,
        )
        self.assertFalse(res["size_evaluations"]["$100"]["route_available"])
        self.assertEqual(res["size_evaluations"]["$100"]["status"], "MISSING_AMM_TOKEN_IDENTITY")

    # 31. Valid order book with correct quote asset passes identity validation
    def test_31_order_book_valid_quote_asset_passes(self):
        book = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token_address": VALID_TOKEN_ADDR,
            "quote_asset": VALID_QUOTE_ADDR,
            "timestamp_utc": self.as_of_utc,
            "bids": [[99.0, 10.0]],
            "asks": [[101.0, 10.0]],
        }
        res = simulate_multi_size(
            book, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR, expected_chain=VALID_EVM_CHAIN,
            expected_pool=VALID_POOL_ADDR, expected_quote_asset=VALID_QUOTE_ADDR,
            require_quote_asset=True,
        )
        s100 = res["size_evaluations"]["$100"]
        self.assertTrue(s100["route_available"])
        self.assertEqual(s100["status"], "EXECUTABLE_DEPTH_AVAILABLE")

    # 32. Order book with missing quote asset fails when quote verification is required
    def test_32_order_book_missing_quote_asset_fails_when_required(self):
        # 1. Missing quote asset with require_quote_asset=True
        book_no_quote = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token_address": VALID_TOKEN_ADDR,
            "timestamp_utc": self.as_of_utc,
            "bids": [[99.0, 10.0]],
            "asks": [[101.0, 10.0]],
        }
        res1 = simulate_multi_size(
            book_no_quote, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR, expected_chain=VALID_EVM_CHAIN,
            expected_pool=VALID_POOL_ADDR, require_quote_asset=True,
        )
        s1 = res1["size_evaluations"]["$100"]
        self.assertFalse(s1["route_available"])
        self.assertEqual(s1["status"], "MISSING_QUOTE_ASSET_EVIDENCE")

        # 2. Missing quote asset when expected_quote_asset is specified
        res2 = simulate_multi_size(
            book_no_quote, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR, expected_chain=VALID_EVM_CHAIN,
            expected_pool=VALID_POOL_ADDR, expected_quote_asset=VALID_QUOTE_ADDR,
        )
        s2 = res2["size_evaluations"]["$100"]
        self.assertFalse(s2["route_available"])
        self.assertEqual(s2["status"], "MISSING_QUOTE_ASSET_EVIDENCE")

    # 33. Order book with incorrect quote asset fails
    def test_33_order_book_incorrect_quote_asset_fails(self):
        # 1. Quote asset mismatch against expected_quote_asset
        book_mismatch = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token_address": VALID_TOKEN_ADDR,
            "quote_asset": VALID_ALT_QUOTE,  # WETH instead of USDC
            "timestamp_utc": self.as_of_utc,
            "bids": [[99.0, 10.0]],
            "asks": [[101.0, 10.0]],
        }
        res_mismatch = simulate_multi_size(
            book_mismatch, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR, expected_quote_asset=VALID_QUOTE_ADDR,
            require_quote_asset=True,
        )
        s_mismatch = res_mismatch["size_evaluations"]["$100"]
        self.assertFalse(s_mismatch["route_available"])
        self.assertEqual(s_mismatch["status"], "QUOTE_ASSET_MISMATCH")

        # 2. Invalid quote asset address format
        book_invalid_addr = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token_address": VALID_TOKEN_ADDR,
            "quote_asset": "0xinvalid_quote_address_with_bad_format",
            "timestamp_utc": self.as_of_utc,
            "bids": [[99.0, 10.0]],
            "asks": [[101.0, 10.0]],
        }
        res_invalid = simulate_multi_size(
            book_invalid_addr, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR, require_quote_asset=True,
        )
        s_invalid = res_invalid["size_evaluations"]["$100"]
        self.assertFalse(s_invalid["route_available"])
        self.assertEqual(s_invalid["status"], "INVALID_TOKEN_ADDRESS")

        # 3. Candidate token and quote asset cannot be the same address
        book_same_addr = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token_address": VALID_TOKEN_ADDR,
            "quote_asset": VALID_TOKEN_ADDR,  # Token equals quote asset
            "timestamp_utc": self.as_of_utc,
            "bids": [[99.0, 10.0]],
            "asks": [[101.0, 10.0]],
        }
        res_same = simulate_multi_size(
            book_same_addr, sizes_usd=[100.0], as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR, require_quote_asset=True,
        )
        s_same = res_same["size_evaluations"]["$100"]
        self.assertFalse(s_same["route_available"])
        self.assertEqual(s_same["status"], "INVALID_TOKEN_ADDRESS")

    # 34. Production qualification gate rejects missing-quote-asset order book
    def test_34_production_gate_rejects_missing_quote_asset_order_book(self):
        cand_book_missing_quote = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "final_pool_address": VALID_POOL_ADDR,
            "final_token_contract": VALID_TOKEN_ADDR,
            "final_quote_chain": VALID_EVM_CHAIN,
            "final_quote_token_orientation_verified": True,
            "final_price_verified": True,
            "token_symbol": "ROCKET",
            "market_type": "dex_pool",
            "expected_quote_asset": VALID_QUOTE_ADDR,
            "quote_token_address": VALID_QUOTE_ADDR,
            "final_price_usd": 100.0,
            "target_price": 140.0,  # +40.0% upside
            "target_price_source": "7D_SWING_RESISTANCE",
            "final_price_timestamp_utc": self.as_of_utc,
            "intended_trade_size_usd": 100.0,
            "liquidity_usd": 100_000.0,
            "fee_bps": 30.0,
            "technical_tp_validation": {
                "status": "PASS",
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "token_address": VALID_TOKEN_ADDR,
                "token_orientation_verified": True,
                "target_price": 140.0,
                "target_price_source": "7D_SWING_RESISTANCE",
                "current_price": 100.0,
                "atr_1h_pct": 10.0,
                "maximum_tp_distance_pct": 40.0,
                "candle_retrieved_utc": self.as_of_utc,
            },
            "trade_plan": {"single_take_profit": 140.0, "intended_trade_size_usd": 100.0, "quote_token_address": VALID_QUOTE_ADDR},
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
            # Order book depth snapshot missing quote_asset
            "order_book": {
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "token_address": VALID_TOKEN_ADDR,
                # "quote_asset" is deliberately omitted
                "timestamp_utc": self.as_of_utc,
                "bids": [[99.0, 10.0]],
                "asks": [[101.0, 10.0]],
            },
        }

        qualified = v425.qualify_final_actionable_candidates([cand_book_missing_quote], self.now_dt)
        self.assertEqual(len(qualified), 0)
        self.assertIn("qualification_blockers", cand_book_missing_quote)
        self.assertTrue(
            any("quote asset verification failed" in b for b in cand_book_missing_quote["qualification_blockers"]),
            f"Expected quote asset failure in blockers: {cand_book_missing_quote['qualification_blockers']}"
        )
        self.assertEqual(cand_book_missing_quote["execution_simulation"]["status"], "MISSING_QUOTE_ASSET_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
