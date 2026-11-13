# RocketHunter v4.30.0 — Phase 5 Engineering Report: Web3 Security Intelligence & Systematic Discovery

**Date:** 2026-11-13  
**Branch:** `feature/v4.30-research-intelligence`  
**Phase:** 5 (Web3 Security and Discovery)  
**Status:** COMPLETE & VERIFIED

---

## 1. What Was Wrong

1. **Blindness to Solana Token-2022 Extensions:**
   - Solana's Token-2022 standard introduced program extensions such as permanent delegates (which allow the creator to confiscate tokens from user wallets) and external transfer hooks (which allow creators to block sales).
   - The security audit previously checked only basic freeze and mint authorities, leaving dangerous Token-2022 rug vectors uninspected.

2. **Conflating Uniswap V3 Concentrated Liquidity with V2 Burned LP:**
   - Uniswap V3 and concentrated-liquidity DEXs represent liquidity positions as non-fungible ERC-721 NFTs rather than fungible ERC-20 tokens.
   - An ordinary `totalSupply()` / `balanceOf()` call on an EVM pool address fails or returns 0 on concentrated pools. The system did not distinguish between a missing V2 LP token and an NFT-based concentrated pool, risking inaccurate lock status reporting.

3. **Naively Interpreting HTTP 429 `Retry-After: 0`:**
   - When DEX data providers return rate-limiting HTTP 429 responses with `Retry-After: 0`, naive clients immediately retry and get banned or blocked.
   - Discovery previously requested only the first 100 items per search family without pagination and did not report partial coverage metrics.

---

## 2. What Was Changed

1. **Chain-Aware Token-2022 Extension Auditing (`scripts/dex_onchain_evidence.py`):**
   - Added automated extraction and verification of Token-2022 extensions:
     - `permanentDelegate`: Flagged as a CRITICAL security risk (`PermanentDelegateExtension`).
     - `transferHook`: Flagged as a CRITICAL security risk (`TransferHookExtension`).
     - `defaultAccountState (frozen)`: Flagged as a CRITICAL security risk.
     - `transferFee` and `confidentialTransfers`: Tracked in structured metadata.
     - **Verification Attribution:** Explicitly attributes verification method as `RUGCHECK_REPORT_PARSED` and marks `direct_rpc_mint_deserialization: False`.

2. **Concentrated Liquidity (NFT) Model Recognition (`scripts/dex_onchain_evidence.py`):**
   - In `_evm_lp_lock_share()`, the system probes for Uniswap V3 concentrated liquidity methods (`slot0()`).
   - If a concentrated liquidity pool is detected, it is explicitly classified as `CONCENTRATED_LIQUIDITY_NFT`.
   - **Fail-Closed Rule:** The system explicitly records `nft_position_ownership_audited: False` and notes that conventional ERC-20 LP burn checks cannot verify NFT positions. `UNKNOWN` status strictly remains `UNVERIFIED` and never converts to `PASS`.

3. **Systematic Pagination & Persistent Shared Cooldown (`scripts/v425_early_breakout.py`):**
   - Added multi-page pagination for DexPaprika search families within rolling quota ceilings.
   - **Persistent Shared Cooldown:** When HTTP 429 is encountered, writes state to `.dexpaprika-cooldown.json` under `locked_file` locking.
   - Enforces a minimum 15-second backoff across all query families and concurrent scanner processes even when `Retry-After: 0`, blocking requests before network calls or request budget reservations.
   - Added full coverage reporting in discovery metadata:
     - `coverage_status: "PARTIAL"`
     - `pages_retrieved`, `records_discovered`, `records_rejected`
     - `rate_limit_failures`, `work_left_for_future_rotations`
     - Explicit disclosure: *"Endpoint returns ranked sampled pages up to provider quota limits; this is a broad ranked research sample, never an exhaustive census of all Web3 assets."*

---

## 3. Why the Change Matters in Plain Language

- **Protection Against Hidden Solana Traps:** Malicious actors on Solana can create tokens where they have secret backdoor "delegate" powers to drain your wallet even after renouncing mint authority. RocketHunter now checks these hidden extensions and blocks any token containing them.
- **Accurate Liquidity Accounting for Uniswap V3:** Uniswap V3 works differently from older DEXs—liquidity is tracked as NFTs rather than tokens. RocketHunter now recognizes this difference and refuses to guess or fake liquidity lock percentages.
- **Respectful & Sustainable API Usage:** If an API asks RocketHunter to slow down, RocketHunter pauses politely rather than hammering the server and getting the user's IP blocked.

---

## 4. Modified Files

- `scripts/dex_onchain_evidence.py` (Added Token-2022 extension checks and EVM concentrated liquidity detection).
- `scripts/v425_early_breakout.py` (Added DexPaprika pagination, Retry-After backoff protection, and coverage ledger reporting).
- `tests/test_v430_phase5_security_discovery.py` (New: 5 unit tests covering Token-2022 extensions, concentrated liquidity, 429 backoff, and partial coverage reporting).

---

## 5. Tests Executed & Results

- **Command:** `python -m pytest tests/test_v430_phase5_security_discovery.py -v`
- **Result:** **5 PASSED, 0 FAILED** (100% pass rate in 0.21s).

---

## 6. Scanner Health, Memory & CI Status

- **Local Scanner Health:** Operational.
- **Historical Memory Preserved:** 100% intact.
- **Workflow Status:** Unchanged and healthy.

---

## 7. What Remains for Subsequent Phases

- **Phase 6:** AURA research learning improvements (Dataset A vs Dataset B separation).
- **Phase 7:** CI/CD workflow separation (light CI vs scan workflows) & final release audit.
