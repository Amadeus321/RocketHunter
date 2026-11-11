import gzip
import json
import tempfile
import unittest
from pathlib import Path

from scripts.v418_forward_guard import apply_forward_guard


class ForwardAgeGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.memory = self.root / "memory"
        (self.memory / "forecasts").mkdir(parents=True)
        (self.memory / "outcomes").mkdir(parents=True)
        self.run_id = "guard-test-run"
        self.main = {
            "skill_version": "4.15.3",
            "coverage": {"unique_asset_rows": 3},
            "new_pools_under_24h": [
                {"identity": "dex:solana:young", "market_type": "dex_pool", "chain": "solana", "contract_address": "young", "pool_address": "pool-young", "age_hours": 18.7, "age_hours_v418": 18.7, "return_window_status": "INSUFFICIENT_24H_HISTORY", "source_reported_change_24h_pct": 1000, "change_24h_pct": None, "price_usd": 0.5, "risk_flags": []},
            ],
            "comparable_24h_gainers_v418": [
                {"identity": "dex:solana:mature", "market_type": "dex_pool", "chain": "solana", "contract_address": "mature", "pool_address": "pool-mature", "age_hours": 48, "age_hours_v418": 48, "return_window_status": "SOURCE_REPORTED_24H_POOL_AGE_GE_24H", "change_24h_pct": 50, "price_usd": 1.5, "risk_flags": []},
            ],
        }
        candidates = [
            {"identity": "dex:solana:young", "market_type": "dex_pool", "chain": "solana", "contract_address": "young", "pool_address": "pool-young", "asset": "YoungCoin", "forecast_score": 99, "forecast_rank": 1, "price": 0.5, "returns_pct": {"1h": 20, "24h": 1000}, "factor_details": [{"factor": "momentum_relative_strength", "note": "24h 1000%"}], "entry_blockers": [], "trade_plan": {"single_take_profit": 0.75}},
            {"identity": "dex:solana:mature", "market_type": "dex_pool", "chain": "solana", "contract_address": "mature", "pool_address": "pool-mature", "asset": "MatureCoin", "forecast_score": 20, "forecast_rank": 2, "price": 1.5, "returns_pct": {"1h": 3, "24h": 50}, "entry_blockers": [], "trade_plan": {"single_take_profit": 2.0}},
        ]
        forward = {"skill_version": "4.15.3", "pipeline_version": "4.18.1", "status": "TOP_ROCKET_REFERENCE_PLAN_READY", "summary": "Top is YoungCoin", "candidates": candidates, "forecast_leader": candidates[0], "upside_leader": candidates[0], "top_rocket_pick": candidates[0], "raw_upside_leader": candidates[0], "trailing_raw_24h_champion": candidates[0], "required_fields": ["entry_price", "single_take_profit", "pair_address"]}
        (self.root / "rocket-hunt.json").write_text(json.dumps(self.main))
        (self.root / "forward-24h-rocket.json").write_text(json.dumps(forward))
        (self.root / "rocket-hunt.md").write_text("### Forward 24h Top Pick\n\nold pick\n\n### Forward-24h ranking\n\n**Important:** the forecast is a ranking heuristic.\n")
        forecast = {
            "run_id": self.run_id,
            "market_key": "dex:solana:young|pool-young||",
            "raw_score": 99,
            "feature_snapshot": {"forecast_score": 99, "change_24h_pct": 1000, "return_window_status": "INSUFFICIENT_24H_HISTORY"},
        }
        with gzip.open(self.memory / "forecasts" / "2026-11-05.jsonl.gz", "wt", encoding="utf-8") as h:
            h.write(json.dumps(forecast) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_young_candidate_is_discovery_only_and_mature_candidate_promoted(self):
        summary = apply_forward_guard(self.root, self.memory, self.run_id)
        out = json.loads((self.root / "forward-24h-rocket.json").read_text())
        self.assertEqual(summary["eligible_forward_candidates"], 1)
        self.assertEqual(summary["discovery_only_candidates"], 1)
        self.assertEqual(out["forecast_leader"]["asset"], "MatureCoin")
        young = next(x for x in out["candidates"] if x["asset"] == "YoungCoin")
        self.assertIsNone(young["forecast_rank"])
        self.assertIsNone(young["forecast_score"])
        self.assertEqual(young["source_reported_return_24h_pct"], 1000)
        self.assertIsNone(young["returns_pct"]["24h"])
        self.assertEqual(out["new_launch_discovery_leads_v418"][0]["asset"], "YoungCoin")

    def test_current_run_memory_score_is_corrected_before_persist(self):
        apply_forward_guard(self.root, self.memory, self.run_id)
        with gzip.open(self.memory / "forecasts" / "2026-11-05.jsonl.gz", "rt", encoding="utf-8") as h:
            row = json.loads(next(h))
        self.assertIsNone(row["raw_score"])
        self.assertEqual(row["source_raw_score_pre_age_guard"], 99)
        self.assertFalse(row["feature_snapshot"]["return_24h_comparable"])
        self.assertNotIn("forecast_score", row["feature_snapshot"])

    def test_markdown_replaces_unqualified_pick(self):
        apply_forward_guard(self.root, self.memory, self.run_id)
        md = (self.root / "rocket-hunt.md").read_text()
        self.assertIn("**MatureCoin**", md)
        self.assertIn("New-launch discovery leads", md)
        self.assertNotIn("old pick", md)


    def test_same_pool_is_reconciled_across_lane_quote_and_venue_metadata(self):
        f = json.loads((self.root / "forward-24h-rocket.json").read_text())
        f["candidates"][0]["venue"] = "solana"
        f["candidates"][0].pop("quote_symbol", None)
        self.main["new_pools_under_24h"][0]["quote_symbol"] = "SOL"
        self.main["new_pools_under_24h"][0]["venue"] = None
        (self.root / "rocket-hunt.json").write_text(json.dumps(self.main))
        (self.root / "forward-24h-rocket.json").write_text(json.dumps(f))
        summary = apply_forward_guard(self.root, self.memory, self.run_id)
        out = json.loads((self.root / "forward-24h-rocket.json").read_text())
        young = next(x for x in out["candidates"] if x["asset"] == "YoungCoin")
        self.assertEqual(summary["discovery_only_candidates"], 1)
        self.assertEqual(young["age_hours_v418"], 18.7)
        self.assertEqual(young["return_window_status"], "INSUFFICIENT_24H_HISTORY")

    def test_conservative_youngest_age_wins_for_duplicate_pool_copies(self):
        # A duplicate copy in another lane may carry a stale/older age. The
        # canonical pool identity must use the youngest observed age.
        self.main["new_launch_discovery_board_v418"] = [
            {"identity": "dex:solana:mature", "market_type": "dex_pool", "chain": "solana", "contract_address": "mature", "pool_address": "pool-mature", "age_hours": 18, "age_hours_v418": 18, "return_window_status": "INSUFFICIENT_24H_HISTORY", "source_reported_change_24h_pct": 50, "change_24h_pct": None, "price_usd": 1.5, "risk_flags": []}
        ]
        (self.root / "rocket-hunt.json").write_text(json.dumps(self.main))
        summary = apply_forward_guard(self.root, self.memory, self.run_id)
        out = json.loads((self.root / "forward-24h-rocket.json").read_text())
        mature = next(x for x in out["candidates"] if x["asset"] == "MatureCoin")
        self.assertEqual(summary["eligible_forward_candidates"], 0)
        self.assertEqual(mature["return_window_status"], "INSUFFICIENT_24H_HISTORY")

    def test_no_age_qualified_candidate_does_not_erase_discoveries(self):
        f = json.loads((self.root / "forward-24h-rocket.json").read_text())
        f["candidates"][1]["age_hours"] = 10
        f["candidates"][1]["age_hours_v418"] = 10
        f["candidates"][1]["return_window_status"] = "INSUFFICIENT_24H_HISTORY"
        f["candidates"][1]["returns_pct"]["24h"] = 50
        self.main["comparable_24h_gainers_v418"][0]["age_hours"] = 10
        self.main["comparable_24h_gainers_v418"][0]["age_hours_v418"] = 10
        self.main["comparable_24h_gainers_v418"][0]["return_window_status"] = "INSUFFICIENT_24H_HISTORY"
        self.main["comparable_24h_gainers_v418"][0]["source_reported_change_24h_pct"] = 50
        self.main["comparable_24h_gainers_v418"][0]["change_24h_pct"] = None
        (self.root / "rocket-hunt.json").write_text(json.dumps(self.main))
        (self.root / "forward-24h-rocket.json").write_text(json.dumps(f))
        summary = apply_forward_guard(self.root, self.memory, self.run_id)
        out = json.loads((self.root / "forward-24h-rocket.json").read_text())
        self.assertEqual(summary["eligible_forward_candidates"], 0)
        self.assertIsNone(out["top_rocket_pick"])
        self.assertEqual(len(out["new_launch_discovery_leads_v418"]), 2)
        self.assertEqual(out["status"], "NO_AGE_QUALIFIED_FORWARD_CANDIDATE")


if __name__ == "__main__":
    unittest.main()
