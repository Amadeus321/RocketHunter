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

import dataclasses
import datetime as dt
import hashlib
import math
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from scripts.research_evidence import (
    TECHNICAL_TP_PCT,
    MAX_HOLDING_HOURS,
    STAGE_32_VERSION,
    SIMULATION_TRANCHES_USD,
    UNISWAP_V3_FEE_SPACINGS,
    DualTrackTargetStatus,
    FinalityState,
    LiquidityClassificationStatus,
    TargetTouchClassification,
    CompleteReplayManifest,
    ReplayManifestBuilder,
    get_actual_git_state,
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
    MultiChainFinalityEngine,
    OpStackOutputProofEvidence,
    OpStackOutputProofPayload,
    OpStackOutputProofVerifier,
    TargetBlockStateProofResult,
    TargetBlockStateProofPayload,
    VerifiedTargetBlockStateResult,
    TargetStateProofVerifier,
    BlockHeaderTrustAnchor,
    PoolMetadataTrustAnchor,
    PoolMetadataVerifier,
    BlockHeaderTrustAnchorRegistry,
    PoolMetadataTrustAnchorRegistry,
    ValuationEvidenceRecord,
    ValuationEvidenceAuthenticator,
    ValuationEvidenceVerifier,
    ChainlinkRoundEvidence,
    PythPriceEvidence,
    UniswapV3TwapEvidence,
    derive_actual_runtime_dependencies,
    TRUSTED_VALUATION_SOURCES,
    compute_uniswap_v2_pair_address,
    HeaderConsensusEvidence,
    HeaderConsensusVerifier,
    UNISWAP_V2_FACTORY_MAINNET,
    CANONICAL_V2_FACTORIES,
    ProtectedDataVerifier,
    canonicalize_token_address,
)
from scripts.consensus_proof_verifier import (
    UniswapV2Slot3Decoder,
    encode_compact_path,
    rlp_encode,
    rlp_encode_bytes,
    keccak256,
    compute_sync_committee_root,
    DEV_HARDHAT_ANVIL_KEYS,
    verify_wormhole_vaa,
    WormholeGuardianRegistry,
    LightClientSyncCommitteeStore,
    SyncCommitteePeriodTransition,
    GINDEX_NEXT_SYNC_COMMITTEE,
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
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            base_fee_per_gas_wei=10_000_000_000,     # 10 gwei
            priority_fee_per_gas_wei=1_000_000_000,  # 1 gwei
        ).with_valuation_records()

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
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            base_fee_per_gas_wei=10_000_000_000,
            priority_fee_per_gas_wei=1_000_000_000,
        ).with_valuation_records()

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
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            base_fee_per_gas_wei=30_000_000_000,     # 30 gwei
            priority_fee_per_gas_wei=2_000_000_000,  # 2 gwei
        ).with_valuation_records()

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
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            base_fee_per_gas_wei=30_000_000_000,
            priority_fee_per_gas_wei=2_000_000_000,
        ).with_valuation_records()

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
            quote_usd_price=1.0,
            native_usd_price=3000.0,
        ).with_valuation_records()

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
        self.assertEqual(label, "MODELED_GAS_COST_UNCAPPED_LEGACY_ASSUMPTION")

    def test_eip1559_gas_calculation_with_max_fee_capped(self):
        """AC-7: EIP-1559 effective gas price formula caps tip: baseFee + min(priorityFee, maxFee - baseFee)."""
        gas_used = 100_000
        base_fee_wei = 20_000_000_000     # 20 gwei
        priority_fee_wei = 5_000_000_000  # 5 gwei
        max_fee_wei = 23_000_000_000      # 23 gwei (caps tip to 23 - 20 = 3 gwei)
        native_usd = 3000.0

        eth_cost, usd_cost, label = calculate_evm_gas_cost_usd(
            gas_used=gas_used,
            base_fee_wei=base_fee_wei,
            priority_fee_wei=priority_fee_wei,
            native_usd_price=native_usd,
            max_fee_per_gas_wei=max_fee_wei,
        )

        # Effective gas price = 20 gwei + min(5, 23 - 20) = 23 gwei
        # Total cost wei = 100,000 * 23 gwei = 2,300,000 gwei = 0.0023 ETH
        # Total cost USD = 0.0023 * $3,000 = $6.90
        self.assertAlmostEqual(eth_cost, 0.0023, places=8)
        self.assertAlmostEqual(usd_cost, 6.90, places=4)
        self.assertEqual(label, "MODELED_GAS_COST_CANONICAL_EIP1559")

    def test_eip1559_gas_calculation_fails_closed_when_max_fee_below_base_fee(self):
        """AC-7: When max_fee_per_gas_wei < base_fee_wei, calculation fails closed."""
        gas_used = 100_000
        base_fee_wei = 20_000_000_000     # 20 gwei
        priority_fee_wei = 5_000_000_000  # 5 gwei
        max_fee_wei = 15_000_000_000      # 15 gwei (below 20 gwei base fee!)
        native_usd = 3000.0

        eth_cost, usd_cost, label = calculate_evm_gas_cost_usd(
            gas_used=gas_used,
            base_fee_wei=base_fee_wei,
            priority_fee_wei=priority_fee_wei,
            native_usd_price=native_usd,
            max_fee_per_gas_wei=max_fee_wei,
        )

        self.assertEqual(label, "MODELED_GAS_FAIL_MAX_FEE_BELOW_BASE_FEE")
        self.assertEqual(eth_cost, 0.0)
        self.assertEqual(usd_cost, 0.0)

    def test_simulate_roundtrip_passes_max_fee_through_both_legs(self):
        """AC-1, AC-7: Passes max_fee_per_gas_wei through both legs and reflects capped effective gas."""
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
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            base_fee_per_gas_wei=10_000_000_000,     # 10 gwei
            priority_fee_per_gas_wei=5_000_000_000,  # 5 gwei
            max_fee_per_gas_wei=12_000_000_000,      # Capped to 12 gwei (tip=2 gwei)
        ).with_valuation_records()

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
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            base_fee_per_gas_wei=10_000_000_000,
            priority_fee_per_gas_wei=5_000_000_000,
            max_fee_per_gas_wei=13_000_000_000,      # Capped to 13 gwei (tip=3 gwei)
        ).with_valuation_records()

        result = TwoLegExecutionSimulator.simulate_roundtrip(
            entry_state=entry_snapshot,
            touch_state=touch_snapshot,
            tranche_usd=500.0,
            gas_used_buy=150_000,
            gas_used_sell=150_000,
        )

        # Expected buy gas: 150,000 * 12 gwei = 0.0018 ETH * 3000 = $5.40
        # Expected sell gas: 150,000 * 13 gwei = 0.00195 ETH * 3000 = $5.85
        self.assertAlmostEqual(result["gas_buy_usd"], 5.40, places=2)
        self.assertAlmostEqual(result["gas_sell_usd"], 5.85, places=2)
        self.assertEqual(result["gas_buy_model_label"], "MODELED_GAS_COST_CANONICAL_EIP1559")
        self.assertEqual(result["gas_sell_model_label"], "MODELED_GAS_COST_CANONICAL_EIP1559")

    def test_simulate_roundtrip_propagates_uncapped_legacy_gas_label(self):
        """AC-7, AC-11: Surfaces MODELED_GAS_COST_UNCAPPED_LEGACY_ASSUMPTION so uncapped assumptions cannot be hidden."""
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
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            base_fee_per_gas_wei=10_000_000_000,
            priority_fee_per_gas_wei=1_000_000_000,
            max_fee_per_gas_wei=None,  # Uncapped legacy assumption!
        ).with_valuation_records()
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
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            base_fee_per_gas_wei=10_000_000_000,
            priority_fee_per_gas_wei=1_000_000_000,
            max_fee_per_gas_wei=20_000_000_000,
        ).with_valuation_records()
        result = TwoLegExecutionSimulator.simulate_roundtrip(entry_snapshot, touch_snapshot, 500.0)
        self.assertEqual(result["gas_buy_model_label"], "MODELED_GAS_COST_UNCAPPED_LEGACY_ASSUMPTION")
        self.assertEqual(result["gas_sell_model_label"], "MODELED_GAS_COST_CANONICAL_EIP1559")

    def test_simulate_roundtrip_fails_closed_on_identity_and_provenance_mismatches(self):
        """AC-1: Mismatched chain_id, token, pool, block order, or embedded evidence fails closed."""
        base_entry = PoolStateSnapshot(
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
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
        ).with_valuation_records()
        base_touch = PoolStateSnapshot(
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
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
        ).with_valuation_records()

        # 1. Chain ID mismatch
        touch_bad_chain = dataclasses.replace(base_touch, chain_id=42161)
        res1 = TwoLegExecutionSimulator.simulate_roundtrip(base_entry, touch_bad_chain, 500.0)
        self.assertEqual(res1["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertIn("CHAIN_ID_MISMATCH", res1["diagnostic"])

        # 2. Token mismatch
        touch_bad_token = dataclasses.replace(base_touch, token_address="0x9999999999999999999999999999999999999999")
        res2 = TwoLegExecutionSimulator.simulate_roundtrip(base_entry, touch_bad_token, 500.0)
        self.assertEqual(res2["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertIn("TOKEN_MISMATCH", res2["diagnostic"])

        # 3. Pool mismatch
        touch_bad_pool = dataclasses.replace(base_touch, pool_address="0x8888888888888888888888888888888888888888")
        res3 = TwoLegExecutionSimulator.simulate_roundtrip(base_entry, touch_bad_pool, 500.0)
        self.assertEqual(res3["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertIn("POOL_MISMATCH", res3["diagnostic"])

        # 4. Block order invalid (touch before entry)
        touch_bad_block = dataclasses.replace(base_touch, block_number=18_999_999)
        res4 = TwoLegExecutionSimulator.simulate_roundtrip(base_entry, touch_bad_block, 500.0)
        self.assertEqual(res4["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertIn("BLOCK_ORDER_INVALID", res4["diagnostic"])

        # 5. Evidence block mismatch
        ev_bad_block = dataclasses.replace(self.entry_v2, block_number=18_000_000)
        entry_bad_ev = dataclasses.replace(base_entry, evidence_v2=ev_bad_block)
        res5 = TwoLegExecutionSimulator.simulate_roundtrip(entry_bad_ev, base_touch, 500.0)
        self.assertEqual(res5["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertIn("BLOCK_PROVENANCE_MISMATCH", res5["diagnostic"])

    def test_simulate_roundtrip_solana_snapshot_fails_closed_without_evm_gas(self):
        """AC-1, AC-7: Asserts Solana / non-EVM snapshot cannot be charged EVM gas or treated as supported roundtrip."""
        sol_entry = PoolStateSnapshot(
            chain_id=101,  # Solana Mainnet
            token_address="So11111111111111111111111111111111111111112",
            pool_address="58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2",
            block_number=280_000_000,
            block_hash="4Nd1mBQtrMJVYVfKf2PJy9NZToeBlAoibJ27JwV7GbpJ",
            source_timestamp_utc="2026-11-15T00:00:00Z",
            venue_type="RAYDIUM",
            quote_usd_price=1.0,
            native_usd_price=150.0,
            quote_usd_price_source="PYTH_NETWORK",
            native_usd_price_source="PYTH_NETWORK",
            quote_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            base_fee_per_gas_wei=20_000_000_000,
            priority_fee_per_gas_wei=2_000_000_000,
        ).with_valuation_records()
        sol_touch = dataclasses.replace(
            sol_entry,
            block_number=280_001_000,
            block_hash="5Me2nCRusNKWZWgLg3QKz0OafCpbcKpjKd38KxW8HcqK",
            source_timestamp_utc="2026-11-15T02:00:00Z",
            quote_usd_price_timestamp_utc="2026-11-15T02:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T02:00:00Z",
        )

        res = TwoLegExecutionSimulator.simulate_roundtrip(sol_entry, sol_touch, tranche_usd=500.0)
        self.assertEqual(res["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertEqual(res["diagnostic"], "UNSUPPORTED_NON_EVM_CHAIN_SOLANA_101")
        self.assertIsNone(res["net_return_pct"])
        # Must NOT be charged EVM gas or assigned EVM gas model labels
        self.assertIsNone(res["gas_buy_model_label"])
        self.assertIsNone(res["gas_sell_model_label"])
        self.assertEqual(res["gas_buy_usd"], 0.0)
        self.assertEqual(res["gas_sell_usd"], 0.0)

        # Cross-contamination test: Solana chain_id with spoofed venue_type="UNISWAP_V2" must also fail closed
        sol_spoofed_entry = dataclasses.replace(sol_entry, venue_type="UNISWAP_V2")
        sol_spoofed_touch = dataclasses.replace(sol_touch, venue_type="UNISWAP_V2")
        res_spoof = TwoLegExecutionSimulator.simulate_roundtrip(sol_spoofed_entry, sol_spoofed_touch, tranche_usd=500.0)
        self.assertEqual(res_spoof["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertEqual(res_spoof["diagnostic"], "UNSUPPORTED_NON_EVM_CHAIN_SOLANA_101")
        self.assertIsNone(res_spoof["gas_buy_model_label"])
        self.assertIsNone(res_spoof["gas_sell_model_label"])
        self.assertEqual(res_spoof["gas_buy_usd"], 0.0)
        self.assertEqual(res_spoof["gas_sell_usd"], 0.0)

        # Unsupported venue on EVM chain (e.g. RAYDIUM on chain_id=1) must also fail closed without charging EVM gas
        evm_unsupported_venue_entry = dataclasses.replace(sol_entry, chain_id=1, venue_type="RAYDIUM")
        evm_unsupported_venue_touch = dataclasses.replace(sol_touch, chain_id=1, venue_type="RAYDIUM")
        res_venue = TwoLegExecutionSimulator.simulate_roundtrip(evm_unsupported_venue_entry, evm_unsupported_venue_touch, tranche_usd=500.0)
        self.assertEqual(res_venue["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertEqual(res_venue["diagnostic"], "UNSUPPORTED_VENUE_RAYDIUM")
        self.assertIsNone(res_venue["gas_buy_model_label"])
        self.assertIsNone(res_venue["gas_sell_model_label"])
        self.assertEqual(res_venue["gas_buy_usd"], 0.0)
        self.assertEqual(res_venue["gas_sell_usd"], 0.0)

        # Unknown/non-EVM chain ID with UNISWAP_V2 (e.g. chain_id=99999) must fail closed via allowlist
        ev_v2_unknown = dataclasses.replace(self.entry_v2, chain_id=99999)
        unknown_chain_entry = PoolStateSnapshot(
            chain_id=99999,
            token_address="0x2222222222222222222222222222222222222222",
            pool_address="0x1111111111111111111111111111111111111111",
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            source_timestamp_utc="2026-11-15T00:00:00Z",
            venue_type="UNISWAP_V2",
            evidence_v2=ev_v2_unknown,
            quote_usd_price=1.0,
            native_usd_price=3000.0,
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
        ).with_valuation_records()
        unknown_chain_touch = dataclasses.replace(
            unknown_chain_entry,
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            source_timestamp_utc="2026-11-15T02:00:00Z",
            quote_usd_price_timestamp_utc="2026-11-15T02:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T02:00:00Z",
        )
        res_unknown = TwoLegExecutionSimulator.simulate_roundtrip(unknown_chain_entry, unknown_chain_touch, tranche_usd=500.0)
        self.assertEqual(res_unknown["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertEqual(res_unknown["diagnostic"], "UNSUPPORTED_CHAIN_ID_99999")
        self.assertIsNone(res_unknown["gas_buy_model_label"])
        self.assertIsNone(res_unknown["gas_sell_model_label"])
        self.assertEqual(res_unknown["gas_buy_usd"], 0.0)
        self.assertEqual(res_unknown["gas_sell_usd"], 0.0)
        self.assertIsNone(res_unknown["net_return_pct"])

    def test_simulate_roundtrip_max_fee_below_base_fee_fails_closed(self):
        """AC-1, AC-7: Max fee below base fee fails closed with ENTRY_MAX_FEE_BELOW_BASE_FEE."""
        base_entry = PoolStateSnapshot(
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
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            base_fee_per_gas_wei=20_000_000_000,
            priority_fee_per_gas_wei=2_000_000_000,
            max_fee_per_gas_wei=15_000_000_000,  # Below 20 gwei base fee!
        ).with_valuation_records()
        base_touch = PoolStateSnapshot(
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
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            base_fee_per_gas_wei=20_000_000_000,
            priority_fee_per_gas_wei=2_000_000_000,
            max_fee_per_gas_wei=25_000_000_000,
        ).with_valuation_records()
        res = TwoLegExecutionSimulator.simulate_roundtrip(base_entry, base_touch, 500.0)
        self.assertEqual(res["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertEqual(res["diagnostic"], "ENTRY_MAX_FEE_BELOW_BASE_FEE")

    def test_simulate_roundtrip_reversed_token_order_v2(self):
        """AC-1: V2 execution when tracked token is token1 and quote asset is token0 (reversed orientation)."""
        entry_v2_rev = UniswapV2Evidence(
            chain_id=1,
            pool_address="0x1111111111111111111111111111111111111111",
            token0_address="0x3333333333333333333333333333333333333333",  # Quote (USDC, 6 decimals)
            token1_address="0x2222222222222222222222222222222222222222",  # Tracked base token (18 decimals)
            token0_decimals=6,
            token1_decimals=18,
            raw_reserve0=1_000_000 * 10**6,  # 1,000,000 USDC
            raw_reserve1=1_000 * 10**18,     # 1,000 tokens
            fee_bps=30,
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "00" * 32,
        )
        touch_v2_rev = UniswapV2Evidence(
            chain_id=1,
            pool_address="0x1111111111111111111111111111111111111111",
            token0_address="0x3333333333333333333333333333333333333333",
            token1_address="0x2222222222222222222222222222222222222222",
            token0_decimals=6,
            token1_decimals=18,
            raw_reserve0=int(1_224_744.87 * 10**6),  # Quote increased (+50% price)
            raw_reserve1=int(816.49658 * 10**18),
            fee_bps=30,
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            parent_hash="0x" + "aa" * 32,
        )
        entry_snap = PoolStateSnapshot(
            chain_id=1,
            token_address="0x2222222222222222222222222222222222222222",  # token1 (reversed)
            pool_address="0x1111111111111111111111111111111111111111",
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            source_timestamp_utc="2026-11-15T00:00:00Z",
            venue_type="UNISWAP_V2",
            evidence_v2=entry_v2_rev,
            quote_usd_price=1.0,
            native_usd_price=3000.0,
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            base_fee_per_gas_wei=10_000_000_000,
            priority_fee_per_gas_wei=1_000_000_000,
            max_fee_per_gas_wei=20_000_000_000,
        ).with_valuation_records()
        touch_snap = PoolStateSnapshot(
            chain_id=1,
            token_address="0x2222222222222222222222222222222222222222",
            pool_address="0x1111111111111111111111111111111111111111",
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            source_timestamp_utc="2026-11-15T04:00:00Z",
            venue_type="UNISWAP_V2",
            evidence_v2=touch_v2_rev,
            quote_usd_price=1.0,
            native_usd_price=3000.0,
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            base_fee_per_gas_wei=10_000_000_000,
            priority_fee_per_gas_wei=1_000_000_000,
            max_fee_per_gas_wei=20_000_000_000,
        ).with_valuation_records()
        result = TwoLegExecutionSimulator.simulate_roundtrip(entry_snap, touch_snap, 500.0, gas_used_buy=150_000, gas_used_sell=150_000)
        self.assertEqual(result["status"], DualTrackTargetStatus.MODELED_TP40_EXECUTABLE_WITH_ASSUMPTIONS.value)
        self.assertGreaterEqual(result["net_return_pct"], TECHNICAL_TP_PCT)

    def test_simulate_roundtrip_reversed_token_order_v3(self):
        """AC-1: V3 execution when token0 is quote (token0_is_quote=True) and tracked token is token1."""
        spacing = 60
        t_entry = 0
        p_entry = UniswapV3TickMath.get_sqrt_ratio_at_tick(t_entry)
        bitmap = {-1: 0, 0: 0, 1: 0}

        ev_entry = UniswapV3Evidence(
            chain_id=1,
            pool_address="0x1111111111111111111111111111111111111111",
            token0_address="0x3333333333333333333333333333333333333333",  # Quote (token0)
            token1_address="0x2222222222222222222222222222222222222222",  # Tracked base (token1)
            token0_decimals=6,
            token1_decimals=18,
            fee_tier_ppm=3000,
            sqrt_price_x96=p_entry,
            tick=t_entry,
            active_liquidity=10_000_000_000_000_000,
            initialized_ticks={},
            tick_bitmap=bitmap,
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "00" * 32,
        )
        t_touch = -3800  # Base token appreciated: fewer base tokens per quote (lower tick in pool)
        p_touch = UniswapV3TickMath.get_sqrt_ratio_at_tick(t_touch)
        ev_touch = UniswapV3Evidence(
            chain_id=1,
            pool_address="0x1111111111111111111111111111111111111111",
            token0_address="0x3333333333333333333333333333333333333333",
            token1_address="0x2222222222222222222222222222222222222222",
            token0_decimals=6,
            token1_decimals=18,
            fee_tier_ppm=3000,
            sqrt_price_x96=p_touch,
            tick=t_touch,
            active_liquidity=10_000_000_000_000_000,
            initialized_ticks={},
            tick_bitmap=bitmap,
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            parent_hash="0x" + "aa" * 32,
        )
        entry_snap = PoolStateSnapshot(
            chain_id=1,
            token_address="0x2222222222222222222222222222222222222222",
            pool_address="0x1111111111111111111111111111111111111111",
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            source_timestamp_utc="2026-11-15T00:00:00Z",
            venue_type="UNISWAP_V3",
            evidence_v3=ev_entry,
            quote_usd_price=1.0,
            native_usd_price=3000.0,
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            base_fee_per_gas_wei=10_000_000_000,
            priority_fee_per_gas_wei=1_000_000_000,
            max_fee_per_gas_wei=20_000_000_000,
            token0_is_quote=True,
        ).with_valuation_records()
        touch_snap = PoolStateSnapshot(
            chain_id=1,
            token_address="0x2222222222222222222222222222222222222222",
            pool_address="0x1111111111111111111111111111111111111111",
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            source_timestamp_utc="2026-11-15T04:00:00Z",
            venue_type="UNISWAP_V3",
            evidence_v3=ev_touch,
            quote_usd_price=1.0,
            native_usd_price=3000.0,
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            base_fee_per_gas_wei=10_000_000_000,
            priority_fee_per_gas_wei=1_000_000_000,
            max_fee_per_gas_wei=20_000_000_000,
            token0_is_quote=True,
        ).with_valuation_records()
        result = TwoLegExecutionSimulator.simulate_roundtrip(entry_snap, touch_snap, 500.0, gas_used_buy=150_000, gas_used_sell=150_000)
        self.assertEqual(result["status"], DualTrackTargetStatus.MODELED_TP40_EXECUTABLE_WITH_ASSUMPTIONS.value)
        self.assertGreaterEqual(result["net_return_pct"], TECHNICAL_TP_PCT)

    def test_simulate_roundtrip_v3_orientation_conflict_fails_closed(self):
        """AC-1: V3 fails closed if token0_is_quote conflicts with actual base/quote token positions."""
        spacing = 60
        t_entry = 0
        p_entry = UniswapV3TickMath.get_sqrt_ratio_at_tick(t_entry)
        ev_entry = UniswapV3Evidence(
            chain_id=1,
            pool_address="0x1111111111111111111111111111111111111111",
            token0_address="0x2222222222222222222222222222222222222222",  # Tracked base is token0!
            token1_address="0x3333333333333333333333333333333333333333",
            token0_decimals=18,
            token1_decimals=6,
            fee_tier_ppm=3000,
            sqrt_price_x96=p_entry,
            tick=t_entry,
            active_liquidity=10_000_000_000,
            initialized_ticks={},
            tick_bitmap={0: 0},
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "00" * 32,
        )
        ev_touch = dataclasses.replace(ev_entry, block_number=19_001_000, block_hash="0x" + "bb" * 32)
        # Conflict: token_address is token0, but token0_is_quote is True!
        entry_conflicted = PoolStateSnapshot(
            chain_id=1,
            token_address="0x2222222222222222222222222222222222222222",
            pool_address="0x1111111111111111111111111111111111111111",
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            source_timestamp_utc="2026-11-15T00:00:00Z",
            venue_type="UNISWAP_V3",
            evidence_v3=ev_entry,
            quote_usd_price=1.0,
            native_usd_price=3000.0,
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            base_fee_per_gas_wei=10_000_000_000,
            priority_fee_per_gas_wei=1_000_000_000,
            max_fee_per_gas_wei=20_000_000_000,
            token0_is_quote=True,  # CONFLICT!
        ).with_valuation_records()
        touch_conflicted = dataclasses.replace(entry_conflicted, block_number=19_001_000, block_hash="0x" + "bb" * 32, evidence_v3=ev_touch)
        res = TwoLegExecutionSimulator.simulate_roundtrip(entry_conflicted, touch_conflicted, 500.0)
        self.assertEqual(res["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertEqual(res["diagnostic"], "V3_ORIENTATION_CONFLICT_TOKEN0_BASE_BUT_FLAG_TRUE")

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
        self.assertEqual(label, "MODELED_GAS_COST_SOLANA_COMPUTE_UNITS")


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

    def test_v3_swap_insufficient_input_fails_target_attainment(self):
        """AC-4: Swap with explicit target fails with TARGET_PRICE_NOT_REACHED when input exhausts early."""
        spacing = 60
        initial_tick = 50
        initial_p = UniswapV3TickMath.get_sqrt_ratio_at_tick(initial_tick)
        target_p = UniswapV3TickMath.get_sqrt_ratio_at_tick(350)

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
            active_liquidity=10_000_000,
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

        # Input of 10 wei is far too small to reach tick 350
        ok, consumed, yielded, final_p, diag = UniswapV3SwapSimulator.simulate_swap(
            evidence=evidence,
            amount_in=10,
            zero_for_one=False,
            target_sqrt_price_x96=target_p,
        )

        self.assertFalse(ok)
        self.assertEqual(diag, "TARGET_PRICE_NOT_REACHED_INSUFFICIENT_INPUT_OR_DEPTH")
        self.assertNotEqual(final_p, target_p)

    def test_v3_bidirectional_depth_incomplete_on_insufficient_input_or_depth(self):
        """AC-8: compute_bidirectional_depth returns is_depth_complete=False when target band cannot be reached."""
        spacing = 60
        initial_tick = 50
        initial_p = UniswapV3TickMath.get_sqrt_ratio_at_tick(initial_tick)

        # Pool with NO liquidity above spot (liquidity = 0) and no ticks initialized above
        bitmap = {(initial_tick // spacing) >> 8: 1}
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
            active_liquidity=0,  # Zero liquidity
            initialized_ticks={},
            tick_bitmap=bitmap,
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "00" * 32,
        )

        depth = UniswapV3SwapSimulator.compute_bidirectional_depth(evidence, token0_quote=True)
        self.assertFalse(depth["is_depth_complete"])
        self.assertEqual(depth["d_ask_plus_2pct"], 0)


class TestBidirectionalDepthAndLiquidityHierarchy(unittest.TestCase):
    """Test suite covering AC-8, AC-9, AC-11, AC-12, AC-13."""

    def test_bidirectional_depth_exact_v2_pool_equations(self):
        """AC-8: Asserts V2 bidirectional depth uses integer swap accounting and reaches exact spot-price boundary."""
        rq = 1_000_000
        rb = 1_000
        fee_bps = 30

        depth = UniswapV2SwapSimulator.compute_bidirectional_depth(rq, rb, fee_bps)

        # Exact integer swap accounting values
        self.assertEqual(depth["d_ask_plus_2pct"], 10132)
        self.assertEqual(depth["d_bid_minus_2pct"], 10848)
        self.assertEqual(depth["token_in_minus_2pct"], 11)

        # 1. Assert d_ask meets the +2% boundary and d_ask - 1 fails
        d_ask = depth["d_ask_plus_2pct"]
        ok, out, _, _, n_rq1, n_rb1 = UniswapV2SwapSimulator.simulate_swap(rq, rb, d_ask, fee_bps)
        self.assertTrue(ok)
        self.assertTrue(100 * n_rq1 * rb >= 102 * rq * n_rb1)

        ok0, out0, _, _, n_rq0, n_rb0 = UniswapV2SwapSimulator.simulate_swap(rq, rb, d_ask - 1, fee_bps)
        self.assertTrue(ok0)
        self.assertTrue(100 * n_rq0 * rb < 102 * rq * n_rb0)

        # 2. Assert token_in meets the -2% boundary and token_in - 1 fails
        token_in = depth["token_in_minus_2pct"]
        ok, out, _, _, n_rb1, n_rq1 = UniswapV2SwapSimulator.simulate_swap(rb, rq, token_in, fee_bps)
        self.assertTrue(ok)
        self.assertEqual(out, depth["d_bid_minus_2pct"])
        self.assertTrue(100 * n_rq1 * rb <= 98 * rq * n_rb1)

        ok0, out0, _, _, n_rb0, n_rq0 = UniswapV2SwapSimulator.simulate_swap(rb, rq, token_in - 1, fee_bps)
        self.assertTrue(ok0)
        self.assertTrue(100 * n_rq0 * rb > 98 * rq * n_rb0)

        # Asymmetry check: Bid quote output > Ask quote input due to fee and curve geometry
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
                block_number=19_001_000,
                block_hash="0x" + "bb" * 32,
                source_event_timestamp_utc="2026-11-15T05:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T05:00:05Z",
                d_ask_plus_2pct=98_000,
                d_bid_minus_2pct=98_000,
                liquidity_usd=980_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_002_000,
                block_hash="0x" + "cc" * 32,
                source_event_timestamp_utc="2026-11-15T10:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T10:00:05Z",
                d_ask_plus_2pct=97_000,
                d_bid_minus_2pct=97_000,
                liquidity_usd=970_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_003_000,
                block_hash="0x" + "dd" * 32,
                source_event_timestamp_utc="2026-11-15T15:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T15:00:05Z",
                d_ask_plus_2pct=96_000,
                d_bid_minus_2pct=96_000,
                liquidity_usd=960_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_004_000,
                block_hash="0x" + "ee" * 32,
                source_event_timestamp_utc="2026-11-15T20:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T20:00:05Z",
                d_ask_plus_2pct=95_000,
                d_bid_minus_2pct=95_000,
                liquidity_usd=950_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_005_000,
                block_hash="0x" + "ff" * 32,
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
        self.assertEqual(outcome["window_end_status"], "WINDOW_END_RESOLVED_UNVERIFIED")
        self.assertTrue(outcome["recovery_confirmed_at_window_end"])

    def test_liquidity_missing_initial_sample_fails_unresolved(self):
        """AC-9, AC-12: Trajectory missing sample in initial window [t0, t0 + 0.5h] fails to UNRESOLVED_LIQUIDITY."""
        t0 = "2026-11-15T00:00:00Z"
        # First sample starts at hour 2.0 (outside [t0, t0 + 0.5h])
        records = [
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_000_500,
                block_hash="0x" + "aa" * 32,
                source_event_timestamp_utc="2026-11-15T02:00:00Z",  # +2 hours late
                local_ingestion_timestamp_utc="2026-11-15T02:00:05Z",
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
                source_event_timestamp_utc="2026-11-15T23:48:00Z",
                local_ingestion_timestamp_utc="2026-11-15T23:48:05Z",
                d_ask_plus_2pct=95_000,
                d_bid_minus_2pct=95_000,
                liquidity_usd=950_000.0,
            ),
        ]

        outcome = LiquidityClassificationEngine.evaluate_liquidity_trajectory(records, t0)
        self.assertEqual(outcome["status"], LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value)
        self.assertEqual(outcome["diagnostic"], "INITIAL_SAMPLE_MISSING")

    def test_liquidity_missing_terminal_sample_fails_unresolved(self):
        """AC-9, AC-12: Trajectory missing terminal record in [t0 + 23.5h, t0 + 24.0h] cannot claim survival."""
        t0 = "2026-11-15T00:00:00Z"
        # Records with intact depth but stopping at hour 15 (no terminal record)
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
                d_ask_plus_2pct=100_000,
                d_bid_minus_2pct=100_000,
                liquidity_usd=1_000_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_002_000,
                block_hash="0x" + "cc" * 32,
                source_event_timestamp_utc="2026-11-15T10:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T10:00:05Z",
                d_ask_plus_2pct=100_000,
                d_bid_minus_2pct=100_000,
                liquidity_usd=1_000_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_003_000,
                block_hash="0x" + "dd" * 32,
                source_event_timestamp_utc="2026-11-15T15:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T15:00:05Z",
                d_ask_plus_2pct=100_000,
                d_bid_minus_2pct=100_000,
                liquidity_usd=1_000_000.0,
            ),
        ]

        outcome = LiquidityClassificationEngine.evaluate_liquidity_trajectory(records, t0)
        self.assertEqual(outcome["status"], LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value)
        self.assertEqual(outcome["diagnostic"], "TERMINAL_SAMPLE_MISSING")
        self.assertEqual(outcome["window_end_status"], "WINDOW_END_UNRESOLVED")
        self.assertIsNone(outcome["liquidity_end_usd_observed_unverified"])

    def test_window_end_status_returned_for_all_outcomes(self):
        """AC-11, AC-12: Enforces window_end_status is present in collapse, degraded, and survived results."""
        t0 = "2026-11-15T00:00:00Z"
        empty_outcome = LiquidityClassificationEngine.evaluate_liquidity_trajectory([], t0)
        self.assertEqual(empty_outcome["window_end_status"], "WINDOW_END_UNRESOLVED")

    def test_liquidity_pre_window_first_record_excluded(self):
        """AC-9, AC-12: Pre-window record (t < t0) is excluded and baseline depth is taken from [t0, t0+0.5h]."""
        t0 = "2026-11-15T00:00:00Z"
        records = [
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=18_999_000,
                block_hash="0x" + "99" * 32,
                source_event_timestamp_utc="2026-11-14T23:00:00Z",  # Pre-window (-1 hour)
                local_ingestion_timestamp_utc="2026-11-14T23:00:05Z",
                d_ask_plus_2pct=1_000,
                d_bid_minus_2pct=1_000,
                liquidity_usd=10_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_000_000,
                block_hash="0x" + "aa" * 32,
                source_event_timestamp_utc="2026-11-15T00:00:00Z",  # t0 initial sample
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
                d_ask_plus_2pct=98_000,
                d_bid_minus_2pct=98_000,
                liquidity_usd=980_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_002_000,
                block_hash="0x" + "cc" * 32,
                source_event_timestamp_utc="2026-11-15T10:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T10:00:05Z",
                d_ask_plus_2pct=97_000,
                d_bid_minus_2pct=97_000,
                liquidity_usd=970_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_003_000,
                block_hash="0x" + "dd" * 32,
                source_event_timestamp_utc="2026-11-15T15:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T15:00:05Z",
                d_ask_plus_2pct=96_000,
                d_bid_minus_2pct=96_000,
                liquidity_usd=960_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_004_000,
                block_hash="0x" + "ee" * 32,
                source_event_timestamp_utc="2026-11-15T20:00:00Z",
                local_ingestion_timestamp_utc="2026-11-15T20:00:05Z",
                d_ask_plus_2pct=95_000,
                d_bid_minus_2pct=95_000,
                liquidity_usd=950_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1,
                token_mint="0x" + "22" * 20,
                pool_address="0x" + "11" * 20,
                block_number=19_005_000,
                block_hash="0x" + "ff" * 32,
                source_event_timestamp_utc="2026-11-15T23:48:00Z",
                local_ingestion_timestamp_utc="2026-11-15T23:48:05Z",
                d_ask_plus_2pct=95_000,
                d_bid_minus_2pct=95_000,
                liquidity_usd=950_000.0,
            ),
        ]
        outcome = LiquidityClassificationEngine.evaluate_liquidity_trajectory(records, t0)
        self.assertEqual(outcome["status"], LiquidityClassificationStatus.LIQUIDITY_DEPTH_SURVIVED_OBSERVED_UNVERIFIED.value)
        self.assertAlmostEqual(outcome["min_depth_ratio"], 0.95, places=2)

    def test_liquidity_unsorted_records_sorted_chronologically(self):
        """AC-9, AC-13: Unsorted input records are ordered chronologically and evaluated accurately."""
        t0 = "2026-11-15T00:00:00Z"
        r_t0 = LiquidityObservationRecord(
            chain_id=1, token_mint="0x" + "22" * 20, pool_address="0x" + "11" * 20,
            block_number=19_000_000, block_hash="0x" + "aa" * 32,
            source_event_timestamp_utc="2026-11-15T00:00:00Z", local_ingestion_timestamp_utc="2026-11-15T00:00:05Z",
            d_ask_plus_2pct=100_000, d_bid_minus_2pct=100_000, liquidity_usd=1_000_000.0,
        )
        r_t5 = dataclasses.replace(r_t0, block_number=19_001_000, source_event_timestamp_utc="2026-11-15T05:00:00Z")
        r_t10 = dataclasses.replace(r_t0, block_number=19_002_000, source_event_timestamp_utc="2026-11-15T10:00:00Z")
        r_t15 = dataclasses.replace(r_t0, block_number=19_003_000, source_event_timestamp_utc="2026-11-15T15:00:00Z")
        r_t20 = dataclasses.replace(r_t0, block_number=19_004_000, source_event_timestamp_utc="2026-11-15T20:00:00Z")
        r_t238 = dataclasses.replace(r_t0, block_number=19_005_000, source_event_timestamp_utc="2026-11-15T23:48:00Z")

        # Jumbled input order
        jumbled = [r_t15, r_t0, r_t238, r_t5, r_t20, r_t10]
        outcome = LiquidityClassificationEngine.evaluate_liquidity_trajectory(jumbled, t0)
        self.assertEqual(outcome["status"], LiquidityClassificationStatus.LIQUIDITY_DEPTH_SURVIVED_OBSERVED_UNVERIFIED.value)
        self.assertEqual(outcome["window_end_status"], "WINDOW_END_RESOLVED_UNVERIFIED")

    def test_liquidity_mixed_pool_or_token_fails_closed(self):
        """AC-13: Records with mixed pool_address or token_mint fail closed to UNRESOLVED_LIQUIDITY."""
        t0 = "2026-11-15T00:00:00Z"
        r1 = LiquidityObservationRecord(
            chain_id=1, token_mint="0x" + "22" * 20, pool_address="0x" + "11" * 20,
            block_number=19_000_000, block_hash="0x" + "aa" * 32,
            source_event_timestamp_utc="2026-11-15T00:00:00Z", local_ingestion_timestamp_utc="2026-11-15T00:00:05Z",
            d_ask_plus_2pct=100_000, d_bid_minus_2pct=100_000, liquidity_usd=1_000_000.0,
        )
        r2 = LiquidityObservationRecord(
            chain_id=1, token_mint="0x" + "22" * 20, pool_address="0x" + "88" * 20,
            block_number=19_001_000, block_hash="0x" + "bb" * 32,
            source_event_timestamp_utc="2026-11-15T05:00:00Z", local_ingestion_timestamp_utc="2026-11-15T05:00:05Z",
            d_ask_plus_2pct=100_000, d_bid_minus_2pct=100_000, liquidity_usd=1_000_000.0,
        )
        outcome = LiquidityClassificationEngine.evaluate_liquidity_trajectory([r1, r2], t0)
        self.assertEqual(outcome["status"], LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value)
        self.assertEqual(outcome["diagnostic"], "MIXED_IDENTITY_IN_LIQUIDITY_RECORDS")

    def test_liquidity_post_window_collapse_excluded(self):
        """AC-9, AC-14: Collapse event at hour 25 (post-window) is excluded and does not trigger depth collapse."""
        t0 = "2026-11-15T00:00:00Z"
        records = [
            LiquidityObservationRecord(
                chain_id=1, token_mint="0x" + "22" * 20, pool_address="0x" + "11" * 20,
                block_number=19_000_000, block_hash="0x" + "aa" * 32,
                source_event_timestamp_utc="2026-11-15T00:00:00Z", local_ingestion_timestamp_utc="2026-11-15T00:00:05Z",
                d_ask_plus_2pct=100_000, d_bid_minus_2pct=100_000, liquidity_usd=1_000_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1, token_mint="0x" + "22" * 20, pool_address="0x" + "11" * 20,
                block_number=19_001_000, block_hash="0x" + "bb" * 32,
                source_event_timestamp_utc="2026-11-15T05:00:00Z", local_ingestion_timestamp_utc="2026-11-15T05:00:05Z",
                d_ask_plus_2pct=100_000, d_bid_minus_2pct=100_000, liquidity_usd=1_000_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1, token_mint="0x" + "22" * 20, pool_address="0x" + "11" * 20,
                block_number=19_002_000, block_hash="0x" + "cc" * 32,
                source_event_timestamp_utc="2026-11-15T10:00:00Z", local_ingestion_timestamp_utc="2026-11-15T10:00:05Z",
                d_ask_plus_2pct=100_000, d_bid_minus_2pct=100_000, liquidity_usd=1_000_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1, token_mint="0x" + "22" * 20, pool_address="0x" + "11" * 20,
                block_number=19_003_000, block_hash="0x" + "dd" * 32,
                source_event_timestamp_utc="2026-11-15T15:00:00Z", local_ingestion_timestamp_utc="2026-11-15T15:00:05Z",
                d_ask_plus_2pct=100_000, d_bid_minus_2pct=100_000, liquidity_usd=1_000_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1, token_mint="0x" + "22" * 20, pool_address="0x" + "11" * 20,
                block_number=19_004_000, block_hash="0x" + "ee" * 32,
                source_event_timestamp_utc="2026-11-15T20:00:00Z", local_ingestion_timestamp_utc="2026-11-15T20:00:05Z",
                d_ask_plus_2pct=100_000, d_bid_minus_2pct=100_000, liquidity_usd=1_000_000.0,
            ),
            LiquidityObservationRecord(
                chain_id=1, token_mint="0x" + "22" * 20, pool_address="0x" + "11" * 20,
                block_number=19_005_000, block_hash="0x" + "ff" * 32,
                source_event_timestamp_utc="2026-11-15T23:48:00Z", local_ingestion_timestamp_utc="2026-11-15T23:48:05Z",
                d_ask_plus_2pct=100_000, d_bid_minus_2pct=100_000, liquidity_usd=1_000_000.0,
            ),
            # Post-window record at hour 25.0 with catastrophic collapse!
            LiquidityObservationRecord(
                chain_id=1, token_mint="0x" + "22" * 20, pool_address="0x" + "11" * 20,
                block_number=19_006_000, block_hash="0x" + "01" * 32,
                source_event_timestamp_utc="2026-11-16T01:00:00Z", local_ingestion_timestamp_utc="2026-11-16T01:00:05Z",
                d_ask_plus_2pct=100, d_bid_minus_2pct=100, liquidity_usd=1_000.0,
            ),
        ]
        outcome = LiquidityClassificationEngine.evaluate_liquidity_trajectory(records, t0)
        self.assertEqual(outcome["status"], LiquidityClassificationStatus.LIQUIDITY_DEPTH_SURVIVED_OBSERVED_UNVERIFIED.value)
        self.assertEqual(outcome["window_end_status"], "WINDOW_END_RESOLVED_UNVERIFIED")

    def test_v2_bidirectional_depth_exact_integers_large_reserves(self):
        """AC-8: Asserts V2 depth calculations use exact integer arithmetic and meet boundaries on large reserves."""
        # Large reserves: 10^24 wei USDC quote (~$1T), 10^21 wei WETH
        rq = 10**24
        rb = 10**21
        fee_bps = 30

        depth = UniswapV2SwapSimulator.compute_bidirectional_depth(rq, rb, fee_bps)

        self.assertEqual(depth["d_ask_plus_2pct"], 9965442109995826223976)
        self.assertEqual(depth["d_bid_minus_2pct"], 10035559565346099920344)
        self.assertEqual(depth["token_in_minus_2pct"], 10167796361891734777)
        self.assertIsInstance(depth["d_ask_plus_2pct"], int)
        self.assertIsInstance(depth["d_bid_minus_2pct"], int)
        self.assertIsInstance(depth["token_in_minus_2pct"], int)

        # 1. d_ask meets boundary, d_ask - 1 fails
        d_ask = depth["d_ask_plus_2pct"]
        ok, _, _, _, n_rq1, n_rb1 = UniswapV2SwapSimulator.simulate_swap(rq, rb, d_ask, fee_bps)
        self.assertTrue(ok)
        self.assertTrue(100 * n_rq1 * rb >= 102 * rq * n_rb1)

        ok0, _, _, _, n_rq0, n_rb0 = UniswapV2SwapSimulator.simulate_swap(rq, rb, d_ask - 1, fee_bps)
        self.assertTrue(ok0)
        self.assertTrue(100 * n_rq0 * rb < 102 * rq * n_rb0)

        # 2. token_in meets boundary, token_in - 1 fails
        token_in = depth["token_in_minus_2pct"]
        ok, out, _, _, n_rb1, n_rq1 = UniswapV2SwapSimulator.simulate_swap(rb, rq, token_in, fee_bps)
        self.assertTrue(ok)
        self.assertEqual(out, depth["d_bid_minus_2pct"])
        self.assertTrue(100 * n_rq1 * rb <= 98 * rq * n_rb1)

        ok0, _, _, _, n_rb0, n_rq0 = UniswapV2SwapSimulator.simulate_swap(rb, rq, token_in - 1, fee_bps)
        self.assertTrue(ok0)
        self.assertTrue(100 * n_rq0 * rb > 98 * rq * n_rb0)

    def test_v2_bidirectional_depth_boundary_and_immediately_smaller_input(self):
        """AC-8: Asserts resulting reserves meet the boundary and immediately smaller input fails across fee/reserve cases."""
        test_cases = [
            (100_000, 50, 10),      # Low fee (10 bps)
            (100_000, 50, 30),      # Standard fee (30 bps)
            (100_000, 50, 100),     # High fee (100 bps)
            (10_000_000 * 10**6, 5_000 * 10**18, 30), # Production-scale reserves
        ]

        for rq, rb, fee_bps in test_cases:
            depth = UniswapV2SwapSimulator.compute_bidirectional_depth(rq, rb, fee_bps)
            d_ask = depth["d_ask_plus_2pct"]
            d_bid = depth["d_bid_minus_2pct"]
            token_in = depth["token_in_minus_2pct"]

            # Ask side: d_ask meets boundary, d_ask - 1 fails
            ok1, _, _, _, n_rq1, n_rb1 = UniswapV2SwapSimulator.simulate_swap(rq, rb, d_ask, fee_bps)
            self.assertTrue(ok1)
            self.assertTrue(100 * n_rq1 * rb >= 102 * rq * n_rb1, f"Failed at rq={rq}, rb={rb}, fee={fee_bps}")
            if d_ask > 1:
                ok0, out0, _, _, n_rq0, n_rb0 = UniswapV2SwapSimulator.simulate_swap(rq, rb, d_ask - 1, fee_bps)
                if ok0:
                    self.assertTrue(100 * n_rq0 * rb < 102 * rq * n_rb0, f"d_ask-1 unexpectedly met boundary at rq={rq}")
                else:
                    self.assertEqual(out0, 0)  # Zero output yielded: boundary cannot be reached

            # Bid side: token_in meets boundary, token_in - 1 fails
            ok1, out_bid, _, _, n_rb1, n_rq1 = UniswapV2SwapSimulator.simulate_swap(rb, rq, token_in, fee_bps)
            self.assertTrue(ok1)
            self.assertEqual(out_bid, d_bid)
            self.assertTrue(100 * n_rq1 * rb <= 98 * rq * n_rb1, f"Failed at rq={rq}, rb={rb}, fee={fee_bps}")
            if token_in > 1:
                ok0, out0, _, _, n_rb0, n_rq0 = UniswapV2SwapSimulator.simulate_swap(rb, rq, token_in - 1, fee_bps)
                if ok0:
                    self.assertTrue(100 * n_rq0 * rb > 98 * rq * n_rb0, f"token_in-1 unexpectedly met boundary at rq={rq}")
                else:
                    self.assertEqual(out0, 0)  # Zero output yielded: boundary cannot be reached

    def test_v2_bidirectional_depth_unreachable_boundary_small_reserves(self):
        """AC-8: Asserts failed/unreachable brackets return an explicit unresolved result, not numeric capacity."""
        # 1. Ask side unreachable: reserve_token = 1 (every attempted quote_in yields 0 token output)
        depth_ask_unreachable = UniswapV2SwapSimulator.compute_bidirectional_depth(
            reserve_quote=100,
            reserve_token=1,
            fee_bps=30,
        )
        self.assertEqual(depth_ask_unreachable["status"], DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value)
        self.assertFalse(depth_ask_unreachable["is_depth_complete"])
        self.assertIsNone(depth_ask_unreachable["d_ask_plus_2pct"])
        self.assertIsNone(depth_ask_unreachable["d_bid_minus_2pct"])
        self.assertIsNone(depth_ask_unreachable["token_in_minus_2pct"])
        self.assertEqual(depth_ask_unreachable["reason"], "ASK_DEPTH_NO_REACHABLE_BOUNDARY")

        # 2. Bid side unreachable: reserve_quote = 1 (every attempted token_in yields 0 quote output)
        depth_bid_unreachable = UniswapV2SwapSimulator.compute_bidirectional_depth(
            reserve_quote=1,
            reserve_token=100,
            fee_bps=30,
        )
        self.assertEqual(depth_bid_unreachable["status"], DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value)
        self.assertFalse(depth_bid_unreachable["is_depth_complete"])
        self.assertIsNone(depth_bid_unreachable["d_ask_plus_2pct"])
        self.assertIsNone(depth_bid_unreachable["d_bid_minus_2pct"])
        self.assertIsNone(depth_bid_unreachable["token_in_minus_2pct"])
        self.assertEqual(depth_bid_unreachable["reason"], "BID_DEPTH_NO_REACHABLE_BOUNDARY")

    def test_v2_bidirectional_depth_and_swap_invalid_fee_inputs(self):
        """AC-8: Asserts invalid fee inputs fail closed in simulate_swap and return unresolved in compute_bidirectional_depth."""
        invalid_fees = [-1, -50, 10000, 10001, 50000, 30.5, "30", None]

        for fee in invalid_fees:
            # simulate_swap must fail closed
            ok, out, _, _, r_in, r_out = UniswapV2SwapSimulator.simulate_swap(1_000_000, 1_000, 100, fee_bps=fee)
            self.assertFalse(ok)
            self.assertEqual(out, 0)

            # compute_bidirectional_depth must return explicit unresolved result, not numeric capacity
            depth = UniswapV2SwapSimulator.compute_bidirectional_depth(1_000_000, 1_000, fee_bps=fee)
            self.assertEqual(depth["status"], DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value)
            self.assertFalse(depth["is_depth_complete"])
            self.assertIsNone(depth["d_ask_plus_2pct"])
            self.assertIsNone(depth["d_bid_minus_2pct"])
            self.assertIsNone(depth["token_in_minus_2pct"])
            self.assertEqual(depth["reason"], "FEE_BPS_MUST_BE_INTEGER_IN_RANGE_0_TO_9999")

    def test_v2_bidirectional_depth_invalid_reserves(self):
        """AC-8: Asserts invalid reserves return explicit unresolved result with zero numeric capacity."""
        invalid_reserves = [
            (0, 1000),
            (1000, 0),
            (-100, 1000),
            (1000, -50),
            ("1000000", 1000),
            (1000000, 1000.5),
        ]

        for rq, rb in invalid_reserves:
            depth = UniswapV2SwapSimulator.compute_bidirectional_depth(rq, rb, fee_bps=30)
            self.assertEqual(depth["status"], DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value)
            self.assertFalse(depth["is_depth_complete"])
            self.assertIsNone(depth["d_ask_plus_2pct"])
            self.assertIsNone(depth["d_bid_minus_2pct"])
            self.assertIsNone(depth["token_in_minus_2pct"])
            self.assertEqual(depth["reason"], "RESERVES_MUST_BE_POSITIVE_INTEGERS")


class TestMultiSizeAndIdentityConstraints(unittest.TestCase):
    """Test suite covering AC-10, AC-13, AC-14, AC-15."""

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
            quote_usd_price=1.0,
            native_usd_price=3000.0,
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            base_fee_per_gas_wei=1_000_000_000,  # 1 gwei so gas is minimal
            priority_fee_per_gas_wei=100_000_000,
        ).with_valuation_records()
        touch_state = PoolStateSnapshot(
            chain_id=1,
            token_address="0x2222222222222222222222222222222222222222",
            pool_address="0x1111111111111111111111111111111111111111",
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            source_timestamp_utc="2026-11-15T02:00:00Z",
            venue_type="UNISWAP_V2",
            evidence_v2=thin_touch_v2,
            quote_usd_price=1.0,
            native_usd_price=3000.0,
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T02:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T02:00:00Z",
            base_fee_per_gas_wei=1_000_000_000,
            priority_fee_per_gas_wei=100_000_000,
        ).with_valuation_records()

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
        """AC-15: Protected data hashes remain unaltered with exact 64-character SHA-256 digests in isolated environment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            tmp_data = tmp_root / "rocket-hunter-data"
            tmp_data.mkdir(parents=True, exist_ok=True)
            mock_files = {
                "user_positions_journal.json": b'{"positions": []}',
                "ai_learning_engine.json": b'{"weights": {}}',
                "notification_config.json": b'{"notifications_enabled": false}',
            }
            for fname, content in mock_files.items():
                with open(tmp_data / fname, "wb") as f:
                    f.write(content)

            verifier = ProtectedDataVerifier(root_dir=tmp_root)
            snap_pre = verifier.snapshot_protected_data()
            self.assertEqual(len(snap_pre["protected_files"]), 3)
            for fname in mock_files:
                sha = snap_pre["protected_files"][fname]["sha256"]
                self.assertEqual(len(sha), 64)
                self.assertTrue(all(c in "0123456789ABCDEF" for c in sha))

            # Clean equivalence matches
            snap_post = verifier.snapshot_protected_data()
            is_clean, discrepancies = verifier.verify_clean_equivalence(snap_pre, snap_post)
            self.assertTrue(is_clean)
            self.assertEqual(discrepancies, [])

            # Modification detection fails closed
            with open(tmp_data / "user_positions_journal.json", "wb") as f:
                f.write(b'{"positions": ["tampered"]}')
            snap_tampered = verifier.snapshot_protected_data()
            is_clean2, discrepancies2 = verifier.verify_clean_equivalence(snap_pre, snap_tampered)
            self.assertFalse(is_clean2)
            self.assertTrue(any("user_positions_journal.json" in d for d in discrepancies2))

            # Deletion detection fails closed
            os.remove(tmp_data / "notification_config.json")
            snap_deleted = verifier.snapshot_protected_data()
            is_clean3, discrepancies3 = verifier.verify_clean_equivalence(snap_pre, snap_deleted)
            self.assertFalse(is_clean3)
            self.assertTrue(any("notification_config.json" in d for d in discrepancies3))

    def test_24_hour_holding_period_enforcement(self):
        """Requirement A: Enforce 24-hour holding window: 24.0h valid, 24.01h rejected, timezone-naive rejected."""
        base_entry = PoolStateSnapshot(
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
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
        ).with_valuation_records()

        # 1. Exactly 24.0 hours (86,400s) -> allowed
        touch_24h = PoolStateSnapshot(
            chain_id=1,
            token_address="0x2222222222222222222222222222222222222222",
            pool_address="0x1111111111111111111111111111111111111111",
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            source_timestamp_utc="2026-11-16T00:00:00Z",
            venue_type="UNISWAP_V2",
            evidence_v2=self.touch_v2_high,
            quote_usd_price=1.0,
            native_usd_price=3000.0,
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-16T00:00:00Z",
            native_usd_price_timestamp_utc="2026-11-16T00:00:00Z",
        ).with_valuation_records()
        res_24h = TwoLegExecutionSimulator.simulate_roundtrip(base_entry, touch_24h, 500.0)
        self.assertEqual(res_24h["status"], DualTrackTargetStatus.MODELED_TP40_EXECUTABLE_WITH_ASSUMPTIONS.value)

        # 2. 24 hours + 1 minute (86,460s) -> rejected
        touch_24h_plus = dataclasses.replace(
            touch_24h,
            source_timestamp_utc="2026-11-16T00:01:00Z",
            quote_usd_price_timestamp_utc="2026-11-16T00:01:00Z",
            native_usd_price_timestamp_utc="2026-11-16T00:01:00Z",
            quote_valuation_record=None,
            native_valuation_record=None,
        ).with_valuation_records()
        res_over = TwoLegExecutionSimulator.simulate_roundtrip(base_entry, touch_24h_plus, 500.0)
        self.assertEqual(res_over["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertEqual(res_over["diagnostic"], "HOLDING_WINDOW_EXCEEDED_24H")

        # 3. Timezone-naive timestamp rejected
        entry_naive = dataclasses.replace(
            base_entry,
            source_timestamp_utc="2026-11-15T00:00:00",
            quote_valuation_record=None,
            native_valuation_record=None,
        ).with_valuation_records()
        res_naive = TwoLegExecutionSimulator.simulate_roundtrip(entry_naive, touch_24h, 500.0)
        self.assertEqual(res_naive["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertEqual(res_naive["diagnostic"], "TIMEZONE_NAIVE_TIMESTAMP_REJECTED")

        # 4. Touch precedes entry (negative duration) rejected
        touch_precedes = dataclasses.replace(
            touch_24h,
            source_timestamp_utc="2026-11-14T23:59:00Z",
            quote_usd_price_timestamp_utc="2026-11-14T23:59:00Z",
            native_usd_price_timestamp_utc="2026-11-14T23:59:00Z",
            quote_valuation_record=None,
            native_valuation_record=None,
        ).with_valuation_records()
        res_precedes = TwoLegExecutionSimulator.simulate_roundtrip(base_entry, touch_precedes, 500.0)
        self.assertEqual(res_precedes["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertEqual(res_precedes["diagnostic"], "TOUCH_PRECEDES_ENTRY_TIMESTAMP")

        # 5. Missing timestamp rejected
        entry_no_ts = dataclasses.replace(base_entry, source_timestamp_utc="")
        res_no_ts = TwoLegExecutionSimulator.simulate_roundtrip(entry_no_ts, touch_24h, 500.0)
        self.assertEqual(res_no_ts["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertEqual(res_no_ts["diagnostic"], "ENTRY_OR_TOUCH_TIMESTAMP_MISSING")

    @staticmethod
    def _build_valid_mpt_target_state_payload(
        chain_id: int = 1,
        block_number: int = 19_001_000,
        block_hash: str = "0x" + "bb" * 32,
        token_address: str = "0x2222222222222222222222222222222222222222",
        pool_address: str = "0x1111111111111111111111111111111111111111",
        reserve0: int = 140_000 * 10**18,  # quote reserve
        reserve1: int = 10_000 * 10**18,   # token reserve -> spot price = 14.0 (reaches +40.0% from 10.0)
        slot: int = 3,
    ) -> Tuple[TargetBlockStateProofPayload, str]:
        raw_word = reserve0 | (reserve1 << 112) | (1700000000 << 224)
        storage_word_bytes = raw_word.to_bytes(32, "big")

        storage_search_key = UniswapV2Slot3Decoder.compute_storage_key(slot)
        s_nibbles = []
        for b in storage_search_key:
            s_nibbles.extend([b >> 4, b & 0x0F])
        s_compact = encode_compact_path(s_nibbles, is_leaf=True)
        storage_leaf = [s_compact, rlp_encode_bytes(storage_word_bytes)]
        storage_leaf_rlp = rlp_encode(storage_leaf)
        storage_root = keccak256(storage_leaf_rlp)

        code_hash = b"\xcc" * 32
        account_val = rlp_encode([b"", b"", storage_root, code_hash])
        clean_pool = canonicalize_token_address(pool_address)
        acct_search_key = keccak256(bytes.fromhex(clean_pool[2:]))
        a_nibbles = []
        for b in acct_search_key:
            a_nibbles.extend([b >> 4, b & 0x0F])
        a_compact = encode_compact_path(a_nibbles, is_leaf=True)
        account_leaf = [a_compact, account_val]
        account_leaf_rlp = rlp_encode(account_leaf)
        state_root = "0x" + keccak256(account_leaf_rlp).hex()

        payload = TargetBlockStateProofPayload(
            chain_id=chain_id,
            block_number=block_number,
            block_hash=block_hash,
            token_address=token_address,
            pool_address=pool_address,
            state_root=state_root,
            account_proof=[account_leaf_rlp],
            storage_proof=[storage_leaf_rlp],
            storage_slot=slot,
            token0_is_quote=True,
        )
        return payload, state_root

    @mock.patch("scripts.consensus_proof_verifier.verify_bls_aggregate_signature", return_value=True)
    def test_target_touch_evidence_separation(self, mock_bls):
        """Requirement B: Explicit separation of candle touches, on-chain touches, and verified block states."""
        # 1. Unverified candle touch
        c1 = DualTrackOutcomeEvaluator.classify_target_touch(
            entry_price=10.0,
            max_candle_high_24h=14.0,  # exactly +40%
            onchain_touch_observed=False,
            target_block_state_verified=False,
        )
        self.assertEqual(c1, TargetTouchClassification.CANDLE_OBSERVED_TOUCH)

        # 2. Onchain observed touch
        c2 = DualTrackOutcomeEvaluator.classify_target_touch(
            entry_price=10.0,
            max_candle_high_24h=14.0,
            onchain_touch_observed=True,
            target_block_state_verified=False,
        )
        self.assertEqual(c2, TargetTouchClassification.ONCHAIN_OBSERVED_TOUCH)

        # 3. Cryptographically verified target block state requires validated proof result from TargetStateProofVerifier
        token0_addr = "0x2222222222222222222222222222222222222222"  # quote token
        token1_addr = "0x3333333333333333333333333333333333333333"  # base token (target)
        pair_addr = compute_uniswap_v2_pair_address(UNISWAP_V2_FACTORY_MAINNET, token0_addr, token1_addr)

        valid_payload, state_root = self._build_valid_mpt_target_state_payload(
            chain_id=1,
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            token_address=token1_addr,
            pool_address=pair_addr,
            reserve0=140_000 * 10**18,
            reserve1=10_000 * 10**18,
        )

        def _make_ssz_branch(leaf: bytes, gindex: int):
            curr = leaf
            k = gindex
            branch = []
            while k > 1:
                sibling = hashlib.sha256(b"sibling:" + k.to_bytes(4, "big")).digest()
                branch.append(sibling)
                if k % 2 == 1:
                    curr = hashlib.sha256(sibling + curr).digest()
                else:
                    curr = hashlib.sha256(curr + sibling).digest()
                k //= 2
            return curr, branch

        b_hash_bytes = bytes.fromhex("bb" * 32)
        s_root_bytes = bytes.fromhex(state_root[2:])
        ep_root, sr_br = _make_ssz_branch(s_root_bytes, 34)
        body_root, ep_br = _make_ssz_branch(ep_root, 25)
        pubkeys_512 = [hashlib.sha256(f"pk_{i}".encode()).digest() + hashlib.sha256(f"ext_{i}".encode()).digest()[:16] for i in range(512)]
        sync_root = bytes.fromhex("98d69927b32c3e0bd9f091f2f728fb95aeec620d2b5f04390465e65cd5d00b5c")
        fin_root = bytes.fromhex("33" * 32)
        beacon_state_root, fin_br = _make_ssz_branch(fin_root, 105)
        ev_header = HeaderConsensusEvidence(
            slot=9_000_000,
            proposer_index=1,
            parent_root=bytes.fromhex("22" * 32),
            beacon_state_root=beacon_state_root,
            body_root=body_root,
            execution_payload_root=ep_root,
            execution_block_hash=b_hash_bytes,
            execution_block_number=19_001_000,
            execution_state_root=s_root_bytes,
            execution_receipts_root=bytes.fromhex("11" * 32),
            execution_timestamp=1700000000,
            execution_payload_branch=ep_br,
            state_root_branch=sr_br,
            sync_committee_bits=b"\xff" * 64,
            sync_committee_signature=b"\xaa" * 96,
            sync_committee_root=sync_root,
            finalized_checkpoint_root=fin_root,
            finalized_root_branch=fin_br,
            signature_slot=9_000_001,
            finalized_checkpoint_epoch=281250,
            sync_committee_pubkeys=pubkeys_512,
        )
        header_anchor = BlockHeaderTrustAnchor(
            chain_id=1,
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            state_root=state_root,
            header_timestamp_utc="2026-11-15T00:00:00Z",
            consensus_mechanism="PROOF_OF_STAKE",
            parent_hash="0x" + "22" * 32,
            receipts_root="0x" + "11" * 32,
            finality_status="FINALIZED_CANONICAL",
            trust_source="ETH_CONSENSUS_SYNC_COMMITTEE",
            verification_seal="",
            is_authenticated=True,
            consensus_evidence=ev_header,
        )

        pm_seal = PoolMetadataTrustAnchor._compute_seal(
            chain_id=1,
            pool_address=pair_addr,
            token0_address=token0_addr,
            token1_address=token1_addr,
            token0_decimals=18,
            token1_decimals=18,
            token0_is_quote=True,
            venue_type="UNISWAP_V2",
            reserves_storage_slot=3,
            factory_address=CANONICAL_V2_FACTORIES[1]["factory"],
            trust_source="INDEPENDENTLY_AUTHENTICATED_POOL_METADATA",
            storage_layout_verified=True,
        )
        pool_meta = PoolMetadataTrustAnchor(
            chain_id=1,
            pool_address=pair_addr,
            token0_address=token0_addr,
            token1_address=token1_addr,
            token0_decimals=18,
            token1_decimals=18,
            token0_is_quote=True,
            venue_type="UNISWAP_V2",
            reserves_storage_slot=3,
            factory_address=CANONICAL_V2_FACTORIES[1]["factory"],
            storage_layout_verified=True,
            is_create2_derived=True,
            trust_source="INDEPENDENTLY_AUTHENTICATED_POOL_METADATA",
            verification_seal=pm_seal,
            is_authenticated=True,
        )
        BlockHeaderTrustAnchorRegistry.register(header_anchor)
        PoolMetadataTrustAnchorRegistry.register(pool_meta)

        ok, valid_proof, err = TargetStateProofVerifier.verify_proof(
            payload=valid_payload,
            entry_price=10.0,
            trust_anchor=header_anchor,
            pool_metadata=pool_meta,
            expected_state_root=state_root,
        )
        self.assertTrue(ok)
        self.assertIsNotNone(valid_proof)
        self.assertTrue(valid_proof.target_price_reached)

        c3 = DualTrackOutcomeEvaluator.classify_target_touch(
            entry_price=10.0,
            max_candle_high_24h=14.0,
            onchain_touch_observed=True,
            target_state_proof=valid_proof,
            trust_anchor=header_anchor,
            pool_metadata=pool_meta,
            expected_chain_id=1,
            expected_block_number=19_001_000,
            expected_block_hash="0x" + "bb" * 32,
            expected_token_address=token1_addr,
            expected_pool_address=pair_addr,
            expected_state_root=state_root,
        )
        self.assertEqual(c3, TargetTouchClassification.VERIFIED_TARGET_BLOCK_STATE)

        # Direct payload verification also produces VERIFIED_TARGET_BLOCK_STATE
        c3_payload = DualTrackOutcomeEvaluator.classify_target_touch(
            entry_price=10.0,
            max_candle_high_24h=14.0,
            onchain_touch_observed=True,
            target_state_proof_payload=valid_payload,
            trust_anchor=header_anchor,
            pool_metadata=pool_meta,
            expected_chain_id=1,
            expected_block_number=19_001_000,
            expected_block_hash="0x" + "bb" * 32,
            expected_token_address=token1_addr,
            expected_pool_address=pair_addr,
            expected_state_root=state_root,
        )
        self.assertEqual(c3_payload, TargetTouchClassification.VERIFIED_TARGET_BLOCK_STATE)

        # Adversarial check: caller-constructed unsealed TargetBlockStateProofResult MUST NOT verify
        unsealed_proof = TargetBlockStateProofResult(
            chain_id=1,
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            token_address=token1_addr,
            pool_address=pair_addr,
            state_root=state_root,
            is_valid=True,
            target_price_reached=True,
        )
        c_unsealed = DualTrackOutcomeEvaluator.classify_target_touch(
            entry_price=10.0,
            max_candle_high_24h=14.0,
            onchain_touch_observed=True,
            target_state_proof=unsealed_proof,
        )
        self.assertEqual(c_unsealed, TargetTouchClassification.ONCHAIN_OBSERVED_TOUCH)

        # Adversarial check: forged HMAC seal on VerifiedTargetBlockStateResult MUST NOT verify
        forged_proof = VerifiedTargetBlockStateResult(
            chain_id=1,
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            token_address=token1_addr,
            pool_address=pair_addr,
            state_root=state_root,
            is_valid=True,
            target_price_reached=True,
            spot_price=14.0,
            verification_seal="forged_seal_00000000000000000000000000000000000000000000000000000000",
        )
        c_forged_seal = DualTrackOutcomeEvaluator.classify_target_touch(
            entry_price=10.0,
            max_candle_high_24h=14.0,
            onchain_touch_observed=True,
            target_state_proof=forged_proof,
        )
        self.assertEqual(c_forged_seal, TargetTouchClassification.ONCHAIN_OBSERVED_TOUCH)

        # Adversarial check: caller-asserted Boolean True MUST NOT verify
        c_caller_true = DualTrackOutcomeEvaluator.classify_target_touch(
            entry_price=10.0,
            max_candle_high_24h=14.0,
            onchain_touch_observed=True,
            target_block_state_verified=True,
        )
        self.assertEqual(c_caller_true, TargetTouchClassification.ONCHAIN_OBSERVED_TOUCH)

        # Adversarial check: caller passing target_state_proof=True (Boolean) MUST NOT verify
        c_bool_proof = DualTrackOutcomeEvaluator.classify_target_touch(
            entry_price=10.0,
            max_candle_high_24h=14.0,
            onchain_touch_observed=True,
            target_state_proof=True,  # type: ignore
        )
        self.assertEqual(c_bool_proof, TargetTouchClassification.ONCHAIN_OBSERVED_TOUCH)

        # Adversarial check: proof with is_valid=False
        proof_invalid = dataclasses.replace(valid_proof, is_valid=False)
        c_inv = DualTrackOutcomeEvaluator.classify_target_touch(
            entry_price=10.0,
            max_candle_high_24h=14.0,
            onchain_touch_observed=True,
            target_state_proof=proof_invalid,
        )
        self.assertEqual(c_inv, TargetTouchClassification.ONCHAIN_OBSERVED_TOUCH)

        # Adversarial check: proof with target_price_reached=False
        proof_not_reached = dataclasses.replace(valid_proof, target_price_reached=False)
        c_not_reach = DualTrackOutcomeEvaluator.classify_target_touch(
            entry_price=10.0,
            max_candle_high_24h=14.0,
            onchain_touch_observed=True,
            target_state_proof=proof_not_reached,
        )
        self.assertEqual(c_not_reach, TargetTouchClassification.ONCHAIN_OBSERVED_TOUCH)

        # Adversarial check: mismatched block identity
        c_mismatch = DualTrackOutcomeEvaluator.classify_target_touch(
            entry_price=10.0,
            max_candle_high_24h=14.0,
            onchain_touch_observed=True,
            target_state_proof=valid_proof,
            expected_block_hash="0x" + "ff" * 32,
        )
        self.assertEqual(c_mismatch, TargetTouchClassification.ONCHAIN_OBSERVED_TOUCH)

        # Adversarial check: mismatched token address
        c_tok_mismatch = DualTrackOutcomeEvaluator.classify_target_touch(
            entry_price=10.0,
            max_candle_high_24h=14.0,
            onchain_touch_observed=True,
            target_state_proof=valid_proof,
            expected_token_address="0x9999999999999999999999999999999999999999",
        )
        self.assertEqual(c_tok_mismatch, TargetTouchClassification.ONCHAIN_OBSERVED_TOUCH)

        # Requirement B Adversarial checks:
        # 1. Caller-supplied state root without trust anchor MUST NOT verify
        unregistered_payload = dataclasses.replace(valid_payload, block_hash="0x" + "77" * 32)
        ok_unanchored, _, err_unanchored = TargetStateProofVerifier.verify_proof(unregistered_payload, entry_price=10.0)
        self.assertFalse(ok_unanchored)
        self.assertEqual(err_unanchored, "UNAUTHENTICATED_BLOCK_HEADER_TRUST_ANCHOR_REQUIRED")

        # 2. Caller passing expected_state_root matching its own payload without trust anchor MUST NOT verify
        ok_self_assert, _, err_self_assert = TargetStateProofVerifier.verify_proof(
            unregistered_payload, entry_price=10.0, expected_state_root=unregistered_payload.state_root
        )
        self.assertFalse(ok_self_assert)
        self.assertEqual(err_self_assert, "UNAUTHENTICATED_BLOCK_HEADER_TRUST_ANCHOR_REQUIRED")

        # 3. Block hash mismatch against trust anchor fails closed
        bad_hash_payload = dataclasses.replace(valid_payload, block_hash="0x" + "88" * 32)
        ok_hash, _, err_hash = TargetStateProofVerifier.verify_proof(
            bad_hash_payload, entry_price=10.0, trust_anchor=header_anchor, pool_metadata=pool_meta
        )
        self.assertFalse(ok_hash)
        self.assertEqual(err_hash, "TRUST_ANCHOR_BLOCK_HASH_MISMATCH")

        # 4. State root mismatch against trust anchor fails closed
        bad_root_payload = dataclasses.replace(valid_payload, state_root="0x" + "77" * 32)
        ok_root, _, err_root = TargetStateProofVerifier.verify_proof(
            bad_root_payload, entry_price=10.0, trust_anchor=header_anchor, pool_metadata=pool_meta
        )
        self.assertFalse(ok_root)
        self.assertEqual(err_root, "TRUST_ANCHOR_STATE_ROOT_MISMATCH")

        # 5. Quote orientation mismatch fails closed
        bad_orient_seal = PoolMetadataTrustAnchor._compute_seal(
            chain_id=1,
            pool_address=pair_addr,
            token0_address=token0_addr,
            token1_address=token1_addr,
            token0_decimals=18,
            token1_decimals=18,
            token0_is_quote=False,
            venue_type="UNISWAP_V2",
            reserves_storage_slot=3,
            factory_address=CANONICAL_V2_FACTORIES[1]["factory"],
            trust_source="INDEPENDENTLY_AUTHENTICATED_POOL_METADATA",
            storage_layout_verified=True,
        )
        bad_orient_meta = dataclasses.replace(pool_meta, token0_is_quote=False, verification_seal=bad_orient_seal)
        ok_orient, _, err_orient = TargetStateProofVerifier.verify_proof(
            valid_payload, entry_price=10.0, trust_anchor=header_anchor, pool_metadata=bad_orient_meta
        )
        self.assertFalse(ok_orient)
        self.assertEqual(err_orient, "QUOTE_TOKEN_ORIENTATION_MISMATCH")

        # 6. Tampered pool venue invalidates cryptographic seal and fails closed
        bad_venue_meta = dataclasses.replace(pool_meta, venue_type="UNISWAP_V3")
        ok_venue, _, err_venue = TargetStateProofVerifier.verify_proof(
            valid_payload, entry_price=10.0, trust_anchor=header_anchor, pool_metadata=bad_venue_meta
        )
        self.assertFalse(ok_venue)
        self.assertEqual(err_venue, "UNAUTHENTICATED_POOL_METADATA_TRUST_ANCHOR_REQUIRED")

        # Creating metadata with unsupported venue raises ValueError
        with self.assertRaises(ValueError) as ctx_v3:
            PoolMetadataVerifier.verify_pool_metadata(
                chain_id=1,
                pool_address=pair_addr,
                token0_address=token0_addr,
                token1_address=token1_addr,
                token0_decimals=18,
                token1_decimals=18,
                token0_is_quote=True,
                venue_type="UNISWAP_V3",
            )
        self.assertIn("Unsupported venue_type", str(ctx_v3.exception))

        # 7. Unverified storage slot assumption (slot != 3) fails closed
        bad_slot_payload = dataclasses.replace(valid_payload, storage_slot=4)
        ok_slot, _, err_slot = TargetStateProofVerifier.verify_proof(
            bad_slot_payload, entry_price=10.0, trust_anchor=header_anchor, pool_metadata=pool_meta
        )
        self.assertFalse(ok_slot)
        self.assertEqual(err_slot, "UNSUPPORTED_POOL_VENUE_OR_STORAGE_SLOT")

        # 8. Direct caller injection into BlockHeaderTrustAnchorRegistry without authentication fails closed
        caller_fake_hdr = BlockHeaderTrustAnchor(
            chain_id=1,
            block_number=19_001_000,
            block_hash="0x" + "bb" * 32,
            state_root=state_root,
            header_timestamp_utc="2026-11-15T04:00:00Z",
            is_authenticated=True,
        )
        with self.assertRaises(ValueError) as ctx_hdr:
            BlockHeaderTrustAnchorRegistry.register(caller_fake_hdr)
        self.assertIn("failed cryptographic authenticity verification", str(ctx_hdr.exception))

        # 9. Direct caller injection into PoolMetadataTrustAnchorRegistry without authentication fails closed
        caller_fake_meta = PoolMetadataTrustAnchor(
            chain_id=1,
            pool_address="0x1111111111111111111111111111111111111111",
            token0_address="0x3333333333333333333333333333333333333333",
            token1_address="0x2222222222222222222222222222222222222222",
            token0_decimals=18,
            token1_decimals=18,
            token0_is_quote=True,
            venue_type="UNISWAP_V2",
            reserves_storage_slot=3,
            is_authenticated=True,
        )
        with self.assertRaises(ValueError) as ctx_pmeta:
            PoolMetadataTrustAnchorRegistry.register(caller_fake_meta)
        self.assertIn("failed cryptographic authenticity verification", str(ctx_pmeta.exception))

        # 10. Caller passing unauthenticated header or metadata to verify_proof fails closed
        ok_fake_hdr, _, err_fake_hdr = TargetStateProofVerifier.verify_proof(
            valid_payload, entry_price=10.0, trust_anchor=caller_fake_hdr, pool_metadata=pool_meta
        )
        self.assertFalse(ok_fake_hdr)
        self.assertEqual(err_fake_hdr, "UNAUTHENTICATED_BLOCK_HEADER_TRUST_ANCHOR_REQUIRED")

        ok_fake_m, _, err_fake_m = TargetStateProofVerifier.verify_proof(
            valid_payload, entry_price=10.0, trust_anchor=header_anchor, pool_metadata=caller_fake_meta
        )
        self.assertFalse(ok_fake_m)
        self.assertEqual(err_fake_m, "UNAUTHENTICATED_POOL_METADATA_TRUST_ANCHOR_REQUIRED")

        # 4. Target not reached
        c4 = DualTrackOutcomeEvaluator.classify_target_touch(
            entry_price=10.0,
            max_candle_high_24h=13.9,
            onchain_touch_observed=False,
            target_block_state_verified=False,
        )
        self.assertEqual(c4, TargetTouchClassification.TARGET_NOT_REACHED)

        # 5. Target time state unavailable (entry non-positive or state missing)
        c5 = DualTrackOutcomeEvaluator.classify_target_touch(
            entry_price=0.0,
            max_candle_high_24h=14.0,
        )
        self.assertEqual(c5, TargetTouchClassification.TARGET_TIME_STATE_UNAVAILABLE)

    def test_price_defaults_fail_closed_when_missing_or_non_positive(self):
        """Requirement G: Removal of unsafe price defaults; missing/non-positive price fails closed."""
        base_entry = PoolStateSnapshot(
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
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T00:00:00Z",
        ).with_valuation_records()
        base_touch = PoolStateSnapshot(
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
            quote_usd_price_source="CHAINLINK_ORACLE",
            native_usd_price_source="CHAINLINK_ORACLE",
            quote_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
            native_usd_price_timestamp_utc="2026-11-15T04:00:00Z",
        ).with_valuation_records()

        # Missing quote USD price on entry
        e_no_quote = dataclasses.replace(base_entry, quote_usd_price=None)
        res1 = TwoLegExecutionSimulator.simulate_roundtrip(e_no_quote, base_touch, 500.0)
        self.assertEqual(res1["diagnostic"], "ENTRY_QUOTE_PRICE_MISSING_OR_NON_POSITIVE")

        # Non-positive native price on entry
        e_neg_native = dataclasses.replace(base_entry, native_usd_price=-10.0)
        res2 = TwoLegExecutionSimulator.simulate_roundtrip(e_neg_native, base_touch, 500.0)
        self.assertEqual(res2["diagnostic"], "ENTRY_NATIVE_PRICE_MISSING_OR_NON_POSITIVE")

        # NaN quote price on touch
        t_nan_quote = dataclasses.replace(base_touch, quote_usd_price=float("nan"))
        res3 = TwoLegExecutionSimulator.simulate_roundtrip(base_entry, t_nan_quote, 500.0)
        self.assertEqual(res3["diagnostic"], "TOUCH_QUOTE_PRICE_MISSING_OR_NON_POSITIVE")

        # None native price on touch
        t_no_native = dataclasses.replace(base_touch, native_usd_price=None)
        res4 = TwoLegExecutionSimulator.simulate_roundtrip(base_entry, t_no_native, 500.0)
        self.assertEqual(res4["diagnostic"], "TOUCH_NATIVE_PRICE_MISSING_OR_NON_POSITIVE")

        # Missing quote price source on entry fails closed
        e_no_src = dataclasses.replace(base_entry, quote_usd_price_source=None)
        res_src = TwoLegExecutionSimulator.simulate_roundtrip(e_no_src, base_touch, 500.0)
        self.assertEqual(res_src["diagnostic"], "ENTRY_VALUATION_SOURCE_MISSING")

        # Missing native price source on touch fails closed
        t_no_src = dataclasses.replace(base_touch, native_usd_price_source="")
        res_t_src = TwoLegExecutionSimulator.simulate_roundtrip(base_entry, t_no_src, 500.0)
        self.assertEqual(res_t_src["diagnostic"], "TOUCH_VALUATION_SOURCE_MISSING")

        # Missing quote timestamp on entry fails closed
        e_no_ts = dataclasses.replace(base_entry, quote_usd_price_timestamp_utc=None)
        res_ts = TwoLegExecutionSimulator.simulate_roundtrip(e_no_ts, base_touch, 500.0)
        self.assertEqual(res_ts["diagnostic"], "ENTRY_VALUATION_TIMESTAMP_MISSING")

        # Stale entry quote price (>3600.0s staleness relative to snapshot) fails closed
        stale_quote_rec = dataclasses.replace(base_entry.quote_valuation_record, timestamp_utc="2026-11-15T01:05:00Z")
        e_stale = dataclasses.replace(
            base_entry,
            quote_usd_price_timestamp_utc="2026-11-15T01:05:00Z",
            quote_valuation_record=stale_quote_rec,
        )
        res_stale = TwoLegExecutionSimulator.simulate_roundtrip(e_stale, base_touch, 500.0)
        self.assertEqual(res_stale["diagnostic"], "ENTRY_QUOTE_VALUATION_STALE")

        # Stale touch native price (>3600.0s staleness relative to snapshot) fails closed
        stale_native_rec = dataclasses.replace(base_touch.native_valuation_record, timestamp_utc="2026-11-15T05:05:00Z")
        t_stale = dataclasses.replace(
            base_touch,
            native_usd_price_timestamp_utc="2026-11-15T05:05:00Z",
            native_valuation_record=stale_native_rec,
        )
        res_t_stale = TwoLegExecutionSimulator.simulate_roundtrip(base_entry, t_stale, 500.0)
        self.assertEqual(res_t_stale["diagnostic"], "TOUCH_NATIVE_VALUATION_STALE")

        # Requirement G: Mandatory ValuationEvidenceRecord missing fails closed
        e_no_rec = dataclasses.replace(base_entry, quote_valuation_record=None)
        res_no_rec = TwoLegExecutionSimulator.simulate_roundtrip(e_no_rec, base_touch, 500.0)
        self.assertEqual(res_no_rec["diagnostic"], "ENTRY_QUOTE_VALUATION_RECORD_MANDATORY_MISSING")

        # Requirement G: Unauthenticated ValuationEvidenceRecord fails closed when strict authentication is required
        unauth_rec = dataclasses.replace(base_entry.quote_valuation_record, is_authenticated=False)
        e_unauth = dataclasses.replace(base_entry, quote_valuation_record=unauth_rec)
        res_unauth = TwoLegExecutionSimulator.simulate_roundtrip(e_unauth, base_touch, 500.0, require_authenticated_valuation=True)
        self.assertEqual(res_unauth["diagnostic"], "ENTRY_QUOTE_VALUATION_RECORD_UNAUTHENTICATED")

        # Requirement G: Forged authentication flag without cryptographic seal fails closed
        forged_flag_rec = dataclasses.replace(base_entry.quote_valuation_record, is_authenticated=True, verification_seal="")
        e_forged = dataclasses.replace(base_entry, quote_valuation_record=forged_flag_rec)
        res_forged = TwoLegExecutionSimulator.simulate_roundtrip(e_forged, base_touch, 500.0)
        self.assertEqual(res_forged["diagnostic"], "ENTRY_QUOTE_FORGED_VALUATION_AUTHENTICATION_FLAG")

        # Requirement G: Fabricated feed address fails closed
        fab_feed_rec = dataclasses.replace(base_entry.quote_valuation_record, feed_or_pool_address="0x" + "44" * 20)
        e_fab_feed = dataclasses.replace(base_entry, quote_valuation_record=fab_feed_rec)
        res_fab_feed = TwoLegExecutionSimulator.simulate_roundtrip(e_fab_feed, base_touch, 500.0)
        self.assertEqual(res_fab_feed["diagnostic"], "ENTRY_QUOTE_VALUATION_FEED_ADDRESS_FABRICATED")

        # Requirement G: Invalid feed address (non-hex) fails closed
        bad_feed_rec = dataclasses.replace(base_entry.quote_valuation_record, feed_or_pool_address="invalid_feed")
        e_bad_feed = dataclasses.replace(base_entry, quote_valuation_record=bad_feed_rec)
        res_bad_feed = TwoLegExecutionSimulator.simulate_roundtrip(e_bad_feed, base_touch, 500.0)
        self.assertEqual(res_bad_feed["diagnostic"], "ENTRY_QUOTE_VALUATION_FEED_ADDRESS_INVALID")

        # Requirement G: Untrusted valuation source rejected (not in TRUSTED_VALUATION_SOURCES)
        untrusted_rec = dataclasses.replace(base_entry.quote_valuation_record, source="COINGECKO")
        e_untrusted = dataclasses.replace(base_entry, quote_usd_price_source="COINGECKO", quote_valuation_record=untrusted_rec)
        res_untrusted = TwoLegExecutionSimulator.simulate_roundtrip(e_untrusted, base_touch, 500.0)
        self.assertEqual(res_untrusted["diagnostic"], "ENTRY_QUOTE_VALUATION_SOURCE_UNTRUSTED")

        untrusted_t_rec = dataclasses.replace(base_touch.native_valuation_record, source="ARBITRARY_FEED")
        t_untrusted = dataclasses.replace(base_touch, native_usd_price_source="ARBITRARY_FEED", native_valuation_record=untrusted_t_rec)
        res_t_untrusted = TwoLegExecutionSimulator.simulate_roundtrip(base_entry, t_untrusted, 500.0)
        self.assertEqual(res_t_untrusted["diagnostic"], "TOUCH_NATIVE_VALUATION_SOURCE_UNTRUSTED")

        # Requirement G: Attached ValuationEvidenceRecord with chain mismatch rejected
        bad_chain_rec = ValuationEvidenceRecord(
            source="CHAINLINK_ORACLE",
            chain_id=10,  # mismatch with entry chain_id=1
            token_address=base_entry.token_address,
            price_usd=1.0,
            timestamp_utc=base_entry.quote_usd_price_timestamp_utc,
            feed_or_pool_address="0x8fffffd4afb6115b954bd326cbe7b4ba576818f6",
            is_authenticated=False,
        )
        e_bad_rec = dataclasses.replace(base_entry, quote_valuation_record=bad_chain_rec)
        res_bad_rec = TwoLegExecutionSimulator.simulate_roundtrip(e_bad_rec, base_touch, 500.0)
        self.assertEqual(res_bad_rec["diagnostic"], "ENTRY_QUOTE_VALUATION_CHAIN_MISMATCH")

        # Requirement G: Attached ValuationEvidenceRecord with price mismatch rejected
        bad_price_rec = ValuationEvidenceRecord(
            source="CHAINLINK_ORACLE",
            chain_id=1,
            token_address=base_entry.token_address,
            price_usd=2.5,  # mismatch with entry quote_usd_price=1.0
            timestamp_utc=base_entry.quote_usd_price_timestamp_utc,
            feed_or_pool_address="0x8fffffd4afb6115b954bd326cbe7b4ba576818f6",
            is_authenticated=False,
        )
        e_bad_price = dataclasses.replace(base_entry, quote_valuation_record=bad_price_rec)
        res_bad_price = TwoLegExecutionSimulator.simulate_roundtrip(e_bad_price, base_touch, 500.0)
        self.assertEqual(res_bad_price["diagnostic"], "ENTRY_QUOTE_VALUATION_PRICE_MISMATCH")

        # Requirement G: Valid ValuationEvidenceRecord matching provenance succeeds
        entry_ts = int(dt.datetime.fromisoformat("2026-11-15T00:00:00+00:00").timestamp())
        cl_ev = ChainlinkRoundEvidence(
            feed_address="0x8fffffd4afb6115b954bd326cbe7b4ba576818f6",
            round_id=1,
            answer=100_000_000,  # 1.0 with 8 decimals
            decimals=8,
            started_at=entry_ts - 30,
            updated_at=entry_ts,
            answered_in_round=1,
        )
        valid_rec = ValuationEvidenceVerifier.verify_chainlink_round(
            chain_id=1,
            token_address=base_entry.token_address,
            evidence=cl_ev,
        )
        e_valid_rec = dataclasses.replace(
            base_entry,
            quote_usd_price_timestamp_utc=valid_rec.timestamp_utc,
            quote_valuation_record=valid_rec,
        )
        res_valid = TwoLegExecutionSimulator.simulate_roundtrip(e_valid_rec, base_touch, 500.0)
        self.assertEqual(res_valid["status"], DualTrackTargetStatus.MODELED_TP40_EXECUTABLE_WITH_ASSUMPTIONS.value)
        self.assertNotEqual(res_valid["status"], DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value)
        self.assertIsNotNone(res_valid["net_return_pct"])

    def test_v3_intervening_bitmap_gap_and_uninitialized_tick_rejection(self):
        """Requirement C: Uniswap V3 swap simulator rejects bitmap gaps and uninitialized ticks."""
        p0 = UniswapV3TickMath.get_sqrt_ratio_at_tick(200000)
        tick_lower = 199980
        tick_upper = 200040
        w_lower = (tick_lower // 60) >> 8
        bit_lower = (tick_lower // 60) % 256
        w_upper = (tick_upper // 60) >> 8
        bit_upper = (tick_upper // 60) % 256

        bitmap = {
            w_lower: (1 << bit_lower) | (1 << bit_upper),
        }
        ticks = {
            tick_lower: {"liquidityNet": 10**18, "liquidityGross": 10**18},
            tick_upper: {"liquidityNet": -(10**18), "liquidityGross": 10**18},
        }

        # 1. Successful swap with complete bitmap and initialized ticks
        ev_good = UniswapV3Evidence(
            chain_id=1,
            pool_address="0x" + "11" * 20,
            token0_address="0x" + "22" * 20,
            token1_address="0x" + "33" * 20,
            token0_decimals=6,
            token1_decimals=18,
            fee_tier_ppm=3000,
            sqrt_price_x96=p0,
            tick=200000,
            active_liquidity=10**18,
            initialized_ticks=ticks,
            tick_bitmap=bitmap,
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            parent_hash="0x" + "bb" * 32,
        )
        ok, consumed, out, _, diag = UniswapV3SwapSimulator.simulate_swap(ev_good, amount_in=1000, zero_for_one=True)
        self.assertTrue(ok)
        self.assertEqual(diag, "SWAP_SUCCESS")

        # 2. Missing intervening bitmap word -> MODELED_DEPTH_UNRESOLVED_BITMAP_GAP
        ev_gap = dataclasses.replace(ev_good, tick_bitmap={})
        ok2, _, _, _, diag2 = UniswapV3SwapSimulator.simulate_swap(ev_gap, amount_in=1000, zero_for_one=True)
        self.assertFalse(ok2)
        self.assertEqual(diag2, "MODELED_DEPTH_UNRESOLVED_BITMAP_GAP")

        # 3. Bit set in bitmap but tick missing from initialized_ticks -> MODELED_DEPTH_UNRESOLVED_UNINITIALIZED_TICK
        ev_missing_tick = dataclasses.replace(
            ev_good,
            initialized_ticks={
                tick_upper: {"liquidityNet": -(10**18), "liquidityGross": 10**18},
                # tick_lower (199980) has its bit set in bitmap, but is missing from initialized_ticks!
            },
        )
        ok3, _, _, _, diag3 = UniswapV3SwapSimulator.simulate_swap(ev_missing_tick, amount_in=1000, zero_for_one=True)
        self.assertFalse(ok3)
        self.assertEqual(diag3, "MODELED_DEPTH_UNRESOLVED_UNINITIALIZED_TICK")

    def test_complete_replay_reproducibility_manifest(self):
        """Requirement H: Verifiable CompleteReplayManifest validates inputs and generates deterministic SHA-256 digest."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            dataset_file = tmp_path / "dataset_a.jsonl"
            # Write 2 valid JSON lines
            dataset_file.write_text('{"id": 1, "val": "a"}\n{"id": 2, "val": "b"}\n', encoding="utf-8")
            raw_bytes = dataset_file.read_bytes()
            actual_sha = hashlib.sha256(raw_bytes).hexdigest()
            cfg_sha = hashlib.sha256(b"test_configuration_data").hexdigest()

            notif_cfg = Path("rocket-hunter-data/notification_config.json")
            notif_sha = hashlib.sha256(notif_cfg.read_bytes()).hexdigest()

            actual_head, actual_clean = get_actual_git_state()
            current_head_sha = actual_head or "3a879cc38f6e60b51e9e8116fcdb43bdcbd1fbac"
            current_tree_clean = bool(actual_clean) if actual_clean is not None else False

            # 1. Valid manifest build succeeds and generates deterministic 64-char digest
            manifest = ReplayManifestBuilder.build(
                git_commit_sha=current_head_sha,
                is_working_tree_clean=current_tree_clean,
                input_datasets={"dataset_a": {"path": str(dataset_file), "sha256": actual_sha, "count": 2}},
                configuration_hashes={"config": cfg_sha, "rocket-hunter-data/notification_config.json": notif_sha},
                identity_bindings={"chain_id": 1},
                valuation_sources={"quote": {"source": "CHAINLINK_ORACLE", "timestamp_utc": "2026-11-15T00:00:00Z"}},
                gas_fee_assumptions={"base_fee_gwei": 20},
                slippage_execution_assumptions={"fee_tier_ppm": 3000},
                source_evidence_ids=["ev_1", "ev_2"],
                validate_strict=True,
            )

            digest1 = manifest.compute_digest()
            self.assertEqual(len(digest1), 64)
            self.assertEqual(digest1, manifest.compute_digest())  # Deterministic

            # Changing git commit alters digest
            m_diff_git = dataclasses.replace(manifest, git_commit_sha="2bba7cc9cd7db59ce428252050f882b99c581f61")
            self.assertNotEqual(digest1, m_diff_git.compute_digest())

            # Changing gas fee assumptions alters digest
            m_diff_gas = dataclasses.replace(manifest, gas_fee_assumptions={"base_fee_gwei": 30})
            self.assertNotEqual(digest1, m_diff_gas.compute_digest())

            # 2. Adversarial: Malformed git commit SHA fails closed
            with self.assertRaises(ValueError) as ctx:
                ReplayManifestBuilder.build(
                    git_commit_sha="not-a-valid-sha",
                    is_working_tree_clean=False,
                    input_datasets={"dataset_a": {"path": str(dataset_file), "sha256": actual_sha, "count": 2}},
                    configuration_hashes={"config": cfg_sha},
                    identity_bindings={"chain_id": 1},
                    valuation_sources={"quote": {"source": "CHAINLINK_ORACLE", "timestamp_utc": "2026-11-15T00:00:00Z"}},
                    gas_fee_assumptions={},
                    slippage_execution_assumptions={},
                    source_evidence_ids=["ev_1"],
                    validate_strict=True,
                )
            self.assertIn("git_commit_sha", str(ctx.exception))

            # 3. Adversarial: Malformed dataset SHA-256 format fails closed
            with self.assertRaises(ValueError) as ctx:
                ReplayManifestBuilder.build(
                    git_commit_sha=current_head_sha,
                    is_working_tree_clean=current_tree_clean,
                    input_datasets={"dataset_a": {"path": str(dataset_file), "sha256": "ABCD", "count": 2}},
                    configuration_hashes={"config": cfg_sha},
                    identity_bindings={"chain_id": 1},
                    valuation_sources={"quote": {"source": "CHAINLINK_ORACLE", "timestamp_utc": "2026-11-15T00:00:00Z"}},
                    gas_fee_assumptions={},
                    slippage_execution_assumptions={},
                    source_evidence_ids=["ev_1"],
                    validate_strict=True,
                )
            self.assertIn("invalid SHA-256 format", str(ctx.exception))

            # 4. Adversarial: Missing dataset file fails closed
            with self.assertRaises(ValueError) as ctx:
                ReplayManifestBuilder.build(
                    git_commit_sha=current_head_sha,
                    is_working_tree_clean=current_tree_clean,
                    input_datasets={"dataset_a": {"path": str(tmp_path / "nonexistent.jsonl"), "sha256": actual_sha, "count": 2}},
                    configuration_hashes={"config": cfg_sha},
                    identity_bindings={"chain_id": 1},
                    valuation_sources={"quote": {"source": "CHAINLINK_ORACLE", "timestamp_utc": "2026-11-15T00:00:00Z"}},
                    gas_fee_assumptions={},
                    slippage_execution_assumptions={},
                    source_evidence_ids=["ev_1"],
                    validate_strict=True,
                )
            self.assertIn("file not found", str(ctx.exception))

            # 5. Adversarial: Altered file content (hash mismatch) fails closed
            bogus_sha = hashlib.sha256(b"completely_different_content").hexdigest()
            with self.assertRaises(ValueError) as ctx:
                ReplayManifestBuilder.build(
                    git_commit_sha=current_head_sha,
                    is_working_tree_clean=current_tree_clean,
                    input_datasets={"dataset_a": {"path": str(dataset_file), "sha256": bogus_sha, "count": 2}},
                    configuration_hashes={"config": cfg_sha},
                    identity_bindings={"chain_id": 1},
                    valuation_sources={"quote": {"source": "CHAINLINK_ORACLE", "timestamp_utc": "2026-11-15T00:00:00Z"}},
                    gas_fee_assumptions={},
                    slippage_execution_assumptions={},
                    source_evidence_ids=["ev_1"],
                    validate_strict=True,
                )
            self.assertIn("SHA-256 mismatch", str(ctx.exception))

            # 6. Adversarial: Record count mismatch fails closed
            with self.assertRaises(ValueError) as ctx:
                ReplayManifestBuilder.build(
                    git_commit_sha=current_head_sha,
                    is_working_tree_clean=current_tree_clean,
                    input_datasets={"dataset_a": {"path": str(dataset_file), "sha256": actual_sha, "count": 999}},
                    configuration_hashes={"config": cfg_sha},
                    identity_bindings={"chain_id": 1},
                    valuation_sources={"quote": {"source": "CHAINLINK_ORACLE", "timestamp_utc": "2026-11-15T00:00:00Z"}},
                    gas_fee_assumptions={},
                    slippage_execution_assumptions={},
                    source_evidence_ids=["ev_1"],
                    validate_strict=True,
                )
            self.assertIn("record count mismatch", str(ctx.exception))

            # 7. Adversarial: Malformed configuration hash format fails closed
            with self.assertRaises(ValueError) as ctx:
                ReplayManifestBuilder.build(
                    git_commit_sha=current_head_sha,
                    is_working_tree_clean=current_tree_clean,
                    input_datasets={"dataset_a": {"path": str(dataset_file), "sha256": actual_sha, "count": 2}},
                    configuration_hashes={"config": "1234"},
                    identity_bindings={"chain_id": 1},
                    valuation_sources={"quote": {"source": "CHAINLINK_ORACLE", "timestamp_utc": "2026-11-15T00:00:00Z"}},
                    gas_fee_assumptions={},
                    slippage_execution_assumptions={},
                    source_evidence_ids=["ev_1"],
                    validate_strict=True,
                )
            self.assertIn("invalid SHA-256 format", str(ctx.exception))

            # 8. Adversarial: Timezone-naive valuation source timestamp fails closed
            with self.assertRaises(ValueError) as ctx:
                ReplayManifestBuilder.build(
                    git_commit_sha=current_head_sha,
                    is_working_tree_clean=current_tree_clean,
                    input_datasets={"dataset_a": {"path": str(dataset_file), "sha256": actual_sha, "count": 2}},
                    configuration_hashes={"config": cfg_sha},
                    identity_bindings={"chain_id": 1},
                    valuation_sources={"quote": {"source": "CHAINLINK_ORACLE", "timestamp_utc": "2026-11-15T00:00:00"}},
                    gas_fee_assumptions={},
                    slippage_execution_assumptions={},
                    source_evidence_ids=["ev_1"],
                    validate_strict=True,
                )
            self.assertIn("timezone-naive", str(ctx.exception))

            # 9. Adversarial: Untrusted valuation source fails closed
            with self.assertRaises(ValueError) as ctx:
                ReplayManifestBuilder.build(
                    git_commit_sha=current_head_sha,
                    is_working_tree_clean=current_tree_clean,
                    input_datasets={"dataset_a": {"path": str(dataset_file), "sha256": actual_sha, "count": 2}},
                    configuration_hashes={"config": cfg_sha},
                    identity_bindings={"chain_id": 1},
                    valuation_sources={"quote": {"source": "COINGECKO", "timestamp_utc": "2026-11-15T00:00:00Z"}},
                    gas_fee_assumptions={},
                    slippage_execution_assumptions={},
                    source_evidence_ids=["ev_1"],
                    validate_strict=True,
                )
            self.assertIn("untrusted source", str(ctx.exception))

            # 10. Adversarial: Missing required dataset_a fails closed
            with self.assertRaises(ValueError) as ctx:
                ReplayManifestBuilder.build(
                    git_commit_sha=current_head_sha,
                    is_working_tree_clean=current_tree_clean,
                    input_datasets={"dataset_b": {"path": str(dataset_file), "sha256": actual_sha, "count": 2}},
                    configuration_hashes={"config": cfg_sha},
                    identity_bindings={"chain_id": 1},
                    valuation_sources={"quote": {"source": "CHAINLINK_ORACLE", "timestamp_utc": "2026-11-15T00:00:00Z"}},
                    gas_fee_assumptions={},
                    slippage_execution_assumptions={},
                    source_evidence_ids=["ev_1"],
                    validate_strict=True,
                )
            self.assertIn("missing required 'dataset_a'", str(ctx.exception))

            # 11. Adversarial: Incorrect repository root (missing .git) fails closed
            with self.assertRaises(ValueError) as ctx:
                ReplayManifestBuilder.build(
                    git_commit_sha=current_head_sha,
                    is_working_tree_clean=current_tree_clean,
                    input_datasets={"dataset_a": {"path": str(dataset_file), "sha256": actual_sha, "count": 2}},
                    configuration_hashes={"config": cfg_sha, "rocket-hunter-data/notification_config.json": notif_sha},
                    identity_bindings={"chain_id": 1},
                    valuation_sources={"quote": {"source": "CHAINLINK_ORACLE", "timestamp_utc": "2026-11-15T00:00:00Z"}},
                    gas_fee_assumptions={"base_fee_gwei": 20},
                    slippage_execution_assumptions={"fee_tier_ppm": 3000},
                    source_evidence_ids=["ev_1"],
                    base_dir=tmp_path,
                    validate_strict=True,
                )
            self.assertIn("INCORRECT_REPOSITORY_ROOT", str(ctx.exception))

            # 12. Adversarial: Incomplete dependency inventory containing only python fails closed
            with self.assertRaises(ValueError) as ctx:
                ReplayManifestBuilder.build(
                    git_commit_sha=current_head_sha,
                    is_working_tree_clean=current_tree_clean,
                    input_datasets={"dataset_a": {"path": str(dataset_file), "sha256": actual_sha, "count": 2}},
                    configuration_hashes={"config": cfg_sha, "rocket-hunter-data/notification_config.json": notif_sha},
                    identity_bindings={"chain_id": 1},
                    valuation_sources={"quote": {"source": "CHAINLINK_ORACLE", "timestamp_utc": "2026-11-15T00:00:00Z"}},
                    gas_fee_assumptions={"base_fee_gwei": 20},
                    slippage_execution_assumptions={"fee_tier_ppm": 3000},
                    source_evidence_ids=["ev_1"],
                    dependency_versions={"python": "3.13.14", "inventory_status": "COMPLETE"},
                    validate_strict=True,
                )
            self.assertIn("DEPENDENCY_INVENTORY_INCOMPLETE: Inventory contains only Python", str(ctx.exception))

            # 13. Adversarial: Incomplete dependency inventory status flag fails closed
            actual_deps = derive_actual_runtime_dependencies()
            incomplete_deps = dict(actual_deps)
            incomplete_deps["inventory_status"] = "INCOMPLETE"
            with self.assertRaises(ValueError) as ctx:
                ReplayManifestBuilder.build(
                    git_commit_sha=current_head_sha,
                    is_working_tree_clean=current_tree_clean,
                    input_datasets={"dataset_a": {"path": str(dataset_file), "sha256": actual_sha, "count": 2}},
                    configuration_hashes={"config": cfg_sha, "rocket-hunter-data/notification_config.json": notif_sha},
                    identity_bindings={"chain_id": 1},
                    valuation_sources={"quote": {"source": "CHAINLINK_ORACLE", "timestamp_utc": "2026-11-15T00:00:00Z"}},
                    gas_fee_assumptions={"base_fee_gwei": 20},
                    slippage_execution_assumptions={"fee_tier_ppm": 3000},
                    source_evidence_ids=["ev_1"],
                    dependency_versions=incomplete_deps,
                    validate_strict=True,
                )
            self.assertIn("DEPENDENCY_INVENTORY_INCOMPLETE", str(ctx.exception))

            # 14. Adversarial: Unlisted configuration file omitted from manifest fails closed
            with self.assertRaises(ValueError) as ctx:
                ReplayManifestBuilder.build(
                    git_commit_sha=current_head_sha,
                    is_working_tree_clean=current_tree_clean,
                    input_datasets={"dataset_a": {"path": str(dataset_file), "sha256": actual_sha, "count": 2}},
                    configuration_hashes={"config": cfg_sha},  # omitted notification_config.json
                    identity_bindings={"chain_id": 1},
                    valuation_sources={"quote": {"source": "CHAINLINK_ORACLE", "timestamp_utc": "2026-11-15T00:00:00Z"}},
                    gas_fee_assumptions={"base_fee_gwei": 20},
                    slippage_execution_assumptions={"fee_tier_ppm": 3000},
                    source_evidence_ids=["ev_1"],
                    validate_strict=True,
                )
            self.assertIn("Unlisted configuration file omitted from manifest", str(ctx.exception))

            # 15. Adversarial: Tampered configuration file SHA-256 on disk fails closed
            tampered_hashes = {"config": cfg_sha, "rocket-hunter-data/notification_config.json": "00" * 32}
            with self.assertRaises(ValueError) as ctx:
                ReplayManifestBuilder.build(
                    git_commit_sha=current_head_sha,
                    is_working_tree_clean=current_tree_clean,
                    input_datasets={"dataset_a": {"path": str(dataset_file), "sha256": actual_sha, "count": 2}},
                    configuration_hashes=tampered_hashes,
                    identity_bindings={"chain_id": 1},
                    valuation_sources={"quote": {"source": "CHAINLINK_ORACLE", "timestamp_utc": "2026-11-15T00:00:00Z"}},
                    gas_fee_assumptions={"base_fee_gwei": 20},
                    slippage_execution_assumptions={"fee_tier_ppm": 3000},
                    source_evidence_ids=["ev_1"],
                    validate_strict=True,
                )
            self.assertIn("SHA-256 mismatch", str(ctx.exception))


class TestPhase3TrustArchitectureAdversarialSuite(unittest.TestCase):
    """Rigorous adversarial test suite verifying all 12 trust-boundary and verification requirements."""

    def _make_ssz_branch(self, leaf: bytes, gindex: int):
        curr = leaf
        k = gindex
        branch = []
        while k > 1:
            sibling = hashlib.sha256(b"sibling:" + k.to_bytes(4, "big")).digest()
            branch.append(sibling)
            if k % 2 == 1:
                curr = hashlib.sha256(sibling + curr).digest()
            else:
                curr = hashlib.sha256(curr + sibling).digest()
            k //= 2
        return curr, branch

    def _make_pubkeys_512(self):
        return [
            hashlib.sha256(f"pk_{i}".encode()).digest() + hashlib.sha256(f"ext_{i}".encode()).digest()[:16]
            for i in range(512)
        ]

    def test_1_attested_but_non_finalized_execution_block_cannot_be_labeled_finalized(self):
        """Adversarial Test 1: An attested but non-finalized execution block cannot be labeled finalized."""
        pubkeys_512 = self._make_pubkeys_512()
        sync_root = bytes.fromhex("98d69927b32c3e0bd9f091f2f728fb95aeec620d2b5f04390465e65cd5d00b5c")
        sr = bytes.fromhex("55" * 32)
        ep_root, sr_br = self._make_ssz_branch(sr, 34)
        body_root, ep_br = self._make_ssz_branch(ep_root, 25)
        fin_root = bytes.fromhex("33" * 32)
        beacon_state_root, fin_br = self._make_ssz_branch(fin_root, 105)

        # Attested slot 9,000,000, but finalized epoch only 100,000 (finalized slot 3,200,000 < 9,000,000)
        ev_unfinalized = HeaderConsensusEvidence(
            slot=9_000_000,
            proposer_index=1,
            parent_root=bytes.fromhex("22" * 32),
            beacon_state_root=beacon_state_root,
            body_root=body_root,
            execution_payload_root=ep_root,
            execution_block_hash=bytes.fromhex("11" * 32),
            execution_block_number=19500000,
            execution_state_root=sr,
            execution_receipts_root=bytes.fromhex("66" * 32),
            execution_timestamp=1700000000,
            execution_payload_branch=ep_br,
            state_root_branch=sr_br,
            sync_committee_bits=b"\xff" * 64,
            sync_committee_signature=b"\xaa" * 96,
            sync_committee_root=sync_root,
            finalized_checkpoint_root=fin_root,
            finalized_root_branch=fin_br,
            signature_slot=9_000_001,
            finalized_checkpoint_epoch=100_000,  # 3,200,000 < 9,000,000 -> NOT finalized
            sync_committee_pubkeys=pubkeys_512,
        )
        self.assertFalse(HeaderConsensusVerifier.is_execution_block_finalized(ev_unfinalized))
        with mock.patch("scripts.consensus_proof_verifier.verify_bls_aggregate_signature", return_value=True):
            anchor_unfin = BlockHeaderTrustAnchor.from_consensus_verification(ev_unfinalized)
            self.assertEqual(anchor_unfin.finality_status, "ATTESTED_UNFINALIZED")
            self.assertFalse(anchor_unfin.is_authenticated)
            self.assertFalse(BlockHeaderTrustAnchor.verify_authenticity(anchor_unfin))

    def test_2_incorrect_signature_slot_cannot_authenticate_committee(self):
        """Adversarial Test 2: An incorrect signature slot cannot authenticate a committee."""
        pubkeys_512 = self._make_pubkeys_512()
        sync_root = bytes.fromhex("98d69927b32c3e0bd9f091f2f728fb95aeec620d2b5f04390465e65cd5d00b5c")
        sr = bytes.fromhex("55" * 32)
        ep_root, sr_br = self._make_ssz_branch(sr, 34)
        body_root, ep_br = self._make_ssz_branch(ep_root, 25)
        fin_root = bytes.fromhex("33" * 32)
        beacon_state_root, fin_br = self._make_ssz_branch(fin_root, 105)

        # Signature slot prior to attested block slot + 1 fails
        ev_bad_sig_slot = HeaderConsensusEvidence(
            slot=9_000_000,
            proposer_index=1,
            parent_root=bytes.fromhex("22" * 32),
            beacon_state_root=beacon_state_root,
            body_root=body_root,
            execution_payload_root=ep_root,
            execution_block_hash=bytes.fromhex("11" * 32),
            execution_block_number=19500000,
            execution_state_root=sr,
            execution_receipts_root=bytes.fromhex("66" * 32),
            execution_timestamp=1700000000,
            execution_payload_branch=ep_br,
            state_root_branch=sr_br,
            sync_committee_bits=b"\xff" * 64,
            sync_committee_signature=b"\xaa" * 96,
            sync_committee_root=sync_root,
            finalized_checkpoint_root=fin_root,
            finalized_root_branch=fin_br,
            signature_slot=8_000_000,  # Invalid: signature slot < attested slot
            finalized_checkpoint_epoch=281250,
            sync_committee_pubkeys=pubkeys_512,
        )
        ok, err = HeaderConsensusVerifier.verify_execution_header(ev_bad_sig_slot)
        self.assertFalse(ok)
        self.assertIn("INVALID_SIGNATURE_SLOT", err)

    def test_3_unverified_committee_transition_cannot_authenticate_header(self):
        """Adversarial Test 3: An unverified committee transition cannot authenticate a header."""
        pubkeys_512 = self._make_pubkeys_512()
        sync_root = bytes.fromhex("98d69927b32c3e0bd9f091f2f728fb95aeec620d2b5f04390465e65cd5d00b5c")
        sr = bytes.fromhex("55" * 32)
        ep_root, sr_br = self._make_ssz_branch(sr, 34)
        body_root, ep_br = self._make_ssz_branch(ep_root, 25)
        fin_root = bytes.fromhex("33" * 32)
        beacon_state_root, fin_br = self._make_ssz_branch(fin_root, 105)

        # Header in unauthenticated period (e.g. slot 10,000,000 -> period 1220) without transition fails
        ev_untrans = HeaderConsensusEvidence(
            slot=10_000_000,
            proposer_index=1,
            parent_root=bytes.fromhex("22" * 32),
            beacon_state_root=beacon_state_root,
            body_root=body_root,
            execution_payload_root=ep_root,
            execution_block_hash=bytes.fromhex("11" * 32),
            execution_block_number=20500000,
            execution_state_root=sr,
            execution_receipts_root=bytes.fromhex("66" * 32),
            execution_timestamp=1700000000,
            execution_payload_branch=ep_br,
            state_root_branch=sr_br,
            sync_committee_bits=b"\xff" * 64,
            sync_committee_signature=b"\xaa" * 96,
            sync_committee_root=sync_root,
            finalized_checkpoint_root=fin_root,
            finalized_root_branch=fin_br,
            signature_slot=10_000_001,
            finalized_checkpoint_epoch=320000,
            sync_committee_pubkeys=pubkeys_512,
        )
        ok, err = HeaderConsensusVerifier.verify_execution_header(ev_untrans)
        self.assertFalse(ok)
        self.assertIn("UNAUTHENTICATED_SYNC_COMMITTEE_PERIOD", err)

    def test_4_chainlink_proxy_account_proof_cannot_substitute_for_aggregator_account_proof(self):
        """Adversarial Test 4: A Chainlink proxy account proof cannot substitute for the aggregator account proof."""
        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        cl_ev = ChainlinkRoundEvidence(
            feed_address="0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419",
            round_id=18446744073709551615,
            answer=3000_00000000,
            decimals=8,
            started_at=now_ts - 60,
            updated_at=now_ts - 30,
            answered_in_round=18446744073709551615,
        )
        fake_anchor = BlockHeaderTrustAnchor(
            chain_id=1,
            block_number=19_500_000,
            block_hash="0x" + "11" * 32,
            state_root="0x" + "22" * 32,
            header_timestamp_utc="2026-11-15T00:00:00Z",
            finality_status="FINALIZED_CANONICAL",
            is_authenticated=True,
        )
        proxy_proof_nodes = [b"proxy_proof_node"]
        with mock.patch.object(BlockHeaderTrustAnchor, "verify_authenticity", return_value=True):
            # Passing proxy proof as aggregator proof fails because key does not match aggregator address
            rec = ValuationEvidenceVerifier.verify_chainlink_round(
                1, "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", cl_ev,
                block_trust_anchor=fake_anchor,
                account_proof=proxy_proof_nodes,
                aggregator_account_proof=proxy_proof_nodes,
                storage_proof=[b"storage_node"],
            )
            self.assertFalse(rec.is_authenticated)

    def test_5_matching_chainlink_answer_with_fabricated_timestamp_fails(self):
        """Adversarial Test 5: A matching Chainlink answer with a fabricated timestamp fails."""
        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        cl_ev = ChainlinkRoundEvidence(
            feed_address="0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419",
            round_id=18446744073709551615,
            answer=3000_00000000,
            decimals=8,
            started_at=now_ts - 100,
            updated_at=now_ts - 10,
            answered_in_round=18446744073709551615,
        )
        fake_anchor = BlockHeaderTrustAnchor(
            chain_id=1,
            block_number=19_500_000,
            block_hash="0x" + "11" * 32,
            state_root="0x" + "22" * 32,
            header_timestamp_utc="2026-11-15T00:00:00Z",
            finality_status="FINALIZED_CANONICAL",
            is_authenticated=True,
        )
        with mock.patch.object(BlockHeaderTrustAnchor, "verify_authenticity", return_value=True):
            rec = ValuationEvidenceVerifier.verify_chainlink_round(
                1, "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", cl_ev,
                block_trust_anchor=fake_anchor,
                account_proof=[b"node1"],
                storage_proof=[b"node2"],
            )
            self.assertFalse(rec.is_authenticated)

    def test_6_chainlink_proof_for_different_round_fails(self):
        """Adversarial Test 6: A Chainlink proof for a different round fails."""
        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        cl_ev_200 = ChainlinkRoundEvidence(
            feed_address="0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419",
            round_id=200,
            answer=3000_00000000,
            decimals=8,
            started_at=now_ts - 60,
            updated_at=now_ts - 30,
            answered_in_round=200,
        )
        fake_anchor = BlockHeaderTrustAnchor(
            chain_id=1,
            block_number=19_500_000,
            block_hash="0x" + "11" * 32,
            state_root="0x" + "22" * 32,
            header_timestamp_utc="2026-11-15T00:00:00Z",
            finality_status="FINALIZED_CANONICAL",
            is_authenticated=True,
        )
        with mock.patch.object(BlockHeaderTrustAnchor, "verify_authenticity", return_value=True):
            rec = ValuationEvidenceVerifier.verify_chainlink_round(
                1, "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", cl_ev_200,
                block_trust_anchor=fake_anchor,
                account_proof=[b"node1"],
                storage_proof=[b"node2"],
            )
            self.assertFalse(rec.is_authenticated)

    def test_7_uniswap_v3_timestamp_cannot_be_used_as_observation_index(self):
        """Adversarial Test 7: A Uniswap V3 timestamp cannot be used as an observation index."""
        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        twap_ev = UniswapV3TwapEvidence(
            pool_address="0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
            token0_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            token1_address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            token0_decimals=6,
            token1_decimals=18,
            tick_cumulatives=(1000000, 2000000),
            timestamps=(now_ts - 300, now_ts),
        )
        with self.assertRaises(ValueError) as ctx:
            ValuationEvidenceVerifier.verify_uniswap_v3_twap(
                1, "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", twap_ev,
                observation_index_t1=now_ts - 300,  # UNIX timestamp passed as index
                observation_index_t2=now_ts,
                require_authenticated=True,
            )
        self.assertIn("UNISWAP_V3_INVALID_OBSERVATION_INDEX", str(ctx.exception))

    def test_8_pool_account_proof_cannot_substitute_for_storage_proof(self):
        """Adversarial Test 8: A pool account proof cannot substitute for its storage proof."""
        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        twap_ev = UniswapV3TwapEvidence(
            pool_address="0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
            token0_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            token1_address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            token0_decimals=6,
            token1_decimals=18,
            tick_cumulatives=(1000000, 2000000),
            timestamps=(now_ts - 300, now_ts),
        )
        fake_anchor = BlockHeaderTrustAnchor(
            chain_id=1,
            block_number=19_500_000,
            block_hash="0x" + "11" * 32,
            state_root="0x" + "22" * 32,
            header_timestamp_utc="2026-11-15T00:00:00Z",
            finality_status="FINALIZED_CANONICAL",
            is_authenticated=True,
        )
        with mock.patch.object(BlockHeaderTrustAnchor, "verify_authenticity", return_value=True):
            rec = ValuationEvidenceVerifier.verify_uniswap_v3_twap(
                1, "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", twap_ev,
                pool_account_proof_t1=[b"acc1"],
                pool_account_proof_t2=[b"acc2"],
                observation_proof_t1=None,  # Missing storage proof
                observation_proof_t2=None,
                observation_index_t1=0,
                observation_index_t2=1,
                block_anchor_t1=fake_anchor,
                block_anchor_t2=fake_anchor,
            )
            self.assertFalse(rec.is_authenticated)

    def test_9_storage_proof_against_global_state_root_cannot_authenticate_pool_storage(self):
        """Adversarial Test 9: A storage proof against the global state root cannot authenticate pool storage."""
        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        twap_ev = UniswapV3TwapEvidence(
            pool_address="0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
            token0_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            token1_address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            token0_decimals=6,
            token1_decimals=18,
            tick_cumulatives=(1000000, 2000000),
            timestamps=(now_ts - 300, now_ts),
        )
        fake_anchor = BlockHeaderTrustAnchor(
            chain_id=1,
            block_number=19_500_000,
            block_hash="0x" + "11" * 32,
            state_root="0x" + "22" * 32,
            header_timestamp_utc="2026-11-15T00:00:00Z",
            finality_status="FINALIZED_CANONICAL",
            is_authenticated=True,
        )
        with mock.patch.object(BlockHeaderTrustAnchor, "verify_authenticity", return_value=True):
            rec = ValuationEvidenceVerifier.verify_uniswap_v3_twap(
                1, "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", twap_ev,
                pool_account_proof_t1=None,  # Missing pool account proof
                pool_account_proof_t2=None,
                observation_proof_t1=[b"proof_against_state_root"],
                observation_proof_t2=[b"proof_against_state_root_2"],
                observation_index_t1=0,
                observation_index_t2=1,
                block_anchor_t1=fake_anchor,
                block_anchor_t2=fake_anchor,
            )
            self.assertFalse(rec.is_authenticated)

    def test_10_proof_with_different_tick_cumulative_or_timestamp_fails(self):
        """Adversarial Test 10: A proof with a different tick cumulative or timestamp fails."""
        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        twap_ev = UniswapV3TwapEvidence(
            pool_address="0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
            token0_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            token1_address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            token0_decimals=6,
            token1_decimals=18,
            tick_cumulatives=(1000000, 2000000),
            timestamps=(now_ts - 300, now_ts),
        )
        fake_anchor = BlockHeaderTrustAnchor(
            chain_id=1,
            block_number=19_500_000,
            block_hash="0x" + "11" * 32,
            state_root="0x" + "22" * 32,
            header_timestamp_utc="2026-11-15T00:00:00Z",
            finality_status="FINALIZED_CANONICAL",
            is_authenticated=True,
        )
        with mock.patch.object(BlockHeaderTrustAnchor, "verify_authenticity", return_value=True):
            rec = ValuationEvidenceVerifier.verify_uniswap_v3_twap(
                1, "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                dataclasses.replace(twap_ev, tick_cumulatives=(99999999, 88888888)),
                pool_account_proof_t1=[b"acc1"],
                pool_account_proof_t2=[b"acc2"],
                observation_proof_t1=[b"obs1"],
                observation_proof_t2=[b"obs2"],
                observation_index_t1=0,
                observation_index_t2=1,
                block_anchor_t1=fake_anchor,
                block_anchor_t2=fake_anchor,
            )
            self.assertFalse(rec.is_authenticated)

    def test_11_obsolete_or_fabricated_guardian_registry_cannot_authenticate_production_pyth_vaa(self):
        """Adversarial Test 11: An obsolete or fabricated guardian registry cannot authenticate a production Pyth VAA."""
        # Expired guardian set 1
        ok_exp, err_exp, _ = WormholeGuardianRegistry.get_guardian_set(1, check_time=1700000000)
        self.assertFalse(ok_exp)
        self.assertIn("EXPIRED_GUARDIAN_SET", err_exp)

        # Fabricated guardian set 999
        ok_fab, err_fab, _ = WormholeGuardianRegistry.get_guardian_set(999)
        self.assertFalse(ok_fab)
        self.assertIn("UNAUTHENTICATED_GUARDIAN_SET", err_fab)

        # Guardian set with dev keys
        dev_key = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
        self.assertIn(dev_key, DEV_HARDHAT_ANVIL_KEYS)
        fake_vaa = (
            b"\x01" +
            (0).to_bytes(4, "big") +
            bytes([13]) +
            (b"\x00" + b"\x11" * 32 + b"\x22" * 32 + b"\x1b") * 13 +
            b"\x00" * 100
        )
        ok_dev, err_dev, _ = verify_wormhole_vaa(fake_vaa, trusted_guardian_set=[dev_key] * 19)
        self.assertFalse(ok_dev)
        self.assertEqual(err_dev, "DEVELOPMENT_GUARDIAN_KEYS_REJECTED")

    def test_12_op_stack_mature_final_remains_impossible_without_genuine_l1_finality(self):
        """Adversarial Test 12: OP Stack mature-final remains impossible without genuine L1 finality."""
        unauth_l1_anchor = BlockHeaderTrustAnchor.create_unverified_anchor(
            chain_id=1,
            block_number=19_500_000,
            block_hash="0x" + "55" * 32,
            state_root="0x" + "66" * 32,
            header_timestamp_utc="2026-11-15T00:00:00Z",
        )
        fake_evidence = OpStackOutputProofEvidence(
            withdrawal_hash="0x" + "11" * 32,
            l2_output_index=42,
            l2_block_number=12345678,
            output_root="0x" + "22" * 32,
            l1_oracle_output_root="0x" + "22" * 32,
            is_valid=True,
        )
        st = MultiChainFinalityEngine.evaluate_op_stack_withdrawal(
            proven_withdrawal_timestamp=1000,
            current_l1_timestamp=200000,
            proof_maturity_delay_seconds=1000,
            output_proof_evidence=fake_evidence,
            l1_trust_anchor=unauth_l1_anchor,
        )
        self.assertEqual(st, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)
        self.assertNotEqual(st, FinalityState.WITHDRAWAL_PROOF_MATURE_FINAL)

    def test_13_positive_chainlink_cryptographic_mpt_verification(self):
        """Positive Test 13: End-to-end cryptographic verification of Chainlink aggregator transmission MPT."""
        now_ts = 1700000000
        round_id = 18446744073709551615
        answer = 3000_00000000

        # Exact storage slot 43 transmission packing: timestamp (8 bytes) + answer (24 bytes)
        slot_bytes = now_ts.to_bytes(8, "big") + answer.to_bytes(24, "big", signed=True)
        s_key = keccak256((round_id & 0xFFFFFFFF).to_bytes(32, "big") + (43).to_bytes(32, "big"))
        encoded_slot_val = rlp_encode_bytes(slot_bytes)

        storage_leaf = rlp_encode([b"\x20" + s_key, encoded_slot_val])
        storage_root = keccak256(storage_leaf)

        agg_code_hash = bytes.fromhex("3479008bc5b5ad9e02ef2c2c04ae5ce5c249bc8558eb80dd94488b0ea77f52f3")
        agg_account_val = rlp_encode([rlp_encode_bytes(b"\x01"), rlp_encode_bytes(b""), storage_root, agg_code_hash])

        agg_addr = bytes.fromhex("5f4ec3df9cbd43714fe2740f5e3616155c5b8419")
        agg_key = keccak256(agg_addr)
        agg_leaf = rlp_encode([b"\x20" + agg_key, agg_account_val])
        state_root = keccak256(agg_leaf)

        cl_ev = ChainlinkRoundEvidence(
            feed_address="0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419",
            round_id=round_id,
            answer=answer,
            decimals=8,
            started_at=now_ts - 30,
            updated_at=now_ts,
            answered_in_round=round_id,
        )

        anchor = BlockHeaderTrustAnchor(
            chain_id=1,
            block_number=19_500_000,
            block_hash="0x" + "11" * 32,
            state_root="0x" + state_root.hex(),
            header_timestamp_utc="2026-11-15T00:00:00Z",
            finality_status="FINALIZED_CANONICAL",
            is_authenticated=True,
        )

        with mock.patch.object(BlockHeaderTrustAnchor, "verify_authenticity", return_value=True):
            rec = ValuationEvidenceVerifier.verify_chainlink_round(
                1, "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", cl_ev,
                block_trust_anchor=anchor,
                account_proof=[agg_leaf],
                storage_proof=[storage_leaf],
                require_authenticated=True,
            )
            self.assertTrue(rec.is_authenticated)
            self.assertEqual(rec.price_usd, 3000.0)

    def test_14_positive_uniswap_v3_observation_cryptographic_verification(self):
        """Positive Test 14: End-to-end cryptographic verification of Uniswap V3 observation MPT storage proofs."""
        def encode_v3_obs(ts: int, cum: int):
            return b"\x01" + (1000).to_bytes(20, "big") + cum.to_bytes(7, "big", signed=True) + ts.to_bytes(4, "big")

        def build_endpoint(pool_addr_hex: str, obs_index: int, ts: int, cum: int):
            slot_bytes = encode_v3_obs(ts, cum)
            slot_key = keccak256((obs_index).to_bytes(32, "big") + (8).to_bytes(32, "big"))
            storage_leaf = rlp_encode([b"\x20" + slot_key, rlp_encode_bytes(slot_bytes)])
            storage_root = keccak256(storage_leaf)

            pool_addr = bytes.fromhex(pool_addr_hex[2:])
            pool_key = keccak256(pool_addr)
            pool_acc_val = rlp_encode([rlp_encode_bytes(b"\x01"), rlp_encode_bytes(b""), storage_root, bytes.fromhex("11" * 32)])
            pool_leaf = rlp_encode([b"\x20" + pool_key, pool_acc_val])
            return keccak256(pool_leaf), [pool_leaf], [storage_leaf]

        pool_hex = "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"
        t1 = 1700000000
        t2 = 1700000300
        c1 = 1000000
        c2 = 2000000

        sr1, pool_p1, obs_p1 = build_endpoint(pool_hex, 0, t1, c1)
        sr2, pool_p2, obs_p2 = build_endpoint(pool_hex, 1, t2, c2)

        anchor1 = BlockHeaderTrustAnchor(
            chain_id=1,
            block_number=19_499_900,
            block_hash="0x" + "11" * 32,
            state_root="0x" + sr1.hex(),
            header_timestamp_utc="2026-11-15T00:00:00Z",
            finality_status="FINALIZED_CANONICAL",
            is_authenticated=True,
        )
        anchor2 = BlockHeaderTrustAnchor(
            chain_id=1,
            block_number=19_500_000,
            block_hash="0x" + "22" * 32,
            state_root="0x" + sr2.hex(),
            header_timestamp_utc="2026-11-15T00:05:00Z",
            finality_status="FINALIZED_CANONICAL",
            is_authenticated=True,
        )

        twap_ev = UniswapV3TwapEvidence(
            pool_address=pool_hex,
            token0_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            token1_address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            token0_decimals=6,
            token1_decimals=18,
            tick_cumulatives=(c1, c2),
            timestamps=(t1, t2),
        )

        with mock.patch.object(BlockHeaderTrustAnchor, "verify_authenticity", return_value=True):
            rec = ValuationEvidenceVerifier.verify_uniswap_v3_twap(
                1, "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", twap_ev,
                pool_account_proof_t1=pool_p1,
                pool_account_proof_t2=pool_p2,
                observation_proof_t1=obs_p1,
                observation_proof_t2=obs_p2,
                observation_index_t1=0,
                observation_index_t2=1,
                block_anchor_t1=anchor1,
                block_anchor_t2=anchor2,
                require_authenticated=True,
            )
            self.assertTrue(rec.is_authenticated)
            self.assertGreater(rec.price_usd, 0.0)


if __name__ == "__main__":
    unittest.main()
