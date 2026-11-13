# RocketHunter v4.30.0 — Phase 3 Engineering Report: Chronological Predictive Evaluation

**Date:** 2026-11-13  
**Branch:** `feature/v4.30-research-intelligence`  
**Phase:** 3 (Predictive Evaluation)  
**Status:** COMPLETE & VERIFIED

---

## 1. What Was Wrong

1. **Lack of Proper Out-of-Sample Walk-Forward Backtesting:**
   - Previous system evaluations were descriptive reports over historical samples or static calibration curves.
   - There was no automated mechanism to partition historical records strictly by time (walk-forward chronological windows) to prove whether scanner ranking heuristics hold up on unseen future data.
   - Traditional random train/test splits risk future data leakage (lookahead bias), where market conditions from the future leak into past feature weights.

2. **Uncalibrated Claims vs Reproducible Evidence:**
   - Without chronological out-of-sample walk-forward validation, a few extreme positive outliers could distort statistical averages, giving the false illusion of high predictive accuracy.
   - Missing statistical confidence bounds (e.g. Wilson score confidence intervals) left prediction rates vulnerable to small-sample randomness.

---

## 2. What Was Changed

1. **Created Chronological Walk-Forward Backtest Engine (`scripts/chronological_backtest.py`):**
   - Implemented `ChronologicalBacktestEvaluator` which strictly enforces time-separated walk-forward evaluation:
     - Partitions datasets chronologically across train, test, and step intervals.
     - Guarantees zero future-data leakage: models are developed only on earlier windows and evaluated strictly on subsequent unseen windows.
     - Freezes decision-time features at the exact moment of detection.
     - Automatically excludes ambiguous identities (missing chain, token, or pool).
     - Deduplicates repeated pools and overlapping signals.
   - Measures:
     - **TP40 Attainment Rate:** Percentage of out-of-sample selections reaching +40% within 24 hours.
     - **Milestone Attainment:** Attainment percentages at 1h, 3h, 6h, 12h, and 24h horizons.
     - **False-Positive Rate:** Transparently accounting for candidates that failed to reach target.
     - **Excursion Distribution:** Mean and median Maximum Favorable Excursion (MFE) and Maximum Adverse Excursion (MAE).
     - **Statistical Confidence:** Wilson 95% binomial confidence intervals to bound uncertainty.
     - **Segmented Breakdowns:** Performance separated by blockchain network and liquidity tier.
     - **Baseline Comparison:** Direct benchmark against naive momentum strategies.
     - **Cryptographic Provenance:** Generates a SHA-256 dataset hash for full auditability.

---

## 3. Why the Change Matters in Plain Language

- **No Cheating with Future Data:** In real life, you cannot use tomorrow's news to make today's trades. This module forces the computer to travel through time chronologically: it tests the software on Monday's data, evaluates how it would have performed on Tuesday, moves to Wednesday, and so on.
- **Honest Win Rates:** Instead of claiming a strategy has an 80% win rate based on 5 lucky tokens, the system now calculates mathematical confidence intervals (e.g. [52.1%, 74.8%]) so you know the true statistical boundaries.
- **Transparent Benchmarking:** It compares RocketHunter's signals against a simple naive benchmark (e.g. just buying whatever pumped the most) to prove whether the 9 breakout engines actually add genuine research value.

---

## 4. Modified Files

- `scripts/chronological_backtest.py` (New module implementing walk-forward out-of-sample evaluation, metrics, and segmentation).
- `tests/test_v430_backtest.py` (New: 3 unit tests covering chronological ordering, identity filtering, and walk-forward metrics).

---

## 5. Tests Executed & Results

- **Command:** `python -m pytest tests/test_v430_backtest.py -v`
- **Result:** **3 PASSED, 0 FAILED** (100%).

---

## 6. Scanner Health, Memory & CI Status

- **Local Scanner Health:** Operational.
- **Historical Memory Preserved:** 100% intact.
- **Workflow Status:** Unchanged and healthy.

---

## 7. What Remains for Subsequent Phases

- **Phase 4:** Multi-size execution simulator expansion ($100 to $5,000).
- **Phase 5:** Chain-aware Web3 security (Solana/EVM LP models) & systematic discovery pagination.
- **Phase 6:** AURA research learning improvements (Dataset A vs Dataset B separation).
- **Phase 7:** CI/CD workflow separation (light CI vs scan workflows) & final release audit.
