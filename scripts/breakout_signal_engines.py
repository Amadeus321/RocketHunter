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

BREAKOUT_SYSTEM_VERSION = "4.29.0"


def _num(val: Any) -> float | None:
    try:
        if val is None or val == "":
            return None
        v = float(val)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def _measurement(row: dict[str, Any], *keys: str) -> tuple[float | None, str]:
    stale_fields = row.get("evidence_stale_fields")
    if isinstance(stale_fields, (list, tuple, set)) and any(key in stale_fields for key in keys):
        return None, "STALE"

    supplied = [row.get(key) for key in keys if row.get(key) is not None and row.get(key) != ""]
    values = [_num(value) for value in supplied]
    if any(value is None for value in values):
        return None, "CONFLICTING"
    values = [value for value in values if value is not None]
    if not values:
        return None, "MISSING"
    if any(not math.isclose(value, values[0], rel_tol=1e-6, abs_tol=1e-9) for value in values[1:]):
        return None, "CONFLICTING"
    return values[0], "VERIFIED"


def _evidence_status(*measurements: tuple[float | None, str]) -> str:
    statuses = [status for _, status in measurements]
    if "CONFLICTING" in statuses:
        return "CONFLICTING"
    if "STALE" in statuses:
        return "STALE"
    present = sum(value is not None for value, _ in measurements)
    if present == 0:
        return "MISSING"
    if present < len(measurements):
        return "PARTIAL"
    return "VERIFIED"


def _unavailable_details(status: str, missing: list[str]) -> dict[str, Any]:
    return {
        "evidence_status": status,
        "missing_measurements": missing,
        "unmeasured": status in {"MISSING", "STALE", "CONFLICTING"},
    }


def _detail_coverage(details: dict[str, Any], measurement_count: int) -> float:
    status = details.get("evidence_status")
    if status == "VERIFIED":
        return 1.0
    if status != "PARTIAL":
        return 0.0
    missing = details.get("missing_measurements")
    if isinstance(missing, list):
        return max(0.0, (measurement_count - len(missing)) / measurement_count)
    return 0.5


class BreakoutSignalEngines:
    """Evaluates 9 independent signal engines to detect imminent 24-hour breakouts."""

    @staticmethod
    def engine_1_momentum_acceleration(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 1: Increasing price velocity before a major breakout."""
        p5m = _measurement(row, "change_5m_pct", "price_change_5m")
        p1h = _measurement(row, "change_1h_pct", "price_change_1h")
        p6h = _measurement(row, "change_6h_pct", "price_change_6h")
        status = _evidence_status(p5m, p1h, p6h)
        if status != "VERIFIED":
            return 0.0, _unavailable_details(status, ["5m_price_change", "1h_price_change", "6h_price_change"])
        p5, p1, p6 = p5m[0], p1h[0], p6h[0]

        # Velocity comparison: short rate vs medium rate
        v_short = p5 * 12.0  # Annualized to 1h scale
        v_med = p1
        v_slow = p6 / 6.0 if p6 else 0.0

        acceleration = max(0.0, v_short - v_med)
        rate_of_change = max(0.0, v_med - v_slow)

        score = min(100.0, (acceleration * 0.4) + (rate_of_change * 0.6) + (max(0.0, p1) * 0.8))
        details = {
            "evidence_status": status,
            "v_short_annualized": round(v_short, 2),
            "v_1h": round(v_med, 2),
            "acceleration_delta": round(acceleration, 2),
        }
        return round(score, 2), details

    @staticmethod
    def engine_2_volume_intelligence(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 2: Abnormal organic volume expansion."""
        v5m = _measurement(row, "volume_5m", "dexpaprika_5m_volume_usd")
        v1h = _measurement(row, "volume_1h", "dexpaprika_1h_volume_usd")
        v24h = _measurement(row, "volume_24h_usd", "volume_24h")
        liquidity = _measurement(row, "liquidity_usd", "best_liquidity_pool_usd")
        status = _evidence_status(v5m, v1h, v24h, liquidity)
        if status in {"MISSING", "STALE", "CONFLICTING"}:
            return 0.0, _unavailable_details(status, ["5m_volume", "1h_volume", "24h_volume", "liquidity"])
        v5, v1, v24, liq = v5m[0], v1h[0], v24h[0], liquidity[0]

        ratio_5m_1h = (v5 * 12.0 / v1) if v5 is not None and v1 is not None and v1 > 0 else None
        turnover_24h = v24 / max(1000.0, liq) if v24 is not None and liq is not None and liq > 0 else None

        score = 0.0
        if ratio_5m_1h is not None and ratio_5m_1h >= 3.0:
            score += 45.0
        elif ratio_5m_1h is not None and ratio_5m_1h >= 1.5:
            score += 25.0
        elif ratio_5m_1h is not None and ratio_5m_1h >= 1.0:
            score += 10.0

        if turnover_24h is not None and 0.5 <= turnover_24h <= 10.0:
            score += 35.0  # Healthy organic turnover
        elif turnover_24h is not None and turnover_24h > 10.0:
            score += 15.0  # Potential wash / hype

        if v24 is not None and v24 >= 50_000:
            score += 20.0
        elif v24 is not None and v24 >= 15_000:
            score += 10.0

        return round(min(100.0, score), 2), {
            "evidence_status": status,
            "missing_measurements": [
                name for name, measurement in (
                    ("5m_volume", v5m), ("1h_volume", v1h),
                    ("24h_volume", v24h), ("liquidity", liquidity),
                ) if measurement[0] is None
            ],
            "burst_ratio_5m_1h": round(ratio_5m_1h, 2) if ratio_5m_1h is not None else None,
            "turnover_24h": round(turnover_24h, 2) if turnover_24h is not None else None,
        }

    @staticmethod
    def engine_3_buy_sell_pressure(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 3: Strong buying pressure and positive cumulative volume delta (CVD)."""
        buy_usd = _measurement(row, "dexpaprika_1h_buy_usd")
        sell_usd = _measurement(row, "dexpaprika_1h_sell_usd")
        tx_buys = _measurement(row, "buys_24h")
        tx_sells = _measurement(row, "sells_24h")
        dollar_status = _evidence_status(buy_usd, sell_usd)
        count_status = _evidence_status(tx_buys, tx_sells)
        if "CONFLICTING" in (dollar_status, count_status):
            return 0.0, _unavailable_details("CONFLICTING", ["buy_sell_measurements"])
        if "STALE" in (dollar_status, count_status):
            return 0.0, _unavailable_details("STALE", ["buy_sell_measurements"])
        if dollar_status == "VERIFIED":
            buy_value, sell_value, evidence_status = buy_usd[0], sell_usd[0], "VERIFIED"
        elif count_status == "VERIFIED":
            buy_value, sell_value, evidence_status = tx_buys[0], tx_sells[0], "PARTIAL"
        else:
            return 0.0, _unavailable_details("MISSING", ["buy_sell_measurements"])
        if buy_value + sell_value <= 0:
            return 0.0, {
                "evidence_status": evidence_status,
                "taker_buy_share_pct": None,
                "note": "No measured trades in the selected window",
            }
        buy_share = (buy_value / (buy_value + sell_value)) * 100.0

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

        return round(score, 2), {
            "evidence_status": evidence_status,
            "taker_buy_share_pct": round(buy_share, 2),
        }

    @staticmethod
    def engine_4_liquidity_intelligence(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 4: Liquidity accumulation and sufficient exit depth cushion."""
        liq_measurement = _measurement(row, "liquidity_usd", "best_liquidity_pool_usd")
        fdv_measurement = _measurement(row, "fdv", "market_cap")
        if liq_measurement[0] is None or liq_measurement[1] in {"STALE", "CONFLICTING"}:
            return 0.0, _unavailable_details(liq_measurement[1], ["liquidity"])
        if fdv_measurement[1] in {"STALE", "CONFLICTING"}:
            return 0.0, _unavailable_details(fdv_measurement[1], ["fdv"])
        liq, fdv = liq_measurement[0], fdv_measurement[0]
        status = _evidence_status(liq_measurement, fdv_measurement)

        score = 0.0
        if liq >= 500_000:
            score = 95.0
        elif liq >= 100_000:
            score = 80.0 + (liq - 100_000) / 400_000 * 15.0
        elif liq >= 40_000:
            score = 60.0 + (liq - 40_000) / 60_000 * 20.0
        elif liq >= 15_000:
            score = 35.0
        elif liq > 0:
            score = 10.0

        liq_ratio = (liq / fdv * 100.0) if fdv is not None and fdv > 0 else None
        if liq_ratio is not None and 8.0 <= liq_ratio <= 35.0:
            score = min(100.0, score + 10.0)

        return round(score, 2), {
            "evidence_status": status,
            "missing_measurements": ["fdv"] if fdv is None else [],
            "liquidity_usd": round(liq, 2),
            "liquidity_to_fdv_pct": round(liq_ratio, 2) if liq_ratio is not None else None,
        }

    @staticmethod
    def engine_5_accumulation_detection(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 5: Consolidation, higher lows, and supply absorption before breakout."""
        chg24 = _measurement(row, "change_24h_pct", "change_24h")
        chg6 = _measurement(row, "change_6h_pct", "change_6h")
        chg1 = _measurement(row, "change_1h_pct", "change_1h")
        status = _evidence_status(chg24, chg6, chg1)
        if status != "VERIFIED":
            return 0.0, _unavailable_details(status, ["24h_price_change", "6h_price_change", "1h_price_change"])
        chg_24h, chg_6h, chg_1h = chg24[0], chg6[0], chg1[0]

        score = 50.0
        # Compression: 24h and 6h moves are modest, but 1h starts to lift
        if -10.0 <= chg_24h <= 25.0 and -5.0 <= chg_6h <= 15.0 and chg_1h > 2.0:
            score = 88.0  # Classic tight consolidation spring
        elif 0.0 <= chg_6h <= 30.0 and chg_1h >= chg_6h / 2.0:
            score = 75.0  # Higher lows structure
        elif chg_24h > 150.0:
            score = 25.0  # Already pumped, no longer accumulating

        return round(score, 2), {
            "evidence_status": status,
            "consolidation_range": abs(chg_24h),
            "is_tight_coil": bool(-10.0 <= chg_24h <= 25.0 and chg_1h > 2.0),
        }

    @staticmethod
    def engine_6_smart_money_research(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 6: Relevant wallet activity and holder distribution."""
        wallet_ev = row.get("wallet_evidence") or row.get("smart_money_wallets") or row.get("top_holders") or row.get("onchain_evidence")
        promo_flags = row.get("extra", {}).get("promo_flags") or row.get("promo_flags") or []

        if isinstance(wallet_ev, dict) and wallet_ev:
            smart = _measurement(wallet_ev, "smart_trader_count")
            top10 = _measurement(wallet_ev, "top_10_holders_share_pct")
            status = _evidence_status(smart, top10)
            if status in {"STALE", "CONFLICTING"}:
                return 0.0, _unavailable_details(status, ["wallet_metrics"])
            if smart[0] is not None and top10[0] is not None:
                smart_wallets, top10_share = smart[0], top10[0]
                if smart_wallets < 0 or not 0 <= top10_share <= 100:
                    return 0.0, _unavailable_details("CONFLICTING", ["wallet_metrics"])
                score = 60.0
                if smart_wallets >= 5:
                    score += 25.0
                elif smart_wallets >= 2:
                    score += 15.0
                if top10_share < 35.0:
                    score += 15.0
                elif top10_share > 75.0:
                    score -= 20.0
                return round(min(100.0, max(0.0, score)), 2), {
                    "evidence_status": "VERIFIED",
                    "smart_traders_tracked": smart_wallets,
                    "top_10_share_pct": top10_share,
                }
            if smart[0] is not None or top10[0] is not None:
                return 0.0, {
                    "evidence_status": "PARTIAL",
                    "missing_measurements": [
                        key for key, measurement in (
                            ("smart_trader_count", smart),
                            ("top_10_holders_share_pct", top10),
                        ) if measurement[0] is None
                    ],
                    "unmeasured": False,
                }

        # If only transaction counts are present: mark as PARTIAL proxy activity, NOT verified smart money
        txns = _measurement(row, "txns_24h")
        if txns[0] is None:
            buys = _measurement(row, "buys_24h")
            sells = _measurement(row, "sells_24h")
            if buys[0] is not None and sells[0] is not None:
                txns = (buys[0] + sells[0], "VERIFIED")
        if txns[0] is not None:
            if txns[0] < 0:
                return 0.0, _unavailable_details("CONFLICTING", ["transaction_count"])
            score = 50.0 if txns[0] > 0 else 0.0
            if txns[0] >= 1500:
                score += 20.0
            elif txns[0] >= 400:
                score += 10.0
            if "community_takeover" in promo_flags:
                score += 10.0
            return round(min(80.0, score), 2), {
                "evidence_status": "PARTIAL",
                "estimated_transactions": txns[0],
                "note": "Transaction activity proxy only; no verified wallet tracking",
            }

        return 0.0, _unavailable_details("MISSING", ["wallet_evidence", "transaction_count"])

    @staticmethod
    def engine_7_breakout_prediction(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 7: Imminent resistance breaks and volatility expansion."""
        potential = _measurement(row, "tp_upside_pct", "potential_pct")
        p5m = _measurement(row, "change_5m_pct", "price_change_5m")
        p1h = _measurement(row, "change_1h_pct", "price_change_1h")
        status = _evidence_status(potential, p5m, p1h)
        if status != "VERIFIED":
            return 0.0, _unavailable_details(status, ["single_tp_upside", "5m_price_change", "1h_price_change"])
        pot_pct, p5, p1 = potential[0], p5m[0], p1h[0]

        score = 50.0
        # Good setup: clean +40% target with fresh early liftoff (p1 between 3% and 25%)
        if pot_pct >= 40.0 and 3.0 <= p1 <= 28.0 and p5 > 0.5:
            score = 90.0
        elif pot_pct >= 40.0 and p1 > 0:
            score = 70.0
        elif p1 > 80.0:
            score = 30.0  # Late breakout chase

        return round(score, 2), {
            "evidence_status": status,
            "target_upside_pct": pot_pct,
            "impulse_1h": p1,
        }

    @staticmethod
    def engine_8_ecosystem_intelligence(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Engine 8: Network flow context, chain activity, and capital rotation."""
        chain = str(row.get("chain") or "").lower()
        flow = _measurement(row, "ecosystem_flow_score")
        corroboration = row.get("cross_provider_corroboration")
        providers = _measurement(corroboration, "provider_count") if isinstance(corroboration, dict) else (None, "MISSING")
        status = _evidence_status(flow, providers)
        if status in {"MISSING", "STALE", "CONFLICTING"}:
            return 0.0, _unavailable_details(status, ["ecosystem_flow", "provider_count"])
        flow_score, prov_cnt = flow[0], providers[0]

        score = min(100.0, (50.0 * flow_score if flow_score is not None else 0.0)
                    + (prov_cnt * 10.0 if prov_cnt is not None else 0.0))
        return round(score, 2), {
            "evidence_status": status,
            "missing_measurements": [
                name for name, measurement in (("ecosystem_flow", flow), ("provider_count", providers))
                if measurement[0] is None
            ],
            "chain": chain,
            "flow_multiplier": flow_score,
            "corroborating_providers": prov_cnt,
        }

    @staticmethod
    def engine_9_risk_intelligence(row: dict[str, Any]) -> tuple[float, list[str]]:
        """Engine 9: Wash trading, liquidity withdrawal, manipulation and contract risks."""
        risks: list[str] = []
        penalty = 0.0

        p24m = _measurement(row, "change_24h_pct", "change_24h")
        p1m = _measurement(row, "change_1h_pct", "change_1h")
        liqm = _measurement(row, "liquidity_usd", "best_liquidity_pool_usd")
        volm = _measurement(row, "volume_24h_usd", "volume_24h")
        if any(value is None for value, _ in (p24m, p1m, liqm, volm)):
            return 0.0, ["INSUFFICIENT_RISK_EVIDENCE"]
        if any(status in {"STALE", "CONFLICTING"} for _, status in (p24m, p1m, liqm, volm)):
            return 0.0, ["UNTRUSTED_RISK_EVIDENCE"]
        p24, p1, liq, vol = p24m[0], p1m[0], liqm[0], volm[0]

        if liq < 10_000:
            risks.append("EXTREME_THIN_LIQUIDITY_RUG_RISK")
            penalty += 40.0
        elif liq < 25_000:
            risks.append("THIN_LIQUIDITY_MICROCAP")
            penalty += 15.0

        if p24 > 150.0 or p1 > 80.0:
            risks.append("LATE_CHASE_EXHAUSTION_RISK")
            penalty += 25.0

        turnover = (vol / liq) if liq > 0 else 0.0
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

        # Evidence weights are renormalized over measurements actually present.
        weights = {
            "momentum_acceleration": 0.16,
            "volume_intelligence": 0.16,
            "buy_sell_pressure": 0.15,
            "liquidity_depth": 0.12,
            "accumulation_detection": 0.11,
            "smart_money": 0.08,
            "breakout_prediction": 0.12,
            "ecosystem_flow": 0.10,
        }

        engine_names = tuple(weights)
        engine_scores = (e1, e2, e3, e4, e5, e6, e7, e8)
        engine_details = (d1, d2, d3, d4, d5, d6, d7, d8)
        measurement_counts = (3, 4, 2, 2, 3, 2, 3, 2)
        evidence_status = {
            f"engine_{index}_{name}": detail.get("evidence_status", "MISSING")
            for index, (name, detail) in enumerate(zip(engine_names, engine_details), start=1)
        }
        risk_inputs = (
            _measurement(row, "change_24h_pct", "change_24h"),
            _measurement(row, "change_1h_pct", "change_1h"),
            _measurement(row, "liquidity_usd", "best_liquidity_pool_usd"),
            _measurement(row, "volume_24h_usd", "volume_24h"),
        )
        risk_evidence_status = _evidence_status(*risk_inputs)
        evidence_status["engine_9_risk_safety"] = risk_evidence_status
        measured_weight = 0.0
        weighted_score = 0.0
        for index, (name, score, detail, measurement_count, weight) in enumerate(
            zip(engine_names, engine_scores, engine_details, measurement_counts, weights.values()), start=1
        ):
            status = evidence_status[f"engine_{index}_{name}"]
            if status in {"VERIFIED", "PARTIAL"}:
                effective_weight = weight * _detail_coverage(detail, measurement_count)
                measured_weight += effective_weight
                weighted_score += score * effective_weight
        measured_confluence = weighted_score / measured_weight if measured_weight else 0.0
        evidence_coverage_pct = round(measured_weight * 100.0, 2)
        has_untrusted_evidence = any(
            status in {"STALE", "CONFLICTING"} for status in evidence_status.values()
        )
        evidence_eligible = (
            evidence_coverage_pct >= 75.0
            and risk_evidence_status == "VERIFIED"
            and not has_untrusted_evidence
        )
        final_confluence = measured_confluence if evidence_eligible else 0.0

        # Risk safety scaling
        safety_multiplier = e9 / 100.0 if risk_evidence_status == "VERIFIED" else 0.0
        final_confluence = round(final_confluence * safety_multiplier, 2)

        tier = "INSUFFICIENT_EVIDENCE" if not evidence_eligible else (
            "HIGH_BREAKOUT_CONFLUENCE" if final_confluence >= 75.0 else (
            "ACCELERATING_WATCHLIST" if final_confluence >= 60.0 else (
                "DEVELOPING_SETUP" if final_confluence >= 40.0 else "LOW_CONFLUENCE"
            ))
        )

        engine_scores = {
            "engine_1_momentum_acceleration": e1,
            "engine_2_volume_intelligence": e2,
            "engine_3_buy_sell_pressure": e3,
            "engine_4_liquidity_depth": e4,
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
            "measured_breakout_confluence_score": round(measured_confluence, 2),
            "evidence_coverage_pct": evidence_coverage_pct,
            "breakout_evidence_eligible": evidence_eligible,
            "evidence_status": "CONFLICTING" if "CONFLICTING" in evidence_status.values() else (
                "STALE" if "STALE" in evidence_status.values() else (
                    "VERIFIED" if evidence_eligible and evidence_coverage_pct == 100.0 else (
                        "PARTIAL" if measured_weight else "MISSING"
                    )
                )
            ),
            "engine_evidence_status": evidence_status,
            "confluence_tier": tier,
            "system_version": BREAKOUT_SYSTEM_VERSION,
            "engines": engine_scores,
            "engine_scores": engine_scores,
            "engine_details": engine_details,
            "acceleration_signals": engine_details,
            "risk_flags": risks,
        }
