#!/usr/bin/env python3
"""Unit tests for verify_completed_scan QA script."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.verify_completed_scan import verify_run


class TestVerifyCompletedScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_scan_status_fails(self):
        res = verify_run(self.run_dir)
        self.assertEqual(res["status"], "FAIL")
        self.assertIn("scan-status.json missing", res["errors"])

    def test_zero_verified_opportunities_passes_fail_closed(self):
        (self.run_dir / "scan-status.json").write_text(
            json.dumps({"status": "COMPLETED", "run_id": "test-run", "scanner_version": "v4.26.1"})
        )
        (self.run_dir / "forward-24h-rocket.json").write_text(
            json.dumps({
                "general_version": "v4.26.1",
                "verified_actionable_opportunities": [],
            })
        )
        res = verify_run(self.run_dir)
        self.assertEqual(res["status"], "PASS")
        self.assertEqual(res["summary"]["verified_actionable_count"], 0)
        self.assertIn("ZERO VERIFIED OPPORTUNITIES", res["summary"]["actionable_verdict"])

    def test_actionable_candidate_with_sub_40_percent_fails(self):
        (self.run_dir / "scan-status.json").write_text(
            json.dumps({"status": "COMPLETED", "run_id": "test-run", "scanner_version": "v4.26.1"})
        )
        (self.run_dir / "forward-24h-rocket.json").write_text(
            json.dumps({
                "general_version": "v4.26.1",
                "verified_actionable_opportunities": [{
                    "pair": "TEST/USD",
                    "chain": "solana",
                    "pool_address": "pool123",
                    "contract_address": "mint123",
                    "price": 1.0,
                    "trade_plan": {"single_take_profit": 1.3999},
                    "tp_upside_pct": 39.99,
                    "safety_verified": True,
                    "executable_route_verified": True,
                }],
            })
        )
        res = verify_run(self.run_dir)
        self.assertEqual(res["status"], "FAIL")
        self.assertTrue(any("< +40.00% gate" in e for e in res["errors"]))


if __name__ == "__main__":
    unittest.main()
