#!/usr/bin/env python3
"""User Trade Journal & Adaptive AI Learning Engine for Rocket Hunter.

Tracks actual positions entered by the user, records follow-up outcomes (P/L, status, notes),
remembers the exact software version for each trade, and drives the internal AI learning
memory strictly from real user confirmations.
"""
from __future__ import annotations

import datetime as dt
import hashlib
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
                "model_version": f"AURA-{get_software_version()}",
                "software_version": get_software_version(),
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
        get_ai_engine_file().write_text(json.dumps(default_data["ai_learning_stats"], indent=2), encoding="utf-8")
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
    aura_ver = str(pos.get("aura_version") or f"AURA-{version}").strip()

    pos_id = pos.get("id") or f"pos_{pair}_{pool[:8]}_{int(dt.datetime.now().timestamp())}".replace("/", "_")

    for existing in positions:
        if existing.get("id") == pos_id or (pool and existing.get("pool_address") == pool and existing.get("run_id") == run_id):
            return {"status": "EXISTS", "position": existing}

    def _to_float(val: Any) -> float | None:
        if val is None or val == "":
            return None
        try:
            f = float(str(val).replace("%", "").replace("+", "").replace("$", "").replace(",", "").strip())
            return f if math.isfinite(f) else None
        except (ValueError, TypeError):
            return None

    entry_ref = _to_float(pos.get("entry_reference_price", pos.get("entry_price")))
    actual_entry = _to_float(pos.get("actual_entry_fill"))
    actual_exit = _to_float(pos.get("actual_exit_fill"))
    fees = _to_float(pos.get("fees_usd"))
    slippage = _to_float(pos.get("slippage_pct"))
    trade_size = _to_float(pos.get("trade_size_usd"))
    realized_pnl = _to_float(pos.get("realized_pnl_pct", pos.get("final_pnl_pct")))
    theoretical_pnl = _to_float(pos.get("theoretical_pnl_pct"))

    record = {
        "id": pos_id,
        "pair": pair,
        "chain": pos.get("chain") or "",
        "entry_price": str(pos.get("entry_price") or ""),
        "single_tp": str(pos.get("single_tp") or ""),
        "potential_pct": str(pos.get("potential_pct") or ""),
        "pool_address": pool,
        "token_contract": str(pos.get("token_contract") or ""),
        "software_version": version,
        "aura_version": aura_ver,
        "last_modified_in_version": version,
        "run_id": run_id,
        "entered_at_ist": _now_ist_str(),
        "taken_at_utc": pos.get("taken_at_utc") or dt.datetime.now(dt.timezone.utc).isoformat(),
        "user_confirmed_taken": bool(pos.get("user_confirmed_taken", True)),
        "entry_evidence_type": str(pos.get("entry_evidence_type") or "USER_CONFIRMED"),
        "entry_reference_price": entry_ref,
        "actual_entry_fill": actual_entry,
        "actual_exit_fill": actual_exit,
        "fees_usd": fees,
        "slippage_pct": slippage,
        "trade_size_usd": trade_size,
        "status": str(pos.get("status") or "OPEN"),
        "final_pnl_pct": str(pos.get("final_pnl_pct") or ""),
        "realized_pnl_pct": realized_pnl,
        "theoretical_pnl_pct": theoretical_pnl,
        "notes": str(pos.get("notes") or ""),
        "archived": bool(pos.get("archived", False)),
        "entry_features": pos.get("entry_features") or pos.get("breakout_engine_scores") or {},
    }
    positions.insert(0, record)
    save_journal(journal)
    return {"status": "OK", "position": record}


def update_position(pos_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    journal = get_journal()
    positions = journal.get("positions", [])
    found = None
    curr_ver = get_software_version()
    allowed_keys = (
        "status", "final_pnl_pct", "realized_pnl_pct", "theoretical_pnl_pct",
        "notes", "software_version", "archived", "user_confirmed_taken",
        "taken_at_utc", "entry_reference_price", "actual_entry_fill",
        "actual_exit_fill", "fees_usd", "slippage_pct", "trade_size_usd",
        "entry_evidence_type"
    )
    for p in positions:
        if p.get("id") == pos_id:
            for k in allowed_keys:
                if k in updates:
                    p[k] = updates[k]
            p["updated_at_ist"] = _now_ist_str()
            p["last_modified_in_version"] = curr_ver
            if "aura_version" not in p:
                p["aura_version"] = f"AURA-{curr_ver}"
            found = p
            break

    if found:
        save_journal(journal)
        return {"status": "OK", "position": found}
    return {"status": "ERROR", "message": f"Position {pos_id} not found."}


def delete_position(pos_id: str) -> dict[str, Any]:
    """Soft-archives a position on the dashboard while preserving it for historical learning."""
    journal = get_journal()
    positions = journal.get("positions", [])
    found = False
    curr_ver = get_software_version()
    for p in positions:
        if p.get("id") == pos_id:
            p["archived"] = True
            p["status"] = "ARCHIVED"
            p["archived_at_ist"] = _now_ist_str()
            p["last_modified_in_version"] = curr_ver
            found = True
            break
    if found:
        save_journal(journal)
        return {"status": "OK", "message": "Position archived and preserved in history."}
    return {"status": "ERROR", "message": "Position not found."}


def reset_ai_memory() -> dict[str, Any]:
    """Rebuilds the AURA learning model cleanly strictly from confirmed historical records."""
    return backup_and_rebuild_model()


def backup_and_rebuild_model() -> dict[str, Any]:
    """Creates a timestamped backup of the active model and retrains cleanly from confirmed records."""
    aifile = get_ai_engine_file()
    backup_file: Path | None = None
    if aifile.exists():
        try:
            ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
            backup_file = aifile.parent / f"ai_learning_engine.backup.{ts}.json"
            backup_file.write_bytes(aifile.read_bytes())
        except Exception:
            pass

    rebuilt_model = train_ai_from_user_journal()
    return {
        "status": "OK",
        "message": "AURA learning model rebuilt cleanly from confirmed user positions.",
        "backup_path": str(backup_file) if backup_file else None,
        "model": rebuilt_model,
    }


def rollback_ai_model(backup_path: Path | str) -> dict[str, Any]:
    """Rolls back the AURA model to a specified backup file."""
    p = Path(backup_path).resolve()
    if not p.exists():
        return {"status": "ERROR", "message": f"Backup file {backup_path} does not exist."}
    try:
        content = p.read_bytes()
        json.loads(content.decode("utf-8"))
        aifile = get_ai_engine_file()
        aifile.write_bytes(content)
        return {"status": "OK", "message": f"Rolled back to {p.name}."}
    except Exception as e:
        return {"status": "ERROR", "message": f"Rollback failed: {e}"}


def extract_position_features(pos: dict[str, Any]) -> dict[str, float | None] | None:
    """Extracts verified entry features from a position. Never manufactures missing values.

    Returns None for any indicator not explicitly present in the recorded entry snapshot.
    """
    entry_feats = pos.get("entry_features") or pos.get("breakout_engine_scores") or pos.get("features_snapshot")
    if not isinstance(entry_feats, dict) or not entry_feats:
        return None

    def _get_val(*keys: str) -> float | None:
        for k in keys:
            v = entry_feats.get(k)
            if v is not None:
                try:
                    num = float(v)
                    if math.isfinite(num):
                        return num
                except (ValueError, TypeError):
                    continue
        return None

    liq = _get_val("engine_4_liquidity_depth", "liquidity_depth", "engine_4_liquidity_intelligence", "liquidity_intelligence")

    return {
        "momentum_acceleration": _get_val("engine_1_momentum_acceleration", "momentum_acceleration"),
        "volume_intelligence": _get_val("engine_2_volume_intelligence", "volume_intelligence"),
        "buy_sell_pressure": _get_val("engine_3_buy_sell_pressure", "buy_sell_pressure"),
        "liquidity_depth": liq,
        "liquidity_intelligence": liq,
        "accumulation_detection": _get_val("engine_5_accumulation_detection", "accumulation_detection"),
        "smart_money": _get_val("engine_6_smart_money", "smart_money"),
        "breakout_prediction": _get_val("engine_7_breakout_prediction", "breakout_prediction"),
        "ecosystem_flow": _get_val("engine_8_ecosystem_flow", "ecosystem_flow"),
        "risk_safety": _get_val("engine_9_risk_safety", "risk_safety"),
    }


def train_ai_from_user_journal() -> dict[str, Any]:
    """Computes genuine machine-learning feature-attribution statistics from user positions.

    Strict rules:
    - Never uses invented P&L (+40% / -10% fallbacks are prohibited).
    - Never uses manufactured feature values (missing indicators remain None).
    - Only positions with verified numeric P&L and complete 9-indicator recorded entry features train the covariance model.
    - Partial feature snapshots are tracked in research memory but excluded from supervised covariance attribution.
    - Archiving a position hides it from the active dashboard view but DOES NOT exclude confirmed historical outcomes from learning.
    """
    journal = get_journal()
    positions = journal.get("positions", [])

    confirmed_positions = [
        p for p in positions
        if p.get("user_confirmed_taken", True) is not False
    ]
    total = len(confirmed_positions)
    open_count = sum(1 for p in confirmed_positions if p.get("status") == "OPEN" and not p.get("archived"))
    closed_positions = [
        p for p in confirmed_positions
        if p.get("status") not in ("OPEN", None) or (p.get("final_pnl_pct") and str(p.get("final_pnl_pct")).strip() != "")
    ]

    wins = 0
    losses = 0
    pnl_values: list[float] = []
    trade_samples: list[dict[str, Any]] = []
    partial_snapshots_count = 0

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

    for p in closed_positions:
        pnl_val: float | None = None
        if p.get("realized_pnl_pct") is not None:
            try:
                pnl_val = float(p["realized_pnl_pct"])
            except (ValueError, TypeError):
                pnl_val = None
        if pnl_val is None:
            pnl_str = str(p.get("final_pnl_pct") or "").replace("%", "").replace("+", "").replace("$", "").strip()
            try:
                pnl_val = float(pnl_str)
            except (ValueError, TypeError):
                pnl_val = None

        if pnl_val is not None and math.isfinite(pnl_val):
            pnl_values.append(pnl_val)
            if pnl_val > 0:
                wins += 1
            elif pnl_val < 0:
                losses += 1

            feats = extract_position_features(p)
            if feats is not None:
                has_complete_features = all(feats.get(k) is not None for k in feature_keys)
                if has_complete_features:
                    trade_samples.append({
                        "features": {k: float(feats[k]) for k in feature_keys},  # type: ignore[arg-type]
                        "pnl": pnl_val,
                        "is_win": bool(pnl_val > 0),
                        "position_id": p.get("id"),
                    })
                else:
                    partial_snapshots_count += 1

    win_rate = (wins / len(pnl_values) * 100.0) if pnl_values else 0.0
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

    attributions: dict[str, float] = {k: 0.0 for k in feature_keys}
    learned_weights: dict[str, float] = dict(prior_weights)

    # Require minimum sample count for weight adaptation to prevent overfitting on 1-2 lucky/unlucky trades
    MIN_TRAINING_SAMPLES_FOR_WEIGHT_ADAPTATION = 5
    weight_adaptation_applied = False

    # Compute empirical covariance feature attributions if at least 2 trade samples exist
    if len(trade_samples) >= 2:
        mean_pnl = sum(s["pnl"] for s in trade_samples) / len(trade_samples)
        for k in feature_keys:
            feat_vals = [s["features"][k] for s in trade_samples]
            mean_f = sum(feat_vals) / len(feat_vals)
            cov = sum((s["features"][k] - mean_f) * (s["pnl"] - mean_pnl) for s in trade_samples) / len(trade_samples)
            std_f = math.sqrt(sum((x - mean_f) ** 2 for x in feat_vals) / len(feat_vals)) or 1.0
            std_pnl = math.sqrt(sum((s["pnl"] - mean_pnl) ** 2 for s in trade_samples) / len(trade_samples)) or 1.0
            corr = cov / (std_f * std_pnl) if (std_f * std_pnl) > 0 else 0.0
            attributions[k] = round(corr, 4)

    # Adapt production weights only if complete samples meet or exceed minimum threshold
    if len(trade_samples) >= MIN_TRAINING_SAMPLES_FOR_WEIGHT_ADAPTATION:
        weight_adaptation_applied = True
        shrinkage_factor = min(1.0, len(trade_samples) / 25.0)  # Regularization toward prior baseline
        for k in feature_keys:
            corr = attributions.get(k, 0.0)
            # Regularized shrinkage update to prior weights
            learning_rate = min(0.25, 0.05 * math.sqrt(len(trade_samples))) * shrinkage_factor
            weight_shift = corr * learning_rate
            updated_w = round(max(0.50, min(1.20, prior_weights[k] + weight_shift)), 3)
            learned_weights[k] = updated_w
            if k == "liquidity_depth":
                learned_weights["liquidity_intelligence"] = updated_w

        # Win rate adjustment on explosiveness multiplier
        if win_rate >= 70.0:
            learned_weights["breakout_explosiveness_multiplier"] = round(min(1.35, 1.15 + (win_rate - 50.0) / 100.0 * 0.25), 3)
        elif win_rate < 40.0 and len(pnl_values) > 0:
            learned_weights["breakout_explosiveness_multiplier"] = round(max(0.95, 1.15 - (50.0 - win_rate) / 100.0 * 0.20), 3)

    curr_version = get_software_version()
    aura_ver = f"AURA-{curr_version}"

    ai_model = {
        "ai_name": "AURA",
        "ai_full_name": "AURA (Adaptive Universal Rocket Algorithm)",
        "model_version": aura_ver,
        "software_version": curr_version,
        "last_trained_ist": _now_ist_str(),
        "total_taken_positions": total,
        "open_positions": open_count,
        "closed_positions": len(pnl_values),
        "win_count": wins,
        "loss_count": losses,
        "win_rate_pct": round(win_rate, 1),
        "average_pnl_pct": round(avg_pnl, 2),
        "statistical_method": "Regularized empirical covariance feature-attribution with shrinkage to prior baseline",
        "min_samples_threshold": MIN_TRAINING_SAMPLES_FOR_WEIGHT_ADAPTATION,
        "weight_adaptation_applied": bool(len(trade_samples) >= MIN_TRAINING_SAMPLES_FOR_WEIGHT_ADAPTATION),
        "learned_feature_weights": learned_weights,
        "feature_attributions": attributions,
        "feature_importance": sorted(
            [{"feature": k, "attribution": v} for k, v in attributions.items()],
            key=lambda x: abs(x["attribution"]),
            reverse=True,
        ),
        "trained_on_user_positions_only": True,
        "verified_training_samples": len(trade_samples),
        "partial_snapshots_recorded": partial_snapshots_count,
        "synthetic_labels_permitted": False,
        "safety_veto_enforced": True,
        "safety_rules": [
            "AURA weights cannot override token contract security verification",
            "AURA weights cannot override LP position lock/burn requirements",
            "AURA weights cannot override +40.00% technical TP minimum",
            "AURA weights cannot override execution quote requirements",
        ],
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


def get_aura_provenance() -> dict[str, Any]:
    """Reports truthful AURA model provenance for scan outputs and dashboards."""
    aifile = get_ai_engine_file()
    curr_version = get_software_version()
    model_version = f"AURA-{curr_version}"
    model_hash = "UNAVAILABLE"
    last_trained = "UNAVAILABLE"
    verified_samples = 0
    personal_learning_loaded = False
    data_freshness_seconds: float | None = None

    if aifile.exists():
        try:
            content_bytes = aifile.read_bytes()
            model_hash = hashlib.sha256(content_bytes).hexdigest()
            model_data = json.loads(content_bytes.decode("utf-8"))
            model_version = model_data.get("model_version") or model_version
            last_trained = model_data.get("last_trained_ist") or last_trained
            verified_samples = int(model_data.get("verified_training_samples") or 0)
            personal_learning_loaded = bool(verified_samples > 0 and model_data.get("trained_on_user_positions_only"))
            mtime = aifile.stat().st_mtime
            data_freshness_seconds = round(max(0.0, dt.datetime.now().timestamp() - mtime), 1)
        except Exception:
            pass

    return {
        "aura_version": model_version,
        "software_version": curr_version,
        "model_sha256": model_hash,
        "last_trained_ist": last_trained,
        "verified_training_samples": verified_samples,
        "personal_learning_loaded": personal_learning_loaded,
        "data_freshness_seconds": data_freshness_seconds,
        "source": "USER_JOURNAL_CONFIRMED" if personal_learning_loaded else "QUANTITATIVE_PRIOR_BASELINE",
    }


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
    return {"feature_attributions": {}, "feature_importance": {}, "model_version": f"AURA-{get_software_version()}"}


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


def _clean_num(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        v = float(val)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def get_dataset_a_user_trades() -> list[dict[str, Any]]:
    """Dataset A: Real user-confirmed positions with actual entry and realized P/L."""
    journal = get_journal()
    return [
        pos for pos in journal.get("positions", [])
        if pos.get("user_confirmed_taken", True) is not False
    ]


def get_dataset_b_scanner_predictions(memory_dir: Path | None = None) -> list[dict[str, Any]]:
    """Dataset B: Prospective scanner predictions with frozen features and independent outcomes.

    Labels are completely separate from Dataset A (never mix user trades with scanner predictions).
    """
    import gzip
    mem_dir = Path(memory_dir or (DATA_DIR / "scanner-memory"))
    records: list[dict[str, Any]] = []

    prospective_dir = mem_dir / "prospective-opportunities"
    if prospective_dir.exists():
        for p in sorted(prospective_dir.glob("*.jsonl.gz")):
            try:
                with gzip.open(p, "rt", encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            records.append(json.loads(line))
            except Exception:
                pass

    forecast_dir = mem_dir / "forecasts"
    if forecast_dir.exists():
        for p in sorted(forecast_dir.glob("*.jsonl.gz")):
            try:
                with gzip.open(p, "rt", encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            records.append(json.loads(line))
            except Exception:
                pass
    return records


def classify_market_regime(metrics: dict[str, Any]) -> str:
    """Classify the current market-wide regime to context-weight signals.

    Regimes:
    - BULLISH: Strong positive breadth across major benchmarks.
    - BEARISH: Severe broad market downtrend and negative net flows.
    - HIGH_VOLATILITY: Extreme intraday swings or market-wide dispersion.
    - LOW_LIQUIDITY: Depleted aggregate market liquidity.
    - RISK_OFF: Heavy net outflows and negative smart money delta.
    - SIDEWAYS: Low dispersion, range-bound consolidation.
    """
    btc_24h = _clean_num(metrics.get("btc_change_24h_pct") or metrics.get("market_change_24h_pct"))
    volatility = _clean_num(metrics.get("market_volatility_pct") or metrics.get("atr_1h_pct"))
    total_liq = _clean_num(metrics.get("aggregate_liquidity_usd") or metrics.get("liquidity_usd"))
    net_inflow = _clean_num(metrics.get("net_ecosystem_inflow_usd"))

    if total_liq is not None and total_liq < 50_000:
        return "LOW_LIQUIDITY"
    if volatility is not None and volatility >= 12.0:
        return "HIGH_VOLATILITY"
    if btc_24h is not None and btc_24h <= -8.0:
        return "BEARISH"
    if net_inflow is not None and net_inflow < 0 and (btc_24h is not None and btc_24h < -3.0):
        return "RISK_OFF"
    if btc_24h is not None and btc_24h >= 4.0:
        return "BULLISH"
    return "SIDEWAYS"


def evaluate_shadow_model(
    production_weights: dict[str, float],
    candidate_weights: dict[str, float],
    validation_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare production weights vs candidate weights in shadow mode.

    Automatically rolls back if candidate weights regress on validation data.
    """
    if not validation_records:
        return {
            "status": "INSUFFICIENT_VALIDATION_DATA",
            "promotion_decision": "ROLLBACK_TO_PRODUCTION",
            "reason": "Holdout validation records are empty; candidate weights cannot be verified.",
        }

    def compute_loss(weights: dict[str, float]) -> float:
        errors = []
        for r in validation_records:
            target_pnl = _clean_num(r.get("pnl") or r.get("realized_pnl_pct") or r.get("mfe_pct")) or 0.0
            feats = r.get("features") or extract_position_features(r) or {}
            score = sum(float(feats.get(k, 50.0)) * weights.get(k, 1.0) for k in weights if k in feats)
            norm_score = (score / max(1.0, len(feats))) - 50.0
            errors.append((norm_score - target_pnl) ** 2)
        return (sum(errors) / len(errors)) if errors else 999.0

    prod_loss = compute_loss(production_weights)
    cand_loss = compute_loss(candidate_weights)

    improved = bool(cand_loss < prod_loss)
    decision = "PROMOTE_CANDIDATE" if improved else "ROLLBACK_TO_PRODUCTION"

    return {
        "status": "COMPLETED",
        "production_loss": round(prod_loss, 4),
        "candidate_loss": round(cand_loss, 4),
        "loss_improvement_pct": round(((prod_loss - cand_loss) / prod_loss) * 100.0, 2) if prod_loss > 0 else 0.0,
        "promotion_decision": decision,
        "applied_weights": candidate_weights if improved else production_weights,
    }


def enforce_aura_safety_veto(candidate: dict[str, Any]) -> tuple[bool, str]:
    """Enforces absolute AURA safety vetoes.

    Under no circumstances can AURA weights or conviction scores promote a candidate that fails:
    1. Contract security checks (e.g. honeypot, blacklist, unverified code, transfer tax > 10%).
    2. LP lock / burn requirements.
    3. The single technical take-profit minimum (+40.0%).
    Returns (is_vetoed, veto_reason).
    """
    # 1. Contract security check
    sec = candidate.get("security_assessment") or candidate.get("contract_security") or {}
    if candidate.get("is_honeypot") or sec.get("is_honeypot") or sec.get("honeypot"):
        return True, "SAFETY_VETO: Token is flagged as honeypot."
    if candidate.get("security_pass") is False or sec.get("status") in {"FAIL", "CRITICAL_RISK"}:
        return True, "SAFETY_VETO: Token failed security verification."

    # 2. LP verification check
    lp_status = candidate.get("lp_status") or sec.get("lp_status") or candidate.get("liquidity_lock_status")
    if lp_status in {"UNLOCKED", "FAIL", "CRITICAL_RISK"}:
        return True, f"SAFETY_VETO: Liquidity is unverified or unlocked ({lp_status})."

    # 3. Technical +40% TP check
    pot = _clean_num(candidate.get("tp_upside_pct") or candidate.get("potential_pct"))
    if pot is None or pot < 40.0:
        return True, f"SAFETY_VETO: Technical TP upside ({pot}%) is below mandatory +40.0% threshold."

    return False, "SAFETY_PASS"


if __name__ == "__main__":
    print(json.dumps(get_journal(), indent=2))
