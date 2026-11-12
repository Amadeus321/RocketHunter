#!/usr/bin/env python3
"""Probabilistic 24-Hour Expected Return, MFE & MAE Modeling Engine for Rocket Hunter.

Calculates realistic execution-aware outcomes rather than raw theoretical TP:
- Probability of reaching technical +40% target within 24 hours
- Expected net 24-hour return (E[R_24h]) factoring win probability & downside drawdowns
- Potential Maximum Favorable Excursion (MFE)
- Potential Maximum Adverse Excursion (MAE)
- Estimated time to target milestone
- Reversal and front-run risk score
- Evidence confidence & completeness index
"""
from __future__ import annotations

import math
from typing import Any

EXPECTED_RETURN_MODEL_VERSION = "4.28.0"
MANDATORY_MIN_TP_PCT = 40.00


def _clean_num(val: Any) -> float | None:
    try:
        if val is None or val == "":
            return None
        v = float(str(val).replace("%", "").replace("+", "").strip())
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


class ExpectedReturnEngine:
    """Computes risk-adjusted, execution-aware expected returns and excursion bands."""

    @staticmethod
    def estimate_outcomes(
        row: dict[str, Any],
        confluence_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compute the full 24-hour expected return and risk projection for a candidate."""
        # 1. Technical Target
        tp_raw = row.get("tp_upside_pct") or row.get("potential_pct") or row.get("target_upside_pct")
        tp_target_pct = _clean_num(tp_raw) or 40.0
        meets_mandatory_tp = tp_target_pct >= MANDATORY_MIN_TP_PCT

        # 2. Confluence & Velocity Inputs
        confluence_score = 50.0
        if confluence_profile:
            confluence_score = float(confluence_profile.get("breakout_confluence_score") or 50.0)

        p1 = _clean_num(row.get("change_1h_pct") or row.get("change_1h")) or 0.0
        p5 = _clean_num(row.get("change_5m_pct") or row.get("change_5m")) or 0.0
        p24 = _clean_num(row.get("change_24h_pct") or row.get("change_24h")) or 0.0
        liq = _clean_num(row.get("liquidity_usd") or row.get("best_liquidity_pool_usd")) or 25_000.0
        vol = _clean_num(row.get("volume_24h_usd") or row.get("volume_24h")) or 50_000.0

        # 3. Probability of reaching +40% target within 24 hours
        # Logit-inspired scaling grounded in confluence score and volatility
        base_logit = (confluence_score - 50.0) / 22.0
        # Target distance penalty: higher theoretical targets have lower baseline probability
        target_distance_penalty = max(0.0, (tp_target_pct - 40.0) / 75.0)
        # Liquidity bonus: deeper liquidity provides steadier trend continuation
        liquidity_factor = min(0.35, max(-0.5, math.log10(max(1000.0, liq) / 50_000.0) * 0.25))

        adjusted_logit = base_logit - target_distance_penalty + liquidity_factor
        prob_raw = 1.0 / (1.0 + math.exp(-adjusted_logit))
        # Clamp probability between 5% and 88% (no market setup is 100% certain)
        prob_tp_hit = round(max(0.05, min(0.88, prob_raw)) * 100.0, 1)

        # 4. Maximum Adverse Excursion (MAE) projection (Downside drawdown risk)
        # Higher recent volatility and lower liquidity expand potential drawdown
        base_drawdown = 8.0 + (abs(p1) * 0.2) + (abs(p24) * 0.05)
        if liq < 25_000:
            base_drawdown += 10.0
        elif liq > 250_000:
            base_drawdown = max(4.0, base_drawdown - 3.0)
        potential_mae_pct = round(-min(45.0, max(5.0, base_drawdown)), 2)

        # 5. Maximum Favorable Excursion (MFE) projection
        # Can overshoot target if momentum confluence is very high
        overshoot_ratio = 1.0 + (max(0.0, confluence_score - 60.0) / 100.0 * 0.4)
        potential_mfe_pct = round(tp_target_pct * overshoot_ratio, 2)

        # 6. Estimated Execution Drag (DEX slippage & price impact)
        # Depends inversely on pool liquidity depth
        slippage_drag_pct = 1.2 if liq >= 100_000 else (2.5 if liq >= 30_000 else 4.8)

        # 7. Expected Net 24-Hour Return
        # E[R] = P(TP) * Target - (1 - P(TP)) * |MAE| - Drag
        p_win = prob_tp_hit / 100.0
        p_loss = 1.0 - p_win
        expected_net_return = (p_win * tp_target_pct) - (p_loss * abs(potential_mae_pct)) - slippage_drag_pct
        expected_net_return_pct = round(expected_net_return, 2)

        # 8. Estimated Time to Target (hours)
        # Based on current hourly velocity and target distance
        hourly_velocity = max(1.5, p1 if p1 > 0 else 2.5)
        raw_hours = tp_target_pct / hourly_velocity
        estimated_hours = round(max(1.0, min(24.0, raw_hours)), 1)

        # 9. Reversal & Front-running Risk Score (0-100)
        reversal_risk = 20.0
        if p24 > 120.0:
            reversal_risk += 35.0  # Overextended 24h pump
        if p1 > 60.0:
            reversal_risk += 25.0  # Blow-off top risk
        if p5 < -1.5 and p1 > 10.0:
            reversal_risk += 20.0  # Short-window rejection
        reversal_risk_score = round(min(98.0, max(5.0, reversal_risk)), 1)

        # 10. Evidence Confidence & Completeness
        corroboration = row.get("cross_provider_corroboration") or {}
        prov_cnt = corroboration.get("provider_count") or 1
        has_volume = vol > 10_000
        has_liquidity = liq > 15_000
        confidence = 0.5 + (0.15 * min(3, prov_cnt)) + (0.1 if has_volume else 0.0) + (0.1 if has_liquidity else 0.0)
        evidence_confidence_pct = round(min(100.0, confidence * 100.0), 1)

        # 11. Realistic Opportunity Rank Score
        # Prioritizes high expected net return and win probability over speculative inflated TP
        rank_score = (
            (expected_net_return_pct * 0.40)
            + (prob_tp_hit * 0.35)
            + (confluence_score * 0.25)
            - (reversal_risk_score * 0.15)
        )
        opportunity_rank = round(max(0.0, min(100.0, rank_score)), 2)

        return {
            "model_version": EXPECTED_RETURN_MODEL_VERSION,
            "meets_mandatory_40pct_tp": meets_mandatory_tp,
            "technical_tp_potential_pct": tp_target_pct,
            "probability_tp_hit_24h_pct": prob_tp_hit,
            "expected_net_24h_return_pct": expected_net_return_pct,
            "potential_mfe_24h_pct": potential_mfe_pct,
            "potential_mae_24h_pct": potential_mae_pct,
            "estimated_hours_to_target": estimated_hours,
            "reversal_risk_score": reversal_risk_score,
            "evidence_confidence_pct": evidence_confidence_pct,
            "realistic_opportunity_rank": opportunity_rank,
            "execution_slippage_drag_pct": slippage_drag_pct,
        }
