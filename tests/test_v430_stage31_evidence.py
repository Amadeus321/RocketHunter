#!/usr/bin/env python3
"""Stage 3.1: Comprehensive Unit & Adversarial Test Suite for Rocket Hunter v4.30.

Exercises and validates all Stage 3.1 acceptance criteria with rigorous adversarial tests:
1. Canonical Uniswap V3 TickMath formula, boundary conditions, and tick-204520 fixture.
2. Protected data integrity verifier (manifest snapshot, deletions, unauthorized creations, fail-closed).
3. Non-tautological RPC evidence verification (payload integrity with expected digests, bound TLS auth,
   fail-closed Tier 3 canonical quorum, reorg quarantine).
4. Multi-chain configuration-driven finality (Ethereum L1, Arbitrum dynamic reader, OP Stack op-contracts/v1.6.0, Solana).
5. Venue-specific execution evidence structures (V3 tick spacing, bitmap coverage, initialized tick validation, V2 fee bounds).
6. Dataset A trade journal reconciliation (strict rejection of tokens_sold > tokens_acquired, identity matching, non-finite checks).
7. Cameron-Gelbach-Miller two-way clustered variance hand-calculated fixture (2x3 panel).
8. Exact Student's t critical values for actual degrees of freedom (df = min(M - 1, B - 1)).
9. Newey-West (1987) time-series fallback implementation on dependent blocks with stationary AR(1) positive control.
10. Negative-control test across repeated independent panels verifying false alarm rate <= 10%.
11. Mandatory sample hurdles (M >= 50, B >= 30, N >= 500) before any confirmatory status.
"""
from __future__ import annotations

import decimal
import math
import random
import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts.research_evidence import (
    APPROVED_BASELINE_COMMIT,
    CANONICAL_OP_COMMIT,
    CANONICAL_OP_TAG,
    TECHNICAL_TP_PCT,
    UNISWAP_V3_FEE_SPACINGS,
    ClusteredEvaluationEngine,
    DatasetAReconciliationEngine,
    DualTrackOutcomeEvaluator,
    EvidenceTier,
    FinalityState,
    InferentialStatus,
    JournalEntryPosition,
    JournalExitExecution,
    ModeledExecutionStatus,
    MultiChainFinalityEngine,
    ProtectedDataVerifier,
    RpcEvidenceRecord,
    RpcProvenanceVerifier,
    UniswapV2Evidence,
    UniswapV3Evidence,
    UniswapV3TickMath,
)


class TestStage31TickMathAndBoundaries(unittest.TestCase):
    """Test canonical Uniswap V3 TickMath implementation against canonical protocol specs."""

    def test_canonical_formula_and_fixture_tick_204520(self):
        """Validates canonical integer sqrtPriceX96 for tick 204520 equals 2186517289083564169714246582247698."""
        tick = 204520
        # 1. Canonical bitwise integer implementation
        val_int = UniswapV3TickMath.get_sqrt_ratio_at_tick(tick)
        self.assertEqual(val_int, 2186517289083564169714246582247698)

        # 2. High-precision Decimal(100) reference ceil(sqrt(1.0001^t) * 2^96)
        val_ceil = UniswapV3TickMath.compute_exact_ceil_scaled(tick)
        self.assertEqual(val_ceil, 2186517289083564169714246582247698)
        self.assertEqual(val_int, val_ceil)

    def test_boundary_inversion_and_adjacent_ticks(self):
        """Validates tick recovery getTickAtSqrtRatio at exact boundaries and immediately adjacent integers."""
        target_tick = 204520
        sqrt_p = UniswapV3TickMath.get_sqrt_ratio_at_tick(target_tick)

        # Exact boundary returns target tick
        self.assertEqual(UniswapV3TickMath.get_tick_at_sqrt_ratio(sqrt_p), target_tick)

        # Immediately adjacent below returns target_tick - 1
        self.assertEqual(UniswapV3TickMath.get_tick_at_sqrt_ratio(sqrt_p - 1), target_tick - 1)

        # Target tick 204521
        sqrt_p_plus_1 = UniswapV3TickMath.get_sqrt_ratio_at_tick(target_tick + 1)
        self.assertEqual(UniswapV3TickMath.get_tick_at_sqrt_ratio(sqrt_p_plus_1), target_tick + 1)
        self.assertEqual(UniswapV3TickMath.get_tick_at_sqrt_ratio(sqrt_p_plus_1 - 1), target_tick)

    def test_protocol_global_limits(self):
        """Validates MIN_TICK and MAX_TICK protocol bounds."""
        min_sqrt = UniswapV3TickMath.MIN_SQRT_RATIO
        max_sqrt = UniswapV3TickMath.MAX_SQRT_RATIO

        self.assertEqual(min_sqrt, 4295128739)
        self.assertEqual(UniswapV3TickMath.get_tick_at_sqrt_ratio(min_sqrt), -887272)

        with self.assertRaises(ValueError):
            UniswapV3TickMath.get_tick_at_sqrt_ratio(min_sqrt - 1)

        with self.assertRaises(ValueError):
            UniswapV3TickMath.get_tick_at_sqrt_ratio(max_sqrt)


class TestStage31ProtectedDataAndManifest(unittest.TestCase):
    """Test reproducibility manifest and protected data integrity verification with creation/deletion detection."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.data_dir = self.temp_dir / "rocket-hunter-data"
        self.data_dir.mkdir(parents=True)
        self.config_dir = self.temp_dir / "config"
        self.config_dir.mkdir(parents=True)
        self.memory_dir = self.data_dir / "scanner-memory"
        self.memory_dir.mkdir(parents=True)
        self.verifier = ProtectedDataVerifier(root_dir=self.temp_dir)

    def test_clean_state_equivalence(self):
        """Validates that identical pre-run and post-run snapshots pass clean verification."""
        journal = self.data_dir / "user_positions_journal.json"
        journal.write_text('{"positions": []}', encoding="utf-8")
        notif = self.data_dir / "notification_config.json"
        notif.write_text('{"enabled": false}', encoding="utf-8")
        cfg = self.config_dir / "production_weights.json"
        cfg.write_text('{"weights": {"momentum": 0.4}}', encoding="utf-8")
        mem = self.memory_dir / "active.json"
        mem.write_text('{"candidates": []}', encoding="utf-8")

        pre = self.verifier.snapshot_protected_data()
        post = self.verifier.snapshot_protected_data()

        is_clean, disc = self.verifier.verify_clean_equivalence(pre, post)
        self.assertTrue(is_clean)
        self.assertEqual(len(disc), 0)

    def test_tamper_detection_fails_closed_on_modification(self):
        """Validates that any unexpected alteration of protected files fails closed."""
        journal = self.data_dir / "user_positions_journal.json"
        journal.write_text('{"positions": []}', encoding="utf-8")
        pre = self.verifier.snapshot_protected_data()

        # Simulate unauthorized modification
        journal.write_text('{"positions": [{"tampered": true}]}', encoding="utf-8")
        post = self.verifier.snapshot_protected_data()

        is_clean, disc = self.verifier.verify_clean_equivalence(pre, post)
        self.assertFalse(is_clean)
        self.assertTrue(any("user_positions_journal.json" in d for d in disc))

    def test_tamper_detection_fails_closed_on_deletion(self):
        """Validates that deletion of a protected file fails closed."""
        mem = self.memory_dir / "ledger.json"
        mem.write_text('{"data": 1}', encoding="utf-8")
        pre = self.verifier.snapshot_protected_data()

        mem.unlink()
        post = self.verifier.snapshot_protected_data()

        is_clean, disc = self.verifier.verify_clean_equivalence(pre, post)
        self.assertFalse(is_clean)
        self.assertTrue(any("deleted" in d for d in disc))

    def test_tamper_detection_fails_closed_on_unauthorized_creation(self):
        """Validates that unauthorized new file creation in protected areas fails closed."""
        pre = self.verifier.snapshot_protected_data()

        new_file = self.memory_dir / "rogue_partition.json"
        new_file.write_text('{"unauthorized": true}', encoding="utf-8")
        post = self.verifier.snapshot_protected_data()

        is_clean, disc = self.verifier.verify_clean_equivalence(pre, post)
        self.assertFalse(is_clean)
        self.assertTrue(any("Unauthorized new file created" in d for d in disc))


class TestStage31RpcProvenanceAndQuarantine(unittest.TestCase):
    """Test three-tiered non-tautological RPC evidence, bound TLS auth, and fail-closed quorum."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.verifier = RpcProvenanceVerifier(data_dir=self.temp_dir)

    def _make_record(
        self,
        block_hash: str = "0x1234",
        parent_hash: str = "0x0000",
        state_root: str = "0xabcd",
        provider_id: str = "alchemy_eth_mainnet",
        provider_domain: str = "eth-mainnet.alchemy.com",
        chain_id: int = 1,
        block_number: int = 19450000,
        tls_verified: bool = True,
        cert_fp: str = "4E19C8A7B5F3E2D1C0B9A8F7E6D5C4B3A291807F6E5D4C3B2A109F8E7D6C5B4A",
        raw_request_bytes: bytes = b'{"method": "eth_call"}',
        raw_response_bytes: bytes = b'{"result": "0x0001"}',
        expected_request_sha256: str | None = None,
        expected_response_sha256: str | None = None,
    ) -> RpcEvidenceRecord:
        import hashlib
        req_hash = expected_request_sha256 or hashlib.sha256(raw_request_bytes).hexdigest().upper()
        resp_hash = expected_response_sha256 or hashlib.sha256(raw_response_bytes).hexdigest().upper()
        return RpcEvidenceRecord(
            rpc_method="eth_call",
            rpc_params=[{"to": "0xpool"}, "latest"],
            provider_id=provider_id,
            provider_domain=provider_domain,
            retrieval_timestamp_utc="2026-03-01T12:00:00Z",
            raw_request_bytes=raw_request_bytes,
            raw_response_bytes=raw_response_bytes,
            expected_request_sha256=req_hash,
            expected_response_sha256=resp_hash,
            chain_id=chain_id,
            block_number=block_number,
            block_hash=block_hash,
            parent_hash=parent_hash,
            state_root=state_root,
            tls_handshake_verified=tls_verified,
            tls_cert_fingerprint_sha256=cert_fp,
            tls_cipher_suite="TLS_AES_256_GCM_SHA384",
        )

    def test_non_tautological_tier1_payload_verification(self):
        """Validates payload integrity against independent expected digests and disk retention."""
        rec = self._make_record()
        self.assertTrue(self.verifier.verify_tier1_payload_integrity(rec))

        # Adversarial: payload altered after expected digest was recorded fails
        bad_rec = self._make_record(
            raw_response_bytes=b'{"result": "0xTAMPERED"}',
            expected_response_sha256="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )
        self.assertFalse(self.verifier.verify_tier1_payload_integrity(bad_rec))

    def test_tier2_provider_binding_and_tls_authentication(self):
        """Validates provider-to-domain/chain binding and cryptographic TLS verification."""
        rec = self._make_record()
        passed, msg = self.verifier.verify_tier2_endpoint_authentication(rec)
        self.assertTrue(passed)
        self.assertEqual(msg, "ENDPOINT_TLS_AUTHENTICATED")

        # Adversarial: Unregistered provider fails
        unregistered = self._make_record(provider_id="rogue_node")
        passed, _ = self.verifier.verify_tier2_endpoint_authentication(unregistered)
        self.assertFalse(passed)

        # Adversarial: Domain mismatch against registered provider fails
        domain_spoof = self._make_record(provider_domain="fake-alchemy.com")
        passed, _ = self.verifier.verify_tier2_endpoint_authentication(domain_spoof)
        self.assertFalse(passed)

        # Adversarial: Unverified TLS handshake fails
        unverified_tls = self._make_record(tls_verified=False)
        passed, _ = self.verifier.verify_tier2_endpoint_authentication(unverified_tls)
        self.assertFalse(passed)

        # Adversarial: Pinned certificate fingerprint mismatch fails
        bad_cert = self._make_record(cert_fp="FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF")
        passed, _ = self.verifier.verify_tier2_endpoint_authentication(bad_cert)
        self.assertFalse(passed)

    def test_tier3_fail_closed_canonical_quorum(self):
        """Validates that Tier 3 fails closed unless independent peer and checkpoint evidence match."""
        rec1 = self._make_record(block_hash="0x1111", state_root="0xaaaa")

        # 1. Missing peer evidence fails closed to FINALITY_PENDING
        passed, state, msg = self.verifier.verify_tier3_canonical_quorum(rec1, independent_peer_record=None, finalized_beacon_block=19450000)
        self.assertFalse(passed)
        self.assertEqual(state, FinalityState.FINALITY_PENDING)
        self.assertEqual(msg, "INDEPENDENT_PEER_QUORUM_MISSING")

        # 2. Missing finalized checkpoint evidence fails closed to FINALITY_PENDING
        rec2_peer = self._make_record(block_hash="0x1111", state_root="0xaaaa")
        passed, state, msg = self.verifier.verify_tier3_canonical_quorum(rec1, independent_peer_record=rec2_peer, finalized_beacon_block=None)
        self.assertFalse(passed)
        self.assertEqual(state, FinalityState.FINALITY_PENDING)
        self.assertEqual(msg, "FINALIZED_CHECKPOINT_EVIDENCE_MISSING")

        # 3. Block number exceeds finalized checkpoint height
        passed, state, msg = self.verifier.verify_tier3_canonical_quorum(rec1, independent_peer_record=rec2_peer, finalized_beacon_block=19440000)
        self.assertFalse(passed)
        self.assertEqual(state, FinalityState.FINALITY_PENDING)

        # 4. Chain ID mismatch across peers fails closed
        rec_diff_chain = self._make_record(chain_id=42161)
        passed, state, msg = self.verifier.verify_tier3_canonical_quorum(rec1, independent_peer_record=rec_diff_chain, finalized_beacon_block=19450000)
        self.assertFalse(passed)
        self.assertEqual(state, FinalityState.FINALITY_PENDING)

        # 5. Block hash divergence quarantines reorg and returns BLOCK_REORGANIZED_ORPHAN
        rec_divergent = self._make_record(block_hash="0x9999", state_root="0xbbbb")
        passed, state, msg = self.verifier.verify_tier3_canonical_quorum(rec1, independent_peer_record=rec_divergent, finalized_beacon_block=19450000)
        self.assertFalse(passed)
        self.assertEqual(state, FinalityState.BLOCK_REORGANIZED_ORPHAN)
        self.assertTrue((self.verifier.quarantine_dir / "1" / "19450000" / "quarantine_manifest.json").is_file())

        # 6. Both match and within finalized height => canonical
        passed, state, msg = self.verifier.verify_tier3_canonical_quorum(rec1, independent_peer_record=rec2_peer, finalized_beacon_block=19450000)
        self.assertTrue(passed)
        self.assertEqual(state, FinalityState.FINALIZED_CANONICAL)


class TestStage31MultiChainFinality(unittest.TestCase):
    """Test configuration-driven finality state machine across EVM and non-EVM chains."""

    def test_arbitrum_dynamic_confirm_period(self):
        """Validates dynamic evaluation of confirmPeriodBlocks() with standard and non-standard periods."""
        status_orbit = MultiChainFinalityEngine.evaluate_arbitrum(
            l1_batch_finalized=True,
            assertion_created_l1_block=19000000,
            current_finalized_l1_block=19008000,  # 8,000 blocks elapsed > 7,200
            confirm_period_blocks=7200,
            challenge_status="NONE",
        )
        self.assertEqual(status_orbit, FinalityState.FINALIZED_CANONICAL)

        status_mainnet = MultiChainFinalityEngine.evaluate_arbitrum(
            l1_batch_finalized=True,
            assertion_created_l1_block=19000000,
            current_finalized_l1_block=19008000,  # 8,000 blocks elapsed < 45,818
            confirm_period_blocks=45818,
            challenge_status="NONE",
        )
        self.assertEqual(status_mainnet, FinalityState.L1_BATCH_FINALITY_ONLY)

    def test_op_stack_contracts_v160_delay_transitions(self):
        """Validates exact op-contracts/v1.6.0 state transitions and delay evaluation."""
        self.assertEqual(CANONICAL_OP_COMMIT, "33f06d2d5e4034125df02264a5ffe84571bd0359")
        self.assertEqual(CANONICAL_OP_TAG, "op-contracts/v1.6.0")

        # Challenger win invalidation
        status_challenger = MultiChainFinalityEngine.evaluate_op_stack(
            l1_batch_finalized=True,
            proposal_game_type=1,
            respected_game_type=1,
            game_status=1,  # CHALLENGER_WINS
            resolved_at=1000,
            current_l1_timestamp=99999999,
            dispute_game_finality_delay_seconds=302400,
        )
        self.assertEqual(status_challenger, FinalityState.PROPOSAL_INVALIDATED_QUARANTINE)

        # Defender win in delay
        status_delay = MultiChainFinalityEngine.evaluate_op_stack(
            l1_batch_finalized=True,
            proposal_game_type=1,
            respected_game_type=1,
            game_status=2,  # DEFENDER_WINS
            resolved_at=1000,
            current_l1_timestamp=1000 + 200000,
            dispute_game_finality_delay_seconds=302400,
        )
        self.assertEqual(status_delay, FinalityState.DISPUTE_GAME_RESOLVED_DEFENDER)

        # Defender win finalized
        status_final = MultiChainFinalityEngine.evaluate_op_stack(
            l1_batch_finalized=True,
            proposal_game_type=1,
            respected_game_type=1,
            game_status=2,
            resolved_at=1000,
            current_l1_timestamp=1000 + 302400,
            dispute_game_finality_delay_seconds=302400,
        )
        self.assertEqual(status_final, FinalityState.L2_OUTPUT_FINALIZED_CANONICAL)

    def test_solana_rooted_commitment(self):
        """Validates Solana commitment levels."""
        self.assertEqual(MultiChainFinalityEngine.evaluate_solana("finalized", is_rooted=True), FinalityState.FINALIZED_CANONICAL)
        self.assertEqual(MultiChainFinalityEngine.evaluate_solana("finalized", is_rooted=False), FinalityState.FINALITY_PENDING)


class TestStage31VenueEvidenceAndDualTrackOutcomes(unittest.TestCase):
    """Test venue-specific execution evidence (tick spacing, bitmap coverage) and dual-track outcomes."""

    def test_uniswap_v3_range_validation_complete(self):
        """Validates complete Uniswap V3 evidence with valid fee, spacing, bitmap, and initialized ticks."""
        spacing = UNISWAP_V3_FEE_SPACINGS[3000]  # 60
        t_curr = 204540  # 204540 % 60 == 0
        t_target = 204660  # 204660 % 60 == 0

        # Word index covering compressed ticks 3409..3411
        # 3409 // 256 = 13, bit pos = 81
        w_idx = (t_curr // spacing) >> 8
        bit_pos = (t_curr // spacing) % 256

        v3 = UniswapV3Evidence(
            chain_id=1,
            pool_address="0xpool3",
            token0_address="0xtoken0",
            token1_address="0xtoken1",
            token0_decimals=6,
            token1_decimals=18,
            fee_tier_ppm=3000,
            sqrt_price_x96=2186517289083564169714246582247698,
            tick=t_curr,
            active_liquidity=1000000,
            initialized_ticks={
                t_curr: {"liquidityNet": 500, "liquidityGross": 500},
            },
            tick_bitmap={w_idx: 1 << bit_pos},
            block_number=19000000,
            block_hash="0xhash",
            parent_hash="0xparent",
        )
        self.assertTrue(v3.is_complete_for_range(target_tick=t_target))

    def test_uniswap_v3_adversarial_validation_fails_closed(self):
        """Validates that missing bitmap coverage, misaligned spacing, or invalid fee tiers fail closed."""
        # 1. Invalid fee tier
        v3_bad_fee = UniswapV3Evidence(
            chain_id=1, pool_address="0xp", token0_address="0xt0", token1_address="0xt1",
            token0_decimals=6, token1_decimals=18, fee_tier_ppm=2500,  # Invalid fee tier
            sqrt_price_x96=1000, tick=0, active_liquidity=100, initialized_ticks={},
            tick_bitmap={}, block_number=1, block_hash="0xh", parent_hash="0xp",
        )
        self.assertFalse(v3_bad_fee.is_complete_for_range(target_tick=100))

        # 2. Token0 == Token1 identity failure
        v3_ident = UniswapV3Evidence(
            chain_id=1, pool_address="0xp", token0_address="0xt0", token1_address="0xt0",
            token0_decimals=6, token1_decimals=18, fee_tier_ppm=3000,
            sqrt_price_x96=1000, tick=0, active_liquidity=100, initialized_ticks={},
            tick_bitmap={}, block_number=1, block_hash="0xh", parent_hash="0xp",
        )
        self.assertFalse(v3_ident.is_complete_for_range(target_tick=100))

        # 3. Missing bitmap word covering simulated path
        v3_missing_bitmap = UniswapV3Evidence(
            chain_id=1, pool_address="0xp", token0_address="0xt0", token1_address="0xt1",
            token0_decimals=6, token1_decimals=18, fee_tier_ppm=3000,
            sqrt_price_x96=1000, tick=0, active_liquidity=100,
            initialized_ticks={0: {"liquidityNet": 100, "liquidityGross": 100}},
            tick_bitmap={},  # Missing word 0
            block_number=1, block_hash="0xh", parent_hash="0xp",
        )
        self.assertFalse(v3_missing_bitmap.is_complete_for_range(target_tick=100))

        # 4. Bitmap bit set but tick missing from initialized_ticks dict
        v3_missing_tick = UniswapV3Evidence(
            chain_id=1, pool_address="0xp", token0_address="0xt0", token1_address="0xt1",
            token0_decimals=6, token1_decimals=18, fee_tier_ppm=3000,
            sqrt_price_x96=1000, tick=0, active_liquidity=100,
            initialized_ticks={},  # Empty dict despite bit set!
            tick_bitmap={0: 1},
            block_number=1, block_hash="0xh", parent_hash="0xp",
        )
        self.assertFalse(v3_missing_tick.is_complete_for_range(target_tick=100))

    def test_uniswap_v2_validation_and_fee_bounds(self):
        """Validates Uniswap V2 evidence structure, reserve positivity, and fee bounds."""
        v2 = UniswapV2Evidence(
            chain_id=1, pool_address="0xp2", token0_address="0xt0", token1_address="0xt1",
            token0_decimals=6, token1_decimals=18, raw_reserve0=1000, raw_reserve1=2000,
            fee_bps=30, block_number=1, block_hash="0xh", parent_hash="0xp",
        )
        self.assertTrue(v2.is_complete())

        # Invalid fee bounds (0 or > 10000 bps)
        v2.fee_bps = 0
        self.assertFalse(v2.is_complete())
        v2.fee_bps = 10001
        self.assertFalse(v2.is_complete())

    def test_dual_track_outcome_resolution(self):
        """Validates technical TP attainment evaluates independently from modeled execution."""
        mkt_reached = DualTrackOutcomeEvaluator.evaluate_market_technical_tp(
            entry_price=10.0,
            max_candle_high_24h=14.05,
        )
        self.assertTrue(mkt_reached)

        status, net_return = DualTrackOutcomeEvaluator.evaluate_modeled_execution(
            is_evidence_complete=True,
            gross_return_pct=40.5,
            price_impact_pct=8.0,
            fee_pct=0.6,
            gas_drag_pct=3.5,
        )
        self.assertEqual(status, ModeledExecutionStatus.MODELED_EXECUTION_WITH_ASSUMPTIONS)
        self.assertAlmostEqual(net_return, 28.4, places=2)


class TestStage31DatasetAReconciliation(unittest.TestCase):
    """Test Dataset A trade journal reconciliation with strict volume, identity, and finite checks."""

    def test_pro_rata_cost_basis_for_partial_sale(self):
        """Validates exact pro-rata cost-basis allocation for partial sales (Q_sold <= Q_acquired)."""
        entry = JournalEntryPosition(
            token_id="ethereum:0x1111", chain_id=1, wallet_address="0xUser", tx_hash="0xentrytx",
            quote_expenditure=1000.0, tokens_acquired=10000.0, entry_gas_cost=20.0, entry_venue_fees=3.0,
        )
        exit_exec = JournalExitExecution(
            token_id="ethereum:0x1111", chain_id=1, wallet_address="0xUser", tx_hash="0xexittx",
            tokens_sold=4000.0, gross_proceeds=600.0, exit_gas_cost=15.0, exit_venue_fees=2.0,
        )

        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, is_cryptographically_verified=True)
        self.assertEqual(outcome.status, EvidenceTier.INDEPENDENTLY_VERIFIED_EXECUTION)
        self.assertEqual(outcome.alpha_ratio, 0.40)
        self.assertEqual(outcome.allocated_expenditure, 400.0)
        self.assertAlmostEqual(outcome.realized_net_pl, 173.8, places=2)
        self.assertEqual(outcome.tokens_remaining, 6000.0)
        self.assertTrue(outcome.contributes_to_verified_pl)

    def test_strict_rejection_when_sold_exceeds_acquired(self):
        """Adversarial: Reject tokens_sold > tokens_acquired without clamping to 1.0."""
        entry = JournalEntryPosition(
            token_id="ethereum:0x1111", chain_id=1, wallet_address="0xUser", tx_hash="0xentrytx",
            quote_expenditure=1000.0, tokens_acquired=10000.0, entry_gas_cost=20.0, entry_venue_fees=3.0,
        )
        # Attempting to sell 12,000 tokens when only 10,000 were acquired
        exit_exec = JournalExitExecution(
            token_id="ethereum:0x1111", chain_id=1, wallet_address="0xUser", tx_hash="0xexittx",
            tokens_sold=12000.0, gross_proceeds=1800.0, exit_gas_cost=15.0, exit_venue_fees=2.0,
        )

        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, is_cryptographically_verified=True)
        self.assertEqual(outcome.status, EvidenceTier.INVALID_TRANCHE_QUANTITY_EXCEEDED)
        self.assertEqual(outcome.alpha_ratio, 0.0)
        self.assertFalse(outcome.contributes_to_verified_pl)

    def test_identity_reconciliation_adversarial(self):
        """Adversarial: Mismatched token, chain, or wallet identities fail closed."""
        entry = JournalEntryPosition(
            token_id="ethereum:0x1111", chain_id=1, wallet_address="0xUserA", tx_hash="0xtx1",
            quote_expenditure=100.0, tokens_acquired=1000.0, entry_gas_cost=5.0, entry_venue_fees=1.0,
        )
        exit_exec_diff_token = JournalExitExecution(
            token_id="ethereum:0x2222", chain_id=1, wallet_address="0xUserA", tx_hash="0xtx2",
            tokens_sold=500.0, gross_proceeds=80.0, exit_gas_cost=5.0, exit_venue_fees=1.0,
        )
        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec_diff_token, is_cryptographically_verified=True)
        self.assertEqual(outcome.status, EvidenceTier.IDENTITY_RECONCILIATION_FAILED)
        self.assertFalse(outcome.contributes_to_verified_pl)

    def test_non_finite_or_negative_values_rejected(self):
        """Adversarial: Non-finite (NaN, inf) or negative values are rejected."""
        entry = JournalEntryPosition(
            token_id="ethereum:0x1111", chain_id=1, wallet_address="0xUserA", tx_hash="0xtx1",
            quote_expenditure=float("nan"), tokens_acquired=1000.0, entry_gas_cost=5.0, entry_venue_fees=1.0,
        )
        exit_exec = JournalExitExecution(
            token_id="ethereum:0x1111", chain_id=1, wallet_address="0xUserA", tx_hash="0xtx2",
            tokens_sold=500.0, gross_proceeds=80.0, exit_gas_cost=5.0, exit_venue_fees=1.0,
        )
        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, is_cryptographically_verified=True)
        self.assertEqual(outcome.status, EvidenceTier.NON_FINITE_OR_NEGATIVE_VALUES)
        self.assertFalse(outcome.contributes_to_verified_pl)


class TestStage31ClusteredStatisticalInference(unittest.TestCase):
    """Test Cameron-Gelbach-Miller two-way clustering, exact t-crit, Newey-West fallback, and sample hurdles."""

    def test_cgm_hand_calculated_2x3_panel_fixture(self):
        """Validates CGM formula against exact hand-calculated 2x3 panel fixture."""
        fixture_data = [
            {"token_id": "T0", "week_block": "W0", "loss_diff": 2.0},
            {"token_id": "T1", "week_block": "W0", "loss_diff": 4.0},
            {"token_id": "T0", "week_block": "W1", "loss_diff": 3.0},
            {"token_id": "T1", "week_block": "W1", "loss_diff": 5.0},
            {"token_id": "T0", "week_block": "W2", "loss_diff": 6.0},
            {"token_id": "T1", "week_block": "W2", "loss_diff": 10.0},
        ]
        res = ClusteredEvaluationEngine.compute_cgm_two_way_variance(fixture_data, field="loss_diff")

        # Hand-calculated analytical fractions:
        # V_token = 16/9, V_time = 7/3, V_inter = 4/3, V_twoway = 25/9, SE_twoway = 5/3
        self.assertAlmostEqual(res["var_token"], 16.0 / 9.0, places=10)
        self.assertAlmostEqual(res["var_time"], 7.0 / 3.0, places=10)
        self.assertAlmostEqual(res["var_inter"], 4.0 / 3.0, places=10)
        self.assertAlmostEqual(res["var_twoway"], 25.0 / 9.0, places=10)
        self.assertAlmostEqual(res["se_reported"], 5.0 / 3.0, places=10)
        self.assertFalse(res["fallback_invoked"])

    def test_exact_students_t_critical_values(self):
        """Validates exact Student's t critical values for actual df (never defaulting to 2.0)."""
        self.assertEqual(ClusteredEvaluationEngine.get_exact_students_t_crit(1), 12.706)
        self.assertEqual(ClusteredEvaluationEngine.get_exact_students_t_crit(5), 2.571)
        self.assertEqual(ClusteredEvaluationEngine.get_exact_students_t_crit(11), 2.201)
        self.assertEqual(ClusteredEvaluationEngine.get_exact_students_t_crit(30), 2.042)
        # For df=100, asymptotic Cornish-Fisher expansion is ~1.984
        t_100 = ClusteredEvaluationEngine.get_exact_students_t_crit(100)
        self.assertAlmostEqual(t_100, 1.984, places=2)

    def test_newey_west_block_variance_implementation(self):
        """Validates Newey-West Bartlett kernel variance implementation on block means."""
        # Fixed block-means series with positive lag-1 autocorrelation
        means = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        nw_var = ClusteredEvaluationEngine.compute_newey_west_block_variance(means, bandwidth=2)
        self.assertGreater(nw_var, 0.0)

    def test_serial_diagnostic_positive_control_stationary_ar1(self):
        """Positive-Control: Persistent weekly shocks trigger Newey-West fallback and status."""
        random.seed(123)
        B = 12
        M = 30

        gamma = [0.0] * B
        for w in range(1, B):
            gamma[w] = 0.85 * gamma[w - 1] + random.gauss(0, 0.02)

        dataset = []
        for w in range(B):
            for i in range(M):
                dataset.append({
                    "token_id": f"token_{i}",
                    "week_block": f"week_{w:02d}",
                    "loss_diff": 0.05 + gamma[w] + random.gauss(0, 0.01),
                })

        eval_res = ClusteredEvaluationEngine.run_stage31_evaluation(dataset, field="loss_diff")
        self.assertEqual(eval_res["status"], InferentialStatus.DEPENDENT_BLOCK_FALLBACK_TRIGGERED.value)
        self.assertEqual(eval_res["diagnostic_flag"], "SERIAL_PERSISTENCE_DETECTED_NEWEY_WEST_FALLBACK")
        self.assertTrue(eval_res["serial_diagnostic"]["fallback_triggered"])
        self.assertGreater(eval_res["se_newey_west"], 0.0)

    def test_serial_diagnostic_negative_control_repeated_panels(self):
        """Negative-Control: 100 independent panels (phi = 0) have false alarm rate <= 10%."""
        random.seed(456)
        B = 12
        M = 30
        S_panels = 100
        false_alarms = 0

        for _ in range(S_panels):
            gamma = [random.gauss(0, 0.08) for _ in range(B)]
            dataset = []
            for w in range(B):
                for i in range(M):
                    dataset.append({
                        "token_id": f"token_{i}",
                        "week_block": f"week_{w:02d}",
                        "loss_diff": 0.05 + gamma[w] + random.gauss(0, 0.02),
                    })
            diag = ClusteredEvaluationEngine.evaluate_inter_block_serial_dependence(dataset, field="loss_diff")
            if diag["fallback_triggered"]:
                false_alarms += 1

        false_alarm_rate = false_alarms / S_panels
        self.assertLessEqual(false_alarm_rate, 0.10)

    def test_sample_hurdles_for_confirmatory_status(self):
        """Enforces that M >= 50, B >= 30, N >= 500 hurdles are required before any confirmatory status."""
        # 1. Panel with B=12 (fails B >= 30 hurdle) -> EXPLORATORY
        dataset_b12 = [
            {"token_id": f"token_{i}", "week_block": f"week_{w:02d}", "loss_diff": 0.05 + (w % 3) * 0.001}
            for w in range(12)
            for i in range(50)
        ]
        eval_b12 = ClusteredEvaluationEngine.run_stage31_evaluation(dataset_b12, field="loss_diff")
        self.assertEqual(eval_b12["status"], InferentialStatus.EXPLORATORY_UNCALIBRATED_INFERENCE.value)
        self.assertTrue("SAMPLE_HURDLES_UNMET" in eval_b12["diagnostic_flag"])

        # 2. Panel with M=50, B=30, N=1500 meeting all hurdles without serial persistence -> CONFIRMATORY
        dataset_conf = [
            {"token_id": f"token_{i}", "week_block": f"week_{w:02d}", "loss_diff": 0.05 + random.gauss(0, 0.005)}
            for w in range(30)
            for i in range(50)
        ]
        eval_conf = ClusteredEvaluationEngine.run_stage31_evaluation(dataset_conf, field="loss_diff")
        self.assertEqual(eval_conf["status"], InferentialStatus.CONFIRMATORY_INFERENCE.value)
        self.assertEqual(eval_conf["diagnostic_flag"], "ADEQUATE_PANEL_DIMENSIONS_AND_INDEPENDENCE_CONFIRMED")


if __name__ == "__main__":
    unittest.main()
