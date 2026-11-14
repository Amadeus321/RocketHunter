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
    validate_quote_usd_conversion,
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
        self.valid_quote_conversion = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": "2026-11-13T12:00:00Z",
            "source": "chainlink_oracle_usd",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "observation": {
                "answer": 100000000,
                "decimals": 8,
                "round_id": 1,
                "updated_at": "2026-11-13T12:00:00Z",
            },
        }
        self.valid_quote_source = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "intended_trade_size_usd": 100.0,
            "quote_token_address": VALID_QUOTE_ADDR,
            "expected_quote_asset": VALID_QUOTE_ADDR,
            "quote_usd_conversion": self.valid_quote_conversion,
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
        src_exp["quote_usd_conversion"]["quote_asset"] = VALID_ALT_QUOTE
        src_exp["quote_usd_conversion"]["feed_address"] = "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419"
        src_exp["quote_usd_conversion"]["valuation_policy"] = "CRYPTO_ORACLE_VERIFIED"
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
            "quote_usd_conversion": self.valid_quote_conversion,
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
            "quote_usd_conversion": self.valid_quote_conversion,
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
            "quote_usd_conversion": self.valid_quote_conversion,
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
            "quote_usd_conversion": self.valid_quote_conversion,
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

    # 35. USD quote asset with verified conversion
    def test_35_usd_quote_asset_with_verified_conversion(self):
        conv = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink_oracle_usd",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "observation": {
                "answer": 100000000,
                "decimals": 8,
                "round_id": 1,
                "updated_at": self.as_of_utc,
            },
        }
        # Test AMM with verified USD quote asset
        amm_res = simulate_constant_product_amm(
            reserve0=10_000.0,
            reserve1=50_000.0,
            buy_notional_usd=100.0,
            fee_bps=30.0,
            chain=VALID_EVM_CHAIN,
            pool_address=VALID_POOL_ADDR,
            token0=VALID_TOKEN_ADDR,
            token1=VALID_QUOTE_ADDR,
            timestamp_utc=self.as_of_utc,
            expected_chain=VALID_EVM_CHAIN,
            expected_pool=VALID_POOL_ADDR,
            expected_token_address=VALID_TOKEN_ADDR,
            expected_quote_asset=VALID_QUOTE_ADDR,
            quote_usd_conversion=conv,
            require_quote_conversion=True,
        )
        self.assertEqual(amm_res["status"], "FULL_ROUND_TRIP_AMM_LIQUIDITY_AVAILABLE")
        self.assertEqual(amm_res["quote_usd_price"], 1.0)
        self.assertEqual(amm_res["buy"]["filled_quote_usd"], 100.0)
        self.assertEqual(amm_res["buy"]["filled_quote_tokens"], 100.0)

    # 36. Non-USD quote asset (e.g. WETH @ $3000) with verified conversion
    def test_36_non_usd_quote_asset_with_verified_conversion(self):
        weth_conv = {
            "price_usd": 3000.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_ALT_QUOTE,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink_oracle_eth_usd",
            "provider": "chainlink",
            "feed_address": "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419",
            "valuation_policy": "CRYPTO_ORACLE_VERIFIED",
            "observation": {
                "answer": 300000000000,
                "decimals": 8,
                "round_id": 1,
                "updated_at": self.as_of_utc,
            },
        }
        # In this pool, quote reserve is 16.666667 WETH ($50k USD equiv)
        weth_reserve = 50_000.0 / 3000.0
        amm_res = simulate_constant_product_amm(
            reserve0=10_000.0,
            reserve1=weth_reserve,
            buy_notional_usd=100.0,
            fee_bps=30.0,
            chain=VALID_EVM_CHAIN,
            pool_address=VALID_POOL_ADDR,
            token0=VALID_TOKEN_ADDR,
            token1=VALID_ALT_QUOTE,
            timestamp_utc=self.as_of_utc,
            expected_chain=VALID_EVM_CHAIN,
            expected_pool=VALID_POOL_ADDR,
            expected_token_address=VALID_TOKEN_ADDR,
            expected_quote_asset=VALID_ALT_QUOTE,
            quote_usd_conversion=weth_conv,
            require_quote_conversion=True,
        )
        self.assertEqual(amm_res["status"], "FULL_ROUND_TRIP_AMM_LIQUIDITY_AVAILABLE")
        self.assertEqual(amm_res["quote_usd_price"], 3000.0)
        self.assertEqual(amm_res["buy"]["filled_quote_usd"], 100.0)
        self.assertAlmostEqual(amm_res["buy"]["filled_quote_tokens"], 100.0 / 3000.0, places=9)

        # Also test order-book simulation with non-USD quote currency (WETH)
        book = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token_address": VALID_TOKEN_ADDR,
            "quote_asset": VALID_ALT_QUOTE,
            "denomination": "WETH",
            "timestamp_utc": self.as_of_utc,
            "bids": [[0.0016, 100.0]],   # bid in WETH per token (~$4.80)
            "asks": [[0.0017, 100.0]],   # ask in WETH per token (~$5.10)
        }
        book_sim = simulate(
            book,
            buy_notional_usd=100.0,
            fee_bps=30.0,
            quote_usd_conversion=weth_conv,
            require_quote_conversion=True,
            require_quote_denomination=True,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_ALT_QUOTE,
            as_of_utc=self.as_of_utc,
        )
        self.assertEqual(book_sim["status"], "FULL_ROUND_TRIP_DEPTH_AVAILABLE")
        self.assertEqual(book_sim["quote_usd_price"], 3000.0)
        self.assertAlmostEqual(book_sim["buy"]["filled_quote_tokens"], 100.0 / 3000.0, places=9)
        self.assertAlmostEqual(book_sim["buy"]["filled_quote_usd"], 100.0, places=9)

    # 37. Missing conversion evidence fails closed
    def test_37_missing_conversion_evidence_fails_closed(self):
        # AMM without conversion when required
        with self.assertRaisesRegex(ValueError, "MISSING_QUOTE_CONVERSION_EVIDENCE"):
            simulate_constant_product_amm(
                reserve0=10_000.0,
                reserve1=50_000.0,
                buy_notional_usd=100.0,
                chain=VALID_EVM_CHAIN,
                pool_address=VALID_POOL_ADDR,
                token0=VALID_TOKEN_ADDR,
                token1=VALID_QUOTE_ADDR,
                timestamp_utc=self.as_of_utc,
                expected_token_address=VALID_TOKEN_ADDR,
                require_quote_conversion=True,
            )

        # Multi-size without conversion when required
        src_no_conv = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
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
        res = simulate_multi_size(
            src_no_conv,
            sizes_usd=[100.0],
            as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR,
            expected_quote_asset=VALID_QUOTE_ADDR,
            require_quote_conversion=True,
        )
        self.assertEqual(res["status"], "MISSING_QUOTE_CONVERSION_EVIDENCE")
        self.assertFalse(res["size_evaluations"]["$100"]["route_available"])

    # 38. Stale conversion evidence fails closed
    def test_38_stale_and_future_dated_conversion_evidence_fails_closed(self):
        stale_conv = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": "2026-11-13T11:50:00Z",  # 10 minutes old (> 300s)
            "source": "chainlink_oracle_usd",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
        }
        with self.assertRaisesRegex(ValueError, "STALE_QUOTE_CONVERSION"):
            simulate_constant_product_amm(
                reserve0=10_000.0,
                reserve1=50_000.0,
                buy_notional_usd=100.0,
                chain=VALID_EVM_CHAIN,
                pool_address=VALID_POOL_ADDR,
                token0=VALID_TOKEN_ADDR,
                token1=VALID_QUOTE_ADDR,
                timestamp_utc=self.as_of_utc,
                expected_token_address=VALID_TOKEN_ADDR,
                quote_usd_conversion=stale_conv,
                as_of_utc=self.as_of_utc,
            )

        future_conv = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": "2026-11-13T12:05:00Z",  # Future-dated
            "source": "chainlink_oracle_usd",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
        }
        with self.assertRaisesRegex(ValueError, "FUTURE_DATED_QUOTE_CONVERSION"):
            simulate_constant_product_amm(
                reserve0=10_000.0,
                reserve1=50_000.0,
                buy_notional_usd=100.0,
                chain=VALID_EVM_CHAIN,
                pool_address=VALID_POOL_ADDR,
                token0=VALID_TOKEN_ADDR,
                token1=VALID_QUOTE_ADDR,
                timestamp_utc=self.as_of_utc,
                expected_token_address=VALID_TOKEN_ADDR,
                quote_usd_conversion=future_conv,
                as_of_utc=self.as_of_utc,
            )

    # 39. Wrong asset or chain conversion fails closed
    def test_39_wrong_asset_or_chain_conversion_fails_closed(self):
        # Mismatched chain
        wrong_chain_conv = {
            "price_usd": 1.0,
            "chain": "solana",  # Expected ethereum
            "quote_asset": VALID_QUOTE_ADDR,
            "timestamp_utc": self.as_of_utc,
            "source": "pyth_oracle",
            "provider": "pyth",
            "feed_id": "solana_feed_123",
            "valuation_policy": "CRYPTO_ORACLE_VERIFIED",
        }
        with self.assertRaisesRegex(ValueError, "QUOTE_CONVERSION_CHAIN_MISMATCH"):
            simulate_constant_product_amm(
                reserve0=10_000.0,
                reserve1=50_000.0,
                buy_notional_usd=100.0,
                chain=VALID_EVM_CHAIN,
                pool_address=VALID_POOL_ADDR,
                token0=VALID_TOKEN_ADDR,
                token1=VALID_QUOTE_ADDR,
                timestamp_utc=self.as_of_utc,
                expected_chain=VALID_EVM_CHAIN,
                expected_token_address=VALID_TOKEN_ADDR,
                expected_quote_asset=VALID_QUOTE_ADDR,
                quote_usd_conversion=wrong_chain_conv,
            )

        # Mismatched quote asset
        wrong_asset_conv = {
            "price_usd": 3000.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_ALT_QUOTE,  # Pool uses VALID_QUOTE_ADDR (USDC), but conversion is for WETH
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink_oracle_eth_usd",
            "provider": "chainlink",
            "feed_address": "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419",
            "valuation_policy": "CRYPTO_ORACLE_VERIFIED",
        }
        with self.assertRaisesRegex(ValueError, "QUOTE_CONVERSION_ASSET_MISMATCH"):
            simulate_constant_product_amm(
                reserve0=10_000.0,
                reserve1=50_000.0,
                buy_notional_usd=100.0,
                chain=VALID_EVM_CHAIN,
                pool_address=VALID_POOL_ADDR,
                token0=VALID_TOKEN_ADDR,
                token1=VALID_QUOTE_ADDR,
                timestamp_utc=self.as_of_utc,
                expected_chain=VALID_EVM_CHAIN,
                expected_token_address=VALID_TOKEN_ADDR,
                expected_quote_asset=VALID_QUOTE_ADDR,
                quote_usd_conversion=wrong_asset_conv,
            )

        # Unverified valuation source
        no_source_conv = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            # No "source", "provider", or "valuation_policy"
        }
        with self.assertRaisesRegex(ValueError, "UNVERIFIED_VALUATION_SOURCE"):
            simulate_constant_product_amm(
                reserve0=10_000.0,
                reserve1=50_000.0,
                buy_notional_usd=100.0,
                chain=VALID_EVM_CHAIN,
                pool_address=VALID_POOL_ADDR,
                token0=VALID_TOKEN_ADDR,
                token1=VALID_QUOTE_ADDR,
                timestamp_utc=self.as_of_utc,
                expected_chain=VALID_EVM_CHAIN,
                expected_token_address=VALID_TOKEN_ADDR,
                expected_quote_asset=VALID_QUOTE_ADDR,
                quote_usd_conversion=no_source_conv,
            )

    # 40. Order book missing or incorrect denomination fails
    def test_40_order_book_missing_or_incorrect_denomination_fails(self):
        book_missing_denom = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token_address": VALID_TOKEN_ADDR,
            # No quote_currency, denomination, quote_asset, or quote_token
            "timestamp_utc": self.as_of_utc,
            "bids": [[99.0, 10.0]],
            "asks": [[101.0, 10.0]],
        }
        with self.assertRaisesRegex(ValueError, "MISSING_QUOTE_DENOMINATION"):
            simulate(
                book_missing_denom,
                buy_notional_usd=100.0,
                require_quote_denomination=True,
            )

        res = simulate_multi_size(
            book_missing_denom,
            sizes_usd=[100.0],
            as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR,
            require_quote_denomination=True,
        )
        self.assertEqual(res["size_evaluations"]["$100"]["status"], "MISSING_QUOTE_DENOMINATION")
        self.assertFalse(res["size_evaluations"]["$100"]["route_available"])

    # 41. Equivalent economic results across USD and non-USD quote-token representations
    def test_41_equivalent_economic_results_usd_vs_quote_token_units(self):
        # Pool A: 10,000 candidate tokens, 50,000 USDC @ $1.00 USD
        conv_usdc = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink_oracle_usd",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "observation": {
                "answer": 100000000,
                "decimals": 8,
                "round_id": 1,
                "updated_at": self.as_of_utc,
            },
        }
        sim_usdc = simulate_constant_product_amm(
            reserve0=10_000.0,
            reserve1=50_000.0,
            buy_notional_usd=100.0,
            fee_bps=30.0,
            chain=VALID_EVM_CHAIN,
            pool_address=VALID_POOL_ADDR,
            token0=VALID_TOKEN_ADDR,
            token1=VALID_QUOTE_ADDR,
            timestamp_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR,
            expected_quote_asset=VALID_QUOTE_ADDR,
            quote_usd_conversion=conv_usdc,
            require_quote_conversion=True,
        )

        # Pool B: 10,000 candidate tokens, 16.666666666666668 WETH @ $3000.00 USD (exact economic equivalence: $50,000 USD)
        conv_weth = {
            "price_usd": 3000.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_ALT_QUOTE,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink_oracle_eth_usd",
            "provider": "chainlink",
            "feed_address": "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419",
            "valuation_policy": "CRYPTO_ORACLE_VERIFIED",
            "observation": {
                "answer": 300000000000,
                "decimals": 8,
                "round_id": 1,
                "updated_at": self.as_of_utc,
            },
        }
        weth_reserve = 50_000.0 / 3000.0
        sim_weth = simulate_constant_product_amm(
            reserve0=10_000.0,
            reserve1=weth_reserve,
            buy_notional_usd=100.0,
            fee_bps=30.0,
            chain=VALID_EVM_CHAIN,
            pool_address=VALID_POOL_ADDR,
            token0=VALID_TOKEN_ADDR,
            token1=VALID_ALT_QUOTE,
            timestamp_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR,
            expected_quote_asset=VALID_ALT_QUOTE,
            quote_usd_conversion=conv_weth,
            require_quote_conversion=True,
        )

        # Verifications of invariance:
        # 1. Output token quantity must be identical
        self.assertAlmostEqual(sim_usdc["buy"]["filled_quantity"], sim_weth["buy"]["filled_quantity"], places=9)
        # 2. USD mid prices must match ($5.00/token)
        self.assertAlmostEqual(sim_usdc["mid_price"], sim_weth["mid_price"], places=9)
        self.assertAlmostEqual(sim_usdc["mid_price"], 5.0, places=9)
        # 3. Buy VWAP in USD must match
        self.assertAlmostEqual(sim_usdc["buy"]["vwap"], sim_weth["buy"]["vwap"], places=9)
        # 4. Price impact in bps must match
        self.assertAlmostEqual(sim_usdc["buy"]["price_impact_bps"], sim_weth["buy"]["price_impact_bps"], places=6)
        self.assertAlmostEqual(sim_usdc["immediate_exit"]["price_impact_bps"], sim_weth["immediate_exit"]["price_impact_bps"], places=6)
        # 5. Sell USD proceeds must match
        self.assertAlmostEqual(sim_usdc["immediate_exit"]["filled_quote_usd"], sim_weth["immediate_exit"]["filled_quote_usd"], places=6)
        # 6. Net PnL USD and return percentage must match
        self.assertAlmostEqual(sim_usdc["round_trip_estimate"]["net_pnl_usd"], sim_weth["round_trip_estimate"]["net_pnl_usd"], places=6)
        self.assertAlmostEqual(sim_usdc["round_trip_estimate"]["net_return_pct"], sim_weth["round_trip_estimate"]["net_return_pct"], places=6)

    # 42. Production qualification gate rejects missing or invalid conversion
    def test_42_production_gate_rejects_missing_or_invalid_conversion(self):
        # Case A: Missing conversion
        cand_no_conv = copy.deepcopy(self.valid_quote_source)
        del cand_no_conv["quote_usd_conversion"]
        cand_no_conv.update({
            "market_type": "dex_pool",
            "final_pool_address": VALID_POOL_ADDR,
            "final_token_contract": VALID_TOKEN_ADDR,
            "final_quote_chain": VALID_EVM_CHAIN,
            "final_quote_token_orientation_verified": True,
            "final_price_verified": True,
            "final_price_usd": 100.0,
            "target_price": 140.0,
            "target_price_source": "7D_SWING_RESISTANCE",
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
        })
        qualified_no_conv = v425.qualify_final_actionable_candidates([cand_no_conv], self.now_dt)
        self.assertEqual(len(qualified_no_conv), 0)
        self.assertTrue(any("quote-asset USD conversion" in b for b in cand_no_conv["qualification_blockers"]))

        # Case B: Stale conversion
        cand_stale_conv = copy.deepcopy(cand_no_conv)
        cand_stale_conv["quote_usd_conversion"] = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": "2026-11-13T11:45:00Z",  # 15 minutes old
            "source": "chainlink_oracle_usd",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
        }
        qualified_stale = v425.qualify_final_actionable_candidates([cand_stale_conv], self.now_dt)
        self.assertEqual(len(qualified_stale), 0)
        self.assertTrue(any("STALE_QUOTE_CONVERSION" in b for b in cand_stale_conv["qualification_blockers"]))

        # Case C: Asset mismatch conversion
        cand_mismatch_conv = copy.deepcopy(cand_no_conv)
        cand_mismatch_conv["quote_usd_conversion"] = {
            "price_usd": 3000.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_ALT_QUOTE,  # Mismatched to VALID_QUOTE_ADDR
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink_oracle_eth_usd",
            "provider": "chainlink",
            "feed_address": "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419",
            "valuation_policy": "CRYPTO_ORACLE_VERIFIED",
        }
        qualified_mismatch = v425.qualify_final_actionable_candidates([cand_mismatch_conv], self.now_dt)
        self.assertEqual(len(qualified_mismatch), 0)
        self.assertTrue(any("QUOTE_CONVERSION_ASSET_MISMATCH" in b for b in cand_mismatch_conv["qualification_blockers"]))

    # 43. Valid trusted-provider conversion with full provenance passes
    def test_43_valid_trusted_provider_conversion_passes(self):
        valid_conv = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink_oracle_usd",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "observation": {
                "answer": 100000000,
                "decimals": 8,
                "round_id": 1,
                "updated_at": self.as_of_utc,
            },
        }
        ok, status, price, details = validate_quote_usd_conversion(
            valid_conv,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertTrue(ok)
        self.assertEqual(status, "CONVERSION_VERIFIED")
        self.assertEqual(price, 1.0)
        self.assertEqual(details["provider"], "chainlink")
        self.assertEqual(details["provenance_id"], "0x8fa59a53f040d12d26f03bc9920373707c3c4004")
        self.assertFalse(details["cryptographic_authenticity_verified"])

    # 44. Arbitrary source string is rejected
    def test_44_arbitrary_source_string_rejected(self):
        conv_arbitrary_source = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "untrusted_arbitrary_oracle_network",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
        }
        ok, status, price, details = validate_quote_usd_conversion(
            conv_arbitrary_source,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "UNVERIFIED_VALUATION_SOURCE")
        self.assertIn("arbitrary or unrecognized valuation source", details["reason"])

    # 45. Arbitrary provider name is rejected
    def test_45_arbitrary_provider_name_rejected(self):
        conv_arbitrary_provider = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "provider": "untrusted_pricing_corp",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
        }
        ok, status, price, details = validate_quote_usd_conversion(
            conv_arbitrary_provider,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "UNVERIFIED_VALUATION_SOURCE")
        self.assertIn("arbitrary or unrecognized valuation provider", details["reason"])

    # 46. Unrecognized valuation policy is rejected
    def test_46_unrecognized_valuation_policy_rejected(self):
        conv_unrecognized_policy = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "TRUST_ME_BRO_POLICY",
        }
        ok, status, price, details = validate_quote_usd_conversion(
            conv_unrecognized_policy,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "UNRECOGNIZED_VALUATION_POLICY")
        self.assertIn("unrecognized valuation policy", details["reason"])

    # 47. Missing provider/feed provenance fails closed
    def test_47_missing_provider_feed_provenance_fails_closed(self):
        conv_no_provenance = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            # No feed_address, feed_id, pair_id, etc.
        }
        ok, status, price, details = validate_quote_usd_conversion(
            conv_no_provenance,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "MISSING_CONVERSION_PROVENANCE")
        self.assertIn("missing valuation provenance", details["reason"])

    # 48. simulate() rejects bare quote_usd_price when conversion is required
    def test_48_simulate_rejects_bare_quote_usd_price_when_conversion_required(self):
        book = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token_address": VALID_TOKEN_ADDR,
            "quote_asset": VALID_QUOTE_ADDR,
            "denomination": "USDC",
            "timestamp_utc": self.as_of_utc,
            "bids": [[99.0, 10.0]],
            "asks": [[101.0, 10.0]],
        }
        with self.assertRaisesRegex(ValueError, "MISSING_QUOTE_CONVERSION_EVIDENCE"):
            simulate(
                book,
                buy_notional_usd=100.0,
                quote_usd_price=1.0,
                require_quote_conversion=True,
            )

    # 49. simulate_constant_product_amm() rejects bare quote_usd_price when conversion is required
    def test_49_amm_rejects_bare_quote_usd_price_when_conversion_required(self):
        with self.assertRaisesRegex(ValueError, "MISSING_QUOTE_CONVERSION_EVIDENCE"):
            simulate_constant_product_amm(
                reserve0=10_000.0,
                reserve1=50_000.0,
                buy_notional_usd=100.0,
                chain=VALID_EVM_CHAIN,
                pool_address=VALID_POOL_ADDR,
                token0=VALID_TOKEN_ADDR,
                token1=VALID_QUOTE_ADDR,
                timestamp_utc=self.as_of_utc,
                expected_token_address=VALID_TOKEN_ADDR,
                expected_quote_asset=VALID_QUOTE_ADDR,
                quote_usd_price=1.0,
                require_quote_conversion=True,
            )

    # 50. simulate_multi_size() rejects missing or invalid conversion evidence
    def test_50_simulate_multi_size_rejects_missing_or_invalid_conversion_evidence(self):
        # Case A: Missing conversion evidence when required
        res_missing = simulate_multi_size(
            {"liquidity_usd": 50_000},
            sizes_usd=[100.0],
            require_quote_conversion=True,
        )
        self.assertEqual(res_missing["status"], "MISSING_QUOTE_CONVERSION_EVIDENCE")
        self.assertFalse(res_missing["size_evaluations"]["$100"]["route_available"])

        # Case B: Arbitrary source in conversion evidence
        res_arbitrary = simulate_multi_size(
            {
                "liquidity_usd": 50_000,
                "quote_usd_conversion": {
                    "price_usd": 1.0,
                    "chain": VALID_EVM_CHAIN,
                    "quote_asset": VALID_QUOTE_ADDR,
                    "reference_asset": "USD",
                    "timestamp_utc": self.as_of_utc,
                    "source": "fake_source",
                    "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
                    "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
                },
            },
            sizes_usd=[100.0],
            as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR,
            expected_quote_asset=VALID_QUOTE_ADDR,
            require_quote_conversion=True,
        )
        self.assertEqual(res_arbitrary["status"], "UNVERIFIED_VALUATION_SOURCE")
        self.assertFalse(res_arbitrary["size_evaluations"]["$100"]["route_available"])

    # 51. Production qualification gate cannot qualify a candidate using fabricated conversion provenance
    def test_51_production_gate_rejects_fabricated_conversion_provenance(self):
        cand_fake = copy.deepcopy(self.valid_quote_source)
        cand_fake.update({
            "market_type": "dex_pool",
            "final_pool_address": VALID_POOL_ADDR,
            "final_token_contract": VALID_TOKEN_ADDR,
            "final_quote_chain": VALID_EVM_CHAIN,
            "final_quote_token_orientation_verified": True,
            "final_price_verified": True,
            "final_price_usd": 100.0,
            "target_price": 140.0,
            "target_price_source": "7D_SWING_RESISTANCE",
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
            "quote_usd_conversion": {
                "price_usd": 1.0,
                "chain": VALID_EVM_CHAIN,
                "quote_asset": VALID_QUOTE_ADDR,
                "reference_asset": "USD",
                "timestamp_utc": self.as_of_utc,
                "source": "fabricated_oracle_network",
                "provider": "fabricated_provider",
                "valuation_policy": "FABRICATED_PEG_POLICY",
                "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            },
        })
        qualified = v425.qualify_final_actionable_candidates([cand_fake], self.now_dt)
        self.assertEqual(len(qualified), 0)
        self.assertTrue(any("quote-asset USD conversion verification failed" in b for b in cand_fake["qualification_blockers"]))

    # =========================================================================
    # MANDATORY REGRESSION TESTS: PROVIDER/FEED IDENTITY BINDING (1 - 15)
    # =========================================================================

    # 1. A genuinely registered Chainlink feed for the correct chain and asset passes
    def test_req01_genuinely_registered_chainlink_feed_passes(self):
        valid_conv = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "observation": {
                "answer": 100000000,
                "decimals": 8,
                "round_id": 1,
                "updated_at": self.as_of_utc,
            },
        }
        ok, status, price, details = validate_quote_usd_conversion(
            valid_conv,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertTrue(ok)
        self.assertEqual(status, "CONVERSION_VERIFIED")
        self.assertEqual(price, 1.0)
        self.assertEqual(details["provider"], "chainlink")
        self.assertEqual(details["provenance_id"], "0x8fa59a53f040d12d26f03bc9920373707c3c4004")
        self.assertEqual(details["usd_reference_asset"], "USD")
        self.assertFalse(details["cryptographic_authenticity_verified"])

    # 2. A fabricated but syntactically valid feed address fails
    def test_req02_fabricated_syntactically_valid_feed_address_fails(self):
        fake_feed_conv = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x1234567890123456789012345678901234567890",  # Syntactically valid EVM hex, but fabricated
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
        }
        ok, status, price, details = validate_quote_usd_conversion(
            fake_feed_conv,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "UNRECOGNIZED_FEED_ADDRESS")
        self.assertIn("not registered in trusted feed registry", details["reason"])

    # 3. A real feed address associated with another asset fails
    def test_req03_real_feed_address_associated_with_another_asset_fails(self):
        # 0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419 is genuine Chainlink ETH/USD, but quote asset is USDC
        mismatch_asset_conv = {
            "price_usd": 3000.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,  # USDC
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419",  # ETH/USD feed
            "valuation_policy": "CRYPTO_ORACLE_VERIFIED",
        }
        ok, status, price, details = validate_quote_usd_conversion(
            mismatch_asset_conv,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "QUOTE_CONVERSION_ASSET_MISMATCH")
        self.assertIn("registered for quote asset", details["reason"])

    # 4. A feed associated with another chain fails
    def test_req04_feed_associated_with_another_chain_fails(self):
        # 0x50834f3163758fcc1df9973b6e91f0f0f0434ac3 is Arbitrum USDC/USD feed, supplied for Ethereum chain
        mismatch_chain_conv = {
            "price_usd": 1.0,
            "chain": "ethereum",
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x50834f3163758fcc1df9973b6e91f0f0f0434ac3",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
        }
        ok, status, price, details = validate_quote_usd_conversion(
            mismatch_chain_conv,
            expected_chain="ethereum",
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "QUOTE_CONVERSION_CHAIN_MISMATCH")
        self.assertIn("registered on chain 'arbitrum', not 'ethereum'", details["reason"])

    # 5. Recognized source="chainlink" combined with provider="pyth" fails
    def test_req05_recognized_source_with_different_provider_fails(self):
        conflicting_conv = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "pyth",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
        }
        ok, status, price, details = validate_quote_usd_conversion(
            conflicting_conv,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "CONFLICTING_PROVIDER_EVIDENCE")
        self.assertIn("conflicting provider evidence", details["reason"])

    # 6. A fabricated Pyth feed ID fails
    def test_req06_fabricated_pyth_feed_id_fails(self):
        fake_pyth_conv = {
            "price_usd": 1.0,
            "chain": "solana",
            "quote_asset": VALID_SOLANA_QUOTE,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "pyth",
            "provider": "pyth",
            "feed_id": "0x1111111111111111111111111111111111111111111111111111111111111111",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
        }
        ok, status, price, details = validate_quote_usd_conversion(
            fake_pyth_conv,
            expected_chain="solana",
            expected_quote_asset=VALID_SOLANA_QUOTE,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "UNRECOGNIZED_FEED_ID")
        self.assertIn("not registered in trusted feed registry", details["reason"])

    # 7. An arbitrary source URL fails
    def test_req07_arbitrary_source_url_fails(self):
        bad_url_conv = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "coinbase",
            "provider": "coinbase",
            "source_url": "https://untrusted-api.market-oracle.xyz/products/USDC-USD/ticker",
            "market_id": "USDC-USD",
            "valuation_policy": "CEX_MARKET_TICKER_VERIFIED",
        }
        ok, status, price, details = validate_quote_usd_conversion(
            bad_url_conv,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "UNTRUSTED_SOURCE_URL")
        self.assertIn("untrusted source URL domain", details["reason"])

    # 8. An arbitrary pair or market ID fails
    def test_req08_arbitrary_pair_or_market_id_fails(self):
        bad_market_conv = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "coinbase",
            "provider": "coinbase",
            "market_id": "FABRICATED-USDC-USD-PAIR",
            "source_url": "https://api.exchange.coinbase.com/products/FABRICATED/ticker",
            "valuation_policy": "CEX_MARKET_TICKER_VERIFIED",
        }
        ok, status, price, details = validate_quote_usd_conversion(
            bad_market_conv,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "UNRECOGNIZED_MARKET_IDENTITY")
        self.assertIn("unrecognized market identifier", details["reason"])

    # 9. An incompatible valuation policy fails
    def test_req09_incompatible_valuation_policy_fails(self):
        incompat_conv = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "CEX_MARKET_TICKER_VERIFIED",  # Incompatible with Chainlink
        }
        ok, status, price, details = validate_quote_usd_conversion(
            incompat_conv,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "INCOMPATIBLE_VALUATION_POLICY")
        self.assertIn("is incompatible with provider 'chainlink'", details["reason"])

    # 10. Missing USD reference identity fails
    def test_req10_missing_usd_reference_identity_fails(self):
        no_usd_ref_conv = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            # No reference_asset or usd_reference_asset
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
        }
        ok, status, price, details = validate_quote_usd_conversion(
            no_usd_ref_conv,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "MISSING_USD_REFERENCE_IDENTITY")
        self.assertIn("missing USD reference asset identity", details["reason"])

    # 11. Stale and future-dated observations fail
    def test_req11_stale_and_future_dated_observations_fail(self):
        stale_conv = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": "2026-11-13T11:45:00Z",  # 15 minutes old (> 300s)
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
        }
        ok, status, _, details = validate_quote_usd_conversion(
            stale_conv,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "STALE_QUOTE_CONVERSION")

        future_conv = copy.deepcopy(stale_conv)
        future_conv["timestamp_utc"] = "2026-11-13T12:05:00Z"  # Future-dated
        ok_fut, status_fut, _, details_fut = validate_quote_usd_conversion(
            future_conv,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok_fut)
        self.assertEqual(status_fut, "FUTURE_DATED_QUOTE_CONVERSION")

    # 12. A conversion with correct names but incorrect provider/feed binding cannot qualify through simulate_multi_size()
    def test_req12_conversion_with_incorrect_feed_binding_cannot_qualify_through_simulate_multi_size(self):
        res = simulate_multi_size(
            {
                "liquidity_usd": 100_000,
                "quote_usd_conversion": {
                    "price_usd": 1.0,
                    "chain": VALID_EVM_CHAIN,
                    "quote_asset": VALID_QUOTE_ADDR,
                    "reference_asset": "USD",
                    "timestamp_utc": self.as_of_utc,
                    "source": "chainlink",
                    "provider": "chainlink",
                    "feed_address": "0x1111111111111111111111111111111111111111",  # Fabricated feed
                    "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
                },
            },
            sizes_usd=[100.0],
            as_of_utc=self.as_of_utc,
            expected_token_address=VALID_TOKEN_ADDR,
            expected_quote_asset=VALID_QUOTE_ADDR,
            require_quote_conversion=True,
        )
        self.assertEqual(res["status"], "UNRECOGNIZED_FEED_ADDRESS")
        self.assertFalse(res["size_evaluations"]["$100"]["route_available"])

    # 13. The production qualify_final_actionable_candidates() gate rejects fabricated feed provenance
    def test_req13_production_gate_rejects_fabricated_feed_provenance(self):
        cand = copy.deepcopy(self.valid_quote_source)
        cand.update({
            "market_type": "dex_pool",
            "final_pool_address": VALID_POOL_ADDR,
            "final_token_contract": VALID_TOKEN_ADDR,
            "final_quote_chain": VALID_EVM_CHAIN,
            "final_quote_token_orientation_verified": True,
            "final_price_verified": True,
            "final_price_usd": 100.0,
            "target_price": 140.0,
            "target_price_source": "7D_SWING_RESISTANCE",
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
            "quote_usd_conversion": {
                "price_usd": 1.0,
                "chain": VALID_EVM_CHAIN,
                "quote_asset": VALID_QUOTE_ADDR,
                "reference_asset": "USD",
                "timestamp_utc": self.as_of_utc,
                "source": "chainlink",
                "provider": "chainlink",
                "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
                "feed_address": "0x9999999999999999999999999999999999999999",  # Fabricated feed
            },
        })
        qualified = v425.qualify_final_actionable_candidates([cand], self.now_dt)
        self.assertEqual(len(qualified), 0)
        self.assertTrue(any("quote-asset USD conversion verification failed: UNRECOGNIZED_FEED_ADDRESS" in b for b in cand["qualification_blockers"]))

    # 14. A valid registered conversion still produces consistent USD-normalized AMM and order-book calculations
    def test_req14_valid_registered_conversion_produces_consistent_usd_normalized_calculations(self):
        # 14a. Constant Product AMM with registered ETH/USD feed
        conv_weth = {
            "price_usd": 3000.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_ALT_QUOTE,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419",
            "valuation_policy": "CRYPTO_ORACLE_VERIFIED",
            "observation": {
                "answer": 300000000000,
                "decimals": 8,
                "round_id": 1,
                "updated_at": self.as_of_utc,
            },
        }
        amm_res = simulate_constant_product_amm(
            reserve0=10_000.0,
            reserve1=50_000.0 / 3000.0,
            buy_notional_usd=100.0,
            fee_bps=30.0,
            chain=VALID_EVM_CHAIN,
            pool_address=VALID_POOL_ADDR,
            token0=VALID_TOKEN_ADDR,
            token1=VALID_ALT_QUOTE,
            timestamp_utc=self.as_of_utc,
            expected_chain=VALID_EVM_CHAIN,
            expected_pool=VALID_POOL_ADDR,
            expected_token_address=VALID_TOKEN_ADDR,
            expected_quote_asset=VALID_ALT_QUOTE,
            quote_usd_conversion=conv_weth,
            require_quote_conversion=True,
        )
        self.assertEqual(amm_res["status"], "FULL_ROUND_TRIP_AMM_LIQUIDITY_AVAILABLE")
        self.assertEqual(amm_res["quote_usd_price"], 3000.0)
        self.assertAlmostEqual(amm_res["buy"]["filled_quote_tokens"], 100.0 / 3000.0, places=6)
        self.assertEqual(amm_res["buy"]["filled_quote_usd"], 100.0)

        # 14b. Order book with registered USDC/USD feed
        book = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token_address": VALID_TOKEN_ADDR,
            "quote_asset": VALID_QUOTE_ADDR,
            "denomination": "USDC",
            "timestamp_utc": self.as_of_utc,
            "bids": [[99.0, 10.0]],
            "asks": [[101.0, 10.0]],
        }
        ob_res = simulate(
            book,
            buy_notional_usd=100.0,
            fee_bps=10.0,
            quote_usd_conversion=self.valid_quote_conversion,
            require_quote_conversion=True,
        )
        self.assertEqual(ob_res["status"], "FULL_ROUND_TRIP_DEPTH_AVAILABLE")
        self.assertEqual(ob_res["quote_usd_price"], 1.0)
        self.assertEqual(ob_res["buy"]["filled_quote_usd"], 100.0)

    # 15. Existing strict-mode bare numeric price rejection continues to work in both simulate() and simulate_constant_product_amm()
    def test_req15_strict_mode_bare_numeric_price_rejection_continues_to_work(self):
        book = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token_address": VALID_TOKEN_ADDR,
            "quote_asset": VALID_QUOTE_ADDR,
            "denomination": "USDC",
            "timestamp_utc": self.as_of_utc,
            "bids": [[99.0, 10.0]],
            "asks": [[101.0, 10.0]],
        }
        with self.assertRaisesRegex(ValueError, "MISSING_QUOTE_CONVERSION_EVIDENCE"):
            simulate(book, buy_notional_usd=100.0, quote_usd_price=1.0, require_quote_conversion=True)

        with self.assertRaisesRegex(ValueError, "MISSING_QUOTE_CONVERSION_EVIDENCE"):
            simulate_constant_product_amm(
                reserve0=10_000.0,
                reserve1=50_000.0,
                buy_notional_usd=100.0,
                chain=VALID_EVM_CHAIN,
                pool_address=VALID_POOL_ADDR,
                token0=VALID_TOKEN_ADDR,
                token1=VALID_QUOTE_ADDR,
                timestamp_utc=self.as_of_utc,
                expected_token_address=VALID_TOKEN_ADDR,
                expected_quote_asset=VALID_QUOTE_ADDR,
                quote_usd_price=1.0,
                require_quote_conversion=True,
            )

    # =========================================================================
    # MANDATORY REGRESSION TESTS: USD VALUATION INTEGRITY & OBSERVATION CHECKS (16 - 30)
    # =========================================================================

    # 16. USDT deviating from $1 with chained conversion compounds correctly
    def test_req16_usdt_deviating_from_one_dollar_chained_conversion(self):
        # Binance ETHUSDT where ETH = 3000 USDT, chained USDT/USD Chainlink oracle = $0.9850 (depegged from $1.00)
        eth_usdt_conv = {
            "price_usd": 3000.0,
            "chain": "ethereum",
            "quote_asset": VALID_ALT_QUOTE,  # WETH on Ethereum
            "reference_asset": "USDT",
            "timestamp_utc": self.as_of_utc,
            "source": "binance",
            "provider": "binance",
            "market_id": "ETHUSDT",
            "valuation_policy": "CEX_MARKET_TICKER_VERIFIED",
            "observation": {
                "symbol": "ETHUSDT",
                "last_price": 3000.0,
                "bid_price": 2999.9,
                "ask_price": 3000.1,
                "volume_24h": 150000.0,
            },
            "chained_conversion": {
                "price_usd": 0.9850,
                "chain": "ethereum",
                "quote_asset": "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
                "reference_asset": "USD",
                "timestamp_utc": self.as_of_utc,
                "source": "chainlink",
                "provider": "chainlink",
                "feed_address": "0x3e7d1eab03480b83a7d35d7647822ec73a6858c6",  # USDT/USD feed
                "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
                "observation": {
                    "answer": 98500000,  # 0.9850 * 10^8
                    "decimals": 8,
                    "round_id": 100,
                    "updated_at": self.as_of_utc,
                },
            },
        }
        ok, status, compound_price, details = validate_quote_usd_conversion(
            eth_usdt_conv,
            expected_chain="ethereum",
            expected_quote_asset=VALID_ALT_QUOTE,
            as_of_utc=self.as_of_utc,
        )
        self.assertTrue(ok)
        self.assertEqual(status, "CONVERSION_VERIFIED")
        # 3000.0 * 0.9850 = 2955.0 USD (not assuming $3000.00!)
        self.assertAlmostEqual(compound_price, 2955.0, places=4)
        self.assertEqual(details["primary_price"], 3000.0)
        self.assertAlmostEqual(details["chained_conversion"]["price_usd"], 0.9850, places=4)

    # 17. Missing USDT/USD conversion fails closed
    def test_req17_missing_usdt_usd_conversion_fails_closed(self):
        eth_usdt_missing_chained = {
            "price_usd": 3000.0,
            "chain": "ethereum",
            "quote_asset": VALID_ALT_QUOTE,
            "reference_asset": "USDT",
            "timestamp_utc": self.as_of_utc,
            "source": "binance",
            "provider": "binance",
            "market_id": "ETHUSDT",
            "valuation_policy": "CEX_MARKET_TICKER_VERIFIED",
            "observation": {
                "symbol": "ETHUSDT",
                "last_price": 3000.0,
                "bid_price": 2999.9,
                "ask_price": 3000.1,
                "volume_24h": 150000.0,
            },
            # Missing chained_conversion!
        }
        ok, status, price, details = validate_quote_usd_conversion(
            eth_usdt_missing_chained,
            expected_chain="ethereum",
            expected_quote_asset=VALID_ALT_QUOTE,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "MISSING_CHAINED_USD_CONVERSION")
        self.assertIsNone(price)

    # 18. Stale USDT/USD conversion fails closed
    def test_req18_stale_usdt_usd_conversion_fails_closed(self):
        eth_usdt_stale_chained = {
            "price_usd": 3000.0,
            "chain": "ethereum",
            "quote_asset": VALID_ALT_QUOTE,
            "reference_asset": "USDT",
            "timestamp_utc": self.as_of_utc,
            "source": "binance",
            "provider": "binance",
            "market_id": "ETHUSDT",
            "valuation_policy": "CEX_MARKET_TICKER_VERIFIED",
            "observation": {
                "symbol": "ETHUSDT",
                "last_price": 3000.0,
                "bid_price": 2999.9,
                "ask_price": 3000.1,
                "volume_24h": 150000.0,
            },
            "chained_conversion": {
                "price_usd": 1.0,
                "chain": "ethereum",
                "quote_asset": "0xdac17f958d2ee523a2206206994597c13d831ec7",
                "reference_asset": "USD",
                "timestamp_utc": "2026-11-13T11:50:00Z",  # 10 minutes old (> 300s)
                "source": "chainlink",
                "provider": "chainlink",
                "feed_address": "0x3e7d1eab03480b83a7d35d7647822ec73a6858c6",
                "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
                "observation": {
                    "answer": 100000000,
                    "decimals": 8,
                    "round_id": 100,
                    "updated_at": "2026-11-13T11:50:00Z",
                },
            },
        }
        ok, status, price, details = validate_quote_usd_conversion(
            eth_usdt_stale_chained,
            expected_chain="ethereum",
            expected_quote_asset=VALID_ALT_QUOTE,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "STALE_CHAINED_USD_CONVERSION")
        self.assertIsNone(price)

    # 19. Correct chained conversion in multi-size simulation
    def test_req19_correct_chained_conversion_multi_size_simulation(self):
        eth_usdt_conv = {
            "price_usd": 3000.0,
            "chain": "ethereum",
            "quote_asset": VALID_ALT_QUOTE,
            "reference_asset": "USDT",
            "timestamp_utc": self.as_of_utc,
            "source": "binance",
            "provider": "binance",
            "market_id": "ETHUSDT",
            "valuation_policy": "CEX_MARKET_TICKER_VERIFIED",
            "observation": {
                "symbol": "ETHUSDT",
                "last_price": 3000.0,
                "bid_price": 2999.9,
                "ask_price": 3000.1,
                "volume_24h": 150000.0,
            },
            "chained_conversion": {
                "price_usd": 0.9990,
                "chain": "ethereum",
                "quote_asset": "0xdac17f958d2ee523a2206206994597c13d831ec7",
                "reference_asset": "USD",
                "timestamp_utc": self.as_of_utc,
                "source": "chainlink",
                "provider": "chainlink",
                "feed_address": "0x3e7d1eab03480b83a7d35d7647822ec73a6858c6",
                "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
                "observation": {
                    "answer": 99900000,
                    "decimals": 8,
                    "round_id": 100,
                    "updated_at": self.as_of_utc,
                },
            },
        }
        res = simulate_multi_size({
            "chain": "ethereum",
            "pool_address": VALID_POOL_ADDR,
            "contract_address": VALID_TOKEN_ADDR,
            "intended_trade_size_usd": 100.0,
            "quote_token_address": VALID_ALT_QUOTE,
            "expected_quote_asset": VALID_ALT_QUOTE,
            "liquidity_usd": 100_000.0,
            "final_price_timestamp_utc": self.as_of_utc,
            "quote_usd_conversion": eth_usdt_conv,
            "amm_reserves": {
                "chain": "ethereum",
                "pool_address": VALID_POOL_ADDR,
                "token0": VALID_TOKEN_ADDR,
                "token1": VALID_ALT_QUOTE,
                "reserve0": 100_000.0,
                "reserve1": 50.0,  # 50 WETH
                "timestamp_utc": self.as_of_utc,
                "fee_bps": 30.0,
            },
        }, as_of_utc=self.as_of_utc)
        self.assertEqual(res["size_evaluations"]["$100"]["status"], "EXECUTABLE_AMM_ROUTE_AVAILABLE")
        # Compound price: 3000 * 0.9990 = 2997.0
        self.assertAlmostEqual(res["size_evaluations"]["$100"]["quote_usd_price"], 2997.0, places=3)

    # 20. Incorrect conversion asset in chained leg fails closed
    def test_req20_incorrect_conversion_asset_in_chained_leg_fails_closed(self):
        # Chained conversion for USDT/USD accidentally specifies an ETH quote asset address
        eth_usdt_bad_chained_asset = {
            "price_usd": 3000.0,
            "chain": "ethereum",
            "quote_asset": VALID_ALT_QUOTE,
            "reference_asset": "USDT",
            "timestamp_utc": self.as_of_utc,
            "source": "binance",
            "provider": "binance",
            "market_id": "ETHUSDT",
            "valuation_policy": "CEX_MARKET_TICKER_VERIFIED",
            "observation": {
                "symbol": "ETHUSDT",
                "last_price": 3000.0,
                "bid_price": 2999.9,
                "ask_price": 3000.1,
                "volume_24h": 150000.0,
            },
            "chained_conversion": {
                "price_usd": 3000.0,
                "chain": "ethereum",
                "quote_asset": VALID_ALT_QUOTE,  # WETH instead of USDT!
                "reference_asset": "USD",
                "timestamp_utc": self.as_of_utc,
                "source": "chainlink",
                "provider": "chainlink",
                "feed_address": "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419",
                "valuation_policy": "CRYPTO_ORACLE_VERIFIED",
                "observation": {
                    "answer": 300000000000,
                    "decimals": 8,
                    "round_id": 100,
                    "updated_at": self.as_of_utc,
                },
            },
        }
        ok, status, price, details = validate_quote_usd_conversion(
            eth_usdt_bad_chained_asset,
            expected_chain="ethereum",
            expected_quote_asset=VALID_ALT_QUOTE,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "QUOTE_CONVERSION_ASSET_MISMATCH")
        self.assertIsNone(price)

    # 21. Candidate that attempts to label USDT as USD fails closed
    def test_req21_candidate_attempting_to_label_usdt_as_usd_fails_closed(self):
        # Binance USDCUSDT quotes in USDT, but evidence attempts to label reference_asset as USD
        binance_label_usdt_as_usd = {
            "price_usd": 1.0,
            "chain": "ethereum",
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",  # False labeling of USDT market!
            "timestamp_utc": self.as_of_utc,
            "source": "binance",
            "provider": "binance",
            "market_id": "USDCUSDT",
            "valuation_policy": "CEX_MARKET_TICKER_VERIFIED",
            "observation": {
                "symbol": "USDCUSDT",
                "last_price": 1.0,
                "bid_price": 0.9999,
                "ask_price": 1.0001,
                "volume_24h": 50000000.0,
            },
        }
        ok, status, price, details = validate_quote_usd_conversion(
            binance_label_usdt_as_usd,
            expected_chain="ethereum",
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "REFERENCE_CURRENCY_MISMATCH")
        self.assertIn("quotes in USDT, not USD", details["reason"])

        # And production qualification gate rejects it
        cand = copy.deepcopy(self.valid_quote_source)
        cand["quote_usd_conversion"] = binance_label_usdt_as_usd
        cand["amm_reserves"] = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token0": VALID_TOKEN_ADDR,
            "token1": VALID_QUOTE_ADDR,
            "reserve0": 100_000.0,
            "reserve1": 50_000.0,
            "timestamp_utc": self.as_of_utc,
            "fee_bps": 30.0,
        }
        qualified = v425.qualify_final_actionable_candidates([cand], self.now_dt)
        self.assertEqual(len(qualified), 0)
        self.assertTrue(any("REFERENCE_CURRENCY_MISMATCH" in b for b in cand["qualification_blockers"]))

    # 22. Registered feed ID with fabricated price rejected (OBSERVATION_PRICE_DISCREPANCY)
    def test_req22_registered_feed_with_fabricated_price_rejected(self):
        # Claimed price is $1.00, but Chainlink returned answer is 200000000 ($2.00)
        conv_price_discrepancy = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "observation": {
                "answer": 200000000,  # $2.00, does not match claimed $1.00!
                "decimals": 8,
                "round_id": 5,
                "updated_at": self.as_of_utc,
            },
        }
        ok, status, price, details = validate_quote_usd_conversion(
            conv_price_discrepancy,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "OBSERVATION_PRICE_DISCREPANCY")
        self.assertIn("does not match claimed price", details["reason"])

    # 23. Registered feed ID with incorrect returned asset rejected (QUOTE_CONVERSION_ASSET_MISMATCH)
    def test_req23_registered_feed_with_incorrect_returned_asset_rejected(self):
        # Genuine feed 0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419 (ETH/USD), but quote asset is USDC
        conv_asset_mismatch = {
            "price_usd": 3000.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,  # USDC
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419",  # ETH/USD feed
            "valuation_policy": "CRYPTO_ORACLE_VERIFIED",
            "observation": {
                "answer": 300000000000,
                "decimals": 8,
                "round_id": 1,
                "updated_at": self.as_of_utc,
            },
        }
        ok, status, price, details = validate_quote_usd_conversion(
            conv_asset_mismatch,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "QUOTE_CONVERSION_ASSET_MISMATCH")

    # 24. Valid provider responses pass across oracles and CEX
    def test_req24_valid_provider_responses_pass(self):
        # 24a. Chainlink Ethereum USDC/USD
        cl_conv = {
            "price_usd": 1.0,
            "chain": "ethereum",
            "quote_asset": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "observation": {
                "answer": 100000000,
                "decimals": 8,
                "round_id": 42,
                "updated_at": self.as_of_utc,
            },
        }
        ok_cl, status_cl, price_cl, _ = validate_quote_usd_conversion(cl_conv, as_of_utc=self.as_of_utc)
        self.assertTrue(ok_cl)
        self.assertEqual(status_cl, "CONVERSION_VERIFIED")
        self.assertEqual(price_cl, 1.0)

        # 24b. Pyth Solana USDC/USD
        pyth_conv = {
            "price_usd": 1.0,
            "chain": "solana",
            "quote_asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "pyth",
            "provider": "pyth",
            "feed_id": "0xeaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "observation": {
                "price": 100000000,
                "expo": -8,
                "publish_time": self.as_of_utc,
            },
        }
        ok_py, status_py, price_py, _ = validate_quote_usd_conversion(pyth_conv, as_of_utc=self.as_of_utc)
        self.assertTrue(ok_py)
        self.assertEqual(status_py, "CONVERSION_VERIFIED")
        self.assertEqual(price_py, 1.0)

        # 24c. Coinbase USDC-USD
        cb_conv = {
            "price_usd": 1.0,
            "chain": "ethereum",
            "quote_asset": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "coinbase",
            "provider": "coinbase",
            "market_id": "USDC-USD",
            "valuation_policy": "CEX_MARKET_TICKER_VERIFIED",
            "observation": {
                "symbol": "USDC-USD",
                "last_price": 1.0,
                "bid_price": 0.9999,
                "ask_price": 1.0001,
                "volume_24h": 45000000.0,
            },
        }
        ok_cb, status_cb, price_cb, _ = validate_quote_usd_conversion(cb_conv, as_of_utc=self.as_of_utc)
        self.assertTrue(ok_cb)
        self.assertEqual(status_cb, "CONVERSION_VERIFIED")
        self.assertEqual(price_cb, 1.0)

    # 25. Registered pool with fabricated TWAP rejected
    def test_req25_registered_pool_with_fabricated_twap_rejected(self):
        # 25a. Window < 60s rejected
        twap_short_window = {
            "price_usd": 3000.0,
            "chain": "ethereum",
            "quote_asset": VALID_ALT_QUOTE,
            "reference_asset": "USDC",
            "timestamp_utc": self.as_of_utc,
            "source": "uniswap_v3_twap",
            "provider": "uniswap_v3_twap",
            "pool_address": "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
            "valuation_policy": "ON_CHAIN_TWAP_VERIFIED",
            "observation": {
                "twap_window_seconds": 30,  # Below minimum 60s
            },
            "usd_conversion_leg": self.valid_quote_conversion,
        }
        ok_win, status_win, _, _ = validate_quote_usd_conversion(twap_short_window, as_of_utc=self.as_of_utc)
        self.assertFalse(ok_win)
        self.assertEqual(status_win, "INVALID_TWAP_WINDOW")

        # 25b. TWAP price discrepancy rejected
        twap_discrepancy = {
            "price_usd": 3000.0,
            "chain": "ethereum",
            "quote_asset": VALID_ALT_QUOTE,
            "reference_asset": "USDC",
            "timestamp_utc": self.as_of_utc,
            "source": "uniswap_v3_twap",
            "provider": "uniswap_v3_twap",
            "pool_address": "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
            "valuation_policy": "ON_CHAIN_TWAP_VERIFIED",
            "observation": {
                "twap_window_seconds": 180,
                "twap_price": 1500.0,  # Discrepant from claimed 3000.0
            },
            "usd_conversion_leg": self.valid_quote_conversion,
        }
        ok_disc, status_disc, _, _ = validate_quote_usd_conversion(twap_discrepancy, as_of_utc=self.as_of_utc)
        self.assertFalse(ok_disc)
        self.assertEqual(status_disc, "OBSERVATION_PRICE_DISCREPANCY")

    # 26. Recognized CEX market with fabricated response rejected
    def test_req26_recognized_cex_market_with_fabricated_response_rejected(self):
        # 26a. Crossed/inverted market: bid > ask
        cex_crossed = {
            "price_usd": 1.0,
            "chain": "ethereum",
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "coinbase",
            "provider": "coinbase",
            "market_id": "USDC-USD",
            "valuation_policy": "CEX_MARKET_TICKER_VERIFIED",
            "observation": {
                "symbol": "USDC-USD",
                "last_price": 1.0,
                "bid_price": 1.05,
                "ask_price": 0.95,  # Crossed: bid > ask
                "volume_24h": 1000000.0,
            },
        }
        ok_inv, status_inv, _, details_inv = validate_quote_usd_conversion(cex_crossed, as_of_utc=self.as_of_utc)
        self.assertFalse(ok_inv)
        self.assertEqual(status_inv, "INVALID_CEX_OBSERVATION")
        self.assertIn("crossed/inverted", details_inv["reason"])

        # 26b. Non-positive volume rejected
        cex_zero_vol = copy.deepcopy(cex_crossed)
        cex_zero_vol["observation"]["bid_price"] = 0.9999
        cex_zero_vol["observation"]["ask_price"] = 1.0001
        cex_zero_vol["observation"]["volume_24h"] = 0.0  # Zero volume
        ok_vol, status_vol, _, _ = validate_quote_usd_conversion(cex_zero_vol, as_of_utc=self.as_of_utc)
        self.assertFalse(ok_vol)
        self.assertEqual(status_vol, "INVALID_CEX_OBSERVATION")

        # 26c. Observation market symbol mismatch rejected
        cex_mismatched_sym = copy.deepcopy(cex_crossed)
        cex_mismatched_sym["observation"]["bid_price"] = 0.9999
        cex_mismatched_sym["observation"]["ask_price"] = 1.0001
        cex_mismatched_sym["observation"]["symbol"] = "ETH-USD"  # Registered is USDC-USD
        ok_sym, status_sym, _, _ = validate_quote_usd_conversion(cex_mismatched_sym, as_of_utc=self.as_of_utc)
        self.assertFalse(ok_sym)
        self.assertEqual(status_sym, "OBSERVATION_MARKET_MISMATCH")

    # 27. DEX pair ID cannot independently prove USD conversion (UNVERIFIED_USD_CONVERSION_PATH)
    def test_req27_dex_pair_id_cannot_independently_prove_usd_conversion(self):
        # GeckoTerminal pool represents USDC/WETH ratio on-chain, NOT direct USD
        gecko_pair_without_leg = {
            "price_usd": 1.0,
            "chain": "ethereum",
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USDC",
            "timestamp_utc": self.as_of_utc,
            "source": "geckoterminal",
            "provider": "geckoterminal",
            "feed_id": "eth_0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
            "valuation_policy": "DEX_AGGREGATOR_VERIFIED",
            "observation": {
                "price": 1.0,
                "token": VALID_QUOTE_ADDR,
                "chain": "ethereum",
                "timestamp": self.as_of_utc,
            },
            # Missing usd_conversion_leg!
        }
        ok, status, price, details = validate_quote_usd_conversion(gecko_pair_without_leg, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "UNVERIFIED_USD_CONVERSION_PATH")
        self.assertIn("requires verified usd_conversion_leg to USD", details["reason"])

        # With verified usd_conversion_leg, it passes and compounds
        gecko_pair_with_leg = copy.deepcopy(gecko_pair_without_leg)
        gecko_pair_with_leg["usd_conversion_leg"] = self.valid_quote_conversion
        ok_leg, status_leg, price_leg, details_leg = validate_quote_usd_conversion(gecko_pair_with_leg, as_of_utc=self.as_of_utc)
        self.assertTrue(ok_leg)
        self.assertEqual(status_leg, "CONVERSION_VERIFIED")
        self.assertEqual(price_leg, 1.0)

    # 28. USDT never silently treated as USD
    def test_req28_usdt_never_silently_treated_as_usd(self):
        # 28a. reference_asset='USDT' rejected without chained conversion
        usdt_bare_ref = {
            "price_usd": 1.0,
            "chain": "ethereum",
            "quote_asset": "0xdac17f958d2ee523a2206206994597c13d831ec7",
            "reference_asset": "USDT",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x3e7d1eab03480b83a7d35d7647822ec73a6858c6",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "observation": {
                "answer": 100000000,
                "decimals": 8,
                "round_id": 1,
                "updated_at": self.as_of_utc,
            },
        }
        ok, status, _, details = validate_quote_usd_conversion(usdt_bare_ref, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "MISSING_CHAINED_USD_CONVERSION")

        # 28b. Binance SOLUSDT claiming reference_asset='USD' rejected
        binance_sol_usd = {
            "price_usd": 150.0,
            "chain": "solana",
            "quote_asset": "So11111111111111111111111111111111111111112",
            "reference_asset": "USD",  # Wrong! Market quotes in USDT!
            "timestamp_utc": self.as_of_utc,
            "source": "binance",
            "provider": "binance",
            "market_id": "SOLUSDT",
            "valuation_policy": "CEX_MARKET_TICKER_VERIFIED",
            "observation": {
                "symbol": "SOLUSDT",
                "last_price": 150.0,
                "bid_price": 149.9,
                "ask_price": 150.1,
                "volume_24h": 200000.0,
            },
        }
        ok_sol, status_sol, _, details_sol = validate_quote_usd_conversion(binance_sol_usd, as_of_utc=self.as_of_utc)
        self.assertFalse(ok_sol)
        self.assertEqual(status_sol, "REFERENCE_CURRENCY_MISMATCH")

    # 29. All conversion paths fail closed when any leg is missing or stale
    def test_req29_all_conversion_paths_fail_closed_when_any_leg_is_missing_or_stale(self):
        # Uniswap v3 TWAP with stale chained conversion leg
        twap_stale_leg = {
            "price_usd": 3000.0,
            "chain": "ethereum",
            "quote_asset": VALID_ALT_QUOTE,
            "reference_asset": "USDC",
            "timestamp_utc": self.as_of_utc,
            "source": "uniswap_v3_twap",
            "provider": "uniswap_v3_twap",
            "pool_address": "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
            "valuation_policy": "ON_CHAIN_TWAP_VERIFIED",
            "observation": {
                "twap_window_seconds": 180,
                "twap_price": 3000.0,
            },
            "usd_conversion_leg": {
                "price_usd": 1.0,
                "chain": "ethereum",
                "quote_asset": VALID_QUOTE_ADDR,
                "reference_asset": "USD",
                "timestamp_utc": "2026-11-13T11:40:00Z",  # Stale: 20 minutes old
                "source": "chainlink",
                "provider": "chainlink",
                "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
                "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
                "observation": {
                    "answer": 100000000,
                    "decimals": 8,
                    "round_id": 1,
                    "updated_at": "2026-11-13T11:40:00Z",
                },
            },
        }
        ok, status, price, details = validate_quote_usd_conversion(twap_stale_leg, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "STALE_CHAINED_USD_CONVERSION")

    # 30. Pyth observation price discrepancy and missing fields
    def test_req30_pyth_observation_price_discrepancy_and_missing_fields(self):
        # 30a. Missing expo
        pyth_missing_expo = {
            "price_usd": 1.0,
            "chain": "solana",
            "quote_asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "pyth",
            "provider": "pyth",
            "feed_id": "0xeaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "observation": {
                "price": 100000000,
                # Missing expo!
                "publish_time": self.as_of_utc,
            },
        }
        ok_exp, status_exp, _, _ = validate_quote_usd_conversion(pyth_missing_expo, as_of_utc=self.as_of_utc)
        self.assertFalse(ok_exp)
        self.assertEqual(status_exp, "MISSING_PROVIDER_OBSERVATION_FIELDS")

        # 30b. Price discrepancy
        pyth_discrepancy = {
            "price_usd": 1.0,
            "chain": "solana",
            "quote_asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "pyth",
            "provider": "pyth",
            "feed_id": "0xeaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "observation": {
                "price": 95000000,  # 0.95, not matching claimed 1.0
                "expo": -8,
                "publish_time": self.as_of_utc,
            },
        }
        ok_disc, status_disc, _, _ = validate_quote_usd_conversion(pyth_discrepancy, as_of_utc=self.as_of_utc)
        self.assertFalse(ok_disc)
        self.assertEqual(status_disc, "OBSERVATION_PRICE_DISCREPANCY")


if __name__ == "__main__":
    unittest.main()
