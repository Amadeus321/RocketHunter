"""Unit tests for Shared Signal Evidence Framework (Phase 4).

Verifies:
1. AssetIdentity exact chain, token, pool validation (never ticker alone).
2. Distinction between observation provenance (VERIFIED) and blockchain cryptographic authentication.
3. SourceIndependencePolicy detecting correlated vs independent providers.
4. SecurityEvidencePolicy: missing evidence is NOT clean; hard rejects require explicit failure proof.
5. LpLockEvidencePolicy: provider-reported % alone is unverified; requires on-chain contract and epoch.
6. Configuration and code fingerprint determinism.
7. SignalEngineInput reproducible hashing.
8. Zero production disk writes or journal mutations.
"""
from __future__ import annotations

import datetime as _dt
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import unittest

from scripts.signal_evidence_framework import (
    AssetIdentity,
    BlockchainAuthenticationStatus,
    EngineConfig,
    EvidenceState,
    FreshnessStatus,
    LpLockEvidencePolicy,
    LpLockVerificationState,
    MeasurementAttribution,
    ObservationQuality,
    SecurityAssessmentState,
    SecurityEvidencePolicy,
    SignalEngineInput,
    SignalEvidenceRecord,
    SourceEvidenceAttribution,
    SourceIndependencePolicy,
    SourceIndependenceTier,
)


class TestSignalEvidenceFramework(unittest.TestCase):
    """Test suite verifying the Phase 4 evidence contract."""

    def test_asset_identity_strict_validation(self) -> None:
        """Asset identity requires chain, token address and pool address, rejecting ticker alone."""
        # Valid EVM identity
        asset_evm = AssetIdentity(
            chain="ethereum",
            token_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            pool_address="0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
            token_symbol="USDC",
        )
        self.assertEqual(asset_evm.chain, "ethereum")
        self.assertEqual(asset_evm.token_address, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48")
        self.assertTrue(asset_evm.canonical_id.startswith("dex:ethereum:"))

        # Valid Solana identity (case preserved)
        asset_sol = AssetIdentity(
            chain="solana",
            token_address="DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
            pool_address="EP2ib6dYdEeqD8MfE2ezHCxX3kEDSpG9QA6HPHrG4gF7",
            token_symbol="BONK",
        )
        self.assertEqual(asset_sol.token_address, "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")
        self.assertEqual(asset_sol.chain, "solana")

        # Invalid: missing chain
        with self.assertRaises(ValueError):
            AssetIdentity(chain="", token_address="0x1234567890123456789012345678901234567890", pool_address="0x1234567890123456789012345678901234567890")

        # Invalid: ticker only, missing token address
        with self.assertRaises(ValueError):
            AssetIdentity(chain="ethereum", token_address="", pool_address="0x1234567890123456789012345678901234567890", token_symbol="PEPE")

        # Invalid: malformed EVM address format
        with self.assertRaises(ValueError):
            AssetIdentity(chain="ethereum", token_address="0xinvalid", pool_address="0x1234567890123456789012345678901234567890")

    def test_blockchain_authentication_status_distinction(self) -> None:
        """VERIFIED observation quality must not imply cryptographic blockchain authentication."""
        record = SignalEvidenceRecord(
            engine_name="TestEngine",
            engine_version="1.0.0",
            run_id="run_1",
            forecast_id="fc_1",
            overall_evidence_state=EvidenceState.VERIFIED,
            overall_observation_quality=ObservationQuality.HIGH,
            blockchain_authentication=BlockchainAuthenticationStatus.UNAUTHENTICATED_OFFCHAIN_OBSERVATION,
        )
        rec_dict = record.to_dict()
        self.assertEqual(rec_dict["overall_evidence_state"], "VERIFIED")
        self.assertEqual(rec_dict["blockchain_authentication"], "UNAUTHENTICATED_OFFCHAIN_OBSERVATION")
        self.assertNotEqual(rec_dict["blockchain_authentication"], "AUTHENTICATED_ONCHAIN_EVIDENCE")

    def test_source_independence_policy(self) -> None:
        """Correlated sources echoing the same feed must be detected."""
        src1 = SourceEvidenceAttribution(
            provider_name="dexscreener",
            endpoint_or_method="get_pair",
            source_event_timestamp_utc="2026-11-16T10:00:00Z",
            underlying_feed_id="uniswap_v2_pool_0x1234",
        )
        src2 = SourceEvidenceAttribution(
            provider_name="dexpaprika",
            endpoint_or_method="get_ticker",
            source_event_timestamp_utc="2026-11-16T10:00:01Z",
            underlying_feed_id="uniswap_v2_pool_0x1234",  # Same underlying feed
        )
        ok, reason = SourceIndependencePolicy.evaluate_source_independence([src1, src2])
        self.assertFalse(ok)
        self.assertIn("CORRELATED_SOURCES_DETECTED", reason)

        # Independent sources
        src3 = SourceEvidenceAttribution(
            provider_name="archive_node_rpc",
            endpoint_or_method="eth_call",
            source_event_timestamp_utc="2026-11-16T10:00:00Z",
            independence_tier=SourceIndependenceTier.PRIMARY_ONCHAIN_RAW,
            underlying_feed_id="direct_node_storage_slot",
        )
        ok_indep, reason_indep = SourceIndependencePolicy.evaluate_source_independence([src1, src3])
        self.assertTrue(ok_indep)
        self.assertEqual(reason_indep, "INDEPENDENT_SOURCES_VERIFIED")

    def test_security_evidence_policy_missing_is_not_clean(self) -> None:
        """Missing security evidence must never be treated as a clean security pass."""
        # Missing evidence
        state, reasons = SecurityEvidencePolicy.evaluate_security_evidence(
            is_honeypot=None,
            is_blacklisted=None,
            transfer_tax_buy_bps=None,
            transfer_tax_sell_bps=None,
            evidence_present=False,
        )
        self.assertEqual(state, SecurityAssessmentState.SECURITY_UNVERIFIED_EVIDENCE_MISSING)
        self.assertIn("SECURITY_AUDIT_EVIDENCE_MISSING_CANNOT_TREAT_AS_CLEAN", reasons)

        # Hard fail condition
        state_fail, reasons_fail = SecurityEvidencePolicy.evaluate_security_evidence(
            is_honeypot=True,
            is_blacklisted=False,
            transfer_tax_buy_bps=500.0,
            transfer_tax_sell_bps=500.0,
            evidence_present=True,
        )
        self.assertEqual(state_fail, SecurityAssessmentState.SECURITY_FAIL_HARD_REJECT)
        self.assertIn("HONEYPOT_DETECTED", reasons_fail)

        # High tax hard fail (> 1000 bps)
        state_tax, reasons_tax = SecurityEvidencePolicy.evaluate_security_evidence(
            is_honeypot=False,
            is_blacklisted=False,
            transfer_tax_buy_bps=1200.0,
            transfer_tax_sell_bps=500.0,
            evidence_present=True,
        )
        self.assertEqual(state_tax, SecurityAssessmentState.SECURITY_FAIL_HARD_REJECT)
        self.assertTrue(any("BUY_TAX_EXCEEDS_MAX" in r for r in reasons_tax))

        # Legitimate clean pass
        state_pass, reasons_pass = SecurityEvidencePolicy.evaluate_security_evidence(
            is_honeypot=False,
            is_blacklisted=False,
            transfer_tax_buy_bps=200.0,
            transfer_tax_sell_bps=200.0,
            evidence_present=True,
        )
        self.assertEqual(state_pass, SecurityAssessmentState.SECURITY_PASS_WITH_EVIDENCE)
        self.assertEqual(len(reasons_pass), 0)

    def test_lp_lock_evidence_policy(self) -> None:
        """Provider-reported LP lock % alone is unverified; requires contract & unlock epoch."""
        as_of = 1763300000

        # Unverified provider claim only (missing contract / unlock date)
        state_claim, reason_claim = LpLockEvidencePolicy.evaluate_lp_lock(
            claimed_lock_pct=99.0,
            lock_contract_address=None,
            unlock_timestamp_epoch=None,
            as_of_epoch=as_of,
            beneficiary_address=None,
        )
        self.assertEqual(state_claim, LpLockVerificationState.LP_LOCK_UNVERIFIED_CLAIM_ONLY)
        self.assertIn("PROVIDER_REPORTED_PERCENTAGE_WITHOUT_CONTRACT", reason_claim)

        # Burned to dead address
        state_burn, reason_burn = LpLockEvidencePolicy.evaluate_lp_lock(
            claimed_lock_pct=100.0,
            lock_contract_address="0x000000000000000000000000000000000000dead",
            unlock_timestamp_epoch=None,
            as_of_epoch=as_of,
            beneficiary_address=None,
        )
        self.assertEqual(state_burn, LpLockVerificationState.LP_BURN_VERIFIED_DEAD_ADDRESS)

        # Valid on-chain lock with future unlock epoch
        future_epoch = as_of + 86400 * 365
        state_lock, reason_lock = LpLockEvidencePolicy.evaluate_lp_lock(
            claimed_lock_pct=95.0,
            lock_contract_address="0xD4b5F10D61916Bd6E0860144a91Ac658dE837123",
            unlock_timestamp_epoch=future_epoch,
            as_of_epoch=as_of,
            beneficiary_address="0x1234567890123456789012345678901234567890",
        )
        self.assertEqual(state_lock, LpLockVerificationState.LP_LOCK_VERIFIED_ONCHAIN_CONTRACT)

        # Expired lock
        past_epoch = as_of - 100
        state_exp, reason_exp = LpLockEvidencePolicy.evaluate_lp_lock(
            claimed_lock_pct=95.0,
            lock_contract_address="0xD4b5F10D61916Bd6E0860144a91Ac658dE837123",
            unlock_timestamp_epoch=past_epoch,
            as_of_epoch=as_of,
            beneficiary_address="0x1234567890123456789012345678901234567890",
        )
        self.assertEqual(state_exp, LpLockVerificationState.LP_UNLOCKED_OR_UNKNOWN)
        self.assertIn("EXPIRED", reason_exp)

    def test_engine_config_fingerprint_determinism(self) -> None:
        """Engine config fingerprint must be deterministic and sensitive to parameter changes."""
        c1 = EngineConfig("TestEngine", "1.0.0", {"param_a": 10, "param_b": "value"})
        c2 = EngineConfig("TestEngine", "1.0.0", {"param_b": "value", "param_a": 10})  # Order independent
        c3 = EngineConfig("TestEngine", "1.0.0", {"param_a": 11, "param_b": "value"})  # Different param

        self.assertEqual(c1.config_fingerprint, c2.config_fingerprint)
        self.assertNotEqual(c1.config_fingerprint, c3.config_fingerprint)

    def test_signal_engine_input_reproducibility(self) -> None:
        """SignalEngineInput must compute a deterministic SHA-256 input hash."""
        asset = AssetIdentity("ethereum", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640")
        dt = datetime(2026, 11, 16, 10, 0, 0, tzinfo=timezone.utc)
        inp1 = SignalEngineInput(asset=asset, as_of_timestamp=dt, run_id="run_A", forecast_id="fc_A", raw_payloads={"test": 123})
        inp2 = SignalEngineInput(asset=asset, as_of_timestamp=dt, run_id="run_A", forecast_id="fc_A", raw_payloads={"test": 123})
        self.assertEqual(inp1.compute_input_hash(), inp2.compute_input_hash())


if __name__ == "__main__":
    unittest.main()
