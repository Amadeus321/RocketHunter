#!/usr/bin/env python3
"""Stage 3.1: Research Evidence, Provenance & Multi-Chain Finality Engine for Rocket Hunter v4.30.

Authoritatively implements Stage 3.1 architectural components with independent review corrections:
1. Reproducibility Manifest & Protected Data Verifier:
   - Enforces SHA-256 pre/post-run integrity of protected project data:
     `user_positions_journal.json`, `ai_learning_engine.json`, `notification_config.json`,
     `config/` files, and `scanner-memory/` partitions.
   - Strictly validates all SHA-256 digests as 64-character hexadecimal strings (fails closed).
   - Detects file modifications, deletions, and unauthorized newly created files (fails closed).
2. Three-Tiered RPC Evidence Preservation & Provenance Engine:
   - Tier 1: Payload Integrity with persistent raw wire request/response disk retention
     and independent expected SHA-256 digest validation (non-tautological).
   - Tier 2 (Downgraded): Categorized strictly as caller-asserted metadata (`ENDPOINT_METADATA_UNVERIFIED`);
     does not claim cryptographic TLS authentication without documented CA/pin verification proofs.
   - Tier 3 (Caller-Asserted / Unverified): Dual-provider consensus verification and chain-specific finality
     evaluations (Ethereum Beacon checkpoint, Arbitrum Nitro assertion confirm window, OP Stack dispute games,
     and Solana rooted slots) evaluate caller-supplied metadata and delay thresholds. In the absence of
     on-chain cryptographic proof validation (e.g. BLS signatures, Merkle inclusion proofs, authenticated
     provider identities), blocks and assertions are classified as `FINALITY_CALLER_ASSERTED_UNVERIFIED`
     and are never promoted to `FINALIZED_CANONICAL`. Discrepancies fail closed to `FINALITY_PENDING` or
     `BLOCK_REORGANIZED_ORPHAN`.
3. Configuration-Driven Multi-Chain Finality State Machine:
   - Evaluates caller-supplied state transition metadata and delay parameters for Arbitrum Nitro,
     OP Stack (op-contracts/v1.6.0 commit `33f06d2d5e4034125df02264a5ffe84571bd0359`), and Solana.
     Direct public evaluations emit caller-asserted/unverified statuses (`FINALITY_CALLER_ASSERTED_UNVERIFIED`)
     rather than canonical finality status when dispute or confirmation delays elapse.
4. Venue-Specific Execution Evidence Data Structures:
   - Uniswap V2: Chain ID, pool, token0 != token1, fee bounds (0 < fee <= 10000 bps), positive reserves.
   - Uniswap V3: Validates canonical fee tiers and tick spacings, mathematical tick/price consistency
     (`UniswapV3TickMath.get_tick_at_sqrt_ratio(sqrtPriceX96) == tick`), full bitmap coverage across
     the simulated path, and bidirectional tick-bitmap consistency (every bit set has an initialized
     tick with `liquidityGross > 0`, and every initialized tick in range has its bit set in the bitmap).
   - Canonical Uniswap V3 TickMath: sqrtPriceX96(t) = ceil(sqrt(1.0001^t) * 2^96).
     Reconciles tick 204520 fixture = 2186517289083564169714246582247698.
5. Dataset A Trade Journal Financial Reconciliation:
   - Binds `INDEPENDENTLY_VERIFIED_EXECUTION` to validated on-chain transaction receipts
     (`ValidatedTransactionReceipt`) and transfer logs matching exact chain, tokens, wallets, and quantities.
   - Rejects unverified receipts to `USER_REPORTED_EXECUTION` (excluded from verified P/L).
   - Strictly REJECTS `tokens_sold > tokens_acquired` (fails closed as `INVALID_TRANCHE_QUANTITY_EXCEEDED`).
   - Validates finite, non-negative amounts; segregates unsold inventory into `UNREALIZED_INVENTORY`.
6. Statistical Validation & Multi-Block Dependence Engine:
   - Chronological sorting of weekly time blocks.
   - Structural reporting of (M_tokens, B_weeks, N_obs) without claiming scalar N_eff.
   - Explicit handling of empty (N=0) and single-observation (N=1) datasets.
   - Cameron-Gelbach-Miller (2011) Two-Way Clustered Variance with Cameron & Miller (2015) fallback.
   - Newey-West (1987) / Bartlett kernel time-series variance fallback on weekly block means.
   - Exact Student's t critical values for actual degrees of freedom df = min(M - 1, B - 1).
   - Strictly brands all inference as exploratory (`EXPLORATORY_UNCALIBRATED_INFERENCE` or
     `DEPENDENT_BLOCK_FALLBACK_TRIGGERED`); never claims "independence confirmed".
"""
from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import decimal
import enum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# Decimal context with 100 digits of precision for canonical TickMath verification
decimal.getcontext().prec = 100

STAGE_31_VERSION = "4.30.0-stage3.1"
STAGE_32_VERSION = "4.30.0-stage3.2"
APPROVED_BASELINE_COMMIT = "2bba7cc9cd7db59ce428252050f882b99c581f61"
CANONICAL_OP_COMMIT = "33f06d2d5e4034125df02264a5ffe84571bd0359"
CANONICAL_OP_TAG = "op-contracts/v1.6.0"

# Technical target requirements
TECHNICAL_TP_PCT = 40.00
MAX_HOLDING_HOURS = 24.0

# Standard simulation scenario tranches (strictly hypothetical depth probing, NOT actual position sizing)
SIMULATION_TRANCHES_USD: List[float] = [100.0, 500.0, 1000.0, 2500.0, 5000.0]

# Uniswap V3 canonical fee tier to tick spacing mapping
UNISWAP_V3_FEE_SPACINGS: Dict[int, int] = {
    100: 1,      # 0.01%
    500: 10,     # 0.05%
    3000: 60,    # 0.30%
    10000: 200,  # 1.00%
}


# ==============================================================================
# 1. State Enums & Value Objects
# ==============================================================================

class FinalityState(str, enum.Enum):
    """Protocol-level block and assertion settlement states."""
    FINALITY_PENDING = "FINALITY_PENDING"
    FINALITY_CALLER_ASSERTED_UNVERIFIED = "FINALITY_CALLER_ASSERTED_UNVERIFIED"
    FINALIZED_CANONICAL = "FINALIZED_CANONICAL"
    BLOCK_REORGANIZED_ORPHAN = "BLOCK_REORGANIZED_ORPHAN"
    L1_BATCH_FINALITY_ONLY = "L1_BATCH_FINALITY_ONLY"
    L2_STATE_UNSAFE = "L2_STATE_UNSAFE"
    L2_STATE_SAFE = "L2_STATE_SAFE"
    L2_STATE_FINALIZED_L1_DA = "L2_STATE_FINALIZED_L1_DA"
    DISPUTE_GAME_IN_PROGRESS = "DISPUTE_GAME_IN_PROGRESS"
    PROPOSAL_INVALIDATED_QUARANTINE = "PROPOSAL_INVALIDATED_QUARANTINE"
    DISPUTE_GAME_RESOLVED_DEFENDER = "DISPUTE_GAME_RESOLVED_DEFENDER"
    DISPUTE_GAME_FINALIZED = "DISPUTE_GAME_FINALIZED"
    L2_OUTPUT_FINALIZED_CANONICAL = "L2_OUTPUT_FINALIZED_CANONICAL"
    WITHDRAWAL_PROOF_MATURE_FINAL = "WITHDRAWAL_PROOF_MATURE_FINAL"
    WITHDRAWAL_PENDING_MATURITY = "WITHDRAWAL_PENDING_MATURITY"


class ModeledExecutionStatus(str, enum.Enum):
    """Execution simulation evidence categories. NEVER labeled verified."""
    MODELED_EXECUTION_WITH_ASSUMPTIONS = "MODELED_EXECUTION_WITH_ASSUMPTIONS"
    EXECUTION_UNVERIFIED = "EXECUTION_UNVERIFIED"


class DualTrackTargetStatus(str, enum.Enum):
    """Dual-track technical vs executable target attainment classifications."""
    # Market price track (unverified telemetry)
    MARKET_TP40_TOUCHED_UNVERIFIED = "MARKET_TP40_TOUCHED_UNVERIFIED"
    MARKET_TP_NOT_REACHED = "MARKET_TP_NOT_REACHED"
    MARKET_TARGET_UNRESOLVED = "MARKET_TARGET_UNRESOLVED"

    # Executable depth simulation track (strictly modeled)
    MODELED_TP40_EXECUTABLE_WITH_ASSUMPTIONS = "MODELED_TP40_EXECUTABLE_WITH_ASSUMPTIONS"
    MODELED_TP40_EXECUTION_IMPACT_FAIL = "MODELED_TP40_EXECUTION_IMPACT_FAIL"
    MODELED_TP40_LIQUIDITY_INSUFFICIENT = "MODELED_TP40_LIQUIDITY_INSUFFICIENT"
    MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING = "MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING"
    MODELED_DEPTH_UNRESOLVED = "MODELED_DEPTH_UNRESOLVED"


class LiquidityClassificationStatus(str, enum.Enum):
    """Liquidity classification status separating fixed-band depth, price markdown, and event removal."""
    LIQUIDITY_DEPTH_COLLAPSE_OBSERVED_UNVERIFIED = "LIQUIDITY_DEPTH_COLLAPSE_OBSERVED_UNVERIFIED"
    LP_WITHDRAWAL_COLLAPSE_OBSERVED_UNVERIFIED = "LP_WITHDRAWAL_COLLAPSE_OBSERVED_UNVERIFIED"
    UNRESOLVED_LIQUIDITY = "UNRESOLVED_LIQUIDITY"
    LIQUIDITY_DEPTH_DEGRADED_OBSERVED_UNVERIFIED = "LIQUIDITY_DEPTH_DEGRADED_OBSERVED_UNVERIFIED"
    ASSET_PRICE_DEPRECIATION_OBSERVED_UNVERIFIED = "ASSET_PRICE_DEPRECIATION_OBSERVED_UNVERIFIED"
    LIQUIDITY_DEPTH_SURVIVED_OBSERVED_UNVERIFIED = "LIQUIDITY_DEPTH_SURVIVED_OBSERVED_UNVERIFIED"


class EvidenceTier(str, enum.Enum):
    """Dataset A trade journal evidence classifications."""
    INDEPENDENTLY_VERIFIED_EXECUTION = "INDEPENDENTLY_VERIFIED_EXECUTION"
    USER_REPORTED_EXECUTION = "USER_REPORTED_EXECUTION"
    EXECUTION_EVIDENCE_INCOMPLETE = "EXECUTION_EVIDENCE_INCOMPLETE"
    RECEIPT_VERIFICATION_FAILED = "RECEIPT_VERIFICATION_FAILED"
    IDENTITY_RECONCILIATION_FAILED = "IDENTITY_RECONCILIATION_FAILED"
    INVALID_TRANCHE_QUANTITY_EXCEEDED = "INVALID_TRANCHE_QUANTITY_EXCEEDED"
    NON_FINITE_OR_NEGATIVE_VALUES = "NON_FINITE_OR_NEGATIVE_VALUES"


class InferentialStatus(str, enum.Enum):
    """Statistical inference classification. Confirmatory claims are explicitly prohibited."""
    EXPLORATORY_UNCALIBRATED_INFERENCE = "EXPLORATORY_UNCALIBRATED_INFERENCE"
    DEPENDENT_BLOCK_FALLBACK_TRIGGERED = "DEPENDENT_BLOCK_FALLBACK_TRIGGERED"


# ==============================================================================
# 2. Canonical Uniswap V3 TickMath & Boundary Mathematics
# ==============================================================================

class UniswapV3TickMath:
    """Canonical integer implementation of Uniswap V3 TickMath.sol.
    
    Source: @uniswap/v3-core/contracts/libraries/TickMath.sol
    Commit: d8b97e6005047b19a79cc7a53c30ea92e596bb05
    Formula: sqrtPriceX96(t) = ceil(sqrt(1.0001^t) * 2^96)
    """

    MIN_TICK: int = -887272
    MAX_TICK: int = 887272
    MIN_SQRT_RATIO: int = 4295128739
    MAX_SQRT_RATIO: int = 1461446703485210103287273052203988822378723970342

    @classmethod
    def get_sqrt_ratio_at_tick(cls, tick: int) -> int:
        """Computes sqrtPriceX96 using canonical fixed-point binary exponentiation."""
        abs_tick = abs(tick)
        if abs_tick > cls.MAX_TICK:
            raise ValueError(f"Tick {tick} exceeds protocol boundary [-887272, 887272]")

        # Binary exponentiation constants from TickMath.sol
        ratio = 0xfffcb933bd6fad37aa2d162d1a594001 if (abs_tick & 0x1 != 0) else (1 << 128)
        if abs_tick & 0x2 != 0: ratio = (ratio * 0xfff97272373d413259a46990580e213a) >> 128
        if abs_tick & 0x4 != 0: ratio = (ratio * 0xfff2e50f5f656932ef12357cf3c7fdcc) >> 128
        if abs_tick & 0x8 != 0: ratio = (ratio * 0xffe5caca7e10e4e61c3624eaa0941cd0) >> 128
        if abs_tick & 0x10 != 0: ratio = (ratio * 0xffcb9843d60f6159c9db58835c30d443) >> 128
        if abs_tick & 0x20 != 0: ratio = (ratio * 0xff973b41fa98c081472e6896dfb254c0) >> 128
        if abs_tick & 0x40 != 0: ratio = (ratio * 0xff2ea16466c96a3843ec78b326b52861) >> 128
        if abs_tick & 0x80 != 0: ratio = (ratio * 0xfe5dee046a99a2a811c461f1969c3053) >> 128
        if abs_tick & 0x100 != 0: ratio = (ratio * 0xfcbe86c7900a88aedcffc83b479aa3a4) >> 128
        if abs_tick & 0x200 != 0: ratio = (ratio * 0xf987a7253ac413176f2b074cf7815e54) >> 128
        if abs_tick & 0x400 != 0: ratio = (ratio * 0xf3392b0822b70005940c7a398e4b70f3) >> 128
        if abs_tick & 0x800 != 0: ratio = (ratio * 0xe7159475a2c29b7443b29c7fa6e889d9) >> 128
        if abs_tick & 0x1000 != 0: ratio = (ratio * 0xd097f3bdfd2022b8845ad8f792aa5825) >> 128
        if abs_tick & 0x2000 != 0: ratio = (ratio * 0xa9f746462d870fdf8a65dc1f90e061e5) >> 128
        if abs_tick & 0x4000 != 0: ratio = (ratio * 0x70d869a156d2a1b890bb3df62baf32f7) >> 128
        if abs_tick & 0x8000 != 0: ratio = (ratio * 0x31be135b97d08fd981231505542fcfa6) >> 128
        if abs_tick & 0x10000 != 0: ratio = (ratio * 0x9aa508b5b7a84e1c677de54f3e99bc9) >> 128
        if abs_tick & 0x20000 != 0: ratio = (ratio * 0x5d6af8dedb81196699c329225ee604) >> 128
        if abs_tick & 0x40000 != 0: ratio = (ratio * 0x2216e584f5fa1ea926041bedfe98) >> 128
        if abs_tick & 0x80000 != 0: ratio = (ratio * 0x48a170391f7dc42444e8fa2) >> 128

        if tick > 0:
            ratio = ((1 << 256) - 1) // ratio

        # Canonical round-up rule (equivalent to ceil)
        sqrt_price_x96 = (ratio >> 32) + (1 if (ratio % (1 << 32) != 0) else 0)
        return sqrt_price_x96

    @classmethod
    def get_tick_at_sqrt_ratio(cls, sqrt_price_x96: int) -> int:
        """Inverts sqrtPriceX96 to find the greatest tick t such that getSqrtRatioAtTick(t) <= sqrt_price_x96."""
        if not (cls.MIN_SQRT_RATIO <= sqrt_price_x96 < cls.MAX_SQRT_RATIO):
            raise ValueError(f"sqrtPriceX96 {sqrt_price_x96} out of bounds [{cls.MIN_SQRT_RATIO}, {cls.MAX_SQRT_RATIO})")

        # Binary search across the tick range [-887272, 887272]
        low = cls.MIN_TICK
        high = cls.MAX_TICK
        ans = low
        while low <= high:
            mid = (low + high) // 2
            ratio = cls.get_sqrt_ratio_at_tick(mid)
            if ratio <= sqrt_price_x96:
                ans = mid
                low = mid + 1
            else:
                high = mid - 1
        return ans

    @classmethod
    def compute_exact_ceil_scaled(cls, tick: int) -> int:
        """High-precision Decimal reference computation of ceil(sqrt(1.0001^t) * 2^96)."""
        base = decimal.Decimal("1.0001")
        t_dec = decimal.Decimal(tick)
        price_raw = base ** t_dec
        sqrt_price = price_raw.sqrt()
        scaled = sqrt_price * (decimal.Decimal(2) ** 96)
        return math.ceil(scaled)


# ==============================================================================
# 3. Reproducibility Manifest & Protected Data Verifier
# ==============================================================================

class ProtectedDataVerifier:
    """Computes and validates pre-run and post-run hashes of all protected project data.
    
    Detects file modifications, deletions, and unauthorized newly created files across:
    - `rocket-hunter-data/user_positions_journal.json`
    - `rocket-hunter-data/ai_learning_engine.json`
    - `rocket-hunter-data/notification_config.json`
    - `config/` (all production configs if directory exists)
    - `rocket-hunter-data/scanner-memory/` (all partition files recursively)
    """

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root = root_dir or Path(__file__).resolve().parents[1]
        self.data_dir = self.root / "rocket-hunter-data"
        self.config_dir = self.root / "config"

    def compute_file_sha256(self, file_path: Path) -> str | None:
        if not file_path.is_file():
            return None
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest().upper()

    def snapshot_protected_data(self) -> dict[str, Any]:
        """Creates an immutable manifest of protected data hashes and inventory."""
        snapshot = {
            "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "protected_files": {},
            "scanner_memory_partitions": {},
            "config_files": {},
        }

        # 1. Known protected files
        known_files = [
            "user_positions_journal.json",
            "ai_learning_engine.json",
            "notification_config.json",
        ]
        for fname in known_files:
            fpath = self.data_dir / fname
            if fpath.is_file():
                snapshot["protected_files"][fname] = {
                    "path": str(fpath),
                    "sha256": self.compute_file_sha256(fpath),
                    "size_bytes": fpath.stat().st_size,
                }

        # 2. Production config directory
        if self.config_dir.is_dir():
            for p in sorted(self.config_dir.rglob("*")):
                if p.is_file():
                    rel_p = str(p.relative_to(self.root))
                    snapshot["config_files"][rel_p] = {
                        "sha256": self.compute_file_sha256(p),
                        "size_bytes": p.stat().st_size,
                    }

        # 3. Scanner memory partitions (recursively captures all files)
        memory_dir = self.data_dir / "scanner-memory"
        if memory_dir.is_dir():
            for p in sorted(memory_dir.rglob("*")):
                if p.is_file():
                    rel_p = str(p.relative_to(self.root))
                    snapshot["scanner_memory_partitions"][rel_p] = {
                        "sha256": self.compute_file_sha256(p),
                        "size_bytes": p.stat().st_size,
                    }

        return snapshot

    def verify_clean_equivalence(self, pre_snapshot: dict[str, Any], post_snapshot: dict[str, Any]) -> Tuple[bool, list[str]]:
        """Verifies zero protected files were modified, created, or deleted (fails closed).
        Strictly rejects malformed or incomplete snapshots missing required categories.
        Validates all SHA-256 digests as strictly 64-character hexadecimal strings.
        """
        discrepancies = []

        if not isinstance(pre_snapshot, dict) or not isinstance(post_snapshot, dict):
            return False, ["Malformed snapshot: pre_snapshot and post_snapshot must be dictionaries"]

        categories = ["protected_files", "scanner_memory_partitions", "config_files"]
        for cat in categories:
            if cat not in pre_snapshot:
                discrepancies.append(f"Incomplete pre_snapshot: missing required category '{cat}'")
            elif not isinstance(pre_snapshot[cat], dict):
                discrepancies.append(f"Malformed pre_snapshot: category '{cat}' must be a dictionary")

            if cat not in post_snapshot:
                discrepancies.append(f"Incomplete post_snapshot: missing required category '{cat}'")
            elif not isinstance(post_snapshot[cat], dict):
                discrepancies.append(f"Malformed post_snapshot: category '{cat}' must be a dictionary")

        if discrepancies:
            return False, discrepancies

        hex_64_pattern = re.compile(r"^[0-9A-Fa-f]{64}$")

        for cat in categories:
            pre_dict = pre_snapshot[cat]
            post_dict = post_snapshot[cat]

            # Detect deletions, invalid metadata entries, and malformed digests
            for path, meta in pre_dict.items():
                if not isinstance(meta, dict) or "sha256" not in meta:
                    discrepancies.append(f"Malformed pre_snapshot metadata entry in {cat}: {path}")
                    continue

                pre_sha256 = meta.get("sha256")
                if not isinstance(pre_sha256, str) or not hex_64_pattern.match(pre_sha256):
                    discrepancies.append(
                        f"Malformed or invalid SHA-256 digest in pre_snapshot {cat}: {path} ({pre_sha256!r})"
                    )

                if path not in post_dict:
                    discrepancies.append(f"Protected item deleted in {cat}: {path}")
                elif not isinstance(post_dict[path], dict) or "sha256" not in post_dict[path]:
                    discrepancies.append(f"Malformed post_snapshot metadata entry in {cat}: {path}")
                else:
                    post_sha256 = post_dict[path].get("sha256")
                    if not isinstance(post_sha256, str) or not hex_64_pattern.match(post_sha256):
                        discrepancies.append(
                            f"Malformed or invalid SHA-256 digest in post_snapshot {cat}: {path} ({post_sha256!r})"
                        )
                    if pre_sha256 != post_sha256:
                        discrepancies.append(
                            f"Protected file modified in {cat}: {path} ({pre_sha256} -> {post_sha256})"
                        )

            # Detect unauthorized creations
            for path in post_dict:
                if path not in pre_dict:
                    discrepancies.append(f"Unauthorized new file created in {cat}: {path}")

        return len(discrepancies) == 0, discrepancies


# ==============================================================================
# 4. Three-Tiered RPC Evidence Preservation & Provenance Engine
# ==============================================================================

@dataclasses.dataclass
class RpcEvidenceRecord:
    """Full wire request/response provenance record with independent expected digests."""
    rpc_method: str
    rpc_params: list[Any]
    provider_id: str
    provider_domain: str
    retrieval_timestamp_utc: str
    raw_request_bytes: bytes
    raw_response_bytes: bytes
    expected_request_sha256: str
    expected_response_sha256: str
    chain_id: int
    block_number: int
    block_hash: str
    parent_hash: str
    state_root: str
    tls_handshake_metadata: dict[str, Any]

    @property
    def computed_request_sha256(self) -> str:
        return hashlib.sha256(self.raw_request_bytes).hexdigest().upper()

    @property
    def computed_response_sha256(self) -> str:
        return hashlib.sha256(self.raw_response_bytes).hexdigest().upper()


class RpcProvenanceVerifier:
    """Decoupled 3-tier RPC verifier with non-tautological wire retention and distinct peer quorum."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or (Path(__file__).resolve().parents[1] / "rocket-hunter-data")
        self.evidence_dir = self.data_dir / "rpc_evidence"
        self.quarantine_dir = self.data_dir / "quarantined_reorg"

    def persist_raw_wire_payloads(self, record: RpcEvidenceRecord) -> Tuple[Path, Path]:
        """Persists raw request and response bytes to disk and returns paths."""
        dest_dir = self.evidence_dir / str(record.chain_id) / str(record.block_number)
        dest_dir.mkdir(parents=True, exist_ok=True)
        req_path = dest_dir / f"req_{record.computed_request_sha256}.bin"
        resp_path = dest_dir / f"resp_{record.computed_response_sha256}.bin"
        with open(req_path, "wb") as f:
            f.write(record.raw_request_bytes)
        with open(resp_path, "wb") as f:
            f.write(record.raw_response_bytes)
        return req_path, resp_path

    def verify_tier1_payload_integrity(self, record: RpcEvidenceRecord) -> bool:
        """Verifies raw wire bytes against independently recorded expected digests (non-tautological)."""
        if not record.raw_request_bytes or not record.raw_response_bytes:
            return False

        # Independent comparison: computed hash must match independently provided expected hash
        if record.computed_request_sha256 != record.expected_request_sha256.upper():
            return False
        if record.computed_response_sha256 != record.expected_response_sha256.upper():
            return False

        # Confirm persisted wire bytes on disk match
        req_path, resp_path = self.persist_raw_wire_payloads(record)
        with open(req_path, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest().upper() != record.expected_request_sha256.upper():
                return False
        with open(resp_path, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest().upper() != record.expected_response_sha256.upper():
                return False

        return True

    def verify_tier2_endpoint_metadata(self, record: RpcEvidenceRecord) -> Tuple[bool, str]:
        """Downgraded Tier 2: Categorized strictly as caller-asserted metadata without cryptographic CA proof."""
        # Does NOT claim cryptographic TLS authentication; explicitly reports unverified transport status
        if not record.provider_domain or not record.provider_id:
            return False, "ENDPOINT_METADATA_INCOMPLETE"
        return False, "ENDPOINT_METADATA_RECORDED_UNVERIFIED"

    def verify_tier3_canonical_quorum(
        self,
        record: RpcEvidenceRecord,
        independent_peer_record: Optional[RpcEvidenceRecord] = None,
        finality_proof: Optional[dict[str, Any]] = None,
    ) -> Tuple[bool, FinalityState, str]:
        """Dual-provider quorum cross-check + chain-specific canonical finality evidence."""
        # 1. Require independent peer record
        if independent_peer_record is None:
            return False, FinalityState.FINALITY_PENDING, "INDEPENDENT_PEER_QUORUM_MISSING"

        # 2. Require distinct independent providers
        if record.provider_id == independent_peer_record.provider_id:
            return False, FinalityState.FINALITY_PENDING, "QUORUM_REQUIRES_DISTINCT_INDEPENDENT_PROVIDERS"

        # 3. Validate matching chain ID and block number
        if record.chain_id != independent_peer_record.chain_id:
            return False, FinalityState.FINALITY_PENDING, "CHAIN_ID_MISMATCH_ACROSS_PROVIDERS"
        if record.block_number != independent_peer_record.block_number:
            return False, FinalityState.FINALITY_PENDING, "BLOCK_NUMBER_MISMATCH_ACROSS_PROVIDERS"

        # 4. Check block hash, parent hash, and state root consensus across distinct peers
        if (
            record.block_hash != independent_peer_record.block_hash
            or record.parent_hash != independent_peer_record.parent_hash
            or record.state_root != independent_peer_record.state_root
        ):
            self.quarantine_reorganized_block(record, reason="Dual-provider block hash/state root divergence")
            return False, FinalityState.BLOCK_REORGANIZED_ORPHAN, "BLOCK_HASH_DIVERGENCE_REORG_ORPHAN"

        # 5. Require chain-specific canonical finality proof
        if finality_proof is None:
            return False, FinalityState.FINALITY_PENDING, "CHAIN_SPECIFIC_FINALITY_PROOF_MISSING"

        proof_type = finality_proof.get("proof_type")

        # Ethereum L1 (chain_id == 1): Beacon Chain finalized checkpoint proof
        if record.chain_id == 1:
            if proof_type != "ETHEREUM_BEACON_FINALITY":
                return False, FinalityState.FINALITY_PENDING, "INVALID_PROOF_TYPE_FOR_ETHEREUM_L1"
            checkpoint_root = finality_proof.get("checkpoint_root")
            checkpoint_block_hash = finality_proof.get("checkpoint_block_hash")
            epoch = finality_proof.get("checkpoint_epoch", 0)
            if not checkpoint_root or not checkpoint_block_hash or epoch <= 0:
                return False, FinalityState.FINALITY_PENDING, "BEACON_CHECKPOINT_PROOF_INCOMPLETE"
            if checkpoint_block_hash != record.block_hash:
                return False, FinalityState.FINALITY_PENDING, "BEACON_CHECKPOINT_HASH_MISMATCH"

        # Arbitrum One (chain_id == 42161): Assertion confirmation period evaluation
        elif record.chain_id == 42161:
            if proof_type != "ARBITRUM_NITRO_ASSERTION":
                return False, FinalityState.FINALITY_PENDING, "INVALID_PROOF_TYPE_FOR_ARBITRUM"
            state = MultiChainFinalityEngine.evaluate_arbitrum(
                l1_batch_finalized=finality_proof.get("l1_batch_finalized", False),
                assertion_created_l1_block=finality_proof.get("assertion_created_l1_block", 0),
                current_finalized_l1_block=finality_proof.get("current_finalized_l1_block", 0),
                confirm_period_blocks=finality_proof.get("confirm_period_blocks", 45818),
                challenge_status=finality_proof.get("challenge_status", "NONE"),
            )
            if state != FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED:
                return False, state, "ARBITRUM_ASSERTION_NOT_FINALIZED"

        # OP Stack / Base (chain_id == 8453): Fault proof dispute game resolution and finality delay
        elif record.chain_id == 8453:
            if proof_type != "OP_STACK_FAULT_PROOF":
                return False, FinalityState.FINALITY_PENDING, "INVALID_PROOF_TYPE_FOR_OP_STACK"
            state = MultiChainFinalityEngine.evaluate_op_stack(
                l1_batch_finalized=finality_proof.get("l1_batch_finalized", False),
                proposal_game_type=finality_proof.get("proposal_game_type", 1),
                respected_game_type=finality_proof.get("respected_game_type", 1),
                game_status=finality_proof.get("game_status", 0),
                resolved_at=finality_proof.get("resolved_at", 0),
                current_l1_timestamp=finality_proof.get("current_l1_timestamp", 0),
                dispute_game_finality_delay_seconds=finality_proof.get("dispute_game_finality_delay_seconds", 302400),
            )
            if state != FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED:
                return False, state, "OP_STACK_OUTPUT_ROOT_NOT_CANONICAL"

        # Solana (chain_id == 101): Rooted Tower BFT commitment
        elif record.chain_id == 101:
            if proof_type != "SOLANA_TOWER_BFT":
                return False, FinalityState.FINALITY_PENDING, "INVALID_PROOF_TYPE_FOR_SOLANA"
            state = MultiChainFinalityEngine.evaluate_solana(
                commitment=finality_proof.get("commitment", ""),
                is_rooted=finality_proof.get("is_rooted", False),
            )
            if state != FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED:
                return False, state, "SOLANA_SLOT_NOT_ROOTED_FINALIZED"

        else:
            return False, FinalityState.FINALITY_PENDING, "UNSUPPORTED_CHAIN_FINALITY_SPECIFICATION"

        # Even when caller-provided proof fields and distinct provider IDs match,
        # in the absence of in-engine cryptographic proof validation (Merkle/BLS)
        # and authenticated provider identities, blocks are never promoted to FINALIZED_CANONICAL.
        return False, FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED, "CALLER_ASSERTED_FINALITY_NOT_CRYPTOGRAPHICALLY_VERIFIED"

    def quarantine_reorganized_block(self, record: RpcEvidenceRecord, reason: str) -> Path:
        """Isolates reorganized or unlinked block artifacts into quarantine sink."""
        dest = self.quarantine_dir / str(record.chain_id) / str(record.block_number)
        dest.mkdir(parents=True, exist_ok=True)
        manifest = {
            "reason": reason,
            "quarantined_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "chain_id": record.chain_id,
            "block_number": record.block_number,
            "block_hash": record.block_hash,
            "parent_hash": record.parent_hash,
            "state_root": record.state_root,
            "provider_id": record.provider_id,
            "provider_domain": record.provider_domain,
            "request_sha256": record.expected_request_sha256,
            "response_sha256": record.expected_response_sha256,
        }
        with open(dest / "quarantine_manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        return dest


# ==============================================================================
# 5. Configuration-Driven Multi-Chain Finality State Machine
# ==============================================================================

class MultiChainFinalityEngine:
    """Evaluates protocol finality state transitions from caller-provided metadata and delay parameters.
    
    In the absence of on-chain cryptographic proofs (e.g. Merkle inclusion proofs, BLS signature verification,
    or authenticated provider identities), evaluations on caller-provided scalars/booleans return
    `FINALITY_CALLER_ASSERTED_UNVERIFIED` rather than promoting to canonical finality.
    """

    @staticmethod
    def evaluate_arbitrum(
        l1_batch_finalized: bool,
        assertion_created_l1_block: int,
        current_finalized_l1_block: int,
        confirm_period_blocks: int,
        challenge_status: str = "NONE",
    ) -> FinalityState:
        """Evaluates Arbitrum Nitro assertion finality against dynamic confirmPeriodBlocks().
        
        Returns FINALITY_CALLER_ASSERTED_UNVERIFIED when the dispute window has elapsed,
        as on-chain assertion state is caller-provided and not cryptographically proven.
        """
        if not l1_batch_finalized:
            return FinalityState.FINALITY_PENDING

        if challenge_status in {"ACTIVE", "UNRESOLVED", "CHALLENGED"}:
            return FinalityState.FINALITY_PENDING

        elapsed_blocks = current_finalized_l1_block - assertion_created_l1_block
        if elapsed_blocks >= confirm_period_blocks and challenge_status == "NONE":
            return FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED

        # L1 batch is finalized, but dispute window is still open
        return FinalityState.L1_BATCH_FINALITY_ONLY

    @staticmethod
    def evaluate_op_stack(
        l1_batch_finalized: bool,
        proposal_game_type: int,
        respected_game_type: int,
        game_status: int,  # 0: IN_PROGRESS, 1: CHALLENGER_WINS, 2: DEFENDER_WINS
        resolved_at: int,
        current_l1_timestamp: int,
        dispute_game_finality_delay_seconds: int,
    ) -> FinalityState:
        """Evaluates OP Stack fault-proof output root states (op-contracts/v1.6.0).
        
        Returns FINALITY_CALLER_ASSERTED_UNVERIFIED when the dispute game is resolved and
        the airgap finality delay has elapsed, as caller-provided scalars lack cryptographic verification.
        """
        # Proposal invalidated by challenger win
        if game_status == 1:  # CHALLENGER_WINS
            return FinalityState.PROPOSAL_INVALIDATED_QUARANTINE

        # Must match respected game type
        if proposal_game_type != respected_game_type:
            return FinalityState.L2_STATE_FINALIZED_L1_DA if l1_batch_finalized else FinalityState.L2_STATE_SAFE

        # Dispute in progress
        if game_status == 0:  # IN_PROGRESS
            return FinalityState.DISPUTE_GAME_IN_PROGRESS

        # Defender won, evaluate finality delay starting at resolvedAt
        if game_status == 2:  # DEFENDER_WINS
            if current_l1_timestamp < resolved_at + dispute_game_finality_delay_seconds:
                return FinalityState.DISPUTE_GAME_RESOLVED_DEFENDER  # in finality airgap delay

            # Delay elapsed and L1 DA finalized => caller-asserted unverified output root
            if l1_batch_finalized:
                return FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED
            return FinalityState.DISPUTE_GAME_FINALIZED

        return FinalityState.FINALITY_PENDING

    @staticmethod
    def evaluate_op_stack_withdrawal(
        proven_withdrawal_timestamp: int,
        current_l1_timestamp: int,
        proof_maturity_delay_seconds: int,
    ) -> FinalityState:
        """Evaluates withdrawal proof maturity delay starting at provenWithdrawal.timestamp."""
        if current_l1_timestamp >= proven_withdrawal_timestamp + proof_maturity_delay_seconds:
            return FinalityState.WITHDRAWAL_PROOF_MATURE_FINAL
        return FinalityState.WITHDRAWAL_PENDING_MATURITY

    @staticmethod
    def evaluate_solana(commitment: str, is_rooted: bool) -> FinalityState:
        """Evaluates Solana slot commitment using rooted Tower BFT criteria.
        
        Returns FINALITY_CALLER_ASSERTED_UNVERIFIED for finalized and rooted slots
        in the absence of authenticated cluster voting proofs.
        """
        if commitment == "finalized" and is_rooted:
            return FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED
        return FinalityState.FINALITY_PENDING


# ==============================================================================
# 6. Venue-Specific Execution Evidence & Dual-Track Outcome Resolution
# ==============================================================================

@dataclasses.dataclass
class UniswapV2Evidence:
    """Required execution evidence for Uniswap V2 constant-product pools."""
    chain_id: int
    pool_address: str
    token0_address: str
    token1_address: str
    token0_decimals: int
    token1_decimals: int
    raw_reserve0: int
    raw_reserve1: int
    fee_bps: int
    block_number: int
    block_hash: str
    parent_hash: str

    def is_complete(self) -> bool:
        if not (
            bool(self.chain_id and self.pool_address)
            and bool(self.token0_address and self.token1_address)
            and self.token0_address.lower() != self.token1_address.lower()
            and self.token0_decimals >= 0 and self.token1_decimals >= 0
            and self.raw_reserve0 > 0 and self.raw_reserve1 > 0
            and 0 < self.fee_bps <= 10000  # Valid fee bounds (0 bps to 100%)
            and self.block_number > 0
            and bool(self.block_hash and self.parent_hash)
        ):
            return False
        return True


@dataclasses.dataclass
class UniswapV3Evidence:
    """Required execution evidence for Uniswap V3 concentrated liquidity pools."""
    chain_id: int
    pool_address: str
    token0_address: str
    token1_address: str
    token0_decimals: int
    token1_decimals: int
    fee_tier_ppm: int
    sqrt_price_x96: int
    tick: int
    active_liquidity: int
    initialized_ticks: Dict[int, Dict[str, Any]]  # tick -> {liquidityNet, liquidityGross}
    tick_bitmap: Dict[int, int]                  # word_pos -> 256-bit int
    block_number: int
    block_hash: str
    parent_hash: str

    def is_complete_for_range(self, target_tick: int) -> bool:
        """Validates tick/price relationship, tick spacing, full bitmap coverage, and bidirectional consistency."""
        # 1. Validate fee tier and canonical spacing
        if self.fee_tier_ppm not in UNISWAP_V3_FEE_SPACINGS:
            return False
        spacing = UNISWAP_V3_FEE_SPACINGS[self.fee_tier_ppm]

        # 2. Validate identities, bounds, and non-empty hashes
        if not (
            bool(self.chain_id and self.pool_address)
            and bool(self.token0_address and self.token1_address)
            and self.token0_address.lower() != self.token1_address.lower()
            and self.token0_decimals >= 0 and self.token1_decimals >= 0
            and self.sqrt_price_x96 > 0
            and self.active_liquidity >= 0
            and self.block_number > 0
            and bool(self.block_hash and self.parent_hash)
            and (UniswapV3TickMath.MIN_TICK <= self.tick <= UniswapV3TickMath.MAX_TICK)
            and (UniswapV3TickMath.MIN_TICK <= target_tick <= UniswapV3TickMath.MAX_TICK)
        ):
            return False

        # 3. Validate mathematical tick/price relationship:
        # In Uniswap V3, getTickAtSqrtRatio(sqrtPriceX96) computes the greatest tick t
        # such that getSqrtRatioAtTick(t) <= sqrtPriceX96.
        # At an exact boundary where sqrtPriceX96 == getSqrtRatioAtTick(t), a zero-for-one
        # swap crossing the boundary sets pool tick to step.tickNext - 1 (i.e. t - 1).
        calc_tick = UniswapV3TickMath.get_tick_at_sqrt_ratio(self.sqrt_price_x96)
        exact_boundary_ratio = UniswapV3TickMath.get_sqrt_ratio_at_tick(calc_tick)
        is_exact_boundary = (self.sqrt_price_x96 == exact_boundary_ratio)
        valid_tick = (self.tick == calc_tick) or (is_exact_boundary and self.tick == calc_tick - 1)
        if not valid_tick:
            return False

        t_min = min(self.tick, target_tick)
        t_max = max(self.tick, target_tick)

        compressed_min = t_min // spacing
        compressed_max = t_max // spacing
        word_min = compressed_min >> 8
        word_max = compressed_max >> 8

        # 4. Verify bitmap words exist for every word covering the simulated price path
        for w in range(word_min, word_max + 1):
            if w not in self.tick_bitmap:
                return False

        # 5. Bidirectional Consistency:
        # Direction A: Every tick in initialized_ticks within [t_min, t_max] MUST have its bit set in tick_bitmap
        for t_init, data in self.initialized_ticks.items():
            if t_init % spacing != 0:
                return False
            if data.get("liquidityGross", 0) <= 0:
                return False

            if t_min <= t_init <= t_max:
                comp_idx = t_init // spacing
                w_pos = comp_idx >> 8
                bit_pos = comp_idx % 256
                if w_pos not in self.tick_bitmap:
                    return False
                if (self.tick_bitmap[w_pos] & (1 << bit_pos)) == 0:
                    return False  # Initialized tick missing from bitmap!

        # Direction B: Every bit set in bitmap within [t_min, t_max] MUST be present in initialized_ticks
        for w in range(word_min, word_max + 1):
            bitmap_word = self.tick_bitmap[w]
            if bitmap_word != 0:
                for bit_pos in range(256):
                    if (bitmap_word & (1 << bit_pos)) != 0:
                        comp_idx = (w << 8) + bit_pos
                        actual_tick = comp_idx * spacing
                        if t_min <= actual_tick <= t_max:
                            if actual_tick not in self.initialized_ticks:
                                return False  # Bit set in bitmap but tick missing from initialized_ticks!

        return True

    def is_complete(self) -> bool:
        """Validates general completeness at current pool tick."""
        return self.is_complete_for_range(self.tick)


class UniswapV3SwapMath:
    """Exact integer math library replicating Uniswap V3 SqrtPriceMath.sol and SwapMath.sol."""

    @staticmethod
    def mul_div_rounding_up(a: int, b: int, denominator: int) -> int:
        """Exact FullMath.mulDivRoundingUp integer operation."""
        if denominator <= 0:
            raise ValueError("Denominator must be positive")
        prod = a * b
        quotient = prod // denominator
        if prod % denominator > 0:
            quotient += 1
        return quotient

    @staticmethod
    def get_amount0_delta_round_down(sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, liquidity: int) -> int:
        """Exact integer implementation of SqrtPriceMath.getAmount0Delta(..., roundUp=false).
        Does NOT subtract two independently rounded reciprocal terms.
        Canonical operation order: (numerator1 * numerator2 // sqrt_ratio_b_x96) // sqrt_ratio_a_x96.
        """
        if sqrt_ratio_a_x96 > sqrt_ratio_b_x96:
            sqrt_ratio_a_x96, sqrt_ratio_b_x96 = sqrt_ratio_b_x96, sqrt_ratio_a_x96
        if sqrt_ratio_a_x96 <= 0:
            return 0
        numerator1 = liquidity << 96
        numerator2 = sqrt_ratio_b_x96 - sqrt_ratio_a_x96
        intermediate = (numerator1 * numerator2) // sqrt_ratio_b_x96
        return intermediate // sqrt_ratio_a_x96

    @staticmethod
    def get_amount0_delta_round_up(sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, liquidity: int) -> int:
        """Exact integer implementation of SqrtPriceMath.getAmount0Delta(..., roundUp=true)."""
        if sqrt_ratio_a_x96 > sqrt_ratio_b_x96:
            sqrt_ratio_a_x96, sqrt_ratio_b_x96 = sqrt_ratio_b_x96, sqrt_ratio_a_x96
        if sqrt_ratio_a_x96 <= 0:
            return 0
        numerator1 = liquidity << 96
        numerator2 = sqrt_ratio_b_x96 - sqrt_ratio_a_x96
        div1 = (numerator1 * numerator2 + sqrt_ratio_b_x96 - 1) // sqrt_ratio_b_x96
        return (div1 + sqrt_ratio_a_x96 - 1) // sqrt_ratio_a_x96

    @staticmethod
    def get_amount1_delta_round_down(sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, liquidity: int) -> int:
        """Exact integer implementation of SqrtPriceMath.getAmount1Delta(..., roundUp=false)."""
        if sqrt_ratio_a_x96 > sqrt_ratio_b_x96:
            sqrt_ratio_a_x96, sqrt_ratio_b_x96 = sqrt_ratio_b_x96, sqrt_ratio_a_x96
        return (liquidity * (sqrt_ratio_b_x96 - sqrt_ratio_a_x96)) >> 96

    @staticmethod
    def get_amount1_delta_round_up(sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, liquidity: int) -> int:
        """Exact integer implementation of SqrtPriceMath.getAmount1Delta(..., roundUp=true)."""
        if sqrt_ratio_a_x96 > sqrt_ratio_b_x96:
            sqrt_ratio_a_x96, sqrt_ratio_b_x96 = sqrt_ratio_b_x96, sqrt_ratio_a_x96
        diff = sqrt_ratio_b_x96 - sqrt_ratio_a_x96
        return ((liquidity * diff) + (1 << 96) - 1) >> 96

    @classmethod
    def get_next_sqrt_price_from_input(
        cls, sqrt_p_x96: int, liquidity: int, amount_in: int, zero_for_one: bool
    ) -> int:
        """Calculates next sqrt price from input amount."""
        if amount_in <= 0:
            return sqrt_p_x96
        numerator1 = liquidity << 96
        if zero_for_one:
            # token0 in -> price moves down
            product = amount_in * sqrt_p_x96
            denominator = numerator1 + product
            return cls.mul_div_rounding_up(numerator1, sqrt_p_x96, denominator)
        else:
            # token1 in -> price moves up
            quotient = (amount_in << 96) // liquidity
            return sqrt_p_x96 + quotient

    @classmethod
    def compute_swap_step(
        cls,
        sqrt_ratio_current_x96: int,
        sqrt_ratio_target_x96: int,
        liquidity: int,
        amount_remaining: int,
        fee_ppm: int,
    ) -> Tuple[int, int, int, int]:
        """Computes single swap step matching SwapMath.sol line by line.
        Returns (sqrt_ratio_next_x96, amount_in, amount_out, fee_amount).
        """
        zero_for_one = sqrt_ratio_current_x96 >= sqrt_ratio_target_x96
        exact_in = amount_remaining >= 0

        if not exact_in:
            raise NotImplementedError("Exact-out swap step not implemented for research depth simulation")

        amount_remaining_less_fee = (amount_remaining * (1_000_000 - fee_ppm)) // 1_000_000
        amount_in = (
            cls.get_amount0_delta_round_up(sqrt_ratio_target_x96, sqrt_ratio_current_x96, liquidity)
            if zero_for_one
            else cls.get_amount1_delta_round_up(sqrt_ratio_current_x96, sqrt_ratio_target_x96, liquidity)
        )

        if amount_remaining_less_fee >= amount_in:
            sqrt_ratio_next_x96 = sqrt_ratio_target_x96
        else:
            sqrt_ratio_next_x96 = cls.get_next_sqrt_price_from_input(
                sqrt_ratio_current_x96,
                liquidity,
                amount_remaining_less_fee,
                zero_for_one,
            )

        max_reached = (sqrt_ratio_target_x96 == sqrt_ratio_next_x96)

        if zero_for_one:
            amount_in = (
                amount_in
                if max_reached
                else cls.get_amount0_delta_round_up(sqrt_ratio_next_x96, sqrt_ratio_current_x96, liquidity)
            )
            amount_out = cls.get_amount1_delta_round_down(sqrt_ratio_next_x96, sqrt_ratio_current_x96, liquidity)
        else:
            amount_in = (
                amount_in
                if max_reached
                else cls.get_amount1_delta_round_up(sqrt_ratio_current_x96, sqrt_ratio_next_x96, liquidity)
            )
            amount_out = cls.get_amount0_delta_round_down(sqrt_ratio_current_x96, sqrt_ratio_next_x96, liquidity)

        if exact_in and not max_reached:
            fee_amount = amount_remaining - amount_in
        else:
            # Boundary-crossing fee rounding up (SwapMath.sol line 57)
            fee_amount = cls.mul_div_rounding_up(amount_in, fee_ppm, 1_000_000 - fee_ppm)

        return sqrt_ratio_next_x96, amount_in, amount_out, fee_amount


class UniswapV3SwapSimulator:
    """Simulates Uniswap V3 concentrated liquidity swaps walking initialized ticks."""

    @classmethod
    def simulate_swap(
        cls,
        evidence: UniswapV3Evidence,
        amount_in: int,
        zero_for_one: bool,
        target_sqrt_price_x96: Optional[int] = None,
    ) -> Tuple[bool, int, int, int, str]:
        """Simulates full tick-by-tick swap across pool initialized ticks.
        Returns (success, amount_in_consumed, amount_out_yielded, final_sqrt_price_x96, diagnostic).
        """
        if amount_in <= 0:
            return False, 0, 0, evidence.sqrt_price_x96, "NON_POSITIVE_AMOUNT_IN"
        if evidence.fee_tier_ppm not in UNISWAP_V3_FEE_SPACINGS:
            return False, 0, 0, evidence.sqrt_price_x96, "UNSUPPORTED_FEE_TIER"

        spacing = UNISWAP_V3_FEE_SPACINGS[evidence.fee_tier_ppm]
        current_tick = evidence.tick
        current_sqrt_price = evidence.sqrt_price_x96
        current_liquidity = evidence.active_liquidity

        has_explicit_target = target_sqrt_price_x96 is not None
        if target_sqrt_price_x96 is None:
            target_sqrt_price_x96 = (
                UniswapV3TickMath.MIN_SQRT_RATIO + 1
                if zero_for_one
                else UniswapV3TickMath.MAX_SQRT_RATIO - 1
            )

        amount_remaining = amount_in
        amount_out_total = 0

        # Sort initialized ticks in walking direction
        sorted_ticks = sorted(evidence.initialized_ticks.keys())
        if zero_for_one:
            boundary_ticks = [t for t in sorted_ticks if t <= current_tick]
            boundary_ticks.reverse()
        else:
            boundary_ticks = [t for t in sorted_ticks if t > current_tick]

        tick_idx = 0
        while amount_remaining > 0 and current_sqrt_price != target_sqrt_price_x96:
            if current_liquidity <= 0:
                if tick_idx >= len(boundary_ticks):
                    return False, amount_in - amount_remaining, amount_out_total, current_sqrt_price, "ZERO_LIQUIDITY_DEPTH_EXHAUSTED"

            if tick_idx < len(boundary_ticks):
                next_tick = boundary_ticks[tick_idx]
                next_sqrt_p = UniswapV3TickMath.get_sqrt_ratio_at_tick(next_tick)
                if zero_for_one:
                    step_target = max(next_sqrt_p, target_sqrt_price_x96)
                else:
                    step_target = min(next_sqrt_p, target_sqrt_price_x96)
            else:
                next_tick = None
                step_target = target_sqrt_price_x96

            # Check bitmap coverage for the path to step_target
            comp_idx = (next_tick // spacing) if next_tick is not None else (current_tick // spacing)
            word_pos = comp_idx >> 8
            if word_pos not in evidence.tick_bitmap:
                return False, amount_in - amount_remaining, amount_out_total, current_sqrt_price, "MODELED_DEPTH_UNRESOLVED_BITMAP_GAP"

            next_sqrt_price, step_in, step_out, fee_step = UniswapV3SwapMath.compute_swap_step(
                current_sqrt_price,
                step_target,
                current_liquidity,
                amount_remaining,
                evidence.fee_tier_ppm,
            )

            amount_remaining -= (step_in + fee_step)
            amount_out_total += step_out
            current_sqrt_price = next_sqrt_price

            # If boundary tick reached, cross tick and adjust liquidity
            if current_sqrt_price == step_target and next_tick is not None and next_sqrt_price == UniswapV3TickMath.get_sqrt_ratio_at_tick(next_tick):
                tick_data = evidence.initialized_ticks.get(next_tick)
                if not tick_data:
                    return False, amount_in - amount_remaining, amount_out_total, current_sqrt_price, "MODELED_DEPTH_UNRESOLVED_UNINITIALIZED_TICK"
                liquidity_net = tick_data.get("liquidityNet", 0)
                if zero_for_one:
                    current_liquidity -= liquidity_net
                    current_tick = next_tick - 1
                else:
                    current_liquidity += liquidity_net
                    current_tick = next_tick
                tick_idx += 1
            elif current_sqrt_price == target_sqrt_price_x96:
                break

        consumed = amount_in - amount_remaining
        if has_explicit_target and current_sqrt_price != target_sqrt_price_x96:
            return False, consumed, amount_out_total, current_sqrt_price, "TARGET_PRICE_NOT_REACHED_INSUFFICIENT_INPUT_OR_DEPTH"
        return True, consumed, amount_out_total, current_sqrt_price, "SWAP_SUCCESS"

    @classmethod
    def compute_bidirectional_depth(
        cls,
        evidence: UniswapV3Evidence,
        token0_quote: bool = True,
    ) -> Dict[str, Any]:
        """Calculates bidirectional depth within +/- 2.0% price impact band."""
        p0_sqrt = evidence.sqrt_price_x96
        sqrt_mult_plus_2pct = int(math.sqrt(1.02) * (1 << 96))
        sqrt_mult_minus_2pct = int(math.sqrt(0.98) * (1 << 96))

        p_target_plus = (p0_sqrt * sqrt_mult_plus_2pct) >> 96
        p_target_minus = (p0_sqrt * sqrt_mult_minus_2pct) >> 96

        if token0_quote:
            ok_ask, q_ask, _, _, _ = cls.simulate_swap(evidence, amount_in=10**24, zero_for_one=True, target_sqrt_price_x96=p_target_minus)
            ok_bid, _, q_bid, _, _ = cls.simulate_swap(evidence, amount_in=10**24, zero_for_one=False, target_sqrt_price_x96=p_target_plus)
        else:
            ok_ask, q_ask, _, _, _ = cls.simulate_swap(evidence, amount_in=10**24, zero_for_one=False, target_sqrt_price_x96=p_target_plus)
            ok_bid, _, q_bid, _, _ = cls.simulate_swap(evidence, amount_in=10**24, zero_for_one=True, target_sqrt_price_x96=p_target_minus)

        return {
            "d_ask_plus_2pct": q_ask if ok_ask else 0,
            "d_bid_minus_2pct": q_bid if ok_bid else 0,
            "is_depth_complete": bool(ok_ask and ok_bid),
        }


class UniswapV2SwapSimulator:
    """Exact integer constant-product swap simulator and bidirectional depth engine."""

    @staticmethod
    def simulate_swap(
        reserve_in: int,
        reserve_out: int,
        amount_in: int,
        fee_bps: int = 30,
    ) -> Tuple[bool, int, float, float, int, int]:
        """Calculates Uniswap V2 integer getAmountOut.
        Returns (success, amount_out, effective_price, price_impact_pct, new_reserve_in, new_reserve_out).
        """
        if reserve_in <= 0 or reserve_out <= 0 or amount_in <= 0:
            return False, 0, 0.0, 0.0, reserve_in, reserve_out

        amount_in_with_fee = amount_in * (10000 - fee_bps)
        numerator = amount_in_with_fee * reserve_out
        denominator = (reserve_in * 10000) + amount_in_with_fee
        amount_out = numerator // denominator

        if amount_out >= reserve_out or amount_out <= 0:
            return False, 0, 0.0, 0.0, reserve_in, reserve_out

        new_reserve_in = reserve_in + amount_in
        new_reserve_out = reserve_out - amount_out

        spot_price_initial = reserve_out / reserve_in
        effective_price = amount_out / amount_in
        price_impact_pct = max(0.0, (1.0 - (effective_price / spot_price_initial)) * 100.0)

        return True, amount_out, effective_price, price_impact_pct, new_reserve_in, new_reserve_out

    @classmethod
    def compute_bidirectional_depth(
        cls,
        reserve_quote: int,
        reserve_token: int,
        fee_bps: int = 30,
    ) -> Dict[str, Any]:
        """Calculates exact integer bidirectional depth within +/- 2.0% price impact band
        using canonical Uniswap V2 swap accounting and exact integer spot-price boundary search.

        Uniswap V2 adds the full gross input to reserveIn, using fee-adjusted input solely
        to compute amountOut:
          new_reserve_in = reserve_in + amount_in
          new_reserve_out = reserve_out - amount_out

        Ask Depth (+2% spot price boundary):
          Searches for the smallest integer quote_in such that:
          100 * (reserve_quote + quote_in) * reserve_token >= 102 * reserve_quote * (reserve_token - token_out).
          D_ask = quote_in.

        Bid Depth (-2% spot price boundary):
          Searches for the smallest integer token_in such that:
          100 * (reserve_quote - quote_out) * reserve_token <= 98 * reserve_quote * (reserve_token + token_in).
          token_in_minus_2pct = token_in.
          D_bid = quote_out (actual quote proceeds yielded by the swap).
        """
        if reserve_quote <= 0 or reserve_token <= 0:
            return {"d_ask_plus_2pct": 0, "d_bid_minus_2pct": 0, "token_in_minus_2pct": 0}

        # 1. Ask Depth (+2% terminal spot price): search for smallest quote_in reaching boundary
        high = max(1, (reserve_quote * 3) // 100)
        while True:
            ok, out, _, _, n_rq, n_rb = cls.simulate_swap(
                reserve_in=reserve_quote,
                reserve_out=reserve_token,
                amount_in=high,
                fee_bps=fee_bps,
            )
            if not ok or high >= reserve_quote:
                high = max(high, reserve_quote)
                break
            if 100 * n_rq * reserve_token >= 102 * reserve_quote * n_rb:
                break
            high *= 2

        low = 1
        while low < high:
            mid = (low + high) // 2
            ok, out, _, _, n_rq, n_rb = cls.simulate_swap(
                reserve_in=reserve_quote,
                reserve_out=reserve_token,
                amount_in=mid,
                fee_bps=fee_bps,
            )
            if ok and (100 * n_rq * reserve_token >= 102 * reserve_quote * n_rb):
                high = mid
            else:
                low = mid + 1
        d_ask = low

        # 2. Bid Depth (-2% terminal spot price): search for smallest token_in reaching boundary
        high = max(1, (reserve_token * 3) // 100)
        while True:
            ok, out, _, _, n_rb, n_rq = cls.simulate_swap(
                reserve_in=reserve_token,
                reserve_out=reserve_quote,
                amount_in=high,
                fee_bps=fee_bps,
            )
            if not ok or high >= reserve_token:
                high = max(high, reserve_token)
                break
            if 100 * n_rq * reserve_token <= 98 * reserve_quote * n_rb:
                break
            high *= 2

        low = 1
        while low < high:
            mid = (low + high) // 2
            ok, out, _, _, n_rb, n_rq = cls.simulate_swap(
                reserve_in=reserve_token,
                reserve_out=reserve_quote,
                amount_in=mid,
                fee_bps=fee_bps,
            )
            if ok and (100 * n_rq * reserve_token <= 98 * reserve_quote * n_rb):
                high = mid
            else:
                low = mid + 1
        token_in = low
        ok_bid, d_bid, _, _, _, _ = cls.simulate_swap(
            reserve_in=reserve_token,
            reserve_out=reserve_quote,
            amount_in=token_in,
            fee_bps=fee_bps,
        )

        return {
            "d_ask_plus_2pct": d_ask,
            "d_bid_minus_2pct": d_bid,
            "token_in_minus_2pct": token_in,
        }


def calculate_evm_gas_cost_usd(
    gas_used: int,
    base_fee_wei: int,
    priority_fee_wei: int,
    native_usd_price: float,
    max_fee_per_gas_wei: Optional[int] = None,
) -> Tuple[float, float, str]:
    """Calculates EVM EIP-1559 gas cost in ETH and USD using canonical effective gas price formula:
    effective_gas_price = base_fee + min(priority_fee, max_fee - base_fee).
    Under canonical EIP-1559:
      - max_fee_per_gas_wei must be >= base_fee_wei. If max_fee < base_fee, transaction cannot execute (fails closed).
      - Returns status 'MODELED_GAS_COST_CANONICAL_EIP1559'.
    If max_fee_per_gas_wei is None:
      - Uncapped assumption is explicitly labeled 'MODELED_GAS_COST_UNCAPPED_LEGACY_ASSUMPTION' (not canonical EIP-1559).
    """
    if base_fee_wei < 0 or priority_fee_wei < 0 or gas_used < 0:
        raise ValueError("Gas parameters must be non-negative")

    if max_fee_per_gas_wei is not None:
        if max_fee_per_gas_wei < base_fee_wei:
            return 0.0, 0.0, "MODELED_GAS_FAIL_MAX_FEE_BELOW_BASE_FEE"
        effective_tip = min(priority_fee_wei, max_fee_per_gas_wei - base_fee_wei)
        status_label = "MODELED_GAS_COST_CANONICAL_EIP1559"
    else:
        effective_tip = priority_fee_wei
        status_label = "MODELED_GAS_COST_UNCAPPED_LEGACY_ASSUMPTION"

    effective_gas_price_wei = base_fee_wei + effective_tip
    gas_cost_wei = gas_used * effective_gas_price_wei
    gas_cost_eth = gas_cost_wei / 1e18
    gas_cost_usd = gas_cost_eth * native_usd_price
    return gas_cost_eth, gas_cost_usd, status_label


def calculate_solana_tx_fee_lamports(
    base_signatures: int,
    compute_unit_limit: int,
    compute_unit_price_micro_lamports: int,
) -> int:
    """Calculates Solana transaction fee using ComputeUnitLimit and ceiling rounding."""
    base_fee = base_signatures * 5000
    micro_lamports = compute_unit_limit * compute_unit_price_micro_lamports
    # Strict ceiling division to round up fractional lamports
    priority_fee = (micro_lamports + 999_999) // 1_000_000
    return base_fee + priority_fee


def calculate_solana_tx_fee_usd(
    base_signatures: int,
    compute_unit_limit: int,
    compute_unit_price_micro_lamports: int,
    sol_usd_price: float,
) -> Tuple[float, float, str]:
    """Calculates Solana transaction fee in SOL and USD with ceiling rounding.
    Returns (fee_sol, fee_usd, 'MODELED_GAS_COST_WITH_ASSUMPTIONS').
    """
    fee_lamports = calculate_solana_tx_fee_lamports(
        base_signatures, compute_unit_limit, compute_unit_price_micro_lamports
    )
    fee_sol = fee_lamports / 1e9
    fee_usd = fee_sol * sol_usd_price
    return fee_sol, fee_usd, "MODELED_GAS_COST_WITH_ASSUMPTIONS"


@dataclasses.dataclass
class PoolStateSnapshot:
    """Pool state snapshot bound to canonical identity and block provenance."""
    chain_id: int
    token_address: str
    pool_address: str
    block_number: int
    block_hash: str
    source_timestamp_utc: str
    venue_type: str  # "UNISWAP_V2" or "UNISWAP_V3"
    evidence_v2: Optional[UniswapV2Evidence] = None
    evidence_v3: Optional[UniswapV3Evidence] = None
    quote_usd_price: float = 1.0
    native_usd_price: float = 3000.0
    base_fee_per_gas_wei: int = 20_000_000_000  # 20 gwei
    priority_fee_per_gas_wei: int = 2_000_000_000  # 2 gwei
    max_fee_per_gas_wei: Optional[int] = None
    token0_is_quote: bool = True


class TwoLegExecutionSimulator:
    """Simulates two-leg roundtrip execution (Buy at entry, Sell at touch) across capital tranches."""

    @classmethod
    def simulate_roundtrip(
        cls,
        entry_state: Optional[PoolStateSnapshot],
        touch_state: Optional[PoolStateSnapshot],
        tranche_usd: float,
        gas_used_buy: int = 150_000,
        gas_used_sell: int = 150_000,
    ) -> Dict[str, Any]:
        """Executes modeled buy against entry pool state and modeled sell against target touch pool state."""
        # 1. State availability check
        if entry_state is None or touch_state is None:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "ENTRY_OR_TOUCH_POOL_STATE_MISSING",
                "tranche_usd": tranche_usd,
            }

        # 2. Strict provenance & identity binding validation
        canon_entry_token = canonicalize_token_address(entry_state.token_address)
        canon_touch_token = canonicalize_token_address(touch_state.token_address)
        canon_entry_pool = canonicalize_token_address(entry_state.pool_address)
        canon_touch_pool = canonicalize_token_address(touch_state.pool_address)

        if entry_state.chain_id != touch_state.chain_id:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": f"CHAIN_ID_MISMATCH_ENTRY_{entry_state.chain_id}_TOUCH_{touch_state.chain_id}",
                "tranche_usd": tranche_usd,
            }

        if canon_entry_token != canon_touch_token:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": f"TOKEN_MISMATCH_ENTRY_{canon_entry_token}_TOUCH_{canon_touch_token}",
                "tranche_usd": tranche_usd,
            }

        if canon_entry_pool != canon_touch_pool:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": f"POOL_MISMATCH_ENTRY_{canon_entry_pool}_TOUCH_{canon_touch_pool}",
                "tranche_usd": tranche_usd,
            }

        if touch_state.block_number < entry_state.block_number:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": f"BLOCK_ORDER_INVALID_TOUCH_{touch_state.block_number}_LT_ENTRY_{entry_state.block_number}",
                "tranche_usd": tranche_usd,
            }

        # Validate embedded evidence matches snapshot provenance
        if entry_state.venue_type == "UNISWAP_V2":
            ev = entry_state.evidence_v2
            if not ev or not ev.is_complete():
                return {"status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value, "net_return_pct": None, "diagnostic": "V2_ENTRY_EVIDENCE_INCOMPLETE", "tranche_usd": tranche_usd}
            if ev.chain_id != entry_state.chain_id or canonicalize_token_address(ev.pool_address) != canon_entry_pool:
                return {"status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value, "net_return_pct": None, "diagnostic": "ENTRY_EVIDENCE_POOL_IDENTITY_MISMATCH", "tranche_usd": tranche_usd}
            if ev.block_number != entry_state.block_number or ev.block_hash.lower() != entry_state.block_hash.lower():
                return {"status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value, "net_return_pct": None, "diagnostic": "ENTRY_EVIDENCE_BLOCK_PROVENANCE_MISMATCH", "tranche_usd": tranche_usd}
            ev_tokens = [canonicalize_token_address(ev.token0_address), canonicalize_token_address(ev.token1_address)]
            if canon_entry_token not in ev_tokens:
                return {"status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value, "net_return_pct": None, "diagnostic": "ENTRY_EVIDENCE_TOKEN_NOT_IN_PAIR", "tranche_usd": tranche_usd}
        elif entry_state.venue_type == "UNISWAP_V3":
            ev3 = entry_state.evidence_v3
            if not ev3 or not ev3.is_complete():
                return {"status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value, "net_return_pct": None, "diagnostic": "V3_ENTRY_EVIDENCE_INCOMPLETE", "tranche_usd": tranche_usd}
            if ev3.chain_id != entry_state.chain_id or canonicalize_token_address(ev3.pool_address) != canon_entry_pool:
                return {"status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value, "net_return_pct": None, "diagnostic": "ENTRY_V3_EVIDENCE_POOL_IDENTITY_MISMATCH", "tranche_usd": tranche_usd}
            if ev3.block_number != entry_state.block_number or ev3.block_hash.lower() != entry_state.block_hash.lower():
                return {"status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value, "net_return_pct": None, "diagnostic": "ENTRY_V3_EVIDENCE_BLOCK_PROVENANCE_MISMATCH", "tranche_usd": tranche_usd}
            ev3_tokens = [canonicalize_token_address(ev3.token0_address), canonicalize_token_address(ev3.token1_address)]
            if canon_entry_token not in ev3_tokens:
                return {"status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value, "net_return_pct": None, "diagnostic": "ENTRY_V3_EVIDENCE_TOKEN_NOT_IN_POOL", "tranche_usd": tranche_usd}

        if touch_state.venue_type == "UNISWAP_V2":
            ev_touch = touch_state.evidence_v2
            if not ev_touch or not ev_touch.is_complete():
                return {"status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value, "net_return_pct": None, "diagnostic": "V2_TOUCH_EVIDENCE_INCOMPLETE", "tranche_usd": tranche_usd}
            if ev_touch.chain_id != touch_state.chain_id or canonicalize_token_address(ev_touch.pool_address) != canon_touch_pool:
                return {"status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value, "net_return_pct": None, "diagnostic": "TOUCH_EVIDENCE_POOL_IDENTITY_MISMATCH", "tranche_usd": tranche_usd}
            if ev_touch.block_number != touch_state.block_number or ev_touch.block_hash.lower() != touch_state.block_hash.lower():
                return {"status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value, "net_return_pct": None, "diagnostic": "TOUCH_EVIDENCE_BLOCK_PROVENANCE_MISMATCH", "tranche_usd": tranche_usd}
            ev_touch_tokens = [canonicalize_token_address(ev_touch.token0_address), canonicalize_token_address(ev_touch.token1_address)]
            if canon_touch_token not in ev_touch_tokens:
                return {"status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value, "net_return_pct": None, "diagnostic": "TOUCH_EVIDENCE_TOKEN_NOT_IN_PAIR", "tranche_usd": tranche_usd}
        elif touch_state.venue_type == "UNISWAP_V3":
            ev3_touch = touch_state.evidence_v3
            if not ev3_touch or not ev3_touch.is_complete():
                return {"status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value, "net_return_pct": None, "diagnostic": "V3_TOUCH_EVIDENCE_INCOMPLETE", "tranche_usd": tranche_usd}
            if ev3_touch.chain_id != touch_state.chain_id or canonicalize_token_address(ev3_touch.pool_address) != canon_touch_pool:
                return {"status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value, "net_return_pct": None, "diagnostic": "TOUCH_V3_EVIDENCE_POOL_IDENTITY_MISMATCH", "tranche_usd": tranche_usd}
            if ev3_touch.block_number != touch_state.block_number or ev3_touch.block_hash.lower() != touch_state.block_hash.lower():
                return {"status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value, "net_return_pct": None, "diagnostic": "TOUCH_V3_EVIDENCE_BLOCK_PROVENANCE_MISMATCH", "tranche_usd": tranche_usd}
            ev3_touch_tokens = [canonicalize_token_address(ev3_touch.token0_address), canonicalize_token_address(ev3_touch.token1_address)]
            if canon_touch_token not in ev3_touch_tokens:
                return {"status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value, "net_return_pct": None, "diagnostic": "TOUCH_V3_EVIDENCE_TOKEN_NOT_IN_POOL", "tranche_usd": tranche_usd}

        # 3. Gas calculation
        _, gas_buy_usd, buy_gas_status = calculate_evm_gas_cost_usd(
            gas_used=gas_used_buy,
            base_fee_wei=entry_state.base_fee_per_gas_wei,
            priority_fee_wei=entry_state.priority_fee_per_gas_wei,
            native_usd_price=entry_state.native_usd_price,
            max_fee_per_gas_wei=entry_state.max_fee_per_gas_wei,
        )
        if buy_gas_status == "MODELED_GAS_FAIL_MAX_FEE_BELOW_BASE_FEE":
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "ENTRY_MAX_FEE_BELOW_BASE_FEE",
                "gas_buy_model_label": buy_gas_status,
                "gas_sell_model_label": None,
                "tranche_usd": tranche_usd,
            }

        _, gas_sell_usd, sell_gas_status = calculate_evm_gas_cost_usd(
            gas_used=gas_used_sell,
            base_fee_wei=touch_state.base_fee_per_gas_wei,
            priority_fee_wei=touch_state.priority_fee_per_gas_wei,
            native_usd_price=touch_state.native_usd_price,
            max_fee_per_gas_wei=touch_state.max_fee_per_gas_wei,
        )
        if sell_gas_status == "MODELED_GAS_FAIL_MAX_FEE_BELOW_BASE_FEE":
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "TOUCH_MAX_FEE_BELOW_BASE_FEE",
                "gas_buy_model_label": buy_gas_status,
                "gas_sell_model_label": sell_gas_status,
                "tranche_usd": tranche_usd,
            }

        # 4. Leg 1: Entry Buy
        q_in = tranche_usd / entry_state.quote_usd_price
        if entry_state.venue_type == "UNISWAP_V2":
            ev = entry_state.evidence_v2
            if not ev or not ev.is_complete():
                return {
                    "status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value,
                    "net_return_pct": None,
                    "diagnostic": "V2_ENTRY_EVIDENCE_INCOMPLETE",
                    "gas_buy_model_label": buy_gas_status,
                    "gas_sell_model_label": sell_gas_status,
                }

            canon_target = canonicalize_token_address(entry_state.token_address)
            canon_t0 = canonicalize_token_address(ev.token0_address)
            canon_t1 = canonicalize_token_address(ev.token1_address)

            if canon_target == canon_t0:
                # Tracked asset is token0, quote is token1
                q_in_raw = int(q_in * (10 ** ev.token1_decimals))
                ok_buy, tokens_acquired_raw, p_buy, impact_buy, _, _ = UniswapV2SwapSimulator.simulate_swap(
                    reserve_in=ev.raw_reserve1, reserve_out=ev.raw_reserve0, amount_in=q_in_raw, fee_bps=ev.fee_bps
                )
            elif canon_target == canon_t1:
                # Tracked asset is token1 (reversed), quote is token0
                q_in_raw = int(q_in * (10 ** ev.token0_decimals))
                ok_buy, tokens_acquired_raw, p_buy, impact_buy, _, _ = UniswapV2SwapSimulator.simulate_swap(
                    reserve_in=ev.raw_reserve0, reserve_out=ev.raw_reserve1, amount_in=q_in_raw, fee_bps=ev.fee_bps
                )
            else:
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": "V2_TARGET_TOKEN_NOT_IN_PAIR",
                    "gas_buy_model_label": buy_gas_status,
                    "gas_sell_model_label": sell_gas_status,
                }

            if not ok_buy:
                return {
                    "status": DualTrackTargetStatus.MODELED_TP40_LIQUIDITY_INSUFFICIENT.value,
                    "net_return_pct": None,
                    "diagnostic": "LEG1_BUY_FAILED_LIQUIDITY",
                    "gas_buy_model_label": buy_gas_status,
                    "gas_sell_model_label": sell_gas_status,
                }
        elif entry_state.venue_type == "UNISWAP_V3":
            ev3 = entry_state.evidence_v3
            if not ev3 or not ev3.is_complete():
                return {
                    "status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value,
                    "net_return_pct": None,
                    "diagnostic": "V3_ENTRY_EVIDENCE_INCOMPLETE",
                    "gas_buy_model_label": buy_gas_status,
                    "gas_sell_model_label": sell_gas_status,
                }

            canon_target = canonicalize_token_address(entry_state.token_address)
            canon_t0 = canonicalize_token_address(ev3.token0_address)
            canon_t1 = canonicalize_token_address(ev3.token1_address)

            if canon_target == canon_t0:
                if entry_state.token0_is_quote:
                    return {
                        "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                        "net_return_pct": None,
                        "diagnostic": "V3_ORIENTATION_CONFLICT_TOKEN0_BASE_BUT_FLAG_TRUE",
                        "gas_buy_model_label": buy_gas_status,
                        "gas_sell_model_label": sell_gas_status,
                    }
                z4o_buy = False
                dec_in = ev3.token1_decimals
            elif canon_target == canon_t1:
                if not entry_state.token0_is_quote:
                    return {
                        "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                        "net_return_pct": None,
                        "diagnostic": "V3_ORIENTATION_CONFLICT_TOKEN1_BASE_BUT_FLAG_FALSE",
                        "gas_buy_model_label": buy_gas_status,
                        "gas_sell_model_label": sell_gas_status,
                    }
                z4o_buy = True
                dec_in = ev3.token0_decimals
            else:
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": "V3_ENTRY_TOKEN_NOT_IN_POOL",
                    "gas_buy_model_label": buy_gas_status,
                    "gas_sell_model_label": sell_gas_status,
                }

            q_in_raw = int(q_in * (10 ** dec_in))
            ok_buy, consumed, tokens_acquired_raw, _, diag = UniswapV3SwapSimulator.simulate_swap(
                ev3, amount_in=q_in_raw, zero_for_one=z4o_buy
            )
            if not ok_buy:
                return {
                    "status": DualTrackTargetStatus.MODELED_TP40_LIQUIDITY_INSUFFICIENT.value,
                    "net_return_pct": None,
                    "diagnostic": f"LEG1_BUY_FAILED_LIQUIDITY_{diag}",
                    "gas_buy_model_label": buy_gas_status,
                    "gas_sell_model_label": sell_gas_status,
                }
        else:
            return {
                "status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value,
                "net_return_pct": None,
                "diagnostic": f"UNSUPPORTED_VENUE_{entry_state.venue_type}",
                "gas_buy_model_label": buy_gas_status,
                "gas_sell_model_label": sell_gas_status,
            }

        # 5. Leg 2: Target Sell (selling exact tokens acquired)
        if touch_state.venue_type == "UNISWAP_V2":
            ev_touch = touch_state.evidence_v2
            if not ev_touch or not ev_touch.is_complete():
                return {
                    "status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value,
                    "net_return_pct": None,
                    "diagnostic": "V2_TOUCH_EVIDENCE_INCOMPLETE",
                    "gas_buy_model_label": buy_gas_status,
                    "gas_sell_model_label": sell_gas_status,
                }

            canon_target = canonicalize_token_address(touch_state.token_address)
            canon_t0 = canonicalize_token_address(ev_touch.token0_address)
            canon_t1 = canonicalize_token_address(ev_touch.token1_address)

            if canon_target == canon_t0:
                # Selling token0 for quote token1
                ok_sell, q_out_raw, p_sell, impact_sell, _, _ = UniswapV2SwapSimulator.simulate_swap(
                    reserve_in=ev_touch.raw_reserve0, reserve_out=ev_touch.raw_reserve1, amount_in=tokens_acquired_raw, fee_bps=ev_touch.fee_bps
                )
                if not ok_sell:
                    return {
                        "status": DualTrackTargetStatus.MODELED_TP40_LIQUIDITY_INSUFFICIENT.value,
                        "net_return_pct": None,
                        "diagnostic": "LEG2_SELL_FAILED_LIQUIDITY",
                        "gas_buy_model_label": buy_gas_status,
                        "gas_sell_model_label": sell_gas_status,
                    }
                gross_proceeds_usd = (q_out_raw / (10 ** ev_touch.token1_decimals)) * touch_state.quote_usd_price
            elif canon_target == canon_t1:
                # Selling token1 for quote token0
                ok_sell, q_out_raw, p_sell, impact_sell, _, _ = UniswapV2SwapSimulator.simulate_swap(
                    reserve_in=ev_touch.raw_reserve1, reserve_out=ev_touch.raw_reserve0, amount_in=tokens_acquired_raw, fee_bps=ev_touch.fee_bps
                )
                if not ok_sell:
                    return {
                        "status": DualTrackTargetStatus.MODELED_TP40_LIQUIDITY_INSUFFICIENT.value,
                        "net_return_pct": None,
                        "diagnostic": "LEG2_SELL_FAILED_LIQUIDITY",
                        "gas_buy_model_label": buy_gas_status,
                        "gas_sell_model_label": sell_gas_status,
                    }
                gross_proceeds_usd = (q_out_raw / (10 ** ev_touch.token0_decimals)) * touch_state.quote_usd_price
            else:
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": "V2_TOUCH_TARGET_TOKEN_NOT_IN_PAIR",
                    "gas_buy_model_label": buy_gas_status,
                    "gas_sell_model_label": sell_gas_status,
                }
        elif touch_state.venue_type == "UNISWAP_V3":
            ev3_touch = touch_state.evidence_v3
            if not ev3_touch or not ev3_touch.is_complete():
                return {
                    "status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value,
                    "net_return_pct": None,
                    "diagnostic": "V3_TOUCH_EVIDENCE_INCOMPLETE",
                    "gas_buy_model_label": buy_gas_status,
                    "gas_sell_model_label": sell_gas_status,
                }

            canon_target = canonicalize_token_address(touch_state.token_address)
            canon_t0 = canonicalize_token_address(ev3_touch.token0_address)
            canon_t1 = canonicalize_token_address(ev3_touch.token1_address)

            if canon_target == canon_t0:
                if touch_state.token0_is_quote:
                    return {
                        "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                        "net_return_pct": None,
                        "diagnostic": "V3_TOUCH_ORIENTATION_CONFLICT",
                        "gas_buy_model_label": buy_gas_status,
                        "gas_sell_model_label": sell_gas_status,
                    }
                z4o_sell = True
                dec_out = ev3_touch.token1_decimals
            elif canon_target == canon_t1:
                if not touch_state.token0_is_quote:
                    return {
                        "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                        "net_return_pct": None,
                        "diagnostic": "V3_TOUCH_ORIENTATION_CONFLICT",
                        "gas_buy_model_label": buy_gas_status,
                        "gas_sell_model_label": sell_gas_status,
                    }
                z4o_sell = False
                dec_out = ev3_touch.token0_decimals
            else:
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": "V3_TOUCH_TOKEN_NOT_IN_POOL",
                    "gas_buy_model_label": buy_gas_status,
                    "gas_sell_model_label": sell_gas_status,
                }

            ok_sell, consumed, q_out_raw, _, diag = UniswapV3SwapSimulator.simulate_swap(
                ev3_touch, amount_in=tokens_acquired_raw, zero_for_one=z4o_sell
            )
            if not ok_sell:
                return {
                    "status": DualTrackTargetStatus.MODELED_TP40_LIQUIDITY_INSUFFICIENT.value,
                    "net_return_pct": None,
                    "diagnostic": f"LEG2_SELL_FAILED_LIQUIDITY_{diag}",
                    "gas_buy_model_label": buy_gas_status,
                    "gas_sell_model_label": sell_gas_status,
                }
            gross_proceeds_usd = (q_out_raw / (10 ** dec_out)) * touch_state.quote_usd_price
        else:
            return {
                "status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value,
                "net_return_pct": None,
                "diagnostic": f"UNSUPPORTED_VENUE_{touch_state.venue_type}",
                "gas_buy_model_label": buy_gas_status,
                "gas_sell_model_label": sell_gas_status,
            }

        # 5. Net Proceeds & Decision
        net_proceeds_usd = gross_proceeds_usd - gas_buy_usd - gas_sell_usd
        net_return_pct = ((net_proceeds_usd - tranche_usd) / tranche_usd) * 100.0

        if net_return_pct >= TECHNICAL_TP_PCT:
            status = DualTrackTargetStatus.MODELED_TP40_EXECUTABLE_WITH_ASSUMPTIONS
        else:
            status = DualTrackTargetStatus.MODELED_TP40_EXECUTION_IMPACT_FAIL

        return {
            "status": status.value,
            "net_return_pct": round(net_return_pct, 4),
            "gross_proceeds_usd": round(gross_proceeds_usd, 4),
            "gas_buy_usd": round(gas_buy_usd, 4),
            "gas_sell_usd": round(gas_sell_usd, 4),
            "gas_buy_model_label": buy_gas_status,
            "gas_sell_model_label": sell_gas_status,
            "tranche_usd": tranche_usd,
            "tokens_acquired": tokens_acquired_raw,
        }

    @classmethod
    def simulate_multi_size(
        cls,
        entry_state: Optional[PoolStateSnapshot],
        touch_state: Optional[PoolStateSnapshot],
        tranches_usd: Optional[List[float]] = None,
    ) -> Dict[str, Any]:
        """Simulates all 5 standard scenarios ($100-$5,000) for depth probing."""
        if tranches_usd is None:
            tranches_usd = SIMULATION_TRANCHES_USD

        results = {}
        for tranche in tranches_usd:
            results[f"tranche_{int(tranche)}"] = cls.simulate_roundtrip(entry_state, touch_state, tranche_usd=tranche)

        return {
            "modeled_scenario_type": "HYPOTHETICAL_DEPTH_PROBING_ONLY_NOT_POSITION_SIZING",
            "tranches": results,
        }


@dataclasses.dataclass
class LiquidityObservationRecord:
    """Observation record for liquidity trajectory bound to exact block provenance."""
    chain_id: int
    token_mint: str
    pool_address: str
    block_number: int
    block_hash: str
    source_event_timestamp_utc: str
    local_ingestion_timestamp_utc: str
    d_ask_plus_2pct: int
    d_bid_minus_2pct: int
    liquidity_usd: float
    has_lp_burn_event: bool = False
    is_authenticated_chain_proof: bool = False


class LiquidityClassificationEngine:
    """Evaluates 4-tier liquidity classification hierarchy separating depth collapse from price depreciation."""

    @classmethod
    def evaluate_liquidity_trajectory(
        cls,
        records: List[LiquidityObservationRecord],
        initial_t0_utc: str,
    ) -> Dict[str, Any]:
        """Evaluates 24-hour liquidity trajectory respecting collapse precedence, strict window boundaries,
        and single-identity consistency.
        """
        if not records:
            return {
                "status": LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value,
                "diagnostic": "NO_LIQUIDITY_RECORDS_AVAILABLE",
                "window_end_status": "WINDOW_END_UNRESOLVED",
                "liquidity_end_usd_observed_unverified": None,
                "liquidity_end_usd_authenticated_verified": None,
                "recovery_observed_after_collapse": False,
                "recovery_confirmed_at_window_end": False,
            }

        # 1. Enforce common exact identity across all records
        target_chain = records[0].chain_id
        target_token = canonicalize_token_address(records[0].token_mint)
        target_pool = canonicalize_token_address(records[0].pool_address)

        for r in records:
            if (
                r.chain_id != target_chain
                or canonicalize_token_address(r.token_mint) != target_token
                or canonicalize_token_address(r.pool_address) != target_pool
            ):
                return {
                    "status": LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value,
                    "diagnostic": "MIXED_IDENTITY_IN_LIQUIDITY_RECORDS",
                    "window_end_status": "WINDOW_END_UNRESOLVED",
                    "liquidity_end_usd_observed_unverified": None,
                    "liquidity_end_usd_authenticated_verified": None,
                    "recovery_observed_after_collapse": False,
                    "recovery_confirmed_at_window_end": False,
                }

        # 2. Parse timestamps and sort chronologically
        try:
            t0_dt = dt.datetime.fromisoformat(initial_t0_utc.replace("Z", "+00:00"))
        except Exception:
            return {
                "status": LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value,
                "diagnostic": "INVALID_INITIAL_T0_TIMESTAMP",
                "window_end_status": "WINDOW_END_UNRESOLVED",
                "liquidity_end_usd_observed_unverified": None,
                "liquidity_end_usd_authenticated_verified": None,
                "recovery_observed_after_collapse": False,
                "recovery_confirmed_at_window_end": False,
            }

        parsed_records = []
        for r in records:
            try:
                r_dt = dt.datetime.fromisoformat(r.source_event_timestamp_utc.replace("Z", "+00:00"))
                parsed_records.append((r_dt, r))
            except Exception:
                return {
                    "status": LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value,
                    "diagnostic": "INVALID_RECORD_TIMESTAMP",
                    "window_end_status": "WINDOW_END_UNRESOLVED",
                    "liquidity_end_usd_observed_unverified": None,
                    "liquidity_end_usd_authenticated_verified": None,
                    "recovery_observed_after_collapse": False,
                    "recovery_confirmed_at_window_end": False,
                }

        parsed_records.sort(key=lambda x: x[0])

        # 3. Restrict records to 24-hour evaluation window [t0, t0 + 24.0h] (excluding out-of-window samples)
        t_init_end = t0_dt + dt.timedelta(hours=0.5)
        t_term_start = t0_dt + dt.timedelta(hours=23.5)
        t_term_end = t0_dt + dt.timedelta(hours=24.0)

        in_window = [(r_dt, r) for r_dt, r in parsed_records if t0_dt <= r_dt <= t_term_end]

        # 4. Initial sample check: [t0, t0 + 0.5h]
        initial_rec = None
        for r_dt, r in in_window:
            if t0_dt <= r_dt <= t_init_end:
                initial_rec = r
                break

        # 5. Terminal sample check: [t0 + 23.5h, t0 + 24.0h]
        terminal_rec = None
        for r_dt, r in reversed(in_window):
            if t_term_start <= r_dt <= t_term_end:
                terminal_rec = r
                break

        window_end_status = "WINDOW_END_RESOLVED_UNVERIFIED" if terminal_rec is not None else "WINDOW_END_UNRESOLVED"
        liquidity_end_usd_unverified = terminal_rec.liquidity_usd if terminal_rec else None
        liquidity_end_usd_verified = None  # Strictly None in Stage 3.2

        has_lp_burn = any(r.has_lp_burn_event for _, r in in_window)

        if initial_rec is None:
            if has_lp_burn:
                return {
                    "status": LiquidityClassificationStatus.LP_WITHDRAWAL_COLLAPSE_OBSERVED_UNVERIFIED.value,
                    "window_end_status": window_end_status,
                    "min_depth_ratio": None,
                    "depth_min_timestamp_utc": None,
                    "recovery_observed_after_collapse": False,
                    "recovery_confirmed_at_window_end": False,
                    "liquidity_end_usd_observed_unverified": liquidity_end_usd_unverified,
                    "liquidity_end_usd_authenticated_verified": None,
                }
            return {
                "status": LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value,
                "diagnostic": "INITIAL_SAMPLE_MISSING",
                "window_end_status": window_end_status,
                "min_depth_ratio": None,
                "recovery_observed_after_collapse": False,
                "recovery_confirmed_at_window_end": False,
                "liquidity_end_usd_observed_unverified": liquidity_end_usd_unverified,
                "liquidity_end_usd_authenticated_verified": None,
            }

        # Use validated initial-window sample as baseline
        initial_ask = initial_rec.d_ask_plus_2pct
        initial_bid = initial_rec.d_bid_minus_2pct
        initial_usd = initial_rec.liquidity_usd

        if initial_ask <= 0 or initial_bid <= 0:
            return {
                "status": LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value,
                "diagnostic": "INITIAL_DEPTH_NON_POSITIVE",
                "window_end_status": window_end_status,
                "min_depth_ratio": None,
                "liquidity_end_usd_observed_unverified": liquidity_end_usd_unverified,
                "liquidity_end_usd_authenticated_verified": None,
                "recovery_observed_after_collapse": False,
                "recovery_confirmed_at_window_end": False,
            }

        recovery_confirmed_end = False
        if terminal_rec is not None:
            r_end_depth = min(terminal_rec.d_ask_plus_2pct / initial_ask, terminal_rec.d_bid_minus_2pct / initial_bid)
            if r_end_depth >= 0.80:
                recovery_confirmed_end = True

        # Priority 1: Check for LP burn events or depth collapse (< 20% on either side)
        min_depth_ratio = 1.0
        depth_collapse_detected = False
        depth_min_timestamp = None

        for r_dt, r in in_window:
            ratio_ask = r.d_ask_plus_2pct / initial_ask
            ratio_bid = r.d_bid_minus_2pct / initial_bid
            r_depth = min(ratio_ask, ratio_bid)

            if r_depth < min_depth_ratio:
                min_depth_ratio = r_depth
                depth_min_timestamp = r.source_event_timestamp_utc

            if r_depth < 0.20:
                depth_collapse_detected = True

        # Recovery checks after collapse
        recovery_observed = False
        if depth_collapse_detected and depth_min_timestamp:
            for r_dt, r in in_window:
                if r.source_event_timestamp_utc > depth_min_timestamp:
                    r_depth = min(r.d_ask_plus_2pct / initial_ask, r.d_bid_minus_2pct / initial_bid)
                    if r_depth >= 0.80:
                        recovery_observed = True
                        break

        # Priority 1: Event-level LP removal or depth collapse
        if has_lp_burn:
            return {
                "status": LiquidityClassificationStatus.LP_WITHDRAWAL_COLLAPSE_OBSERVED_UNVERIFIED.value,
                "window_end_status": window_end_status,
                "min_depth_ratio": round(min_depth_ratio, 4),
                "depth_min_timestamp_utc": depth_min_timestamp,
                "recovery_observed_after_collapse": recovery_observed,
                "recovery_confirmed_at_window_end": recovery_confirmed_end,
                "liquidity_end_usd_observed_unverified": liquidity_end_usd_unverified,
                "liquidity_end_usd_authenticated_verified": None,
            }

        if depth_collapse_detected:
            return {
                "status": LiquidityClassificationStatus.LIQUIDITY_DEPTH_COLLAPSE_OBSERVED_UNVERIFIED.value,
                "window_end_status": window_end_status,
                "min_depth_ratio": round(min_depth_ratio, 4),
                "depth_min_timestamp_utc": depth_min_timestamp,
                "recovery_observed_after_collapse": recovery_observed,
                "recovery_confirmed_at_window_end": recovery_confirmed_end,
                "liquidity_end_usd_observed_unverified": liquidity_end_usd_unverified,
                "liquidity_end_usd_authenticated_verified": None,
            }

        # Priority 2: Missing observations & boundary coverage
        if terminal_rec is None:
            return {
                "status": LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value,
                "diagnostic": "TERMINAL_SAMPLE_MISSING",
                "window_end_status": "WINDOW_END_UNRESOLVED",
                "min_depth_ratio": round(min_depth_ratio, 4),
                "recovery_observed_after_collapse": False,
                "recovery_confirmed_at_window_end": False,
                "liquidity_end_usd_observed_unverified": None,
                "liquidity_end_usd_authenticated_verified": None,
            }

        has_gap = False
        for i in range(len(in_window) - 1):
            t1 = in_window[i][0]
            t2 = in_window[i + 1][0]
            if (t2 - t1).total_seconds() > 6 * 3600:
                has_gap = True
                break

        if has_gap:
            return {
                "status": LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value,
                "diagnostic": "MID_WINDOW_GAP_EXCEEDS_6_HOURS",
                "window_end_status": window_end_status,
                "min_depth_ratio": round(min_depth_ratio, 4),
                "recovery_observed_after_collapse": False,
                "recovery_confirmed_at_window_end": recovery_confirmed_end,
                "liquidity_end_usd_observed_unverified": liquidity_end_usd_unverified,
                "liquidity_end_usd_authenticated_verified": None,
            }

        # Priority 3: Degraded (0.20 <= min_depth_ratio < 0.80)
        if min_depth_ratio < 0.80:
            return {
                "status": LiquidityClassificationStatus.LIQUIDITY_DEPTH_DEGRADED_OBSERVED_UNVERIFIED.value,
                "window_end_status": window_end_status,
                "min_depth_ratio": round(min_depth_ratio, 4),
                "recovery_observed_after_collapse": False,
                "recovery_confirmed_at_window_end": recovery_confirmed_end,
                "liquidity_end_usd_observed_unverified": liquidity_end_usd_unverified,
                "liquidity_end_usd_authenticated_verified": None,
            }

        # Priority 4: Survived or Asset Price Depreciation
        min_usd_ratio = min((r.liquidity_usd / initial_usd) for _, r in in_window)
        if min_usd_ratio < 0.20:
            status = LiquidityClassificationStatus.ASSET_PRICE_DEPRECIATION_OBSERVED_UNVERIFIED
        else:
            status = LiquidityClassificationStatus.LIQUIDITY_DEPTH_SURVIVED_OBSERVED_UNVERIFIED

        return {
            "status": status.value,
            "window_end_status": window_end_status,
            "min_depth_ratio": round(min_depth_ratio, 4),
            "min_usd_ratio": round(min_usd_ratio, 4),
            "recovery_observed_after_collapse": False,
            "recovery_confirmed_at_window_end": recovery_confirmed_end,
            "liquidity_end_usd_observed_unverified": liquidity_end_usd_unverified,
            "liquidity_end_usd_authenticated_verified": None,
        }


class DualTrackOutcomeEvaluator:
    """Evaluates technical pattern attainment independently from modeled execution."""

    @staticmethod
    def evaluate_market_technical_tp(
        entry_price: float,
        max_candle_high_24h: float,
    ) -> bool:
        """Evaluates whether market price reached technical +40.00% TP within 24h."""
        if entry_price <= 0:
            return False
        target_price = entry_price * (1.0 + TECHNICAL_TP_PCT / 100.0)
        return max_candle_high_24h >= target_price

    @staticmethod
    def evaluate_modeled_execution(
        is_evidence_complete: bool,
        gross_return_pct: float,
        price_impact_pct: float,
        fee_pct: float,
        gas_drag_pct: float,
    ) -> Tuple[ModeledExecutionStatus, Optional[float]]:
        """Evaluates hypothetical net execution proceeds. Never labeled verified."""
        if not is_evidence_complete:
            return ModeledExecutionStatus.EXECUTION_UNVERIFIED, None

        net_return_pct = gross_return_pct - price_impact_pct - fee_pct - gas_drag_pct
        return ModeledExecutionStatus.MODELED_EXECUTION_WITH_ASSUMPTIONS, net_return_pct


# ==============================================================================
# 7. Dataset A Trade Journal Financial Reconciliation
# ==============================================================================

def canonicalize_token_address(token_identifier: str) -> str:
    """Extracts canonical on-chain token address by stripping network namespace prefixes.
    e.g. 'ethereum:0x1111' -> '0x1111', 'solana:token-a' -> 'token-a', '0x1111' -> '0x1111'
    """
    s = str(token_identifier).strip().lower()
    if ":" in s:
        s = s.split(":")[-1].strip()
    return s


@dataclasses.dataclass
class OnChainTransferEvent:
    """Validated transfer log event."""
    token_address: str
    from_address: str
    to_address: str
    amount: float


@dataclasses.dataclass
class ValidatedTransactionReceipt:
    """Cryptographic/node-validated transaction receipt with event logs."""
    chain_id: int
    tx_hash: str
    block_number: int
    block_hash: str
    status: int  # 1: success, 0: revert
    gas_used: int
    effective_gas_price_wei: int
    transfers: list[OnChainTransferEvent]
    raw_receipt_bytes: bytes
    expected_receipt_sha256: str = ""
    is_canonical_block: bool = False
    reconciled_gas_cost: Optional[float] = None
    reconciled_quote_amount: Optional[float] = None
    is_trusted_chain_validated: bool = False

    @property
    def receipt_sha256(self) -> str:
        if not self.raw_receipt_bytes:
            return ""
        return hashlib.sha256(self.raw_receipt_bytes).hexdigest().upper()

    def verify_integrity(self) -> Tuple[bool, str]:
        """Validates raw payload bytes, expected digest, and canonical block confirmation."""
        if not self.raw_receipt_bytes:
            return False, "EMPTY_RECEIPT_BYTES"
        if not self.expected_receipt_sha256:
            return False, "MISSING_EXPECTED_RECEIPT_SHA256"
        if self.receipt_sha256 != self.expected_receipt_sha256.upper():
            return False, "RECEIPT_SHA256_MISMATCH"
        if not self.is_canonical_block:
            return False, "NON_CANONICAL_RECEIPT_BLOCK"
        if not self.block_hash or not self.tx_hash:
            return False, "MISSING_BLOCK_OR_TX_HASH"
        return True, "RECEIPT_INTEGRITY_VERIFIED"


@dataclasses.dataclass
class JournalEntryPosition:
    token_id: str
    chain_id: int
    wallet_address: str
    tx_hash: str
    quote_expenditure: float
    tokens_acquired: float
    entry_gas_cost: float
    entry_venue_fees: float


@dataclasses.dataclass
class JournalExitExecution:
    token_id: str
    chain_id: int
    wallet_address: str
    tx_hash: str
    tokens_sold: float
    gross_proceeds: float
    exit_gas_cost: float
    exit_venue_fees: float
    other_costs: float = 0.0


@dataclasses.dataclass
class ReconciledTrancheOutcome:
    status: EvidenceTier
    alpha_ratio: float
    allocated_expenditure: float
    allocated_entry_gas: float
    allocated_entry_fees: float
    realized_net_pl: float
    realized_net_return_pct: float
    tokens_remaining: float
    cost_basis_remaining: float
    contributes_to_verified_pl: bool


class DatasetAReconciliationEngine:
    """Reconciles user trade journal tranches with validated on-chain transfer evidence."""

    @classmethod
    def reconcile_tranche(
        cls,
        entry: JournalEntryPosition,
        exit_exec: JournalExitExecution,
        entry_receipt: Optional[ValidatedTransactionReceipt] = None,
        exit_receipt: Optional[ValidatedTransactionReceipt] = None,
    ) -> ReconciledTrancheOutcome:
        # 1. Finite and non-negative value validation
        values_to_check = [
            entry.quote_expenditure, entry.tokens_acquired, entry.entry_gas_cost, entry.entry_venue_fees,
            exit_exec.tokens_sold, exit_exec.gross_proceeds, exit_exec.exit_gas_cost, exit_exec.exit_venue_fees,
            exit_exec.other_costs
        ]
        if not all(math.isfinite(v) and v >= 0 for v in values_to_check):
            return ReconciledTrancheOutcome(
                status=EvidenceTier.NON_FINITE_OR_NEGATIVE_VALUES,
                alpha_ratio=0.0,
                allocated_expenditure=0.0,
                allocated_entry_gas=0.0,
                allocated_entry_fees=0.0,
                realized_net_pl=0.0,
                realized_net_return_pct=0.0,
                tokens_remaining=entry.tokens_acquired if math.isfinite(entry.tokens_acquired) else 0.0,
                cost_basis_remaining=entry.quote_expenditure if math.isfinite(entry.quote_expenditure) else 0.0,
                contributes_to_verified_pl=False,
            )

        # 2. Identity reconciliation (token, chain, wallet, transactions)
        if not (
            entry.token_id == exit_exec.token_id
            and entry.chain_id == exit_exec.chain_id
            and entry.wallet_address.lower() == exit_exec.wallet_address.lower()
            and bool(entry.tx_hash and exit_exec.tx_hash)
        ):
            return ReconciledTrancheOutcome(
                status=EvidenceTier.IDENTITY_RECONCILIATION_FAILED,
                alpha_ratio=0.0,
                allocated_expenditure=0.0,
                allocated_entry_gas=0.0,
                allocated_entry_fees=0.0,
                realized_net_pl=0.0,
                realized_net_return_pct=0.0,
                tokens_remaining=entry.tokens_acquired,
                cost_basis_remaining=entry.quote_expenditure,
                contributes_to_verified_pl=False,
            )

        # 3. Strict volume constraint: Reject tokens_sold > tokens_acquired (never clamp!)
        if exit_exec.tokens_sold > entry.tokens_acquired:
            return ReconciledTrancheOutcome(
                status=EvidenceTier.INVALID_TRANCHE_QUANTITY_EXCEEDED,
                alpha_ratio=0.0,
                allocated_expenditure=0.0,
                allocated_entry_gas=0.0,
                allocated_entry_fees=0.0,
                realized_net_pl=0.0,
                realized_net_return_pct=0.0,
                tokens_remaining=entry.tokens_acquired,
                cost_basis_remaining=entry.quote_expenditure,
                contributes_to_verified_pl=False,
            )

        # 4. Validated Receipt and Transfer Log Evidence Binding
        if entry_receipt is None or exit_receipt is None:
            return ReconciledTrancheOutcome(
                status=EvidenceTier.USER_REPORTED_EXECUTION,
                alpha_ratio=0.0,
                allocated_expenditure=0.0,
                allocated_entry_gas=0.0,
                allocated_entry_fees=0.0,
                realized_net_pl=0.0,
                realized_net_return_pct=0.0,
                tokens_remaining=entry.tokens_acquired,
                cost_basis_remaining=entry.quote_expenditure,
                contributes_to_verified_pl=False,
            )

        # 4a. Verify cryptographic payload integrity and canonical block status
        entry_ok, _ = entry_receipt.verify_integrity()
        exit_ok, _ = exit_receipt.verify_integrity()
        if not (entry_ok and exit_ok):
            return ReconciledTrancheOutcome(
                status=EvidenceTier.RECEIPT_VERIFICATION_FAILED,
                alpha_ratio=0.0,
                allocated_expenditure=0.0,
                allocated_entry_gas=0.0,
                allocated_entry_fees=0.0,
                realized_net_pl=0.0,
                realized_net_return_pct=0.0,
                tokens_remaining=entry.tokens_acquired,
                cost_basis_remaining=entry.quote_expenditure,
                contributes_to_verified_pl=False,
            )

        # 4b. Check receipt success status
        if entry_receipt.status != 1 or exit_receipt.status != 1:
            return ReconciledTrancheOutcome(
                status=EvidenceTier.EXECUTION_EVIDENCE_INCOMPLETE,
                alpha_ratio=0.0,
                allocated_expenditure=0.0,
                allocated_entry_gas=0.0,
                allocated_entry_fees=0.0,
                realized_net_pl=0.0,
                realized_net_return_pct=0.0,
                tokens_remaining=entry.tokens_acquired,
                cost_basis_remaining=entry.quote_expenditure,
                contributes_to_verified_pl=False,
            )

        # 4c. Reconcile receipts against exact chain and transaction hashes
        if not (
            entry_receipt.chain_id == entry.chain_id
            and entry_receipt.tx_hash.lower() == entry.tx_hash.lower()
            and exit_receipt.chain_id == exit_exec.chain_id
            and exit_receipt.tx_hash.lower() == exit_exec.tx_hash.lower()
        ):
            return ReconciledTrancheOutcome(
                status=EvidenceTier.RECEIPT_VERIFICATION_FAILED,
                alpha_ratio=0.0,
                allocated_expenditure=0.0,
                allocated_entry_gas=0.0,
                allocated_entry_fees=0.0,
                realized_net_pl=0.0,
                realized_net_return_pct=0.0,
                tokens_remaining=entry.tokens_acquired,
                cost_basis_remaining=entry.quote_expenditure,
                contributes_to_verified_pl=False,
            )

        # 4d. Verify transfer log amounts matching acquired and sold quantities with canonical token_address
        canonical_entry_token = canonicalize_token_address(entry.token_id)
        canonical_exit_token = canonicalize_token_address(exit_exec.token_id)

        has_entry_transfer = any(
            canonicalize_token_address(t.token_address) == canonical_entry_token
            and t.to_address.lower() == entry.wallet_address.lower()
            and math.isclose(t.amount, entry.tokens_acquired, rel_tol=1e-4)
            for t in entry_receipt.transfers
        )
        has_exit_transfer = any(
            canonicalize_token_address(t.token_address) == canonical_exit_token
            and t.from_address.lower() == exit_exec.wallet_address.lower()
            and math.isclose(t.amount, exit_exec.tokens_sold, rel_tol=1e-4)
            for t in exit_receipt.transfers
        )

        if not (has_entry_transfer and has_exit_transfer):
            return ReconciledTrancheOutcome(
                status=EvidenceTier.RECEIPT_VERIFICATION_FAILED,
                alpha_ratio=0.0,
                allocated_expenditure=0.0,
                allocated_entry_gas=0.0,
                allocated_entry_fees=0.0,
                realized_net_pl=0.0,
                realized_net_return_pct=0.0,
                tokens_remaining=entry.tokens_acquired,
                cost_basis_remaining=entry.quote_expenditure,
                contributes_to_verified_pl=False,
            )

        # 4e. Required amounts validation: proceeds and gas costs must be present and non-negative
        if (
            entry_receipt.reconciled_gas_cost is None
            or entry_receipt.reconciled_quote_amount is None
            or exit_receipt.reconciled_gas_cost is None
            or exit_receipt.reconciled_quote_amount is None
            or entry_receipt.reconciled_gas_cost < 0
            or entry_receipt.reconciled_quote_amount < 0
            or exit_receipt.reconciled_gas_cost < 0
            or exit_receipt.reconciled_quote_amount < 0
        ):
            return ReconciledTrancheOutcome(
                status=EvidenceTier.EXECUTION_EVIDENCE_INCOMPLETE,
                alpha_ratio=0.0,
                allocated_expenditure=0.0,
                allocated_entry_gas=0.0,
                allocated_entry_fees=0.0,
                realized_net_pl=0.0,
                realized_net_return_pct=0.0,
                tokens_remaining=entry.tokens_acquired,
                cost_basis_remaining=entry.quote_expenditure,
                contributes_to_verified_pl=False,
            )

        # Check reconciliation against reported costs
        if (
            not math.isclose(entry.entry_gas_cost, entry_receipt.reconciled_gas_cost, rel_tol=1e-2)
            or not math.isclose(entry.quote_expenditure, entry_receipt.reconciled_quote_amount, rel_tol=1e-2)
            or not math.isclose(exit_exec.exit_gas_cost, exit_receipt.reconciled_gas_cost, rel_tol=1e-2)
            or not math.isclose(exit_exec.gross_proceeds, exit_receipt.reconciled_quote_amount, rel_tol=1e-2)
        ):
            return ReconciledTrancheOutcome(
                status=EvidenceTier.RECEIPT_VERIFICATION_FAILED,
                alpha_ratio=0.0,
                allocated_expenditure=0.0,
                allocated_entry_gas=0.0,
                allocated_entry_fees=0.0,
                realized_net_pl=0.0,
                realized_net_return_pct=0.0,
                tokens_remaining=entry.tokens_acquired,
                cost_basis_remaining=entry.quote_expenditure,
                contributes_to_verified_pl=False,
            )

        if exit_exec.tokens_sold <= 0 or entry.tokens_acquired <= 0:
            return ReconciledTrancheOutcome(
                status=EvidenceTier.EXECUTION_EVIDENCE_INCOMPLETE,
                alpha_ratio=0.0,
                allocated_expenditure=0.0,
                allocated_entry_gas=0.0,
                allocated_entry_fees=0.0,
                realized_net_pl=0.0,
                realized_net_return_pct=0.0,
                tokens_remaining=entry.tokens_acquired,
                cost_basis_remaining=entry.quote_expenditure,
                contributes_to_verified_pl=False,
            )

        # 5. Exact pro-rata cost-basis allocation (Q_sold <= Q_acquired)
        alpha = exit_exec.tokens_sold / entry.tokens_acquired
        allocated_exp = round(entry.quote_expenditure * alpha, 6)
        allocated_gas_entry = round(entry.entry_gas_cost * alpha, 6)
        allocated_fees_entry = round(entry.entry_venue_fees * alpha, 6)

        realized_net_pl = (
            exit_exec.gross_proceeds
            - allocated_exp
            - (allocated_gas_entry + exit_exec.exit_gas_cost)
            - (allocated_fees_entry + exit_exec.exit_venue_fees)
            - exit_exec.other_costs
        )

        total_cost_basis = allocated_exp + allocated_gas_entry + allocated_fees_entry
        realized_return_pct = (realized_net_pl / total_cost_basis) * 100.0 if total_cost_basis > 0 else 0.0

        tokens_remaining = round(entry.tokens_acquired - exit_exec.tokens_sold, 6)
        cost_basis_remaining = round(entry.quote_expenditure - allocated_exp, 6)

        # 6. Provenance classification:
        # Checking caller-supplied boolean flags or digests does not authenticate the chain source.
        # Until receipt bytes and state logs are independently fetched or cryptographically validated
        # by the engine against an authenticated, trusted chain client/node, all caller-supplied
        # receipts remain strictly classified as USER_REPORTED_EXECUTION and are excluded from verified P/L
        # (contributes_to_verified_pl = False). Even if a receipt asserts is_trusted_chain_validated=True,
        # it cannot promote to INDEPENDENTLY_VERIFIED_EXECUTION.
        status = EvidenceTier.USER_REPORTED_EXECUTION
        contributes = False

        return ReconciledTrancheOutcome(
            status=status,
            alpha_ratio=alpha,
            allocated_expenditure=allocated_exp,
            allocated_entry_gas=allocated_gas_entry,
            allocated_entry_fees=allocated_fees_entry,
            realized_net_pl=round(realized_net_pl, 4),
            realized_net_return_pct=round(realized_return_pct, 4),
            tokens_remaining=tokens_remaining,
            cost_basis_remaining=cost_basis_remaining,
            contributes_to_verified_pl=contributes,
        )


# ==============================================================================
# 8. Statistical Validation & Multi-Block Dependence Engine
# ==============================================================================

class ClusteredEvaluationEngine:
    """Non-parametric two-way clustering and serial-dependence diagnostic engine.
    
    Adheres strictly to:
    - Chronological sorting of weekly blocks
    - Safe handling of empty (N=0) and single-observation (N=1) datasets
    - Cameron, Gelbach, & Miller (2011) two-way clustered variance
    - Cameron & Miller (2015) non-positive variance fallback heuristic
    - Exact Student's t critical values for actual df = min(M - 1, B - 1)
    - Newey-West (1987) / Bartlett kernel time-series variance fallback for dependent blocks
    - Strictly exploratory labeling: confirmatory status is explicitly avoided because
      finite-sample diagnostic screens cannot establish asymptotic serial independence.
    """

    # Exact two-sided 95% Student's t critical values for df 1 through 30
    EXACT_STUDENTS_T_CRIT_95: Dict[int, float] = {
        1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
        6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
        16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
        21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
        26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
    }

    @classmethod
    def get_exact_students_t_crit(cls, df: int) -> float:
        """Returns exact two-sided 95% critical value for degrees of freedom df."""
        if df < 1:
            return 12.706
        if df in cls.EXACT_STUDENTS_T_CRIT_95:
            return cls.EXACT_STUDENTS_T_CRIT_95[df]
        # Precise Cornish-Fisher asymptotic expansion for df > 30 (z = 1.959964)
        z = 1.959964
        t_val = z + (z**3 + z) / (4.0 * df) + (5.0 * z**5 + 16.0 * z**3 + 3.0 * z) / (96.0 * df**2)
        return round(t_val, 4)

    @classmethod
    def get_structural_counts(cls, dataset: list[dict[str, Any]]) -> dict[str, int]:
        """Returns raw structural dimensions normalized by parsed week index."""
        tokens = set(x.get("token_id") for x in dataset)
        raw_weeks = [x.get("week_block") for x in dataset]
        is_valid, _, unique_blocks, _ = cls.validate_and_classify_dataset_weeks(raw_weeks)
        if is_valid and unique_blocks:
            b_weeks = len(unique_blocks)
        else:
            b_weeks = len(set(raw_weeks))
        return {
            "M_tokens": len(tokens),
            "B_weeks": b_weeks,
            "N_obs": len(dataset),
        }

    @classmethod
    def compute_cgm_two_way_variance(
        cls, dataset: list[dict[str, Any]], field: str = "loss_diff"
    ) -> dict[str, Any]:
        """Calculates Cameron-Gelbach-Miller (2011) two-way clustered variance with safe edge cases."""
        N = len(dataset)
        if N == 0:
            return {
                "mean_d": 0.0, "var_naive": 0.0, "var_token": 0.0, "var_time": 0.0,
                "var_inter": 0.0, "var_twoway": 0.0, "var_reported": 0.0, "se_reported": 0.0,
                "se_naive": 0.0, "fallback_invoked": False,
            }
        if N == 1:
            val = float(dataset[0][field])
            return {
                "mean_d": val, "var_naive": 0.0, "var_token": 0.0, "var_time": 0.0,
                "var_inter": 0.0, "var_twoway": 0.0, "var_reported": 0.0, "se_reported": 0.0,
                "se_naive": 0.0, "fallback_invoked": False,
            }

        counts = cls.get_structural_counts(dataset)
        M = counts["M_tokens"]
        B = counts["B_weeks"]

        mean_d = sum(x[field] for x in dataset) / N
        residuals = [x[field] - mean_d for x in dataset]

        # 1. Naive OLS variance of mean
        var_naive = (sum(r**2 for r in residuals) / (N - 1)) / N

        # 2. Token-clustered variance (V_token)
        token_sums: dict[str, float] = {}
        for idx, obs in enumerate(dataset):
            t_id = obs["token_id"]
            token_sums[t_id] = token_sums.get(t_id, 0.0) + residuals[idx]
        var_token = (M / (M - 1)) * sum(s**2 for s in token_sums.values()) / (N**2) if M > 1 else var_naive

        # 3. Time-block clustered variance (V_time)
        # Group observations by continuous week index so aliases belong to the same cluster
        raw_weeks = [x.get("week_block") for x in dataset]
        is_valid, _, _, _ = cls.validate_and_classify_dataset_weeks(raw_weeks)
        if is_valid:
            parsed_idx_map = {w: cls.classify_and_parse_week(w)[1] for w in set(raw_weeks)}
        else:
            parsed_idx_map = {w: w for w in set(raw_weeks)}

        week_sums: dict[Any, float] = {}
        for idx, obs in enumerate(dataset):
            w_val = obs.get("week_block")
            cluster_key = parsed_idx_map.get(w_val, w_val)
            week_sums[cluster_key] = week_sums.get(cluster_key, 0.0) + residuals[idx]
        var_time = (B / (B - 1)) * sum(s**2 for s in week_sums.values()) / (N**2) if B > 1 else var_naive

        # 4. Intersection variance (V_token_intersect_time)
        inter_sums: dict[Tuple[str, Any], float] = {}
        for idx, obs in enumerate(dataset):
            t_id = obs["token_id"]
            w_val = obs.get("week_block")
            cluster_key = parsed_idx_map.get(w_val, w_val)
            pair_key = (t_id, cluster_key)
            inter_sums[pair_key] = inter_sums.get(pair_key, 0.0) + residuals[idx]
        K_inter = len(inter_sums)
        var_inter = (K_inter / (K_inter - 1)) * sum(s**2 for s in inter_sums.values()) / (N**2) if K_inter > 1 else var_naive

        # 5. Exact Cameron-Gelbach-Miller two-way variance
        var_twoway = var_token + var_time - var_inter

        # Consistent non-positive fallback rule (Cameron & Miller 2015 heuristic)
        if var_twoway <= 0:
            var_reported = max(var_token, var_time)
            fallback_invoked = True
        else:
            var_reported = var_twoway
            fallback_invoked = False

        se_reported = math.sqrt(var_reported)

        return {
            "mean_d": mean_d,
            "var_naive": var_naive,
            "var_token": var_token,
            "var_time": var_time,
            "var_inter": var_inter,
            "var_twoway": var_twoway,
            "var_reported": var_reported,
            "se_reported": se_reported,
            "se_naive": math.sqrt(var_naive),
            "fallback_invoked": fallback_invoked,
        }

    @classmethod
    def classify_and_parse_week(cls, week_val: Any) -> Tuple[str, Optional[int]]:
        """Classifies a week identifier into an explicit format family and continuous week index.
        Returns: (format_family, integer_index) or ('INVALID', None). Never fabricates arbitrary hashes.
        Rejects ambiguous numeric timestamps lacking an explicit unit.
        """
        if week_val is None:
            return "INVALID", None
        s = str(week_val).strip()
        if not s:
            return "INVALID", None

        # 1. ISO week: YYYY-Www (e.g. 2026-W05)
        iso_week_match = re.fullmatch(r"(\d{4})-W(\d{1,2})", s, re.IGNORECASE)
        if iso_week_match:
            year = int(iso_week_match.group(1))
            week = int(iso_week_match.group(2))
            if 1 <= week <= 53:
                try:
                    return "ISO_CALENDAR", dt.date.fromisocalendar(year, week, 1).toordinal() // 7
                except (ValueError, OverflowError):
                    return "INVALID", None
            return "INVALID", None

        # 2. ISO calendar date: YYYY-MM-DD
        iso_date_match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
        if iso_date_match:
            try:
                d = dt.date.fromisoformat(s)
                y, w, _ = d.isocalendar()
                return "ISO_CALENDAR", dt.date.fromisocalendar(y, w, 1).toordinal() // 7
            except ValueError:
                return "INVALID", None

        # 3. Explicit relative week label: week_01, week_12
        week_num_match = re.fullmatch(r"week_?(\d+)", s, re.IGNORECASE)
        if week_num_match:
            return "RELATIVE_LABEL", int(week_num_match.group(1))

        # 4. Explicit Unix timestamp with unit (seconds vs milliseconds)
        # Suffix matching:
        ms_suffix_match = re.fullmatch(r"(\d+)(?:_?ms|_?msec)", s, re.IGNORECASE)
        s_suffix_match = re.fullmatch(r"(\d+)(?:_?s|_?sec)", s, re.IGNORECASE)
        epoch_prefix_match = re.fullmatch(r"epoch_?(s|ms):?\s*(\d+)", s, re.IGNORECASE)

        if ms_suffix_match:
            ms_val = int(ms_suffix_match.group(1))
            return "UNIX_TIMESTAMP", (ms_val // 1000) // (7 * 86400)
        elif s_suffix_match:
            s_val = int(s_suffix_match.group(1))
            return "UNIX_TIMESTAMP", s_val // (7 * 86400)
        elif epoch_prefix_match:
            unit = epoch_prefix_match.group(1).lower()
            val = int(epoch_prefix_match.group(2))
            if unit == "ms":
                return "UNIX_TIMESTAMP", (val // 1000) // (7 * 86400)
            else:
                return "UNIX_TIMESTAMP", val // (7 * 86400)

        # 5. Plain relative integer (small numbers e.g. 0, 1, 2, ..., 52, up to 100,000)
        # Large bare integers (> 100,000) without explicit unit are strictly rejected as ambiguous
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            try:
                val = int(s)
                if abs(val) <= 100_000:
                    return "RELATIVE_INTEGER", val
                else:
                    return "INVALID", None
            except ValueError:
                return "INVALID", None

        return "INVALID", None

    @classmethod
    def validate_and_classify_dataset_weeks(
        cls, raw_weeks: list[Any]
    ) -> Tuple[bool, str, list[dict[str, Any]], str]:
        """Validates that all week identifiers in the dataset conform to a single, consistent format family.
        Strictly rejects invalid identifiers, ambiguous numeric timestamps, and mixed formats.
        Returns: (is_valid, format_family, unique_blocks, diagnostic_reason)
        where unique_blocks is sorted chronologically by continuous week index and groups same-week aliases.
        """
        if not raw_weeks:
            return True, "EMPTY", [], "EMPTY_DATASET"

        parsed_map: dict[Any, Tuple[str, int]] = {}
        for w in set(raw_weeks):
            fmt, idx = cls.classify_and_parse_week(w)
            if fmt == "INVALID" or idx is None:
                s = str(w).strip()
                if s.isdigit() and int(s) > 100_000:
                    reason = f"Ambiguous numeric timestamp without explicit unit (e.g. 's' or 'ms'): {w}"
                else:
                    reason = f"Unrecognized or invalid week identifier: {w}"
                return False, "INVALID", [], reason
            parsed_map[w] = (fmt, idx)

        formats = {fmt for fmt, _ in parsed_map.values()}
        if len(formats) > 1:
            return False, "MIXED", [], f"Mixed week format families detected: {sorted(list(formats))}"

        format_family = list(formats)[0]

        # Group by unique week index (normalizing same-week aliases)
        # Maintain first seen label order from raw_weeks for consistent block labeling
        first_seen_label: dict[int, Any] = {}
        all_labels_for_idx: dict[int, set[Any]] = {}
        for w in raw_weeks:
            idx = parsed_map[w][1]
            if idx not in first_seen_label:
                first_seen_label[idx] = w
                all_labels_for_idx[idx] = set()
            all_labels_for_idx[idx].add(w)

        unique_indices = sorted(first_seen_label.keys())
        unique_blocks = [
            {
                "week_index": idx,
                "representative_label": first_seen_label[idx],
                "alias_labels": all_labels_for_idx[idx],
            }
            for idx in unique_indices
        ]

        return True, format_family, unique_blocks, "VALID_CONSISTENT_FORMAT"

    @classmethod
    def parse_utc_week_index(cls, week_val: Any) -> int:
        """Parses arbitrary week representations into an integer continuous week index. Raises ValueError on invalid format."""
        fmt, idx = cls.classify_and_parse_week(week_val)
        if fmt == "INVALID" or idx is None:
            raise ValueError(f"Unrecognized or invalid week identifier: {week_val}")
        return idx

    @classmethod
    def _sort_weeks_chronologically(cls, week_keys: list[Any]) -> list[Any]:
        """Strictly sorts week identifiers in chronological order using validated UTC week index."""
        unique_weeks = list(set(week_keys))
        return sorted(unique_weeks, key=lambda w: (cls.parse_utc_week_index(w), str(w)))

    @classmethod
    def compute_newey_west_block_variance(
        cls,
        block_means: list[float],
        week_indices: list[int] | None = None,
        bandwidth: int | None = None,
    ) -> float:
        """Computes Newey-West (1987) / Bartlett kernel time-series variance on weekly block means."""
        B = len(block_means)
        if B < 2:
            return 0.0
        grand_mean = sum(block_means) / B
        res = [bm - grand_mean for bm in block_means]

        L = bandwidth or max(1, math.ceil(B ** 0.25))
        gamma_0 = sum(r**2 for r in res) / B

        omega = gamma_0
        for l in range(1, min(L + 1, B)):
            if week_indices is not None:
                pairs = [(i, j) for i in range(B) for j in range(B) if week_indices[i] == week_indices[j] + l]
                gamma_l = (sum(res[i] * res[j] for i, j in pairs) / B) if pairs else 0.0
            else:
                gamma_l = sum(res[t] * res[t - l] for t in range(l, B)) / B
            weight = 1.0 - (l / (L + 1))
            omega += 2.0 * weight * gamma_l

        var_mean = max(0.0, omega / B)
        return var_mean

    @classmethod
    def evaluate_inter_block_serial_dependence(
        cls, dataset: list[dict[str, Any]], field: str = "loss_diff"
    ) -> dict[str, Any]:
        """Evaluates lag-1 and lag-2 autocorrelations of weekly block-mean residuals in chronological order.
        Strictly rejects invalid identifiers, ambiguous numeric timestamps, and mixed formats
        before performing any count or dependence calculations.
        Normalizes same-week aliases by continuous week index.
        """
        raw_weeks = [x.get("week_block") for x in dataset]
        is_valid, fmt_family, unique_blocks, reason = cls.validate_and_classify_dataset_weeks(raw_weeks)

        # 1. Format validation first: invalid or mixed formats fail closed immediately
        if not is_valid:
            return {
                "rho_1": 0.0,
                "rho_adj": 0.0,
                "rho_2": 0.0,
                "bartlett_threshold": 1.0,
                "fallback_triggered": False,
                "newey_west_se": 0.0,
                "week_means": [],
                "chronological_weeks": [],
                "is_valid_format": False,
                "diagnostic_reason": reason,
            }

        # 2. Extract normalized blocks grouped by week index
        B = len(unique_blocks)
        if B < 3:
            block_means = []
            chronological_weeks = []
            if unique_blocks:
                parsed_idx_map = {w: cls.classify_and_parse_week(w)[1] for w in set(raw_weeks)}
                for b in unique_blocks:
                    idx = b["week_index"]
                    obs_w = [x[field] for x in dataset if parsed_idx_map.get(x.get("week_block")) == idx]
                    if obs_w:
                        block_means.append(sum(obs_w) / len(obs_w))
                        chronological_weeks.append(b["representative_label"])

            return {
                "rho_1": 0.0,
                "rho_adj": 0.0,
                "rho_2": 0.0,
                "bartlett_threshold": 1.0,
                "fallback_triggered": False,
                "newey_west_se": 0.0,
                "week_means": block_means,
                "chronological_weeks": chronological_weeks,
                "is_valid_format": True,
                "diagnostic_reason": "B_WEEKS_LESS_THAN_3",
            }

        # 3. For B >= 3, compute week means and lag correlations normalized by parsed week index
        parsed_idx_map = {w: cls.classify_and_parse_week(w)[1] for w in set(raw_weeks)}
        week_means = []
        chronological_weeks = []
        week_indices = []

        for b in unique_blocks:
            idx = b["week_index"]
            obs_w = [x[field] for x in dataset if parsed_idx_map.get(x.get("week_block")) == idx]
            week_means.append(sum(obs_w) / len(obs_w))
            chronological_weeks.append(b["representative_label"])
            week_indices.append(idx)

        grand_mean = sum(week_means) / len(week_means)
        res = [wm - grand_mean for wm in week_means]
        denom = sum(r**2 for r in res)

        # Lag-1 autocorrelation strictly between consecutive calendar weeks (week_idx[i] == week_idx[j] + 1)
        lag1_pairs = [
            (i, j) for i in range(len(week_means)) for j in range(len(week_means))
            if week_indices[i] == week_indices[j] + 1
        ]
        num_1 = sum(res[i] * res[j] for i, j in lag1_pairs) if lag1_pairs else 0.0
        rho_1 = num_1 / denom if denom > 0 and lag1_pairs else 0.0
        rho_adj_1 = (rho_1 + (1.0 + 3.0 * rho_1) / B) if B > 0 else 0.0

        # Lag-2 autocorrelation strictly between weeks separated by exactly 2 weeks (week_idx[i] == week_idx[j] + 2)
        lag2_pairs = [
            (i, j) for i in range(len(week_means)) for j in range(len(week_means))
            if week_indices[i] == week_indices[j] + 2
        ]
        num_2 = sum(res[i] * res[j] for i, j in lag2_pairs) if lag2_pairs else 0.0
        rho_2 = num_2 / denom if denom > 0 and lag2_pairs else 0.0

        bartlett_threshold = 2.0 / math.sqrt(B)
        fallback_triggered = (
            (abs(rho_1) > bartlett_threshold)
            or (abs(rho_adj_1) > bartlett_threshold)
            or (abs(rho_2) > bartlett_threshold)
        )

        nw_var = cls.compute_newey_west_block_variance(week_means, week_indices=week_indices)
        nw_se = math.sqrt(nw_var)

        return {
            "rho_1": rho_1,
            "rho_adj": rho_adj_1,
            "rho_2": rho_2,
            "bartlett_threshold": bartlett_threshold,
            "fallback_triggered": fallback_triggered,
            "newey_west_se": nw_se,
            "week_means": week_means,
            "chronological_weeks": chronological_weeks,
            "is_valid_format": True,
            "diagnostic_reason": "VALID_CONSISTENT_FORMAT",
        }

    @classmethod
    def run_stage31_evaluation(
        cls, dataset: list[dict[str, Any]], field: str = "loss_diff"
    ) -> dict[str, Any]:
        """Executes Stage 3.1 inferential evaluation pipeline. All outputs remain strictly exploratory."""
        N = len(dataset)
        if N < 2:
            mean_d = float(dataset[0][field]) if N == 1 else 0.0
            return {
                "status": InferentialStatus.EXPLORATORY_UNCALIBRATED_INFERENCE.value,
                "diagnostic_flag": "INSUFFICIENT_DATA_N_LESS_THAN_2",
                "structural_counts": cls.get_structural_counts(dataset),
                "degrees_of_freedom": 1,
                "t_critical_value": 12.706,
                "mean_treatment_effect": round(mean_d, 6),
                "se_effective": 0.0,
                "se_cgm_two_way": 0.0,
                "se_newey_west": 0.0,
                "ci_95": (round(mean_d, 6), round(mean_d, 6)),
                "cgm_fallback_invoked": False,
                "serial_diagnostic": {
                    "rho_1": 0.0, "rho_adj": 0.0, "rho_2": 0.0,
                    "bartlett_threshold": 1.0, "fallback_triggered": False,
                    "newey_west_se": 0.0, "week_means": [],
                },
            }

        counts = cls.get_structural_counts(dataset)
        cgm_res = cls.compute_cgm_two_way_variance(dataset, field=field)
        dep_res = cls.evaluate_inter_block_serial_dependence(dataset, field=field)

        M = counts["M_tokens"]
        B = counts["B_weeks"]

        df = max(1, min(M - 1, B - 1))
        t_crit = cls.get_exact_students_t_crit(df)

        # Status determination: Confirmatory status is forbidden; all results are explicitly exploratory
        if not dep_res.get("is_valid_format", True):
            status = InferentialStatus.EXPLORATORY_UNCALIBRATED_INFERENCE
            diagnostic_flag = f"INVALID_OR_MIXED_WEEK_FORMAT_CANNOT_ESTIMATE_DEPENDENCE: {dep_res.get('diagnostic_reason', '')}"
            se_effective = cgm_res["se_reported"]
        elif dep_res["fallback_triggered"]:
            status = InferentialStatus.DEPENDENT_BLOCK_FALLBACK_TRIGGERED
            diagnostic_flag = "SERIAL_PERSISTENCE_DETECTED_NEWEY_WEST_FALLBACK"
            se_effective = max(cgm_res["se_reported"], dep_res["newey_west_se"])
        else:
            status = InferentialStatus.EXPLORATORY_UNCALIBRATED_INFERENCE
            diagnostic_flag = "EXPLORATORY_FINITE_SAMPLE_SCREENS_DO_NOT_ESTABLISH_ASYMPTOTIC_INDEPENDENCE"
            se_effective = cgm_res["se_reported"]

        ci_lower = cgm_res["mean_d"] - t_crit * se_effective
        ci_upper = cgm_res["mean_d"] + t_crit * se_effective

        return {
            "status": status.value,
            "diagnostic_flag": diagnostic_flag,
            "structural_counts": counts,
            "degrees_of_freedom": df,
            "t_critical_value": t_crit,
            "mean_treatment_effect": round(cgm_res["mean_d"], 6),
            "se_effective": round(se_effective, 6),
            "se_cgm_two_way": round(cgm_res["se_reported"], 6),
            "se_newey_west": round(dep_res["newey_west_se"], 6),
            "ci_95": (round(ci_lower, 6), round(ci_upper, 6)),
            "cgm_fallback_invoked": cgm_res["fallback_invoked"],
            "serial_diagnostic": dep_res,
        }
