#!/usr/bin/env python3
"""tests/test_v430_stage32_execution.py
Comprehensive unit and adversarial test suite for Rocket Hunter v4.30 Stage 3.2:
- Two-leg execution simulation (Buy at entry, Sell at touch)
- Canonical per-gas fee calculation (EVM EIP-1559 and Solana ComputeUnitLimit)
- Exact SqrtPriceMath token0 rounding order & boundary fee rounding up
- Bidirectional depth computation (±2% price impact band) on V2 and V3
- 4-tier liquidity classification hierarchy (depth collapse vs price depreciation)
- Explicit terminal window observation vs authenticated verification partitioning
- Standard simulation-only tranches ($100-$5,000)
- Protected data ledger SHA-256 equivalence preservation
"""

import datetime as dt
import hashlib
import math
import os
import unittest
from typing import Dict, List

from scripts.research_evidence import (
    TECHNICAL_TP_PCT,
    MAX_HOLDING_HOURS,
    STAGE_32_VERSION,
    SIMULATION_TRANCHES_USD,
    UNISWAP_V3_FEE_SPACINGS,
    DualTrackTargetStatus,
    LiquidityClassificationStatus,
    UniswapV2Evidence,
    UniswapV3Evidence,
    UniswapV3TickMath,
    UniswapV3SwapMath,
    UniswapV3SwapSimulator,
    UniswapV2SwapSimulator,
    calculate_evm_gas_cost_usd,
    calculate_solana_tx_fee_lamports,
    calculate_solana_tx_fee_usd,
    PoolStateSnapshot,
    TwoLegExecutionSimulator,
    LiquidityObservationRecord,
    LiquidityClassificationEngine,
    DualTrackOutcomeEvaluator,
    ProtectedDataVerifier,
)


class TestStage32TwoLegExecutionAndGasMath(unittest.TestCase):
    """Test suite covering AC-1, AC-2, AC-3, AC-7, AC-10, AC-11, AC-13."""

    def setUp(self):
        self.entry_v2 = UniswapV2Evidence(
            chain_id=1,
            pool_address="0x1111111111111111111111111111111111111111",
            token0_address="0x2222222222222222222222222222222222222222",  # Base token
            token1_address="0x3333333333333333333333333333333333333333",  # Quote (USDC, 6 decimals)
            token0_decimals=18,
            token1_decimals=6,
            raw_reserve0=1_000 * 10**18,   # 1,000 tokens
            raw_reserve1=1_000_000 * 10**6, # 1,000,000 USDC ($1,000 per token)
            fee_bps=30,
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "00" * 32,
        )

        # Touch state: spot price moved up by +50% ($1,500 per token)
        self.touch_v2_high = UniswapV2Evidence(
            chain_id=1,
            pool_address="0x1111111111111111111111111111111111111111",
            token0_address="0x2222222222222222222222222222222222222222",
            token1_address="0x3333333333333333333333333333333333333333",
            token0_decimals=18,
            token1_decimals=6,
            raw_reserve0=int(816.49658 * 10**18),
            raw_reserve1=int(1_224_744.87 * 10**6),
            fee_bps=30,
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            parent_hash="0x" + "aa" * 32,
        )

    def test_two_leg_roundtrip_execution_reaches_target(self):
        """AC-1, AC-2: $500 roundtrip reaching >= +40% net return emits MODELED_TP40_EXECUTABLE_WITH_ASSUMPTIONS."""
        entry_snapshot = PoolStateSnapshot(
            chain_id=1,
            token_address="0x2222222222222222222222222222222222222222",
            pool_address="0x1111111111111111111111111111111111111111",
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            source_timestamp_utc="2026-11-15T00:00:00Z",
            venue_type="UNISWAP_V2",
            evidence_v2=self.entry_v2,
            quote_usd_price=1.0,
            native_usd_price=3000.0,
            base_fee_per_gas_wei=10_000_000_000,     # 10 gwei
            priority_fee_per_gas_wei=1_000_000_000,  # 1 gwei
        )

        touch_snapshot = PoolStateSnapshot(
            chain_id=1,
            token_address="0x2222222222222222222222222222222222222222",
            pool_address="0x1111111111111111111111111111111111111111",
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            source_timestamp_utc="2026-11-15T04:00:00Z",
            venue_type="UNISWAP_V2",
            evidence_v2=self.touch_v2_high,
            quote_usd_price=1.0,
            native_usd_price=3000.0,
            base_fee_per_gas_wei=10_000_000_000,
            priority_fee_per_gas_wei=1_000_000_000,
        )

        result = TwoLegExecutionSimulator.simulate_roundtrip(
            entry_state=entry_snapshot,
            touch_state=touch_snapshot,
            tranche_usd=500.0,
            gas_used_buy=150_000,
            gas_used_sell=150_000,
        )

        self.assertEqual(result["status"], DualTrackTargetStatus.MODELED_TP40_EXECUTABLE_WITH_ASSUMPTIONS.value)
        self.assertGreaterEqual(result["net_return_pct"], TECHNICAL_TP_PCT)
        self.assertAlmostEqual(result["net_return_pct"], 47.48, delta=2.0)
        self.assertGreater(result["tokens_acquired"], 0)
        self.assertGreater(result["gross_proceeds_usd"], 500.0 * 1.40)

    def test_two_leg_roundtrip_fails_on_gas_and_impact(self):
        """AC-3: Nominal market touches +40%, but roundtrip fees and gas drag bring net return < +40%."""
        touch_v2_exact40 = UniswapV2Evidence(
            chain_id=1,
            pool_address="0x1111111111111111111111111111111111111111",
            token0_address="0x2222222222222222222222222222222222222222",
            token1_address="0x3333333333333333333333333333333333333333",
            token0_decimals=18,
            token1_decimals=6,
            raw_reserve0=int((1_000 / 1.183216) * 10**18),
            raw_reserve1=int((1_000_000 * 1.183216) * 10**6),
            fee_bps=30,
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            parent_hash="0x" + "aa" * 32,
        )

        entry_snapshot = PoolStateSnapshot(
            chain_id=1,
            token_address="0x2222222222222222222222222222222222222222",
            pool_address="0x1111111111111111111111111111111111111111",
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            source_timestamp_utc="2026-11-15T00:00:00Z",
            venue_type="UNISWAP_V2",
            evidence_v2=self.entry_v2,
            quote_usd_price=1.0,
            native_usd_price=3000.0,
            base_fee_per_gas_wei=30_000_000_000,     # 30 gwei
            priority_fee_per_gas_wei=2_000_000_000,  # 2 gwei
        )

        touch_snapshot = PoolStateSnapshot(
            chain_id=1,
            token_address="0x2222222222222222222222222222222222222222",
            pool_address="0x1111111111111111111111111111111111111111",
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            source_timestamp_utc="2026-11-15T04:00:00Z",
            venue_type="UNISWAP_V2",
            evidence_v2=touch_v2_exact40,
            quote_usd_price=1.0,
            native_usd_price=3000.0,
            base_fee_per_gas_wei=30_000_000_000,
            priority_fee_per_gas_wei=2_000_000_000,
        )

        result = TwoLegExecutionSimulator.simulate_roundtrip(
            entry_state=entry_snapshot,
            touch_state=touch_snapshot,
            tranche_usd=100.0,  # Small tranche where gas drag ($19.20 total) is severe relative to size
            gas_used_buy=200_000,
            gas_used_sell=200_000,
        )

        self.assertEqual(result["status"], DualTrackTargetStatus.MODELED_TP40_EXECUTION_IMPACT_FAIL.value)
        self.assertLess(result["net_return_pct"], TECHNICAL_TP_PCT)
        self.assertGreater(result["gas_buy_usd"], 0)
        self.assertGreater(result["gas_sell_usd"], 0)

    def test_two_leg_roundtrip_missing_touch_state_fails_closed(self):
        """AC-1: Omitting either state fails closed to MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING."""
        entry_snapshot = PoolStateSnapshot(
            chain_id=1,
            token_address="0x2222222222222222222222222222222222222222",
            pool_address="0x1111111111111111111111111111111111111111",
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            source_timestamp_utc="2026-11-15T00:00:00Z",
            venue_type="UNISWAP_V2",
            evidence_v2=self.entry_v2,
        )

        result = TwoLegExecutionSimulator.simulate_roundtrip(
            entry_state=entry_snapshot,
            touch_state=None,  # Missing touch state
            tranche_usd=500.0,
        )

        self.assertEqual(result["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertIsNone(result["net_return_pct"])
        self.assertEqual(result["diagnostic"], "ENTRY_OR_TOUCH_POOL_STATE_MISSING")

    def test_eip1559_gas_calculation_unambiguous_per_gas(self):
        """AC-7: GasUsed * (BaseFee + Tip) calculation with native to USD conversion."""
        gas_used = 185_000
        base_fee_wei = 20_000_000_000     # 20 gwei
        priority_fee_wei = 2_000_000_000  # 2 gwei
        native_usd = 3000.0

        eth_cost, usd_cost, label = calculate_evm_gas_cost_usd(
            gas_used=gas_used,
            base_fee_wei=base_fee_wei,
            priority_fee_wei=priority_fee_wei,
            native_usd_price=native_usd,
        )

        # Expected: 185,000 * 22 gwei = 4,070,000 gwei = 0.00407 ETH
        self.assertAlmostEqual(eth_cost, 0.00407, places=8)
        # Expected USD: 0.00407 * $3,000 = $12.21
        self.assertAlmostEqual(usd_cost, 12.21, places=4)
        self.assertEqual(label, "MODELED_GAS_COST_WITH_ASSUMPTIONS")

    def test_solana_priority_fee_uses_limit_and_ceiling_rounding(self):
        """AC-7: Asserts Solana priority fee uses ComputeUnitLimit and rounds UP on fractional lamports."""
        base_sigs = 1
        limit = 200_000
        used = 65_432  # Must be ignored
        price_micro_lamports = 1_234
        sol_usd = 150.0

        fee_lamports = calculate_solana_tx_fee_lamports(
            base_signatures=base_sigs,
            compute_unit_limit=limit,
            compute_unit_price_micro_lamports=price_micro_lamports,
        )

        # 200,000 * 1,234 = 246,800,000 micro-lamports -> / 1,000,000 = 246.8 lamports
        # ceil(246.8) = 247 lamports. Base fee = 5,000 lamports. Total = 5,247 lamports.
        self.assertEqual(fee_lamports, 5_247)

        # Confirm floor division (which gives 246 + 5000 = 5246) is rejected
        floor_fee = (base_sigs * 5000) + ((limit * price_micro_lamports) // 1_000_000)
        self.assertNotEqual(fee_lamports, floor_fee)

        # Confirm using compute_units_used (which gives ceil(80.74) = 81 + 5000 = 5081) is rejected
        consumed_fee = (base_sigs * 5000) + ((used * price_micro_lamports + 999_999) // 1_000_000)
        self.assertNotEqual(fee_lamports, consumed_fee)

        fee_sol, fee_usd, label = calculate_solana_tx_fee_usd(
            base_signatures=base_sigs,
            compute_unit_limit=limit,
            compute_unit_price_micro_lamports=price_micro_lamports,
            sol_usd_price=sol_usd,
        )

        # 5,247 / 1e9 = 0.000005247 SOL
        self.assertAlmostEqual(fee_sol, 0.000005247, places=12)
        # 0.000005247 * $150.00 = $0.00078705
        self.assertAlmostEqual(fee_usd, 0.00078705, places=8)
        self.assertEqual(label, "MODELED_GAS_COST_WITH_ASSUMPTIONS")


class TestUniswapV3ExactMathAndSimulation(unittest.TestCase):
    """Test suite covering AC-4, AC-5, AC-6."""

    def test_v3_token0_rounding_exact_operation_order(self):
        """AC-4, AC-5: Asserts SqrtPriceMath getAmount0Delta round-down operation order without reciprocal drift."""
        # Small integer fixture: L=1000, sqrt_a=100, sqrt_b=105
        liquidity = 1000
        sqrt_a = 100
        sqrt_b = 105

        canonical_output = UniswapV3SwapMath.get_amount0_delta_round_down(sqrt_a, sqrt_b, liquidity)

        # Canonical Solidity: intermediate = (L << 96) * (sqrt_b - sqrt_a) // sqrt_b; result = intermediate // sqrt_a
        num1 = liquidity << 96
        num2 = sqrt_b - sqrt_a
        expected_solidity = ((num1 * num2) // sqrt_b) // sqrt_a
        self.assertEqual(canonical_output, expected_solidity)
        self.assertEqual(canonical_output, 37727696435363970282639976350)

        # Reciprocal subtraction introduces 1-wei drift
        term1 = num1 // sqrt_a
        term2 = num1 // sqrt_b
        reciprocal_drift_output = term1 - term2
        self.assertEqual(reciprocal_drift_output, 37727696435363970282639976351)
        self.assertNotEqual(canonical_output, reciprocal_drift_output)
        self.assertEqual(reciprocal_drift_output - canonical_output, 1)

    def test_v3_exact_fee_rounding_up_on_boundary_crossing(self):
        """AC-6: Asserts boundary fee matches SwapMath.sol line 57 mulDivRoundingUp."""
        fee_ppm = 3000  # 0.30%
        step_in = 10_000
        denom = 1_000_000 - fee_ppm

        fee_rounded_up = UniswapV3SwapMath.mul_div_rounding_up(step_in, fee_ppm, denom)
        # Exact product: 10,000 * 3,000 = 30,000,000
        # 30,000,000 / 997,000 = 30.0902708... -> ceil = 31
        self.assertEqual(fee_rounded_up, 31)

        # Verify floor division gives 30 and would fail
        self.assertNotEqual(fee_rounded_up, (step_in * fee_ppm) // denom)

    def test_v3_multi_tick_walking_across_three_ticks(self):
        """AC-4: Walks across 3 initialized ticks and adjusts active liquidity accurately."""
        spacing = 60
        initial_tick = 50
        initial_p = UniswapV3TickMath.get_sqrt_ratio_at_tick(initial_tick)
        initial_l = 10_000_000

        bitmap = {}
        for t in [50, 100, 200, 300]:
            w = (t // spacing) >> 8
            bitmap[w] = 1

        evidence = UniswapV3Evidence(
            chain_id=1,
            pool_address="0x" + "33" * 20,
            token0_address="0x" + "11" * 20,
            token1_address="0x" + "22" * 20,
            token0_decimals=18,
            token1_decimals=6,
            fee_tier_ppm=3000,
            sqrt_price_x96=initial_p,
            tick=initial_tick,
            active_liquidity=initial_l,
            initialized_ticks={
                100: {"liquidityNet": 2_000_000},
                200: {"liquidityNet": -1_000_000},
                300: {"liquidityNet": 5_000_000},
            },
            tick_bitmap=bitmap,
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "00" * 32,
        )

        ok, consumed, yielded, final_p, diag = UniswapV3SwapSimulator.simulate_swap(
            evidence=evidence,
            amount_in=100_000_000,
            zero_for_one=False,
            target_sqrt_price_x96=UniswapV3TickMath.get_sqrt_ratio_at_tick(350),
        )

        self.assertTrue(ok)
        self.assertEqual(diag, "SWAP_SUCCESS")
        self.assertGreater(consumed, 0)
        self.assertGreater(yielded, 0)
        self.assertGreater(final_p, initial_p)

    def test_v3_swap_incomplete_bitmap_fails_closed(self):
        """AC-4: Missing word in bitmap fails closed to MODELED_DEPTH_UNRESOLVED_BITMAP_GAP."""
        evidence = UniswapV3Evidence(
            chain_id=1,
            pool_address="0x" + "33" * 20,
            token0_address="0x" + "11" * 20,
            token1_address="0x" + "22" * 20,
            token0_decimals=18,
            token1_decimals=6,
            fee_tier_ppm=3000,
            sqrt_price_x96=UniswapV3TickMath.get_sqrt_ratio_at_tick(100),
            tick=100,
            active_liquidity=1_000_000,
            initialized_ticks={200: {"liquidityNet": 100_000}},
            tick_bitmap={},  # Empty bitmap simulates missing proof/word
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "00" * 32,
        )

        ok, consumed, yielded, final_p, diag = UniswapV3SwapSimulator.simulate_swap(
            evidence=evidence,
            amount_in=50_000,
            zero_for_one=False,
        )

        self.assertFalse(ok)
        self.assertEqual(diag, "MODELED_DEPTH_UNRESOLVED_BITMAP_GAP")


class TestBidirectionalDepthAndLiquidityHierarchy(unittest.TestCase):
    """Test suite covering AC-8, AC-9, AC-11, AC-12, AC-13."""

    def test_bidirectional_depth_exact_v2_pool_equations(self):
        """AC-8: Asserts V2 bidirectional depth equations where fee applies to input only, not quote proceeds."""
        rq = 1_000_000
        rb = 1_000
        fee_bps = 30

        depth = UniswapV2SwapSimulator.compute_bidirectional_depth(rq, rb, fee_bps)

        # Expected D_ask = rq * (sqrt(1.02) - 1) / (1 - 0.003) = 1,000,000 * 0.0099504938 / 0.997 = 9,980
        self.assertEqual(depth["d_ask_plus_2pct"], 9980)

        # Expected D_bid = rq * (1 - sqrt(0.98)) = 1,000,000 * 0.010050506 = 10,050 (NO fee multiplier on proceeds)
        self.assertEqual(depth["d_bid_minus_2pct"], 10050)

        # Expected token input required = rb * (1/sqrt(0.98) - 1) / (1 - 0.003) = 10
        self.assertEqual(depth["token_in_minus_2pct"], 10)

        # Asymmetry check: Proceeds yielded > Input required due to constant product curve slope
        self.assertGreater(depth["d_bid_minus_2pct"], depth["d_ask_plus_2pct"])

    def test_bidirectional_depth_asymmetric_v3_fixture(self):
        """AC-8: Validates asymmetric V3 pool where bid depth differs heavily from ask depth."""
        # Current tick 204520: sqrt_p = 2186517289083564169714246582247698
        p0 = 2186517289083564169714246582247698
        spacing = 60
        # Heavily asymmetric liquidity: 20x more liquidity below spot than above spot
        w0 = (204520 // spacing) >> 8
        w_plus = (204600 // spacing) >> 8
        w_minus = (204400 // spacing) >> 8
        bitmap = {w0: 1, w_plus: 1, w_minus: 1}

        evidence = UniswapV3Evidence(
            chain_id=1,
            pool_address="0x" + "44" * 20,
            token0_address="0x" + "11" * 20,
            token1_address="0x" + "22" * 20,
            token0_decimals=6,
            token1_decimals=18,
            fee_tier_ppm=3000,
            sqrt_price_x96=p0,
            tick=204520,
            active_liquidity=500_000 * 10**6,  # Thin above spot
            initialized_ticks={
                204500: {"liquidityNet": -9_500_000 * 10**6},  # High bid capacity (upper tick of lower range)
                204540: {"liquidityNet": -400_000 * 10**6},
            },
            tick_bitmap=bitmap,
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "00" * 32,
        )

        depth = UniswapV3SwapSimulator.compute_bidirectional_depth(evidence, token0_quote=False)
        self.assertTrue(depth["is_depth_complete"])
        self.assertGreater(depth["d_bid_minus_2pct"], 0)
        self.assertGreater(depth["d_ask_plus_2pct"], 0)
        # Ask depth is much smaller than bid depth
        self.assertLess(depth["d_ask_plus_2pct"], depth["d_bid_minus_2pct"])

    def test_liquidity_collapse_triggered_by_single_side_drop(self):
        """AC-9: Dropping below 20% on EITHER ask or bid triggers LIQUIDITY_DEPTH_COLLAPSE_OBSERVED_UNVERIFIED."""
        t0 = "2026-11-15T00:00:00Z"
        records = [
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_000_000,
                block_hash="0x" + "aa" * 32,
                source_event_timestamp_utc="2026-11-15T00:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T00:00:05Z",
                d_ask_plus_2pct=100_000,
                d_bid_minus_2pct=100_000,
                liquidity_usd=1_000_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_001_000,
                block_hash="0x" + "bb" * 32,
                source_event_timestamp_utc="2026-11-15T05:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T05:00:05Z",
                d_ask_plus_2pct=90_000,  # Ask depth remains 90%
                d_bid_minus_2pct=15_000, # Bid depth collapses to 15% (< 20%)
                liquidity_usd=800_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_003_000,
                block_hash="0x" + "cc" * 32,
                source_event_timestamp_utc="2026-11-15T23:45:00Z",
                local_ingestion_timestamp_utc="2026-11-15T23:45:05Z",
                d_ask_plus_2pct=90_000,
                d_bid_minus_2pct=15_000,
                liquidity_usd=800_000.0,
            ),
        ]

        outcome = LiquidityClassificationEngine.evaluate_liquidity_trajectory(records, t0)
        self.assertEqual(outcome["status"], LiquidityClassificationStatus.LIQUIDITY_DEPTH_COLLAPSE_OBSERVED_UNVERIFIED.value)
        self.assertEqual(outcome["min_depth_ratio"], 0.15)
        self.assertIsNone(outcome["liquidity_end_usd_authenticated_verified"])
        self.assertEqual(outcome["liquidity_end_usd_observed_unverified"], 800_000.0)

    def test_liquidity_price_depreciation_does_not_trigger_depth_collapse(self):
        """AC-9: Token price drop 80% with depth intact triggers ASSET_PRICE_DEPRECIATION_OBSERVED_UNVERIFIED."""
        t0 = "2026-11-15T00:00:00Z"
        # Spaced <= 5h intervals to satisfy continuous coverage check (Priority 2 gap check)
        records = [
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_000_000,
                block_hash="0x" + "aa" * 32,
                source_event_timestamp_utc="2026-11-15T00:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T00:00:05Z",
                d_ask_plus_2pct=100_000,
                d_bid_minus_2pct=100_000,
                liquidity_usd=1_000_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_001_000,
                block_hash="0x" + "bb" * 32,
                source_event_timestamp_utc="2026-11-15T05:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T05:00:05Z",
                d_ask_plus_2pct=85_000,
                d_bid_minus_2pct=85_000,
                liquidity_usd=500_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_002_000,
                block_hash="0x" + "cc" * 32,
                source_event_timestamp_utc="2026-11-15T10:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T10:00:05Z",
                d_ask_plus_2pct=85_000,  # 85% depth maintained
                d_bid_minus_2pct=85_000, # 85% depth maintained
                liquidity_usd=150_000.0, # 85% USD markdown (ratio = 0.15 < 0.20)
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_003_000,
                block_hash="0x" + "dd" * 32,
                source_event_timestamp_utc="2026-11-15T15:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T15:00:05Z",
                d_ask_plus_2pct=85_000,
                d_bid_minus_2pct=85_000,
                liquidity_usd=150_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_004_000,
                block_hash="0x" + "ee" * 32,
                source_event_timestamp_utc="2026-11-15T20:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T20:00:05Z",
                d_ask_plus_2pct=85_000,
                d_bid_minus_2pct=85_000,
                liquidity_usd=150_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_005_000,
                block_hash="0x" + "ff" * 32,
                source_event_timestamp_utc="2026-11-15T23:45:00Z",
                local_ingestion_timestamp_utc="2026-11-15T23:45:05Z",
                d_ask_plus_2pct=85_000,
                d_bid_minus_2pct=85_000,
                liquidity_usd=150_000.0,
            ),
        ]

        outcome = LiquidityClassificationEngine.evaluate_liquidity_trajectory(records, t0)
        self.assertEqual(outcome["status"], LiquidityClassificationStatus.ASSET_PRICE_DEPRECIATION_OBSERVED_UNVERIFIED.value)
        self.assertEqual(outcome["min_depth_ratio"], 0.85)
        self.assertEqual(outcome["min_usd_ratio"], 0.15)
        self.assertEqual(outcome["liquidity_end_usd_observed_unverified"], 150_000.0)
        self.assertIsNone(outcome["liquidity_end_usd_authenticated_verified"])

    def test_liquidity_lp_withdrawal_requires_event_evidence(self):
        """AC-9: LP_WITHDRAWAL_COLLAPSE_OBSERVED_UNVERIFIED emitted strictly when Burn event log exists."""
        t0 = "2026-11-15T00:00:00Z"
        records_with_burn = [
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_000_000,
                block_hash="0x" + "aa" * 32,
                source_event_timestamp_utc="2026-11-15T00:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T00:00:05Z",
                d_ask_plus_2pct=100_000,
                d_bid_minus_2pct=100_000,
                liquidity_usd=1_000_000.0,
                has_lp_burn_event=False,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_001_000,
                block_hash="0x" + "bb" * 32,
                source_event_timestamp_utc="2026-11-15T05:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T05:00:05Z",
                d_ask_plus_2pct=10_000,
                d_bid_minus_2pct=10_000,
                liquidity_usd=100_000.0,
                has_lp_burn_event=True,  # Explicit on-chain Burn event log
            ),
        ]

        outcome = LiquidityClassificationEngine.evaluate_liquidity_trajectory(records_with_burn, t0)
        self.assertEqual(outcome["status"], LiquidityClassificationStatus.LP_WITHDRAWAL_COLLAPSE_OBSERVED_UNVERIFIED.value)

    def test_terminal_snapshot_wording_unverified_vs_verified(self):
        """AC-11, AC-12: Terminal field is populated as unverified; verified field remains strictly None."""
        t0 = "2026-11-15T00:00:00Z"
        records = [
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_000_000,
                block_hash="0x" + "aa" * 32,
                source_event_timestamp_utc="2026-11-15T00:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T00:00:05Z",
                d_ask_plus_2pct=100_000,
                d_bid_minus_2pct=100_000,
                liquidity_usd=1_000_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_004_000,
                block_hash="0x" + "dd" * 32,
                source_event_timestamp_utc="2026-11-15T23:48:00Z",  # +23.8 hours
                local_ingestion_timestamp_utc="2026-11-15T23:48:05Z",
                d_ask_plus_2pct=95_000,
                d_bid_minus_2pct=95_000,
                liquidity_usd=950_000.0,
            ),
        ]

        outcome = LiquidityClassificationEngine.evaluate_liquidity_trajectory(records, t0)
        self.assertEqual(outcome["liquidity_end_usd_observed_unverified"], 950_000.0)
        self.assertIsNone(outcome["liquidity_end_usd_authenticated_verified"])
        self.assertTrue(outcome["recovery_confirmed_at_window_end"])


class TestMultiSizeAndIdentityConstraints(unittest.TestCase):
    """Test suite covering AC-10, AC-13, AC-14, AC-15."""

    def test_multi_size_simulation_depth_divergence(self):
        """AC-10: Standard simulation scenarios ($100-$5,000) depth divergence labeled strictly as scenarios."""
        thin_entry_v2 = UniswapV2Evidence(
            chain_id=1,
            pool_address="0x1111111111111111111111111111111111111111",
            token0_address="0x2222222222222222222222222222222222222222",
            token1_address="0x3333333333333333333333333333333333333333",
            token0_decimals=18,
            token1_decimals=6,
            raw_reserve0=10 * 10**18,
            raw_reserve1=10_000 * 10**6,
            fee_bps=30,
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "00" * 32,
        )
        thin_touch_v2 = UniswapV2Evidence(
            chain_id=1,
            pool_address="0x1111111111111111111111111111111111111111",
            token0_address="0x2222222222222222222222222222222222222222",
            token1_address="0x3333333333333333333333333333333333333333",
            token0_decimals=18,
            token1_decimals=6,
            raw_reserve0=int((10 / math.sqrt(1.6)) * 10**18),
            raw_reserve1=int((10_000 * math.sqrt(1.6)) * 10**6),
            fee_bps=30,
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            parent_hash="0x" + "aa" * 32,
        )

        entry_state = PoolStateSnapshot(
            chain_id=1,
            token_address="0x2222222222222222222222222222222222222222",
            pool_address="0x1111111111111111111111111111111111111111",
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            source_timestamp_utc="2026-11-15T00:00:00Z",
            venue_type="UNISWAP_V2",
            evidence_v2=thin_entry_v2,
            native_usd_price=3000.0,
            base_fee_per_gas_wei=1_000_000_000,  # 1 gwei so gas is minimal
            priority_fee_per_gas_wei=100_000_000,
        )
        touch_state = PoolStateSnapshot(
            chain_id=1,
            token_address="0x2222222222222222222222222222222222222222",
            pool_address="0x1111111111111111111111111111111111111111",
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            source_timestamp_utc="2026-11-15T02:00:00Z",
            venue_type="UNISWAP_V2",
            evidence_v2=thin_touch_v2,
            native_usd_price=3000.0,
            base_fee_per_gas_wei=1_000_000_000,
            priority_fee_per_gas_wei=100_000_000,
        )

        multi_res = TwoLegExecutionSimulator.simulate_multi_size(entry_state, touch_state)

        self.assertEqual(multi_res["modeled_scenario_type"], "HYPOTHETICAL_DEPTH_PROBING_ONLY_NOT_POSITION_SIZING")
        tranches = multi_res["tranches"]
        self.assertIn("tranche_100", tranches)
        self.assertIn("tranche_500", tranches)
        self.assertIn("tranche_1000", tranches)
        self.assertIn("tranche_2500", tranches)
        self.assertIn("tranche_5000", tranches)

        # $100 tranche on $10k pool has small impact -> succeeds
        self.assertEqual(tranches["tranche_100"]["status"], DualTrackTargetStatus.MODELED_TP40_EXECUTABLE_WITH_ASSUMPTIONS.value)
        # $5000 tranche consumes 50% of pool reserves -> severe price impact causes fail
        self.assertEqual(tranches["tranche_5000"]["status"], DualTrackTargetStatus.MODELED_TP40_EXECUTION_IMPACT_FAIL.value)
        self.assertLess(tranches["tranche_5000"]["net_return_pct"], TECHNICAL_TP_PCT)

    def test_identity_and_dual_timestamp_binding(self):
        """AC-13: Every record validates exact (chain_id, token_mint, pool_address, block_hash) and dual timestamps."""
        rec = LiquidityObservationRecord(
            chain_id=42161,
            token_mint="0x" + "55" * 20,
            pool_address="0x" + "66" * 20,
            block_number=150_000_000,
            block_hash="0x" + "ee" * 32,
            source_event_timestamp_utc="2026-11-15T10:00:00Z",
            local_ingestion_timestamp_utc="2026-11-15T10:00:02Z",
            d_ask_plus_2pct=50_000,
            d_bid_minus_2pct=48_000,
            liquidity_usd=500_000.0,
        )

        self.assertEqual(rec.chain_id, 42161)
        self.assertEqual(len(rec.block_hash), 66)  # '0x' + 64 hex chars
        self.assertTrue(rec.source_event_timestamp_utc.endswith("Z"))
        self.assertTrue(rec.local_ingestion_timestamp_utc.endswith("Z"))
        t_src = dt.datetime.fromisoformat(rec.source_event_timestamp_utc.replace("Z", "+00:00"))
        t_ing = dt.datetime.fromisoformat(rec.local_ingestion_timestamp_utc.replace("Z", "+00:00"))
        self.assertGreaterEqual(t_ing, t_src)

    def test_protected_data_clean_equivalence(self):
        """AC-15: Protected data hashes remain unaltered with exact 64-character SHA-256 digests."""
        expected_hashes = {
            "rocket-hunter-data/user_positions_journal.json": "35CB27CD355FCE8EF5279E1192A5FF55701317F75A823627E9A9B5DB3065E1C0",
            "rocket-hunter-data/ai_learning_engine.json": "E4D8B76856BE9BA7822628AC70795263163EB681B9602E0C745321C3CC636318",
            "rocket-hunter-data/notification_config.json": "14066A465D309FA27343DAA8920BC0F45E163B5B03BED69C6027F7102B581956",
        }

        for rel_path, exp_sha in expected_hashes.items():
            full_path = os.path.join(os.getcwd(), rel_path)
            self.assertTrue(os.path.exists(full_path), f"File {rel_path} must exist")
            with open(full_path, "rb") as f:
                content = f.read()
            computed_sha = hashlib.sha256(content).hexdigest().upper()
            self.assertEqual(len(computed_sha), 64)
            self.assertEqual(computed_sha, exp_sha, f"Byte mismatch detected in protected file: {rel_path}")


if __name__ == "__main__":
    unittest.main()
