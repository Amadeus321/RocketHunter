# RocketHunter v4.30.0 — Baseline Audit & Memory Protection Report (Phase 0)

**Date:** 2026-11-13  
**Base Commit SHA:** `11166313475fb0001422a75ff34b9843b9f7043b`  
**Active Development Branch:** `feature/v4.30-research-intelligence`  
**Target Release:** v4.30.0  
**Status:** COMPLETE (Phase 0)

---

## 1. Development Environment & Baseline Protection

1. **Branch Isolation:**
   - Switched from `main` to `feature/v4.30-research-intelligence`.
   - All v4.30 engineering will occur strictly within this branch until audit acceptance.

2. **Memory & Data Protection:**
   - Complete snapshot of active scanner memory and research data copied from `rocket-hunter-data/` to `rocket-hunter-data-v430-backup/`.
   - `.gitignore` updated to guarantee backup directories (`*-backup*`, `rocket-hunter-data-v430-backup/`) are never committed or polluted.
   - Historical learning ledgers, user trade journal (`rocket-hunter-data/scanner-memory/`), negative regression examples (`FAIR/SOL`, `USOE/SOL`, `CORLA/WBNB`), and provider rate-limiting states are frozen and protected.

3. **Deterministic Test Suite Baseline:**
   - Test command: `python -m pytest tests/ -v`
   - **Result: 357 passed, 19 subtests passed in 74.84s (0 failures, 0 errors).**
   - Exact Python version: Python 3.13.14 (win32).

---

## 2. Architectural Pipeline Review

RocketHunter operates a read-only multi-stage research pipeline with three distinct output categories:
1. **Discovery Leads:** High-interest assets detected across CEX/DEXvenues.
2. **Research Watchlist:** High-upside candidates (+40% target) undergoing safety, on-chain and liquidity qualification.
3. **Verified Actionable Opportunities:** Candidates passing every identity, liquidity, LP lock/burn, smart contract security, fresh two-way quote, and verified technical take-profit requirement.

### Core Modules Audited:
- `scripts/rocket_hunt.py`: Discovery engine, momentum/gain calculations, candidate scoring.
- `scripts/v425_early_breakout.py`: Early breakout scoring, confluence evaluation, final actionable qualification gate.
- `scripts/breakout_signal_engines.py`: 9-engine breakout detection suite (Momentum Acceleration, Organic Volume Burst, Buy/Sell CVD Pressure, Liquidity Depth, Accumulation Compression, Smart-Money/Holder Behavior, Resistance Proximity, Ecosystem Flow, Risk Intelligence).
- `scripts/expected_return_engine.py`: Heuristic 24h risk-adjusted return, upside excursion projection, single TP40 check.
- `scripts/dex_onchain_evidence.py` & `scripts/dex_loss_guard.py`: Fail-closed security adapter covering Solana (RugCheck/Raydium/RPC) and EVM (RPC LP analysis, Blockscout holder & first-native-funder analysis).
- `scripts/execution_simulator.py`: Multi-size read-only two-way quote simulator.
- `scripts/unified_budget_manager.py`: Adaptive runtime budget manager with roll-forward capabilities.
- `scripts/user_trade_learning.py` (AURA): Feature attribution learning on user-taken trades, versioned model rollback, negative regression safeguards.
- `scripts/collect_watchlist_observations.py` & `scripts/analyze_candle_outcomes.py`: 24h observation collection and hourly candle outcome evaluator.
- `scripts/research_readiness.py` & `scripts/status_dashboard.py`: Statistical readiness verification and static monitoring dashboard.

---

## 3. Existing Limitations & Known Deficiencies

Prior to v4.30 upgrades, the system exhibits the following known deficiencies requiring remediation:

1. **Impossible 1-Hour Reversal Condition (`scripts/rocket_hunt.py`):**
   - Line 140 clamps signed 1h price change `change_1h_pct` to `max(0.0, ...)`, making a subsequent check for `p1 < 0` mathematically dead code. Reversal penalties are never applied to strong 24h gainers pulling back aggressively in the last hour.

2. **Silent Engine Exception Suppression (`scripts/v425_early_breakout.py`):**
   - Signal engine evaluation and expected return calls catch broad exceptions without recording structured diagnostic provenance, obscuring underlying provider or schema failures.

3. **Missing vs. Zero Ambiguity:**
   - Several signal components treat unmeasured or missing data as zero or neutral defaults rather than explicitly reporting `UNMEASURED` or `MISSING`, risking false signals on low-coverage tokens.

4. **Sample Size & Predictive Backtesting Gap:**
   - Historical candle evaluation only has 6 complete 24-hour windows out of 60 exact-identity joins (54 censored/incomplete). The platform is currently `NOT READY` as a predictive strategy and lacks walk-forward chronological out-of-sample backtesting.

5. **Execution Simulator Scope:**
   - Multi-size scenarios ($100, $500, $1k, $5k) need explicit liquidity depth, size-dependent price impact curves, round-trip cost calculations, and freshness validation.

6. **Web3 Security & LP Model Limitations:**
   - Concentrated liquidity positions (Uniswap v3 NFTs, Raydium CLMM) are not fully distinguished from traditional fungible LP share locks. Solana token extensions (transfer fees, confidential transfers) need explicit detection.

7. **Discovery Pagination & Provider Budgets:**
   - DexPaprika search families retrieve up to 100 records per query family without following pagination cursors. Provider rate-limiting (HTTP 429) must strictly honor exponential backoff and `Retry-After`.

8. **AURA Learning Separation:**
   - AURA must strictly maintain two independent datasets: Dataset A (User-confirmed positions) and Dataset B (Prospective scanner predictions with frozen decision-time features), preventing label leakage.

9. **CI/CD Workflow Coupling:**
   - Pushes to `main` previously could trigger expensive full market scans. Code validation and unit tests must be separated from live data collection.

---

## 4. Phase 0 Deliverable Sign-Off

- Baseline commit: `1116631`
- Baseline tests: 357 passing (100%)
- Branch: `feature/v4.30-research-intelligence`
- Scanner memory backed up and locked.
- Proceeding to Phase 1 (Immediate Correctness Fixes).
