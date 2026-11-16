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
import hmac
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    from scripts.consensus_proof_verifier import (
        WormholeGuardianSet,
        CANONICAL_PRODUCTION_GUARDIAN_SETS,
        DEV_HARDHAT_ANVIL_KEYS,
        compute_sync_committee_root,
        keccak256,
        verify_mpt_proof,
        AccountStateLeaf,
        UniswapV2Slot3Decoder,
        MPTVerificationResult,
        rlp_encode,
        rlp_encode_bytes,
        rlp_decode,
        encode_compact_path,
        compute_uniswap_v2_pair_address,
        HeaderConsensusEvidence,
        HeaderConsensusVerifier,
        UNISWAP_V2_FACTORY_MAINNET,
        secp256k1_ecrecover,
        parse_wormhole_vaa,
        verify_wormhole_vaa,
        GINDEX_EXECUTION_PAYLOAD_STATE_ROOT,
        GINDEX_FINALIZED_CHECKPOINT_ROOT,
    )
except ImportError:
    from consensus_proof_verifier import (
        WormholeGuardianSet,
        CANONICAL_PRODUCTION_GUARDIAN_SETS,
        DEV_HARDHAT_ANVIL_KEYS,
        compute_sync_committee_root,
        keccak256,
        verify_mpt_proof,
        AccountStateLeaf,
        UniswapV2Slot3Decoder,
        MPTVerificationResult,
        rlp_encode,
        rlp_encode_bytes,
        rlp_decode,
        encode_compact_path,
        compute_uniswap_v2_pair_address,
        HeaderConsensusEvidence,
        HeaderConsensusVerifier,
        UNISWAP_V2_FACTORY_MAINNET,
        secp256k1_ecrecover,
        parse_wormhole_vaa,
        verify_wormhole_vaa,
        GINDEX_EXECUTION_PAYLOAD_STATE_ROOT,
        GINDEX_FINALIZED_CHECKPOINT_ROOT,
    )

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

TRUSTED_VALUATION_SOURCES: Set[str] = {
    "CHAINLINK_ORACLE",
    "UNISWAP_V3_TWAP",
    "PYTH_NETWORK",
}


def get_actual_git_state(base_dir: Optional[Path] = None) -> Tuple[Optional[str], Optional[bool]]:
    """Inspects actual git state (HEAD SHA and working tree cleanliness) via git CLI.
    Returns (head_sha, is_clean). If git is unavailable or not in a git repo, returns (None, None).
    Never silently falls back to a different working directory.
    """
    try:
        cwd = str(base_dir) if base_dir else os.getcwd()
        res_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if res_sha.returncode != 0:
            return None, None
        head_sha = res_sha.stdout.strip()

        res_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if res_status.returncode != 0:
            return head_sha, None
        is_clean = len(res_status.stdout.strip()) == 0
        return head_sha, is_clean
    except Exception:
        return None, None


def derive_actual_runtime_dependencies() -> Dict[str, str]:
    """Derives complete runtime dependency inventory from actual installed packages via importlib.metadata.
    Strictly fails closed if enumeration fails or raises an exception.
    """
    import importlib.metadata
    deps: Dict[str, str] = {}
    enumeration_succeeded = True
    try:
        for dist in importlib.metadata.distributions():
            name = dist.metadata.get("Name")
            if name:
                deps[name.lower()] = dist.version
    except Exception:
        enumeration_succeeded = False

    deps["python"] = sys.version.split()[0]
    pkg_count = len([k for k in deps.keys() if k not in {"python", "inventory_status"}])
    if enumeration_succeeded and pkg_count >= 2:
        deps["inventory_status"] = "COMPLETE"
    else:
        deps["inventory_status"] = "INCOMPLETE"
    return deps


def derive_reproducible_environment_info(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Captures reproducible environment information, distinguishing installed-package inventory
    from a complete reproducible dependency lock (e.g. requirements.txt or poetry.lock).
    """
    root = base_dir or Path(os.getcwd())
    lock_candidates = [
        ("requirements.txt", "PIP_REQUIREMENTS_LOCK"),
        ("Pipfile.lock", "PIPENV_LOCK"),
        ("poetry.lock", "POETRY_LOCK"),
        ("pyproject.toml", "PYPROJECT_TOML"),
    ]
    lock_info: Dict[str, Any] = {
        "lock_type": "UNPINNED_ENVIRONMENT",
        "lock_file": None,
        "lock_sha256": None,
        "is_reproducible_lock": False,
        "environment_reproducibility": "INSTALLED_INVENTORY_ONLY_NOT_REPRODUCIBLE_LOCK",
    }
    for fname, ltype in lock_candidates:
        candidate_path = root / fname
        if candidate_path.is_file():
            lock_info["lock_type"] = ltype
            lock_info["lock_file"] = fname
            lock_info["lock_sha256"] = hashlib.sha256(candidate_path.read_bytes()).hexdigest().upper()
            lock_info["is_reproducible_lock"] = True
            lock_info["environment_reproducibility"] = f"PINNED_DEPENDENCY_LOCK_{ltype}"
            break
    return lock_info


@dataclasses.dataclass
class CompleteReplayManifest:
    """Verifiable, tamper-evident replay manifest capturing complete environment and assumptions."""
    git_commit_sha: str
    is_working_tree_clean: bool
    python_version: str
    dependency_versions: Dict[str, str]
    input_datasets: Dict[str, Dict[str, Any]]
    configuration_hashes: Dict[str, str]
    identity_bindings: Dict[str, Any]
    valuation_sources: Dict[str, Any]
    gas_fee_assumptions: Dict[str, Any]
    slippage_execution_assumptions: Dict[str, Any]
    source_evidence_ids: List[str]
    consumed_configurations: Optional[List[str]] = None
    reproducible_environment: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    def compute_digest(self) -> str:
        """Deterministic SHA-256 digest over canonical JSON representation."""
        canonical_str = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()

    def validate(self, base_dir: Optional[Path] = None) -> Tuple[bool, List[str]]:
        """Strictly validates all manifest metadata, file paths, SHA-256 formats and hashes,
        record counts, configuration hashes, dependency versions, and valuation sources.
        Fails closed on any discrepancy or unverifiable input.
        """
        errors = []
        hex64_pattern = re.compile(r"^[0-9a-fA-F]{64}$")
        hex40_pattern = re.compile(r"^[0-9a-fA-F]{40}$")

        # 1. Validate git commit SHA format and actual git state
        root = base_dir or Path(os.getcwd())
        if base_dir is not None:
            resolved_base = Path(base_dir).resolve()
            if not (resolved_base / ".git").exists():
                errors.append(f"INCORRECT_REPOSITORY_ROOT: base_dir '{base_dir}' is not a git repository root (missing .git)")

        actual_head, actual_clean = get_actual_git_state(root)
        if actual_head is None or actual_clean is None:
            errors.append("GIT_STATE_UNAVAILABLE: Git commit and working tree state could not be independently inspected")
        else:
            if not self.git_commit_sha or not hex40_pattern.match(self.git_commit_sha):
                errors.append(f"Invalid git_commit_sha format: {self.git_commit_sha!r}")
            elif self.git_commit_sha.lower() != actual_head.lower():
                errors.append(f"Git commit SHA mismatch: claimed {self.git_commit_sha}, actual {actual_head}")
            if self.is_working_tree_clean != actual_clean:
                errors.append(f"Git working tree clean status mismatch: claimed {self.is_working_tree_clean}, actual {actual_clean}")

        # 2. Validate python and dependency versions
        if not self.python_version or not isinstance(self.python_version, str):
            errors.append(f"Invalid python_version: {self.python_version!r}")
        if not self.dependency_versions or not isinstance(self.dependency_versions, dict):
            errors.append("dependency_versions must be a non-empty dictionary")
        else:
            if "python" not in self.dependency_versions:
                errors.append("dependency_versions missing required 'python' entry")
            pkg_keys = {k for k in self.dependency_versions.keys() if k not in {"python", "inventory_status"}}
            if len(pkg_keys) == 0:
                errors.append("DEPENDENCY_INVENTORY_INCOMPLETE: Inventory contains only Python without installed runtime dependencies")
            inv_status = self.dependency_versions.get("inventory_status")
            if inv_status == "INCOMPLETE":
                errors.append("DEPENDENCY_INVENTORY_INCOMPLETE: strict replay requires COMPLETE inventory")
            elif inv_status is not None and inv_status != "COMPLETE":
                errors.append(f"Invalid dependency inventory_status: {inv_status!r}")

            import importlib.metadata
            for pkg_name, claimed_ver in self.dependency_versions.items():
                if pkg_name in {"python", "inventory_status"}:
                    continue
                try:
                    actual_ver = importlib.metadata.version(pkg_name)
                    if claimed_ver != actual_ver:
                        errors.append(f"Dependency '{pkg_name}' version mismatch: claimed {claimed_ver}, installed {actual_ver}")
                except importlib.metadata.PackageNotFoundError:
                    errors.append(f"Dependency '{pkg_name}' not installed in runtime environment")
                except Exception as e:
                    errors.append(f"Dependency '{pkg_name}' inspection error: {e}")

        # 3. Validate input datasets (dataset_a is required)
        if not self.input_datasets or not isinstance(self.input_datasets, dict):
            errors.append("input_datasets must be a non-empty dictionary")
        else:
            if "dataset_a" not in self.input_datasets:
                errors.append("input_datasets missing required 'dataset_a'")

            root = base_dir or Path(os.getcwd())
            for d_name, d_meta in self.input_datasets.items():
                if not isinstance(d_meta, dict):
                    errors.append(f"Dataset '{d_name}' metadata must be a dictionary")
                    continue
                d_path_str = d_meta.get("path")
                d_sha = d_meta.get("sha256")
                d_count = d_meta.get("record_count") if "record_count" in d_meta else d_meta.get("count")

                # Validate SHA-256 format
                if not d_sha or not isinstance(d_sha, str) or not hex64_pattern.match(d_sha):
                    errors.append(f"Dataset '{d_name}' has invalid SHA-256 format: {d_sha!r}")

                # Validate record count format
                if d_count is None or type(d_count) is not int or d_count < 0:
                    errors.append(f"Dataset '{d_name}' has invalid record_count: {d_count!r}")

                # Validate actual file on disk
                if not d_path_str:
                    errors.append(f"Dataset '{d_name}' missing path")
                else:
                    d_path = Path(d_path_str)
                    if not d_path.is_absolute():
                        d_path = root / d_path
                    if not d_path.is_file():
                        errors.append(f"Dataset '{d_name}' file not found: {d_path}")
                    else:
                        with open(d_path, "rb") as f:
                            actual_bytes = f.read()
                        actual_sha = hashlib.sha256(actual_bytes).hexdigest().upper()
                        if d_sha and actual_sha != d_sha.upper():
                            errors.append(f"Dataset '{d_name}' SHA-256 mismatch: claimed {d_sha}, actual {actual_sha}")

                        # Validate actual record count
                        actual_count = 0
                        try:
                            text_content = actual_bytes.decode("utf-8")
                            try:
                                json_obj = json.loads(text_content)
                                if isinstance(json_obj, list):
                                    actual_count = len(json_obj)
                                elif isinstance(json_obj, dict):
                                    if "positions" in json_obj and isinstance(json_obj["positions"], list):
                                        actual_count = len(json_obj["positions"])
                                    elif "records" in json_obj and isinstance(json_obj["records"], list):
                                        actual_count = len(json_obj["records"])
                                    else:
                                        actual_count = len(json_obj)
                            except json.JSONDecodeError:
                                actual_count = sum(1 for line in text_content.splitlines() if line.strip())
                        except Exception as e:
                            errors.append(f"Dataset '{d_name}' failed record count parse: {e}")

                        if d_count is not None and actual_count != d_count:
                            errors.append(f"Dataset '{d_name}' record count mismatch: claimed {d_count}, actual {actual_count}")

        # 4. Validate configuration hashes and verify on-disk files if present
        if not self.configuration_hashes or not isinstance(self.configuration_hashes, dict):
            errors.append("configuration_hashes must be a non-empty dictionary")
        else:
            root = base_dir or Path(os.getcwd())
            for c_name, c_hash in self.configuration_hashes.items():
                if not c_hash or not isinstance(c_hash, str) or not hex64_pattern.match(c_hash):
                    errors.append(f"Configuration '{c_name}' has invalid SHA-256 format: {c_hash!r}")
                else:
                    cfg_file = Path(c_name)
                    if not cfg_file.is_absolute():
                        cfg_file = root / cfg_file
                    if cfg_file.is_file():
                        with open(cfg_file, "rb") as f:
                            actual_cfg_sha = hashlib.sha256(f.read()).hexdigest().upper()
                        if actual_cfg_sha != c_hash.upper():
                            errors.append(f"Configuration '{c_name}' SHA-256 mismatch: claimed {c_hash}, actual {actual_cfg_sha}")
                    elif c_name.endswith(".json") or c_name.endswith(".yaml") or c_name.endswith(".toml") or "/" in c_name or "\\" in c_name:
                        errors.append(f"Configuration file not found on disk: {c_name}")

            # Validate explicitly consumed configurations if provided
            if self.consumed_configurations is not None:
                for req_cfg in self.consumed_configurations:
                    if not any(k == req_cfg or Path(k).name == Path(req_cfg).name for k in self.configuration_hashes.keys()):
                        errors.append(f"Missing replay configuration input: '{req_cfg}' consumed by replay but omitted from configuration_hashes")
                    req_path = Path(req_cfg)
                    if not req_path.is_absolute():
                        req_path = root / req_path
                    if not req_path.is_file():
                        errors.append(f"Missing replay configuration input file on disk: '{req_cfg}'")

            # Discover all configuration inputs actually consumed by the replay on disk (recursive)
            config_patterns = ["*config*.json", "*config*.yaml", "*config*.toml"]
            discovered_configs = set()
            ignored_parts = {".git", ".pytest_cache", "__pycache__", ".system_generated", "venv", "env", ".venv", "node_modules", "brain", "review_package"}
            for pat in config_patterns:
                for cfg_path in root.rglob(pat):
                    if cfg_path.is_file():
                        try:
                            rel = cfg_path.relative_to(root)
                        except ValueError:
                            continue
                        if any(part in ignored_parts or part.startswith("backup") or part.startswith(".backup") for part in rel.parts):
                            continue
                        discovered_configs.add(rel.as_posix())

            for disc_cfg in sorted(discovered_configs):
                if not any(k == disc_cfg or Path(k).name == Path(disc_cfg).name for k in self.configuration_hashes.keys()):
                    errors.append(f"Unlisted configuration file omitted from manifest: {disc_cfg}")

        # 5. Validate valuation sources against trusted registry
        if not self.valuation_sources or not isinstance(self.valuation_sources, dict):
            errors.append("valuation_sources must be a non-empty dictionary")
        else:
            for v_name, v_meta in self.valuation_sources.items():
                if not isinstance(v_meta, dict):
                    errors.append(f"Valuation source '{v_name}' must be a dictionary")
                    continue
                v_src = v_meta.get("source")
                if not v_src or not isinstance(v_src, str):
                    errors.append(f"Valuation source '{v_name}' missing exact 'source' identifier")
                elif v_src not in TRUSTED_VALUATION_SOURCES:
                    errors.append(f"Valuation source '{v_name}' has untrusted source: {v_src!r}")
                ts = v_meta.get("timestamp_utc")
                if not ts or not isinstance(ts, str):
                    errors.append(f"Valuation source '{v_name}' missing 'timestamp_utc'")
                else:
                    try:
                        parsed_ts = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if parsed_ts.tzinfo is None:
                            errors.append(f"Valuation source '{v_name}' timestamp is timezone-naive")
                    except Exception:
                        errors.append(f"Valuation source '{v_name}' has invalid timestamp format: {ts!r}")

        # 6. Validate source evidence IDs
        if not self.source_evidence_ids or not isinstance(self.source_evidence_ids, list) or len(self.source_evidence_ids) == 0:
            errors.append("source_evidence_ids must be a non-empty list")

        # 7. Validate reproducible_environment and lockfile integrity if present
        if self.reproducible_environment is not None:
            if not isinstance(self.reproducible_environment, dict):
                errors.append("reproducible_environment must be a dictionary")
            else:
                lock_file = self.reproducible_environment.get("lock_file")
                claimed_sha = self.reproducible_environment.get("lock_sha256")
                is_lock = self.reproducible_environment.get("is_reproducible_lock")
                if is_lock:
                    if not lock_file:
                        errors.append("reproducible_environment claims is_reproducible_lock=True but lock_file is None")
                    if not claimed_sha:
                        errors.append("reproducible_environment claims is_reproducible_lock=True but lock_sha256 is None")
                if lock_file:
                    lf_path = Path(lock_file)
                    if not lf_path.is_absolute():
                        lf_path = root / lf_path
                    if not lf_path.is_file():
                        errors.append(f"Lockfile '{lock_file}' specified in reproducible_environment not found on disk")
                    else:
                        actual_sha = hashlib.sha256(lf_path.read_bytes()).hexdigest().upper()
                        if claimed_sha and actual_sha != claimed_sha.upper():
                            errors.append(f"Lockfile '{lock_file}' SHA-256 mismatch: claimed {claimed_sha}, actual {actual_sha}")

        return len(errors) == 0, errors


class ReplayManifestBuilder:
    """Constructs verifiable, tamper-evident replay manifests for research intelligence runs."""

    @classmethod
    def build(
        cls,
        git_commit_sha: str,
        is_working_tree_clean: bool,
        input_datasets: Dict[str, Dict[str, Any]],
        configuration_hashes: Dict[str, str],
        identity_bindings: Dict[str, Any],
        valuation_sources: Dict[str, Any],
        gas_fee_assumptions: Dict[str, Any],
        slippage_execution_assumptions: Dict[str, Any],
        source_evidence_ids: List[str],
        dependency_versions: Optional[Dict[str, str]] = None,
        python_version: Optional[str] = None,
        base_dir: Optional[Path] = None,
        reproducible_environment: Optional[Dict[str, Any]] = None,
        validate_strict: bool = True,
    ) -> CompleteReplayManifest:
        if dependency_versions is None:
            dependency_versions = derive_actual_runtime_dependencies()
        if reproducible_environment is None:
            reproducible_environment = derive_reproducible_environment_info(base_dir=base_dir)
        manifest = CompleteReplayManifest(
            git_commit_sha=git_commit_sha,
            is_working_tree_clean=is_working_tree_clean,
            python_version=python_version or sys.version.split()[0],
            dependency_versions=dependency_versions,
            input_datasets=input_datasets,
            configuration_hashes=configuration_hashes,
            identity_bindings=identity_bindings,
            valuation_sources=valuation_sources,
            gas_fee_assumptions=gas_fee_assumptions,
            slippage_execution_assumptions=slippage_execution_assumptions,
            source_evidence_ids=sorted(list(source_evidence_ids)),
            reproducible_environment=reproducible_environment,
        )
        if validate_strict:
            is_valid, errors = manifest.validate(base_dir=base_dir)
            if not is_valid:
                raise ValueError(f"CompleteReplayManifest validation failed: {'; '.join(errors)}")
        return manifest


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
        if type(record.chain_id) is not int or record.chain_id <= 0:
            raise ValueError(f"chain_id must be a positive integer, got: {record.chain_id!r}")
        if type(record.block_number) is not int or record.block_number <= 0:
            raise ValueError(f"block_number must be a positive integer, got: {record.block_number!r}")

        dest_dir = (self.evidence_dir / str(record.chain_id) / str(record.block_number)).resolve()
        evidence_root = self.evidence_dir.resolve()
        try:
            dest_dir.relative_to(evidence_root)
        except ValueError:
            raise ValueError(f"Path traversal detected in evidence directory: {dest_dir}")

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
        if type(record.chain_id) is not int or record.chain_id <= 0:
            raise ValueError(f"chain_id must be a positive integer, got: {record.chain_id!r}")
        if type(record.block_number) is not int or record.block_number <= 0:
            raise ValueError(f"block_number must be a positive integer, got: {record.block_number!r}")

        dest = (self.quarantine_dir / str(record.chain_id) / str(record.block_number)).resolve()
        quarantine_root = self.quarantine_dir.resolve()
        try:
            dest.relative_to(quarantine_root)
        except ValueError:
            raise ValueError(f"Path traversal detected in quarantine directory: {dest}")

        dest.mkdir(parents=True, exist_ok=True)
        now_dt = dt.datetime.now(dt.timezone.utc)
        reason_hash = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:8]

        manifest = {
            "reason": reason,
            "quarantined_at_utc": now_dt.isoformat(),
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

        # 1. Exclusive creation of unique immutable artifact with high-resolution nanosecond timestamp & entropy
        created_file = None
        for _ in range(10):
            timestamp_ns = time.time_ns()
            entropy = os.urandom(6).hex()
            candidate_file = dest / f"quarantine_{record.block_hash}_{timestamp_ns}_{entropy}_{reason_hash}.json"
            try:
                with open(candidate_file, "x", encoding="utf-8") as f:
                    json.dump(manifest, f, indent=2)
                created_file = candidate_file
                break
            except FileExistsError:
                continue

        if created_file is None:
            raise RuntimeError(f"Failed to exclusively create quarantine artifact in {dest}")

        # 2. Preserve prior canonical manifest; only write if it does not already exist
        canonical_manifest = dest / "quarantine_manifest.json"
        if not canonical_manifest.exists():
            try:
                with open(canonical_manifest, "x", encoding="utf-8") as f:
                    json.dump(manifest, f, indent=2)
            except FileExistsError:
                pass  # Race condition safe: existing canonical manifest is never overwritten

        # 3. Append to immutable history log
        log_file = dest / "quarantine_history.jsonl"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(manifest) + "\n")

        return dest


# ==============================================================================
# 5. Configuration-Driven Multi-Chain Finality State Machine & Trust Anchors
# ==============================================================================

BASE58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
HEX_ALPHABET = set("0123456789abcdefABCDEF")


def canonicalize_token_address(token_identifier: str, chain: str | int | None = None) -> str:
    """Extracts canonical on-chain token address:
    - EVM: lowercases and validates 0x hex format.
    - Solana: strictly preserves Base58 case and validates Base58 charset (len 32-44). Never lowercases Solana addresses.
    - Rejects malformed addresses or characters.
    """
    if not token_identifier or not isinstance(token_identifier, str):
        raise ValueError(f"Invalid token identifier: {token_identifier}")

    raw = token_identifier.strip()
    prefix = ""
    addr = raw
    if ":" in raw:
        parts = raw.split(":", 1)
        prefix = parts[0].strip().lower()
        addr = parts[1].strip()

    is_solana = False
    if chain is not None:
        chain_str = str(chain).strip().lower()
        if chain_str in {"solana", "sol", "101", "102", "103"}:
            is_solana = True
    elif prefix in {"solana", "sol"}:
        is_solana = True
    elif not addr.startswith("0x") and 32 <= len(addr) <= 44 and all(c in BASE58_ALPHABET for c in addr):
        is_solana = True

    if is_solana:
        if not (32 <= len(addr) <= 44):
            raise ValueError(f"Invalid Solana address length ({len(addr)}): {addr}")
        if not all(c in BASE58_ALPHABET for c in addr):
            raise ValueError(f"Invalid Solana Base58 character in address: {addr}")
        return addr  # Strictly preserve case!

    # EVM address
    if not addr.lower().startswith("0x"):
        raise ValueError(f"Invalid EVM address (must start with 0x): {token_identifier}")
    hex_body = addr[2:]
    if not hex_body or not all(c in HEX_ALPHABET for c in hex_body):
        raise ValueError(f"Invalid EVM address hex characters: {token_identifier}")

    return "0x" + hex_body.lower()


_POOL_METADATA_SEAL_KEY: bytes = os.urandom(32)


@dataclasses.dataclass(frozen=True)
class BlockHeaderTrustAnchor:
    """Independently authenticated block header / consensus commitment trust anchor."""
    chain_id: int
    block_number: int
    block_hash: str
    state_root: str
    header_timestamp_utc: str
    consensus_mechanism: str = "PROOF_OF_STAKE"
    parent_hash: Optional[str] = None
    receipts_root: Optional[str] = None
    finality_status: str = "FINALIZED_CANONICAL"
    trust_source: str = "ETH_CONSENSUS_SYNC_COMMITTEE"
    verification_seal: str = ""
    is_authenticated: bool = False
    consensus_evidence: Optional[HeaderConsensusEvidence] = None

    @classmethod
    def _compute_seal(cls, *args: Any, **kwargs: Any) -> str:
        """Abolished: Local HMAC seals are not treated as consensus authentication."""
        return ""

    @classmethod
    def create_unverified_anchor(
        cls,
        chain_id: int,
        block_number: int,
        block_hash: str,
        state_root: str,
        header_timestamp_utc: str,
        consensus_mechanism: str = "PROOF_OF_STAKE",
        parent_hash: Optional[str] = None,
        receipts_root: Optional[str] = None,
        finality_status: str = "BLOCK_HEADER_UNVERIFIED",
        trust_source: str = "UNVERIFIED_CALLER_ASSERTION",
    ) -> "BlockHeaderTrustAnchor":
        """Factory method returning an explicitly unverified anchor without cryptographic authentication."""
        return cls(
            chain_id=chain_id,
            block_number=block_number,
            block_hash=block_hash,
            state_root=state_root,
            header_timestamp_utc=header_timestamp_utc,
            consensus_mechanism=consensus_mechanism,
            parent_hash=parent_hash,
            receipts_root=receipts_root,
            finality_status="BLOCK_HEADER_UNVERIFIED",
            trust_source=trust_source,
            verification_seal="",
            is_authenticated=False,
            consensus_evidence=None,
        )

    @classmethod
    def from_consensus_verification(
        cls,
        evidence: HeaderConsensusEvidence,
        trusted_sync_committee_root: Optional[bytes] = None,
        chain_id: int = 1,
        header_timestamp_utc: Optional[str] = None,
    ) -> "BlockHeaderTrustAnchor":
        """Cryptographically verifies consensus evidence and issues an authenticated BlockHeaderTrustAnchor.
        If verification fails or consensus evidence is invalid or unverified, returns an explicitly unverified anchor (is_authenticated=False).
        """
        ok, err = HeaderConsensusVerifier.verify_execution_header(
            evidence=evidence,
            trusted_sync_committee_root=trusted_sync_committee_root,
        )
        b_hash_hex = "0x" + evidence.execution_block_hash.hex()
        s_root_hex = "0x" + evidence.execution_state_root.hex()
        ts_str = header_timestamp_utc or dt.datetime.fromtimestamp(
            evidence.execution_timestamp, dt.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        if not ok or err is not None:
            return cls.create_unverified_anchor(
                chain_id=chain_id,
                block_number=evidence.execution_block_number,
                block_hash=b_hash_hex,
                state_root=s_root_hex,
                header_timestamp_utc=ts_str,
                finality_status="BLOCK_HEADER_UNVERIFIED",
                trust_source=f"CONSENSUS_VERIFICATION_DOWNGRADED:{err}",
            )

        is_finalized = HeaderConsensusVerifier.is_execution_block_finalized(evidence)
        finality_status = "FINALIZED_CANONICAL" if is_finalized else "ATTESTED_UNFINALIZED"
        is_authenticated = is_finalized

        return cls(
            chain_id=chain_id,
            block_number=evidence.execution_block_number,
            block_hash=b_hash_hex,
            state_root=s_root_hex,
            header_timestamp_utc=ts_str,
            consensus_mechanism="PROOF_OF_STAKE",
            parent_hash="0x" + evidence.parent_root.hex(),
            receipts_root="0x" + evidence.execution_receipts_root.hex() if evidence.execution_receipts_root else None,
            finality_status=finality_status,
            trust_source="ETH_CONSENSUS_SYNC_COMMITTEE" if is_finalized else "ETH_CONSENSUS_ATTESTED_ONLY",
            verification_seal="",
            is_authenticated=is_authenticated,
            consensus_evidence=evidence,
        )

    @classmethod
    def verify_authenticity(cls, anchor: Any) -> bool:
        """Cryptographically verifies that an anchor was genuinely authenticated by underlying consensus evidence."""
        if not isinstance(anchor, BlockHeaderTrustAnchor):
            return False
        if not anchor.is_authenticated:
            return False
        if anchor.finality_status != "FINALIZED_CANONICAL":
            return False
        if anchor.consensus_evidence is None or not isinstance(anchor.consensus_evidence, HeaderConsensusEvidence):
            return False

        ev = anchor.consensus_evidence
        if not HeaderConsensusVerifier.is_execution_block_finalized(ev):
            return False
        ok, err = HeaderConsensusVerifier.verify_execution_header(ev)
        if not ok or err is not None:
            return False

        if anchor.block_number != ev.execution_block_number:
            return False
        if anchor.block_hash.lower() != ("0x" + ev.execution_block_hash.hex().lower()):
            return False
        if anchor.state_root.lower() != ("0x" + ev.execution_state_root.hex().lower()):
            return False
        if anchor.parent_hash and ev.parent_root and anchor.parent_hash.lower() != ("0x" + ev.parent_root.hex().lower()):
            return False
        if anchor.receipts_root and ev.execution_receipts_root and anchor.receipts_root.lower() != ("0x" + ev.execution_receipts_root.hex().lower()):
            return False

        return True


@dataclasses.dataclass(frozen=True)
class PoolMetadataTrustAnchor:
    """Independently authenticated pool metadata and storage layout anchor."""
    chain_id: int
    pool_address: str
    token0_address: str
    token1_address: str
    token0_decimals: int
    token1_decimals: int
    token0_is_quote: bool
    venue_type: str = "UNISWAP_V2"
    reserves_storage_slot: int = 3
    factory_address: str = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
    storage_layout_verified: bool = False
    is_create2_derived: bool = False
    trust_source: str = "CANONICAL_FACTORY_REGISTRY"
    verification_seal: str = ""
    is_authenticated: bool = False

    @classmethod
    def _compute_seal(
        cls,
        chain_id: int,
        pool_address: str,
        token0_address: str,
        token1_address: str,
        token0_decimals: int,
        token1_decimals: int,
        token0_is_quote: bool,
        venue_type: str,
        reserves_storage_slot: int,
        factory_address: str,
        trust_source: str,
        storage_layout_verified: bool,
    ) -> str:
        data = f"{chain_id}:{pool_address.lower()}:{token0_address.lower()}:{token1_address.lower()}:{token0_decimals}:{token1_decimals}:{token0_is_quote}:{venue_type}:{reserves_storage_slot}:{factory_address.lower()}:{trust_source}:{storage_layout_verified}"
        return hmac.new(_POOL_METADATA_SEAL_KEY, data.encode("utf-8"), hashlib.sha256).hexdigest()

    @classmethod
    def create_unverified_metadata(
        cls,
        chain_id: int,
        pool_address: str,
        token0_address: str,
        token1_address: str,
        token0_decimals: int,
        token1_decimals: int,
        token0_is_quote: bool,
        venue_type: str = "UNISWAP_V2",
        reserves_storage_slot: int = 3,
        factory_address: str = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        trust_source: str = "UNVERIFIED_CALLER_METADATA",
    ) -> "PoolMetadataTrustAnchor":
        """Factory method returning unverified pool metadata."""
        return cls(
            chain_id=chain_id,
            pool_address=canonicalize_token_address(pool_address),
            token0_address=canonicalize_token_address(token0_address),
            token1_address=canonicalize_token_address(token1_address),
            token0_decimals=token0_decimals,
            token1_decimals=token1_decimals,
            token0_is_quote=token0_is_quote,
            venue_type=venue_type,
            reserves_storage_slot=reserves_storage_slot,
            factory_address=canonicalize_token_address(factory_address),
            storage_layout_verified=False,
            is_create2_derived=False,
            trust_source=trust_source,
            verification_seal="",
            is_authenticated=False,
        )

    @classmethod
    def verify_authenticity(cls, meta: Any) -> bool:
        """Cryptographically verifies pool metadata and storage layout verification seal."""
        if not isinstance(meta, PoolMetadataTrustAnchor):
            return False
        if not meta.is_authenticated or not meta.verification_seal or not meta.storage_layout_verified:
            return False
        expected_seal = cls._compute_seal(
            chain_id=meta.chain_id,
            pool_address=meta.pool_address,
            token0_address=meta.token0_address,
            token1_address=meta.token1_address,
            token0_decimals=meta.token0_decimals,
            token1_decimals=meta.token1_decimals,
            token0_is_quote=meta.token0_is_quote,
            venue_type=meta.venue_type,
            reserves_storage_slot=meta.reserves_storage_slot,
            factory_address=meta.factory_address,
            trust_source=meta.trust_source,
            storage_layout_verified=meta.storage_layout_verified,
        )
        return hmac.compare_digest(meta.verification_seal, expected_seal)


CANONICAL_V2_PAIR_RUNTIME_CODE_HASHES: Dict[int, bytes] = {
    1: bytes.fromhex("c46b1ad77c95c808800ba84050a4985cfb14ff26046e7f8fbcfcc3c1e3b5e40c"),
    11155111: bytes.fromhex("c46b1ad77c95c808800ba84050a4985cfb14ff26046e7f8fbcfcc3c1e3b5e40c"),
}

UNISWAP_V3_POOL_RUNTIME_CODE_HASH: bytes = bytes.fromhex(
    "a598dd2fba360510c5a8f02f44423a4468e902df5857dbce3ca162a43a3a31ff"
)

CANONICAL_CHAINLINK_CODE_HASHES: Dict[int, bytes] = {
    1: bytes.fromhex("3479008bc5b5ad9e02ef2c2c04ae5ce5c249bc8558eb80dd94488b0ea77f52f3"),
    11155111: bytes.fromhex("3479008bc5b5ad9e02ef2c2c04ae5ce5c249bc8558eb80dd94488b0ea77f52f3"),
}

CANONICAL_PYTH_EMITTERS: Set[Tuple[int, bytes]] = {
    # Wormhole chain 26: Pythnet authoritative Wormhole emitter contract
    (26, bytes.fromhex("e101424e8655b23739dd0d4e032534378012a86bbfa44002f4b4b1984479b45d")),
    # Wormhole chain 1: Solana Pyth authoritative Wormhole emitter program
    (1, bytes.fromhex("71ff630fd034a3a0c70088f0a39229f6bfa53f0188a87d66c3042c8ebfbc502f")),
}

CANONICAL_WORMHOLE_GUARDIANS: Dict[int, List[str]] = {
    1: list(CANONICAL_PRODUCTION_GUARDIAN_SETS[3].guardians),
    11155111: list(CANONICAL_PRODUCTION_GUARDIAN_SETS[3].guardians),
}


CANONICAL_V2_FACTORIES: Dict[int, Dict[str, Any]] = {
    1: {  # Ethereum Mainnet Uniswap V2
        "factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f".lower(),
        "init_code_hash": bytes.fromhex("96e8ac4277198ff8b6f785478aa9a39f403cb768dd02cbee326c3e7da348845f"),
        "venue": "UNISWAP_V2",
        "reserves_storage_slot": 3,
    },
    11155111: {  # Sepolia Uniswap V2
        "factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f".lower(),
        "init_code_hash": bytes.fromhex("96e8ac4277198ff8b6f785478aa9a39f403cb768dd02cbee326c3e7da348845f"),
        "venue": "UNISWAP_V2",
        "reserves_storage_slot": 3,
    },
}

CANONICAL_AUTHENTICATED_TOKENS: Dict[int, Dict[str, Dict[str, Any]]] = {
    1: {  # Ethereum Mainnet
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": {"symbol": "WETH", "decimals": 18, "is_quote": True},
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": {"symbol": "USDC", "decimals": 6, "is_quote": True},
        "0xdac17f958d2ee523a2206206994597c13d831ec7": {"symbol": "USDT", "decimals": 6, "is_quote": True},
        "0x6b175474e89094c44da98b954eedeac495271d0f": {"symbol": "DAI", "decimals": 18, "is_quote": True},
    },
    11155111: {  # Sepolia
        "0x7b79995e5f793a07bc00c21412e50ecae098e7f9": {"symbol": "WETH", "decimals": 18, "is_quote": True},
    },
    8453: {  # Base
        "0x4200000000000000000000000000000000000006": {"symbol": "WETH", "decimals": 18, "is_quote": True},
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": {"symbol": "USDC", "decimals": 6, "is_quote": True},
    },
}


class PoolMetadataVerifier:
    """Independently verifies pool metadata, token ordering, CREATE2 pair address derivation, and storage layout."""

    @classmethod
    def verify_pool_metadata(
        cls,
        chain_id: int,
        pool_address: str,
        token0_address: str,
        token1_address: str,
        token0_decimals: int,
        token1_decimals: int,
        token0_is_quote: bool,
        factory_address: Optional[str] = None,
        reserves_storage_slot: int = 3,
        venue_type: str = "UNISWAP_V2",
        block_trust_anchor: Optional[BlockHeaderTrustAnchor] = None,
        pool_account_proof: Optional[list[bytes]] = None,
        pool_code_proof: Optional[bytes | list[bytes]] = None,
        state_proofs: Any = None,
    ) -> PoolMetadataTrustAnchor:
        """Independently validates and seals PoolMetadataTrustAnchor using CREATE2 derivation,
        MPT account proof of canonical deployed runtime code hash, and canonical token validation."""
        if chain_id not in CANONICAL_V2_FACTORIES:
            raise ValueError(f"Unsupported chain_id for canonical pool metadata verification: {chain_id}")

        canonical_deployment = CANONICAL_V2_FACTORIES[chain_id]
        expected_factory = canonical_deployment["factory"]
        init_code = canonical_deployment["init_code_hash"]

        canon_pool = canonicalize_token_address(pool_address)
        canon_t0 = canonicalize_token_address(token0_address)
        canon_t1 = canonicalize_token_address(token1_address)

        if factory_address is not None:
            canon_fac = canonicalize_token_address(factory_address)
            if canon_fac.lower() != expected_factory.lower():
                raise ValueError(f"Fabricated factory address: expected {expected_factory}, got {factory_address}")
        else:
            canon_fac = expected_factory

        if venue_type != "UNISWAP_V2":
            raise ValueError(f"Unsupported venue_type: {venue_type}")
        if reserves_storage_slot != 3:
            raise ValueError(f"Uniswap V2 reserves storage slot must be 3, got: {reserves_storage_slot}")
        if not (0 <= token0_decimals <= 36) or not (0 <= token1_decimals <= 36):
            raise ValueError("Token decimals must be between 0 and 36")

        int_t0 = int(canon_t0[2:], 16)
        int_t1 = int(canon_t1[2:], 16)
        if int_t0 >= int_t1:
            raise ValueError(
                f"Canonical token ordering violation: token0 ({canon_t0}) must be numerically less than token1 ({canon_t1})"
            )

        # Independently derive CREATE2 pair address
        computed_pair = compute_uniswap_v2_pair_address(
            factory=canon_fac,
            token0=canon_t0,
            token1=canon_t1,
            init_code_hash=init_code,
        )
        if canon_pool.lower() != computed_pair.lower():
            raise ValueError(
                f"Pool address mismatch: claimed {pool_address}, CREATE2 derived {computed_pair}"
            )

        # 1. Authenticate token decimals & quote orientation against canonical registry
        t0_auth = False
        t1_auth = False
        chain_tokens = CANONICAL_AUTHENTICATED_TOKENS.get(chain_id, {})

        if canon_t0 in chain_tokens:
            expected_t0 = chain_tokens[canon_t0]
            if token0_decimals != expected_t0["decimals"]:
                raise ValueError(f"Token0 decimals contradiction: claimed {token0_decimals}, verified {expected_t0['decimals']}")
            t0_auth = True

        if canon_t1 in chain_tokens:
            expected_t1 = chain_tokens[canon_t1]
            if token1_decimals != expected_t1["decimals"]:
                raise ValueError(f"Token1 decimals contradiction: claimed {token1_decimals}, verified {expected_t1['decimals']}")
            t1_auth = True

        # 2. Deployed runtime code authentication strictly via MPT account proof
        # Caller-supplied code hash or state dicts are strictly rejected without genuine MPT proof
        code_auth = False
        expected_runtime_code_hash = CANONICAL_V2_PAIR_RUNTIME_CODE_HASHES.get(chain_id)

        mpt_nodes = pool_account_proof if pool_account_proof is not None else (pool_code_proof if isinstance(pool_code_proof, list) else None)

        if (
            block_trust_anchor is not None
            and BlockHeaderTrustAnchor.verify_authenticity(block_trust_anchor)
            and mpt_nodes is not None
            and len(mpt_nodes) > 0
            and expected_runtime_code_hash is not None
        ):
            s_root = bytes.fromhex(block_trust_anchor.state_root[2:] if block_trust_anchor.state_root.startswith("0x") else block_trust_anchor.state_root)
            pool_key = keccak256(bytes.fromhex(canon_pool[2:]))
            acc_res = verify_mpt_proof(root_hash=s_root, search_key_bytes=pool_key, proof_nodes=mpt_nodes)
            if acc_res.is_valid and acc_res.is_included and acc_res.value_bytes:
                try:
                    acc_fields = rlp_decode(acc_res.value_bytes)
                    if isinstance(acc_fields, list) and len(acc_fields) >= 4:
                        deployed_code_hash = acc_fields[3]
                        if deployed_code_hash == expected_runtime_code_hash:
                            code_auth = True
                except Exception:
                    code_auth = False

        # If any element lacks independent cryptographic verification, retain modeled unverified status
        if not code_auth or not t0_auth or not t1_auth:
            return PoolMetadataTrustAnchor(
                chain_id=chain_id,
                pool_address=canon_pool,
                token0_address=canon_t0,
                token1_address=canon_t1,
                token0_decimals=token0_decimals,
                token1_decimals=token1_decimals,
                token0_is_quote=token0_is_quote,
                venue_type=venue_type,
                reserves_storage_slot=reserves_storage_slot,
                factory_address=canon_fac,
                storage_layout_verified=False,
                is_create2_derived=True,
                trust_source="MODELED_UNVERIFIED_POOL_METADATA",
                verification_seal="",
                is_authenticated=False,
            )

        seal = PoolMetadataTrustAnchor._compute_seal(
            chain_id=chain_id,
            pool_address=canon_pool,
            token0_address=canon_t0,
            token1_address=canon_t1,
            token0_decimals=token0_decimals,
            token1_decimals=token1_decimals,
            token0_is_quote=token0_is_quote,
            venue_type=venue_type,
            reserves_storage_slot=reserves_storage_slot,
            factory_address=canon_fac,
            trust_source="INDEPENDENTLY_AUTHENTICATED_POOL_METADATA",
            storage_layout_verified=True,
        )

        return PoolMetadataTrustAnchor(
            chain_id=chain_id,
            pool_address=canon_pool,
            token0_address=canon_t0,
            token1_address=canon_t1,
            token0_decimals=token0_decimals,
            token1_decimals=token1_decimals,
            token0_is_quote=token0_is_quote,
            venue_type=venue_type,
            reserves_storage_slot=reserves_storage_slot,
            factory_address=canon_fac,
            storage_layout_verified=True,
            is_create2_derived=True,
            trust_source="INDEPENDENTLY_AUTHENTICATED_POOL_METADATA",
            verification_seal=seal,
            is_authenticated=True,
        )


class BlockHeaderTrustAnchorRegistry:
    """Process-level canonical registry for independently authenticated block header trust anchors."""
    _anchors: Dict[Tuple[int, int, str], BlockHeaderTrustAnchor] = {}

    @classmethod
    def register(cls, anchor: BlockHeaderTrustAnchor) -> None:
        if not BlockHeaderTrustAnchor.verify_authenticity(anchor):
            raise ValueError("Cannot register unauthenticated block header trust anchor: failed cryptographic authenticity verification")
        key = (anchor.chain_id, anchor.block_number, anchor.block_hash.lower())
        cls._anchors[key] = anchor

    @classmethod
    def get(cls, chain_id: int, block_number: int, block_hash: str) -> Optional[BlockHeaderTrustAnchor]:
        return cls._anchors.get((chain_id, block_number, block_hash.lower()))

    @classmethod
    def clear(cls) -> None:
        cls._anchors.clear()


class PoolMetadataTrustAnchorRegistry:
    """Process-level canonical registry for independently authenticated pool metadata."""
    _metadata: Dict[Tuple[int, str], PoolMetadataTrustAnchor] = {}

    @classmethod
    def register(cls, metadata: PoolMetadataTrustAnchor) -> None:
        if not PoolMetadataTrustAnchor.verify_authenticity(metadata):
            raise ValueError("Cannot register unauthenticated pool metadata trust anchor: failed cryptographic authenticity verification")
        key = (metadata.chain_id, canonicalize_token_address(metadata.pool_address))
        cls._metadata[key] = metadata

    @classmethod
    def get(cls, chain_id: int, pool_address: str) -> Optional[PoolMetadataTrustAnchor]:
        try:
            canon = canonicalize_token_address(pool_address)
            return cls._metadata.get((chain_id, canon))
        except Exception:
            return None

    @classmethod
    def clear(cls) -> None:
        cls._metadata.clear()


OP_STACK_TRUSTED_L1_DEPLOYMENTS: Dict[int, Dict[str, Any]] = {
    8453: {  # Base
        "l1_chain_id": 1,
        "oracle_address": "0x56315b90c40730925ec54a5a02b662791cb298f5",
        "dispute_game_factory": "0x43edb88c4b80fdd2ceee3949752eabe26df56079",
        "canonical_output_slot": 3,
        "contract_name": "L2OutputOracle",
    },
    10: {    # OP Mainnet
        "l1_chain_id": 1,
        "oracle_address": "0xdfe97868233d1aa22e815a266982f2cf17685a27",
        "dispute_game_factory": "0x0c450125bf4ff25cc5cb4e6a0d2f83733077e641",
        "canonical_output_slot": 3,
        "contract_name": "L2OutputOracle",
    },
    11155420: { # OP Sepolia
        "l1_chain_id": 11155111,
        "oracle_address": "0x90e9c4f8a994a250f6aEdf66130B09405776077b",
        "dispute_game_factory": "0x05f96151cb2203650501e5cfde60114561540a3e",
        "canonical_output_slot": 3,
        "contract_name": "L2OutputOracle",
    },
}


@dataclasses.dataclass
class OpStackOutputProofPayload:
    """Raw cryptographic output-proof payload for OP Stack Bedrock / DisputeGame withdrawal."""
    withdrawal_hash: str
    l2_output_index: int
    l2_block_number: int
    l2_block_hash: str
    l2_state_root: str
    l2_message_passer_storage_root: str
    l2_message_passer_proof: List[bytes]  # MANDATORY: inclusion proof of withdrawal_hash
    l1_oracle_address: str
    l1_block_number: int
    l1_block_hash: str
    l1_oracle_account_proof: List[bytes]
    l1_output_storage_proof: List[bytes]
    l2_chain_id: int = 8453
    version_byte: bytes = b"\x00"
    l1_output_slot: Optional[int] = None
    l1_oracle_output_root: Optional[str] = None


@dataclasses.dataclass
class OpStackAuthenticatedProofResult:
    """Non-forgeable result sealed by OpStackOutputProofVerifier."""
    withdrawal_hash: str
    l2_output_index: int
    l2_block_number: int
    output_root: str
    l1_oracle_output_root: str
    is_valid: bool
    proof_type: str = "OP_STACK_AUTHENTICATED_OUTPUT_PROOF"
    verification_seal: str = ""


@dataclasses.dataclass
class OpStackOutputProofEvidence:
    """Caller-constructible output-proof evidence for OP Stack (unauthenticated unless sealed)."""
    withdrawal_hash: str
    l2_output_index: int
    l2_block_number: int
    output_root: str
    l1_oracle_output_root: str
    is_valid: bool
    proof_type: str = "OP_STACK_OUTPUT_PROPOSAL_PROOF"


class OpStackOutputProofVerifier:
    """Cryptographic output-proof verifier for OP Stack Bedrock withdrawals.

    Verifies output root composition:
        keccak256(version_byte || state_root || message_passer_storage_root || block_hash)
    and optional L2ToL1MessagePasser inclusion proof.
    Issues a tamper-evident OpStackAuthenticatedProofResult protected by a process-isolated HMAC seal.
    Caller-constructed dataclasses or unsealed results are strictly rejected.
    """
    _SEAL_KEY: bytes = os.urandom(32)

    @classmethod
    def _compute_seal(
        cls,
        withdrawal_hash: str,
        l2_output_index: int,
        l2_block_number: int,
        output_root: str,
        l1_oracle_output_root: str,
        is_valid: bool,
    ) -> str:
        data = f"{withdrawal_hash.lower()}:{l2_output_index}:{l2_block_number}:{output_root.lower()}:{l1_oracle_output_root.lower()}:{is_valid}"
        return hmac.new(cls._SEAL_KEY, data.encode("utf-8"), hashlib.sha256).hexdigest()

    @classmethod
    def is_authenticated_result(cls, result: Any) -> bool:
        """Verifies that the proof result carries a valid HMAC verification seal from this process."""
        if not isinstance(result, OpStackAuthenticatedProofResult):
            return False
        if not result.is_valid or not result.verification_seal:
            return False
        expected_seal = cls._compute_seal(
            withdrawal_hash=result.withdrawal_hash,
            l2_output_index=result.l2_output_index,
            l2_block_number=result.l2_block_number,
            output_root=result.output_root,
            l1_oracle_output_root=result.l1_oracle_output_root,
            is_valid=result.is_valid,
        )
        return hmac.compare_digest(result.verification_seal, expected_seal)

    @classmethod
    def verify_withdrawal_proof(
        cls,
        payload: OpStackOutputProofPayload,
        l1_trust_anchor: Optional[BlockHeaderTrustAnchor] = None,
        expected_withdrawal_hash: Optional[str] = None,
        expected_output_root: Optional[str] = None,
        l1_chain_id: int = 1,
    ) -> Tuple[bool, Optional[OpStackAuthenticatedProofResult], Optional[str]]:
        """Cryptographically verifies OP Stack output root composition, mandatory inclusion proof,
        and mandatory L1 publication proof anchored to an authenticated L1 block header.
        """
        # 1. Format checks
        if not payload.withdrawal_hash.startswith("0x") or len(payload.withdrawal_hash) != 66:
            return False, None, "INVALID_WITHDRAWAL_HASH_FORMAT"
        if not payload.l2_block_hash.startswith("0x") or len(payload.l2_block_hash) != 66:
            return False, None, "INVALID_L2_BLOCK_HASH_FORMAT"
        if not payload.l2_state_root.startswith("0x") or len(payload.l2_state_root) != 66:
            return False, None, "INVALID_L2_STATE_ROOT_FORMAT"
        if not payload.l2_message_passer_storage_root.startswith("0x") or len(payload.l2_message_passer_storage_root) != 66:
            return False, None, "INVALID_L2_MESSAGE_PASSER_STORAGE_ROOT_FORMAT"
        if payload.l2_block_number <= 0 or payload.l2_output_index < 0:
            return False, None, "INVALID_BLOCK_OR_INDEX"

        # 2. Strict version byte check (Bedrock requires 0x00)
        if payload.version_byte != b"\x00":
            return False, None, "INVALID_OP_STACK_VERSION_BYTE"

        if expected_withdrawal_hash is not None and payload.withdrawal_hash.lower() != expected_withdrawal_hash.lower():
            return False, None, "WITHDRAWAL_HASH_MISMATCH"

        # 3. Mandatory independently authenticated L1 block header trust anchor
        anchor = l1_trust_anchor or BlockHeaderTrustAnchorRegistry.get(
            l1_chain_id, payload.l1_block_number, payload.l1_block_hash
        )
        if anchor is None or not BlockHeaderTrustAnchor.verify_authenticity(anchor):
            return False, None, "UNAUTHENTICATED_L1_BLOCK_HEADER_TRUST_ANCHOR_REQUIRED"

        deployment = OP_STACK_TRUSTED_L1_DEPLOYMENTS.get(payload.l2_chain_id)
        if deployment is None:
            return False, None, f"UNSUPPORTED_OP_STACK_L2_CHAIN_{payload.l2_chain_id}"
        if anchor.chain_id != deployment["l1_chain_id"]:
            return False, None, f"L1_TRUST_ANCHOR_CHAIN_MISMATCH_EXPECTED_{deployment['l1_chain_id']}_GOT_{anchor.chain_id}"
        if anchor.block_number != payload.l1_block_number:
            return False, None, f"L1_TRUST_ANCHOR_BLOCK_NUMBER_MISMATCH_{anchor.block_number}_VS_{payload.l1_block_number}"
        if anchor.block_hash.lower() != payload.l1_block_hash.lower():
            return False, None, "L1_TRUST_ANCHOR_BLOCK_HASH_MISMATCH"

        # Reject caller-selected oracle addresses and arbitrary storage slots
        expected_oracle = canonicalize_token_address(deployment["oracle_address"])
        try:
            actual_oracle = canonicalize_token_address(payload.l1_oracle_address)
        except Exception:
            return False, None, f"INVALID_L1_ORACLE_ADDRESS_FORMAT_{payload.l1_oracle_address}"
        if actual_oracle != expected_oracle:
            return False, None, f"REJECTED_CALLER_SELECTED_ORACLE_ADDRESS_{payload.l1_oracle_address}"

        canonical_slot = deployment["canonical_output_slot"]
        if payload.l1_output_slot is not None and payload.l1_output_slot != canonical_slot:
            return False, None, f"REJECTED_ARBITRARY_STORAGE_SLOT_{payload.l1_output_slot}"

        # 4. Mandatory L2 message passer MPT inclusion proof
        if not payload.l2_message_passer_proof or not isinstance(payload.l2_message_passer_proof, list) or len(payload.l2_message_passer_proof) == 0:
            return False, None, "MANDATORY_L2_MESSAGE_PASSER_PROOF_MISSING"

        try:
            mp_root = bytes.fromhex(payload.l2_message_passer_storage_root[2:])
            w_bytes = bytes.fromhex(payload.withdrawal_hash[2:])
            slot0_bytes = (0).to_bytes(32, "big")
            search_key = keccak256(w_bytes + slot0_bytes)

            mpt_res = verify_mpt_proof(
                root_hash=mp_root,
                search_key_bytes=search_key,
                proof_nodes=payload.l2_message_passer_proof,
            )
            if not mpt_res.is_valid or not mpt_res.is_included:
                return False, None, f"MESSAGE_PASSER_MPT_PROOF_INVALID: {mpt_res.error}"
        except Exception as e:
            return False, None, f"MESSAGE_PASSER_PROOF_EXCEPTION: {e}"

        # 5. Output root composition verification: keccak256(version_byte || state_root || message_passer_root || block_hash)
        try:
            s_root = bytes.fromhex(payload.l2_state_root[2:])
            b_hash = bytes.fromhex(payload.l2_block_hash[2:])
            output_root_preimage = b"\x00" + s_root + mp_root + b_hash
            computed_output_root = "0x" + keccak256(output_root_preimage).hex()
        except Exception as e:
            return False, None, f"OUTPUT_ROOT_COMPUTATION_FAILED: {e}"

        if expected_output_root is not None and computed_output_root.lower() != expected_output_root.lower():
            return False, None, f"EXPECTED_OUTPUT_ROOT_MISMATCH_{computed_output_root}_VS_{expected_output_root}"

        # 6. Mandatory L1 Oracle Account and Storage Proof Verification
        if not payload.l1_oracle_account_proof or not payload.l1_output_storage_proof:
            return False, None, "MANDATORY_L1_ORACLE_PROOF_MISSING"

        try:
            l1_state_root_bytes = bytes.fromhex(anchor.state_root[2:])
            canon_oracle = canonicalize_token_address(payload.l1_oracle_address)
            oracle_acct_key = keccak256(bytes.fromhex(canon_oracle[2:]))

            oracle_acct_res = verify_mpt_proof(
                root_hash=l1_state_root_bytes,
                search_key_bytes=oracle_acct_key,
                proof_nodes=payload.l1_oracle_account_proof,
            )
            if not oracle_acct_res.is_valid or not oracle_acct_res.is_included or not oracle_acct_res.value_bytes:
                return False, None, f"L1_ORACLE_ACCOUNT_PROOF_INVALID: {oracle_acct_res.error}"

            oracle_leaf = AccountStateLeaf.from_rlp(oracle_acct_res.value_bytes)
            if not oracle_leaf.is_valid:
                return False, None, f"L1_ORACLE_LEAF_INVALID: {oracle_leaf.error}"
            oracle_storage_root = oracle_leaf.storage_root

            # Storage key for outputRoots[l2_output_index]
            mapping_slot = canonical_slot
            storage_slot_bytes = mapping_slot.to_bytes(32, "big")
            index_bytes = payload.l2_output_index.to_bytes(32, "big")
            oracle_storage_key = keccak256(index_bytes + storage_slot_bytes)

            oracle_storage_res = verify_mpt_proof(
                root_hash=oracle_storage_root,
                search_key_bytes=oracle_storage_key,
                proof_nodes=payload.l1_output_storage_proof,
            )
            if not oracle_storage_res.is_valid or not oracle_storage_res.is_included or not oracle_storage_res.value_bytes:
                return False, None, f"L1_ORACLE_STORAGE_PROOF_INVALID: {oracle_storage_res.error}"

            val = oracle_storage_res.value_bytes
            try:
                dec = rlp_decode(val)
                if isinstance(dec, bytes):
                    val = dec
            except Exception:
                pass

            if len(val) >= 32:
                published_root = "0x" + val[:32].hex()
            else:
                published_root = "0x" + val.hex()

            if published_root.lower() != computed_output_root.lower():
                return False, None, f"L1_ORACLE_PUBLISHED_OUTPUT_ROOT_MISMATCH_{published_root}_VS_{computed_output_root}"
        except Exception as e:
            return False, None, f"L1_ORACLE_PROOF_VERIFICATION_EXCEPTION: {e}"

        if payload.l1_oracle_output_root is not None and payload.l1_oracle_output_root.lower() != published_root.lower():
            return False, None, f"COMPUTED_OUTPUT_ROOT_MISMATCH_{computed_output_root}_VS_{payload.l1_oracle_output_root}"

        seal = cls._compute_seal(
            withdrawal_hash=payload.withdrawal_hash,
            l2_output_index=payload.l2_output_index,
            l2_block_number=payload.l2_block_number,
            output_root=computed_output_root,
            l1_oracle_output_root=published_root,
            is_valid=True,
        )

        res = OpStackAuthenticatedProofResult(
            withdrawal_hash=payload.withdrawal_hash,
            l2_output_index=payload.l2_output_index,
            l2_block_number=payload.l2_block_number,
            output_root=computed_output_root,
            l1_oracle_output_root=published_root,
            is_valid=True,
            proof_type="OP_STACK_AUTHENTICATED_OUTPUT_PROOF",
            verification_seal=seal,
        )
        return True, res, None


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

    @classmethod
    def evaluate_op_stack_withdrawal(
        cls,
        proven_withdrawal_timestamp: int,
        current_l1_timestamp: int,
        proof_maturity_delay_seconds: int,
        output_proof_evidence: Optional[Any] = None,
        output_proof_payload: Optional[OpStackOutputProofPayload] = None,
        expected_withdrawal_hash: Optional[str] = None,
        expected_output_root: Optional[str] = None,
        l1_trust_anchor: Optional[BlockHeaderTrustAnchor] = None,
        **kwargs: Any,
    ) -> FinalityState:
        """Evaluates withdrawal proof maturity delay starting at provenWithdrawal.timestamp.
        Strictly rejects caller-asserted boolean flags (e.g. has_authenticated_output_proof=True).
        Requires a cryptographically authenticated OpStackAuthenticatedProofResult sealed by OpStackOutputProofVerifier
        bound to an independently authenticated L1 block header trust anchor.
        Caller timestamps alone or unauthenticated dataclasses must NEVER produce WITHDRAWAL_PROOF_MATURE_FINAL.
        Future timestamps (current_l1_timestamp < proven_withdrawal_timestamp) return FINALITY_PENDING.
        """
        if current_l1_timestamp < proven_withdrawal_timestamp:
            return FinalityState.FINALITY_PENDING

        delay_elapsed = current_l1_timestamp >= proven_withdrawal_timestamp + proof_maturity_delay_seconds

        is_proof_authenticated = False
        if output_proof_payload is not None and isinstance(output_proof_payload, OpStackOutputProofPayload):
            ok, res, _ = OpStackOutputProofVerifier.verify_withdrawal_proof(
                payload=output_proof_payload,
                l1_trust_anchor=l1_trust_anchor,
                expected_withdrawal_hash=expected_withdrawal_hash,
                expected_output_root=expected_output_root,
            )
            if ok and res is not None:
                is_proof_authenticated = True
        elif output_proof_evidence is not None:
            if OpStackOutputProofVerifier.is_authenticated_result(output_proof_evidence):
                proof = output_proof_evidence
                hash_match = (expected_withdrawal_hash is None or proof.withdrawal_hash.lower() == expected_withdrawal_hash.lower())
                root_match = (expected_output_root is None or proof.output_root.lower() == expected_output_root.lower())
                oracle_match = proof.output_root.lower() == proof.l1_oracle_output_root.lower()
                if hash_match and root_match and oracle_match:
                    is_proof_authenticated = True

        if delay_elapsed:
            if is_proof_authenticated:
                return FinalityState.WITHDRAWAL_PROOF_MATURE_FINAL
            return FinalityState.FINALITY_CALLER_ASSERTED_UNVERIFIED

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
            gross = data.get("liquidityGross") if "liquidityGross" in data else abs(data.get("liquidityNet", 0))
            if gross <= 0:
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
                            gross = self.initialized_ticks[actual_tick].get("liquidityGross")
                            if gross is not None and gross <= 0:
                                return False

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

            w_cur = (current_tick // spacing) >> 8
            if tick_idx < len(boundary_ticks):
                next_tick = boundary_ticks[tick_idx]
                next_sqrt_p = UniswapV3TickMath.get_sqrt_ratio_at_tick(next_tick)
                if zero_for_one:
                    step_target = max(next_sqrt_p, target_sqrt_price_x96)
                else:
                    step_target = min(next_sqrt_p, target_sqrt_price_x96)
                boundary_tick = next_tick
                boundary_sqrt_p = next_sqrt_p
            else:
                next_tick = None
                if zero_for_one:
                    boundary_tick = (w_cur << 8) * spacing
                    boundary_sqrt_p = UniswapV3TickMath.get_sqrt_ratio_at_tick(boundary_tick)
                    step_target = max(boundary_sqrt_p, target_sqrt_price_x96)
                else:
                    boundary_tick = (((w_cur + 1) << 8) - 1) * spacing
                    boundary_sqrt_p = UniswapV3TickMath.get_sqrt_ratio_at_tick(boundary_tick)
                    step_target = min(boundary_sqrt_p, target_sqrt_price_x96)

            step_target_tick = UniswapV3TickMath.get_tick_at_sqrt_ratio(step_target)
            t_min_check = min(current_tick, step_target_tick)
            t_max_check = max(current_tick, step_target_tick)
            w_target = (step_target_tick // spacing) >> 8

            # Verify all intervening bitmap words exist between w_cur and w_target
            w_range_min = min(w_cur, w_target)
            w_range_max = max(w_cur, w_target)
            for w in range(w_range_min, w_range_max + 1):
                if w not in evidence.tick_bitmap:
                    return False, amount_in - amount_remaining, amount_out_total, current_sqrt_price, "MODELED_DEPTH_UNRESOLVED_BITMAP_GAP"
                word_val = evidence.tick_bitmap[w]
                if word_val != 0:
                    for bit_pos in range(256):
                        if (word_val & (1 << bit_pos)) != 0:
                            comp_idx = (w << 8) + bit_pos
                            act_tick = comp_idx * spacing
                            if t_min_check <= act_tick <= t_max_check:
                                if act_tick not in evidence.initialized_ticks:
                                    return False, amount_in - amount_remaining, amount_out_total, current_sqrt_price, "MODELED_DEPTH_UNRESOLVED_UNINITIALIZED_TICK"
                                gross = evidence.initialized_ticks[act_tick].get("liquidityGross") if "liquidityGross" in evidence.initialized_ticks[act_tick] else abs(evidence.initialized_ticks[act_tick].get("liquidityNet", 0))
                                if gross <= 0:
                                    return False, amount_in - amount_remaining, amount_out_total, current_sqrt_price, "MODELED_DEPTH_UNRESOLVED_UNINITIALIZED_TICK"

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
                gross = tick_data.get("liquidityGross") if "liquidityGross" in tick_data else abs(tick_data.get("liquidityNet", 0))
                if gross <= 0:
                    return False, amount_in - amount_remaining, amount_out_total, current_sqrt_price, "MODELED_DEPTH_UNRESOLVED_UNINITIALIZED_TICK"
                liquidity_net = tick_data.get("liquidityNet", 0)
                if zero_for_one:
                    current_liquidity -= liquidity_net
                    current_tick = next_tick - 1
                else:
                    current_liquidity += liquidity_net
                    current_tick = next_tick
                tick_idx += 1
            elif current_sqrt_price == step_target and next_tick is None and current_sqrt_price == boundary_sqrt_p:
                if zero_for_one:
                    current_tick = boundary_tick - 1
                else:
                    current_tick = boundary_tick + 1
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
        if not isinstance(fee_bps, int) or fee_bps < 0 or fee_bps >= 10000:
            return False, 0, 0.0, 0.0, reserve_in, reserve_out
        if (
            not isinstance(reserve_in, int)
            or not isinstance(reserve_out, int)
            or not isinstance(amount_in, int)
            or reserve_in <= 0
            or reserve_out <= 0
            or amount_in <= 0
        ):
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

        Returns:
          - On success:
              {
                  "status": "RESOLVED",
                  "d_ask_plus_2pct": d_ask,
                  "d_bid_minus_2pct": d_bid,
                  "token_in_minus_2pct": token_in,
                  "is_depth_complete": True,
                  "reason": "OK",
              }
          - On invalid reserves/fee or unreachable/zero-output boundary:
              {
                  "status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value,
                  "d_ask_plus_2pct": None,
                  "d_bid_minus_2pct": None,
                  "token_in_minus_2pct": None,
                  "is_depth_complete": False,
                  "reason": <diagnostic string>,
              }
        """
        if (
            not isinstance(reserve_quote, int)
            or not isinstance(reserve_token, int)
            or reserve_quote <= 0
            or reserve_token <= 0
        ):
            return {
                "status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value,
                "d_ask_plus_2pct": None,
                "d_bid_minus_2pct": None,
                "token_in_minus_2pct": None,
                "is_depth_complete": False,
                "reason": "RESERVES_MUST_BE_POSITIVE_INTEGERS",
            }

        if not isinstance(fee_bps, int) or fee_bps < 0 or fee_bps >= 10000:
            return {
                "status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value,
                "d_ask_plus_2pct": None,
                "d_bid_minus_2pct": None,
                "token_in_minus_2pct": None,
                "is_depth_complete": False,
                "reason": "FEE_BPS_MUST_BE_INTEGER_IN_RANGE_0_TO_9999",
            }

        # 1. Ask Depth (+2% terminal spot price boundary): search for smallest quote_in reaching boundary
        def check_ask(amount: int) -> bool:
            if amount <= 0:
                return False
            ok, out, _, _, n_rq, n_rb = cls.simulate_swap(
                reserve_in=reserve_quote,
                reserve_out=reserve_token,
                amount_in=amount,
                fee_bps=fee_bps,
            )
            return bool(ok and (100 * n_rq * reserve_token >= 102 * reserve_quote * n_rb))

        high_ask = max(1, (reserve_quote * 3) // 100)
        found_high_ask = False
        for _ in range(128):
            if check_ask(high_ask):
                found_high_ask = True
                break
            next_high = high_ask * 2
            if next_high <= high_ask:
                break
            high_ask = next_high

        if not found_high_ask:
            return {
                "status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value,
                "d_ask_plus_2pct": None,
                "d_bid_minus_2pct": None,
                "token_in_minus_2pct": None,
                "is_depth_complete": False,
                "reason": "ASK_DEPTH_NO_REACHABLE_BOUNDARY",
            }

        low_ask = 1
        while low_ask < high_ask:
            mid = (low_ask + high_ask) // 2
            if check_ask(mid):
                high_ask = mid
            else:
                low_ask = mid + 1

        if not check_ask(low_ask):
            return {
                "status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value,
                "d_ask_plus_2pct": None,
                "d_bid_minus_2pct": None,
                "token_in_minus_2pct": None,
                "is_depth_complete": False,
                "reason": "ASK_DEPTH_NO_REACHABLE_BOUNDARY",
            }
        d_ask = low_ask

        # 2. Bid Depth (-2% terminal spot price boundary): search for smallest token_in reaching boundary
        def check_bid(amount: int) -> bool:
            if amount <= 0:
                return False
            ok, out, _, _, n_rb, n_rq = cls.simulate_swap(
                reserve_in=reserve_token,
                reserve_out=reserve_quote,
                amount_in=amount,
                fee_bps=fee_bps,
            )
            return bool(ok and (100 * n_rq * reserve_token <= 98 * reserve_quote * n_rb))

        high_bid = max(1, (reserve_token * 3) // 100)
        found_high_bid = False
        for _ in range(128):
            if check_bid(high_bid):
                found_high_bid = True
                break
            next_high = high_bid * 2
            if next_high <= high_bid:
                break
            high_bid = next_high

        if not found_high_bid:
            return {
                "status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value,
                "d_ask_plus_2pct": None,
                "d_bid_minus_2pct": None,
                "token_in_minus_2pct": None,
                "is_depth_complete": False,
                "reason": "BID_DEPTH_NO_REACHABLE_BOUNDARY",
            }

        low_bid = 1
        while low_bid < high_bid:
            mid = (low_bid + high_bid) // 2
            if check_bid(mid):
                high_bid = mid
            else:
                low_bid = mid + 1

        if not check_bid(low_bid):
            return {
                "status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value,
                "d_ask_plus_2pct": None,
                "d_bid_minus_2pct": None,
                "token_in_minus_2pct": None,
                "is_depth_complete": False,
                "reason": "BID_DEPTH_NO_REACHABLE_BOUNDARY",
            }
        token_in = low_bid

        ok_bid, d_bid, _, _, _, _ = cls.simulate_swap(
            reserve_in=reserve_token,
            reserve_out=reserve_quote,
            amount_in=token_in,
            fee_bps=fee_bps,
        )
        if not ok_bid or d_bid <= 0:
            return {
                "status": DualTrackTargetStatus.MODELED_DEPTH_UNRESOLVED.value,
                "d_ask_plus_2pct": None,
                "d_bid_minus_2pct": None,
                "token_in_minus_2pct": None,
                "is_depth_complete": False,
                "reason": "BID_DEPTH_NO_REACHABLE_OUTPUT",
            }

        return {
            "status": "RESOLVED",
            "d_ask_plus_2pct": d_ask,
            "d_bid_minus_2pct": d_bid,
            "token_in_minus_2pct": token_in,
            "is_depth_complete": True,
            "reason": "OK",
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
    Returns (fee_sol, fee_usd, 'MODELED_GAS_COST_SOLANA_COMPUTE_UNITS').
    """
    fee_lamports = calculate_solana_tx_fee_lamports(
        base_signatures, compute_unit_limit, compute_unit_price_micro_lamports
    )
    fee_sol = fee_lamports / 1e9
    fee_usd = fee_sol * sol_usd_price
    return fee_sol, fee_usd, "MODELED_GAS_COST_SOLANA_COMPUTE_UNITS"


CANONICAL_VALUATION_FEEDS: Dict[int, Set[str]] = {
    1: {  # Ethereum Mainnet
        "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419",  # ETH / USD
        "0x8fffffd4afb6115b954bd326cbe7b4ba576818f6",  # USDC / USD
        "0x3e7d1eab13ad0104d2750b8863b489d65364e32d",  # USDT / USD
    },
    8453: {  # Base
        "0x71041dddad3595f9cef3dccf156597b71c782d68",  # ETH / USD
        "0x7e860098f58b3649411852513627710b2ca66624",  # USDC / USD
    },
    10: {  # OP Mainnet
        "0x13e3ee699d1909e989722e753853ae30b17e08c5",  # ETH / USD
        "0x16a9fa2fda030272a0ee55664d6ca523d79311a6",  # USDC / USD
    },
    42161: {  # Arbitrum One
        "0x639fe6ab55c921f74e7fac1ee960c0b6293ba612",  # ETH / USD
        "0x50834f3163758fcc1df9973b6e91f0f0f0434ad0",  # USDC / USD
    },
    56: {  # BSC
        "0x0567f2323251f0aab15c8dfb1967e4e8a7d42aee",  # BNB / USD
        "0x14e613ac84a31f709eadbdf89c6cc390fdc9540a",  # ETH / USD
    },
    137: {  # Polygon
        "0xab594600376ec9fd91f8e885dadf0ce036862de0",  # MATIC / USD
        "0xf9680d99d6c9589e2a93a78a04a279e509205945",  # ETH / USD
    },
    43114: {  # Avalanche
        "0x0a77230d17318075983913bc2145db16c7366156",  # AVAX / USD
        "0x976b3b0304f98a3e913307322215702a46630053",  # ETH / USD
    },
}

_VALUATION_SEAL_KEY: bytes = os.urandom(32)


@dataclasses.dataclass(frozen=True)
class ChainlinkRoundEvidence:
    """Raw on-chain Chainlink latestRoundData/getRoundData evidence."""
    feed_address: str
    round_id: int
    answer: int
    decimals: int
    started_at: int
    updated_at: int
    answered_in_round: int


@dataclasses.dataclass(frozen=True)
class PythPriceEvidence:
    """Raw on-chain Pyth price update evidence."""
    feed_id: str
    price: int
    conf: int
    expo: int
    publish_time: int


@dataclasses.dataclass(frozen=True)
class UniswapV3TwapEvidence:
    """Raw on-chain Uniswap V3 observe() tick cumulatives evidence."""
    pool_address: str
    token0_address: str
    token1_address: str
    token0_decimals: int
    token1_decimals: int
    tick_cumulatives: Tuple[int, int]
    timestamps: Tuple[int, int]


@dataclasses.dataclass(frozen=True)
class ValuationEvidenceRecord:
    """Cryptographically bound valuation evidence record."""
    source: str
    chain_id: int
    token_address: str
    price_usd: float
    timestamp_utc: str
    feed_or_pool_address: Optional[str] = None
    round_id_or_observation_index: Optional[int] = None
    observation_digest: str = ""
    verification_seal: str = ""
    is_authenticated: bool = False


class ValuationEvidenceVerifier:
    """Cryptographic verifier deriving authenticated valuation records strictly from raw oracle evidence."""

    @classmethod
    def verify_chainlink_round(
        cls,
        chain_id: int,
        token_address: str,
        evidence: ChainlinkRoundEvidence,
        block_trust_anchor: Optional[BlockHeaderTrustAnchor] = None,
        storage_proof: Optional[list[bytes]] = None,
        account_proof: Optional[list[bytes]] = None,
        proxy_storage_proof: Optional[list[bytes]] = None,
        aggregator_account_proof: Optional[list[bytes]] = None,
        aggregator_storage_proof: Optional[list[bytes]] = None,
        require_authenticated: bool = False,
    ) -> ValuationEvidenceRecord:
        """Derives valuation record from Chainlink round data.
        Mandatorily treats proxy contract and aggregator contract as separate accounts:
        1. Authenticates proxy account proof against authenticated block state root.
        2. Verifies proxy contract deployed code.
        3. Decodes aggregator address from proxy storage (slot 0) or verified aggregator proof.
        4. Authenticates aggregator account proof and verifies runtime code hash.
        5. Derives exact transmission mapping storage slot: keccak256(roundId, 43).
        6. Verifies transmission storage proof against aggregator storageRoot (never proxy storageRoot).
        7. Decodes exact contract-specific Transmission struct: int192 answer (bytes 8..32), uint64 timestamp (bytes 0..8).
        """
        if chain_id not in CANONICAL_VALUATION_FEEDS:
            raise ValueError(f"Unsupported chain_id for valuation authentication: {chain_id}")
        canon_feed = canonicalize_token_address(evidence.feed_address)
        if canon_feed not in CANONICAL_VALUATION_FEEDS[chain_id]:
            raise ValueError(f"Fabricated or unverified Chainlink feed address: {evidence.feed_address} on chain {chain_id}")
        if evidence.round_id <= 0:
            raise ValueError(f"Invalid Chainlink round_id: {evidence.round_id}")
        if evidence.answered_in_round < evidence.round_id:
            raise ValueError(f"Chainlink round not answered: {evidence.answered_in_round} < {evidence.round_id}")
        if evidence.answer <= 0:
            raise ValueError(f"Chainlink non-positive answer: {evidence.answer}")
        if not (0 <= evidence.decimals <= 18):
            raise ValueError(f"Invalid decimals: {evidence.decimals}")
        if evidence.updated_at <= 0 or evidence.started_at <= 0 or evidence.updated_at < evidence.started_at:
            raise ValueError(f"Invalid timestamps: started_at={evidence.started_at}, updated_at={evidence.updated_at}")

        derived_price = float(evidence.answer) / (10 ** evidence.decimals)
        canon_token = canonicalize_token_address(token_address)
        ts_utc = dt.datetime.fromtimestamp(evidence.updated_at, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        obs_preimage = f"{chain_id}:{canon_feed}:{canon_token}:{evidence.round_id}:{evidence.answer}:{evidence.decimals}:{evidence.updated_at}"
        obs_digest = hashlib.sha256(obs_preimage.encode("utf-8")).hexdigest()

        # Cryptographic anchor and proof verification
        has_anchor = block_trust_anchor is not None and BlockHeaderTrustAnchor.verify_authenticity(block_trust_anchor)
        eff_storage_proof = aggregator_storage_proof if aggregator_storage_proof is not None else storage_proof
        has_proofs = eff_storage_proof is not None and len(eff_storage_proof) > 0 and account_proof is not None and len(account_proof) > 0

        proof_verified = False
        if has_anchor and has_proofs:
            try:
                state_root_bytes = bytes.fromhex(
                    block_trust_anchor.state_root[2:] if block_trust_anchor.state_root.startswith("0x") else block_trust_anchor.state_root
                )
                feed_addr_bytes = bytes.fromhex(canon_feed[2:])
                feed_key = keccak256(feed_addr_bytes)

                # 1. Verify proxy account proof against block state root
                acc_res = verify_mpt_proof(root_hash=state_root_bytes, search_key_bytes=feed_key, proof_nodes=account_proof)
                if acc_res.is_valid and acc_res.is_included and acc_res.value_bytes:
                    proxy_fields = rlp_decode(acc_res.value_bytes)
                    if isinstance(proxy_fields, list) and len(proxy_fields) >= 4:
                        proxy_storage_root = proxy_fields[2]
                        proxy_code_hash = proxy_fields[3]

                        # Proxy must be a deployed contract
                        if len(proxy_storage_root) == 32 and len(proxy_code_hash) == 32 and proxy_code_hash != b"\x00" * 32:
                            # 2. Separate Aggregator Account: Proxy account proof cannot substitute for aggregator account proof
                            aggregator_sroot = None
                            if aggregator_account_proof is not None and len(aggregator_account_proof) > 0:
                                # Determine aggregator address: from proxy storage proof (slot 0) or feed address
                                agg_addr_bytes = None
                                if proxy_storage_proof is not None and len(proxy_storage_proof) > 0:
                                    slot0_key = keccak256((0).to_bytes(32, "big"))
                                    p_sres = verify_mpt_proof(root_hash=proxy_storage_root, search_key_bytes=slot0_key, proof_nodes=proxy_storage_proof)
                                    if p_sres.is_valid and p_sres.is_included and p_sres.value_bytes:
                                        slot0_val = rlp_decode(p_sres.value_bytes)
                                        if isinstance(slot0_val, bytes) and len(slot0_val) >= 20:
                                            agg_addr_bytes = slot0_val[-20:]
                                if agg_addr_bytes is None:
                                    agg_addr_bytes = feed_addr_bytes

                                agg_key = keccak256(agg_addr_bytes)
                                agg_res = verify_mpt_proof(root_hash=state_root_bytes, search_key_bytes=agg_key, proof_nodes=aggregator_account_proof)
                                if agg_res.is_valid and agg_res.is_included and agg_res.value_bytes:
                                    agg_fields = rlp_decode(agg_res.value_bytes)
                                    if isinstance(agg_fields, list) and len(agg_fields) >= 4:
                                        agg_code_hash = agg_fields[3]
                                        expected_code = CANONICAL_CHAINLINK_CODE_HASHES.get(chain_id)
                                        if expected_code is None or agg_code_hash == expected_code:
                                            aggregator_sroot = agg_fields[2]
                            elif proxy_storage_proof is None:
                                # Direct aggregator verification path where account_proof is the aggregator
                                expected_code = CANONICAL_CHAINLINK_CODE_HASHES.get(chain_id)
                                if expected_code is None or proxy_code_hash == expected_code:
                                    aggregator_sroot = proxy_storage_root

                            if aggregator_sroot is not None and len(aggregator_sroot) == 32:
                                # 3. Exact OffchainAggregator s_transmissions mapping storage slot: keccak256(roundId, 43)
                                s_key = keccak256((evidence.round_id & 0xFFFFFFFF).to_bytes(32, "big") + (43).to_bytes(32, "big"))
                                s_res = verify_mpt_proof(root_hash=aggregator_sroot, search_key_bytes=s_key, proof_nodes=eff_storage_proof)
                                if s_res.is_valid and s_res.is_included and s_res.value_bytes:
                                    val_unpacked = rlp_decode(s_res.value_bytes)
                                    if isinstance(val_unpacked, bytes) and len(val_unpacked) > 0:
                                        slot_bytes = val_unpacked.rjust(32, b"\x00")
                                        # Exact Transmission struct unpacking: int192 answer (bytes 8..32), uint64 timestamp (bytes 0..8)
                                        ans = int.from_bytes(slot_bytes[8:32], "big", signed=True)
                                        ts = int.from_bytes(slot_bytes[0:8], "big")
                                        # Strict binding: matching answer alone NEVER authenticates without exact timestamp binding
                                        if ans == evidence.answer and ts == evidence.updated_at and evidence.started_at <= evidence.updated_at:
                                            proof_verified = True
            except Exception:
                proof_verified = False

        if not proof_verified:
            if require_authenticated:
                raise ValueError("Chainlink round data lacks cryptographic MPT account/storage proof anchored to authenticated state root")
            return ValuationEvidenceRecord(
                source="CHAINLINK_ORACLE",
                chain_id=chain_id,
                token_address=canon_token,
                price_usd=derived_price,
                timestamp_utc=ts_utc,
                feed_or_pool_address=canon_feed,
                round_id_or_observation_index=evidence.round_id,
                observation_digest=obs_digest,
                verification_seal="",
                is_authenticated=False,
            )

        seal = ValuationEvidenceAuthenticator._compute_seal(
            source="CHAINLINK_ORACLE",
            chain_id=chain_id,
            token_address=canon_token,
            price_usd=derived_price,
            timestamp_utc=ts_utc,
            feed_or_pool_address=canon_feed,
            round_id_or_observation_index=evidence.round_id,
            observation_digest=obs_digest,
        )

        return ValuationEvidenceRecord(
            source="CHAINLINK_ORACLE",
            chain_id=chain_id,
            token_address=canon_token,
            price_usd=derived_price,
            timestamp_utc=ts_utc,
            feed_or_pool_address=canon_feed,
            round_id_or_observation_index=evidence.round_id,
            observation_digest=obs_digest,
            verification_seal=seal,
            is_authenticated=True,
        )

    @classmethod
    def verify_pyth_price(
        cls,
        chain_id: int,
        token_address: str,
        evidence: PythPriceEvidence,
        pyth_signature_proof: Optional[bytes] = None,
        require_authenticated: bool = False,
    ) -> ValuationEvidenceRecord:
        """Derives valuation record from Pyth price update.
        Mandatorily requires cryptographic Wormhole VM signature proof for authenticated records.
        """
        if chain_id not in CANONICAL_VALUATION_FEEDS:
            raise ValueError(f"Unsupported chain_id for valuation authentication: {chain_id}")
        canon_feed = canonicalize_token_address(evidence.feed_id) if evidence.feed_id.startswith("0x") and len(evidence.feed_id) == 42 else evidence.feed_id.lower()
        if evidence.price <= 0:
            raise ValueError(f"Pyth non-positive price: {evidence.price}")
        if evidence.conf < 0 or evidence.conf >= evidence.price:
            raise ValueError(f"Pyth confidence exceeds sanity bounds: conf={evidence.conf}, price={evidence.price}")
        if evidence.publish_time <= 0:
            raise ValueError(f"Invalid publish_time: {evidence.publish_time}")

        derived_price = float(evidence.price) * (10 ** evidence.expo)
        if derived_price <= 0 or not math.isfinite(derived_price):
            raise ValueError(f"Invalid derived price: {derived_price}")

        canon_token = canonicalize_token_address(token_address)
        ts_utc = dt.datetime.fromtimestamp(evidence.publish_time, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        obs_preimage = f"{chain_id}:{canon_feed}:{canon_token}:{evidence.price}:{evidence.expo}:{evidence.publish_time}"
        obs_digest = hashlib.sha256(obs_preimage.encode("utf-8")).hexdigest()

        # Cryptographic Pyth Wormhole signature proof check
        vaa_verified = False
        if pyth_signature_proof is not None and len(pyth_signature_proof) > 0:
            ok_vaa, vaa_err, parsed_vaa = verify_wormhole_vaa(
                raw_vaa=pyth_signature_proof,
                current_timestamp=evidence.publish_time,
            )
            if ok_vaa and parsed_vaa is not None:
                emitter_chain = parsed_vaa.get("emitter_chain")
                emitter_addr = parsed_vaa.get("emitter_address")
                is_valid_emitter = False
                if emitter_chain in (26, 1) and isinstance(emitter_addr, bytes):
                    for ec, ea in CANONICAL_PYTH_EMITTERS:
                        if ec == emitter_chain and (ea == emitter_addr or ea in emitter_addr or emitter_addr in ea):
                            is_valid_emitter = True
                            break

                if is_valid_emitter:
                    try:
                        payload = parsed_vaa["payload"]
                        if payload.startswith(b"P2WH"):
                            hdr_len = int.from_bytes(payload[6:8], "big")
                            msg_payload = payload[8 + hdr_len:]
                        else:
                            msg_payload = payload
                        if len(msg_payload) >= 61:
                            p_feed_id = "0x" + msg_payload[1:33].hex().lower()
                            p_price = int.from_bytes(msg_payload[33:41], "big", signed=True)
                            p_conf = int.from_bytes(msg_payload[41:49], "big")
                            p_expo = int.from_bytes(msg_payload[49:53], "big", signed=True)
                            p_pub_time = int.from_bytes(msg_payload[53:61], "big", signed=True)
                            if (
                                p_feed_id == canon_feed.lower()
                                and p_price == evidence.price
                                and p_conf == evidence.conf
                                and p_expo == evidence.expo
                                and p_pub_time == evidence.publish_time
                            ):
                                vaa_verified = True
                    except Exception:
                        vaa_verified = False

        if not vaa_verified:
            if require_authenticated:
                raise ValueError("Pyth price update lacks valid cryptographic Wormhole/Pyth signature proof")
            return ValuationEvidenceRecord(
                source="PYTH_NETWORK",
                chain_id=chain_id,
                token_address=canon_token,
                price_usd=derived_price,
                timestamp_utc=ts_utc,
                feed_or_pool_address=canon_feed if canon_feed.startswith("0x") and len(canon_feed) == 42 else "0x" + "00"*20,
                round_id_or_observation_index=evidence.publish_time,
                observation_digest=obs_digest,
                verification_seal="",
                is_authenticated=False,
            )

        seal = ValuationEvidenceAuthenticator._compute_seal(
            source="PYTH_NETWORK",
            chain_id=chain_id,
            token_address=canon_token,
            price_usd=derived_price,
            timestamp_utc=ts_utc,
            feed_or_pool_address=canon_feed if canon_feed.startswith("0x") and len(canon_feed) == 42 else "0x" + "00"*20,
            round_id_or_observation_index=evidence.publish_time,
            observation_digest=obs_digest,
        )

        return ValuationEvidenceRecord(
            source="PYTH_NETWORK",
            chain_id=chain_id,
            token_address=canon_token,
            price_usd=derived_price,
            timestamp_utc=ts_utc,
            feed_or_pool_address=canon_feed if canon_feed.startswith("0x") and len(canon_feed) == 42 else "0x" + "00"*20,
            round_id_or_observation_index=evidence.publish_time,
            observation_digest=obs_digest,
            verification_seal=seal,
            is_authenticated=True,
        )

    @classmethod
    def verify_uniswap_v3_twap(
        cls,
        chain_id: int,
        token_address: str,
        evidence: UniswapV3TwapEvidence,
        observation_proof_t1: Optional[list[bytes]] = None,
        observation_proof_t2: Optional[list[bytes]] = None,
        pool_account_proof_t1: Optional[list[bytes]] = None,
        pool_account_proof_t2: Optional[list[bytes]] = None,
        observation_index_t1: Optional[int] = None,
        observation_index_t2: Optional[int] = None,
        block_anchor_t1: Optional[BlockHeaderTrustAnchor] = None,
        block_anchor_t2: Optional[BlockHeaderTrustAnchor] = None,
        require_authenticated: bool = False,
    ) -> ValuationEvidenceRecord:
        """Derives valuation record from Uniswap V3 observe() tick cumulatives.
        Mandatorily implements exact Uniswap V3 observation storage layout:
        1. Authenticates pool account proofs against block state root to extract pool storageRoot.
        2. Proves observation storage slots against pool storageRoot (never global state root).
        3. Enforces valid observation ring-buffer indexes (uint16 < 65536; timestamps rejected).
        4. Derives observation mapping slot: keccak256(uint256(index) || uint256(8)).
        5. Decodes Observation struct: bool initialized (byte 0), int56 tickCumulative (bytes 21..28), uint32 blockTimestamp (bytes 28..32).
        6. Verifies exact binding of decoded timestamps and tick cumulatives at both endpoints.
        """
        if chain_id not in CANONICAL_VALUATION_FEEDS:
            raise ValueError(f"Unsupported chain_id for valuation authentication: {chain_id}")
        t1, t2 = evidence.timestamps
        if t2 - t1 < 60:
            raise ValueError(f"TWAP window too short: {t2 - t1} seconds < 60 seconds minimum")
        c1, c2 = evidence.tick_cumulatives
        avg_tick = (c2 - c1) / (t2 - t1)
        raw_price = (1.0001 ** avg_tick) * (10 ** (evidence.token0_decimals - evidence.token1_decimals))
        if raw_price <= 0 or not math.isfinite(raw_price):
            raise ValueError(f"Invalid derived TWAP price: {raw_price}")

        canon_pool = canonicalize_token_address(evidence.pool_address)
        canon_token = canonicalize_token_address(token_address)
        ts_utc = dt.datetime.fromtimestamp(t2, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        obs_preimage = f"{chain_id}:{canon_pool}:{canon_token}:{c1}:{c2}:{t1}:{t2}"
        obs_digest = hashlib.sha256(obs_preimage.encode("utf-8")).hexdigest()

        # Reject UNIX timestamps used as observation indexes (< 65536 mandatory)
        if observation_index_t1 is not None and observation_index_t1 >= 65536:
            if require_authenticated:
                raise ValueError("UNISWAP_V3_INVALID_OBSERVATION_INDEX: UNIX timestamp cannot be used as observation ring-buffer index")
            observation_index_t1 = None
        if observation_index_t2 is not None and observation_index_t2 >= 65536:
            if require_authenticated:
                raise ValueError("UNISWAP_V3_INVALID_OBSERVATION_INDEX: UNIX timestamp cannot be used as observation ring-buffer index")
            observation_index_t2 = None

        has_anchors = (
            block_anchor_t1 is not None and BlockHeaderTrustAnchor.verify_authenticity(block_anchor_t1) and
            block_anchor_t2 is not None and BlockHeaderTrustAnchor.verify_authenticity(block_anchor_t2)
        )
        has_obs_proofs = (
            observation_proof_t1 is not None and len(observation_proof_t1) > 0 and
            observation_proof_t2 is not None and len(observation_proof_t2) > 0
        )
        has_indices = (observation_index_t1 is not None and observation_index_t2 is not None)

        obs_verified = False
        if has_anchors and has_obs_proofs and has_indices:
            try:
                sr1 = bytes.fromhex(block_anchor_t1.state_root[2:] if block_anchor_t1.state_root.startswith("0x") else block_anchor_t1.state_root)
                sr2 = bytes.fromhex(block_anchor_t2.state_root[2:] if block_anchor_t2.state_root.startswith("0x") else block_anchor_t2.state_root)
                pool_key = keccak256(bytes.fromhex(canon_pool[2:]))

                # 1. Authenticate pool account proof to extract pool storage root (never use stateRoot as storage root)
                pool_sroot1 = None
                pool_sroot2 = None
                if pool_account_proof_t1 is not None and len(pool_account_proof_t1) > 0:
                    p1_acc = verify_mpt_proof(root_hash=sr1, search_key_bytes=pool_key, proof_nodes=pool_account_proof_t1)
                    if p1_acc.is_valid and p1_acc.is_included and p1_acc.value_bytes:
                        acc_f1 = rlp_decode(p1_acc.value_bytes)
                        if isinstance(acc_f1, list) and len(acc_f1) >= 4 and len(acc_f1[2]) == 32:
                            pool_sroot1 = acc_f1[2]

                if pool_account_proof_t2 is not None and len(pool_account_proof_t2) > 0:
                    p2_acc = verify_mpt_proof(root_hash=sr2, search_key_bytes=pool_key, proof_nodes=pool_account_proof_t2)
                    if p2_acc.is_valid and p2_acc.is_included and p2_acc.value_bytes:
                        acc_f2 = rlp_decode(p2_acc.value_bytes)
                        if isinstance(acc_f2, list) and len(acc_f2) >= 4 and len(acc_f2[2]) == 32:
                            pool_sroot2 = acc_f2[2]

                # 2. Verify observation storage proofs against pool storageRoot
                if pool_sroot1 is not None and pool_sroot2 is not None:
                    slot_key_t1 = keccak256((observation_index_t1).to_bytes(32, "big") + (8).to_bytes(32, "big"))
                    slot_key_t2 = keccak256((observation_index_t2).to_bytes(32, "big") + (8).to_bytes(32, "big"))

                    sres1 = verify_mpt_proof(root_hash=pool_sroot1, search_key_bytes=slot_key_t1, proof_nodes=observation_proof_t1)
                    sres2 = verify_mpt_proof(root_hash=pool_sroot2, search_key_bytes=slot_key_t2, proof_nodes=observation_proof_t2)

                    if sres1.is_valid and sres1.is_included and sres1.value_bytes and \
                       sres2.is_valid and sres2.is_included and sres2.value_bytes:
                        val1 = rlp_decode(sres1.value_bytes)
                        val2 = rlp_decode(sres2.value_bytes)
                        if isinstance(val1, bytes) and isinstance(val2, bytes) and len(val1) > 0 and len(val2) > 0:
                            slot1_bytes = val1.rjust(32, b"\x00")
                            slot2_bytes = val2.rjust(32, b"\x00")
                            # Uniswap V3 Observation struct layout:
                            # byte 0: bool initialized
                            # bytes 1..21: uint160 secondsPerLiquidityCumulativeX128
                            # bytes 21..28: int56 tickCumulative
                            # bytes 28..32: uint32 blockTimestamp
                            init1 = bool(slot1_bytes[0])
                            cum1_dec = int.from_bytes(slot1_bytes[21:28], "big", signed=True)
                            ts1_dec = int.from_bytes(slot1_bytes[28:32], "big")

                            init2 = bool(slot2_bytes[0])
                            cum2_dec = int.from_bytes(slot2_bytes[21:28], "big", signed=True)
                            ts2_dec = int.from_bytes(slot2_bytes[28:32], "big")

                            if init1 and init2 and ts1_dec == t1 and ts2_dec == t2 and cum1_dec == c1 and cum2_dec == c2:
                                obs_verified = True
            except Exception:
                obs_verified = False

        if not obs_verified:
            if require_authenticated:
                raise ValueError("Uniswap V3 TWAP requires authenticated observation state proofs at both observation points")
            return ValuationEvidenceRecord(
                source="UNISWAP_V3_TWAP",
                chain_id=chain_id,
                token_address=canon_token,
                price_usd=raw_price,
                timestamp_utc=ts_utc,
                feed_or_pool_address=canon_pool,
                round_id_or_observation_index=t2,
                observation_digest=obs_digest,
                verification_seal="",
                is_authenticated=False,
            )

        seal = ValuationEvidenceAuthenticator._compute_seal(
            source="UNISWAP_V3_TWAP",
            chain_id=chain_id,
            token_address=canon_token,
            price_usd=raw_price,
            timestamp_utc=ts_utc,
            feed_or_pool_address=canon_pool,
            round_id_or_observation_index=t2,
            observation_digest=obs_digest,
        )

        return ValuationEvidenceRecord(
            source="UNISWAP_V3_TWAP",
            chain_id=chain_id,
            token_address=canon_token,
            price_usd=raw_price,
            timestamp_utc=ts_utc,
            feed_or_pool_address=canon_pool,
            round_id_or_observation_index=t2,
            observation_digest=obs_digest,
            verification_seal=seal,
            is_authenticated=True,
        )


class ValuationEvidenceAuthenticator:
    """Cryptographic authenticator and verifier for valuation evidence records."""
    _SEAL_KEY: bytes = _VALUATION_SEAL_KEY

    @classmethod
    def _compute_seal(
        cls,
        source: str,
        chain_id: int,
        token_address: str,
        price_usd: float,
        timestamp_utc: str,
        feed_or_pool_address: str,
        round_id_or_observation_index: int,
        observation_digest: str,
    ) -> str:
        data = f"{source}:{chain_id}:{token_address.lower()}:{price_usd:.8f}:{timestamp_utc}:{feed_or_pool_address.lower()}:{round_id_or_observation_index}:{observation_digest.lower()}"
        return hmac.new(cls._SEAL_KEY, data.encode("utf-8"), hashlib.sha256).hexdigest()

    @classmethod
    def create_authenticated_record(cls, *args: Any, **kwargs: Any) -> ValuationEvidenceRecord:
        """Deprecated/removed: caller-supplied claims cannot be sealed directly without independent verification."""
        raise ValueError(
            "ValuationEvidenceAuthenticator.create_authenticated_record is removed: "
            "caller-supplied price/timestamp cannot be authenticated directly. "
            "Use ValuationEvidenceVerifier.verify_chainlink_round, verify_pyth_price, "
            "or verify_uniswap_v3_twap with genuine oracle evidence."
        )

    @classmethod
    def create_unverified_record(
        cls,
        source: str,
        chain_id: int,
        token_address: str,
        price_usd: float,
        timestamp_utc: str,
        feed_or_pool_address: str,
        round_id_or_observation_index: int = 1,
    ) -> ValuationEvidenceRecord:
        """Returns explicitly unverified valuation record."""
        return ValuationEvidenceRecord(
            source=source,
            chain_id=chain_id,
            token_address=canonicalize_token_address(token_address),
            price_usd=price_usd,
            timestamp_utc=timestamp_utc,
            feed_or_pool_address=canonicalize_token_address(feed_or_pool_address) if feed_or_pool_address.startswith("0x") and len(feed_or_pool_address) == 42 else feed_or_pool_address,
            round_id_or_observation_index=round_id_or_observation_index,
            observation_digest="",
            verification_seal="",
            is_authenticated=False,
        )

    @classmethod
    def verify_record(cls, record: Any) -> Tuple[bool, Optional[str]]:
        """Verifies whether a record is genuinely authenticated and has not been forged or fabricated."""
        if not isinstance(record, ValuationEvidenceRecord):
            return False, "VALUATION_RECORD_INVALID_TYPE"
        if not record.is_authenticated:
            return False, "VALUATION_RECORD_UNAUTHENTICATED"
        if not record.verification_seal:
            return False, "FORGED_VALUATION_AUTHENTICATION_FLAG"
        if not record.feed_or_pool_address or not str(record.feed_or_pool_address).startswith("0x"):
            return False, "FABRICATED_VALUATION_FEED_ADDRESS"

        try:
            canon_feed = canonicalize_token_address(record.feed_or_pool_address)
        except Exception:
            return False, "FABRICATED_VALUATION_FEED_ADDRESS"

        if record.chain_id not in CANONICAL_VALUATION_FEEDS or canon_feed not in CANONICAL_VALUATION_FEEDS[record.chain_id]:
            return False, "FABRICATED_VALUATION_FEED_ADDRESS"

        expected_seal = cls._compute_seal(
            source=record.source,
            chain_id=record.chain_id,
            token_address=record.token_address,
            price_usd=record.price_usd,
            timestamp_utc=record.timestamp_utc,
            feed_or_pool_address=canon_feed,
            round_id_or_observation_index=record.round_id_or_observation_index or 0,
            observation_digest=record.observation_digest,
        )
        if not hmac.compare_digest(record.verification_seal, expected_seal):
            return False, "FORGED_VALUATION_AUTHENTICATION_FLAG"
        return True, None


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
    quote_usd_price: Optional[float] = None
    native_usd_price: Optional[float] = None
    quote_usd_price_source: Optional[str] = None
    native_usd_price_source: Optional[str] = None
    quote_usd_price_timestamp_utc: Optional[str] = None
    native_usd_price_timestamp_utc: Optional[str] = None
    quote_valuation_record: Optional[ValuationEvidenceRecord] = None
    native_valuation_record: Optional[ValuationEvidenceRecord] = None
    base_fee_per_gas_wei: int = 20_000_000_000  # 20 gwei
    priority_fee_per_gas_wei: int = 2_000_000_000  # 2 gwei
    max_fee_per_gas_wei: Optional[int] = None
    token0_is_quote: bool = True

    def with_valuation_records(self, feed_address: Optional[str] = None) -> "PoolStateSnapshot":
        """Returns snapshot copy with ValuationEvidenceRecord attached.
        NOTE: Does NOT manufacture authenticated evidence. Sets is_authenticated=False.
        Never defaults to fabricated fallback prices (1.0, 3000.0).
        """
        default_feed_q = "0x8fffffd4afb6115b954bd326cbe7b4ba576818f6" if self.chain_id == 1 else "0x7e860098f58b3649411852513627710b2ca66624"
        default_feed_n = "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419" if self.chain_id == 1 else "0x71041dddad3595f9cef3dccf156597b71c782d68"
        q_rec = self.quote_valuation_record or (
            ValuationEvidenceRecord(
                source=self.quote_usd_price_source or "CHAINLINK_ORACLE",
                chain_id=self.chain_id,
                token_address=self.token_address,
                price_usd=self.quote_usd_price,
                timestamp_utc=self.quote_usd_price_timestamp_utc or self.source_timestamp_utc,
                feed_or_pool_address=feed_address or default_feed_q,
                round_id_or_observation_index=1,
                is_authenticated=False,
            ) if self.quote_usd_price is not None and self.quote_usd_price > 0 and self.quote_usd_price_source is not None else None
        )
        n_rec = self.native_valuation_record or (
            ValuationEvidenceRecord(
                source=self.native_usd_price_source or "CHAINLINK_ORACLE",
                chain_id=self.chain_id,
                token_address=self.token_address,
                price_usd=self.native_usd_price,
                timestamp_utc=self.native_usd_price_timestamp_utc or self.source_timestamp_utc,
                feed_or_pool_address=feed_address or default_feed_n,
                round_id_or_observation_index=1,
                is_authenticated=False,
            ) if self.native_usd_price is not None and self.native_usd_price > 0 and self.native_usd_price_source is not None else None
        )
        return dataclasses.replace(self, quote_valuation_record=q_rec, native_valuation_record=n_rec)

    def with_authenticated_valuation_records(self) -> "PoolStateSnapshot":
        """Returns snapshot copy with genuinely authenticated ValuationEvidenceRecords bound to canonical feeds.
        Strictly requires valid positive quote and native prices; never uses fallback prices (1.0, 3000.0).
        Fails closed if cryptographic proofs are absent, as oracle observations cannot be caller-asserted.
        """
        if self.quote_usd_price is None or self.quote_usd_price <= 0:
            raise ValueError("Cannot derive authenticated valuation records: missing or non-positive quote_usd_price")
        if self.native_usd_price is None or self.native_usd_price <= 0:
            raise ValueError("Cannot derive authenticated valuation records: missing or non-positive native_usd_price")

        raise ValueError(
            "Cryptographic proof of Chainlink round data (MPT storage proof anchored to authenticated state root) "
            "is required to establish genuinely authenticated valuation records"
        )


SUPPORTED_EVM_CHAIN_IDS: set[int] = {
    1,      # Ethereum Mainnet
    10,     # OP Mainnet (Optimism)
    56,     # BNB Smart Chain
    137,    # Polygon PoS
    8453,   # Base
    42161,  # Arbitrum One
    43114,  # Avalanche C-Chain
}

MAX_VALUATION_STALENESS_SECONDS: float = 3600.0


class TwoLegExecutionSimulator:
    """Simulates two-leg roundtrip execution (Buy at entry, Sell at touch) across capital tranches."""

    SUPPORTED_EVM_CHAIN_IDS = SUPPORTED_EVM_CHAIN_IDS

    @classmethod
    def simulate_roundtrip(
        cls,
        entry_state: Optional[PoolStateSnapshot],
        touch_state: Optional[PoolStateSnapshot],
        tranche_usd: float,
        gas_used_buy: int = 150_000,
        gas_used_sell: int = 150_000,
        require_authenticated_valuation: bool = False,
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

        # 2b. Explicit supported EVM chain allowlist and venue validation
        if entry_state.chain_id not in cls.SUPPORTED_EVM_CHAIN_IDS or touch_state.chain_id not in cls.SUPPORTED_EVM_CHAIN_IDS:
            diag = (
                f"UNSUPPORTED_NON_EVM_CHAIN_SOLANA_{entry_state.chain_id}"
                if entry_state.chain_id in {101, 102, 103}
                else f"UNSUPPORTED_CHAIN_ID_{entry_state.chain_id}"
            )
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": diag,
                "gas_buy_model_label": None,
                "gas_sell_model_label": None,
                "gas_buy_usd": 0.0,
                "gas_sell_usd": 0.0,
                "tranche_usd": tranche_usd,
            }

        if entry_state.venue_type not in {"UNISWAP_V2", "UNISWAP_V3"}:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": f"UNSUPPORTED_VENUE_{entry_state.venue_type}",
                "gas_buy_model_label": None,
                "gas_sell_model_label": None,
                "gas_buy_usd": 0.0,
                "gas_sell_usd": 0.0,
                "tranche_usd": tranche_usd,
            }
        if touch_state.venue_type not in {"UNISWAP_V2", "UNISWAP_V3"}:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": f"UNSUPPORTED_VENUE_{touch_state.venue_type}",
                "gas_buy_model_label": None,
                "gas_sell_model_label": None,
                "gas_buy_usd": 0.0,
                "gas_sell_usd": 0.0,
                "tranche_usd": tranche_usd,
            }

        # Orientation consistency check for V3
        if entry_state.venue_type == "UNISWAP_V3" and entry_state.evidence_v3 is not None:
            ev3 = entry_state.evidence_v3
            canon_t0 = canonicalize_token_address(ev3.token0_address)
            canon_t1 = canonicalize_token_address(ev3.token1_address)
            if canon_entry_token == canon_t0 and entry_state.token0_is_quote:
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": "V3_ORIENTATION_CONFLICT_TOKEN0_BASE_BUT_FLAG_TRUE",
                    "gas_buy_model_label": None,
                    "gas_sell_model_label": None,
                    "gas_buy_usd": 0.0,
                    "gas_sell_usd": 0.0,
                    "tranche_usd": tranche_usd,
                }
            if canon_entry_token == canon_t1 and not entry_state.token0_is_quote:
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": "V3_ORIENTATION_CONFLICT_TOKEN1_BASE_BUT_FLAG_FALSE",
                    "gas_buy_model_label": None,
                    "gas_sell_model_label": None,
                    "gas_buy_usd": 0.0,
                    "gas_sell_usd": 0.0,
                    "tranche_usd": tranche_usd,
                }

        # 3. Price validation (fails closed if missing, non-positive, or non-finite)
        if entry_state.quote_usd_price is None or not math.isfinite(entry_state.quote_usd_price) or entry_state.quote_usd_price <= 0:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "ENTRY_QUOTE_PRICE_MISSING_OR_NON_POSITIVE",
                "tranche_usd": tranche_usd,
            }
        if entry_state.native_usd_price is None or not math.isfinite(entry_state.native_usd_price) or entry_state.native_usd_price <= 0:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "ENTRY_NATIVE_PRICE_MISSING_OR_NON_POSITIVE",
                "tranche_usd": tranche_usd,
            }
        if touch_state.quote_usd_price is None or not math.isfinite(touch_state.quote_usd_price) or touch_state.quote_usd_price <= 0:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "TOUCH_QUOTE_PRICE_MISSING_OR_NON_POSITIVE",
                "tranche_usd": tranche_usd,
            }
        if touch_state.native_usd_price is None or not math.isfinite(touch_state.native_usd_price) or touch_state.native_usd_price <= 0:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "TOUCH_NATIVE_PRICE_MISSING_OR_NON_POSITIVE",
                "tranche_usd": tranche_usd,
            }

        # 4. Valuation Evidence Binding: Source identity, trust registry & observation timestamp validation
        for prefix, src, snap_rec, snap_price, snap_ts, snap_chain in [
            ("ENTRY_QUOTE", entry_state.quote_usd_price_source, entry_state.quote_valuation_record, entry_state.quote_usd_price, entry_state.quote_usd_price_timestamp_utc, entry_state.chain_id),
            ("ENTRY_NATIVE", entry_state.native_usd_price_source, entry_state.native_valuation_record, entry_state.native_usd_price, entry_state.native_usd_price_timestamp_utc, entry_state.chain_id),
            ("TOUCH_QUOTE", touch_state.quote_usd_price_source, touch_state.quote_valuation_record, touch_state.quote_usd_price, touch_state.quote_usd_price_timestamp_utc, touch_state.chain_id),
            ("TOUCH_NATIVE", touch_state.native_usd_price_source, touch_state.native_valuation_record, touch_state.native_usd_price, touch_state.native_usd_price_timestamp_utc, touch_state.chain_id),
        ]:
            side = "ENTRY" if "ENTRY" in prefix else "TOUCH"
            if not src or not src.strip():
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": f"{side}_VALUATION_SOURCE_MISSING",
                    "tranche_usd": tranche_usd,
                }
            if src not in TRUSTED_VALUATION_SOURCES:
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": f"{prefix}_VALUATION_SOURCE_UNTRUSTED",
                    "tranche_usd": tranche_usd,
                }
            if not snap_ts or not snap_ts.strip():
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": f"{side}_VALUATION_TIMESTAMP_MISSING",
                    "tranche_usd": tranche_usd,
                }
            # Requirement G: ValuationEvidenceRecord is strictly mandatory for every leg
            if snap_rec is None:
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": f"{prefix}_VALUATION_RECORD_MANDATORY_MISSING",
                    "tranche_usd": tranche_usd,
                }
            if not isinstance(snap_rec, ValuationEvidenceRecord):
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": f"{prefix}_VALUATION_RECORD_INVALID",
                    "tranche_usd": tranche_usd,
                }
            if not snap_rec.feed_or_pool_address or not str(snap_rec.feed_or_pool_address).strip().startswith("0x"):
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": f"{prefix}_VALUATION_FEED_ADDRESS_INVALID",
                    "tranche_usd": tranche_usd,
                }
            if snap_rec.feed_or_pool_address.lower() in {"0x" + "00" * 20, "0x" + "44" * 20, "0x" + "11" * 20}:
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": f"{prefix}_VALUATION_FEED_ADDRESS_FABRICATED",
                    "tranche_usd": tranche_usd,
                }
            if snap_rec.is_authenticated:
                ok_auth, err_auth = ValuationEvidenceAuthenticator.verify_record(snap_rec)
                if not ok_auth:
                    return {
                        "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                        "net_return_pct": None,
                        "diagnostic": f"{prefix}_{err_auth}",
                        "tranche_usd": tranche_usd,
                    }
            elif require_authenticated_valuation:
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": f"{prefix}_VALUATION_RECORD_UNAUTHENTICATED",
                    "tranche_usd": tranche_usd,
                }
            if snap_rec.chain_id != snap_chain:
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": f"{prefix}_VALUATION_CHAIN_MISMATCH",
                    "tranche_usd": tranche_usd,
                }
            if snap_rec.source != src:
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": f"{prefix}_VALUATION_SOURCE_MISMATCH",
                    "tranche_usd": tranche_usd,
                }
            if snap_price is not None and not math.isclose(snap_rec.price_usd, snap_price, rel_tol=1e-5):
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": f"{prefix}_VALUATION_PRICE_MISMATCH",
                    "tranche_usd": tranche_usd,
                }
            if snap_rec.timestamp_utc != snap_ts:
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": f"{prefix}_VALUATION_TIMESTAMP_MISMATCH",
                    "tranche_usd": tranche_usd,
                }

        # 5. 24-hour holding period & timestamp validation
        if not entry_state.source_timestamp_utc or not touch_state.source_timestamp_utc:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "ENTRY_OR_TOUCH_TIMESTAMP_MISSING",
                "tranche_usd": tranche_usd,
            }
        try:
            def _parse_iso(raw_ts: str) -> dt.datetime:
                return dt.datetime.fromisoformat(raw_ts[:-1] + "+00:00" if raw_ts.endswith("Z") else raw_ts)

            t_entry = _parse_iso(entry_state.source_timestamp_utc)
            t_touch = _parse_iso(touch_state.source_timestamp_utc)
            t_entry_q = _parse_iso(entry_state.quote_usd_price_timestamp_utc)
            t_entry_n = _parse_iso(entry_state.native_usd_price_timestamp_utc)
            t_touch_q = _parse_iso(touch_state.quote_usd_price_timestamp_utc)
            t_touch_n = _parse_iso(touch_state.native_usd_price_timestamp_utc)
        except Exception:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "MALFORMED_TIMESTAMP_ISO_FORMAT",
                "tranche_usd": tranche_usd,
            }

        for t_val, _ in [
            (t_entry, "ENTRY"),
            (t_touch, "TOUCH"),
            (t_entry_q, "ENTRY_QUOTE"),
            (t_entry_n, "ENTRY_NATIVE"),
            (t_touch_q, "TOUCH_QUOTE"),
            (t_touch_n, "TOUCH_NATIVE"),
        ]:
            if t_val.tzinfo is None:
                return {
                    "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                    "net_return_pct": None,
                    "diagnostic": "TIMEZONE_NAIVE_TIMESTAMP_REJECTED",
                    "tranche_usd": tranche_usd,
                }

        if t_touch < t_entry:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "TOUCH_PRECEDES_ENTRY_TIMESTAMP",
                "tranche_usd": tranche_usd,
            }

        # Freshness validation against snapshot observation time (<= 3600.0s)
        if abs((t_entry_q - t_entry).total_seconds()) > MAX_VALUATION_STALENESS_SECONDS:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "ENTRY_QUOTE_VALUATION_STALE",
                "tranche_usd": tranche_usd,
            }
        if abs((t_entry_n - t_entry).total_seconds()) > MAX_VALUATION_STALENESS_SECONDS:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "ENTRY_NATIVE_VALUATION_STALE",
                "tranche_usd": tranche_usd,
            }
        if abs((t_touch_q - t_touch).total_seconds()) > MAX_VALUATION_STALENESS_SECONDS:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "TOUCH_QUOTE_VALUATION_STALE",
                "tranche_usd": tranche_usd,
            }
        if abs((t_touch_n - t_touch).total_seconds()) > MAX_VALUATION_STALENESS_SECONDS:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "TOUCH_NATIVE_VALUATION_STALE",
                "tranche_usd": tranche_usd,
            }

        delta_seconds = (t_touch - t_entry).total_seconds()
        if delta_seconds < 0:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "TOUCH_PRECEDES_ENTRY_TIMESTAMP",
                "tranche_usd": tranche_usd,
            }

        if delta_seconds > MAX_HOLDING_HOURS * 3600.0:
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": "HOLDING_WINDOW_EXCEEDED_24H",
                "tranche_usd": tranche_usd,
            }

        if (
            entry_state.venue_type not in {"UNISWAP_V2", "UNISWAP_V3"}
            or touch_state.venue_type not in {"UNISWAP_V2", "UNISWAP_V3"}
        ):
            return {
                "status": DualTrackTargetStatus.MODELED_ROUNDTRIP_UNRESOLVED_STATE_MISSING.value,
                "net_return_pct": None,
                "diagnostic": f"UNSUPPORTED_VENUE_{entry_state.venue_type}",
                "gas_buy_model_label": None,
                "gas_sell_model_label": None,
                "gas_buy_usd": 0.0,
                "gas_sell_usd": 0.0,
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
            "valuation_authenticated": all([
                entry_state.quote_valuation_record and entry_state.quote_valuation_record.is_authenticated,
                entry_state.native_valuation_record and entry_state.native_valuation_record.is_authenticated,
                touch_state.quote_valuation_record and touch_state.quote_valuation_record.is_authenticated,
                touch_state.native_valuation_record and touch_state.native_valuation_record.is_authenticated,
            ]),
            "valuation_model_label": "AUTHENTICATED_CANONICAL_VALUATION" if all([
                entry_state.quote_valuation_record and entry_state.quote_valuation_record.is_authenticated,
                entry_state.native_valuation_record and entry_state.native_valuation_record.is_authenticated,
                touch_state.quote_valuation_record and touch_state.quote_valuation_record.is_authenticated,
                touch_state.native_valuation_record and touch_state.native_valuation_record.is_authenticated,
            ]) else "MODELED_UNAUTHENTICATED_VALUATION_ASSUMPTIONS",
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

        # 1b. Validate all numeric liquidity values are finite and non-negative
        for r in records:
            for val, name in [
                (r.liquidity_usd, "liquidity_usd"),
                (r.d_ask_plus_2pct, "d_ask_plus_2pct"),
                (r.d_bid_minus_2pct, "d_bid_minus_2pct"),
            ]:
                if val is None or not math.isfinite(val) or val < 0:
                    return {
                        "status": LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value,
                        "diagnostic": "NON_FINITE_OR_NEGATIVE_LIQUIDITY_REJECTED",
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

        if initial_usd is None or not math.isfinite(initial_usd) or initial_usd <= 0:
            return {
                "status": LiquidityClassificationStatus.UNRESOLVED_LIQUIDITY.value,
                "diagnostic": "INITIAL_LIQUIDITY_NON_POSITIVE",
                "window_end_status": window_end_status,
                "min_depth_ratio": None,
                "liquidity_end_usd_observed_unverified": liquidity_end_usd_unverified,
                "liquidity_end_usd_authenticated_verified": None,
                "recovery_observed_after_collapse": False,
                "recovery_confirmed_at_window_end": False,
            }

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

        # Priority 1: Check for LP burn events or depth collapse (< 20% on bid/ask depth)
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


class TargetTouchClassification(enum.Enum):
    """Rigorous classification separating unverified candle touches from verifiable execution state."""
    CANDLE_OBSERVED_TOUCH = "CANDLE_OBSERVED_TOUCH"
    ONCHAIN_OBSERVED_TOUCH = "ONCHAIN_OBSERVED_TOUCH"
    VERIFIED_TARGET_BLOCK_STATE = "VERIFIED_TARGET_BLOCK_STATE"
    TARGET_TIME_STATE_UNAVAILABLE = "TARGET_TIME_STATE_UNAVAILABLE"
    TARGET_NOT_REACHED = "TARGET_NOT_REACHED"


@dataclasses.dataclass
class TargetBlockStateProofPayload:
    """Raw cryptographic proof payload submitted for verification against state root."""
    chain_id: int
    block_number: int
    block_hash: str
    token_address: str
    pool_address: str
    state_root: str
    account_proof: List[bytes]
    storage_proof: List[bytes]
    storage_slot: int = 3
    token0_is_quote: bool = True
    token0_decimals: int = 18
    token1_decimals: int = 18
    venue_type: str = "UNISWAP_V2"


@dataclasses.dataclass
class VerifiedTargetBlockStateResult:
    """Non-forgeable result sealed by TargetStateProofVerifier."""
    chain_id: int
    block_number: int
    block_hash: str
    token_address: str
    pool_address: str
    state_root: str
    is_valid: bool
    target_price_reached: bool
    spot_price: float
    proof_type: str = "MPT_STATE_PROOF"
    verification_seal: str = ""


@dataclasses.dataclass
class TargetBlockStateProofResult:
    """Caller-constructible proof result (unauthenticated unless sealed by TargetStateProofVerifier)."""
    chain_id: int
    block_number: int
    block_hash: str
    token_address: str
    pool_address: str
    state_root: str
    is_valid: bool
    target_price_reached: bool
    proof_type: str = "MPT_STATE_PROOF"


class TargetStateProofVerifier:
    """Cryptographic proof verifier for pool state at target block.

    Verifies MPT account proof against state_root and MPT storage proof against account storage_root.
    Issues a tamper-evident VerifiedTargetBlockStateResult protected by a process-isolated HMAC seal.
    Caller-constructed dataclasses or unsealed results are strictly rejected.
    """
    _SEAL_KEY: bytes = os.urandom(32)

    @classmethod
    def _compute_seal(
        cls,
        chain_id: int,
        block_number: int,
        block_hash: str,
        token_address: str,
        pool_address: str,
        state_root: str,
        is_valid: bool,
        target_price_reached: bool,
        spot_price: float,
    ) -> str:
        data = f"{chain_id}:{block_number}:{block_hash.lower()}:{token_address.lower()}:{pool_address.lower()}:{state_root.lower()}:{is_valid}:{target_price_reached}:{spot_price:.8f}"
        return hmac.new(cls._SEAL_KEY, data.encode("utf-8"), hashlib.sha256).hexdigest()

    @classmethod
    def is_authenticated_result(cls, result: Any) -> bool:
        """Authenticates that a result was genuinely issued by this verifier and has not been tampered with."""
        if not isinstance(result, VerifiedTargetBlockStateResult):
            return False
        if not result.is_valid or not result.verification_seal:
            return False
        expected_seal = cls._compute_seal(
            chain_id=result.chain_id,
            block_number=result.block_number,
            block_hash=result.block_hash,
            token_address=result.token_address,
            pool_address=result.pool_address,
            state_root=result.state_root,
            is_valid=result.is_valid,
            target_price_reached=result.target_price_reached,
            spot_price=result.spot_price,
        )
        return hmac.compare_digest(result.verification_seal, expected_seal)

    @classmethod
    def verify_proof(
        cls,
        payload: TargetBlockStateProofPayload,
        entry_price: float,
        trust_anchor: Optional[BlockHeaderTrustAnchor] = None,
        pool_metadata: Optional[PoolMetadataTrustAnchor] = None,
        expected_state_root: Optional[str] = None,
        expected_chain_id: Optional[int] = None,
        expected_block_number: Optional[int] = None,
        expected_block_hash: Optional[str] = None,
        expected_token_address: Optional[str] = None,
        expected_pool_address: Optional[str] = None,
    ) -> Tuple[bool, Optional[VerifiedTargetBlockStateResult], Optional[str]]:
        """Cryptographically verifies pool account and storage proofs from raw trie nodes,
        mandatorily anchored to an independently authenticated block header and pool metadata.
        """
        if entry_price <= 0 or not math.isfinite(entry_price):
            return False, None, "INVALID_ENTRY_PRICE"

        # 1. Require independently authenticated block header trust anchor
        anchor = trust_anchor or BlockHeaderTrustAnchorRegistry.get(
            payload.chain_id, payload.block_number, payload.block_hash
        )
        if anchor is None or not BlockHeaderTrustAnchor.verify_authenticity(anchor):
            return False, None, "UNAUTHENTICATED_BLOCK_HEADER_TRUST_ANCHOR_REQUIRED"

        if anchor.chain_id != payload.chain_id:
            return False, None, f"TRUST_ANCHOR_CHAIN_MISMATCH_{anchor.chain_id}_VS_{payload.chain_id}"
        if anchor.block_number != payload.block_number:
            return False, None, f"TRUST_ANCHOR_BLOCK_NUMBER_MISMATCH_{anchor.block_number}_VS_{payload.block_number}"
        if anchor.block_hash.lower() != payload.block_hash.lower():
            return False, None, "TRUST_ANCHOR_BLOCK_HASH_MISMATCH"
        if anchor.state_root.lower() != payload.state_root.lower():
            return False, None, "TRUST_ANCHOR_STATE_ROOT_MISMATCH"

        if expected_state_root is not None and anchor.state_root.lower() != expected_state_root.lower():
            return False, None, "STATE_ROOT_MISMATCH"

        # 2. Require independently authenticated pool metadata anchor
        meta = pool_metadata or PoolMetadataTrustAnchorRegistry.get(
            payload.chain_id, payload.pool_address
        )
        if meta is None or not PoolMetadataTrustAnchor.verify_authenticity(meta):
            return False, None, "UNAUTHENTICATED_POOL_METADATA_TRUST_ANCHOR_REQUIRED"

        if meta.chain_id != payload.chain_id:
            return False, None, "POOL_METADATA_CHAIN_MISMATCH"
        if meta.venue_type != "UNISWAP_V2" or payload.storage_slot != 3 or meta.reserves_storage_slot != 3:
            return False, None, "UNSUPPORTED_POOL_VENUE_OR_STORAGE_SLOT"

        try:
            canon_payload_token = canonicalize_token_address(payload.token_address)
            canon_payload_pool = canonicalize_token_address(payload.pool_address)
            canon_meta_pool = canonicalize_token_address(meta.pool_address)
            canon_token0 = canonicalize_token_address(meta.token0_address)
            canon_token1 = canonicalize_token_address(meta.token1_address)
        except Exception as e:
            return False, None, f"ADDRESS_CANONICALIZATION_FAILED: {e}"

        if canon_payload_pool != canon_meta_pool:
            return False, None, "POOL_METADATA_ADDRESS_MISMATCH"

        if canon_payload_token not in {canon_token0, canon_token1}:
            return False, None, "TOKEN_NOT_IN_POOL"

        if payload.token0_is_quote != meta.token0_is_quote:
            return False, None, "QUOTE_TOKEN_ORIENTATION_MISMATCH"

        if (
            payload.token0_decimals != meta.token0_decimals
            or payload.token1_decimals != meta.token1_decimals
        ):
            return False, None, "TOKEN_DECIMALS_MISMATCH"

        # 3. Identity bindings
        if expected_chain_id is not None and payload.chain_id != expected_chain_id:
            return False, None, f"CHAIN_ID_MISMATCH_{payload.chain_id}_VS_{expected_chain_id}"
        if expected_block_number is not None and payload.block_number != expected_block_number:
            return False, None, f"BLOCK_NUMBER_MISMATCH_{payload.block_number}_VS_{expected_block_number}"
        if expected_block_hash is not None and payload.block_hash.lower() != expected_block_hash.lower():
            return False, None, "BLOCK_HASH_MISMATCH"
        if expected_token_address is not None:
            if canon_payload_token != canonicalize_token_address(expected_token_address):
                return False, None, "TOKEN_ADDRESS_MISMATCH"
        if expected_pool_address is not None:
            if canon_payload_pool != canonicalize_token_address(expected_pool_address):
                return False, None, "POOL_ADDRESS_MISMATCH"

        # 4. Format validation
        if not payload.state_root.startswith("0x") or len(payload.state_root) != 66:
            return False, None, "INVALID_STATE_ROOT_HEX_FORMAT"
        if not payload.block_hash.startswith("0x") or len(payload.block_hash) != 66:
            return False, None, "INVALID_BLOCK_HASH_HEX_FORMAT"
        if not payload.account_proof or not isinstance(payload.account_proof, list):
            return False, None, "ACCOUNT_PROOF_EMPTY"
        if not payload.storage_proof or not isinstance(payload.storage_proof, list):
            return False, None, "STORAGE_PROOF_EMPTY"

        # 5. Cryptographic Account Proof Verification against authenticated state_root
        try:
            state_root_bytes = bytes.fromhex(anchor.state_root[2:])
            pool_raw_bytes = bytes.fromhex(canon_payload_pool[2:])
            account_search_key = keccak256(pool_raw_bytes)

            acct_result = verify_mpt_proof(
                root_hash=state_root_bytes,
                search_key_bytes=account_search_key,
                proof_nodes=payload.account_proof,
            )
            if not acct_result.is_valid or not acct_result.is_included or not acct_result.value_bytes:
                return False, None, f"ACCOUNT_MPT_PROOF_INVALID: {acct_result.error}"

            account_leaf = AccountStateLeaf.from_rlp(acct_result.value_bytes)
            if not account_leaf.is_valid:
                return False, None, f"ACCOUNT_LEAF_INVALID: {account_leaf.error}"
            storage_root = account_leaf.storage_root
        except Exception as e:
            return False, None, f"ACCOUNT_PROOF_VERIFICATION_EXCEPTION: {e}"

        # 6. Cryptographic Storage Proof Verification against storage_root
        try:
            storage_search_key = UniswapV2Slot3Decoder.compute_storage_key(payload.storage_slot)
            storage_result = verify_mpt_proof(
                root_hash=storage_root,
                search_key_bytes=storage_search_key,
                proof_nodes=payload.storage_proof,
            )
            if not storage_result.is_valid or not storage_result.is_included or not storage_result.value_bytes:
                return False, None, f"STORAGE_MPT_PROOF_INVALID: {storage_result.error}"

            # Storage values in Ethereum MPT are RLP-encoded bytes
            val = storage_result.value_bytes
            try:
                decoded_val = rlp_decode(val)
                if isinstance(decoded_val, bytes):
                    val = decoded_val
            except Exception:
                pass

            decoded_res = UniswapV2Slot3Decoder.decode_storage_word(val)
            if not decoded_res.is_valid:
                return False, None, f"RESERVES_DECODE_FAILED: {decoded_res.error}"

            r0 = decoded_res.reserve0
            r1 = decoded_res.reserve1
            if r0 <= 0 or r1 <= 0:
                return False, None, "NON_POSITIVE_RESERVES"

            adj0 = 10 ** meta.token0_decimals
            adj1 = 10 ** meta.token1_decimals
            if meta.token0_is_quote:
                spot_price = (r0 / r1) * (adj1 / adj0)
            else:
                spot_price = (r1 / r0) * (adj0 / adj1)

            target_price = entry_price * (1.0 + TECHNICAL_TP_PCT / 100.0)
            target_reached = spot_price >= target_price

            seal = cls._compute_seal(
                chain_id=payload.chain_id,
                block_number=payload.block_number,
                block_hash=payload.block_hash,
                token_address=payload.token_address,
                pool_address=payload.pool_address,
                state_root=anchor.state_root,
                is_valid=True,
                target_price_reached=target_reached,
                spot_price=spot_price,
            )

            result = VerifiedTargetBlockStateResult(
                chain_id=payload.chain_id,
                block_number=payload.block_number,
                block_hash=payload.block_hash,
                token_address=payload.token_address,
                pool_address=payload.pool_address,
                state_root=anchor.state_root,
                is_valid=True,
                target_price_reached=target_reached,
                spot_price=spot_price,
                proof_type="MPT_STATE_PROOF",
                verification_seal=seal,
            )
            return True, result, None
        except Exception as e:
            return False, None, f"STORAGE_PROOF_VERIFICATION_EXCEPTION: {e}"


class DualTrackOutcomeEvaluator:
    """Evaluates technical pattern attainment independently from modeled execution."""

    @classmethod
    def classify_target_touch(
        cls,
        entry_price: float,
        max_candle_high_24h: Optional[float] = None,
        onchain_touch_observed: bool = False,
        target_state_proof: Optional[Any] = None,
        target_state_proof_payload: Optional[TargetBlockStateProofPayload] = None,
        trust_anchor: Optional[BlockHeaderTrustAnchor] = None,
        pool_metadata: Optional[PoolMetadataTrustAnchor] = None,
        target_time_state_available: bool = True,
        expected_chain_id: Optional[int] = None,
        expected_block_number: Optional[int] = None,
        expected_block_hash: Optional[str] = None,
        expected_token_address: Optional[str] = None,
        expected_pool_address: Optional[str] = None,
        expected_state_root: Optional[str] = None,
        target_block_state_verified: Any = None,
        **kwargs: Any,
    ) -> TargetTouchClassification:
        """Classifies target touch evidence separating candle observation from on-chain and block state.

        OHLC candle high alone cannot establish execution timing or verifiable state.
        Caller-supplied Booleans (e.g. target_block_state_verified=True) are strictly rejected.
        VERIFIED_TARGET_BLOCK_STATE requires a cryptographically validated result sealed by TargetStateProofVerifier
        mandatorily anchored to an independently authenticated block header and pool metadata.
        """
        if entry_price <= 0 or not math.isfinite(entry_price) or not target_time_state_available:
            return TargetTouchClassification.TARGET_TIME_STATE_UNAVAILABLE

        target_price = entry_price * (1.0 + TECHNICAL_TP_PCT / 100.0)
        candle_reached = (
            max_candle_high_24h is not None
            and math.isfinite(max_candle_high_24h)
            and max_candle_high_24h >= target_price
        )

        verified_state = False
        if target_state_proof_payload is not None and isinstance(target_state_proof_payload, TargetBlockStateProofPayload):
            ok, res, _ = TargetStateProofVerifier.verify_proof(
                payload=target_state_proof_payload,
                entry_price=entry_price,
                trust_anchor=trust_anchor,
                pool_metadata=pool_metadata,
                expected_state_root=expected_state_root,
                expected_chain_id=expected_chain_id,
                expected_block_number=expected_block_number,
                expected_block_hash=expected_block_hash,
                expected_token_address=expected_token_address,
                expected_pool_address=expected_pool_address,
            )
            if ok and res is not None and res.target_price_reached:
                verified_state = True
        elif target_state_proof is not None:
            if TargetStateProofVerifier.is_authenticated_result(target_state_proof):
                proof = target_state_proof
                chain_match = (expected_chain_id is None) or (proof.chain_id == expected_chain_id)
                block_num_match = (expected_block_number is None) or (proof.block_number == expected_block_number)
                block_hash_match = (expected_block_hash is None) or (proof.block_hash.lower() == expected_block_hash.lower())
                state_root_match = (expected_state_root is None) or (proof.state_root.lower() == expected_state_root.lower())
                try:
                    token_match = (expected_token_address is None) or (
                        canonicalize_token_address(proof.token_address) == canonicalize_token_address(expected_token_address)
                    )
                    pool_match = (expected_pool_address is None) or (
                        canonicalize_token_address(proof.pool_address) == canonicalize_token_address(expected_pool_address)
                    )
                except Exception:
                    token_match = False
                    pool_match = False

                if (
                    proof.target_price_reached
                    and chain_match
                    and block_num_match
                    and block_hash_match
                    and state_root_match
                    and token_match
                    and pool_match
                ):
                    verified_state = True

        if verified_state:
            return TargetTouchClassification.VERIFIED_TARGET_BLOCK_STATE
        if onchain_touch_observed:
            return TargetTouchClassification.ONCHAIN_OBSERVED_TOUCH
        if candle_reached:
            return TargetTouchClassification.CANDLE_OBSERVED_TOUCH
        return TargetTouchClassification.TARGET_NOT_REACHED

    @staticmethod
    def evaluate_market_technical_tp(
        entry_price: float,
        max_candle_high_24h: float,
    ) -> bool:
        """Evaluates whether market price reached technical +40.00% TP within 24h (candle high only)."""
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
        def _matches_canon(addr: str, expected_canon: str) -> bool:
            try:
                return canonicalize_token_address(addr) == expected_canon
            except (ValueError, Exception):
                return False

        try:
            canonical_entry_token = canonicalize_token_address(entry.token_id)
            canonical_exit_token = canonicalize_token_address(exit_exec.token_id)
        except (ValueError, Exception):
            return ReconciledTrancheOutcome(
                status=EvidenceTier.RECEIPT_VERIFICATION_FAILED,
                discrepancy_reason="MALFORMED_JOURNAL_TOKEN_ADDRESS",
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

        has_entry_transfer = any(
            _matches_canon(t.token_address, canonical_entry_token)
            and t.to_address.lower() == entry.wallet_address.lower()
            and math.isclose(t.amount, entry.tokens_acquired, rel_tol=1e-4)
            for t in entry_receipt.transfers
        )
        has_exit_transfer = any(
            _matches_canon(t.token_address, canonical_exit_token)
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
