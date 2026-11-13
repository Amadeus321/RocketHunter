from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import pytest

from scripts.user_trade_learning import (
    classify_market_regime,
    enforce_aura_safety_veto,
    evaluate_shadow_model,
    get_dataset_a_user_trades,
    get_dataset_b_scanner_predictions,
    train_ai_from_user_journal,
)


def test_minimum_samples_threshold_prevents_premature_weight_adaptation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Setup isolated journal and ai engine file
    journal_file = tmp_path / "user_positions_journal.json"
    ai_file = tmp_path / "ai_learning_engine.json"
    monkeypatch.setattr("scripts.user_trade_learning.get_journal_file", lambda: journal_file)
    monkeypatch.setattr("scripts.user_trade_learning.get_ai_engine_file", lambda: ai_file)

    # 3 trades (< 5 threshold)
    few_trades = [
        {
            "id": f"pos_{i}",
            "symbol": f"TEST{i}/USDT",
            "user_confirmed_taken": True,
            "status": "CLOSED",
            "realized_pnl_pct": 50.0,
            "features_snapshot": {
                "momentum_acceleration": 90.0,
                "volume_intelligence": 90.0,
                "buy_sell_pressure": 85.0,
                "liquidity_depth": 80.0,
                "accumulation_detection": 80.0,
                "smart_money": 75.0,
                "breakout_prediction": 88.0,
                "ecosystem_flow": 70.0,
                "risk_safety": 95.0,
            },
        }
        for i in range(3)
    ]
    journal_file.write_text(json.dumps({"positions": few_trades}), encoding="utf-8")

    model = train_ai_from_user_journal()
    assert model["weight_adaptation_applied"] is False
    assert model["min_samples_threshold"] == 5
    # Learned weights remain at baseline prior
    assert model["learned_feature_weights"]["momentum_acceleration"] == 0.88


def test_weight_adaptation_applies_shrinkage_with_sufficient_samples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    journal_file = tmp_path / "user_positions_journal.json"
    ai_file = tmp_path / "ai_learning_engine.json"
    monkeypatch.setattr("scripts.user_trade_learning.get_journal_file", lambda: journal_file)
    monkeypatch.setattr("scripts.user_trade_learning.get_ai_engine_file", lambda: ai_file)

    # 6 trades (>= 5 threshold)
    sufficient_trades = [
        {
            "id": f"pos_{i}",
            "symbol": f"TEST{i}/USDT",
            "user_confirmed_taken": True,
            "status": "CLOSED",
            "realized_pnl_pct": 20.0 + i * 10.0,
            "features_snapshot": {
                "momentum_acceleration": 60.0 + i * 5.0,
                "volume_intelligence": 70.0,
                "buy_sell_pressure": 75.0,
                "liquidity_depth": 80.0,
                "accumulation_detection": 80.0,
                "smart_money": 75.0,
                "breakout_prediction": 85.0,
                "ecosystem_flow": 70.0,
                "risk_safety": 90.0,
            },
        }
        for i in range(6)
    ]
    journal_file.write_text(json.dumps({"positions": sufficient_trades}), encoding="utf-8")

    model = train_ai_from_user_journal()
    assert model["weight_adaptation_applied"] is True
    assert model["verified_training_samples"] == 6
    # Momentum correlates positively with PnL, weight should have shifted upwards within bounds
    assert model["learned_feature_weights"]["momentum_acceleration"] >= 0.88


def test_dataset_a_and_b_are_strictly_decoupled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    journal_file = tmp_path / "user_positions_journal.json"
    monkeypatch.setattr("scripts.user_trade_learning.get_journal_file", lambda: journal_file)
    journal_file.write_text(json.dumps({
        "positions": [
            {"id": "user_pos_1", "symbol": "USER/USDC", "user_confirmed_taken": True, "realized_pnl_pct": 45.0},
            {"id": "skipped_pos_2", "symbol": "SKIP/USDC", "user_confirmed_taken": False},
        ]
    }), encoding="utf-8")

    mem_dir = tmp_path / "scanner-memory"
    prospective_dir = mem_dir / "prospective-opportunities"
    prospective_dir.mkdir(parents=True)
    import gzip
    gz_file = prospective_dir / "20261113.jsonl.gz"
    with gzip.open(gz_file, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"candidate_id": "cand_1", "pair": "SCAN/USDC", "projected_gain_pct": 48.0}) + "\n")

    dataset_a = get_dataset_a_user_trades()
    dataset_b = get_dataset_b_scanner_predictions(memory_dir=mem_dir)

    assert len(dataset_a) == 1
    assert dataset_a[0]["symbol"] == "USER/USDC"

    assert len(dataset_b) == 1
    assert dataset_b[0]["pair"] == "SCAN/USDC"


def test_market_regime_classification() -> None:
    assert classify_market_regime({"aggregate_liquidity_usd": 25000}) == "LOW_LIQUIDITY"
    assert classify_market_regime({"market_volatility_pct": 15.0}) == "HIGH_VOLATILITY"
    assert classify_market_regime({"btc_change_24h_pct": -9.5}) == "BEARISH"
    assert classify_market_regime({"btc_change_24h_pct": -4.0, "net_ecosystem_inflow_usd": -100000}) == "RISK_OFF"
    assert classify_market_regime({"btc_change_24h_pct": 5.2}) == "BULLISH"
    assert classify_market_regime({"btc_change_24h_pct": 0.5, "market_volatility_pct": 3.0}) == "SIDEWAYS"


def test_shadow_model_evaluation_and_rollback() -> None:
    prod_weights = {"momentum_acceleration": 0.88, "volume_intelligence": 0.90}
    bad_cand_weights = {"momentum_acceleration": 0.20, "volume_intelligence": 0.20}
    good_cand_weights = {"momentum_acceleration": 0.92, "volume_intelligence": 0.95}

    val_records = [
        {"pnl": 40.0, "features": {"momentum_acceleration": 90.0, "volume_intelligence": 90.0}},
        {"pnl": 50.0, "features": {"momentum_acceleration": 95.0, "volume_intelligence": 95.0}},
    ]

    res_regressed = evaluate_shadow_model(prod_weights, bad_cand_weights, val_records)
    assert res_regressed["promotion_decision"] == "ROLLBACK_TO_PRODUCTION"
    assert res_regressed["applied_weights"] == prod_weights

    res_improved = evaluate_shadow_model(prod_weights, good_cand_weights, val_records)
    assert res_improved["promotion_decision"] == "PROMOTE_CANDIDATE"
    assert res_improved["applied_weights"] == good_cand_weights


def test_aura_safety_veto_enforcement() -> None:
    # 1. Honeypot veto
    veto, reason = enforce_aura_safety_veto({
        "symbol": "TRAP/SOL",
        "is_honeypot": True,
        "tp_upside_pct": 75.0,
    })
    assert veto is True
    assert "honeypot" in reason.lower()

    # 2. Unlocked LP veto
    veto, reason = enforce_aura_safety_veto({
        "symbol": "RUG/ETH",
        "is_honeypot": False,
        "lp_status": "UNLOCKED",
        "tp_upside_pct": 80.0,
    })
    assert veto is True
    assert "unlocked" in reason.lower()

    # 3. Sub-40% TP veto
    veto, reason = enforce_aura_safety_veto({
        "symbol": "SLOW/USDC",
        "is_honeypot": False,
        "lp_status": "LOCKED",
        "tp_upside_pct": 25.0,
    })
    assert veto is True
    assert "below mandatory +40.0%" in reason

    # 4. Legitimate candidate passes
    veto, reason = enforce_aura_safety_veto({
        "symbol": "ROCKET/SOL",
        "is_honeypot": False,
        "lp_status": "BURNED",
        "tp_upside_pct": 55.0,
    })
    assert veto is False
    assert reason == "SAFETY_PASS"
