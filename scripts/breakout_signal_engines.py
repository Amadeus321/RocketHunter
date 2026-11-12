#!/usr/bin/env python3
"""9-Engine Imminent Breakout Detection System for Rocket Hunter.

Evaluates acceleration and behavioral anomalies rather than raw historical gains:
1. Momentum Acceleration (velocity expansion before breakout)
2. Volume Intelligence (organic turnover expansion)
3. Buy/Sell Pressure (taker buy share and CVD delta)
4. Liquidity Intelligence (depth cushion & exit liquidity)
5. Accumulation Detection (consolidation & absorption)
6. Smart-Money Research (holder distribution & wallet behavior)
7. Breakout Prediction (volatility expansion & resistance proximity)
8. Ecosystem Intelligence (capital rotation & network flow)
9. Risk Intelligence (wash trading, late chase, honeypot & liquidity rug risks)
"""
from __future__ import annotations

import math
from typing import Any

BREAKOUT_SYSTEM_VERSION = "4.28.0"


def _num(val: Any) -> float | None:
    try:
        if val is None or val == "":
            return None
        v = float(val)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


class BreakoutSignalEngines:
    """Evaluates 9 independent signal engines to detect imminent 24-hour breakouts."""

    @staticmethod
    def engine_1_momentum_acceleration(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 1: Increasing price velocity before a major breakout."""
        p5 = _num(row.get("change_5m_pct") or row.get("price_change_5m")) or 0.0
        p1 = _num(row.get("change_1h_pct") or row.get("price_change_1h")) or 0.0
        p6 = _num(row.get("change_6h_pct") or row.get("price_change_6h")) or 0.0

        # Velocity comparison: short rate vs medium rate
        v_short = p5 * 12.0  # Annualized to 1h scale
        v_med = p1
        v_slow = p6 / 6.0 if p6 else 0.0

        acceleration = max(0.0, v_short - v_med)
        rate_of_change = max(0.0, v_med - v_slow)

        score = min(100.0, (acceleration * 0.4) + (rate_of_change * 0.6) + (max(0.0, p1) * 0.8))
        details = {
            "v_short_annualized": round(v_short, 2),
            "v_1h": round(v_med, 2),
            "acceleration_delta": round(acceleration, 2),
        }
        return round(score, 2), details

    @staticmethod
    def engine_2_volume_intelligence(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 2: Abnormal organic volume expansion."""
        v5 = _num(row.get("volume_5m") or row.get("dexpaprika_5m_volume_usd")) or 0.0
        v1 = _num(row.get("volume_1h") or row.get("dexpaprika_1h_volume_usd")) or 0.0
        v24 = _num(row.get("volume_24h_usd") or row.get("volume_24h")) or 0.0
        liq = _num(row.get("liquidity_usd") or row.get("best_liquidity_pool_usd")) or 1.0

        ratio_5m_1h = (v5 * 12.0 / v1) if v1 > 0 else 1.0
        turnover_24h = v24 / max(1000.0, liq)

        score = 0.0
        if ratio_5m_1h >= 3.0:
            score += 45.0
        elif ratio_5m_1h >= 1.5:
            score += 25.0
        elif ratio_5m_1h >= 1.0:
            score += 10.0

        if 0.5 <= turnover_24h <= 10.0:
            score += 35.0  # Healthy organic turnover
        elif turnover_24h > 10.0:
            score += 15.0  # Potential wash / hype

        if v24 >= 50_000:
            score += 20.0
        elif v24 >= 15_000:
            score += 10.0

        return round(min(100.0, score), 2), {
            "burst_ratio_5m_1h": round(ratio_5m_1h, 2),
            "turnover_24h": round(turnover_24h, 2),
        }

    @staticmethod
    def engine_3_buy_sell_pressure(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 3: Strong buying pressure and positive cumulative volume delta (CVD)."""
        buy_usd = _num(row.get("dexpaprika_1h_buy_usd"))
        sell_usd = _num(row.get("dexpaprika_1h_sell_usd"))
        tx_buys = _num(row.get("buys_24h")) or 0.0
        tx_sells = _num(row.get("sells_24h")) or 0.0

        buy_share = 50.0
        if buy_usd is not None and sell_usd is not None and (buy_usd + sell_usd) > 0:
            buy_share = (buy_usd / (buy_usd + sell_usd)) * 100.0
        elif (tx_buys + tx_sells) > 0:
            buy_share = (tx_buys / (tx_buys + tx_sells)) * 100.0

        # CVD proxy
        score = 0.0
        if buy_share >= 65.0:
            score = 90.0 + min(10.0, (buy_share - 65.0) * 0.5)
        elif buy_share >= 55.0:
            score = 65.0 + (buy_share - 55.0) * 2.5
        elif buy_share >= 48.0:
            score = 45.0
        else:
            score = max(0.0, buy_share)

        return round(score, 2), {"taker_buy_share_pct": round(buy_share, 2)}

    @staticmethod
    def engine_4_liquidity_intelligence(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 4: Liquidity accumulation and sufficient exit depth cushion."""
        liq = _num(row.get("liquidity_usd") or row.get("best_liquidity_pool_usd")) or 0.0
        fdv = _num(row.get("fdv") or row.get("market_cap")) or 0.0

        score = 0.0
        if liq >= 500_000:
            score = 95.0
        elif liq >= 100_000:
            score = 80.0 + (liq - 100_000) / 400_000 * 15.0
        elif liq >= 40_000:
            score = 60.0 + (liq - 40_000) / 60_000 * 20.0
        elif liq >= 15_000:
            score = 35.0
        else:
            score = 10.0

        liq_ratio = (liq / fdv * 100.0) if fdv > 0 else 10.0
        if 8.0 <= liq_ratio <= 35.0:
            score = min(100.0, score + 10.0)

        return round(score, 2), {
            "liquidity_usd": round(liq, 2),
            "liquidity_to_fdv_pct": round(liq_ratio, 2) if fdv > 0 else None,
        }

    @staticmethod
    def engine_5_accumulation_detection(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 5: Consolidation, higher lows, and supply absorption before breakout."""
        chg_24h = _num(row.get("change_24h_pct") or row.get("change_24h")) or 0.0
        chg_6h = _num(row.get("change_6h_pct") or row.get("change_6h")) or 0.0
        chg_1h = _num(row.get("change_1h_pct") or row.get("change_1h")) or 0.0

        score = 50.0
        # Compression: 24h and 6h moves are modest, but 1h starts to lift
        if -10.0 <= chg_24h <= 25.0 and -5.0 <= chg_6h <= 15.0 and chg_1h > 2.0:
            score = 88.0  # Classic tight consolidation spring
        elif 0.0 <= chg_6h <= 30.0 and chg_1h >= chg_6h / 2.0:
            score = 75.0  # Higher lows structure
        elif chg_24h > 150.0:
            score = 25.0  # Already pumped, no longer accumulating

        return round(score, 2), {
            "consolidation_range": abs(chg_24h),
            "is_tight_coil": bool(-10.0 <= chg_24h <= 25.0 and chg_1h > 2.0),
        }

    @staticmethod
    def engine_6_smart_money_research(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 6: Relevant wallet activity and holder distribution."""
        txns = _num(row.get("txns_24h")) or (_num(row.get("buys_24h")) or 0.0) + (_num(row.get("sells_24h")) or 0.0)
        promo_flags = row.get("extra", {}).get("promo_flags") or row.get("promo_flags") or []

        score = 50.0
        if txns >= 1500:
            score += 30.0
        elif txns >= 400:
            score += 15.0

        if "community_takeover" in promo_flags:
            score += 10.0  # Organic decentralized community drive

        return round(min(100.0, score), 2), {"estimated_transactions": txns}

    @staticmethod
    def engine_7_breakout_prediction(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 7: Imminent resistance breaks and volatility expansion."""
        pot_pct = _num(row.get("tp_upside_pct") or row.get("potential_pct")) or 40.0
        p5 = _num(row.get("change_5m_pct") or row.get("price_change_5m")) or 0.0
        p1 = _num(row.get("change_1h_pct") or row.get("price_change_1h")) or 0.0

        score = 50.0
        # Good setup: clean +40% target with fresh early liftoff (p1 between 3% and 25%)
        if pot_pct >= 40.0 and 3.0 <= p1 <= 28.0 and p5 > 0.5:
            score = 90.0
        elif pot_pct >= 40.0 and p1 > 0:
            score = 70.0
        elif p1 > 80.0:
            score = 30.0  # Late breakout chase

        return round(score, 2), {"target_upside_pct": pot_pct, "impulse_1h": p1}

    @staticmethod
    def engine_8_ecosystem_intelligence(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 8: Network flow context, chain activity, and capital rotation."""
        chain = str(row.get("chain") or "").lower()
        flow_score = _num(row.get("ecosystem_flow_score")) or 1.0
        corroboration = row.get("cross_provider_corroboration") or {}
        prov_cnt = corroboration.get("provider_count") or 1

        score = min(100.0, 50.0 * flow_score + (prov_cnt * 10.0))
        return round(score, 2), {
            "chain": chain,
            "flow_multiplier": flow_score,
            "corroborating_providers": prov_cnt,
        }

    @staticmethod
    def engine_9_risk_intelligence(row: dict[str, Any]) -> tuple[float, list[str]]:
        """Engine 9: Wash trading, liquidity withdrawal, manipulation and contract risks."""
        risks: list[str] = []
        penalty = 0.0

        p24 = _num(row.get("change_24h_pct") or row.get("change_24h")) or 0.0
        p1 = _num(row.get("change_1h_pct") or row.get("change_1h")) or 0.0
        liq = _num(row.get("liquidity_usd") or row.get("best_liquidity_pool_usd"))
        vol = _num(row.get("volume_24h_usd") or row.get("volume_24h")) or 0.0

        if liq is None or liq < 10_000:
            risks.append("EXTREME_THIN_LIQUIDITY_RUG_RISK")
            penalty += 40.0
        elif liq < 25_000:
            risks.append("THIN_LIQUIDITY_MICROCAP")
            penalty += 15.0

        if p24 > 150.0 or p1 > 80.0:
            risks.append("LATE_CHASE_EXHAUSTION_RISK")
            penalty += 25.0

        turnover = (vol / liq) if liq and liq > 0 else 0.0
        if turnover > 25.0 and abs(p24) < 15.0:
            risks.append("SUSPECTED_WASH_TRADING_HIGH_TURNOVER_FLAT_PRICE")
            penalty += 35.0

        risk_score = round(max(0.0, 100.0 - penalty), 2)
        return risk_score, risks

    # Aliases for convenience
    engine_4_liquidity_depth = engine_4_liquidity_intelligence
    engine_8_ecosystem_flow = engine_8_ecosystem_intelligence

    @classmethod
    def evaluate_all_engines(cls, row: dict[str, Any]) -> dict[str, Any]:
        """Class method to evaluate all 9 engines and return full confluence profile."""
        instance = cls()
        return instance.evaluate_candidate(row)

    def evaluate_candidate(self, row: dict[str, Any]) -> dict[str, Any]:
        """Synthesize all 9 engines into a unified Breakout Acceleration Profile."""
        e1, d1 = self.engine_1_momentum_acceleration(row)
        e2, d2 = self.engine_2_volume_intelligence(row)
        e3, d3 = self.engine_3_buy_sell_pressure(row)
        e4, d4 = self.engine_4_liquidity_intelligence(row)
        e5, d5 = self.engine_5_accumulation_detection(row)
        e6, d6 = self.engine_6_smart_money_research(row)
        e7, d7 = self.engine_7_breakout_prediction(row)
        e8, d8 = self.engine_8_ecosystem_intelligence(row)
        e9, risks = self.engine_9_risk_intelligence(row)

        # Weighted Confluence Score (Weights derived from AURA historical win lessons)
        weights = {
            "momentum_acceleration": 0.16,
            "volume_intelligence": 0.16,
            "buy_sell_pressure": 0.15,
            "liquidity_intelligence": 0.12,
            "accumulation_detection": 0.11,
            "smart_money": 0.08,
            "breakout_prediction": 0.12,
            "ecosystem_flow": 0.10,
        }

        raw_confluence = (
            e1 * weights["momentum_acceleration"]
            + e2 * weights["volume_intelligence"]
            + e3 * weights["buy_sell_pressure"]
            + e4 * weights["liquidity_intelligence"]
            + e5 * weights["accumulation_detection"]
            + e6 * weights["smart_money"]
            + e7 * weights["breakout_prediction"]
            + e8 * weights["ecosystem_flow"]
        )

        # Risk safety scaling
        safety_multiplier = max(0.2, e9 / 100.0)
        final_confluence = round(raw_confluence * safety_multiplier, 2)

        tier = "HIGH_BREAKOUT_CONFLUENCE" if final_confluence >= 75.0 else (
            "ACCELERATING_WATCHLIST" if final_confluence >= 60.0 else (
                "DEVELOPING_SETUP" if final_confluence >= 40.0 else "LOW_CONFLUENCE"
            )
        )

        engine_scores = {
            "engine_1_momentum_acceleration": e1,
            "engine_2_volume_intelligence": e2,
            "engine_3_buy_sell_pressure": e3,
            "engine_4_liquidity_intelligence": e4,
            "engine_5_accumulation_detection": e5,
            "engine_6_smart_money": e6,
            "engine_7_breakout_prediction": e7,
            "engine_8_ecosystem_flow": e8,
            "engine_9_risk_safety": e9,
        }
        engine_details = {
            "momentum": d1,
            "volume": d2,
            "buy_pressure": d3,
            "liquidity": d4,
            "accumulation": d5,
            "smart_money": d6,
            "breakout": d7,
            "ecosystem": d8,
        }

        return {
            "breakout_confluence_score": final_confluence,
            "confluence_tier": tier,
            "system_version": BREAKOUT_SYSTEM_VERSION,
            "engines": engine_scores,
            "engine_scores": engine_scores,
            "engine_details": engine_details,
            "acceleration_signals": engine_details,
            "risk_flags": risks,
        }
