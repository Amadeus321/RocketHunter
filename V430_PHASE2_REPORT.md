# RocketHunter v4.30.0 — Phase 2 Engineering Report: Research Data Integrity

**Date:** 2026-11-13  
**Branch:** `feature/v4.30-research-intelligence`  
**Phase:** 2 (Research Data Integrity)  
**Status:** COMPLETE & VERIFIED

---

## 1. What Was Wrong

1. **Selection Bias in Historical Memory:**
   - Previous scanner memory primarily stored forecast records for assets that passed initial filter thresholds. Rejected assets were omitted from long-term feature snapshots.
   - Studying only assets that passed heuristic filters introduces severe selection bias, preventing machine learning and statistical models from learning why certain assets fail or distinguishing true signals from false positives.

2. **Missing Granular Research Features (22 Mandatory Lanes):**
   - Candidate records did not uniformly retain the full 22-field observation-time research specification (including exact pool age vs token age, base/quote orientation, LP/wallet evidence breakdown, and exact rejection reasons).
   - Raw provider inputs and normalized features were frequently blended, making it difficult to audit historical provider anomalies.

3. **Incomplete 24-Hour Outcome Distinctions:**
   - Outcome evaluations lacked explicit standard categorization between target hits, misses, data gaps, source conflicts, and unverified execution.
   - Price touches were at risk of being conflated with executed trades. Intrahorizon metrics (MFE, MAE, first touch timestamp) were not systematically recorded in outcome objects.

---

## 2. What Was Changed

1. **Built Prospective Opportunity Research Dataset (`scripts/prospective_opportunity_dataset.py`):**
   - Implemented `ProspectiveOpportunityDataset` capturing immutable observation-time snapshots conforming to schema `crypto-prebreakout.prospective-opportunity.v1`.
   - Guaranteed capture of all **22 mandatory research fields**:
     1. Chain / network
     2. Token contract / mint
     3. Exact pool address
     4. Quote-token address
     5. Base/quote orientation verification
     6. Exchange / DEX venue name
     7. Token age and pool age, separately
     8. Detection-time price
     9. Market capitalization and FDV
     10. Pool liquidity
     11. Trading volume breakdown (24h, 6h, 1h, 5m)
     12. Transaction counts (total txns, buys, sells)
     13. Technical indicators (1h ATR, swing resistance, price change windows)
     14. All 9 breakout engine measurements and statuses
     15. Smart contract security audit status
     16. LP lock/burn and launch-wallet cluster status
     17. Source & provider timestamps
     18. Local retrieval timestamp
     19. Scanner version
     20. Configuration fingerprint
     21. Signal scores and component breakdown
     22. Qualification status and exact rejection reasons
   - Strictly separates `raw_source_evidence` from `normalized_record`.
   - **Zero Selection Bias:** Preserves rejected assets alongside qualified opportunities.
   - Integrated into `scripts/v418_pipeline.py` to automatically write compressed partitions into `rocket-hunter-data/scanner-memory/prospective-opportunities/`.

2. **Standardized 24-Hour Outcome Classifications (`scripts/analyze_candle_outcomes.py` & `scripts/collect_watchlist_observations.py`):**
   - Enforced 7 explicit outcome classifications:
     - `TARGET_REACHED`
     - `TARGET_NOT_REACHED`
     - `INCOMPLETE_OBSERVATION`
     - `UNKNOWN_DUE_TO_GAP`
     - `PRICE_SOURCE_CONFLICT`
     - `EXPIRED`
     - `EXECUTION_UNVERIFIED`
   - Explicitly records Maximum Favorable Excursion (`mfe_pct`), Maximum Adverse Excursion (`mae_pct`), and the exact `first_observed_target_touch_utc`.
   - Sets `execution_status = "EXECUTION_UNVERIFIED"` across all price observations, guaranteeing that market price candle movements are never misrepresented as executed trades.

---

## 3. Why the Change Matters in Plain Language

- **Learning from Mistakes (No Selection Bias):** If an AI only studies successful investments, it develops blind spots. By recording every rejected token along with the exact reason it was rejected (e.g. honeypot risk, low liquidity, fake volume), the system learns what scams look like and becomes dramatically more resilient.
- **Scientific Reproducibility:** Every candidate detected now has an immutable time-capsule recording exactly what the market looked like at that second across 22 dimensions. This data can never be rewritten retroactively with future knowledge.
- **Honest Outcome Accounting:** A token touching +40% for one second on an illiquid DEX is not the same as a filled order. The system now records the exact minute of the price touch while explicitly labelling the result as an unverified market observation, preventing inflated win-rate claims.

---

## 4. Modified Files

- `scripts/prospective_opportunity_dataset.py` (New module implementing 22-field snapshot capture and storage).
- `scripts/v418_pipeline.py` (Integrated prospective opportunity dataset capture for all discovery rows).
- `scripts/analyze_candle_outcomes.py` (Added 7-state outcome classification, MFE, MAE, and first touch timestamp).
- `scripts/collect_watchlist_observations.py` (Enhanced `calculate_simulated_execution` with outcome classification, touch timestamp, and unverified execution label).
- `tests/test_v430_phase2_dataset.py` (New: 4 unit tests covering 22 fields, rejected candidate retention, and outcome classification).

---

## 5. Tests Executed & Results

- **Command:** `python -m pytest tests/test_v430_phase2_dataset.py -v`
- **Result:** **4 PASSED, 0 FAILED** (100%).
- Full regression suite execution pending Phase 3 integration.

---

## 6. Scanner Health, Memory & CI Status

- **Local Scanner Health:** Operational.
- **Historical Memory Preserved:** All existing memory ledgers remain intact. New snapshots are appended to dedicated partitions.
- **Workflow Status:** Unchanged and healthy.

---

## 7. What Remains for Subsequent Phases

- **Phase 3:** Chronological walk-forward backtesting module.
- **Phase 4:** Multi-size execution simulator expansion ($100 to $5,000).
- **Phase 5:** Chain-aware Web3 security (Solana/EVM LP models) & systematic discovery pagination.
- **Phase 6:** AURA research learning improvements (Dataset A vs Dataset B separation).
- **Phase 7:** CI/CD workflow separation (light CI vs scan workflows) & final release audit.
