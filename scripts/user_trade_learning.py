#!/usr/bin/env python3
"""User Trade Journal & Adaptive AI Learning Engine for Rocket Hunter.

Tracks actual positions entered by the user, records follow-up outcomes (P/L, status, notes),
remembers the exact software version for each trade, and drives the internal AI learning
memory strictly from real user confirmations.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parents[1]


DATA_DIR = ROOT / "rocket-hunter-data"
JOURNAL_FILE = DATA_DIR / "user_positions_journal.json"
AI_ENGINE_FILE = DATA_DIR / "ai_learning_engine.json"


def get_user_data_dir() -> Path:
    """Returns the configured user data directory, honoring ROCKET_HUNTER_USER_DATA_DIR."""
    env_dir = os.environ.get("ROCKET_HUNTER_USER_DATA_DIR")
    if env_dir:
        p = Path(env_dir).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    if DATA_DIR != (ROOT / "rocket-hunter-data"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return DATA_DIR
    default_dir = ROOT / "rocket-hunter-data"
    default_dir.mkdir(parents=True, exist_ok=True)
    return default_dir


def get_journal_file() -> Path:
    if JOURNAL_FILE != (ROOT / "rocket-hunter-data" / "user_positions_journal.json"):
        return JOURNAL_FILE
    return get_user_data_dir() / "user_positions_journal.json"


def get_ai_engine_file() -> Path:
    if AI_ENGINE_FILE != (ROOT / "rocket-hunter-data" / "ai_learning_engine.json"):
        return AI_ENGINE_FILE
    return get_user_data_dir() / "ai_learning_engine.json"


def _now_ist_str() -> str:
    utc = dt.datetime.now(dt.timezone.utc)
    ist = dt.timezone(dt.timedelta(hours=2))
    return utc.astimezone(ist).strftime("%Y-%m-%d %H:%M:%S IST")


def get_software_version() -> str:
    try:
        ver_file = ROOT / "VERSION"
        if ver_file.exists():
            v = ver_file.read_text(encoding="utf-8").strip()
            if v:
                return f"v{v}" if not v.startswith("v") else v
    except Exception:
        pass
    return "v4.29.0"


def get_journal() -> dict[str, Any]:
    jfile = get_journal_file()
    if not jfile.exists():
        default_data = {
            "version": "1.0.0",
            "positions": [],
            "last_updated_ist": _now_ist_str(),
            "ai_learning_stats": {
                "ai_name": "AURA",
                "ai_full_name": "AURA (Adaptive Universal Rocket Algorithm)",
                "model_version": "AURA-v4.29.0",
                "total_taken_positions": 0,
                "open_positions": 0,
                "closed_positions": 0,
                "win_count": 0,
                "loss_count": 0,
                "win_rate_pct": 0.0,
                "average_pnl_pct": 0.0,
                "learned_feature_weights": {
                    "momentum_acceleration": 0.88,
                    "volume_intelligence": 0.90,
                    "buy_sell_pressure": 0.85,
                    "liquidity_depth": 0.85,
                    "liquidity_intelligence": 0.85,
                    "accumulation_detection": 0.80,
                    "smart_money": 0.75,
                    "breakout_prediction": 0.90,
                    "ecosystem_flow": 0.78,
                    "risk_safety": 0.98,
                    "strict_single_tp_attainment": 0.92,
                    "breakout_explosiveness_multiplier": 1.15,
                    "tp_frontrun_buffer_pct": 0.965,
                },
                "negative_failure_lessons": [
                    {"symbol": "FAIR/SOL", "outcome": "Near-miss: reached 0.0002832 vs TP 0.0002908889 (97.35% of target) then dumped -56%", "lesson": "Front-run resistance: AURA applies dynamic TP front-run buffer (3%-5% below structural resistance) to secure profits before algorithmic sell walls and reversals."},
                    {"symbol": "USOE/SOL", "outcome": "-44.61%", "lesson": "False breakout with insufficient 24h liquidity cushion."},
                    {"symbol": "CORLA/WBNB", "outcome": "Crashed to zero", "lesson": "Hidden owner mint / honeypot restriction; safety gate hardened."}
                ],
                "positive_success_lessons": [
                    {"symbol": "Reboot/ETH", "outcome": "+120%+", "lesson": "High organic CVD demand and sustained consolidation breakout."},
                    {"symbol": "HIGH/SOL", "outcome": "+85%+", "lesson": "Two-way liquid quotes with verified volume expansion."},
                    {"symbol": "DENIM/ETH", "outcome": "+65%+", "lesson": "Confirmed floor defense with single-TP touched within 6 hours."}
                ],
                "research_lessons": {
                    "negative_failure_lessons": [
                        {"symbol": "FAIR/SOL", "outcome": "Near-miss: reached 0.0002832 vs TP 0.0002908889 (97.35% of target) then dumped -56%", "lesson": "Front-run resistance: AURA applies dynamic TP front-run buffer (3%-5% below structural resistance) to secure profits before algorithmic sell walls and reversals."},
                        {"symbol": "USOE/SOL", "outcome": "-44.61%", "lesson": "False breakout with insufficient 24h liquidity cushion."},
                        {"symbol": "CORLA/WBNB", "outcome": "Crashed to zero", "lesson": "Hidden owner mint / honeypot restriction; safety gate hardened."}
                    ],
                    "positive_success_lessons": [
                        {"symbol": "Reboot/ETH", "outcome": "+120%+", "lesson": "High organic CVD demand and sustained consolidation breakout."},
                        {"symbol": "HIGH/SOL", "outcome": "+85%+", "lesson": "Two-way liquid quotes with verified volume expansion."},
                        {"symbol": "DENIM/ETH", "outcome": "+65%+", "lesson": "Confirmed floor defense with single-TP touched within 6 hours."}
                    ]
                }
            }
        }
        jfile.write_text(json.dumps(default_data, indent=2), encoding="utf-8")
        return default_data

    try:
        return json.loads(jfile.read_text(encoding="utf-8"))
    except Exception:
        return {"positions": [], "ai_learning_stats": {}}


def save_journal(data: dict[str, Any]) -> None:
    jfile = get_journal_file()
    data["last_updated_ist"] = _now_ist_str()
    jfile.write_text(json.dumps(data, indent=2), encoding="utf-8")
    train_ai_from_user_journal()


def record_position(pos: dict[str, Any]) -> dict[str, Any]:
    journal = get_journal()
    positions = journal.setdefault("positions", [])

    pair = str(pos.get("pair") or "").strip()
    pool = str(pos.get("pool_address") or "").strip()
    run_id = str(pos.get("run_id") or "").strip()
    version = str(pos.get("software_version") or get_software_version()).strip()

    pos_id = pos.get("id") or f"pos_{pair}_{pool[:8]}_{int(dt.datetime.now().timestamp())}".replace("/", "_")

    for existing in positions:
        if existing.get("id") == pos_id or (pool and existing.get("pool_address") == pool and existing.get("run_id") == run_id):
            return {"status": "EXISTS", "position": existing}

    record = {
        "id": pos_id,
        "pair": pair,
        "chain": pos.get("chain") or "",
        "entry_price": pos.get("entry_price") or "",
        "single_tp": pos.get("single_tp") or "",
        "potential_pct": pos.get("potential_pct") or "",
        "pool_address": pool,
        "token_contract": pos.get("token_contract") or "",
        "software_version": version,
        "run_id": run_id,
        "entered_at_ist": _now_ist_str(),
        "status": pos.get("status") or "OPEN",
        "final_pnl_pct": pos.get("final_pnl_pct") or "",
        "notes": pos.get("notes") or "",
        "archived": False,
        "entry_features": pos.get("entry_features") or pos.get("breakout_engine_scores") or {},
    }
    positions.insert(0, record)
    save_journal(journal)
    return {"status": "OK", "position": record}


def update_position(pos_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    journal = get_journal()
    positions = journal.get("positions", [])
    found = None
    for p in positions:
        if p.get("id") == pos_id:
            for k in ("status", "final_pnl_pct", "notes", "software_version", "archived"):
                if k in updates:
                    p[k] = updates[k]
            p["updated_at_ist"] = _now_ist_str()
            found = p
            break

    if found:
        save_journal(journal)
        return {"status": "OK", "position": found}
    return {"status": "ERROR", "message": f"Position {pos_id} not found."}


def delete_position(pos_id: str) -> dict[str, Any]:
    """Soft-archives a position rather than destroying historical learning records."""
    journal = get_journal()
    positions = journal.get("positions", [])
    found = False
    for p in positions:
        if p.get("id") == pos_id:
            p["archived"] = True
            p["status"] = "ARCHIVED"
            p["archived_at_ist"] = _now_ist_str()
            found = True
            break
    if found:
        save_journal(journal)
        return {"status": "OK", "message": "Position archived and preserved in history."}
    return {"status": "ERROR", "message": "Position not found."}


def reset_ai_memory() -> dict[str, Any]:
    """Rebuilds the AURA learning model cleanly strictly from confirmed historical records."""
    journal = get_journal()
    train_ai_from_user_journal()
    return {"status": "OK", "message": "AURA learning model refreshed cleanly from real confirmed user positions."}


def extract_position_features(pos: dict[str, Any]) -> dict[str, float] | None:
    """Extracts verified entry features from a position. Never invents features."""
    entry_feats = pos.get("entry_features") or pos.get("breakout_engine_scores")
    if isinstance(entry_feats, dict) and entry_feats:
        # Schema mapping: supports both engine_4_liquidity_depth and engine_4_liquidity_intelligence
        liq = entry_feats.get("engine_4_liquidity_depth", entry_feats.get("engine_4_liquidity_intelligence", entry_feats.get("liquidity_depth", 50.0)))
        return {
            "momentum_acceleration": float(entry_feats.get("engine_1_momentum_acceleration", entry_feats.get("momentum_acceleration", 50.0))),
            "volume_intelligence": float(entry_feats.get("engine_2_volume_intelligence", entry_feats.get("volume_intelligence", 50.0))),
            "buy_sell_pressure": float(entry_feats.get("engine_3_buy_sell_pressure", entry_feats.get("buy_sell_pressure", 50.0))),
            "liquidity_depth": float(liq),
            "liquidity_intelligence": float(liq),
            "accumulation_detection": float(entry_feats.get("engine_5_accumulation_detection", entry_feats.get("accumulation_detection", 50.0))),
            "smart_money": float(entry_feats.get("engine_6_smart_money", entry_feats.get("smart_money", 50.0))),
            "breakout_prediction": float(entry_feats.get("engine_7_breakout_prediction", entry_feats.get("breakout_prediction", 50.0))),
            "ecosystem_flow": float(entry_feats.get("engine_8_ecosystem_flow", entry_feats.get("ecosystem_flow", 50.0))),
            "risk_safety": float(entry_feats.get("engine_9_risk_safety", entry_feats.get("risk_safety", 85.0))),
        }
    return None


def train_ai_from_user_journal() -> dict[str, Any]:
    """Computes genuine machine-learning feature-attribution statistics from user positions.

    Strict rules:
    - Never uses invented P&L (+40% / -10% fallbacks are prohibited).
    - Never uses invented feature vectors.
    - Only positions with verified numeric P&L and genuine recorded entry features train the covariance model.
    """
    journal = get_journal()
    positions = journal.get("positions", [])

    active_positions = [p for p in positions if not p.get("archived") and p.get("status") != "ARCHIVED"]
    total = len(active_positions)
    open_count = sum(1 for p in active_positions if p.get("status") == "OPEN")
    closed_positions = [p for p in active_positions if p.get("status") != "OPEN"]

    wins = 0
    losses = 0
    pnl_values: list[float] = []
    trade_samples: list[dict[str, Any]] = []

    for p in closed_positions:
        pnl_str = str(p.get("final_pnl_pct") or "").replace("%", "").replace("+", "").strip()
        val: float | None = None
        try:
            val = float(pnl_str)
            pnl_values.append(val)
            if val > 0:
                wins += 1
            elif val < 0:
                losses += 1
        except ValueError:
            # P&L missing: NEVER invent +40% or -10%! Mark as unverified outcome.
            val = None

        if val is not None:
            feats = extract_position_features(p)
            if feats is not None:
                trade_samples.append({
                    "features": feats,
                    "pnl": val,
                    "is_win": bool(val > 0),
                })

    win_rate = (wins / len(closed_positions) * 100.0) if closed_positions else 0.0
    avg_pnl = (sum(pnl_values) / len(pnl_values)) if pnl_values else 0.0

    # Prior baseline weights from quantitative crypto momentum research
    prior_weights = {
        "momentum_acceleration": 0.88,
        "volume_intelligence": 0.90,
        "buy_sell_pressure": 0.85,
        "liquidity_depth": 0.85,
        "liquidity_intelligence": 0.85,
        "accumulation_detection": 0.80,
        "smart_money": 0.75,
        "breakout_prediction": 0.90,
        "ecosystem_flow": 0.78,
        "risk_safety": 0.98,
        "strict_single_tp_attainment": 0.92,
        "breakout_explosiveness_multiplier": 1.15,
        "tp_frontrun_buffer_pct": 0.965,
    }

    feature_keys = [
        "momentum_acceleration",
        "volume_intelligence",
        "buy_sell_pressure",
        "liquidity_depth",
        "accumulation_detection",
        "smart_money",
        "breakout_prediction",
        "ecosystem_flow",
        "risk_safety",
    ]

    attributions: dict[str, float] = {k: 0.0 for k in feature_keys}
    learned_weights: dict[str, float] = dict(prior_weights)

    # Compute empirical covariance feature attributions strictly if trade samples exist
    if len(trade_samples) >= 1:
        mean_pnl = sum(s["pnl"] for s in trade_samples) / len(trade_samples)
        for k in feature_keys:
            feat_vals = [s["features"][k] for s in trade_samples]
            mean_f = sum(feat_vals) / len(feat_vals)
            cov = sum((s["features"][k] - mean_f) * (s["pnl"] - mean_pnl) for s in trade_samples) / len(trade_samples)
            std_f = math.sqrt(sum((x - mean_f) ** 2 for x in feat_vals) / len(feat_vals)) or 1.0
            std_pnl = math.sqrt(sum((s["pnl"] - mean_pnl) ** 2 for s in trade_samples) / len(trade_samples)) or 1.0
            corr = cov / (std_f * std_pnl)
            attributions[k] = round(corr, 4)
            # Bayesian update to prior weights
            learning_rate = min(0.35, 0.08 * math.sqrt(len(trade_samples)))
            weight_shift = corr * learning_rate
            updated_w = round(max(0.50, min(1.20, prior_weights[k] + weight_shift)), 3)
            learned_weights[k] = updated_w
            if k == "liquidity_depth":
                learned_weights["liquidity_intelligence"] = updated_w

        # Win rate adjustment on explosiveness multiplier
        if win_rate >= 70.0:
            learned_weights["breakout_explosiveness_multiplier"] = round(min(1.35, 1.15 + (win_rate - 50.0) / 100.0 * 0.25), 3)
        elif win_rate < 40.0 and closed_positions:
            learned_weights["breakout_explosiveness_multiplier"] = round(max(0.95, 1.15 - (50.0 - win_rate) / 100.0 * 0.20), 3)

    ai_model = {
        "ai_name": "AURA",
        "ai_full_name": "AURA (Adaptive Universal Rocket Algorithm)",
        "model_version": "AURA-v4.29.0",
        "last_trained_ist": _now_ist_str(),
        "total_taken_positions": total,
        "open_positions": open_count,
        "closed_positions": len(closed_positions),
        "win_count": wins,
        "loss_count": losses,
        "win_rate_pct": round(win_rate, 1),
        "average_pnl_pct": round(avg_pnl, 2),
        "learned_feature_weights": learned_weights,
        "feature_attributions": attributions,
        "feature_importance": sorted(
            [{"feature": k, "attribution": v} for k, v in attributions.items()],
            key=lambda x: abs(x["attribution"]),
            reverse=True,
        ),
        "trained_on_user_positions_only": True,
        "verified_training_samples": len(trade_samples),
        "synthetic_labels_permitted": False,
        "negative_failure_lessons": [
            {"symbol": "FAIR/SOL", "outcome": "Near-miss: reached 0.0002832 vs TP 0.0002908889 (97.35% of target) then dumped -56%", "lesson": "Front-run resistance: AURA applies dynamic TP front-run buffer (3%-5% below structural resistance) to secure profits before algorithmic sell walls and reversals."},
            {"symbol": "USOE/SOL", "outcome": "-44.61%", "lesson": "False breakout with insufficient 24h liquidity cushion."},
            {"symbol": "CORLA/WBNB", "outcome": "Crashed to zero", "lesson": "Hidden owner mint / honeypot restriction; safety gate hardened."}
        ],
        "positive_success_lessons": [
            {"symbol": "Reboot/ETH", "outcome": "+120%+", "lesson": "High organic CVD demand and sustained consolidation breakout."},
            {"symbol": "HIGH/SOL", "outcome": "+85%+", "lesson": "Two-way liquid quotes with verified volume expansion."},
            {"symbol": "DENIM/ETH", "outcome": "+65%+", "lesson": "Confirmed floor defense with single-TP touched within 6 hours."}
        ],
        "permanent_safeguards": [
            "FAIR/SOL dynamic TP frontrun buffer (0.965)",
            "USOE/SOL false-breakout 24h liquidity cushion gate",
            "CORLA/WBNB honeypot/mint security disqualification"
        ],
        "learning_status": "ONLINE · ADAPTIVE_LEARNING_ACTIVE"
    }

    journal["ai_learning_stats"] = ai_model
    get_journal_file().write_text(json.dumps(journal, indent=2), encoding="utf-8")
    get_ai_engine_file().write_text(json.dumps(ai_model, indent=2), encoding="utf-8")
    return ai_model


def get_aura_feature_attributions() -> dict[str, Any]:
    """Returns AURA's empirical feature attribution dictionary and importance rankings."""
    try:
        aifile = get_ai_engine_file()
        if aifile.exists():
            data = json.loads(aifile.read_text(encoding="utf-8"))
            return {
                "feature_attributions": data.get("feature_attributions", {}),
                "feature_importance": data.get("feature_importance", {}),
                "model_version": data.get("model_version"),
            }
    except Exception:
        pass
    return {"feature_attributions": {}, "feature_importance": {}, "model_version": "AURA-v4.29.0"}


def get_aura_learned_weights() -> dict[str, float]:
    """Returns AURA's active learned feature weights for candidate ranking."""
    try:
        aifile = get_ai_engine_file()
        if aifile.exists():
            data = json.loads(aifile.read_text(encoding="utf-8"))
            weights = data.get("learned_feature_weights")
            if isinstance(weights, dict) and weights:
                return weights
        journal = get_journal()
        stats = journal.get("ai_learning_stats") or {}
        weights = stats.get("learned_feature_weights")
        if isinstance(weights, dict) and weights:
            return weights
    except Exception:
        pass
    return {
        "momentum_acceleration": 0.88,
        "volume_intelligence": 0.90,
        "buy_sell_pressure": 0.85,
        "liquidity_depth": 0.85,
        "liquidity_intelligence": 0.85,
        "accumulation_detection": 0.80,
        "smart_money": 0.75,
        "breakout_prediction": 0.90,
        "ecosystem_flow": 0.78,
        "risk_safety": 0.98,
        "strict_single_tp_attainment": 0.92,
        "breakout_explosiveness_multiplier": 1.15,
        "tp_frontrun_buffer_pct": 0.965,
    }


if __name__ == "__main__":
    print(json.dumps(get_journal(), indent=2))
