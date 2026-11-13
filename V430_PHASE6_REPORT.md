# RocketHunter v4.30.0 — Phase 6 Report: AURA Learning & Market Regime Intelligence

## Executive Summary
Phase 6 upgrades the AURA (Adaptive Universal Rocket Algorithm) learning engine with principled statistical methods, strictly decouples real user execution feedback (Dataset A) from prospective scanner observations (Dataset B), implements market regime classification, provides shadow model evaluation with automated rollback on regression, and cements hard safety vetoes preventing AURA conviction weights from overriding security or technical thresholds.

---

## Key Enhancements

### 1. Production Ranking Weights Pinned to Baseline
- **Problem**: 1–5 user trades from the position journal cannot be treated as unbiased scanner prediction evidence. User positions reflect heavy post-selection and survivorship bias (assets the user chose to enter). Allowing 5 trades to shift production weights causes severe overfitting.
- **Solution**: Active production ranking weights are strictly pinned to immutable `PRODUCTION_BASELINE_WEIGHTS`. When training on user journal entries, learned weights are saved strictly as `candidate_learned_weights` and `shadow_weights`. Production ranking functions (`get_aura_production_weights`) strictly return baseline weights until rigorous out-of-sample promotion criteria are met.

### 2. Regularized Empirical Covariance & Candidate Weight Bounding
- Weight updates for candidate models apply shrinkage regularization toward the prior baseline:
  $$\text{shrinkage\_factor} = \min\left(1.0, \frac{N_{\text{trades}}}{25.0}\right)$$
  $$\text{learning\_rate} = \min\left(0.25, 0.05 \cdot \sqrt{N_{\text{trades}}}\right) \cdot \text{shrinkage\_factor}$$
- Candidate weights are bounded strictly within $[0.50, 1.20]$, preventing catastrophic weight collapse or explosive runaway multipliers.

### 3. Strict Decoupling of Dataset A and Dataset B
- **Dataset A (`get_dataset_a_user_trades`)**: Real, user-confirmed positions from `user_positions_journal.json` with actual entry, exit, and realized P/L. Represents post-selection user conviction.
- **Dataset B (`get_dataset_b_scanner_predictions`)**: Prospective scanner predictions with frozen decision-time features and independently monitored outcomes from `scanner-memory/prospective-opportunities/`. Represents unbiased scanner observations (including rejected candidates).
- **Integrity**: Zero label or feature contamination between user trades and automated scanner opportunities.

### 4. Market Regime Classification
Implemented `classify_market_regime(metrics: dict)` to contextualize opportunity quality and prevent false breakouts during hostile macro liquidity environments:
- `LOW_LIQUIDITY`: Aggregate liquidity $< \$50,000$.
- `HIGH_VOLATILITY`: Market volatility or ATR-1h $\ge 12.0\%$.
- `BEARISH`: Benchmark BTC 24h change $\le -8.0\%$.
- `RISK_OFF`: Net ecosystem outflows with BTC $< -3.0\%$.
- `BULLISH`: Benchmark BTC 24h change $\ge +4.0\%$.
- `SIDEWAYS`: Range-bound consolidation.

### 5. Rigorous Shadow Model Evaluation & Rollback
Implemented `evaluate_shadow_model(production_weights, candidate_weights, validation_records)` and `promote_or_rollback_model()`:
- Requires a minimum of **30 mature holdout records** from Dataset B before promotion can even be considered.
- Requires candidate loss to improve upon production loss by at least **2.0%** ($>0.02$).
- If validation records are insufficient ($<30$) or candidate does not demonstrate a 2%+ improvement, the evaluator automatically enforces `ROLLBACK_TO_PRODUCTION`.

### 6. Production Enforcement of Hard Safety Vetoes (`enforce_aura_safety_veto`)
Explicitly enforced immutable safety boundaries, wired directly into `qualify_final_actionable_candidates` and `refresh_and_filter_candidates`:
1. **Contract Security**: AURA conviction weights cannot bypass honeypot flags, transfer tax $>10\%$, or critical security failures.
2. **Liquidity Locks**: Unlocked liquidity is an automatic veto regardless of AURA score.
3. **Take-Profit Minimum**: Technical take-profit target must be at least $+40.0\%$ above verified entry. AURA cannot lower or waive this requirement.

---

## Verification & Test Results
- Suite: `tests/test_v430_phase6_aura.py` (6 passed) + `tests/test_user_trade_learning.py` (5 passed)
- Full 11/11 tests passing with zero failures.
