"""Shared Signal Evidence Contract and Deterministic Engine Framework (Phase 4).

Establishes a unified, versioned, source-attributable signal contract for the
9 Advanced Detection Engines.

Key Principles:
1. Provider observation != cryptographic blockchain authentication.
   `VERIFIED` indicates observation provenance and data integrity standards,
   never cryptographic consensus finality.
2. Exact Identity: Tokens are strictly identified by (chain, token_address/mint, pool_address),
   never by ticker symbol alone.
3. Anti-Correlation: Multiple providers repeating the same pool/trade telemetry do not
   constitute independent corroboration.
4. Security Gating: Missing security evidence is NOT clean security evidence.
5. LP Lock Rigor: LP lock claims require contract, position, beneficiary, and unlock epoch;
   a provider-reported percentage alone is strictly unverified.
6. Research Isolation: Pure research and shadow evaluation; zero live order execution,
   zero wallet signing, zero automated strategy weight promotion.
"""
from __future__ import annotations

import abc
import dataclasses
import datetime as _dt
from datetime import datetime, timezone
import enum
import hashlib
import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from scripts.address_identity import build_canonical_dex_identity, is_evm_chain, same_address
except ImportError:
    from address_identity import build_canonical_dex_identity, is_evm_chain, same_address

FRAMEWORK_VERSION = "4.30.0"


# ==============================================================================
# 1. State Classifications & Enumerations
# ==============================================================================

class EvidenceState(str, enum.Enum):
    """Explicit observation and telemetry evidence states."""
    VERIFIED = "VERIFIED"            # Observation adheres to quality, continuity & provenance standards
    ESTIMATED = "ESTIMATED"          # Value derived via interpolation or algorithmic estimation
    MISSING = "MISSING"              # Required telemetry was completely absent
    STALE = "STALE"                  # Observation timestamp is older than allowable freshness window
    CONFLICTING = "CONFLICTING"      # Two or more concurrent sources report irreconcilable values
    UNAVAILABLE = "UNAVAILABLE"      # Upstream provider rate-limited, unreachable, or endpoint degraded
    ERROR = "ERROR"                  # Exception encountered during parsing or decoding


class ObservationQuality(str, enum.Enum):
    """Integrity and completeness tier of the received observation."""
    HIGH = "HIGH"                    # Complete continuity, exact timestamps, zero anomalies
    MEDIUM = "MEDIUM"                # Minor acceptable non-critical gaps within tolerance
    LOW = "LOW"                      # High jitter or near tolerance boundary
    DEGRADED = "DEGRADED"            # Significant gaps, partial interval coverage
    UNAVAILABLE = "UNAVAILABLE"      # Telemetry unusable


class FreshnessStatus(str, enum.Enum):
    """Freshness of the observation relative to evaluation as_of_timestamp."""
    FRESH = "FRESH"                  # Within primary active freshness window
    WARM = "WARM"                    # Acceptable but approaching staleness threshold
    STALE = "STALE"                  # Exceeds maximum staleness threshold
    EXPIRED = "EXPIRED"              # Terminal staleness; unusable
    FUTURE_REJECTED = "FUTURE_REJECTED"  # Timestamp is ahead of as_of_timestamp (anti-lookahead violation)


class BlockchainAuthenticationStatus(str, enum.Enum):
    """Strict segregation of blockchain cryptographic status from observation quality."""
    UNAUTHENTICATED_OFFCHAIN_OBSERVATION = "UNAUTHENTICATED_OFFCHAIN_OBSERVATION"
    CALLER_ASSERTED_UNAUTHENTICATED = "CALLER_ASSERTED_UNAUTHENTICATED"
    CRYPTOGRAPHIC_AUTHENTICATION_NOT_CLAIMED = "CRYPTOGRAPHIC_AUTHENTICATION_NOT_CLAIMED"
    DEFERRED_CONSENSUS_UNVERIFIED = "DEFERRED_CONSENSUS_UNVERIFIED"
    AUTHENTICATED_ONCHAIN_EVIDENCE = "AUTHENTICATED_ONCHAIN_EVIDENCE"  # Strictly reserved for verified MPT/SSZ proofs


class SourceIndependenceTier(str, enum.Enum):
    """Provenance hierarchy to detect correlated or mirrored telemetry."""
    PRIMARY_ONCHAIN_RAW = "PRIMARY_ONCHAIN_RAW"          # Direct archive/full node execution state
    INDEPENDENT_INDEXER = "INDEPENDENT_INDEXER"          # Dedicated indexer with verifiable ingestion log
    AGGREGATOR_CORRELATED = "AGGREGATOR_CORRELATED"      # Public aggregator (DexScreener, Birdeye, DexPaprika)
    MIRRORED_SECONDARY = "MIRRORED_SECONDARY"            # Downstream service echoing upstream aggregator


class SecurityAssessmentState(str, enum.Enum):
    """Contract and tokenomics security states."""
    SECURITY_PASS_WITH_EVIDENCE = "SECURITY_PASS_WITH_EVIDENCE"
    SECURITY_FAIL_HARD_REJECT = "SECURITY_FAIL_HARD_REJECT"
    SECURITY_UNVERIFIED_EVIDENCE_MISSING = "SECURITY_UNVERIFIED_EVIDENCE_MISSING"
    SECURITY_CONFLICTING_EVIDENCE = "SECURITY_CONFLICTING_EVIDENCE"


class LpLockVerificationState(str, enum.Enum):
    """LP lock proof verification states."""
    LP_LOCK_VERIFIED_ONCHAIN_CONTRACT = "LP_LOCK_VERIFIED_ONCHAIN_CONTRACT"
    LP_BURN_VERIFIED_DEAD_ADDRESS = "LP_BURN_VERIFIED_DEAD_ADDRESS"
    LP_LOCK_UNVERIFIED_CLAIM_ONLY = "LP_LOCK_UNVERIFIED_CLAIM_ONLY"
    LP_UNLOCKED_OR_UNKNOWN = "LP_UNLOCKED_OR_UNKNOWN"


# ==============================================================================
# 2. Asset & Source Identity
# ==============================================================================

@dataclasses.dataclass(frozen=True)
class AssetIdentity:
    """Exact, immutable on-chain identity for a candidate asset and pool.

    Never identifies a token by ticker symbol alone.
    """
    chain: str
    token_address: str
    pool_address: str
    token_symbol: Optional[str] = None
    token_decimals: Optional[int] = None
    canonical_id: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        c = str(self.chain or "").strip().lower()
        t = str(self.token_address or "").strip()
        p = str(self.pool_address or "").strip()

        if not c:
            raise ValueError("AssetIdentity requires non-empty 'chain'")
        if not t:
            raise ValueError("AssetIdentity requires non-empty 'token_address'")
        if not p:
            raise ValueError("AssetIdentity requires non-empty 'pool_address'")

        # Enforce EVM hexadecimal formatting when applicable
        if is_evm_chain(c):
            if not re.fullmatch(r"0x[0-9a-fA-F]{40}", t):
                raise ValueError(f"Invalid EVM token_address format: {t}")
            if not re.fullmatch(r"0x[0-9a-fA-F]{40}", p):
                raise ValueError(f"Invalid EVM pool_address format: {p}")
            t = t.lower()
            p = p.lower()
        else:
            # Solana / non-EVM address validation (base58, 32-44 chars)
            if len(t) < 32 or len(t) > 44:
                raise ValueError(f"Invalid non-EVM token_address length: {t}")
            if len(p) < 32 or len(p) > 44:
                raise ValueError(f"Invalid non-EVM pool_address length: {p}")

        cid = build_canonical_dex_identity(c, t, p)
        if not cid:
            raise ValueError(f"Failed to generate canonical_id for ({c}, {t}, {p})")

        object.__setattr__(self, "chain", c)
        object.__setattr__(self, "token_address", t)
        object.__setattr__(self, "pool_address", p)
        object.__setattr__(self, "canonical_id", cid)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain": self.chain,
            "token_address": self.token_address,
            "pool_address": self.pool_address,
            "token_symbol": self.token_symbol,
            "token_decimals": self.token_decimals,
            "canonical_id": self.canonical_id,
        }


@dataclasses.dataclass(frozen=True)
class SourceEvidenceAttribution:
    """Attribution metadata for raw source observations."""
    provider_name: str
    endpoint_or_method: str
    source_event_timestamp_utc: str
    response_sha256: Optional[str] = None
    independence_tier: SourceIndependenceTier = SourceIndependenceTier.AGGREGATOR_CORRELATED
    underlying_feed_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "endpoint_or_method": self.endpoint_or_method,
            "source_event_timestamp_utc": self.source_event_timestamp_utc,
            "response_sha256": self.response_sha256,
            "independence_tier": self.independence_tier.value,
            "underlying_feed_id": self.underlying_feed_id,
        }


# ==============================================================================
# 3. Measurement Attribution & Signal Record Contract
# ==============================================================================

@dataclasses.dataclass(frozen=True)
class MeasurementAttribution:
    """Granular attribution for an individual calculated measurement."""
    measurement_name: str
    value: Optional[float]
    units: str
    interval_str: Optional[str]
    freshness: FreshnessStatus
    evidence_state: EvidenceState
    observation_quality: ObservationQuality
    reason_code: str
    as_of_timestamp_utc: str
    threshold_used: Optional[float] = None
    coverage_ratio: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "measurement_name": self.measurement_name,
            "value": round(self.value, 6) if self.value is not None else None,
            "units": self.units,
            "interval_str": self.interval_str,
            "freshness": self.freshness.value,
            "evidence_state": self.evidence_state.value,
            "observation_quality": self.observation_quality.value,
            "reason_code": self.reason_code,
            "as_of_timestamp_utc": self.as_of_timestamp_utc,
            "threshold_used": self.threshold_used,
            "coverage_ratio": round(self.coverage_ratio, 4) if self.coverage_ratio is not None else None,
        }


@dataclasses.dataclass
class SignalEvidenceRecord:
    """Shared, versioned signal evidence record produced by a detection engine."""
    schema_version: str = FRAMEWORK_VERSION
    engine_name: str = ""
    engine_version: str = ""
    run_id: str = ""
    forecast_id: str = ""
    configuration_fingerprint: str = ""
    code_fingerprint: str = ""
    asset: Optional[AssetIdentity] = None
    decision_timestamp_utc: str = ""
    as_of_timestamp_utc: str = ""
    measurements: List[MeasurementAttribution] = dataclasses.field(default_factory=list)
    coverage_completeness: float = 0.0
    overall_evidence_state: EvidenceState = EvidenceState.UNAVAILABLE
    overall_observation_quality: ObservationQuality = ObservationQuality.UNAVAILABLE
    blockchain_authentication: BlockchainAuthenticationStatus = (
        BlockchainAuthenticationStatus.UNAUTHENTICATED_OFFCHAIN_OBSERVATION
    )
    score: Optional[float] = None
    confidence: Optional[float] = None
    probability_estimate: Optional[float] = None  # Strictly null unless calibrated
    eligibility_verdict: str = "UNAVAILABLE"       # ELIGIBLE, INELIGIBLE, UNAVAILABLE, HARD_REJECT
    exclusion_reasons: List[str] = dataclasses.field(default_factory=list)
    source_attributions: List[SourceEvidenceAttribution] = dataclasses.field(default_factory=list)
    reproducible_input_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "engine_name": self.engine_name,
            "engine_version": self.engine_version,
            "run_id": self.run_id,
            "forecast_id": self.forecast_id,
            "configuration_fingerprint": self.configuration_fingerprint,
            "code_fingerprint": self.code_fingerprint,
            "asset": self.asset.to_dict() if self.asset else None,
            "decision_timestamp_utc": self.decision_timestamp_utc,
            "as_of_timestamp_utc": self.as_of_timestamp_utc,
            "measurements": [m.to_dict() for m in self.measurements],
            "coverage_completeness": round(self.coverage_completeness, 4),
            "overall_evidence_state": self.overall_evidence_state.value,
            "overall_observation_quality": self.overall_observation_quality.value,
            "blockchain_authentication": self.blockchain_authentication.value,
            "score": round(self.score, 2) if self.score is not None else None,
            "confidence": round(self.confidence, 4) if self.confidence is not None else None,
            "probability_estimate": self.probability_estimate,
            "eligibility_verdict": self.eligibility_verdict,
            "exclusion_reasons": list(self.exclusion_reasons),
            "source_attributions": [s.to_dict() for s in self.source_attributions],
            "reproducible_input_hash": self.reproducible_input_hash,
        }

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


# ==============================================================================
# 4. Engine Configuration & Shared Input Model
# ==============================================================================

@dataclasses.dataclass(frozen=True)
class EngineConfig:
    """Immutable base configuration for a detection engine."""
    engine_name: str
    engine_version: str
    parameters: Dict[str, Any] = dataclasses.field(default_factory=dict)
    config_fingerprint: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        raw_repr = json.dumps(
            {"engine_name": self.engine_name, "engine_version": self.engine_version, "params": self.parameters},
            sort_keys=True,
        )
        fp = hashlib.sha256(raw_repr.encode("utf-8")).hexdigest()
        object.__setattr__(self, "config_fingerprint", fp)

    def get(self, key: str, default: Any = None) -> Any:
        return self.parameters.get(key, default)


@dataclasses.dataclass
class SignalEngineInput:
    """Shared input payload delivered to a detection engine."""
    asset: AssetIdentity
    as_of_timestamp: datetime
    run_id: str
    forecast_id: str
    raw_payloads: Dict[str, Any] = dataclasses.field(default_factory=dict)
    source_attributions: List[SourceEvidenceAttribution] = dataclasses.field(default_factory=list)

    def compute_input_hash(self) -> str:
        data = {
            "canonical_id": self.asset.canonical_id,
            "as_of_timestamp": self.as_of_timestamp.astimezone(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "forecast_id": self.forecast_id,
            "raw_payloads": self.raw_payloads,
        }
        raw = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ==============================================================================
# 5. Shared Risk, Independence & Gating Policies
# ==============================================================================

class SourceIndependencePolicy:
    """Evaluates provider independence to prevent correlated double-counting."""

    @staticmethod
    def evaluate_source_independence(attributions: List[SourceEvidenceAttribution]) -> Tuple[bool, str]:
        """Detects if multiple sources are merely echoing identical underlying trade feeds."""
        if not attributions:
            return False, "NO_ATTRIBUTIONS_PROVIDED"
        if len(attributions) == 1:
            return True, "SINGLE_SOURCE_PROVENANCE_RECORDED"

        seen_feeds: Dict[str, str] = {}
        correlated_sources = []
        for attr in attributions:
            feed_key = attr.underlying_feed_id or f"{attr.provider_name}:{attr.endpoint_or_method}"
            if feed_key in seen_feeds:
                correlated_sources.append((attr.provider_name, seen_feeds[feed_key]))
            else:
                seen_feeds[feed_key] = attr.provider_name

        if correlated_sources:
            return False, f"CORRELATED_SOURCES_DETECTED: {correlated_sources}"
        return True, "INDEPENDENT_SOURCES_VERIFIED"


class SecurityEvidencePolicy:
    """Strict security evaluation invariants."""

    @staticmethod
    def evaluate_security_evidence(
        is_honeypot: Optional[bool],
        is_blacklisted: Optional[bool],
        transfer_tax_buy_bps: Optional[float],
        transfer_tax_sell_bps: Optional[float],
        evidence_present: bool,
    ) -> Tuple[SecurityAssessmentState, List[str]]:
        """Evaluates contract security.

        Rule: Missing security evidence is NOT clean evidence.
        Rule: Hard reject requires defined, verified failure condition.
        """
        rejection_reasons: List[str] = []

        if not evidence_present or is_honeypot is None:
            return (
                SecurityAssessmentState.SECURITY_UNVERIFIED_EVIDENCE_MISSING,
                ["SECURITY_AUDIT_EVIDENCE_MISSING_CANNOT_TREAT_AS_CLEAN"],
            )

        if is_honeypot is True:
            rejection_reasons.append("HONEYPOT_DETECTED")
        if is_blacklisted is True:
            rejection_reasons.append("BLACKLIST_RESTRICTION_DETECTED")

        # Transfer tax threshold: max 10.0% (1000 bps)
        if transfer_tax_buy_bps is not None and transfer_tax_buy_bps > 1000.0:
            rejection_reasons.append(f"BUY_TAX_EXCEEDS_MAX_1000_BPS: {transfer_tax_buy_bps}")
        if transfer_tax_sell_bps is not None and transfer_tax_sell_bps > 1000.0:
            rejection_reasons.append(f"SELL_TAX_EXCEEDS_MAX_1000_BPS: {transfer_tax_sell_bps}")

        if rejection_reasons:
            return SecurityAssessmentState.SECURITY_FAIL_HARD_REJECT, rejection_reasons

        return SecurityAssessmentState.SECURITY_PASS_WITH_EVIDENCE, []


class LpLockEvidencePolicy:
    """Evaluates LP lock claims."""

    @staticmethod
    def evaluate_lp_lock(
        claimed_lock_pct: Optional[float],
        lock_contract_address: Optional[str],
        unlock_timestamp_epoch: Optional[int],
        as_of_epoch: int,
        beneficiary_address: Optional[str],
    ) -> Tuple[LpLockVerificationState, str]:
        """Validates LP lock claim against structural parameters.

        Rule: A provider-reported lock percentage alone is unverified.
        """
        if claimed_lock_pct is None or claimed_lock_pct <= 0:
            return LpLockVerificationState.LP_UNLOCKED_OR_UNKNOWN, "NO_LP_LOCK_CLAIMED"

        # Check for permanent burn to standard dead addresses
        if lock_contract_address and lock_contract_address.lower() in {
            "0x0000000000000000000000000000000000000000",
            "0x000000000000000000000000000000000000dead",
            "11111111111111111111111111111111",  # Solana system program / dead
        }:
            if claimed_lock_pct >= 90.0:
                return LpLockVerificationState.LP_BURN_VERIFIED_DEAD_ADDRESS, "VERIFIED_BURNED_TO_DEAD_ADDRESS"

        # If only percentage is reported without contract or unlock date:
        if not lock_contract_address or unlock_timestamp_epoch is None:
            return (
                LpLockVerificationState.LP_LOCK_UNVERIFIED_CLAIM_ONLY,
                "PROVIDER_REPORTED_PERCENTAGE_WITHOUT_CONTRACT_OR_TIMESTAMP",
            )

        # Validate that lock timestamp is in the future
        if unlock_timestamp_epoch <= as_of_epoch:
            return LpLockVerificationState.LP_UNLOCKED_OR_UNKNOWN, "LP_LOCK_ALREADY_EXPIRED"

        if claimed_lock_pct >= 90.0:
            return (
                LpLockVerificationState.LP_LOCK_VERIFIED_ONCHAIN_CONTRACT,
                f"VERIFIED_LOCKED_UNTIL_EPOCH_{unlock_timestamp_epoch}",
            )

        return (
            LpLockVerificationState.LP_LOCK_UNVERIFIED_CLAIM_ONLY,
            f"LP_LOCK_PERCENTAGE_BELOW_THRESHOLD: {claimed_lock_pct}%",
        )


# ==============================================================================
# 6. Deterministic Detection Engine Base Class
# ==============================================================================

class BaseDetectionEngine(abc.ABC):
    """Abstract deterministic interface implemented by all 9 detection engines."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config

    @property
    @abc.abstractmethod
    def engine_name(self) -> str:
        """Unique descriptive engine name."""
        ...

    @property
    @abc.abstractmethod
    def engine_version(self) -> str:
        """Semantic version of the engine."""
        ...

    @abc.abstractmethod
    def compute_code_fingerprint(self) -> str:
        """Deterministic hash representing the active engine code implementation."""
        ...

    @abc.abstractmethod
    def validate_inputs(self, engine_input: SignalEngineInput) -> Tuple[bool, List[str]]:
        """Validates payload structure, timestamp bounds, and formatting."""
        ...

    @abc.abstractmethod
    def calculate_features(self, engine_input: SignalEngineInput) -> Dict[str, Any]:
        """Computes deterministic quantitative features without scoring."""
        ...

    @abc.abstractmethod
    def score_features(
        self, features: Dict[str, Any]
    ) -> Tuple[Optional[float], Optional[float], EvidenceState, ObservationQuality, List[str]]:
        """Computes normalized score [0, 100], confidence [0, 1], and reason codes."""
        ...

    def evaluate(self, engine_input: SignalEngineInput) -> SignalEvidenceRecord:
        """Top-level evaluation pipeline executing validation -> feature calculation -> scoring."""
        # 1. Validation
        is_valid, validation_errors = self.validate_inputs(engine_input)
        now_utc = datetime.now(timezone.utc).isoformat()
        as_of_str = engine_input.as_of_timestamp.astimezone(timezone.utc).isoformat()

        if not is_valid:
            record = SignalEvidenceRecord(
                engine_name=self.engine_name,
                engine_version=self.engine_version,
                run_id=engine_input.run_id,
                forecast_id=engine_input.forecast_id,
                configuration_fingerprint=self.config.config_fingerprint,
                code_fingerprint=self.compute_code_fingerprint(),
                asset=engine_input.asset,
                decision_timestamp_utc=now_utc,
                as_of_timestamp_utc=as_of_str,
                coverage_completeness=0.0,
                overall_evidence_state=EvidenceState.ERROR,
                overall_observation_quality=ObservationQuality.UNAVAILABLE,
                score=None,
                confidence=None,
                probability_estimate=None,
                eligibility_verdict="UNAVAILABLE",
                exclusion_reasons=validation_errors,
                source_attributions=list(engine_input.source_attributions),
                reproducible_input_hash=engine_input.compute_input_hash(),
            )
            return record

        # 2. Feature calculation
        features = self.calculate_features(engine_input)

        # 3. Scoring
        score, confidence, ev_state, obs_quality, exclusions = self.score_features(features)

        # 4. Extract measurements list from features
        measurements: List[MeasurementAttribution] = features.get("measurements", [])
        coverage: float = float(features.get("coverage_completeness", 0.0))

        # Eligibility determination
        verdict = "ELIGIBLE"
        min_score = float(self.config.get("min_score_eligible", 50.0))
        if ev_state in {EvidenceState.UNAVAILABLE, EvidenceState.ERROR, EvidenceState.MISSING}:
            verdict = "UNAVAILABLE"
        elif exclusions:
            verdict = "HARD_REJECT" if any("REJECT" in e or "FAIL" in e for e in exclusions) else "INELIGIBLE"
        elif score is None or score < min_score:
            verdict = "INELIGIBLE"

        return SignalEvidenceRecord(
            engine_name=self.engine_name,
            engine_version=self.engine_version,
            run_id=engine_input.run_id,
            forecast_id=engine_input.forecast_id,
            configuration_fingerprint=self.config.config_fingerprint,
            code_fingerprint=self.compute_code_fingerprint(),
            asset=engine_input.asset,
            decision_timestamp_utc=now_utc,
            as_of_timestamp_utc=as_of_str,
            measurements=measurements,
            coverage_completeness=coverage,
            overall_evidence_state=ev_state,
            overall_observation_quality=obs_quality,
            score=score,
            confidence=confidence,
            probability_estimate=None,  # Never invent probability estimates without empirical calibration
            eligibility_verdict=verdict,
            exclusion_reasons=exclusions,
            source_attributions=list(engine_input.source_attributions),
            reproducible_input_hash=engine_input.compute_input_hash(),
        )
