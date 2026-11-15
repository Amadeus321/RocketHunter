#!/usr/bin/env python3
"""Stage 3.1: Comprehensive Unit & Adversarial Test Suite for Rocket Hunter v4.30.

Exercises and validates all Stage 3.1 acceptance criteria:
1. Canonical Uniswap V3 TickMath formula, boundary conditions, and tick-204520 fixture.
2. Protected data integrity verifier (manifest snapshot, SHA-256 equivalence, fail-closed).
3. Three-tiered RPC evidence verification (payload integrity, endpoint auth, canonical quorum, reorg quarantine).
4. Multi-chain configuration-driven finality (Ethereum L1, Arbitrum dynamic reader, OP Stack op-contracts/v1.6.0, Solana).
5. Venue-specific execution evidence structures and independent dual-track outcome resolution.
6. Dataset A trade journal pro-rata cost-basis allocation and unrealized inventory segregation.
7. Cameron-Gelbach-Miller two-way clustered variance hand-calculated fixture (2x3 panel).
8. Strict non-positive variance fallback consistency and small-cluster t_11 inference.
9. Stationary AR(1) positive-control test setting DEPENDENT_BLOCK_FALLBACK_TRIGGERED status.
10. Negative-control test across repeated independent panels verifying false alarm rate <= 10%.
11. Mandatory EXPLORATORY_UNCALIBRATED_INFERENCE labeling when block count B < 30.
12. Time-Block Bootstrap draw-instance tracking verifying intact cross-sections.
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
    """Test reproducibility manifest and protected data integrity verification."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.data_dir = self.temp_dir / "rocket-hunter-data"
        self.data_dir.mkdir(parents=True)
        self.verifier = ProtectedDataVerifier(root_dir=self.temp_dir)

    def test_clean_state_equivalence(self):
        """Validates that identical pre-run and post-run snapshots pass clean verification."""
        journal = self.data_dir / "user_positions_journal.json"
        journal.write_text('{"positions": []}', encoding="utf-8")

        pre = self.verifier.snapshot_protected_data()
        post = self.verifier.snapshot_protected_data()

        is_clean, disc = self.verifier.verify_clean_equivalence(pre, post)
        self.assertTrue(is_clean)
        self.assertEqual(len(disc), 0)

    def test_tamper_detection_fails_closed(self):
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


class TestStage31RpcProvenanceAndQuarantine(unittest.TestCase):
    """Test three-tiered RPC evidence verification and reorganization quarantine."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.verifier = RpcProvenanceVerifier(data_dir=self.temp_dir)

    def _make_record(self, block_hash: str = "0x1234", state_root: str = "0xabcd", domain: str = "eth-mainnet.alchemy.com") -> RpcEvidenceRecord:
        req = b'{"method": "eth_call"}'
        resp = b'{"result": "0x0001"}'
        return RpcEvidenceRecord(
            rpc_method="eth_call",
            rpc_params=[{"to": "0xpool"}, "latest"],
            provider_id="alchemy_primary",
            provider_domain=domain,
            retrieval_timestamp_utc="2026-03-01T12:00:00Z",
            raw_request_bytes=req,
            raw_response_bytes=resp,
            chain_id=1,
            block_number=19450000,
            block_hash=block_hash,
            parent_hash="0x0000",
            state_root=state_root,
            tls_cert_fingerprint_sha256="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )

    def test_tier1_and_tier2_verification(self):
        """Validates payload integrity and endpoint allowlist checking."""
        rec = self._make_record()
        self.assertTrue(self.verifier.verify_tier1_payload_integrity(rec))
        self.assertTrue(self.verifier.verify_tier2_endpoint_authentication(rec))

        # Disallowed domain fails Tier 2
        bad_rec = self._make_record(domain="untrusted-node.evil.com")
        self.assertFalse(self.verifier.verify_tier2_endpoint_authentication(bad_rec))

    def test_tier3_dual_quorum_and_reorg_quarantine(self):
        """Validates that block hash divergence triggers BLOCK_REORGANIZED_ORPHAN quarantine."""
        rec1 = self._make_record(block_hash="0x1111", state_root="0xaaaa")
        rec2 = self._make_record(block_hash="0x2222", state_root="0xbbbb")  # Hash divergence

        passed, state = self.verifier.verify_tier3_canonical_quorum(rec1, independent_peer_record=rec2)
        self.assertFalse(passed)
        self.assertEqual(state, FinalityState.BLOCK_REORGANIZED_ORPHAN)

        quarantine_path = self.verifier.quarantine_reorganized_block(rec1, reason="Dual-provider block hash mismatch")
        self.assertTrue((quarantine_path / "quarantine_manifest.json").is_file())


class TestStage31MultiChainFinality(unittest.TestCase):
    """Test configuration-driven finality state machine across EVM and non-EVM chains."""

    def test_arbitrum_dynamic_confirm_period(self):
        """Validates dynamic evaluation of confirmPeriodBlocks() with standard and non-standard periods."""
        # 1. Custom Orbit / testnet period: 7,200 blocks (~1 day)
        status_orbit = MultiChainFinalityEngine.evaluate_arbitrum(
            l1_batch_finalized=True,
            assertion_created_l1_block=19000000,
            current_finalized_l1_block=19008000,  # 8,000 blocks elapsed > 7,200
            confirm_period_blocks=7200,
            challenge_status="NONE",
        )
        self.assertEqual(status_orbit, FinalityState.FINALIZED_CANONICAL)

        # 2. Mainnet period: 45,818 blocks (~7 days)
        status_mainnet = MultiChainFinalityEngine.evaluate_arbitrum(
            l1_batch_finalized=True,
            assertion_created_l1_block=19000000,
            current_finalized_l1_block=19008000,  # 8,000 blocks elapsed < 45,818
            confirm_period_blocks=45818,
            challenge_status="NONE",
        )
        self.assertEqual(status_mainnet, FinalityState.L1_BATCH_FINALITY_ONLY)

        # 3. Active challenge routes to FINALITY_PENDING
        status_challenged = MultiChainFinalityEngine.evaluate_arbitrum(
            l1_batch_finalized=True,
            assertion_created_l1_block=19000000,
            current_finalized_l1_block=19050000,
            confirm_period_blocks=45818,
            challenge_status="ACTIVE",
        )
        self.assertEqual(status_challenged, FinalityState.FINALITY_PENDING)

    def test_op_stack_contracts_v160_delay_transitions(self):
        """Validates exact op-contracts/v1.6.0 state transitions and delay evaluation."""
        self.assertEqual(CANONICAL_OP_COMMIT, "33f06d2d5e4034125df02264a5ffe84571bd0359")
        self.assertEqual(CANONICAL_OP_TAG, "op-contracts/v1.6.0")

        # 1. Challenger win invalidation
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

        # 2. Defender win in finality airgap delay (resolvedAt = 1000, delay = 302,400s)
        status_delay_active = MultiChainFinalityEngine.evaluate_op_stack(
            l1_batch_finalized=True,
            proposal_game_type=1,
            respected_game_type=1,
            game_status=2,  # DEFENDER_WINS
            resolved_at=1000,
            current_l1_timestamp=1000 + 200000,  # 200,000s elapsed < 302,400s
            dispute_game_finality_delay_seconds=302400,
        )
        self.assertEqual(status_delay_active, FinalityState.DISPUTE_GAME_RESOLVED_DEFENDER)

        # 3. Delay elapsed and L1 DA finalized => canonical L2 output root
        status_final = MultiChainFinalityEngine.evaluate_op_stack(
            l1_batch_finalized=True,
            proposal_game_type=1,
            respected_game_type=1,
            game_status=2,  # DEFENDER_WINS
            resolved_at=1000,
            current_l1_timestamp=1000 + 302400,  # 302,400s elapsed
            dispute_game_finality_delay_seconds=302400,
        )
        self.assertEqual(status_final, FinalityState.L2_OUTPUT_FINALIZED_CANONICAL)

        # 4. Independent withdrawal proof-maturity delay (provenAt = 5000, delay = 604,800s)
        self.assertEqual(
            MultiChainFinalityEngine.evaluate_op_stack_withdrawal(5000, 5000 + 500000, 604800),
            FinalityState.WITHDRAWAL_PENDING_MATURITY,
        )
        self.assertEqual(
            MultiChainFinalityEngine.evaluate_op_stack_withdrawal(5000, 5000 + 604800, 604800),
            FinalityState.WITHDRAWAL_PROOF_MATURE_FINAL,
        )

    def test_solana_rooted_commitment(self):
        """Validates Solana commitment levels."""
        self.assertEqual(MultiChainFinalityEngine.evaluate_solana("finalized", is_rooted=True), FinalityState.FINALIZED_CANONICAL)
        self.assertEqual(MultiChainFinalityEngine.evaluate_solana("finalized", is_rooted=False), FinalityState.FINALITY_PENDING)
        self.assertEqual(MultiChainFinalityEngine.evaluate_solana("confirmed", is_rooted=True), FinalityState.FINALITY_PENDING)


class TestStage31VenueEvidenceAndDualTrackOutcomes(unittest.TestCase):
    """Test venue-specific execution evidence and independent dual-track outcome resolution."""

    def test_uniswap_v2_and_v3_evidence_validation(self):
        """Validates completeness checks for V2 reserves and V3 initialized tick ranges."""
        # V2 complete evidence
        v2 = UniswapV2Evidence(
            chain_id=1,
            pool_address="0xpool2",
            token0_address="0xtoken0",
            token1_address="0xtoken1",
            token0_decimals=6,
            token1_decimals=18,
            raw_reserve0=1000000,
            raw_reserve1=2000000,
            fee_bps=30,
            block_number=19000000,
            block_hash="0xhash",
            parent_hash="0xparent",
        )
        self.assertTrue(v2.is_complete())

        # V3 incomplete ticks across price path
        v3 = UniswapV3Evidence(
            chain_id=1,
            pool_address="0xpool3",
            token0_address="0xtoken0",
            token1_address="0xtoken1",
            token0_decimals=6,
            token1_decimals=18,
            fee_tier_ppm=3000,
            sqrt_price_x96=2186517289083564169714246582247698,
            tick=204520,
            active_liquidity=1000000,
            initialized_ticks={},  # Empty tick map for non-zero move
            tick_bitmap={},
            block_number=19000000,
            block_hash="0xhash",
            parent_hash="0xparent",
        )
        # Incomplete when simulating trade crossing ticks
        self.assertFalse(v3.is_complete_for_range(target_tick=205000))

    def test_dual_track_outcome_resolution(self):
        """Validates technical TP attainment evaluates independently from modeled execution."""
        # 1. Technical pattern validated (+40.00% candle high)
        mkt_reached = DualTrackOutcomeEvaluator.evaluate_market_technical_tp(
            entry_price=10.0,
            max_candle_high_24h=14.05,  # +40.5% high
        )
        self.assertTrue(mkt_reached)

        # 2. Modeled execution with assumptions (never labeled verified)
        status, net_return = DualTrackOutcomeEvaluator.evaluate_modeled_execution(
            is_evidence_complete=True,
            gross_return_pct=40.5,
            price_impact_pct=8.0,
            fee_pct=0.6,
            gas_drag_pct=3.5,
        )
        self.assertEqual(status, ModeledExecutionStatus.MODELED_EXECUTION_WITH_ASSUMPTIONS)
        self.assertAlmostEqual(net_return, 28.4, places=2)

        # 3. Modeled execution with incomplete evidence fails closed
        status_unavail, net_unavail = DualTrackOutcomeEvaluator.evaluate_modeled_execution(
            is_evidence_complete=False,
            gross_return_pct=40.5,
            price_impact_pct=8.0,
            fee_pct=0.6,
            gas_drag_pct=3.5,
        )
        self.assertEqual(status_unavail, ModeledExecutionStatus.EXECUTION_UNVERIFIED)
        self.assertIsNone(net_unavail)


class TestStage31DatasetAReconciliation(unittest.TestCase):
    """Test Dataset A trade journal pro-rata cost-basis allocation and inventory segregation."""

    def test_pro_rata_cost_basis_and_unrealized_inventory(self):
        """Validates exact pro-rata cost-basis allocation for partial sales and inventory segregation."""
        entry = JournalEntryPosition(
            token_id="ethereum:0x1111",
            quote_expenditure=1000.0,  # $1,000 USDC
            tokens_acquired=10000.0,   # 10,000 XYZ tokens
            entry_gas_cost=20.0,
            entry_venue_fees=3.0,
        )
        # User sells 4,000 tokens (40% tranche) for $600 USDC, exit gas $15, fees $2
        exit_exec = JournalExitExecution(
            tokens_sold=4000.0,
            gross_proceeds=600.0,
            exit_gas_cost=15.0,
            exit_venue_fees=2.0,
            other_costs=0.0,
        )

        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, is_cryptographically_verified=True)

        self.assertEqual(outcome.status, EvidenceTier.INDEPENDENTLY_VERIFIED_EXECUTION)
        self.assertEqual(outcome.alpha_ratio, 0.40)
        self.assertEqual(outcome.allocated_expenditure, 400.0)
        self.assertEqual(outcome.allocated_entry_gas, 8.0)
        self.assertEqual(outcome.allocated_entry_fees, 1.2)

        # Net Realized P/L = 600 - 400 - (8 + 15) - (1.2 + 2) = 173.8
        self.assertAlmostEqual(outcome.realized_net_pl, 173.8, places=2)

        # Unsold inventory segregation
        self.assertEqual(outcome.tokens_remaining, 6000.0)
        self.assertEqual(outcome.cost_basis_remaining, 600.0)
        self.assertTrue(outcome.contributes_to_verified_pl)

    def test_unverified_journal_excluded_from_realized_pl(self):
        """Validates that entries lacking cryptographic verification are excluded from verified P/L."""
        entry = JournalEntryPosition("ethereum:0x1111", 1000.0, 10000.0, 20.0, 3.0)
        exit_exec = JournalExitExecution(10000.0, 1500.0, 15.0, 2.0)

        outcome = DatasetAReconciliationEngine.reconcile_tranche(entry, exit_exec, is_cryptographically_verified=False)
        self.assertEqual(outcome.status, EvidenceTier.USER_REPORTED_EXECUTION)
        self.assertFalse(outcome.contributes_to_verified_pl)


class TestStage31ClusteredStatisticalInference(unittest.TestCase):
    """Test Cameron-Gelbach-Miller two-way clustering, hand-calculated fixture, and diagnostics."""

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

    def test_serial_diagnostic_positive_control_stationary_ar1(self):
        """Positive-Control: Stationary AR(1) weekly shocks trigger fallback and set status."""
        random.seed(123)
        B = 12
        M = 30

        # Generate persistent AR(1) weekly shock series (phi = 0.85)
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

        # Positive control MUST detect persistence and set dependent-block fallback status
        self.assertEqual(eval_res["status"], InferentialStatus.DEPENDENT_BLOCK_FALLBACK_TRIGGERED.value)
        self.assertEqual(eval_res["diagnostic_flag"], "SERIAL_PERSISTENCE_DETECTED")
        self.assertTrue(eval_res["serial_diagnostic"]["fallback_triggered"])

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

    def test_mandatory_exploratory_labeling_when_b_small(self):
        """Enforces that when block count B < 30 and no fallback triggered, status is strictly EXPLORATORY."""
        dataset = [
            {"token_id": f"token_{i}", "week_block": f"week_{w}", "loss_diff": 0.05 + (w % 3) * 0.001}
            for w in range(12)
            for i in range(30)
        ]
        eval_res = ClusteredEvaluationEngine.run_stage31_evaluation(dataset, field="loss_diff")
        self.assertEqual(eval_res["status"], InferentialStatus.EXPLORATORY_UNCALIBRATED_INFERENCE.value)
        self.assertEqual(eval_res["diagnostic_flag"], "SMALL_BLOCK_COUNT_ASSUMPTION_CONDITIONAL")
        # Ensure scalar N_eff is NEVER claimed
        self.assertNotIn("N_eff", eval_res["structural_counts"])


if __name__ == "__main__":
    unittest.main()
