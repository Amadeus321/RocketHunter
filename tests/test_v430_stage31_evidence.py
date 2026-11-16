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

import dataclasses
import decimal
import hashlib
import math
import random
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

from scripts.research_evidence import (
    APPROVED_BASELINE_COMMIT,
    CANONICAL_OP_COMMIT,
    CANONICAL_OP_TAG,
    TECHNICAL_TP_PCT,
    UNISWAP_V3_FEE_SPACINGS,
    BlockHeaderTrustAnchor,
    BlockHeaderTrustAnchorRegistry,
    ClusteredEvaluationEngine,
    DatasetAReconciliationEngine,
    DualTrackOutcomeEvaluator,
    EvidenceTier,
    FinalityState,
    InferentialStatus,
    JournalEntryPosition,
    JournalExitExecution,
    LiquidityObservationRecord,
    LiquidityClassificationEngine,
    LiquidityClassificationStatus,
    ModeledExecutionStatus,
    MultiChainFinalityEngine,
    OpStackOutputProofEvidence,
    OpStackOutputProofPayload,
    OpStackOutputProofVerifier,
    OpStackAuthenticatedProofResult,
    OnChainTransferEvent,
    ProtectedDataVerifier,
    RpcEvidenceRecord,
    RpcProvenanceVerifier,
    UniswapV2Evidence,
    UniswapV3Evidence,
    UniswapV3TickMath,
    ValidatedTransactionReceipt,
    canonicalize_token_address,
)
from scripts.consensus_proof_verifier import (
    keccak256,
    encode_compact_path,
    rlp_encode,
    rlp_encode_bytes,
    AccountStateLeaf,
    HeaderConsensusEvidence,
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

    def test_matching_malformed_hashes_fail_closed(self):
        """Validates that matching but malformed, None, or non-hex digests fail closed."""
        # 1. Matching malformed/non-hex string (e.g. "BADHASH")
        snap_malformed_1 = {
            "protected_files": {"user_positions_journal.json": {"sha256": "BADHASH", "size_bytes": 10}},
            "scanner_memory_partitions": {},
            "config_files": {},
        }
        snap_malformed_2 = {
            "protected_files": {"user_positions_journal.json": {"sha256": "BADHASH", "size_bytes": 10}},
            "scanner_memory_partitions": {},
            "config_files": {},
        }
        is_clean, disc = self.verifier.verify_clean_equivalence(snap_malformed_1, snap_malformed_2)
        self.assertFalse(is_clean)
        self.assertTrue(any("Malformed or invalid SHA-256 digest" in d for d in disc))

        # 2. Matching None hash
        snap_none_1 = {
            "protected_files": {"user_positions_journal.json": {"sha256": None, "size_bytes": 10}},
            "scanner_memory_partitions": {},
            "config_files": {},
        }
        snap_none_2 = {
            "protected_files": {"user_positions_journal.json": {"sha256": None, "size_bytes": 10}},
            "scanner_memory_partitions": {},
            "config_files": {},
        }
        is_clean, disc = self.verifier.verify_clean_equivalence(snap_none_1, snap_none_2)
        self.assertFalse(is_clean)
        self.assertTrue(any("Malformed or invalid SHA-256 digest" in d for d in disc))

        # 3. Matching truncated 32-char hex (e.g. MD5 length instead of 64-char SHA-256)
        snap_md5_1 = {
            "protected_files": {"user_positions_journal.json": {"sha256": "a" * 32, "size_bytes": 10}},
            "scanner_memory_partitions": {},
            "config_files": {},
        }
        snap_md5_2 = {
            "protected_files": {"user_positions_journal.json": {"sha256": "a" * 32, "size_bytes": 10}},
            "scanner_memory_partitions": {},
            "config_files": {},
        }
        is_clean, disc = self.verifier.verify_clean_equivalence(snap_md5_1, snap_md5_2)
        self.assertFalse(is_clean)
        self.assertTrue(any("Malformed or invalid SHA-256 digest" in d for d in disc))


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

        # Defender win finalized (elapsed delay -> caller-asserted unverified, not L2_OUTPUT_FINALIZED_CANONICAL)
        status_final = MultiChainFinalityEngine.evaluate_op_stack(
            l1_batch_finalized=True,
            proposal_game_type=1,
            respected_game_type=1,
            game_status=2,
            resolved_at=1000,
            current_l1_timestamp=1000 + 302400,
            dispute_game_finality_delay_seconds=302400,
        )
        self.assertEqual(status_final, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)

    def test_direct_multichain_finality_engine_downgrades_to_unverified(self):
        """Validates that direct evaluation on Arbitrum, OP Stack, and Solana NEVER returns canonical finality."""
        # Arbitrum Nitro: elapsed >= confirm_period_blocks -> FINALITY_CALLER_ASSERTED_UNVERIFIED
        arb_state = MultiChainFinalityEngine.evaluate_arbitrum(
            l1_batch_finalized=True,
            assertion_created_l1_block=1000,
            current_finalized_l1_block=1000 + 50000,
            confirm_period_blocks=45818,
            challenge_status="NONE",
        )
        self.assertEqual(arb_state, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)
        self.assertNotEqual(arb_state, FinalityState.FINALIZED_CANONICAL)

        # OP Stack: defender wins and delay elapsed -> FINALITY_CALLER_ASSERTED_UNVERIFIED
        op_state = MultiChainFinalityEngine.evaluate_op_stack(
            l1_batch_finalized=True,
            proposal_game_type=1,
            respected_game_type=1,
            game_status=2,  # DEFENDER_WINS
            resolved_at=1000,
            current_l1_timestamp=1000 + 400000,
            dispute_game_finality_delay_seconds=302400,
        )
        self.assertEqual(op_state, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)
        self.assertNotEqual(op_state, FinalityState.L2_OUTPUT_FINALIZED_CANONICAL)
        self.assertNotEqual(op_state, FinalityState.FINALIZED_CANONICAL)

        # Solana: finalized commitment and rooted -> FINALITY_CALLER_ASSERTED_UNVERIFIED
        sol_state = MultiChainFinalityEngine.evaluate_solana(
            commitment="finalized",
            is_rooted=True,
        )
        self.assertEqual(sol_state, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)
        self.assertNotEqual(sol_state, FinalityState.FINALIZED_CANONICAL)


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

    def test_invalid_iso_week_53_fails_closed_without_raising(self):
        """Regression: 2021 has only 52 ISO weeks; 2021-W53 must return ('INVALID', None)
        and the dataset validator must fail closed rather than raising ValueError from date.fromisocalendar().
        """
        # Direct parsing
        fmt, idx = ClusteredEvaluationEngine.classify_and_parse_week("2021-W53")
        self.assertEqual(fmt, "INVALID")
        self.assertIsNone(idx)

        # Contrast with a valid ISO week 53 year (e.g. 2020 has 53 ISO weeks)
        fmt_2020, idx_2020 = ClusteredEvaluationEngine.classify_and_parse_week("2020-W53")
        self.assertEqual(fmt_2020, "ISO_CALENDAR")
        self.assertIsNotNone(idx_2020)

        # Dataset validator regression: must fail closed rather than raising ValueError
        dataset_invalid_week53 = [
            {"token_id": "T0", "week_block": "2021-W52", "loss_diff": 0.1},
            {"token_id": "T0", "week_block": "2021-W53", "loss_diff": 0.2},
            {"token_id": "T0", "week_block": "2022-W01", "loss_diff": 0.3},
        ]
        is_valid, fmt_fam, blocks, reason = ClusteredEvaluationEngine.validate_and_classify_dataset_weeks(
            [x["week_block"] for x in dataset_invalid_week53]
        )
        self.assertFalse(is_valid)
        self.assertEqual(fmt_fam, "INVALID")
        self.assertTrue("2021-W53" in reason)

        # Full pipeline evaluation handles safely
        diag = ClusteredEvaluationEngine.evaluate_inter_block_serial_dependence(dataset_invalid_week53, field="loss_diff")
        self.assertFalse(diag["is_valid_format"])
        self.assertEqual(diag["rho_1"], 0.0)
        self.assertEqual(diag["rho_2"], 0.0)

    def test_same_week_alias_normalization_and_grouping(self):
        """Validates that labels referring to the same calendar week (e.g. 2026-W01 and 2026-01-01)
        are grouped by parsed week index and do not inflate B_weeks or distort week means.
        """
        dataset = [
            {"token_id": "T0", "week_block": "2026-W01", "loss_diff": 0.10},
            {"token_id": "T1", "week_block": "2026-01-01", "loss_diff": 0.20},  # Same calendar week as 2026-W01
            {"token_id": "T0", "week_block": "2026-W02", "loss_diff": 0.30},
            {"token_id": "T1", "week_block": "2026-W02", "loss_diff": 0.30},
            {"token_id": "T0", "week_block": "2026-W03", "loss_diff": 0.50},
            {"token_id": "T1", "week_block": "2026-W03", "loss_diff": 0.50},
        ]
        # Counts must reflect B=3 weeks, not 4
        counts = ClusteredEvaluationEngine.get_structural_counts(dataset)
        self.assertEqual(counts["B_weeks"], 3)
        self.assertEqual(counts["N_obs"], 6)

        diag = ClusteredEvaluationEngine.evaluate_inter_block_serial_dependence(dataset, field="loss_diff")
        self.assertEqual(len(diag["chronological_weeks"]), 3)
        self.assertEqual(len(diag["week_means"]), 3)
        # Week 1 mean should average 0.10 and 0.20 -> 0.15
        self.assertAlmostEqual(diag["week_means"][0], 0.15, places=6)
        self.assertAlmostEqual(diag["week_means"][1], 0.30, places=6)
        self.assertAlmostEqual(diag["week_means"][2], 0.50, places=6)

    def test_explicit_timestamp_unit_seconds_vs_milliseconds(self):
        """Validates that explicit units ('s' and 'ms') map identical timestamps to the same week index,
        while ambiguous bare numeric timestamps without explicit units are strictly rejected.
        """
        # 1767225600 is 2026-01-01 00:00:00 UTC (week of 2026-W01)
        # In seconds: 1767225600s
        # In milliseconds: 1767225600000ms
        fmt_s, idx_s = ClusteredEvaluationEngine.classify_and_parse_week("1767225600s")
        fmt_ms, idx_ms = ClusteredEvaluationEngine.classify_and_parse_week("1767225600000ms")
        self.assertEqual(fmt_s, "UNIX_TIMESTAMP")
        self.assertEqual(fmt_ms, "UNIX_TIMESTAMP")
        self.assertEqual(idx_s, idx_ms)

        # Ambiguous bare numbers without unit must fail closed as INVALID
        fmt_ambig_s, idx_ambig_s = ClusteredEvaluationEngine.classify_and_parse_week("1767225600")
        fmt_ambig_ms, idx_ambig_ms = ClusteredEvaluationEngine.classify_and_parse_week("1767225600000")
        self.assertEqual(fmt_ambig_s, "INVALID")
        self.assertIsNone(idx_ambig_s)
        self.assertEqual(fmt_ambig_ms, "INVALID")
        self.assertIsNone(idx_ambig_ms)

        # Datasets with ambiguous bare timestamps must fail closed without estimating dependence
        dataset_ambig = [
            {"token_id": "T0", "week_block": "1767225600", "loss_diff": 0.1},
            {"token_id": "T0", "week_block": "1767830400", "loss_diff": 0.2},
            {"token_id": "T0", "week_block": "1768435200", "loss_diff": 0.3},
        ]
        diag = ClusteredEvaluationEngine.evaluate_inter_block_serial_dependence(dataset_ambig, field="loss_diff")
        self.assertFalse(diag["is_valid_format"])
        self.assertTrue("Ambiguous numeric timestamp" in diag["diagnostic_reason"])

    def test_format_validation_before_b_less_than_3_early_return(self):
        """Validates that short datasets (B < 3 or N < 3) with invalid or mixed formats
        retain the correct invalid/mixed diagnostic rather than masking it with B_WEEKS_LESS_THAN_3.
        """
        # Short dataset with invalid format (N=2, B=2)
        dataset_short_invalid = [
            {"token_id": "T0", "week_block": "invalid_week_foo", "loss_diff": 0.1},
            {"token_id": "T0", "week_block": "invalid_week_bar", "loss_diff": 0.2},
        ]
        diag_inv = ClusteredEvaluationEngine.evaluate_inter_block_serial_dependence(dataset_short_invalid, field="loss_diff")
        self.assertFalse(diag_inv["is_valid_format"])
        self.assertTrue("Unrecognized or invalid" in diag_inv["diagnostic_reason"])

        # Short dataset with mixed format (N=2)
        dataset_short_mixed = [
            {"token_id": "T0", "week_block": "2026-W01", "loss_diff": 0.1},
            {"token_id": "T0", "week_block": "week_02", "loss_diff": 0.2},
        ]
        diag_mix = ClusteredEvaluationEngine.evaluate_inter_block_serial_dependence(dataset_short_mixed, field="loss_diff")
        self.assertFalse(diag_mix["is_valid_format"])
        self.assertTrue("Mixed week format" in diag_mix["diagnostic_reason"])

        # Short dataset with valid format (N=2, B=2) correctly reports B_WEEKS_LESS_THAN_3
        dataset_short_valid = [
            {"token_id": "T0", "week_block": "2026-W01", "loss_diff": 0.1},
            {"token_id": "T0", "week_block": "2026-W02", "loss_diff": 0.2},
        ]
        diag_val = ClusteredEvaluationEngine.evaluate_inter_block_serial_dependence(dataset_short_valid, field="loss_diff")
        self.assertTrue(diag_val["is_valid_format"])
        self.assertEqual(diag_val["diagnostic_reason"], "B_WEEKS_LESS_THAN_3")

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


class TestStage31IndependentAuditCorrections(unittest.TestCase):
    """Test suite for Phase 3 audit corrections covering requirements D, E, F, J."""

    def test_rpc_provenance_verifier_secure_filesystem_paths(self):
        """Requirement D: Validate positive integers, reject path traversal, and create collision-safe quarantine files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            verifier = RpcProvenanceVerifier(data_dir=Path(tmpdir))
            valid_rec = RpcEvidenceRecord(
                rpc_method="eth_getBlockByNumber",
                rpc_params=[19000000, False],
                retrieval_timestamp_utc="2026-11-15T00:00:00Z",
                tls_handshake_metadata={"alpn": "h2"},
                chain_id=1,
                block_number=19_000_000,
                block_hash="0x" + "aa" * 32,
                parent_hash="0x" + "bb" * 32,
                state_root="0x" + "cc" * 32,
                provider_id="alchemy_primary",
                provider_domain="eth-mainnet.g.alchemy.com",
                raw_request_bytes=b'{"jsonrpc":"2.0","id":1,"method":"eth_getBlockByNumber"}',
                raw_response_bytes=b'{"jsonrpc":"2.0","id":1,"result":{}}',
                expected_request_sha256=hashlib.sha256(b'{"jsonrpc":"2.0","id":1,"method":"eth_getBlockByNumber"}').hexdigest().upper(),
                expected_response_sha256=hashlib.sha256(b'{"jsonrpc":"2.0","id":1,"result":{}}').hexdigest().upper(),
            )

            # 1. Normal persistence works
            req_p, resp_p = verifier.persist_raw_wire_payloads(valid_rec)
            self.assertTrue(req_p.is_file())
            self.assertTrue(resp_p.is_file())

            # 2. Non-integer chain_id rejected (e.g. boolean, string)
            bad_rec_bool = dataclasses.replace(valid_rec, chain_id=True)
            with self.assertRaises(ValueError):
                verifier.persist_raw_wire_payloads(bad_rec_bool)

            # 3. Negative chain_id rejected
            bad_rec_neg = dataclasses.replace(valid_rec, chain_id=-1)
            with self.assertRaises(ValueError):
                verifier.persist_raw_wire_payloads(bad_rec_neg)

            # 4. Zero block_number rejected
            bad_rec_zero = dataclasses.replace(valid_rec, block_number=0)
            with self.assertRaises(ValueError):
                verifier.persist_raw_wire_payloads(bad_rec_zero)

            # 5. Requirement D: Quarantine creates genuinely immutable collision-safe manifests
            # Exclusive creation preserves prior artifacts across repeated writes with identical block hashes and reasons
            for _ in range(5):
                q_dest = verifier.quarantine_reorganized_block(valid_rec, reason="Reorg detected")

            self.assertTrue(q_dest.is_dir())
            self.assertTrue((q_dest / "quarantine_manifest.json").is_file())
            # All 5 unique quarantine files must exist simultaneously with no collisions or overwrites
            immutable_files = list(q_dest.glob("quarantine_0x*.json"))
            self.assertEqual(len(immutable_files), 5)
            # Ensure unique filenames
            self.assertEqual(len(set(f.name for f in immutable_files)), 5)
            # Verify history log recorded all 5 events
            history_file = q_dest / "quarantine_history.jsonl"
            self.assertTrue(history_file.is_file())
            history_lines = [l for l in history_file.read_text(encoding="utf-8").strip().splitlines() if l.strip()]
            self.assertEqual(len(history_lines), 5)

    def test_liquidity_classification_hardened_adversarial(self):
        """Requirement E: Harden liquidity classification with NaN/inf rejection, initial checks, and permanent collapse."""
        t0 = "2026-11-15T00:00:00Z"
        base_record = LiquidityObservationRecord(
            chain_id=1,
            token_mint="0x" + "22" * 20,
            pool_address="0x" + "11" * 20,
            block_number=19_000_000,
            block_hash="0x" + "aa" * 32,
            source_event_timestamp_utc="2026-11-15T00:10:00Z",
            local_ingestion_timestamp_utc="2026-11-15T00:10:05Z",
            d_ask_plus_2pct=100_000,
            d_bid_minus_2pct=100_000,
            liquidity_usd=1_000_000.0,
        )

        # 1. NaN in liquidity_usd rejected
        rec_nan = dataclasses.replace(base_record, liquidity_usd=float("nan"))
        res_nan = LiquidityClassificationEngine.evaluate_liquidity_trajectory([rec_nan], t0)
        self.assertEqual(res_nan["status"], LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value)
        self.assertEqual(res_nan["diagnostic"], "NON_FINITE_OR_NEGATIVE_LIQUIDITY_REJECTED")

        # 2. Negative depth rejected
        rec_neg = dataclasses.replace(base_record, d_ask_plus_2pct=-50)
        res_neg = LiquidityClassificationEngine.evaluate_liquidity_trajectory([rec_neg], t0)
        self.assertEqual(res_neg["status"], LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value)
        self.assertEqual(res_neg["diagnostic"], "NON_FINITE_OR_NEGATIVE_LIQUIDITY_REJECTED")

        # 3. Initial liquidity <= 0 rejected
        rec_zero_init = dataclasses.replace(base_record, liquidity_usd=0.0)
        res_zero = LiquidityClassificationEngine.evaluate_liquidity_trajectory([rec_zero_init], t0)
        self.assertEqual(res_zero["status"], LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value)
        self.assertEqual(res_zero["diagnostic"], "INITIAL_LIQUIDITY_NON_POSITIVE")

        # 4. Permanent collapse: collapse occurs mid-window, recovery occurs later, status remains collapsed
        rec_mid_collapse = dataclasses.replace(
            base_record,
            source_event_timestamp_utc="2026-11-15T06:00:00Z",
            d_ask_plus_2pct=10_000,   # 10% of initial (collapse < 20%)
            d_bid_minus_2pct=10_000,
            liquidity_usd=100_000.0,
        )
        rec_recovered_terminal = dataclasses.replace(
            base_record,
            source_event_timestamp_utc="2026-11-15T23:50:00Z",
            d_ask_plus_2pct=90_000,   # 90% (recovered)
            d_bid_minus_2pct=90_000,
            liquidity_usd=900_000.0,
        )
        res_collapse_recovery = LiquidityClassificationEngine.evaluate_liquidity_trajectory(
            [base_record, rec_mid_collapse, rec_recovered_terminal], t0
        )
        self.assertEqual(
            res_collapse_recovery["status"],
            LiquidityClassificationStatus.LIQUIDITY_DEPTH_COLLAPSE_OBSERVED_UNVERIFIED.value,
        )
        self.assertTrue(res_collapse_recovery["recovery_observed_after_collapse"])

    def test_chain_specific_token_address_normalization(self):
        """Requirement F: EVM addresses normalized to lowercase hex; Solana addresses strictly preserve Base58 case."""
        # 1. EVM address normalization (lowercase)
        evm_mixed = "0x" + "AbCdEf" * 6 + "123456"
        self.assertEqual(canonicalize_token_address(evm_mixed, chain=1), evm_mixed.lower())

        # 2. EVM with namespace prefix
        self.assertEqual(canonicalize_token_address(f"ethereum:{evm_mixed}"), evm_mixed.lower())

        # 3. Solana address strictly preserves case
        sol_mint = "So11111111111111111111111111111111111111112"
        self.assertEqual(canonicalize_token_address(sol_mint, chain=101), sol_mint)
        self.assertEqual(canonicalize_token_address(f"solana:{sol_mint}"), sol_mint)

        # 4. Differing case Solana addresses do NOT collide
        sol_mint_lower = "so11111111111111111111111111111111111111112"
        self.assertNotEqual(
            canonicalize_token_address(sol_mint, chain="solana"),
            canonicalize_token_address(sol_mint_lower, chain="solana"),
        )

        # 5. Invalid Solana Base58 characters (e.g. '0', 'O', 'I', 'l') raise ValueError
        with self.assertRaises(ValueError):
            canonicalize_token_address("solana:00000000000000000000000000000000000000000000")

        # 6. Invalid EVM hex characters raise ValueError
        with self.assertRaises(ValueError):
            canonicalize_token_address("0xZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ")

    @mock.patch("scripts.consensus_proof_verifier.verify_bls_aggregate_signature", return_value=True)
    def test_op_stack_withdrawal_maturity_delays(self, mock_bls):
        """Requirement J: Evaluate OP Stack withdrawal maturity delay and authentication."""
        t_proven = 1_000_000
        delay = 604_800  # 7 days

        # 1. Future timestamp (current < proven) -> FINALITY_PENDING
        st_future = MultiChainFinalityEngine.evaluate_op_stack_withdrawal(
            proven_withdrawal_timestamp=t_proven,
            current_l1_timestamp=t_proven - 100,
            proof_maturity_delay_seconds=delay,
        )
        self.assertEqual(st_future, FinalityState.FINALITY_PENDING)

        # 2. Pending maturity (current in delay airgap) -> WITHDRAWAL_PENDING_MATURITY
        st_pending = MultiChainFinalityEngine.evaluate_op_stack_withdrawal(
            proven_withdrawal_timestamp=t_proven,
            current_l1_timestamp=t_proven + 300_000,
            proof_maturity_delay_seconds=delay,
        )
        self.assertEqual(st_pending, FinalityState.WITHDRAWAL_PENDING_MATURITY)

        # 3. Delay elapsed without cryptographic output proof -> FINALITY_CALLER_ASSERTED_UNVERIFIED
        st_elapsed_unauth = MultiChainFinalityEngine.evaluate_op_stack_withdrawal(
            proven_withdrawal_timestamp=t_proven,
            current_l1_timestamp=t_proven + delay + 100,
            proof_maturity_delay_seconds=delay,
            has_authenticated_output_proof=False,
        )
        self.assertEqual(st_elapsed_unauth, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)

        # 4. Requirement J: Delay elapsed with cryptographically authenticated output proof -> WITHDRAWAL_PROOF_MATURE_FINAL
        w_hash = "0x" + "11" * 32
        slot0 = (0).to_bytes(32, "big")
        search_key = keccak256(bytes.fromhex(w_hash[2:]) + slot0)
        nibbles = []
        for b in search_key:
            nibbles.extend([b >> 4, b & 0x0F])
        compact = encode_compact_path(nibbles, is_leaf=True)
        leaf = [compact, b"\x01"]
        leaf_rlp = rlp_encode(leaf)
        mp_root_bytes = keccak256(leaf_rlp)
        mp_root = "0x" + mp_root_bytes.hex()
        s_root = "0x" + "22" * 32
        b_hash = "0x" + "33" * 32
        preimage = b"\x00" + bytes.fromhex(s_root[2:]) + mp_root_bytes + bytes.fromhex(b_hash[2:])
        output_root = "0x" + keccak256(preimage).hex()

        # Construct L1 Oracle Storage Proof
        out_root_bytes = bytes.fromhex(output_root[2:])
        mapping_slot = 3
        storage_key = keccak256((42).to_bytes(32, "big") + mapping_slot.to_bytes(32, "big"))
        nibbles_storage = []
        for b in storage_key:
            nibbles_storage.extend([b >> 4, b & 0x0F])
        compact_storage = encode_compact_path(nibbles_storage, is_leaf=True)
        storage_leaf_rlp = rlp_encode([compact_storage, rlp_encode_bytes(out_root_bytes)])
        oracle_storage_root = keccak256(storage_leaf_rlp)

        # Construct L1 Oracle Account Proof
        oracle_addr = "0x56315b90c40730925ec54a5a02b662791cb298f5"
        oracle_acct_key = keccak256(bytes.fromhex(oracle_addr[2:]))
        nonce_bytes = (1).to_bytes(1, "big")
        balance_bytes = b""
        code_hash = keccak256(b"code")
        acct_val_rlp = rlp_encode([nonce_bytes, balance_bytes, oracle_storage_root, code_hash])
        nibbles_acct = []
        for b in oracle_acct_key:
            nibbles_acct.extend([b >> 4, b & 0x0F])
        compact_acct = encode_compact_path(nibbles_acct, is_leaf=True)
        acct_leaf_rlp = rlp_encode([compact_acct, acct_val_rlp])
        l1_state_root = "0x" + keccak256(acct_leaf_rlp).hex()

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

        def _create_verified_anchor(b_num, b_hash_str, s_root_str, c_id=1):
            b_hash_bytes = bytes.fromhex(b_hash_str[2:])
            s_root_bytes = bytes.fromhex(s_root_str[2:])
            ep_root, sr_br = _make_ssz_branch(s_root_bytes, 34)
            body_root, ep_br = _make_ssz_branch(ep_root, 25)
            pubkeys_512 = [hashlib.sha256(f"pk_{i}".encode()).digest() + hashlib.sha256(f"ext_{i}".encode()).digest()[:16] for i in range(512)]
            sync_root = bytes.fromhex("98d69927b32c3e0bd9f091f2f728fb95aeec620d2b5f04390465e65cd5d00b5c")
            fin_root = bytes.fromhex("33" * 32)
            beacon_state_root, fin_br = _make_ssz_branch(fin_root, 105)
            ev = HeaderConsensusEvidence(
                slot=9_000_000,
                proposer_index=1,
                parent_root=bytes.fromhex("22" * 32),
                beacon_state_root=beacon_state_root,
                body_root=body_root,
                execution_payload_root=ep_root,
                execution_block_hash=b_hash_bytes,
                execution_block_number=b_num,
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
            return BlockHeaderTrustAnchor(
                chain_id=c_id,
                block_number=b_num,
                block_hash=b_hash_str,
                state_root=s_root_str,
                header_timestamp_utc="2026-11-15T00:00:00Z",
                consensus_mechanism="PROOF_OF_STAKE",
                parent_hash="0x" + "22" * 32,
                receipts_root="0x" + "11" * 32,
                finality_status="FINALIZED_CANONICAL",
                trust_source="ETH_CONSENSUS_SYNC_COMMITTEE",
                verification_seal="",
                is_authenticated=True,
                consensus_evidence=ev,
            )

        # Register L1 Trust Anchor
        l1_b_hash = "0x" + "55" * 32
        l1_anchor = _create_verified_anchor(19_500_000, l1_b_hash, l1_state_root, c_id=1)
        with mock.patch("scripts.consensus_proof_verifier.verify_bls_aggregate_signature", return_value=True):
            BlockHeaderTrustAnchorRegistry.register(l1_anchor)

        valid_op_payload = OpStackOutputProofPayload(
            withdrawal_hash=w_hash,
            l2_output_index=42,
            l2_block_number=12345678,
            l2_block_hash=b_hash,
            l2_state_root=s_root,
            l2_message_passer_storage_root=mp_root,
            l2_message_passer_proof=[leaf_rlp],
            l1_oracle_address=oracle_addr,
            l1_block_number=19_500_000,
            l1_block_hash=l1_b_hash,
            l1_oracle_account_proof=[acct_leaf_rlp],
            l1_output_storage_proof=[storage_leaf_rlp],
            l2_chain_id=8453,
            version_byte=b"\x00",
            l1_output_slot=3,
            l1_oracle_output_root=output_root,
        )

        ok, valid_op_proof, err = OpStackOutputProofVerifier.verify_withdrawal_proof(
            valid_op_payload, l1_trust_anchor=l1_anchor
        )
        self.assertTrue(ok)
        self.assertIsNotNone(valid_op_proof)

        st_elapsed_auth = MultiChainFinalityEngine.evaluate_op_stack_withdrawal(
            proven_withdrawal_timestamp=t_proven,
            current_l1_timestamp=t_proven + delay + 100,
            proof_maturity_delay_seconds=delay,
            output_proof_evidence=valid_op_proof,
            expected_withdrawal_hash=w_hash,
            expected_output_root=output_root,
            l1_trust_anchor=l1_anchor,
        )
        self.assertEqual(st_elapsed_auth, FinalityState.WITHDRAWAL_PROOF_MATURE_FINAL)

        # Direct payload verification also produces WITHDRAWAL_PROOF_MATURE_FINAL
        st_elapsed_payload = MultiChainFinalityEngine.evaluate_op_stack_withdrawal(
            proven_withdrawal_timestamp=t_proven,
            current_l1_timestamp=t_proven + delay + 100,
            proof_maturity_delay_seconds=delay,
            output_proof_payload=valid_op_payload,
            expected_withdrawal_hash=w_hash,
            expected_output_root=output_root,
            l1_trust_anchor=l1_anchor,
        )
        self.assertEqual(st_elapsed_payload, FinalityState.WITHDRAWAL_PROOF_MATURE_FINAL)

        # 5. Adversarial checks for Requirement J:
        # 5a. Caller-constructed unsealed dataclass alone MUST NOT promote to verified finality
        unsealed_evidence = OpStackOutputProofEvidence(
            withdrawal_hash=w_hash,
            l2_output_index=42,
            l2_block_number=12345678,
            output_root=output_root,
            l1_oracle_output_root=output_root,
            is_valid=True,
        )
        st_unsealed = MultiChainFinalityEngine.evaluate_op_stack_withdrawal(
            proven_withdrawal_timestamp=t_proven,
            current_l1_timestamp=t_proven + delay + 100,
            proof_maturity_delay_seconds=delay,
            output_proof_evidence=unsealed_evidence,
        )
        self.assertEqual(st_unsealed, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)

        # 5b. Forged HMAC seal on OpStackAuthenticatedProofResult is rejected
        forged_result = OpStackAuthenticatedProofResult(
            withdrawal_hash=w_hash,
            l2_output_index=42,
            l2_block_number=12345678,
            output_root=output_root,
            l1_oracle_output_root=output_root,
            is_valid=True,
            verification_seal="forged_seal_00000000000000000000000000000000000000000000000000000000",
        )
        st_forged_seal = MultiChainFinalityEngine.evaluate_op_stack_withdrawal(
            proven_withdrawal_timestamp=t_proven,
            current_l1_timestamp=t_proven + delay + 100,
            proof_maturity_delay_seconds=delay,
            output_proof_evidence=forged_result,
        )
        self.assertEqual(st_forged_seal, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)

        # 5c. Caller-asserted Boolean flag `has_authenticated_output_proof=True` alone is strictly rejected
        st_forged_bool = MultiChainFinalityEngine.evaluate_op_stack_withdrawal(
            proven_withdrawal_timestamp=t_proven,
            current_l1_timestamp=t_proven + delay + 100,
            proof_maturity_delay_seconds=delay,
            has_authenticated_output_proof=True,
        )
        self.assertEqual(st_forged_bool, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)

        # 5d. Caller passing output_proof_evidence=True (Boolean) is rejected
        st_bool_evidence = MultiChainFinalityEngine.evaluate_op_stack_withdrawal(
            proven_withdrawal_timestamp=t_proven,
            current_l1_timestamp=t_proven + delay + 100,
            proof_maturity_delay_seconds=delay,
            output_proof_evidence=True,  # type: ignore
        )
        self.assertEqual(st_bool_evidence, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)

        # 5e. Proof with is_valid=False is rejected
        proof_invalid = dataclasses.replace(valid_op_proof, is_valid=False)
        st_proof_invalid = MultiChainFinalityEngine.evaluate_op_stack_withdrawal(
            proven_withdrawal_timestamp=t_proven,
            current_l1_timestamp=t_proven + delay + 100,
            proof_maturity_delay_seconds=delay,
            output_proof_evidence=proof_invalid,
        )
        self.assertEqual(st_proof_invalid, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)

        # 5f. Mismatched output root rejected
        st_root_mismatch = MultiChainFinalityEngine.evaluate_op_stack_withdrawal(
            proven_withdrawal_timestamp=t_proven,
            current_l1_timestamp=t_proven + delay + 100,
            proof_maturity_delay_seconds=delay,
            output_proof_evidence=valid_op_proof,
            expected_output_root="0x" + "ff" * 32,
        )
        self.assertEqual(st_root_mismatch, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)

        # 5g. Mismatched withdrawal identity rejected
        st_id_mismatch = MultiChainFinalityEngine.evaluate_op_stack_withdrawal(
            proven_withdrawal_timestamp=t_proven,
            current_l1_timestamp=t_proven + delay + 100,
            proof_maturity_delay_seconds=delay,
            output_proof_evidence=valid_op_proof,
            expected_withdrawal_hash="0x" + "ee" * 32,
        )
        self.assertEqual(st_id_mismatch, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)

        # 5h. Output root does not match L1 oracle root rejected
        proof_oracle_mismatch = dataclasses.replace(valid_op_proof, l1_oracle_output_root="0x" + "dd" * 32)
        st_oracle_mismatch = MultiChainFinalityEngine.evaluate_op_stack_withdrawal(
            proven_withdrawal_timestamp=t_proven,
            current_l1_timestamp=t_proven + delay + 100,
            proof_maturity_delay_seconds=delay,
            output_proof_evidence=proof_oracle_mismatch,
        )
        self.assertEqual(st_oracle_mismatch, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)

        # 5i. Mandatory L1 Oracle Account or Storage proof missing fails closed
        p_missing_l1 = dataclasses.replace(valid_op_payload, l1_oracle_account_proof=[])
        ok_l1_miss, _, err_l1 = OpStackOutputProofVerifier.verify_withdrawal_proof(p_missing_l1, l1_trust_anchor=l1_anchor)
        self.assertFalse(ok_l1_miss)
        self.assertIn("MANDATORY_L1_ORACLE_PROOF_MISSING", err_l1)

        # 5j. Mandatory L2 message passer proof missing fails closed
        p_missing_l2 = dataclasses.replace(valid_op_payload, l2_message_passer_proof=[])
        ok_l2_miss, _, err_l2 = OpStackOutputProofVerifier.verify_withdrawal_proof(p_missing_l2, l1_trust_anchor=l1_anchor)
        self.assertFalse(ok_l2_miss)
        self.assertIn("MANDATORY_L2_MESSAGE_PASSER_PROOF_MISSING", err_l2)

        # 5k. Invalid Bedrock version byte (e.g. b"\x01") fails closed
        p_bad_version = dataclasses.replace(valid_op_payload, version_byte=b"\x01")
        ok_ver, _, err_ver = OpStackOutputProofVerifier.verify_withdrawal_proof(p_bad_version, l1_trust_anchor=l1_anchor)
        self.assertFalse(ok_ver)
        self.assertEqual(err_ver, "INVALID_OP_STACK_VERSION_BYTE")

        # 5l. Unauthenticated L1 trust anchor fails closed
        unauth_anchor = BlockHeaderTrustAnchor(
            chain_id=1,
            block_number=19_500_000,
            block_hash=l1_b_hash,
            state_root=l1_state_root,
            header_timestamp_utc="2026-11-15T00:00:00Z",
            is_authenticated=False,
        )
        ok_unauth, _, err_unauth = OpStackOutputProofVerifier.verify_withdrawal_proof(valid_op_payload, l1_trust_anchor=unauth_anchor)
        self.assertFalse(ok_unauth)
        self.assertEqual(err_unauth, "UNAUTHENTICATED_L1_BLOCK_HEADER_TRUST_ANCHOR_REQUIRED")

        # 5m. L1 block number mismatch fails closed
        p_block_mismatch = dataclasses.replace(valid_op_payload, l1_block_number=99_999_999)
        ok_blk, _, err_blk = OpStackOutputProofVerifier.verify_withdrawal_proof(p_block_mismatch, l1_trust_anchor=l1_anchor)
        self.assertFalse(ok_blk)
        self.assertIn("L1_TRUST_ANCHOR_BLOCK_NUMBER_MISMATCH", err_blk)

        # 5n. Caller-selected oracle address fails closed
        p_bad_oracle = dataclasses.replace(valid_op_payload, l1_oracle_address="0x1111111111111111111111111111111111111111")
        ok_oracle, _, err_oracle = OpStackOutputProofVerifier.verify_withdrawal_proof(p_bad_oracle, l1_trust_anchor=l1_anchor)
        self.assertFalse(ok_oracle)
        self.assertIn("REJECTED_CALLER_SELECTED_ORACLE_ADDRESS", err_oracle)

        # 5o. Arbitrary output storage slot fails closed
        p_bad_slot = dataclasses.replace(valid_op_payload, l1_output_slot=4)
        ok_slot, _, err_slot = OpStackOutputProofVerifier.verify_withdrawal_proof(p_bad_slot, l1_trust_anchor=l1_anchor)
        self.assertFalse(ok_slot)
        self.assertEqual(err_slot, "REJECTED_ARBITRARY_STORAGE_SLOT_4")

        # 5p. Direct caller injection into BlockHeaderTrustAnchorRegistry without authentication fails closed
        caller_fake_anchor = BlockHeaderTrustAnchor(
            chain_id=1,
            block_number=19_500_001,
            block_hash="0x" + "aa" * 32,
            state_root="0x" + "bb" * 32,
            header_timestamp_utc="2026-11-15T00:00:00Z",
            is_authenticated=True,
        )
        with self.assertRaises(ValueError) as ctx_inj:
            BlockHeaderTrustAnchorRegistry.register(caller_fake_anchor)
        self.assertIn("failed cryptographic authenticity verification", str(ctx_inj.exception))

        # 5q. L1 trust anchor chain ID mismatch fails closed
        l1_wrong_chain_anchor = _create_verified_anchor(19_500_000, l1_b_hash, l1_state_root, c_id=10)
        ok_chain, _, err_chain = OpStackOutputProofVerifier.verify_withdrawal_proof(valid_op_payload, l1_trust_anchor=l1_wrong_chain_anchor)
        self.assertFalse(ok_chain)
        self.assertIn("L1_TRUST_ANCHOR_CHAIN_MISMATCH", err_chain)

        # 5r. Caller-created anchor with forged seal fails closed
        forged_seal_anchor = BlockHeaderTrustAnchor(
            chain_id=1,
            block_number=19_500_000,
            block_hash=l1_b_hash,
            state_root=l1_state_root,
            header_timestamp_utc="2026-11-15T00:00:00Z",
            verification_seal="0x" + "ff" * 32,
            is_authenticated=True,
        )
        ok_forge, _, err_forge = OpStackOutputProofVerifier.verify_withdrawal_proof(valid_op_payload, l1_trust_anchor=forged_seal_anchor)
        self.assertFalse(ok_forge)
        self.assertEqual(err_forge, "UNAUTHENTICATED_L1_BLOCK_HEADER_TRUST_ANCHOR_REQUIRED")

        # 5s. Caller-constructed unsealed evidence NEVER produces WITHDRAWAL_PROOF_MATURE_FINAL
        forged_evidence = OpStackOutputProofEvidence(
            withdrawal_hash=w_hash,
            l2_output_index=42,
            l2_block_number=12345678,
            output_root=output_root,
            l1_oracle_output_root=output_root,
            is_valid=True,
        )
        st_forged = MultiChainFinalityEngine.evaluate_op_stack_withdrawal(
            proven_withdrawal_timestamp=t_proven,
            current_l1_timestamp=t_proven + delay + 100,
            proof_maturity_delay_seconds=delay,
            output_proof_evidence=forged_evidence,
            expected_withdrawal_hash=w_hash,
            expected_output_root=output_root,
            l1_trust_anchor=l1_anchor,
        )
        self.assertEqual(st_forged, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED)
        self.assertNotEqual(st_forged, FinalityState.WITHDRAWAL_PROOF_MATURE_FINAL)


if __name__ == "__main__":
    unittest.main()
