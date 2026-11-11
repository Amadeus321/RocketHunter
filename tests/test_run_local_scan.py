import contextlib
import gzip
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_local_scan


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


class LocalRunMemoryTests(unittest.TestCase):
    def test_local_scan_defaults_keep_gecko_discovery_bounded(self):
        args = run_local_scan.build_arg_parser().parse_args(["--outdir", "run-test"])
        self.assertEqual(args.gecko_pages, 1)
        self.assertEqual(args.network_sweep_limit, 12)
        self.assertEqual(args.workers, 4)
        self.assertEqual(args.preflight_workers, 4)

    def test_local_resource_limits_are_forwarded_to_the_shared_scan_runtime(self):
        args = run_local_scan.build_arg_parser().parse_args([
            "--outdir", "run-test", "--workers", "3", "--preflight-workers", "2",
        ])
        command = run_local_scan.build_scan_command(args, Path("run-test"), 120)
        self.assertEqual(command[command.index("--workers") + 1], "3")
        self.assertEqual(command[command.index("--preflight-workers") + 1], "2")
        self.assertEqual(command[command.index("--gecko-pages") + 1], "1")

    def test_local_worker_limits_reject_values_above_twelve(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                run_local_scan.main(["--outdir", "run-test", "--workers", "13"])
        self.assertEqual(raised.exception.code, 2)

    def test_concurrent_snapshot_merge_unions_ledgers_and_rebuilds_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical, snapshot = root / "memory", root / "snapshot"
            write_rows(canonical / "forecasts" / "2026-11-09.jsonl.gz", [
                {"forecast_id": "old", "snapshot_utc": "2026-11-09T00:00:00Z", "feature_snapshot": {}}
            ])
            write_rows(snapshot / "forecasts" / "2026-11-09.jsonl.gz", [
                {"forecast_id": "old", "snapshot_utc": "2026-11-09T00:00:00Z", "feature_snapshot": {}},
                {"forecast_id": "new", "snapshot_utc": "2026-11-09T01:00:00Z", "feature_snapshot": {}},
            ])
            write_rows(snapshot / "outcomes" / "2026-11-09.jsonl.gz", [
                {"forecast_id": "new", "horizon_hours": 1, "status": "UNAVAILABLE_IDENTITY_NOT_OBSERVED"}
            ])
            write_rows(snapshot / "discovery" / "2026-11-09.jsonl.gz", [
                {"discovery_id": "d1", "market_key": "dex:test:1"}
            ])

            run_local_scan.merge_memory(snapshot, canonical, root / "memory.lock")
            run_local_scan.merge_memory(snapshot, canonical, root / "memory.lock")

            forecasts = run_local_scan._read_jsonl(canonical / "forecasts" / "2026-11-09.jsonl.gz")
            outcomes = run_local_scan._read_jsonl(canonical / "outcomes" / "2026-11-09.jsonl.gz")
            discoveries = run_local_scan._read_jsonl(canonical / "discovery" / "2026-11-09.jsonl.gz")
            self.assertEqual({r["forecast_id"] for r in forecasts}, {"old", "new"})
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(len(discoveries), 1)
            manifest = json.loads((canonical / "memory-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["historical_forecasts"], 2)
            self.assertTrue((canonical / "calibration-state.json").exists())

    def test_overlapping_rotation_snapshots_advance_without_reverting_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "memory"
            baseline = root / "baseline"
            snapshot1, snapshot2 = root / "snapshot-1", root / "snapshot-2"
            base_state = {"schema": "crypto-prebreakout.network-rotation.v1", "last_rotation_slot": 10,
                          "last_updated_utc": "2026-11-09T00:00:00Z", "cumulative_networks_swept": ["a"]}
            target = canonical / "network-rotation-state.json"
            target.parent.mkdir(parents=True)
            baseline.mkdir()
            target.write_text(json.dumps(base_state), encoding="utf-8")
            (baseline / target.name).write_text(json.dumps(base_state), encoding="utf-8")
            newer = {**base_state, "last_rotation_slot": 11, "last_updated_utc": "2026-11-09T01:00:00Z",
                     "cumulative_networks_swept": ["a", "b"]}
            for snap in (snapshot1, snapshot2):
                snap.mkdir()
                (snap / target.name).write_text(json.dumps(newer), encoding="utf-8")

            lock = root / "memory.lock"
            run_local_scan.merge_memory(snapshot1, canonical, lock, baseline)
            run_local_scan.merge_memory(snapshot2, canonical, lock, baseline)
            merged = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(merged["last_rotation_slot"], 12)
            self.assertEqual(set(merged["cumulative_networks_swept"]), {"a", "b"})

    def test_runner_defaults_to_durable_memory_name(self):
        self.assertEqual(run_local_scan.DEFAULT_MEMORY.name, "scanner-memory")

    def test_existing_bundled_memory_is_used_without_git_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Path(tmp) / "scanner-memory"
            memory.mkdir()
            manifest = memory / "memory-manifest.json"
            manifest.write_text('{"historical_forecasts": 225644}', encoding="utf-8")
            with patch.object(run_local_scan.subprocess, "run", side_effect=AssertionError("unexpected Git probe")), \
                 patch.object(run_local_scan.subprocess, "check_output", side_effect=AssertionError("unexpected Git archive")):
                run_local_scan.ensure_memory(memory)
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["historical_forecasts"], 225644)

    def test_overlay_runtime_isolated_from_working_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = run_local_scan.prepare_runtime(root / "runtime", time.monotonic() + 120,
                                                     root / "install.log")
            self.assertEqual(
                (runtime / "VERSION").read_text(encoding="utf-8").strip(),
                (run_local_scan.ROOT / "VERSION").read_text(encoding="utf-8").strip(),
            )
            source = (runtime / "scripts" / "rocket_hunt.py").read_text(encoding="utf-8")
            for marker in ("_iv419", "_v423_install", "_v424_install", "_v425_install"):
                self.assertIn(marker, source)
            self.assertNotIn("_iv419", (run_local_scan.ROOT / "scripts" / "rocket_hunt.py").read_text(encoding="utf-8"))
            for relative in ("scripts/rocket_hunt.py", "scripts/run_full_scan.py",
                             "scripts/forward_24h_report.py", "scripts/render_forward_watchlist_markdown.py"):
                result = subprocess.run([sys.executable, relative, "--help"], cwd=runtime,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        timeout=20, check=False)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            for relative in ("scripts/rocket_hunt.py", "scripts/run_full_scan.py",
                             "scripts/forward_24h_report.py", "scripts/render_forward_watchlist_markdown.py"):
                result = subprocess.run([sys.executable, relative, "--help"], cwd=runtime,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        timeout=20, check=False)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_dexpaprika_budget_reservation_is_shared_across_run_snapshots(self):
        from scripts import v425_early_breakout as early
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "shared"
            old = os.environ.get("ROCKET_HUNTER_SHARED_MEMORY_DIR")
            os.environ["ROCKET_HUNTER_SHARED_MEMORY_DIR"] = str(shared)
            try:
                ok1, count1 = early._reserve_request(root / "run-1", "trend")
                ok2, count2 = early._reserve_request(root / "run-2", "trend")
                self.assertTrue(ok1 and ok2)
                self.assertEqual((count1, count2), (1, 2))
                ledger = json.loads((shared / "dexpaprika-request-ledger.json").read_text(encoding="utf-8"))
                self.assertEqual(len(ledger["requests"]), 2)
            finally:
                if old is None:
                    os.environ.pop("ROCKET_HUNTER_SHARED_MEMORY_DIR", None)
                else:
                    os.environ["ROCKET_HUNTER_SHARED_MEMORY_DIR"] = old

    def test_parallel_dexpaprika_reservations_are_serialized_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "shared"
            env = os.environ.copy()
            env["ROCKET_HUNTER_SHARED_MEMORY_DIR"] = str(shared)
            code = (
                "from pathlib import Path; from scripts.v425_early_breakout import _reserve_request; "
                "print(_reserve_request(Path(__import__('sys').argv[1]), 'parallel'))"
            )
            children = [subprocess.Popen([sys.executable, "-c", code, str(root / f"run-{i}")],
                                         cwd=run_local_scan.ROOT, env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        for i in range(6)]
            results = [child.communicate(timeout=20) for child in children]
            self.assertTrue(all(child.returncode == 0 for child in children), results)
            ledger = json.loads((shared / "dexpaprika-request-ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(len(ledger["requests"]), 6)

    def test_birdeye_monthly_budget_is_shared_across_run_snapshots(self):
        from scripts import v424_birdeye_free as birdeye
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "shared"
            old_env = os.environ.get("ROCKET_HUNTER_SHARED_MEMORY_DIR")
            old_cap = birdeye.SCANNER_MONTHLY_CU_CEILING
            os.environ["ROCKET_HUNTER_SHARED_MEMORY_DIR"] = str(shared)
            birdeye.SCANNER_MONTHLY_CU_CEILING = 40
            try:
                self.assertTrue(birdeye._reserve_cu(root / "run-1", 30, "trend"))
                self.assertFalse(birdeye._reserve_cu(root / "run-2", 30, "trend"))
                ledger = json.loads((shared / "birdeye-cu-ledger.json").read_text(encoding="utf-8"))
                self.assertEqual(ledger["estimated_cu_reserved"], 30)
            finally:
                birdeye.SCANNER_MONTHLY_CU_CEILING = old_cap
                if old_env is None:
                    os.environ.pop("ROCKET_HUNTER_SHARED_MEMORY_DIR", None)
                else:
                    os.environ["ROCKET_HUNTER_SHARED_MEMORY_DIR"] = old_env

if __name__ == "__main__":
    unittest.main()
