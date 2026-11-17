#!/usr/bin/env python3
"""Unit tests for User Trade Learning & AURA Feature-Attribution Learning Engine."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import user_trade_learning as utl


class TestUserTradeLearning(unittest.TestCase):
    """Test user trade recording, feature attribution, and AURA learning engine."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.orig_data_dir = utl.DATA_DIR
        self.orig_journal = utl.JOURNAL_FILE
        self.orig_ai_file = utl.AI_ENGINE_FILE

        utl.DATA_DIR = self.temp_dir
        utl.JOURNAL_FILE = self.temp_dir / "user_positions_journal.json"
        utl.AI_ENGINE_FILE = self.temp_dir / "ai_learning_engine.json"

    def tearDown(self):
        utl.DATA_DIR = self.orig_data_dir
        utl.JOURNAL_FILE = self.orig_journal
        utl.AI_ENGINE_FILE = self.orig_ai_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_record_position_and_version_provenance(self):
        pos = {
            "pair": "TEST/SOL",
            "chain": "solana",
            "entry_price": "1.0",
            "single_tp": "1.45",
            "potential_pct": "+45.00%",
            "pool_address": "pool_abc123",
            "token_contract": "token_xyz789",
            "software_version": "v4.26.6",
            "run_id": "run-001",
        }
        res = utl.record_position(pos)
        self.assertEqual(res["status"], "OK")
        self.assertEqual(res["position"]["software_version"], "v4.26.6")

        journal = utl.get_journal()
        self.assertEqual(len(journal["positions"]), 1)
        self.assertEqual(journal["positions"][0]["pair"], "TEST/SOL")

    def test_feature_attribution_training_on_user_positions(self):
        # 1. Win trade with strong volume burst and momentum
        win_pos = {
            "pair": "WINNER/SOL",
            "chain": "solana",
            "entry_price": "0.10",
            "single_tp": "0.145",
            "potential_pct": "+45.00%",
            "pool_address": "pool_win_1",
            "software_version": "v4.27.0",
            "status": "TP_HIT",
            "final_pnl_pct": "+42.50%",
            "notes": "Strong organic volume breakout and positive CVD",
            "entry_features": {
                "momentum_acceleration": 90.0,
                "volume_intelligence": 95.0,
                "buy_sell_pressure": 88.0,
                "liquidity_depth": 75.0,
                "accumulation_detection": 82.0,
                "smart_money": 80.0,
                "breakout_prediction": 92.0,
                "ecosystem_flow": 85.0,
                "risk_safety": 90.0,
            },
        }
        utl.record_position(win_pos)

        # 2. Loss trade with thin liquidity
        loss_pos = {
            "pair": "LOSER/SOL",
            "chain": "solana",
            "entry_price": "0.05",
            "single_tp": "0.075",
            "potential_pct": "+50.00%",
            "pool_address": "pool_loss_1",
            "software_version": "v4.27.0",
            "status": "CLOSED_LOSS",
            "final_pnl_pct": "-18.50%",
            "notes": "Thin liquidity microcap false breakout",
            "entry_features": {
                "momentum_acceleration": 50.0,
                "volume_intelligence": 45.0,
                "buy_sell_pressure": 40.0,
                "liquidity_depth": 20.0,
                "accumulation_detection": 45.0,
                "smart_money": 40.0,
                "breakout_prediction": 55.0,
                "ecosystem_flow": 80.0,
                "risk_safety": 35.0,
            },
        }
        utl.record_position(loss_pos)

        ai_model = utl.train_ai_from_user_journal()
        self.assertTrue(ai_model["trained_on_user_positions_only"])
        self.assertEqual(ai_model["win_count"], 1)
        self.assertEqual(ai_model["loss_count"], 1)
        self.assertEqual(ai_model["win_rate_pct"], 50.0)

        # Volume and momentum were high in the win trade and low in the loss trade
        # Feature attribution should be positive
        attributions = ai_model["feature_attributions"]
        self.assertGreater(attributions["volume_intelligence"], 0.0)
        self.assertGreater(attributions["momentum_acceleration"], 0.0)

        # Learned weights should reflect attribution
        weights = utl.get_aura_learned_weights()
        self.assertIn("volume_intelligence", weights)
        self.assertIn("breakout_explosiveness_multiplier", weights)

        # Permanent negative lessons must be preserved
        safeguards = [lesson["symbol"] for lesson in ai_model["negative_failure_lessons"]]
        self.assertIn("FAIR/SOL", safeguards)
        self.assertIn("USOE/SOL", safeguards)
        self.assertIn("CORLA/WBNB", safeguards)

    def test_partial_features_return_none_and_excluded_from_supervised_matrix(self):
        # A position with ONLY momentum_acceleration
        partial_pos = {
            "pair": "PARTIAL/SOL",
            "chain": "solana",
            "entry_price": "0.10",
            "status": "TP_HIT",
            "final_pnl_pct": "+40.00%",
            "entry_features": {
                "momentum_acceleration": 90.0,
            },
        }
        feats = utl.extract_position_features(partial_pos)
        self.assertIsNotNone(feats)
        self.assertEqual(feats["momentum_acceleration"], 90.0)
        # All other unmeasured features MUST be None, NEVER defaulted to 50 or 85!
        self.assertIsNone(feats["volume_intelligence"])
        self.assertIsNone(feats["buy_sell_pressure"])
        self.assertIsNone(feats["liquidity_depth"])
        self.assertIsNone(feats["risk_safety"])

        # Recording this partial position
        utl.record_position(partial_pos)
        ai_model = utl.train_ai_from_user_journal()
        # It must be counted as a partial snapshot, but NOT in verified_training_samples
        self.assertEqual(ai_model["partial_snapshots_recorded"], 1)
        self.assertEqual(ai_model["verified_training_samples"], 0)

    def test_archiving_preserves_confirmed_outcome_in_learning(self):
        # A completed trade that gets soft-archived on dashboard
        pos = {
            "id": "pos_archive_test",
            "pair": "ARCHIVE/SOL",
            "chain": "solana",
            "entry_price": "1.0",
            "status": "TP_HIT",
            "final_pnl_pct": "+45.00%",
            "entry_features": {
                "momentum_acceleration": 80.0,
                "volume_intelligence": 85.0,
                "buy_sell_pressure": 75.0,
                "liquidity_depth": 70.0,
                "accumulation_detection": 75.0,
                "smart_money": 70.0,
                "breakout_prediction": 80.0,
                "ecosystem_flow": 75.0,
                "risk_safety": 90.0,
            },
        }
        utl.record_position(pos)
        # Soft-archive it from the dashboard
        del_res = utl.delete_position("pos_archive_test")
        self.assertEqual(del_res["status"], "OK")

        journal = utl.get_journal()
        self.assertTrue(journal["positions"][0]["archived"])

        # Learning must still include this valid closed trade!
        ai_model = utl.train_ai_from_user_journal()
        self.assertEqual(ai_model["win_count"], 1)
        self.assertEqual(ai_model["verified_training_samples"], 1)

    def test_backup_rebuild_and_provenance(self):
        rebuild = utl.backup_and_rebuild_model()
        self.assertEqual(rebuild["status"], "OK")

        prov = utl.get_aura_provenance()
        sw_ver = utl.get_software_version()
        self.assertTrue(prov["aura_version"].startswith(f"AURA-{sw_ver}"))
        self.assertEqual(prov["software_version"], sw_ver)
        self.assertNotEqual(prov["model_sha256"], "UNAVAILABLE")

    def test_delete_position_by_pool_address_and_hard_delete(self):
        pos = {
            "id": "pos_target_delete_test",
            "pair": "TEST/SOL",
            "chain": "solana",
            "pool_address": "PoolAddr123456789Test",
            "entry_price": "0.01",
            "status": "OPEN",
        }
        utl.record_position(pos)
        # Delete by pool address
        del_res = utl.delete_position("PoolAddr123456789Test")
        self.assertEqual(del_res["status"], "OK")

        journal = utl.get_journal()
        matching = [p for p in journal["positions"] if p.get("pool_address") == "PoolAddr123456789Test"]
        self.assertEqual(len(matching), 1)
        self.assertTrue(matching[0].get("archived"))
        self.assertEqual(matching[0].get("status"), "ARCHIVED")

        # Now test hard delete
        hard_res = utl.delete_position("PoolAddr123456789Test", hard=True)
        self.assertEqual(hard_res["status"], "OK")
        journal_after = utl.get_journal()
        matching_after = [p for p in journal_after["positions"] if p.get("pool_address") == "PoolAddr123456789Test"]
        self.assertEqual(len(matching_after), 0)

    def test_archived_positions_excluded_from_active_dashboard_view(self):
        utl.record_position({
            "id": "pos_active_one",
            "pair": "ACTIVE/SOL",
            "pool_address": "ActivePool111",
            "status": "OPEN",
        })
        utl.record_position({
            "id": "pos_to_be_archived",
            "pair": "REMOVE/SOL",
            "pool_address": "RemovePool222",
            "status": "OPEN",
        })
        # Archive one
        utl.delete_position("pos_to_be_archived")

        from scripts import status_dashboard
        j = utl.get_journal()
        all_p = j.get("positions", [])
        active_p = [p for p in all_p if not p.get("archived") and (p.get("status") or "").upper() != "ARCHIVED"]

        self.assertEqual(len(active_p), 1)
        self.assertEqual(active_p[0]["id"], "pos_active_one")

    def test_re_recording_archived_position_reactivates_to_open(self):
        pos = {
            "pair": "CLAUDIA / SOL",
            "chain": "solana",
            "entry_price": "0.006761",
            "single_tp": "0.011626",
            "potential_pct": "+71.97%",
            "pool_address": "4XtavTkbwQpoTiQWswbyvBF3z4Cj5CjjfGGiDMZFk116",
            "run_id": "run-reactivate-test",
        }
        # First record
        res1 = utl.record_position(pos)
        self.assertEqual(res1["status"], "OK")

        # User unselects/archives it
        del_res = utl.delete_position("4XtavTkbwQpoTiQWswbyvBF3z4Cj5CjjfGGiDMZFk116")
        self.assertEqual(del_res["status"], "OK")

        journal = utl.get_journal()
        saved = [p for p in journal["positions"] if p.get("pool_address") == "4XtavTkbwQpoTiQWswbyvBF3z4Cj5CjjfGGiDMZFk116"][0]
        self.assertTrue(saved.get("archived"))
        self.assertEqual(saved.get("status"), "ARCHIVED")

        # User clicks "Take Position" again on the same candidate
        res2 = utl.record_position(pos)
        self.assertEqual(res2["status"], "OK")
        self.assertEqual(res2.get("action"), "REACTIVATED")

        journal_after = utl.get_journal()
        saved_after = [p for p in journal_after["positions"] if p.get("pool_address") == "4XtavTkbwQpoTiQWswbyvBF3z4Cj5CjjfGGiDMZFk116"][0]
        self.assertFalse(saved_after.get("archived"))
        self.assertEqual(saved_after.get("status"), "OPEN")
        self.assertEqual(saved_after.get("entry_price"), "0.006761")


if __name__ == "__main__":
    unittest.main()
