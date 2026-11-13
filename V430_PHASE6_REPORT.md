# RocketHunter v4.30.0 — Phase 6 Report: AURA Learning & Market Regime Intelligence

## Executive Summary
Phase 6 upgrades the AURA (Adaptive Universal Rocket Algorithm) learning engine with principled statistical methods, strictly decouples real user execution feedback (Dataset A) from prospective scanner observations (Dataset B), implements market regime classification, provides shadow model evaluation with automated rollback on regression, and cements hard safety vetoes preventing AURA conviction weights from overriding security or technical thresholds.

---

## Key Enhancements

### 1. Minimum Sample Threshold for Weight Adaptation
- **Problem**: Previously, 1–2 outlier trades (or lucky meme pumps) could disproportionately shift feature weights, causing overfitting and unstable ranking.
- **Solution**: Implemented `MIN_TRAINING_SAMPLES_FOR_WEIGHT_ADAPTATION = 5`. Feature attribution telemetry is calculated for exploratory analysis when $\ge 2$ samples are present, but active model weights remain firmly pinned to the robust quantitative prior baseline until at least 5 resolved trades are completed.

### 2. Regularized Empirical Covariance & Shrinkage to Prior Baseline
- Weight updates now apply shrinkage regularization toward the prior baseline:
  $$\text{shrinkage\_factor} = \min\left(1.0, \frac{N_{\text{trades}}}{25.0}\right)$$
  $$\text{learning\_rate} = \min\left(0.25, 0.05 \cdot \sqrt{N_{\text{trades}}}\right) \cdot \text{shrinkage\_factor}$$
- Adapted weights are bounded strictly within $[0.50, 1.20]$, preventing catastrophic weight collapse or explosive runaway multipliers.

### 3. Strict Decoupling of Dataset A and Dataset B
- **Dataset A (`get_dataset_a_user_trades`)**: Real, user-confirmed positions from `user_positions_journal.json` with actual entry, exit, and realized P/L.
- **Dataset B (`get_dataset_b_scanner_predictions`)**: Prospective scanner predictions with frozen decision-time features and independently monitored outcomes from `scanner-memory/prospective-opportunities/`.
- **Integrity**: Zero label or feature contamination between user trades and automated scanner opportunities.

### 4. Market Regime Classification
Implemented `classify_market_regime(metrics: dict)` to contextualize opportunity quality and prevent false breakouts during hostile macro liquidity environments:
- `LOW_LIQUIDITY`: Aggregate liquidity $< \$50,000$.
- `HIGH_VOLATILITY`: Market volatility or ATR-1h $\ge 12.0\%$.
- `BEARISH`: Benchmark BTC 24h change $\le -8.0\%$.
- `RISK_OFF`: Net ecosystem outflows with BTC $< -3.0\%$.
- `BULLISH`: Benchmark BTC 24h change $\ge +4.0\%$.
- `SIDEWAYS`: Range-bound consolidation.

### 5. Shadow Model Evaluation & Automatic Rollback
Implemented `evaluate_shadow_model(production_weights, candidate_weights, validation_records)`:
- Tests candidate model weights on an independent holdout set of validation trade records.
- Compares mean squared error (loss) against production weights.
- Emits `PROMOTE_CANDIDATE` only if candidate loss is strictly lower than production loss.
- Automatically triggers `ROLLBACK_TO_PRODUCTION` if candidate weights regress.

### 6. Strict Safety Veto Bounds (`enforce_aura_safety_veto`)
Explicitly enforced immutable safety boundaries:
1. **Contract Security**: AURA conviction weights cannot bypass honeypot flags, transfer tax $>10\%$, or critical security failures.
2. **Liquidity Locks**: Unlocked liquidity is an automatic veto regardless of AURA score.
3. **Take-Profit Minimum**: Technical take-profit target must be at least $+40.0\%$ above verified entry. AURA cannot lower or waive this requirement.

---

## Verification & Test Results
- Suite: `tests/test_v430_phase6_aura.py` (6 passed) + `tests/test_user_trade_learning.py` (5 passed)
- Full 11/11 tests passing with zero failures.
