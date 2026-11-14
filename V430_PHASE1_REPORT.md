# RocketHunter v4.30.0 — Phase 1 Engineering Report: Immediate Correctness Fixes

**Date:** 2026-11-13  
**Branch:** `feature/v4.30-research-intelligence`  
**Phase:** 1 (Immediate Correctness Fixes)  
**Status:** COMPLETE & VERIFIED

---

## 1. What Was Wrong

1. **Impossible 1-Hour Reversal Condition (`scripts/rocket_hunt.py`):**
   - In `score_candidate()`, the 1-hour percentage change (`change_1h_pct`) was previously clamped to zero or positive using `p1 = max(0.0, n(row.get("change_1h_pct")) or 0.0)`.
   - A subsequent conditional check `if p24 >= 500 and p1 is not None and p1 < 0:` attempted to apply a 4-point penalty to extreme 24h pumps that are aggressively reversing in the latest hour.
   - Because `p1` was pre-clamped to $\ge 0$, `p1 < 0` could **never evaluate to true**. The reversal penalty was dead code, allowing rapidly reversing tokens to retain high momentum scores.

2. **Silent Engine Exception Suppression (`scripts/v425_early_breakout.py`):**
   - In `score_candidate()`, calls to `BreakoutSignalEngines.evaluate_all_engines(row)` and `ExpectedReturnEngine.estimate_outcomes(row, engines_res)` were enclosed in a bare `except Exception: pass`.
   - Any runtime bug, missing dependency, unexpected data type, or arithmetic error inside the 9 signal engines or expected-return model was swallowed completely without recording diagnostics.
   - Downstream consumers had no way to distinguish whether a signal was absent because market data was missing or because the software engine had crashed.

3. **Missing-Data Integrity & Safety Representation:**
   - There was no explicit recording of execution state (`signal_engine_executed`, `expected_return_engine_executed`, `engine_failure_module`, `engine_exception_category`).
   - A silent crash could allow a token with broken signals to slip past safety filters without recording why its evaluation failed.

---

## 2. What Was Changed

1. **Decoupled Signed 1h Movement from Nonnegative Momentum (`scripts/rocket_hunt.py`):**
   - Preserved `p1_raw = n(row.get("change_1h_pct"))` to retain the signed direction (positive or negative).
   - Computed `p1 = max(0.0, p1_raw) if p1_raw is not None else 0.0` for nonnegative positive momentum contribution.
   - Reversal check now tests `if p24 >= 500 and p1_raw is not None and p1_raw < 0: penalty += 4.0`.

2. **Replaced Silent Exception Suppression with Structured Diagnostics (`scripts/v425_early_breakout.py`):**
   - Tracked execution state explicitly:
     - `signal_engine_executed` (bool)
     - `expected_return_engine_executed` (bool)
     - `engine_failure_module` (e.g. `"breakout_signal_engines"`, `"expected_return_engine"`, `"import"`, or `None`)
     - `engine_exception_category` (e.g. `"ZeroDivisionError"`, `"TypeError"`, or `None`)
     - `engine_failure_diagnostic` (sanitized string without exposing secrets or credentials)
     - `engine_result_status` (`"SUCCESS"`, `"DATA_UNAVAILABLE"`, or `"SOFTWARE_FAILURE"`)
     - `expected_return_status` (`"SUCCESS"`, `"DATA_UNAVAILABLE"`, or `"SOFTWARE_FAILURE"`)
   - If software execution fails:
     - Automatically attaches risk flags: `"SOFTWARE_ERROR_SIGNAL_ENGINES_INCOMPLETE"` and `"UNVERIFIED_DUE_TO_ENGINE_FAILURE"`.
     - Forces `breakout_evidence_eligible = False` and zeroes evidence coverage.
     - Enforces fail-closed behavior in `qualify_final_actionable_candidates`: software failure generates the blocker `"signal engine software execution failed or threw an exception"` and immediately excludes the candidate from the verified actionable lane.

3. **Preserved Missing-Data Semantics Across All 9 Engines (`scripts/breakout_signal_engines.py`):**
   - Verified that unmeasured fields return explicit `MISSING`, `PARTIAL`, `STALE`, or `CONFLICTING` status and zero scores, rather than inventing synthetic numbers.

---

## 3. Why the Change Matters in Plain Language

- **Protection Against Pump Dumps:** When a meme coin spikes 500% in 24 hours but is crashing down -15% in the last hour, the system now properly detects the reversal and penalizes its score instead of pretending the drop never happened.
- **No More Hidden Software Bugs:** If any calculation in the 9 signal engines fails, the software no longer sweeps the error under the rug. It records exactly what broke, why it broke, and ensures the broken asset is never marked as a safe or verified trade opportunity.
- **Honest Data:** Missing market information is never treated as a positive signal or zero risk. If an exchange does not provide wallet or buy/sell data, the system flags it as missing rather than assuming safe conditions.

---

## 4. Modified Files

- `scripts/rocket_hunt.py` (Fixed reversal condition).
- `scripts/v425_early_breakout.py` (Added explicit engine execution tracking, diagnostics, fail-closed handling on software errors).
- `tests/test_rocket_hunt_reversal.py` (New: 5 unit tests covering reversal penalty, flat moves, zero moves, missing, and non-finite values).
- `tests/test_v430_engine_diagnostics.py` (New: 5 unit tests covering software failure capture, diagnostic recording, missing data distinction, and fail-closed final gate behavior).

---

## 5. Tests Executed & Results

- **Command:** `python -m pytest tests/ -v`
- **Total Tests Run:** 367 unit tests + 19 subtests.
- **Results:** **367 PASSED, 0 FAILED** (100% pass rate in 42.05s).
- **New Tests:**
  - `test_rocket_hunt_reversal.py`: 5 passed.
  - `test_v430_engine_diagnostics.py`: 5 passed.

---

## 6. Scanner Health, Memory & CI Status

- **Local Scanner Health:** Fully operational; imports and pipeline components compile and pass all regression gates.
- **Historical Memory Preserved:** All historical ledgers, user trade journal, and forecasts in `rocket-hunter-data/scanner-memory/` remain 100% untouched and backed up in `rocket-hunter-data-v430-backup/`.
- **Workflow Status:** Workflow and script contracts validated locally.

---

## 7. What Remains for Subsequent Phases

- **Phase 2:** Research data integrity — prospective opportunity snapshots (22+ fields), 24h outcome collection improvements (MFE, MAE, intrahour touch, outcome classification).
- **Phase 3:** Chronological walk-forward backtesting module.
- **Phase 4:** Multi-size execution simulator expansion ($100 to $5,000).
- **Phase 5:** Chain-aware Web3 security (Solana/EVM LP models) & systematic discovery pagination.
- **Phase 6:** AURA research learning improvements (Dataset A vs Dataset B separation).
- **Phase 7:** CI/CD workflow separation (light CI vs scan workflows) & final release audit.
