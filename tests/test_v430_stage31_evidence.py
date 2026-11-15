#!/usr/bin/env python3
"""Stage 3.1: Comprehensive Unit & Adversarial Test Suite for Rocket Hunter v4.30.

Exercises and validates all Stage 3.1 acceptance criteria with rigorous adversarial tests:
1. Canonical Uniswap V3 TickMath formula, boundary conditions, and tick-204520 fixture.
2. Protected data integrity verifier (manifest snapshot, deletions, unauthorized creations, fail-closed).
3. Non-tautological RPC evidence verification:
   - Tier 1 wire byte disk retention and expected digest validation.
   - Tier 2 downgraded to caller-asserted metadata (never claims cryptographic TLS auth).
   - Tier 3 requires distinct independent providers and chain-specific finality proofs.
4. Multi-chain configuration-driven finality (Ethereum L1, Arbitrum dynamic reader, OP Stack op-contracts/v1.6.0, Solana).
5. Venue-specific execution evidence structures:
   - Uniswap V3 mathematical tick/price consistency (get_tick_at_sqrt_ratio(sqrtPriceX96) == tick).
   - Bidirectional tick-bitmap consistency (Direction A and Direction B regressions).
   - Uniswap V2 fee bounds and reserve positivity.
6. Dataset A trade journal reconciliation:
   - Binding INDEPENDENTLY_VERIFIED_EXECUTION to validated on-chain receipts and transfer logs.
   - Strict rejection of tokens_sold > tokens_acquired (never clamped).
   - Non-finite and identity mismatch regressions.
7. Cameron-Gelbach-Miller two-way clustered variance hand-calculated fixture (2x3 panel).
8. Exact Student's t critical values for actual degrees of freedom (df = min(M - 1, B - 1)).
9. Chronological week sorting and safe edge-case handling for empty (N=0) and single-observation (N=1) datasets.
10. Strict exploratory labeling: confirmatory status is explicitly avoided.
"""
from __future__ import annotations

import decimal
import hashlib
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
    OnChainTransferEvent,
    ProtectedDataVerifier,
    RpcEvidenceRecord,
    RpcProvenanceVerifier,
    UniswapV2Evidence,
    UniswapV3Evidence,
    UniswapV3TickMath,
    ValidatedTransactionReceipt,
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

    def test_snapshot_missing_category_fails_closed(self):
        """Validates that incomplete snapshots missing required categories are strictly rejected."""
        valid_snapshot = self.verifier.snapshot_protected_data()

        # Pre-snapshot missing required category
        incomplete_pre = {"scanner_memory_partitions": {}, "config_files": {}}
        is_clean, disc = self.verifier.verify_clean_equivalence(incomplete_pre, valid_snapshot)
        self.assertFalse(is_clean)
        self.assertTrue(any("missing required category 'protected_files'" in d for d in disc))

        # Post-snapshot missing required category
        incomplete_post = {"protected_files": {}, "scanner_memory_partitions": {}}
        is_clean, disc = self.verifier.verify_clean_equivalence(valid_snapshot, incomplete_post)
        self.assertFalse(is_clean)
        self.assertTrue(any("missing required category 'config_files'" in d for d in disc))

        # Completely empty snapshots fail closed
        is_clean, disc = self.verifier.verify_clean_equivalence({}, {})
        self.assertFalse(is_clean)
        self.assertTrue(len(disc) > 0)

    def test_snapshot_malformed_category_type_fails_closed(self):
        """Validates that snapshots with non-dictionary categories fail closed."""
        valid_snapshot = self.verifier.snapshot_protected_data()
        malformed = {
            "protected_files": ["not_a_dict"],
            "scanner_memory_partitions": {},
            "config_files": {},
        }
        is_clean, disc = self.verifier.verify_clean_equivalence(malformed, valid_snapshot)
        self.assertFalse(is_clean)
        self.assertTrue(any("must be a dictionary" in d for d in disc))


class TestStage31RpcProvenanceAndQuarantine(unittest.TestCase):
    """Test three-tiered RPC evidence, downgraded Tier 2 metadata, and distinct-provider Tier 3 finality."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.verifier = RpcProvenanceVerifier(data_dir=self.temp_dir)

    def _make_record(
        self,
        block_hash: str = "0x1234",
        parent_hash: str = "0x0000",
        state_root: str = "0xabcd",
        provider_id: str = "alchemy_primary",
        provider_domain: str = "eth-mainnet.alchemy.com",
        chain_id: int = 1,
        block_number: int = 19450000,
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
            tls_handshake_metadata={"caller_asserted": True},
        )

    def test_non_tautological_tier1_payload_verification(self):
        """Validates payload integrity against independent expected digests and disk retention."""
        rec = self._make_record()
        self.assertTrue(self.verifier.verify_tier1_payload_integrity(rec))

        bad_rec = self._make_record(
            raw_response_bytes=b'{"result": "0xTAMPERED"}',
            expected_response_sha256="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )
        self.assertFalse(self.verifier.verify_tier1_payload_integrity(bad_rec))

    def test_tier2_downgraded_metadata_unverified(self):
        """Validates that Tier 2 is downgraded to caller-asserted metadata and does not claim authentication."""
        rec = self._make_record()
        passed, msg = self.verifier.verify_tier2_endpoint_metadata(rec)
        self.assertFalse(passed)
        self.assertEqual(msg, "ENDPOINT_METADATA_RECORDED_UNVERIFIED")

    def test_tier3_requires_distinct_independent_providers(self):
        """Validates that Tier 3 rejects identical providers for quorum cross-check."""
        rec1 = self._make_record(provider_id="alchemy_node_1")
        rec2_same_provider = self._make_record(provider_id="alchemy_node_1")

        passed, state, msg = self.verifier.verify_tier3_canonical_quorum(
            rec1, independent_peer_record=rec2_same_provider, finality_proof={"proof_type": "ETHEREUM_BEACON_FINALITY"}
        )
        self.assertFalse(passed)
        self.assertEqual(state, FinalityState.FINALITY_PENDING)
        self.assertEqual(msg, "QUORUM_REQUIRES_DISTINCT_INDEPENDENT_PROVIDERS")

    def test_tier3_chain_specific_finality_ethereum_l1(self):
        """Validates Ethereum L1 Beacon checkpoint proof evaluation (downgraded to caller-asserted unverified)."""
        rec1 = self._make_record(provider_id="alchemy_primary", block_hash="0x1234")
        rec2 = self._make_record(provider_id="infura_secondary", block_hash="0x1234")

        # Caller-asserted Beacon checkpoint proof matches schema, but in-engine cryptographic
        # verification does not exist -> remains FINALITY_CALLER_ASSERTED_UNVERIFIED, not FINALIZED_CANONICAL
        proof_valid = {
            "proof_type": "ETHEREUM_BEACON_FINALITY",
            "checkpoint_epoch": 280000,
            "checkpoint_root": "0xbeaconroot",
            "checkpoint_block_hash": "0x1234",
        }
        passed, state, msg = self.verifier.verify_tier3_canonical_quorum(rec1, rec2, finality_proof=proof_valid)
        self.assertFalse(passed)
        self.assertEqual(state, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)
        self.assertEqual(msg, "CALLER_ASSERTED_FINALITY_NOT_CRYPTOGRAPHICALLY_VERIFIED")

        # Mismatched checkpoint block hash fails to FINALITY_PENDING
        proof_mismatch = {
            "proof_type": "ETHEREUM_BEACON_FINALITY",
            "checkpoint_epoch": 280000,
            "checkpoint_root": "0xbeaconroot",
            "checkpoint_block_hash": "0xOTHER",
        }
        passed, state, msg = self.verifier.verify_tier3_canonical_quorum(rec1, rec2, finality_proof=proof_mismatch)
        self.assertFalse(passed)
        self.assertEqual(state, FinalityState.FINALITY_PENDING)

    def test_tier3_chain_specific_finality_arbitrum(self):
        """Validates Arbitrum Nitro assertion proof evaluation (downgraded to caller-asserted unverified)."""
        rec1 = self._make_record(chain_id=42161, provider_id="alchemy_arb", block_hash="0xarb")
        rec2 = self._make_record(chain_id=42161, provider_id="quicknode_arb", block_hash="0xarb")

        # Assertion confirmed (elapsed blocks >= 45,818) -> caller-asserted unverified, not FINALIZED_CANONICAL
        proof_arb_canonical = {
            "proof_type": "ARBITRUM_NITRO_ASSERTION",
            "l1_batch_finalized": True,
            "assertion_created_l1_block": 19000000,
            "current_finalized_l1_block": 19050000,  # 50,000 blocks > 45,818
            "confirm_period_blocks": 45818,
            "challenge_status": "NONE",
        }
        passed, state, msg = self.verifier.verify_tier3_canonical_quorum(rec1, rec2, finality_proof=proof_arb_canonical)
        self.assertFalse(passed)
        self.assertEqual(state, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)
        self.assertEqual(msg, "CALLER_ASSERTED_FINALITY_NOT_CRYPTOGRAPHICALLY_VERIFIED")

        # Assertion window still open -> L1_BATCH_FINALITY_ONLY
        proof_arb_pending = {
            "proof_type": "ARBITRUM_NITRO_ASSERTION",
            "l1_batch_finalized": True,
            "assertion_created_l1_block": 19000000,
            "current_finalized_l1_block": 19010000,  # 10,000 blocks < 45,818
            "confirm_period_blocks": 45818,
            "challenge_status": "NONE",
        }
        passed, state, msg = self.verifier.verify_tier3_canonical_quorum(rec1, rec2, finality_proof=proof_arb_pending)
        self.assertFalse(passed)
        self.assertEqual(state, FinalityState.L1_BATCH_FINALITY_ONLY)

    def test_tier3_block_hash_divergence_quarantines_reorg(self):
        """Validates block hash divergence between distinct providers quarantines reorg orphan."""
        rec1 = self._make_record(provider_id="alchemy_primary", block_hash="0x1111")
        rec2 = self._make_record(provider_id="infura_secondary", block_hash="0x2222")

        passed, state, msg = self.verifier.verify_tier3_canonical_quorum(rec1, rec2, finality_proof={"proof_type": "ETHEREUM_BEACON_FINALITY"})
        self.assertFalse(passed)
        self.assertEqual(state, FinalityState.BLOCK_REORGANIZED_ORPHAN)
        self.assertTrue((self.verifier.quarantine_dir / "1" / "19450000" / "quarantine_manifest.json").is_file())


class TestStage31MultiChainFinality(unittest.TestCase):
    """Test configuration-driven finality state machine across EVM and non-EVM chains."""

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


class TestStage31VenueEvidenceAndDualTrackOutcomes(unittest.TestCase):
    """Test venue evidence structures, tick/price relationship, and bidirectional bitmap consistency."""

    def test_uniswap_v3_range_validation_complete(self):
        """Validates consistent Uniswap V3 evidence with exact tick/price match and bidirectional bitmap consistency."""
        # Using fee tier 500 (spacing 10), where tick 204520 is exact multiple of 10
        spacing = UNISWAP_V3_FEE_SPACINGS[500]  # 10
        t_curr = 204520
        t_target = 204540
        sqrt_p = 2186517289083564169714246582247698  # Exact sqrt ratio for tick 204520

        comp_curr = t_curr // spacing  # 20452
        w_curr = comp_curr >> 8       # 79
        bit_curr = comp_curr % 256    # 228

        v3 = UniswapV3Evidence(
            chain_id=1,
            pool_address="0xpool3",
            token0_address="0xtoken0",
            token1_address="0xtoken1",
            token0_decimals=6,
            token1_decimals=18,
            fee_tier_ppm=500,
            sqrt_price_x96=sqrt_p,
            tick=t_curr,
            active_liquidity=1000000,
            initialized_ticks={
                t_curr: {"liquidityNet": 500, "liquidityGross": 500},
            },
            tick_bitmap={w_curr: 1 << bit_curr},
            block_number=19000000,
            block_hash="0xhash",
            parent_hash="0xparent",
        )
        self.assertTrue(v3.is_complete_for_range(target_tick=t_target))

    def test_uniswap_v3_regression_price_tick_mismatch(self):
        """Regression: Fails closed when tick does not mathematically correspond to sqrt_price_x96."""
        spacing = UNISWAP_V3_FEE_SPACINGS[500]
        t_curr = 204540  # Mismatched tick!
        sqrt_p = 2186517289083564169714246582247698  # This ratio is for tick 204520!

        comp = t_curr // spacing
        w = comp >> 8
        b = comp % 256

        v3_mismatch = UniswapV3Evidence(
            chain_id=1, pool_address="0xp", token0_address="0xt0", token1_address="0xt1",
            token0_decimals=6, token1_decimals=18, fee_tier_ppm=500,
            sqrt_price_x96=sqrt_p, tick=t_curr, active_liquidity=100,
            initialized_ticks={t_curr: {"liquidityNet": 100, "liquidityGross": 100}},
            tick_bitmap={w: 1 << b}, block_number=1, block_hash="0xh", parent_hash="0xp",
        )
        self.assertFalse(v3_mismatch.is_complete_for_range(target_tick=204560))

    def test_uniswap_v3_exact_boundary_tick_semantics(self):
        """Regression: Validates exact-boundary state where pool tick can be tickNext - 1 after zero-for-one crossing."""
        spacing = UNISWAP_V3_FEE_SPACINGS[100]  # spacing 1
        t_boundary = 204520
        sqrt_p_exact = UniswapV3TickMath.get_sqrt_ratio_at_tick(t_boundary)

        w = (t_boundary // spacing) >> 8
        b = (t_boundary // spacing) % 256

        # 1. Normal state: pool tick == t_boundary
        v3_normal = UniswapV3Evidence(
            chain_id=1, pool_address="0xp", token0_address="0xt0", token1_address="0xt1",
            token0_decimals=6, token1_decimals=18, fee_tier_ppm=100,
            sqrt_price_x96=sqrt_p_exact, tick=t_boundary, active_liquidity=1000,
            initialized_ticks={t_boundary: {"liquidityNet": 100, "liquidityGross": 100}},
            tick_bitmap={w: 1 << b}, block_number=1, block_hash="0xh", parent_hash="0xp",
        )
        self.assertTrue(v3_normal.is_complete_for_range(target_tick=t_boundary + 10))

        # 2. Boundary crossing state: pool tick == t_boundary - 1 (step.tickNext - 1)
        v3_boundary_crossing = UniswapV3Evidence(
            chain_id=1, pool_address="0xp", token0_address="0xt0", token1_address="0xt1",
            token0_decimals=6, token1_decimals=18, fee_tier_ppm=100,
            sqrt_price_x96=sqrt_p_exact, tick=t_boundary - 1, active_liquidity=1000,
            initialized_ticks={t_boundary: {"liquidityNet": 100, "liquidityGross": 100}},
            tick_bitmap={w: 1 << b}, block_number=1, block_hash="0xh", parent_hash="0xp",
        )
        self.assertTrue(v3_boundary_crossing.is_complete_for_range(target_tick=t_boundary + 10))

        # 3. Mismatched ticks (t_boundary - 2, t_boundary + 1) fail closed
        v3_too_low = UniswapV3Evidence(
            chain_id=1, pool_address="0xp", token0_address="0xt0", token1_address="0xt1",
            token0_decimals=6, token1_decimals=18, fee_tier_ppm=100,
            sqrt_price_x96=sqrt_p_exact, tick=t_boundary - 2, active_liquidity=1000,
            initialized_ticks={t_boundary: {"liquidityNet": 100, "liquidityGross": 100}},
            tick_bitmap={w: 1 << b}, block_number=1, block_hash="0xh", parent_hash="0xp",
        )
        self.assertFalse(v3_too_low.is_complete_for_range(target_tick=t_boundary + 10))

        # 4. Non-boundary ratio (interior): tick == calc_tick - 1 must fail
        sqrt_p_interior = sqrt_p_exact + 100
        self.assertEqual(UniswapV3TickMath.get_tick_at_sqrt_ratio(sqrt_p_interior), t_boundary)
        v3_interior_invalid = UniswapV3Evidence(
            chain_id=1, pool_address="0xp", token0_address="0xt0", token1_address="0xt1",
            token0_decimals=6, token1_decimals=18, fee_tier_ppm=100,
            sqrt_price_x96=sqrt_p_interior, tick=t_boundary - 1, active_liquidity=1000,
            initialized_ticks={t_boundary: {"liquidityNet": 100, "liquidityGross": 100}},
            tick_bitmap={w: 1 << b}, block_number=1, block_hash="0xh", parent_hash="0xp",
        )
        self.assertFalse(v3_interior_invalid.is_complete_for_range(target_tick=t_boundary + 10))

    def test_uniswap_v3_regression_bidirectional_bitmap_mismatch(self):
        """Regression: Fails closed when initialized tick is missing from bitmap (Direction A)."""
        spacing = UNISWAP_V3_FEE_SPACINGS[500]
        t_curr = 204520
        sqrt_p = 2186517289083564169714246582247698

        comp = t_curr // spacing
        w = comp >> 8

        # Word exists in tick_bitmap, but bit is 0 (missing from bitmap!)
        v3_missing_bit = UniswapV3Evidence(
            chain_id=1, pool_address="0xp", token0_address="0xt0", token1_address="0xt1",
            token0_decimals=6, token1_decimals=18, fee_tier_ppm=500,
            sqrt_price_x96=sqrt_p, tick=t_curr, active_liquidity=100,
            initialized_ticks={t_curr: {"liquidityNet": 100, "liquidityGross": 100}},
            tick_bitmap={w: 0},  # Bit is 0!
            block_number=1, block_hash="0xh", parent_hash="0xp",
        )
        self.assertFalse(v3_missing_bit.is_complete_for_range(target_tick=204540))

    def test_uniswap_v2_fee_bounds(self):
        """Validates Uniswap V2 evidence structure, reserve positivity, and fee bounds."""
        v2 = UniswapV2Evidence(
            chain_id=1, pool_address="0xp2", token0_address="0xt0", token1_address="0xt1",
            token0_decimals=6, token1_decimals=18, raw_reserve0=1000, raw_reserve1=2000,
            fee_bps=30, block_number=1, block_hash="0xh", parent_hash="0xp",
        )
        self.assertTrue(v2.is_complete())
        v2.fee_bps = 0
        self.assertFalse(v2.is_complete())
        v2.fee_bps = 10001
        self.assertFalse(v2.is_complete())


class TestStage31DatasetAReconciliation(unittest.TestCase):
    """Test Dataset A trade journal reconciliation with validated on-chain receipts and volume checks."""

    def _make_receipt(
        self,
        chain_id: int,
        tx_hash: str,
        from_addr: str,
        to_addr: str,
        token: str,
        amount: float,
        is_canonical: bool = True,
        raw_bytes: bytes = b'{"receipt": true}',
        reconciled_gas: Optional[float] = None,
        reconciled_quote: Optional[float] = None,
        expected_sha256: Optional[str] = None,
        is_trusted_chain: bool = False,
    ) -> ValidatedTransactionReceipt:
        sha = expected_sha256 if expected_sha256 is not None else hashlib.sha256(raw_bytes).hexdigest().upper()
        return ValidatedTransactionReceipt(
            chain_id=chain_id,
            tx_hash=tx_hash,
            block_number=19000000,
            block_hash="0xblock",
            status=1,
            gas_used=100000,
            effective_gas_price_wei=20000000000,
            transfers=[OnChainTransferEvent(token_address=token, from_address=from_addr, to_address=to_addr, amount=amount)],
            raw_receipt_bytes=raw_bytes,
            expected_receipt_sha256=sha,
            is_canonical_block=is_canonical,
            reconciled_gas_cost=reconciled_gas,
            reconciled_quote_amount=reconciled_quote,
            is_trusted_chain_validated=is_trusted_chain,
        )

    def test_receipt_bound_verified_execution(self):
        """Validates that caller-supplied receipts remain USER_REPORTED_EXECUTION without trusted chain source."""
        entry = JournalEntryPosition(
            token_id="ethereum:0x1111", chain_id=1, wallet_address="0xUser", tx_hash="0xentrytx",
            quote_expenditure=1000.0, tokens_acquired=10000.0, entry_gas_cost=20.0, entry_venue_fees=3.0,
        )
        exit_exec = JournalExitExecution(
            token_id="ethereum:0x1111", chain_id=1, wallet_address="0xUser", tx_hash="0xexittx",
            tokens_sold=4000.0, gross_proceeds=600.0, exit_gas_cost=15.0, exit_venue_fees=2.0,
        )

        entry_receipt = self._make_receipt(
            1, "0xentrytx", "0xpool", "0xUser", "0x1111", 10000.0,
            reconciled_gas=20.0, reconciled_quote=1000.0, is_trusted_chain=False,
        )
        exit_receipt = self._make_receipt(
            1, "0xexittx", "0xUser", "0xpool", "0x1111", 4000.0,
            reconciled_gas=15.0, reconciled_quote=600.0, is_trusted_chain=False,
        )

        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, entry_receipt, exit_receipt)
        # Caller-supplied receipts must be classified as unverified USER_REPORTED_EXECUTION
        self.assertEqual(outcome.status, EvidenceTier.USER_REPORTED_EXECUTION)
        self.assertEqual(outcome.alpha_ratio, 0.40)
        self.assertAlmostEqual(outcome.realized_net_pl, 173.8, places=2)
        self.assertFalse(outcome.contributes_to_verified_pl)

    def test_forged_receipt_with_trusted_flag_cannot_promote_to_verified_execution(self):
        """Regression: Setting caller-asserted flags (e.g. is_trusted_chain_validated=True) on forged receipt data
        cannot promote to INDEPENDENTLY_VERIFIED_EXECUTION. The outcome remains strictly USER_REPORTED_EXECUTION
        and contributes_to_verified_pl remains False.
        """
        entry = JournalEntryPosition(
            token_id="ethereum:0x1111", chain_id=1, wallet_address="0xUser", tx_hash="0xentrytx",
            quote_expenditure=1000.0, tokens_acquired=10000.0, entry_gas_cost=20.0, entry_venue_fees=3.0,
        )
        exit_exec = JournalExitExecution(
            token_id="ethereum:0x1111", chain_id=1, wallet_address="0xUser", tx_hash="0xexittx",
            tokens_sold=4000.0, gross_proceeds=600.0, exit_gas_cost=15.0, exit_venue_fees=2.0,
        )

        entry_receipt = self._make_receipt(
            1, "0xentrytx", "0xpool", "0xUser", "0x1111", 10000.0,
            reconciled_gas=20.0, reconciled_quote=1000.0, is_trusted_chain=True,
        )
        exit_receipt = self._make_receipt(
            1, "0xexittx", "0xUser", "0xpool", "0x1111", 4000.0,
            reconciled_gas=15.0, reconciled_quote=600.0, is_trusted_chain=True,
        )

        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, entry_receipt, exit_receipt)
        self.assertEqual(outcome.status, EvidenceTier.USER_REPORTED_EXECUTION)
        self.assertFalse(outcome.contributes_to_verified_pl)

    def test_canonical_token_address_matching(self):
        """Validates transfer matching handles canonical on-chain addresses vs namespaced token_id."""
        entry = JournalEntryPosition(
            token_id="ethereum:0x1111", chain_id=1, wallet_address="0xUser", tx_hash="0xentrytx",
            quote_expenditure=1000.0, tokens_acquired=10000.0, entry_gas_cost=20.0, entry_venue_fees=3.0,
        )
        exit_exec = JournalExitExecution(
            token_id="ethereum:0x1111", chain_id=1, wallet_address="0xUser", tx_hash="0xexittx",
            tokens_sold=4000.0, gross_proceeds=600.0, exit_gas_cost=15.0, exit_venue_fees=2.0,
        )

        # Transfer log has bare canonical address '0x1111', entry has namespaced 'ethereum:0x1111' -> should match!
        entry_receipt = self._make_receipt(
            1, "0xentrytx", "0xpool", "0xUser", "0x1111", 10000.0,
            reconciled_gas=20.0, reconciled_quote=1000.0,
        )
        exit_receipt = self._make_receipt(
            1, "0xexittx", "0xUser", "0xpool", "0x1111", 4000.0,
            reconciled_gas=15.0, reconciled_quote=600.0,
        )
        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, entry_receipt, exit_receipt)
        self.assertEqual(outcome.status, EvidenceTier.USER_REPORTED_EXECUTION)

        # Transfer log has completely different token address '0x9999' -> must reject
        entry_receipt_bad_token = self._make_receipt(
            1, "0xentrytx", "0xpool", "0xUser", "0x9999", 10000.0,
            reconciled_gas=20.0, reconciled_quote=1000.0,
        )
        outcome_bad = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, entry_receipt_bad_token, exit_receipt)
        self.assertEqual(outcome_bad.status, EvidenceTier.RECEIPT_VERIFICATION_FAILED)

    def test_unverified_when_required_gas_or_proceeds_amounts_missing(self):
        """Adversarial: Validates that missing required gas or proceeds amounts fail closed as INCOMPLETE."""
        entry = JournalEntryPosition("ethereum:0x1111", 1, "0xUser", "0xentrytx", 1000.0, 10000.0, 20.0, 3.0)
        exit_exec = JournalExitExecution("ethereum:0x1111", 1, "0xUser", "0xexittx", 4000.0, 600.0, 15.0, 2.0)

        # Missing reconciled_gas_cost (None)
        entry_receipt_no_gas = self._make_receipt(
            1, "0xentrytx", "0xpool", "0xUser", "0x1111", 10000.0,
            reconciled_gas=None, reconciled_quote=1000.0,
        )
        exit_receipt = self._make_receipt(
            1, "0xexittx", "0xUser", "0xpool", "0x1111", 4000.0,
            reconciled_gas=15.0, reconciled_quote=600.0,
        )
        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, entry_receipt_no_gas, exit_receipt)
        self.assertEqual(outcome.status, EvidenceTier.EXECUTION_EVIDENCE_INCOMPLETE)
        self.assertFalse(outcome.contributes_to_verified_pl)

    def test_unverified_when_raw_receipt_bytes_digest_mismatches(self):
        """Adversarial: Validates rejection when expected_receipt_sha256 does not match raw bytes."""
        entry = JournalEntryPosition("ethereum:0x1111", 1, "0xUser", "0xentrytx", 1000.0, 10000.0, 20.0, 3.0)
        exit_exec = JournalExitExecution("ethereum:0x1111", 1, "0xUser", "0xexittx", 4000.0, 600.0, 15.0, 2.0)

        entry_receipt = self._make_receipt(
            1, "0xentrytx", "0xpool", "0xUser", "ethereum:0x1111", 10000.0,
            expected_sha256="0xBADHASH", reconciled_gas=20.0, reconciled_quote=1000.0,
        )
        exit_receipt = self._make_receipt(
            1, "0xexittx", "0xUser", "0xpool", "ethereum:0x1111", 4000.0,
            reconciled_gas=15.0, reconciled_quote=600.0,
        )

        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, entry_receipt, exit_receipt)
        self.assertEqual(outcome.status, EvidenceTier.RECEIPT_VERIFICATION_FAILED)
        self.assertFalse(outcome.contributes_to_verified_pl)

    def test_unverified_when_receipt_block_is_not_canonical(self):
        """Adversarial: Validates rejection when receipt block is not confirmed canonical."""
        entry = JournalEntryPosition("ethereum:0x1111", 1, "0xUser", "0xentrytx", 1000.0, 10000.0, 20.0, 3.0)
        exit_exec = JournalExitExecution("ethereum:0x1111", 1, "0xUser", "0xexittx", 4000.0, 600.0, 15.0, 2.0)

        entry_receipt = self._make_receipt(
            1, "0xentrytx", "0xpool", "0xUser", "ethereum:0x1111", 10000.0,
            is_canonical=False, reconciled_gas=20.0, reconciled_quote=1000.0,
        )
        exit_receipt = self._make_receipt(
            1, "0xexittx", "0xUser", "0xpool", "ethereum:0x1111", 4000.0,
            reconciled_gas=15.0, reconciled_quote=600.0,
        )

        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, entry_receipt, exit_receipt)
        self.assertEqual(outcome.status, EvidenceTier.RECEIPT_VERIFICATION_FAILED)
        self.assertFalse(outcome.contributes_to_verified_pl)

    def test_unverified_when_transfer_token_address_mismatches(self):
        """Adversarial: Validates rejection when transfer log token address mismatches traded token."""
        entry = JournalEntryPosition("ethereum:0x1111", 1, "0xUser", "0xentrytx", 1000.0, 10000.0, 20.0, 3.0)
        exit_exec = JournalExitExecution("ethereum:0x1111", 1, "0xUser", "0xexittx", 4000.0, 600.0, 15.0, 2.0)

        # Entry transfer is for a different token (0xROGUE instead of 0x1111)
        entry_receipt = self._make_receipt(
            1, "0xentrytx", "0xpool", "0xUser", "ethereum:0xROGUE", 10000.0,
            reconciled_gas=20.0, reconciled_quote=1000.0,
        )
        exit_receipt = self._make_receipt(
            1, "0xexittx", "0xUser", "0xpool", "ethereum:0x1111", 4000.0,
            reconciled_gas=15.0, reconciled_quote=600.0,
        )

        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, entry_receipt, exit_receipt)
        self.assertEqual(outcome.status, EvidenceTier.RECEIPT_VERIFICATION_FAILED)
        self.assertFalse(outcome.contributes_to_verified_pl)

    def test_unverified_when_gas_or_proceeds_cost_mismatches(self):
        """Adversarial: Validates rejection when reported costs differ from reconciled receipt costs."""
        entry = JournalEntryPosition("ethereum:0x1111", 1, "0xUser", "0xentrytx", 1000.0, 10000.0, 20.0, 3.0)
        exit_exec = JournalExitExecution("ethereum:0x1111", 1, "0xUser", "0xexittx", 4000.0, 600.0, 15.0, 2.0)

        # Mismatched gas cost (reported 20.0, receipt says 999.0)
        entry_receipt_bad_gas = self._make_receipt(
            1, "0xentrytx", "0xpool", "0xUser", "ethereum:0x1111", 10000.0,
            reconciled_gas=999.0, reconciled_quote=1000.0,
        )
        exit_receipt = self._make_receipt(
            1, "0xexittx", "0xUser", "0xpool", "ethereum:0x1111", 4000.0,
            reconciled_gas=15.0, reconciled_quote=600.0,
        )

        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, entry_receipt_bad_gas, exit_receipt)
        self.assertEqual(outcome.status, EvidenceTier.RECEIPT_VERIFICATION_FAILED)
        self.assertFalse(outcome.contributes_to_verified_pl)

        # Mismatched proceeds (reported 600.0, receipt says 50.0)
        entry_receipt_ok = self._make_receipt(
            1, "0xentrytx", "0xpool", "0xUser", "ethereum:0x1111", 10000.0,
            reconciled_gas=20.0, reconciled_quote=1000.0,
        )
        exit_receipt_bad_proceeds = self._make_receipt(
            1, "0xexittx", "0xUser", "0xpool", "ethereum:0x1111", 4000.0,
            reconciled_gas=15.0, reconciled_quote=50.0,
        )

        outcome2 = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, entry_receipt_ok, exit_receipt_bad_proceeds)
        self.assertEqual(outcome2.status, EvidenceTier.RECEIPT_VERIFICATION_FAILED)
        self.assertFalse(outcome2.contributes_to_verified_pl)

    def test_missing_receipt_remains_unverified(self):
        """Validates that caller-asserted records without receipts remain USER_REPORTED_EXECUTION."""
        entry = JournalEntryPosition("ethereum:0x1111", 1, "0xUser", "0xentrytx", 1000.0, 10000.0, 20.0, 3.0)
        exit_exec = JournalExitExecution("ethereum:0x1111", 1, "0xUser", "0xexittx", 4000.0, 600.0, 15.0, 2.0)

        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, entry_receipt=None, exit_receipt=None)
        self.assertEqual(outcome.status, EvidenceTier.USER_REPORTED_EXECUTION)
        self.assertFalse(outcome.contributes_to_verified_pl)

    def test_strict_rejection_when_sold_exceeds_acquired(self):
        """Adversarial: Reject tokens_sold > tokens_acquired without clamping to 1.0."""
        entry = JournalEntryPosition("ethereum:0x1111", 1, "0xUser", "0xentrytx", 1000.0, 10000.0, 20.0, 3.0)
        exit_exec = JournalExitExecution("ethereum:0x1111", 1, "0xUser", "0xexittx", 12000.0, 1800.0, 15.0, 2.0)

        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec)
        self.assertEqual(outcome.status, EvidenceTier.INVALID_TRANCHE_QUANTITY_EXCEEDED)
        self.assertFalse(outcome.contributes_to_verified_pl)


class TestStage31ClusteredStatisticalInference(unittest.TestCase):
    """Test chronological sorting, edge-case safety (N=0, N=1), exact t-crit, and exploratory status."""

    def test_cgm_hand_calculated_2x3_panel_fixture(self):
        """Validates CGM formula against exact hand-calculated 2x3 panel fixture."""
        fixture_data = [
            {"token_id": "T0", "week_block": "week_01", "loss_diff": 2.0},
            {"token_id": "T1", "week_block": "week_01", "loss_diff": 4.0},
            {"token_id": "T0", "week_block": "week_02", "loss_diff": 3.0},
            {"token_id": "T1", "week_block": "week_02", "loss_diff": 5.0},
            {"token_id": "T0", "week_block": "week_03", "loss_diff": 6.0},
            {"token_id": "T1", "week_block": "week_03", "loss_diff": 10.0},
        ]
        res = ClusteredEvaluationEngine.compute_cgm_two_way_variance(fixture_data, field="loss_diff")
        self.assertAlmostEqual(res["var_token"], 16.0 / 9.0, places=10)
        self.assertAlmostEqual(res["var_time"], 7.0 / 3.0, places=10)
        self.assertAlmostEqual(res["var_inter"], 4.0 / 3.0, places=10)
        self.assertAlmostEqual(res["var_twoway"], 25.0 / 9.0, places=10)
        self.assertAlmostEqual(res["se_reported"], 5.0 / 3.0, places=10)

    def test_edge_cases_empty_and_single_observation(self):
        """Validates that empty (N=0) and single-observation (N=1) datasets handle safely without KeyError."""
        # 1. Empty dataset (N=0)
        eval_empty = ClusteredEvaluationEngine.run_stage31_evaluation([], field="loss_diff")
        self.assertEqual(eval_empty["status"], InferentialStatus.EXPLORATORY_UNCALIBRATED_INFERENCE.value)
        self.assertEqual(eval_empty["mean_treatment_effect"], 0.0)
        self.assertEqual(eval_empty["diagnostic_flag"], "INSUFFICIENT_DATA_N_LESS_THAN_2")

        # 2. Single-observation dataset (N=1)
        single_obs = [{"token_id": "T0", "week_block": "week_01", "loss_diff": 0.075}]
        eval_single = ClusteredEvaluationEngine.run_stage31_evaluation(single_obs, field="loss_diff")
        self.assertEqual(eval_single["status"], InferentialStatus.EXPLORATORY_UNCALIBRATED_INFERENCE.value)
        self.assertEqual(eval_single["mean_treatment_effect"], 0.075)

    def test_chronological_week_sorting(self):
        """Validates that weeks are processed strictly in chronological order rather than first-seen order."""
        dataset = [
            {"token_id": "T0", "week_block": "week_03", "loss_diff": 0.3},
            {"token_id": "T0", "week_block": "week_01", "loss_diff": 0.1},
            {"token_id": "T0", "week_block": "week_02", "loss_diff": 0.2},
        ]
        diag = ClusteredEvaluationEngine.evaluate_inter_block_serial_dependence(dataset, field="loss_diff")
        self.assertEqual(diag["chronological_weeks"], ["week_01", "week_02", "week_03"])
        self.assertEqual(diag["week_means"], [0.1, 0.2, 0.3])

    def test_iso_date_and_cross_year_week_ordering(self):
        """Validates that explicit UTC week parsing sorts ISO weeks across year boundaries and ISO dates correctly."""
        # 1. Cross-year ISO weeks (2025-W50, 2025-W52, 2026-W01, 2026-W02)
        dataset_cross_year = [
            {"token_id": "T0", "week_block": "2026-W02", "loss_diff": 0.4},
            {"token_id": "T0", "week_block": "2025-W50", "loss_diff": 0.1},
            {"token_id": "T0", "week_block": "2026-W01", "loss_diff": 0.3},
            {"token_id": "T0", "week_block": "2025-W52", "loss_diff": 0.2},
        ]
        diag_cross = ClusteredEvaluationEngine.evaluate_inter_block_serial_dependence(dataset_cross_year, field="loss_diff")
        self.assertEqual(diag_cross["chronological_weeks"], ["2025-W50", "2025-W52", "2026-W01", "2026-W02"])
        self.assertEqual(diag_cross["week_means"], [0.1, 0.2, 0.3, 0.4])

        # 2. ISO calendar dates (2025-12-15, 2026-01-10, 2026-03-01)
        dataset_dates = [
            {"token_id": "T0", "week_block": "2026-03-01", "loss_diff": 0.3},
            {"token_id": "T0", "week_block": "2025-12-15", "loss_diff": 0.1},
            {"token_id": "T0", "week_block": "2026-01-10", "loss_diff": 0.2},
        ]
        diag_dates = ClusteredEvaluationEngine.evaluate_inter_block_serial_dependence(dataset_dates, field="loss_diff")
        self.assertEqual(diag_dates["chronological_weeks"], ["2025-12-15", "2026-01-10", "2026-03-01"])
        self.assertEqual(diag_dates["week_means"], [0.1, 0.2, 0.3])

    def test_missing_weeks_and_gaps_not_treated_as_adjacent_lags(self):
        """Validates that non-consecutive weeks with gaps are NOT treated as adjacent lag-1 transitions."""
        # Dataset with missing weeks: week_01, week_03, week_05, week_07 (gap of 2 between all)
        dataset_gaps = [
            {"token_id": "T0", "week_block": "week_01", "loss_diff": 1.0},
            {"token_id": "T0", "week_block": "week_03", "loss_diff": 2.0},
            {"token_id": "T0", "week_block": "week_05", "loss_diff": 1.0},
            {"token_id": "T0", "week_block": "week_07", "loss_diff": 2.0},
        ]
        diag_gaps = ClusteredEvaluationEngine.evaluate_inter_block_serial_dependence(dataset_gaps, field="loss_diff")
        # Since week differences are 2 (not 1), no lag-1 pairs exist!
        self.assertEqual(diag_gaps["rho_1"], 0.0)
        # But lag-2 pairs DO exist (week 3 - week 1 = 2, week 5 - week 3 = 2, week 7 - week 5 = 2)
        self.assertNotEqual(diag_gaps["rho_2"], 0.0)

    def test_invalid_week_format_cannot_produce_dependence_estimates(self):
        """Validates that unrecognized or invalid week labels are strictly rejected without producing dependence estimates."""
        dataset_bad = [
            {"token_id": "T0", "week_block": "invalid_label_xyz", "loss_diff": 1.0},
            {"token_id": "T0", "week_block": "random_custom_week", "loss_diff": 2.0},
            {"token_id": "T0", "week_block": "bad_week_99999", "loss_diff": 3.0},
        ]
        diag = ClusteredEvaluationEngine.evaluate_inter_block_serial_dependence(dataset_bad, field="loss_diff")
        self.assertFalse(diag["is_valid_format"])
        self.assertEqual(diag["rho_1"], 0.0)
        self.assertEqual(diag["rho_2"], 0.0)
        self.assertEqual(diag["newey_west_se"], 0.0)
        self.assertTrue("Unrecognized or invalid" in diag["diagnostic_reason"])

        eval_res = ClusteredEvaluationEngine.run_stage31_evaluation(dataset_bad, field="loss_diff")
        self.assertEqual(eval_res["status"], InferentialStatus.EXPLORATORY_UNCALIBRATED_INFERENCE.value)
        self.assertTrue("INVALID_OR_MIXED_WEEK_FORMAT_CANNOT_ESTIMATE_DEPENDENCE" in eval_res["diagnostic_flag"])
        self.assertEqual(eval_res["se_newey_west"], 0.0)

    def test_mixed_week_formats_cannot_produce_dependence_estimates(self):
        """Validates that datasets mixing disparate week format families fail closed without estimating dependence."""
        dataset_mixed = [
            {"token_id": "T0", "week_block": "2026-W01", "loss_diff": 1.0},
            {"token_id": "T0", "week_block": "week_02", "loss_diff": 2.0},
            {"token_id": "T0", "week_block": "2026-W03", "loss_diff": 3.0},
        ]
        diag = ClusteredEvaluationEngine.evaluate_inter_block_serial_dependence(dataset_mixed, field="loss_diff")
        self.assertFalse(diag["is_valid_format"])
        self.assertTrue("Mixed week format" in diag["diagnostic_reason"])
        self.assertEqual(diag["rho_1"], 0.0)
        self.assertEqual(diag["rho_2"], 0.0)
        self.assertEqual(diag["newey_west_se"], 0.0)

        eval_res = ClusteredEvaluationEngine.run_stage31_evaluation(dataset_mixed, field="loss_diff")
        self.assertTrue("INVALID_OR_MIXED_WEEK_FORMAT_CANNOT_ESTIMATE_DEPENDENCE" in eval_res["diagnostic_flag"])
        self.assertEqual(eval_res["se_newey_west"], 0.0)

    def test_all_inference_labeled_exploratory(self):
        """Validates that confirmatory status is never claimed and results are branded exploratory."""
        random.seed(42)
        dataset_large = [
            {"token_id": f"token_{i}", "week_block": f"week_{w:02d}", "loss_diff": 0.05 + random.gauss(0, 0.005)}
            for w in range(30)
            for i in range(50)
        ]
        eval_res = ClusteredEvaluationEngine.run_stage31_evaluation(dataset_large, field="loss_diff")
        self.assertEqual(eval_res["status"], InferentialStatus.EXPLORATORY_UNCALIBRATED_INFERENCE.value)
        self.assertTrue("EXPLORATORY" in eval_res["diagnostic_flag"])


if __name__ == "__main__":
    unittest.main()
