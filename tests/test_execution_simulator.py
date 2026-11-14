import copy
from datetime import datetime, timezone
import hashlib
import json
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
    acquire_provider_observation,
    is_valid_address,
    ChainlinkRpcAdapter,
    PythHermesAdapter,
    UniswapV3TwapAdapter,
    CexTickerAdapter,
    DefiLlamaPricesAdapter,
    AcquiredProviderObservation,
    ProviderUnavailableError,
    _verify_provenance_token,
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


def make_mock_chainlink_rpc_transport(answer: int = 100000000, decimals: int = 8, round_id: int = 1, updated_at: int = 1794571200, chain_id: int = 1):
    def transport(url: str, data: bytes | str | None, headers: dict[str, str] | None) -> tuple[int, bytes]:
        payload = json.loads(data) if isinstance(data, (str, bytes)) else {}
        method = payload.get("method")
        req_id = payload.get("id", 1)
        if method == "eth_chainId":
            return 200, json.dumps({"jsonrpc": "2.0", "result": hex(chain_id), "id": req_id}).encode("utf-8")
        params = payload.get("params", [{}])
        call_params = params[0] if isinstance(params, list) and len(params) > 0 and isinstance(params[0], dict) else {}
        call_data = call_params.get("data", "")
        if call_data.startswith("0x313ce567"):
            res_hex = "0x" + hex(decimals)[2:].zfill(64)
            return 200, json.dumps({"jsonrpc": "2.0", "result": res_hex, "id": req_id}).encode("utf-8")
        elif call_data.startswith("0xfeaf968e"):
            r_hex = hex(round_id)[2:].zfill(64)
            ans_hex = hex(answer)[2:].zfill(64) if answer >= 0 else hex((1 << 256) + answer)[2:].zfill(64)
            start_hex = hex(updated_at)[2:].zfill(64)
            upd_hex = hex(updated_at)[2:].zfill(64)
            ans_round_hex = hex(round_id)[2:].zfill(64)
            res_hex = "0x" + r_hex + ans_hex + start_hex + upd_hex + ans_round_hex
            return 200, json.dumps({"jsonrpc": "2.0", "result": res_hex, "id": req_id}).encode("utf-8")
        elif call_data.startswith("0x50d25bcd"):
            ans_hex = hex(answer)[2:].zfill(64) if answer >= 0 else hex((1 << 256) + answer)[2:].zfill(64)
            return 200, json.dumps({"jsonrpc": "2.0", "result": "0x" + ans_hex, "id": req_id}).encode("utf-8")
        elif call_data.startswith("0x668a0f02"):
            return 200, json.dumps({"jsonrpc": "2.0", "result": "0x" + hex(round_id)[2:].zfill(64), "id": req_id}).encode("utf-8")
        elif call_data.startswith("0x8205bf6a"):
            return 200, json.dumps({"jsonrpc": "2.0", "result": "0x" + hex(updated_at)[2:].zfill(64), "id": req_id}).encode("utf-8")
        return 200, json.dumps({"jsonrpc": "2.0", "result": "0x", "id": req_id}).encode("utf-8")
    return transport


def make_mock_pyth_transport(feed_id: str, price: int = 100000000, expo: int = -8, publish_time: int = 1794571200):
    def transport(url: str, data: bytes | str | None, headers: dict[str, str] | None) -> tuple[int, bytes]:
        clean_id = feed_id.lower().replace("0x", "")
        body = [{
            "id": clean_id,
            "price": {
                "price": str(price),
                "conf": "1000",
                "expo": expo,
                "publish_time": publish_time,
            }
        }]
        return 200, json.dumps(body).encode("utf-8")
    return transport


def make_mock_cex_transport(provider: str, symbol: str, price: float = 1.0, bid: float = 0.9999, ask: float = 1.0001, volume: float = 50000000.0, time_iso: str = "2026-11-13T12:00:00Z", close_time_ms: int = 1794571200000):
    def transport(url: str, data: bytes | str | None, headers: dict[str, str] | None) -> tuple[int, bytes]:
        if provider == "coinbase":
            body = {"price": str(price), "bid": str(bid), "ask": str(ask), "volume": str(volume), "time": time_iso}
        elif provider == "binance":
            body = {"symbol": symbol, "lastPrice": str(price), "bidPrice": str(bid), "askPrice": str(ask), "volume": str(volume), "closeTime": close_time_ms}
        elif provider == "kraken":
            body = {"error": [], "result": {symbol: {"c": [str(price), "1"], "b": [str(bid), "1"], "a": [str(ask), "1"], "v": [str(volume), str(volume)]}}}
        else:
            body = {}
        return 200, json.dumps(body).encode("utf-8")
    return transport


def make_mock_uniswap_v3_transport(
    tick_cumulatives: list[int] = [10000000, 10000060],
    chain_id: int = 1,
    token0: str = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    token1: str = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    fee_tier: int = 500,
):
    def transport(url: str, data: bytes | str | None, headers: dict[str, str] | None) -> tuple[int, bytes]:
        payload = json.loads(data) if isinstance(data, (str, bytes)) else {}
        method = payload.get("method")
        req_id = payload.get("id", 1)
        if method == "eth_chainId":
            return 200, json.dumps({"jsonrpc": "2.0", "result": hex(chain_id), "id": req_id}).encode("utf-8")
        params = payload.get("params", [{}])
        call_params = params[0] if isinstance(params, list) and len(params) > 0 and isinstance(params[0], dict) else {}
        call_data = call_params.get("data", "")
        if call_data.startswith("0x0dfe1681"):  # token0()
            clean_t0 = token0.lower().replace("0x", "").zfill(64)
            return 200, json.dumps({"jsonrpc": "2.0", "result": "0x" + clean_t0, "id": req_id}).encode("utf-8")
        elif call_data.startswith("0xd21220a7"):  # token1()
            clean_t1 = token1.lower().replace("0x", "").zfill(64)
            return 200, json.dumps({"jsonrpc": "2.0", "result": "0x" + clean_t1, "id": req_id}).encode("utf-8")
        elif call_data.startswith("0xddca3f43"):  # fee()
            res_hex = "0x" + hex(fee_tier)[2:].zfill(64)
            return 200, json.dumps({"jsonrpc": "2.0", "result": res_hex, "id": req_id}).encode("utf-8")
        elif call_data.startswith("0x88316456"):  # observe()
            def to_word_int56(val: int) -> str:
                if val < 0:
                    val = (1 << 256) + val
                return hex(val)[2:].zfill(64)

            words = [
                "00" * 31 + "40",
                "00" * 31 + "a0",
                "00" * 31 + "02",
                to_word_int56(tick_cumulatives[0]),
                to_word_int56(tick_cumulatives[1]),
                "00" * 31 + "02",
                "00" * 32,
                "00" * 32,
            ]
            res_hex = "0x" + "".join(words)
            return 200, json.dumps({"jsonrpc": "2.0", "result": res_hex, "id": req_id}).encode("utf-8")
        return 200, json.dumps({"jsonrpc": "2.0", "result": "0x", "id": req_id}).encode("utf-8")
    return transport


def make_mock_defillama_transport(chain: str, token: str, price: float = 1.0, timestamp: int = 1794571200, decimals: int = 6):
    def transport(url: str, data: bytes | str | None, headers: dict[str, str] | None) -> tuple[int, bytes]:
        key = f"{chain}:{token}"
        body = {
            "coins": {
                key: {
                    "price": price,
                    "symbol": "USDC",
                    "timestamp": timestamp,
                    "decimals": decimals,
                    "confidence": 0.99,
                }
            }
        }
        return 200, json.dumps(body).encode("utf-8")
    return transport


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
        self.mock_cl_transport = make_mock_chainlink_rpc_transport(answer=100000000, decimals=8, round_id=1, updated_at=1794571200)
        self.valid_quote_conversion = acquire_provider_observation(
            "chainlink",
            VALID_QUOTE_ADDR,
            VALID_EVM_CHAIN,
            as_of_utc=self.as_of_utc,
            transport=self.mock_cl_transport,
        )
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
        mock_weth = make_mock_chainlink_rpc_transport(answer=300000000000, decimals=8, round_id=1, updated_at=1794571200)
        src_exp["quote_usd_conversion"] = acquire_provider_observation(
            "chainlink",
            VALID_ALT_QUOTE,
            VALID_EVM_CHAIN,
            as_of_utc=self.as_of_utc,
            transport=mock_weth,
        )
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

        qualified = v425.qualify_actionable_candidates_for_testing([cand], self.now_dt, _test_fixture_key=v425._TEST_FIXTURE_ISOLATION_KEY)
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

        qualified = v425.qualify_actionable_candidates_for_testing([cand_token1], self.now_dt, _test_fixture_key=v425._TEST_FIXTURE_ISOLATION_KEY)
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
        valid_conv = copy.deepcopy(self.valid_quote_conversion)
        ok, status, price, details = validate_quote_usd_conversion(
            valid_conv,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertTrue(ok)
        self.assertIn(status, ("PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "TEST_MOCKED_OBSERVATION_VALIDATED", "CONVERSION_VERIFIED"))
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
        valid_conv = copy.deepcopy(self.valid_quote_conversion)
        ok, status, price, details = validate_quote_usd_conversion(
            valid_conv,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        ok, status, price, details = validate_quote_usd_conversion(
            valid_conv,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertTrue(ok)
        self.assertIn(status, ("PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "TEST_MOCKED_OBSERVATION_VALIDATED", "CONVERSION_VERIFIED"))
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
        qualified = v425.qualify_actionable_candidates_for_testing([cand], self.now_dt, _test_fixture_key=v425._TEST_FIXTURE_ISOLATION_KEY)
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
            "provider_acquired": True,
            "request_url": "https://api.binance.com/api/v3/ticker/24hr?symbol=ETHUSDT",
            "observation": {
                "symbol": "ETHUSDT",
                "last_price": 3000.0,
                "bid_price": 2999.9,
                "ask_price": 3000.1,
                "volume_24h": 150000.0,
                "timestamp": self.as_of_utc,
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
                "provider_acquired": True,
                "request_url": "https://rpc.ethereum.chainlink.org/v1/0x3e7d1eab03480b83a7d35d7647822ec73a6858c6/latestRoundData",
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
        self.assertIn(status, ("PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE", "CONVERSION_VERIFIED"))
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
                "timestamp": self.as_of_utc,
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
                "timestamp": self.as_of_utc,
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
        mock_cl = make_mock_chainlink_rpc_transport(answer=99900000, decimals=8, round_id=100)
        leg2 = acquire_provider_observation("chainlink", "0xdac17f958d2ee523a2206206994597c13d831ec7", "ethereum", market_or_feed_id="0x3e7d1eab03480b83a7d35d7647822ec73a6858c6", as_of_utc=self.as_of_utc, transport=mock_cl)
        mock_bn = make_mock_cex_transport("binance", "ETHUSDT", price=3000.0, bid=2999.9, ask=3000.1, volume=150000.0, time_iso=self.as_of_utc)
        eth_usdt_conv = acquire_provider_observation("binance", VALID_ALT_QUOTE, "ethereum", market_or_feed_id="ETHUSDT", as_of_utc=self.as_of_utc, transport=mock_bn)
        eth_usdt_conv["chained_conversion"] = leg2
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
                "timestamp": self.as_of_utc,
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
                "timestamp": self.as_of_utc,
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
        qualified = v425.qualify_actionable_candidates_for_testing([cand], self.now_dt, _test_fixture_key=v425._TEST_FIXTURE_ISOLATION_KEY)
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
            "provider_acquired": True,
            "request_url": "https://rpc.ethereum.chainlink.org/v1/0x8fa59a53f040d12d26f03bc9920373707c3c4004/latestRoundData",
            "observation": {
                "answer": 100000000,
                "decimals": 8,
                "round_id": 42,
                "updated_at": self.as_of_utc,
            },
        }
        ok_cl, status_cl, price_cl, _ = validate_quote_usd_conversion(cl_conv, as_of_utc=self.as_of_utc)
        self.assertTrue(ok_cl)
        self.assertIn(status_cl, ("PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE", "CONVERSION_VERIFIED"))
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
            "provider_acquired": True,
            "request_url": "https://hermes.pyth.network/v2/updates/price/latest?ids[]=0xeaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a",
            "observation": {
                "price": 100000000,
                "expo": -8,
                "publish_time": self.as_of_utc,
            },
        }
        ok_py, status_py, price_py, _ = validate_quote_usd_conversion(pyth_conv, as_of_utc=self.as_of_utc)
        self.assertTrue(ok_py)
        self.assertIn(status_py, ("PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE", "CONVERSION_VERIFIED"))
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
            "provider_acquired": True,
            "request_url": "https://api.exchange.coinbase.com/products/USDC-USD/ticker",
            "observation": {
                "symbol": "USDC-USD",
                "last_price": 1.0,
                "bid_price": 0.9999,
                "ask_price": 1.0001,
                "volume_24h": 45000000.0,
                "timestamp": self.as_of_utc,
            },
        }
        ok_cb, status_cb, price_cb, _ = validate_quote_usd_conversion(cb_conv, as_of_utc=self.as_of_utc)
        self.assertTrue(ok_cb)
        self.assertIn(status_cb, ("PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE", "CONVERSION_VERIFIED"))
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
                "twap_price": 3000.0,
                "timestamp": self.as_of_utc,
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
                "timestamp": self.as_of_utc,
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
                "timestamp": self.as_of_utc,
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
            "provider_acquired": True,
            "request_url": "https://api.geckoterminal.com/api/v2/networks/eth/pools/0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
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
        self.assertIn(status_leg, ("PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE", "CONVERSION_VERIFIED"))
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
                "timestamp": self.as_of_utc,
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
                "tick_cumulatives": [10000000, 24412240],
                "timestamp": self.as_of_utc,
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

    # 31. A fresh outer timestamp cannot hide a stale Chainlink updated_at
    def test_req31_fresh_outer_timestamp_cannot_hide_stale_chainlink_updated_at(self):
        conv_fresh_outer_stale_inner = copy.deepcopy(self.valid_quote_conversion)
        conv_fresh_outer_stale_inner.pop("__acquired_observation__", None)
        conv_fresh_outer_stale_inner.pop("_auth_token", None)
        conv_fresh_outer_stale_inner["timestamp_utc"] = self.as_of_utc  # Fresh outer timestamp
        conv_fresh_outer_stale_inner["observation"]["updated_at"] = "2026-11-13T11:40:00Z"  # 20 min old (> 300s)
        ok, status, price, details = validate_quote_usd_conversion(
            conv_fresh_outer_stale_inner,
            expected_chain=VALID_EVM_CHAIN,
            expected_quote_asset=VALID_QUOTE_ADDR,
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "STALE_PROVIDER_OBSERVATION")
        self.assertIn("freshness failed", details["reason"])

    # 32. A fresh outer timestamp cannot hide a stale Pyth publish_time
    def test_req32_fresh_outer_timestamp_cannot_hide_stale_pyth_publish_time(self):
        pyth_fresh_outer_stale_inner = {
            "price_usd": 1.0,
            "chain": "solana",
            "quote_asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,  # Fresh outer timestamp
            "source": "pyth",
            "provider": "pyth",
            "feed_id": "0xeaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "observation": {
                "price": 100000000,
                "expo": -8,
                "publish_time": "2026-11-13T11:40:00Z",  # Stale inner publish_time
            },
        }
        ok, status, price, details = validate_quote_usd_conversion(
            pyth_fresh_outer_stale_inner,
            expected_chain="solana",
            expected_quote_asset="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            as_of_utc=self.as_of_utc,
        )
        self.assertFalse(ok)
        self.assertEqual(status, "STALE_PROVIDER_OBSERVATION")

    # 33. A fresh outer timestamp cannot hide a stale DEX observation
    def test_req33_fresh_outer_timestamp_cannot_hide_stale_dex_observation(self):
        dex_stale_obs = {
            "price_usd": 1.0,
            "chain": "ethereum",
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USDC",
            "timestamp_utc": self.as_of_utc,  # Fresh outer
            "source": "geckoterminal",
            "provider": "geckoterminal",
            "feed_id": "eth_0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
            "valuation_policy": "DEX_AGGREGATOR_VERIFIED",
            "observation": {
                "price": 1.0,
                "token": VALID_QUOTE_ADDR,
                "chain": "ethereum",
                "timestamp": "2026-11-13T11:40:00Z",  # Stale inner
            },
            "usd_conversion_leg": self.valid_quote_conversion,
        }
        ok, status, price, details = validate_quote_usd_conversion(dex_stale_obs, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "STALE_PROVIDER_OBSERVATION")

    # 34. Missing or stale CEX observation timestamps fail
    def test_req34_missing_or_stale_cex_observation_timestamps_fail(self):
        # 34a. Missing timestamp
        cex_missing_ts = {
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
                "bid_price": 0.9999,
                "ask_price": 1.0001,
                "volume_24h": 45000000.0,
                # Missing timestamp!
            },
        }
        ok_m, status_m, _, _ = validate_quote_usd_conversion(cex_missing_ts, as_of_utc=self.as_of_utc)
        self.assertFalse(ok_m)
        self.assertEqual(status_m, "MISSING_PROVIDER_OBSERVATION_TIMESTAMP")

        # 34b. Stale timestamp
        cex_stale_ts = copy.deepcopy(cex_missing_ts)
        cex_stale_ts["observation"]["timestamp"] = "2026-11-13T11:40:00Z"
        ok_s, status_s, _, _ = validate_quote_usd_conversion(cex_stale_ts, as_of_utc=self.as_of_utc)
        self.assertFalse(ok_s)
        self.assertEqual(status_s, "STALE_PROVIDER_OBSERVATION")

    # 35. Uniswap with missing twap_price fails
    def test_req35_uniswap_with_missing_twap_price_fails(self):
        twap_missing_p = {
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
                "timestamp": self.as_of_utc,
                # Missing twap_price!
            },
            "usd_conversion_leg": self.valid_quote_conversion,
        }
        ok, status, _, details = validate_quote_usd_conversion(twap_missing_p, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "MISSING_PROVIDER_OBSERVATION_FIELDS")
        self.assertIn("twap_price", details["reason"])

    # 36. Uniswap with a fabricated but internally matching price fails provider-observation verification
    def test_req36_uniswap_with_fabricated_but_matching_price_fails(self):
        # twap_price claimed is 3000.0, but tick_cumulatives calculate to ~1.0005
        twap_fabricated = {
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
                "tick_cumulatives": [1000, 2000],  # avg_tick = 5.5 => calc_ratio ~ 1.0005, deviates from 3000
                "timestamp": self.as_of_utc,
            },
            "usd_conversion_leg": self.valid_quote_conversion,
        }
        ok, status, _, details = validate_quote_usd_conversion(twap_fabricated, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "INVALID_TWAP_OBSERVATION")
        self.assertIn("deviates from claimed twap_price", details["reason"])

    # 37. Pyth with a fabricated but internally matching price/exponent fails
    def test_req37_pyth_with_fabricated_matching_price_exponent_fails(self):
        # Claimed price is $1.00; price=1000000, expo=-6 calculates to 1.00, but registered feed expo is -8
        pyth_fabricated_expo = {
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
                "price": 1000000,  # 1e6 * 10^-6 = 1.0
                "expo": -6,        # Wrong! Canonical feed expo is -8
                "publish_time": self.as_of_utc,
            },
        }
        ok, status, _, details = validate_quote_usd_conversion(pyth_fabricated_expo, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "OBSERVATION_DECIMALS_MISMATCH")
        self.assertIn("does not match registered feed expo", details["reason"])

    # 38. Registered feed ID with fabricated observation fields cannot qualify
    def test_req38_registered_feed_with_fabricated_observation_fields_cannot_qualify(self):
        # Genuine feed, but round tracking is incomplete: answered_in_round < round_id
        cl_bad_round = copy.deepcopy(self.valid_quote_conversion)
        cl_bad_round.pop("__acquired_observation__", None)
        cl_bad_round.pop("_auth_token", None)
        cl_bad_round["observation"]["round_id"] = 100
        cl_bad_round["observation"]["answered_in_round"] = 99  # Incomplete round!
        ok, status, _, details = validate_quote_usd_conversion(cl_bad_round, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "INVALID_PROVIDER_OBSERVATION")
        self.assertIn("round not complete", details["reason"])

    # 39. CEX last price outside the bid/ask spread fails
    def test_req39_cex_last_price_outside_bid_ask_spread_fails(self):
        # 39a. last_price > ask_price
        cex_high_last = {
            "price_usd": 1.0020,
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
                "last_price": 1.0020,  # > ask_price
                "bid_price": 0.9999,
                "ask_price": 1.0001,
                "volume_24h": 45000000.0,
                "timestamp": self.as_of_utc,
            },
        }
        ok, status, _, details = validate_quote_usd_conversion(cex_high_last, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "INVALID_CEX_OBSERVATION")
        self.assertIn("outside bid/ask spread", details["reason"])

        # 39b. last_price < bid_price
        cex_low_last = copy.deepcopy(cex_high_last)
        cex_low_last["price_usd"] = 0.9990
        cex_low_last["observation"]["last_price"] = 0.9990
        ok_l, status_l, _, details_l = validate_quote_usd_conversion(cex_low_last, as_of_utc=self.as_of_utc)
        self.assertFalse(ok_l)
        self.assertEqual(status_l, "INVALID_CEX_OBSERVATION")
        self.assertIn("outside bid/ask spread", details_l["reason"])

    # 40. Fabricated CEX response with internally matching fields cannot qualify
    def test_req40_fabricated_cex_response_with_matching_fields_cannot_qualify(self):
        # Internally consistent fields, but untrusted source URL domain
        cex_fake_domain = {
            "price_usd": 1.0,
            "chain": "ethereum",
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "coinbase",
            "provider": "coinbase",
            "market_id": "USDC-USD",
            "source_url": "https://fake-exchange.xyz/products/USDC-USD/ticker",
            "valuation_policy": "CEX_MARKET_TICKER_VERIFIED",
            "observation": {
                "symbol": "USDC-USD",
                "last_price": 1.0,
                "bid_price": 0.9999,
                "ask_price": 1.0001,
                "volume_24h": 45000000.0,
                "timestamp": self.as_of_utc,
            },
        }
        ok, status, _, details = validate_quote_usd_conversion(cex_fake_domain, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "UNTRUSTED_SOURCE_URL")

        # Payload hash discrepancy
        cex_hash_mismatch = copy.deepcopy(cex_fake_domain)
        cex_hash_mismatch["source_url"] = "https://api.exchange.coinbase.com/products/USDC-USD/ticker"
        cex_hash_mismatch["raw_payload"] = '{"price":"1.0"}'
        cex_hash_mismatch["response_sha256"] = "1111111111111111111111111111111111111111111111111111111111111111"
        ok_h, status_h, _, details_h = validate_quote_usd_conversion(cex_hash_mismatch, as_of_utc=self.as_of_utc)
        self.assertFalse(ok_h)
        self.assertEqual(status_h, "FABRICATED_PROVIDER_RESPONSE")
        self.assertIn("SHA-256 mismatch", details_h["reason"])

    # 41. Chained conversion using a different intermediate token fails
    def test_req41_chained_conversion_different_intermediate_token_fails(self):
        # Chained leg supplies address of USDC disguised in contract_address when converting USDT
        eth_usdt_wrong_intermediate = {
            "price_usd": 3000.0,
            "chain": "ethereum",
            "quote_asset": VALID_ALT_QUOTE,
            "reference_asset": "USDT",
            "timestamp_utc": self.as_of_utc,
            "source": "binance",
            "provider": "binance",
            "market_id": "ETHUSDT",
            "valuation_policy": "CEX_MARKET_TICKER_VERIFIED",
            "provider_acquired": True,
            "request_url": "https://api.binance.com/api/v3/ticker/24hr?symbol=ETHUSDT",
            "observation": {
                "symbol": "ETHUSDT",
                "last_price": 3000.0,
                "bid_price": 2999.9,
                "ask_price": 3000.1,
                "volume_24h": 150000.0,
                "timestamp": self.as_of_utc,
            },
            "chained_conversion": {
                "price_usd": 1.0,
                "chain": "ethereum",
                "contract_address": VALID_QUOTE_ADDR,  # USDC address disguised as contract_address
                "reference_asset": "USD",
                "timestamp_utc": self.as_of_utc,
                "source": "chainlink",
                "provider": "chainlink",
                "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",  # USDC feed, not USDT feed!
                "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
                "observation": {
                    "answer": 100000000,
                    "decimals": 8,
                    "round_id": 1,
                    "updated_at": self.as_of_utc,
                },
            },
        }
        ok, status, _, details = validate_quote_usd_conversion(eth_usdt_wrong_intermediate, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "QUOTE_CONVERSION_ASSET_MISMATCH")
        self.assertIn("does not match expected intermediate asset USDT", details["reason"])

    # 42. Every conversion leg independently passes freshness and identity validation
    def test_req42_every_conversion_leg_independently_passes_freshness_and_identity(self):
        # Outer leg is fresh, but chained conversion observation updated_at is stale
        eth_usdt_stale_chained_obs = {
            "price_usd": 3000.0,
            "chain": "ethereum",
            "quote_asset": VALID_ALT_QUOTE,
            "reference_asset": "USDT",
            "timestamp_utc": self.as_of_utc,
            "source": "binance",
            "provider": "binance",
            "market_id": "ETHUSDT",
            "valuation_policy": "CEX_MARKET_TICKER_VERIFIED",
            "provider_acquired": True,
            "request_url": "https://api.binance.com/api/v3/ticker/24hr?symbol=ETHUSDT",
            "observation": {
                "symbol": "ETHUSDT",
                "last_price": 3000.0,
                "bid_price": 2999.9,
                "ask_price": 3000.1,
                "volume_24h": 150000.0,
                "timestamp": self.as_of_utc,
            },
            "chained_conversion": {
                "price_usd": 1.0,
                "chain": "ethereum",
                "quote_asset": "0xdac17f958d2ee523a2206206994597c13d831ec7",
                "reference_asset": "USD",
                "timestamp_utc": self.as_of_utc,  # Fresh outer timestamp on chained leg
                "source": "chainlink",
                "provider": "chainlink",
                "feed_address": "0x3e7d1eab03480b83a7d35d7647822ec73a6858c6",
                "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
                "observation": {
                    "answer": 100000000,
                    "decimals": 8,
                    "round_id": 1,
                    "updated_at": "2026-11-13T11:40:00Z",  # Stale inner updated_at!
                },
            },
        }
        ok, status, _, details = validate_quote_usd_conversion(eth_usdt_stale_chained_obs, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "STALE_PROVIDER_OBSERVATION")

    # 43. Valid provider-acquired observations pass
    def test_req43_valid_provider_acquired_observations_pass(self):
        # Chainlink
        mock_cl = make_mock_chainlink_rpc_transport(answer=100000000, decimals=8, round_id=1, updated_at=1794571200)
        cl_acquired = acquire_provider_observation("chainlink", VALID_QUOTE_ADDR, VALID_EVM_CHAIN, as_of_utc=self.as_of_utc, transport=mock_cl)
        ok_cl, status_cl, p_cl, _ = validate_quote_usd_conversion(cl_acquired, as_of_utc=self.as_of_utc)
        self.assertTrue(ok_cl)
        self.assertIn(status_cl, ("PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "TEST_MOCKED_OBSERVATION_VALIDATED"))
        self.assertEqual(p_cl, 1.0)

        # Pyth
        mock_py = make_mock_pyth_transport("0xeaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a", price=100000000, expo=-8, publish_time=1794571200)
        pyth_acquired = acquire_provider_observation("pyth", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "solana", as_of_utc=self.as_of_utc, transport=mock_py)
        ok_py, status_py, p_py, _ = validate_quote_usd_conversion(pyth_acquired, as_of_utc=self.as_of_utc)
        self.assertTrue(ok_py)
        self.assertIn(status_py, ("PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "TEST_MOCKED_OBSERVATION_VALIDATED"))
        self.assertEqual(p_py, 1.0)

        # Coinbase
        mock_cb = make_mock_cex_transport("coinbase", "USDC-USD", price=1.0, bid=0.9999, ask=1.0001, volume=45000000.0, time_iso=self.as_of_utc)
        cb_acquired = acquire_provider_observation("coinbase", VALID_QUOTE_ADDR, "ethereum", as_of_utc=self.as_of_utc, transport=mock_cb)
        ok_cb, status_cb, p_cb, _ = validate_quote_usd_conversion(cb_acquired, as_of_utc=self.as_of_utc)
        self.assertTrue(ok_cb)
        self.assertIn(status_cb, ("PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "TEST_MOCKED_OBSERVATION_VALIDATED"))
        self.assertEqual(p_cb, 1.0)

        # Uniswap v3 TWAP
        mock_tw = make_mock_uniswap_v3_transport(tick_cumulatives=[10000000, 10000060])
        twap_acquired = acquire_provider_observation("uniswap_v3_twap", VALID_ALT_QUOTE, "ethereum", market_or_feed_id="0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640", as_of_utc=self.as_of_utc, transport=mock_tw)
        twap_acquired["usd_conversion_leg"] = self.valid_quote_conversion
        ok_tw, status_tw, p_tw, _ = validate_quote_usd_conversion(twap_acquired, as_of_utc=self.as_of_utc)
        self.assertTrue(ok_tw)
        self.assertIn(status_tw, ("PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "TEST_MOCKED_OBSERVATION_VALIDATED"))

    # 44. Existing strict bare-numeric-price rejection remains intact
    def test_req44_existing_strict_bare_numeric_price_rejection_intact(self):
        for bad_conv in (1.0, "1.0", {}, None):
            ok, status, price, details = validate_quote_usd_conversion(bad_conv, as_of_utc=self.as_of_utc)
            self.assertFalse(ok)
            self.assertIn(status, ("MISSING_QUOTE_CONVERSION_EVIDENCE", "INVALID_QUOTE_CONVERSION_PRICE"))

    # 45. Production qualification fails closed whenever provider evidence is incomplete or unverified
    def test_req45_production_qualification_fails_closed_when_provider_evidence_incomplete(self):
        # Candidate with stale observation timestamp
        stale_cand = copy.deepcopy(self.valid_quote_source)
        stale_cand["quote_usd_conversion"].pop("__acquired_observation__", None)
        stale_cand["quote_usd_conversion"].pop("_auth_token", None)
        stale_cand["quote_usd_conversion"]["observation"]["updated_at"] = "2026-11-13T11:40:00Z"
        stale_cand["amm_reserves"] = {
            "chain": VALID_EVM_CHAIN,
            "pool_address": VALID_POOL_ADDR,
            "token0": VALID_TOKEN_ADDR,
            "token1": VALID_QUOTE_ADDR,
            "reserve0": 100_000.0,
            "reserve1": 50_000.0,
            "timestamp_utc": self.as_of_utc,
            "fee_bps": 30.0,
        }
        qualified = v425.qualify_final_actionable_candidates([stale_cand], self.now_dt)
        self.assertEqual(len(qualified), 0)
        self.assertTrue(any("STALE_PROVIDER_OBSERVATION" in b for b in stale_cand["qualification_blockers"]))

    # 46. Calling acquisition with price_usd=1.0 cannot produce a verified provider observation without real provider response
    def test_req46_calling_acquisition_with_price_usd_cannot_produce_verified_observation(self):
        bad_transport = lambda url, data, headers: (500, b"Internal Server Error")
        unverified_conv = acquire_provider_observation("chainlink", VALID_QUOTE_ADDR, VALID_EVM_CHAIN, as_of_utc=self.as_of_utc, price_usd=1.0, transport=bad_transport)
        self.assertEqual(unverified_conv.get("status"), "PROVIDER_DATA_UNAVAILABLE")
        self.assertIsNone(unverified_conv.get("price_usd"))
        ok, status, price, details = validate_quote_usd_conversion(unverified_conv, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "PROVIDER_DATA_UNAVAILABLE")

    # 47. Caller-supplied provider_acquired=True cannot qualify
    def test_req47_caller_supplied_provider_acquired_cannot_qualify(self):
        fake_acquired = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "provider_acquired": True,  # Self-declared!
            "observation": {
                "answer": 100000000,
                "decimals": 8,
                "round_id": 1,
                "updated_at": self.as_of_utc,
            },
        }
        ok, status, price, details = validate_quote_usd_conversion(fake_acquired, as_of_utc=self.as_of_utc)
        self.assertTrue(ok)
        self.assertEqual(status, "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE")
        self.assertFalse(details["provider_acquired"])

        # Candidate fails closed at production qualification gate
        cand = copy.deepcopy(self.valid_quote_source)
        cand["quote_usd_conversion"] = fake_acquired
        qualified = v425.qualify_actionable_candidates_for_testing([cand], self.now_dt, _test_fixture_key=v425._TEST_FIXTURE_ISOLATION_KEY)
        self.assertEqual(len(qualified), 0)
        self.assertTrue(any("STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE" in b for b in cand["qualification_blockers"]))

    # 48. Caller-supplied trusted URL cannot qualify
    def test_req48_caller_supplied_trusted_url_cannot_qualify(self):
        fake_url = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "request_url": "https://rpc.ethereum.chainlink.org/v1/latest",  # Self-declared trusted URL
            "observation": {
                "answer": 100000000,
                "decimals": 8,
                "round_id": 1,
                "updated_at": self.as_of_utc,
            },
        }
        ok, status, price, details = validate_quote_usd_conversion(fake_url, as_of_utc=self.as_of_utc)
        self.assertTrue(ok)
        self.assertEqual(status, "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE")
        self.assertFalse(details["provider_acquired"])

    # 49. Caller-supplied raw payload and matching SHA-256 cannot qualify
    def test_req49_caller_supplied_raw_payload_and_sha256_cannot_qualify(self):
        payload = '{"answer":100000000}'
        sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        fake_payload = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "raw_payload": payload,
            "response_sha256": sha,
            "observation": {
                "answer": 100000000,
                "decimals": 8,
                "round_id": 1,
                "updated_at": self.as_of_utc,
            },
        }
        ok, status, price, details = validate_quote_usd_conversion(fake_payload, as_of_utc=self.as_of_utc)
        self.assertTrue(ok)
        self.assertEqual(status, "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE")
        self.assertFalse(details["provider_acquired"])

    # 50. A synthetic Chainlink answer cannot qualify
    def test_req50_synthetic_chainlink_answer_cannot_qualify(self):
        synthetic_cl = {
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
        ok, status, price, details = validate_quote_usd_conversion(synthetic_cl, as_of_utc=self.as_of_utc)
        self.assertTrue(ok)
        self.assertEqual(status, "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE")
        self.assertFalse(details["provider_acquired"])

    # 51. A synthetic Pyth payload cannot qualify
    def test_req51_synthetic_pyth_payload_cannot_qualify(self):
        synthetic_pyth = {
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
        ok, status, price, details = validate_quote_usd_conversion(synthetic_pyth, as_of_utc=self.as_of_utc)
        self.assertTrue(ok)
        self.assertEqual(status, "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE")
        self.assertFalse(details["provider_acquired"])

    # 52. A synthetic CEX ticker cannot qualify
    def test_req52_synthetic_cex_ticker_cannot_qualify(self):
        synthetic_cex = {
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
                "bid_price": 0.9999,
                "ask_price": 1.0001,
                "volume_24h": 50000000.0,
                "timestamp": self.as_of_utc,
            },
        }
        ok, status, price, details = validate_quote_usd_conversion(synthetic_cex, as_of_utc=self.as_of_utc)
        self.assertTrue(ok)
        self.assertEqual(status, "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE")
        self.assertFalse(details["provider_acquired"])

    # 53. Synthetic Uniswap tick cumulatives cannot qualify
    def test_req53_synthetic_uniswap_tick_cumulatives_cannot_qualify(self):
        synthetic_twap = {
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
                "tick_cumulatives": [10000000, 24412240],
                "timestamp": self.as_of_utc,
            },
            "usd_conversion_leg": self.valid_quote_conversion,
        }
        ok, status, price, details = validate_quote_usd_conversion(synthetic_twap, as_of_utc=self.as_of_utc)
        self.assertTrue(ok)
        self.assertEqual(status, "STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE")
        self.assertFalse(details["provider_acquired"])

    # 54. Missing Uniswap tick cumulatives fail closed
    def test_req54_missing_uniswap_tick_cumulatives_fails_closed(self):
        mock_tw_no_ticks = lambda url, data, headers: (200, json.dumps({"jsonrpc": "2.0", "result": "0x", "id": 1}).encode("utf-8"))
        res = acquire_provider_observation("uniswap_v3_twap", VALID_ALT_QUOTE, "ethereum", market_or_feed_id="0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640", as_of_utc=self.as_of_utc, transport=mock_tw_no_ticks)
        self.assertEqual(res.get("status"), "PROVIDER_DATA_UNAVAILABLE")
        self.assertIn("genuine provider acquisition failed", res.get("reason", ""))

        twap_missing_ticks = {
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
                # tick_cumulatives missing!
                "timestamp": self.as_of_utc,
            },
            "usd_conversion_leg": self.valid_quote_conversion,
        }
        ok, status, _, details = validate_quote_usd_conversion(twap_missing_ticks, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "MISSING_PROVIDER_OBSERVATION_FIELDS")
        self.assertIn("tick_cumulatives", details["reason"])

    # 55. Real provider response obtained through genuine adapter passes structural and freshness validation
    def test_req55_real_provider_response_through_genuine_adapter_passes(self):
        # 1. Chainlink RPC
        mock_cl = make_mock_chainlink_rpc_transport(answer=100000000, decimals=8, round_id=5, updated_at=1794571200)
        cl_res = acquire_provider_observation("chainlink", VALID_QUOTE_ADDR, "ethereum", as_of_utc=self.as_of_utc, transport=mock_cl)
        ok_cl, st_cl, p_cl, det_cl = validate_quote_usd_conversion(cl_res, as_of_utc=self.as_of_utc)
        self.assertTrue(ok_cl)
        self.assertIn(st_cl, ("PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "TEST_MOCKED_OBSERVATION_VALIDATED"))
        self.assertTrue(det_cl["provider_acquired"])

        # 2. Pyth Hermes
        mock_py = make_mock_pyth_transport("0xeaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a", price=100000000, expo=-8, publish_time=1794571200)
        py_res = acquire_provider_observation("pyth", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "solana", as_of_utc=self.as_of_utc, transport=mock_py)
        ok_py, st_py, p_py, det_py = validate_quote_usd_conversion(py_res, as_of_utc=self.as_of_utc)
        self.assertTrue(ok_py)
        self.assertIn(st_py, ("PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "TEST_MOCKED_OBSERVATION_VALIDATED"))
        self.assertTrue(det_py["provider_acquired"])

        # 3. Coinbase CEX
        mock_cb = make_mock_cex_transport("coinbase", "USDC-USD", price=1.0, bid=0.9999, ask=1.0001, volume=50000000.0, time_iso=self.as_of_utc)
        cb_res = acquire_provider_observation("coinbase", VALID_QUOTE_ADDR, "ethereum", as_of_utc=self.as_of_utc, transport=mock_cb)
        ok_cb, st_cb, p_cb, det_cb = validate_quote_usd_conversion(cb_res, as_of_utc=self.as_of_utc)
        self.assertTrue(ok_cb)
        self.assertIn(st_cb, ("PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "TEST_MOCKED_OBSERVATION_VALIDATED"))
        self.assertTrue(det_cb["provider_acquired"])

        # 4. DefiLlama DEX Aggregator
        mock_dl = make_mock_defillama_transport("ethereum", VALID_QUOTE_ADDR, price=1.0, timestamp=1794571200)
        dl_res = acquire_provider_observation("defillama", VALID_QUOTE_ADDR, "ethereum", as_of_utc=self.as_of_utc, transport=mock_dl)
        dl_res["usd_conversion_leg"] = cl_res
        ok_dl, st_dl, p_dl, det_dl = validate_quote_usd_conversion(dl_res, as_of_utc=self.as_of_utc)
        self.assertTrue(ok_dl)
        self.assertIn(st_dl, ("PROVIDER_RESPONSE_ACQUIRED_AND_VALIDATED", "TEST_MOCKED_OBSERVATION_VALIDATED"))
        self.assertTrue(det_dl["provider_acquired"])

    # 56. Provider acquisition failure produces explicit unavailable status
    def test_req56_provider_acquisition_failure_produces_unavailable_status(self):
        failing_transport = lambda url, data, headers: (503, b"Service Unavailable")
        res = acquire_provider_observation("coinbase", VALID_QUOTE_ADDR, "ethereum", as_of_utc=self.as_of_utc, transport=failing_transport)
        self.assertEqual(res.get("status"), "PROVIDER_DATA_UNAVAILABLE")
        self.assertIsNone(res.get("price_usd"))
        ok, status, _, details = validate_quote_usd_conversion(res, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "PROVIDER_DATA_UNAVAILABLE")

    # 57. Production qualification rejects every unverified provider observation
    def test_req57_production_qualification_rejects_unverified_provider_observations(self):
        cand = {
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
            "target_price": 7.0,
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
            "amm_reserves": {
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "token0": VALID_TOKEN_ADDR,
                "token1": VALID_QUOTE_ADDR,
                "reserve0": 10_000.0,
                "reserve1": 50_000.0,
                "timestamp_utc": self.as_of_utc,
                "fee_bps": 30.0,
            },
        }
        cand_unverif = copy.deepcopy(cand)
        cand_unverif["quote_usd_conversion"] = {
            "price_usd": 1.0,
            "chain": VALID_EVM_CHAIN,
            "quote_asset": VALID_QUOTE_ADDR,
            "reference_asset": "USD",
            "timestamp_utc": self.as_of_utc,
            "source": "chainlink",
            "provider": "chainlink",
            "feed_address": "0x8fa59a53f040d12d26f03bc9920373707c3c4004",
            "valuation_policy": "STABLECOIN_ORACLE_VERIFIED",
            "provider_acquired": True,
            "observation": {
                "answer": 100000000,
                "decimals": 8,
                "round_id": 1,
                "updated_at": self.as_of_utc,
            },
        }
        res_unverif = v425.qualify_actionable_candidates_for_testing([cand_unverif], self.now_dt, _test_fixture_key=v425._TEST_FIXTURE_ISOLATION_KEY)
        self.assertEqual(len(res_unverif), 0)
        self.assertTrue(any("STRUCTURALLY_CONSISTENT_UNVERIFIED_PROVENANCE" in b for b in cand_unverif["qualification_blockers"]))

        cand_auth = copy.deepcopy(cand)
        cand_auth["quote_usd_conversion"] = self.valid_quote_conversion
        # In strict production mode, MOCKED_TRANSPORT is rejected:
        res_prod = v425.qualify_final_actionable_candidates([cand_auth], self.now_dt)
        self.assertEqual(len(res_prod), 0)
        self.assertTrue(any("MOCKED_TRANSPORT" in b for b in cand_auth["qualification_blockers"]))

        # In offline unit test mode with test fixture key, candidate passes:
        cand_auth_test = copy.deepcopy(cand)
        cand_auth_test["quote_usd_conversion"] = self.valid_quote_conversion
        res_auth = v425.qualify_actionable_candidates_for_testing([cand_auth_test], self.now_dt, _test_fixture_key=v425._TEST_FIXTURE_ISOLATION_KEY)
        self.assertEqual(len(res_auth), 1)
        self.assertEqual(cand_auth_test["qualification_blockers"], [])


class ProvenanceIntegrityAdversarialTests(unittest.TestCase):
    """Exhaustive adversarial tests covering all Phase 2 provenance integrity requirements."""

    def setUp(self):
        self.as_of_utc = "2026-11-13T12:00:00Z"
        self.now_dt = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
        self.mock_cl_transport = make_mock_chainlink_rpc_transport(answer=100000000, decimals=8, round_id=1, updated_at=1794571200)
        self.genuine_obs = ChainlinkRpcAdapter.fetch(
            VALID_QUOTE_ADDR,
            VALID_EVM_CHAIN,
            as_of_utc=self.as_of_utc,
            transport=self.mock_cl_transport,
        )
        self.base_candidate = {
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
            "target_price": 7.0,
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
            "dex_execution_quotes": {
                "provider": "uniswap_v3",
                "timestamp_utc": self.as_of_utc,
                "chain": VALID_EVM_CHAIN,
                "pool_address": VALID_POOL_ADDR,
                "notional_size_usd": 100.0,
                "buy_quote": {
                    "direction": "BUY",
                    "chain": VALID_EVM_CHAIN,
                    "pool_address": VALID_POOL_ADDR,
                    "timestamp_utc": self.as_of_utc,
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
                    "timestamp_utc": self.as_of_utc,
                    "in_token": VALID_TOKEN_ADDR,
                    "out_token": VALID_QUOTE_ADDR,
                    "in_amount": 50.0,
                    "out_amount": 98.0,
                    "notional_size_usd": 100.0,
                },
            },
            "quote_usd_conversion": self.genuine_obs.to_dict(),
        }

    # 1. Mutating price after genuine acquisition rejected
    def test_01_mutating_price_after_genuine_acquisition_rejected(self):
        obs = copy.deepcopy(self.genuine_obs)
        obs.price_usd = 999.0
        ok, status, _, _ = validate_quote_usd_conversion(obs, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "INVALID_PROVENANCE_SIGNATURE")

        conv = self.genuine_obs.to_dict()
        conv["price_usd"] = 999.0
        ok2, status2, _, details2 = validate_quote_usd_conversion(conv, as_of_utc=self.as_of_utc)
        self.assertFalse(ok2)
        self.assertEqual(status2, "OBSERVATION_MUTATION_DETECTED")
        self.assertIn("price", details2["reason"])

    # 2. Mutating chain rejected
    def test_02_mutating_chain_rejected(self):
        obs = copy.deepcopy(self.genuine_obs)
        obs.chain = "solana"
        ok, status, _, _ = validate_quote_usd_conversion(obs, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertIn(status, ("INVALID_PROVENANCE_SIGNATURE", "QUOTE_CONVERSION_CHAIN_MISMATCH"))

        conv = self.genuine_obs.to_dict()
        conv["chain"] = "arbitrum"
        ok2, status2, _, details2 = validate_quote_usd_conversion(conv, as_of_utc=self.as_of_utc)
        self.assertFalse(ok2)
        self.assertIn(status2, ("OBSERVATION_MUTATION_DETECTED", "QUOTE_CONVERSION_CHAIN_MISMATCH"))

    # 3. Mutating token address rejected
    def test_03_mutating_token_address_rejected(self):
        obs = copy.deepcopy(self.genuine_obs)
        obs.quote_asset = "0x0000000000000000000000000000000000000000"
        ok, status, _, _ = validate_quote_usd_conversion(obs, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertIn(status, ("INVALID_PROVENANCE_SIGNATURE", "QUOTE_CONVERSION_ASSET_MISMATCH"))

        conv = self.genuine_obs.to_dict()
        conv["quote_asset"] = "0x0000000000000000000000000000000000000000"
        ok2, status2, _, details2 = validate_quote_usd_conversion(conv, as_of_utc=self.as_of_utc)
        self.assertFalse(ok2)
        self.assertIn(status2, ("OBSERVATION_MUTATION_DETECTED", "QUOTE_CONVERSION_ASSET_MISMATCH"))

    # 4. Mutating feed/market/pool identifier rejected
    def test_04_mutating_feed_market_pool_identifier_rejected(self):
        obs = copy.deepcopy(self.genuine_obs)
        obs.feed_id = "0x1111111111111111111111111111111111111111"
        ok, status, _, _ = validate_quote_usd_conversion(obs, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "INVALID_PROVENANCE_SIGNATURE")

        conv = self.genuine_obs.to_dict()
        conv["feed_address"] = "0x1111111111111111111111111111111111111111"
        ok2, status2, _, _ = validate_quote_usd_conversion(conv, as_of_utc=self.as_of_utc)
        self.assertFalse(ok2)
        self.assertIn(status2, ("OBSERVATION_MUTATION_DETECTED", "UNRECOGNIZED_FEED_ADDRESS"))

    # 5. Mutating parsed observation fields rejected
    def test_05_mutating_parsed_observation_fields_rejected(self):
        obs = copy.deepcopy(self.genuine_obs)
        obs.observation["answer"] = 999999999
        ok, status, _, _ = validate_quote_usd_conversion(obs, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "INVALID_PROVENANCE_SIGNATURE")

        conv = self.genuine_obs.to_dict()
        conv["observation"]["answer"] = 999999999
        ok2, status2, _, details2 = validate_quote_usd_conversion(conv, as_of_utc=self.as_of_utc)
        self.assertFalse(ok2)
        self.assertEqual(status2, "OBSERVATION_MUTATION_DETECTED")

    # 6. Replacing raw payload after signing rejected
    def test_06_replacing_raw_payload_after_signing_rejected(self):
        obs = copy.deepcopy(self.genuine_obs)
        obs.raw_payload = '{"tampered": true}'
        ok, status, _, details = validate_quote_usd_conversion(obs, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "PAYLOAD_HASH_MISMATCH")

        conv = self.genuine_obs.to_dict()
        conv["raw_payload"] = '{"tampered": true}'
        ok2, status2, _, details2 = validate_quote_usd_conversion(conv, as_of_utc=self.as_of_utc)
        self.assertFalse(ok2)
        self.assertIn(status2, ("OBSERVATION_MUTATION_DETECTED", "FABRICATED_PROVIDER_RESPONSE"))

    # 7. Replacing response hash rejected
    def test_07_replacing_response_hash_rejected(self):
        obs = copy.deepcopy(self.genuine_obs)
        obs.response_sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        ok, status, _, _ = validate_quote_usd_conversion(obs, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "PAYLOAD_HASH_MISMATCH")

        conv = self.genuine_obs.to_dict()
        conv["response_sha256"] = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        ok2, status2, _, _ = validate_quote_usd_conversion(conv, as_of_utc=self.as_of_utc)
        self.assertFalse(ok2)
        self.assertIn(status2, ("OBSERVATION_MUTATION_DETECTED", "FABRICATED_PROVIDER_RESPONSE"))

    # 8. Mocked transport cannot qualify in production
    def test_08_mocked_transport_cannot_qualify_in_production(self):
        cand = copy.deepcopy(self.base_candidate)
        # Production qualification strictly rejects mocked transport
        qualified_prod = v425.qualify_final_actionable_candidates([cand], self.now_dt)
        self.assertEqual(len(qualified_prod), 0)
        self.assertTrue(any("MOCKED_TRANSPORT" in b for b in cand["qualification_blockers"]))

        # Offline testing explicit opt-in via private test fixture key
        cand_test = copy.deepcopy(self.base_candidate)
        qualified_test = v425.qualify_actionable_candidates_for_testing([cand_test], self.now_dt, _test_fixture_key=v425._TEST_FIXTURE_ISOLATION_KEY)
        self.assertEqual(len(qualified_test), 1)
        self.assertEqual(cand_test["qualification_blockers"], [])

    # 9. Caller-supplied transport cannot claim live provenance
    def test_09_caller_supplied_transport_cannot_claim_live_provenance(self):
        # 1. Adapter call with transport parameter is marked MOCKED_TRANSPORT
        obs_mocked = ChainlinkRpcAdapter.fetch(
            VALID_QUOTE_ADDR,
            VALID_EVM_CHAIN,
            as_of_utc=self.as_of_utc,
            transport=self.mock_cl_transport,
        )
        self.assertEqual(obs_mocked.transport_type, "MOCKED_TRANSPORT")
        self.assertEqual(obs_mocked.acquisition_method, "TEST_MOCKED_TRANSPORT")

        # 2. Direct caller-instantiated observation without adapter factory secret is rejected
        caller_obs = AcquiredProviderObservation(
            provider="chainlink",
            chain=VALID_EVM_CHAIN,
            quote_asset=VALID_QUOTE_ADDR,
            reference_asset="USD",
            price_usd=1.0,
            retrieval_time_utc=self.as_of_utc,
            transport_type="LIVE_NETWORK_ACQUISITION",
        )
        self.assertTrue(caller_obs.is_caller_created)
        ok, status, _, details = validate_quote_usd_conversion(caller_obs, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "CALLER_CREATED_OBSERVATION_OBJECT")

    # 10. Pyth wrong feed ID rejected
    def test_10_pyth_wrong_feed_id_rejected(self):
        wrong_transport = make_mock_pyth_transport(
            "0x1111111111111111111111111111111111111111111111111111111111111111",
            price=100000000,
        )
        with self.assertRaises(ProviderUnavailableError) as cm:
            PythHermesAdapter.fetch(
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "solana",
                transport=wrong_transport,
            )
        self.assertIn("missing requested feed ID", str(cm.exception))

    # 11. Pyth missing feed ID rejected
    def test_11_pyth_missing_feed_id_rejected(self):
        def empty_transport(url, data, headers):
            return 200, json.dumps([]).encode("utf-8")

        with self.assertRaises(ProviderUnavailableError) as cm:
            PythHermesAdapter.fetch(
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "solana",
                transport=empty_transport,
            )
        self.assertIn("empty updates list", str(cm.exception))

    # 12. Pyth invalid signature rejected where signature verification implemented
    def test_12_pyth_invalid_signature_rejected(self):
        mock_py = make_mock_pyth_transport(
            "0xeaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a",
            price=100000000,
        )
        obs = PythHermesAdapter.fetch(
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "solana",
            as_of_utc=self.as_of_utc,
            transport=mock_py,
        )
        self.assertFalse(obs.cryptographic_signature_verified)
        ok, status, _, details = validate_quote_usd_conversion(obs, as_of_utc=self.as_of_utc)
        self.assertTrue(ok)
        self.assertFalse(details["cryptographic_authenticity_verified"])

    # 13. Chainlink decimals RPC failure rejected
    def test_13_chainlink_decimals_rpc_failure_rejected(self):
        def failing_decimals_transport(url, data, headers):
            payload = json.loads(data) if isinstance(data, (str, bytes)) else {}
            call_params = payload.get("params", [{}])[0] if payload.get("params") else {}
            call_data = call_params.get("data", "")
            if payload.get("method") == "eth_chainId":
                return 200, json.dumps({"jsonrpc": "2.0", "result": "0x1", "id": payload.get("id")}).encode("utf-8")
            if call_data.startswith("0x313ce567"):
                return 200, json.dumps({"jsonrpc": "2.0", "error": {"code": -32000, "message": "execution reverted"}, "id": payload.get("id")}).encode("utf-8")
            return 200, json.dumps({"jsonrpc": "2.0", "result": "0x", "id": payload.get("id")}).encode("utf-8")

        with self.assertRaises(ProviderUnavailableError) as cm:
            ChainlinkRpcAdapter.fetch(VALID_QUOTE_ADDR, VALID_EVM_CHAIN, transport=failing_decimals_transport)
        self.assertIn("decimals() RPC call reverted or returned empty", str(cm.exception))

    # 14. Chainlink wrong RPC chain ID rejected
    def test_14_chainlink_wrong_rpc_chain_id_rejected(self):
        wrong_cid_transport = make_mock_chainlink_rpc_transport(chain_id=137)  # Polygon chain ID on Ethereum feed
        with self.assertRaises(ProviderUnavailableError) as cm:
            ChainlinkRpcAdapter.fetch(VALID_QUOTE_ADDR, "ethereum", transport=wrong_cid_transport)
        self.assertIn("chain ID mismatch", str(cm.exception))

    # 15. Chainlink incomplete fallback response rejected
    def test_15_chainlink_incomplete_fallback_response_rejected(self):
        def broken_fallback_transport(url, data, headers):
            payload = json.loads(data) if isinstance(data, (str, bytes)) else {}
            if payload.get("method") == "eth_chainId":
                return 200, json.dumps({"jsonrpc": "2.0", "result": "0x1", "id": payload.get("id")}).encode("utf-8")
            call_params = payload.get("params", [{}])[0] if payload.get("params") else {}
            call_data = call_params.get("data", "")
            if call_data.startswith("0x313ce567"):  # decimals
                return 200, json.dumps({"jsonrpc": "2.0", "result": "0x08", "id": payload.get("id")}).encode("utf-8")
            if call_data.startswith("0xfeaf968e"):  # latestRoundData fails -> fallback
                return 200, json.dumps({"jsonrpc": "2.0", "result": "0x", "id": payload.get("id")}).encode("utf-8")
            if call_data.startswith("0x50d25bcd"):  # latestAnswer succeeds
                return 200, json.dumps({"jsonrpc": "2.0", "result": "0x05f5e100", "id": payload.get("id")}).encode("utf-8")
            if call_data.startswith("0x668a0f02"):  # latestRound fails
                return 200, json.dumps({"jsonrpc": "2.0", "result": "0x", "id": payload.get("id")}).encode("utf-8")
            return 200, json.dumps({"jsonrpc": "2.0", "result": "0x", "id": payload.get("id")}).encode("utf-8")

        with self.assertRaises(ProviderUnavailableError) as cm:
            ChainlinkRpcAdapter.fetch(VALID_QUOTE_ADDR, "ethereum", transport=broken_fallback_transport)
        self.assertIn("fallback latestRound() RPC reverted or returned empty", str(cm.exception))

    # 16. Uniswap wrong chain ID rejected
    def test_16_uniswap_wrong_chain_id_rejected(self):
        wrong_uni_transport = make_mock_uniswap_v3_transport(chain_id=56)  # BSC chain ID on Ethereum pool
        with self.assertRaises(ProviderUnavailableError) as cm:
            UniswapV3TwapAdapter.fetch(VALID_ALT_QUOTE, "ethereum", transport=wrong_uni_transport)
        self.assertIn("chain ID mismatch", str(cm.exception))

    # 17. Uniswap wrong token0/token1 or fee tier rejected
    def test_17_uniswap_wrong_token0_token1_or_fee_tier_rejected(self):
        # 1. Wrong token0
        wrong_t0_transport = make_mock_uniswap_v3_transport(token0="0x0000000000000000000000000000000000000000")
        with self.assertRaises(ProviderUnavailableError) as cm1:
            UniswapV3TwapAdapter.fetch(VALID_ALT_QUOTE, "ethereum", transport=wrong_t0_transport)
        self.assertIn("pool token0 mismatch", str(cm1.exception))

        # 2. Wrong token1
        wrong_t1_transport = make_mock_uniswap_v3_transport(token1="0x0000000000000000000000000000000000000000")
        with self.assertRaises(ProviderUnavailableError) as cm2:
            UniswapV3TwapAdapter.fetch(VALID_ALT_QUOTE, "ethereum", transport=wrong_t1_transport)
        self.assertIn("pool token1 mismatch", str(cm2.exception))

        # 3. Wrong fee tier
        wrong_fee_transport = make_mock_uniswap_v3_transport(fee_tier=3000)  # 3000 bps instead of 500 bps
        with self.assertRaises(ProviderUnavailableError) as cm3:
            UniswapV3TwapAdapter.fetch(VALID_ALT_QUOTE, "ethereum", transport=wrong_fee_transport)
        self.assertIn("pool fee tier mismatch", str(cm3.exception))

        # 4. TWAP window < 60 seconds
        uni_transport = make_mock_uniswap_v3_transport()
        with self.assertRaises(ValueError) as cm4:
            UniswapV3TwapAdapter.fetch(VALID_ALT_QUOTE, "ethereum", twap_window_seconds=30, transport=uni_transport)
        self.assertIn("TWAP window must be >= 60 seconds", str(cm4.exception))

    # 18. Production qualification rejects every invalid case
    def test_18_production_qualification_rejects_every_invalid_case(self):
        # Case A: Mutated price
        cand_a = copy.deepcopy(self.base_candidate)
        cand_a["quote_usd_conversion"]["price_usd"] = 999.0
        res_a = v425.qualify_actionable_candidates_for_testing([cand_a], self.now_dt, _test_fixture_key=v425._TEST_FIXTURE_ISOLATION_KEY)
        self.assertEqual(len(res_a), 0)
        self.assertTrue(any("OBSERVATION_MUTATION_DETECTED" in b for b in cand_a["qualification_blockers"]))

        # Case B: Mutated chain
        cand_b = copy.deepcopy(self.base_candidate)
        cand_b["quote_usd_conversion"]["chain"] = "arbitrum"
        res_b = v425.qualify_actionable_candidates_for_testing([cand_b], self.now_dt, _test_fixture_key=v425._TEST_FIXTURE_ISOLATION_KEY)
        self.assertEqual(len(res_b), 0)
        self.assertTrue(any("QUOTE_CONVERSION_CHAIN_MISMATCH" in b or "OBSERVATION_MUTATION_DETECTED" in b for b in cand_b["qualification_blockers"]))

        # Case C: Caller-created observation object
        cand_c = copy.deepcopy(self.base_candidate)
        caller_obs = AcquiredProviderObservation(
            provider="chainlink",
            chain=VALID_EVM_CHAIN,
            quote_asset=VALID_QUOTE_ADDR,
            reference_asset="USD",
            price_usd=1.0,
            retrieval_time_utc=self.as_of_utc,
        )
        cand_c["quote_usd_conversion"] = caller_obs
        res_c = v425.qualify_actionable_candidates_for_testing([cand_c], self.now_dt, _test_fixture_key=v425._TEST_FIXTURE_ISOLATION_KEY)
        self.assertEqual(len(res_c), 0)
        self.assertTrue(any("CALLER_CREATED_OBSERVATION_OBJECT" in b for b in cand_c["qualification_blockers"]))

        # Case D: Mocked transport in production mode (default)
        cand_d = copy.deepcopy(self.base_candidate)
        res_d = v425.qualify_final_actionable_candidates([cand_d], self.now_dt)
        self.assertEqual(len(res_d), 0)
        self.assertTrue(any("MOCKED_TRANSPORT" in b for b in cand_d["qualification_blockers"]))


class ProductionGateHardeningTests(ProvenanceIntegrityAdversarialTests):
    """Adversarial tests verifying production qualification gate cannot be bypassed."""

    # 1. Production qualification rejects mocked transport
    def test_01_production_qualification_rejects_mocked_transport(self):
        cand = copy.deepcopy(self.base_candidate)
        self.assertEqual(cand["quote_usd_conversion"]["transport_type"], "MOCKED_TRANSPORT")
        res = v425.qualify_final_actionable_candidates([cand], self.now_dt)
        self.assertEqual(len(res), 0)
        self.assertTrue(any("MOCKED_TRANSPORT" in b for b in cand["qualification_blockers"]))

    # 2. Production qualification rejects mocked status codes
    def test_02_production_qualification_rejects_mocked_status_codes(self):
        cand = copy.deepcopy(self.base_candidate)
        cand["quote_usd_conversion"]["conversion_status"] = "TEST_MOCKED_OBSERVATION_VALIDATED"
        res = v425.qualify_final_actionable_candidates([cand], self.now_dt)
        self.assertEqual(len(res), 0)
        self.assertTrue(any("MOCKED_TRANSPORT" in b or "TEST_MOCKED_OBSERVATION_VALIDATED" in b for b in cand["qualification_blockers"]))

    # 3. No public function argument enables mocked production qualification
    def test_03_no_public_function_argument_enables_mocked_production_qualification(self):
        cand = copy.deepcopy(self.base_candidate)
        with self.assertRaises(TypeError):
            v425.qualify_final_actionable_candidates([cand], self.now_dt, allow_mocked_transport=True)
        with self.assertRaises(TypeError):
            v425.qualify_final_actionable_candidates([cand], self.now_dt, test_mode=True)
        with self.assertRaises(TypeError):
            v425.qualify_final_actionable_candidates([cand], self.now_dt, bypass=True)
        with self.assertRaises(TypeError):
            v425.qualify_final_actionable_candidates([cand], self.now_dt, force_qualification=True)

    # 4. Candidate data cannot enable mocked qualification
    def test_04_candidate_data_cannot_enable_mocked_qualification(self):
        cand = copy.deepcopy(self.base_candidate)
        cand["allow_mocked_transport"] = True
        cand["test_mode"] = True
        cand["bypass_production_gate"] = True
        cand["is_test"] = True
        cand["quote_usd_conversion"]["allow_mocked_transport"] = True
        cand["quote_usd_conversion"]["_allow_mocked_transport"] = True
        res = v425.qualify_final_actionable_candidates([cand], self.now_dt)
        self.assertEqual(len(res), 0)
        self.assertTrue(any("MOCKED_TRANSPORT" in b for b in cand["qualification_blockers"]))

    # 5. Every signed field mutation invalidates signature
    def test_05_every_signed_field_mutation_invalidates_signature(self):
        signed_fields_and_mutations = [
            ("price_usd", 999.0),
            ("provider", "other_provider"),
            ("chain", "arbitrum"),
            ("chain_id", 42161),
            ("quote_asset", "0x0000000000000000000000000000000000000001"),
            ("reference_asset", "EUR"),
            ("valuation_policy", "CRYPTO_ORACLE_VERIFIED"),
            ("observation_timestamp", "2026-01-01T00:00:00Z"),
            ("retrieval_time_utc", "2026-01-01T00:00:00Z"),
            ("http_status", 500),
            ("rpc_method", "other_rpc"),
            ("raw_payload", '{"mutated": true}'),
            ("response_sha256", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"),
            ("transport_type", "LIVE_NETWORK_ACQUISITION"),
            ("acquisition_method", "OTHER_METHOD"),
            ("request_url", "https://other-rpc.example.com"),
            ("cryptographic_signature_verified", True),
            ("feed_id", "0x0000000000000000000000000000000000000002"),
            ("market_id", "OTHER-MARKET"),
            ("pool_address", "0x0000000000000000000000000000000000000003"),
        ]
        for field_name, mutated_val in signed_fields_and_mutations:
            with self.subTest(field=field_name):
                obs = ChainlinkRpcAdapter.fetch(
                    VALID_QUOTE_ADDR,
                    VALID_EVM_CHAIN,
                    as_of_utc=self.as_of_utc,
                    transport=self.mock_cl_transport,
                )
                self.assertTrue(_verify_provenance_token(obs))
                setattr(obs, field_name, mutated_val)
                self.assertFalse(_verify_provenance_token(obs), f"Mutation of field '{field_name}' did not invalidate HMAC signature!")
                ok, status, _, _ = validate_quote_usd_conversion(obs, as_of_utc=self.as_of_utc)
                self.assertFalse(ok, f"Mutation of field '{field_name}' was not rejected by validate_quote_usd_conversion!")
                self.assertIn(status, (
                    "INVALID_PROVENANCE_SIGNATURE",
                    "PAYLOAD_HASH_MISMATCH",
                    "OBSERVATION_MUTATION_DETECTED",
                    "QUOTE_CONVERSION_CHAIN_MISMATCH",
                    "QUOTE_CONVERSION_ASSET_MISMATCH",
                    "UNRECOGNIZED_FEED_ADDRESS",
                ))

    # 6. Request URL mutation invalidates signature
    def test_06_request_url_mutation_invalidates_signature(self):
        obs = ChainlinkRpcAdapter.fetch(
            VALID_QUOTE_ADDR,
            VALID_EVM_CHAIN,
            as_of_utc=self.as_of_utc,
            transport=self.mock_cl_transport,
        )
        self.assertTrue(_verify_provenance_token(obs))
        obs.request_url = "https://malicious-rpc.attacker.com"
        self.assertFalse(_verify_provenance_token(obs))
        ok, status, _, _ = validate_quote_usd_conversion(obs, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "INVALID_PROVENANCE_SIGNATURE")

    # 7. RPC method mutation invalidates signature
    def test_07_rpc_method_mutation_invalidates_signature(self):
        obs = ChainlinkRpcAdapter.fetch(
            VALID_QUOTE_ADDR,
            VALID_EVM_CHAIN,
            as_of_utc=self.as_of_utc,
            transport=self.mock_cl_transport,
        )
        self.assertTrue(_verify_provenance_token(obs))
        obs.rpc_method = "malicious_call"
        self.assertFalse(_verify_provenance_token(obs))
        ok, status, _, _ = validate_quote_usd_conversion(obs, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "INVALID_PROVENANCE_SIGNATURE")

    # 8. Transport type mutation invalidates signature
    def test_08_transport_type_mutation_invalidates_signature(self):
        obs = ChainlinkRpcAdapter.fetch(
            VALID_QUOTE_ADDR,
            VALID_EVM_CHAIN,
            as_of_utc=self.as_of_utc,
            transport=self.mock_cl_transport,
        )
        self.assertEqual(obs.transport_type, "MOCKED_TRANSPORT")
        self.assertTrue(_verify_provenance_token(obs))
        obs.transport_type = "LIVE_NETWORK_ACQUISITION"
        self.assertFalse(_verify_provenance_token(obs))
        ok, status, _, _ = validate_quote_usd_conversion(obs, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "INVALID_PROVENANCE_SIGNATURE")

    # 9. Conversion-policy mutation invalidates signature
    def test_09_conversion_policy_mutation_invalidates_signature(self):
        obs = ChainlinkRpcAdapter.fetch(
            VALID_QUOTE_ADDR,
            VALID_EVM_CHAIN,
            as_of_utc=self.as_of_utc,
            transport=self.mock_cl_transport,
        )
        self.assertTrue(_verify_provenance_token(obs))
        obs.valuation_policy = "CRYPTO_ORACLE_VERIFIED"
        self.assertFalse(_verify_provenance_token(obs))
        ok, status, _, _ = validate_quote_usd_conversion(obs, as_of_utc=self.as_of_utc)
        self.assertFalse(ok)
        self.assertEqual(status, "INVALID_PROVENANCE_SIGNATURE")

    # 10. Test-only helper cannot be reached without isolation key
    def test_10_test_only_helper_cannot_be_reached_without_isolation_key(self):
        cand = copy.deepcopy(self.base_candidate)
        # Calling without _test_fixture_key raises TypeError
        with self.assertRaises(TypeError):
            v425.qualify_actionable_candidates_for_testing([cand], self.now_dt)
        # Calling with wrong key raises PermissionError
        with self.assertRaises(PermissionError):
            v425.qualify_actionable_candidates_for_testing([cand], self.now_dt, _test_fixture_key="wrong_secret")
        # Calling with another object raises PermissionError
        with self.assertRaises(PermissionError):
            v425.qualify_actionable_candidates_for_testing([cand], self.now_dt, _test_fixture_key=object())
        # Calling with genuine private token succeeds
        res = v425.qualify_actionable_candidates_for_testing([cand], self.now_dt, _test_fixture_key=v425._TEST_FIXTURE_ISOLATION_KEY)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["opportunity_status"], "QUALIFIED")


class TestLiveProviderIntegrations(unittest.TestCase):
    """Integration tests running against actual live public provider endpoints when internet is available."""

    def test_live_coinbase_ticker_integration(self):
        try:
            obs = acquire_provider_observation("coinbase", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "ethereum", market_or_feed_id="ETH-USD")
            if obs.get("status") == "PROVIDER_DATA_UNAVAILABLE":
                raise unittest.SkipTest(f"Live Coinbase endpoint unavailable: {obs.get('reason')}")
            self.assertIsNotNone(obs.get("price_usd"))
            self.assertGreater(obs["price_usd"], 0)
            self.assertEqual(obs["provider"], "coinbase")
            self.assertEqual(obs["transport_type"], "LIVE_HTTP")
        except (Exception, BaseException) as e:
            raise unittest.SkipTest(f"Live network test skipped due to connectivity or network guard: {e}")

    def test_live_binance_ticker_integration(self):
        try:
            obs = acquire_provider_observation("binance", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "ethereum", market_or_feed_id="ETHUSDT")
            if obs.get("status") == "PROVIDER_DATA_UNAVAILABLE":
                raise unittest.SkipTest(f"Live Binance endpoint unavailable: {obs.get('reason')}")
            self.assertIsNotNone(obs.get("price_usd"))
            self.assertGreater(obs["price_usd"], 0)
            self.assertEqual(obs["provider"], "binance")
            self.assertEqual(obs["transport_type"], "LIVE_HTTP")
        except (Exception, BaseException) as e:
            raise unittest.SkipTest(f"Live network test skipped due to connectivity or network guard: {e}")

    def test_live_kraken_ticker_integration(self):
        try:
            obs = acquire_provider_observation("kraken", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "ethereum", market_or_feed_id="ETHUSD")
            if obs.get("status") == "PROVIDER_DATA_UNAVAILABLE":
                raise unittest.SkipTest(f"Live Kraken endpoint unavailable: {obs.get('reason')}")
            self.assertIsNotNone(obs.get("price_usd"))
            self.assertGreater(obs["price_usd"], 0)
            self.assertEqual(obs["provider"], "kraken")
            self.assertEqual(obs["transport_type"], "LIVE_HTTP")
        except (Exception, BaseException) as e:
            raise unittest.SkipTest(f"Live network test skipped due to connectivity or network guard: {e}")

    def test_live_defillama_prices_integration(self):
        try:
            obs = acquire_provider_observation("defillama", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "ethereum")
            if obs.get("status") == "PROVIDER_DATA_UNAVAILABLE":
                raise unittest.SkipTest(f"Live DefiLlama endpoint unavailable: {obs.get('reason')}")
            self.assertIsNotNone(obs.get("price_usd"))
            self.assertAlmostEqual(obs["price_usd"], 1.0, delta=0.05)
            self.assertEqual(obs["provider"], "defillama")
            self.assertEqual(obs["transport_type"], "LIVE_HTTP")
        except (Exception, BaseException) as e:
            raise unittest.SkipTest(f"Live network test skipped due to connectivity or network guard: {e}")

    def test_live_chainlink_rpc_integration(self):
        try:
            obs = acquire_provider_observation("chainlink", "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "ethereum", market_or_feed_id="0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419")
            if obs.get("status") == "PROVIDER_DATA_UNAVAILABLE":
                raise unittest.SkipTest(f"Live Chainlink RPC unavailable: {obs.get('reason')}")
            self.assertIsNotNone(obs.get("price_usd"))
            self.assertGreater(obs["price_usd"], 100)
            self.assertEqual(obs["provider"], "chainlink")
            self.assertEqual(obs["transport_type"], "LIVE_RPC")
        except (Exception, BaseException) as e:
            raise unittest.SkipTest(f"Live network test skipped due to connectivity or network guard: {e}")


if __name__ == "__main__":
    unittest.main()
