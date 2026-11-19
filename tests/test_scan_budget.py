import contextlib
import io
import json
import signal
import subprocess
import tempfile
import time
import urllib.error
import unittest
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from scripts.process_runner import run_bounded
from scripts.run_full_scan import (
    _mark_scan_state, _run_bounded, build_parser, build_scan_command,
    gecko_pacing_budget_minutes,
)
from scripts.rocket_hunt import RocketHunter
from scripts.post_scan_audit import audit_gecko_spot_cvd
from scripts.venues import Fetcher
from scripts import live_price_verifier


class ScanBudgetTests(unittest.TestCase):
    def test_scan_defaults_are_dex_only_and_sweep_the_bounded_network_registry(self):
        args = build_parser().parse_args(["--outdir", "run-test"])

        self.assertEqual(args.network_sweep_limit, 128)
        self.assertIsNone(args.venue)
        self.assertNotIn("--venue", build_scan_command(args, Path("run-test")))

    def test_gecko_access_or_rate_limit_halts_followup_requests_without_retry(self):
        for status in (403, 418, 429):
            with self.subTest(status=status):
                hunter = RocketHunter()
                limited_fx = Mock()
                limited_fx.timeout = 8
                limited_fx.log = [{
                    "url": "https://api.geckoterminal.com/example",
                    "status": status,
                    "error": "HTTP %d" % status,
                }]
                limited_fx.get.return_value = None

                with patch("scripts.rocket_hunt.venues.Fetcher", return_value=limited_fx) as factory:
                    self.assertIsNone(hunter._fetch_gecko_json(
                        "gecko_test", "https://api.geckoterminal.com/example"
                    ))
                    self.assertIsNone(hunter._fetch_gecko_json(
                        "gecko_test", "https://api.geckoterminal.com/next"
                    ))

                self.assertEqual(factory.call_args.kwargs["retries"], 0)
                limited_fx.get.assert_called_once()

    def test_gecko_cvd_uses_versioned_accept_header_and_stops_after_rate_limit(self):
        candidates = [
            {"chain": "base", "pool_address": "pool-1"},
            {"chain": "base", "pool_address": "pool-2"},
        ]
        limited_response = (None, {"ok": False, "status": 429, "error": "HTTP 429"})
        with patch("scripts.post_scan_audit._select_dex_candidates", return_value=candidates), \
             patch("scripts.post_scan_audit._json_get", return_value=limited_response) as request:
            result = audit_gecko_spot_cvd({}, limit=2)

        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["headers"]["Accept"], "application/json;version=20230203")
        self.assertEqual(request.call_args.kwargs["retries"], 0)
        self.assertEqual(result["audited"][1]["status"], "SKIPPED")

    def test_gecko_fetcher_uses_required_versioned_accept_header(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b"{}"
        response.__enter__.return_value = response
        with patch("scripts.venues.urllib.request.urlopen", return_value=response) as request:
            self.assertEqual(Fetcher().get("https://api.geckoterminal.com/api/v2/networks/base/pools"), {})

        request_object = request.call_args.args[0]
        self.assertEqual(request_object.get_header("Accept"), "application/json;version=20230203")

    def test_gecko_security_check_stops_all_requests_after_rate_limit(self):
        error = urllib.error.HTTPError(
            "https://api.geckoterminal.com/example", 429, "Too Many Requests", {}, None,
        )
        with patch.object(live_price_verifier, "_GECKO_REQUESTS_HALTED", None), \
             patch("scripts.live_price_verifier.urllib.request.urlopen", side_effect=error) as request:
            bad, reason = live_price_verifier.check_geckoterminal_security(
                "eth", "0x" + "a" * 40,
            )
            next_bad, next_reason = live_price_verifier.check_geckoterminal_security(
                "eth", "0x" + "b" * 40,
            )

        self.assertTrue(bad)
        self.assertTrue(next_bad)
        self.assertIn("UNVERIFIED_GECKOTERMINAL_REQUESTS_HALTED", reason)
        self.assertIn("UNVERIFIED_GECKOTERMINAL_REQUESTS_HALTED", next_reason)
        request.assert_called_once()

    def test_interrupted_scan_cannot_leave_stale_actionable_pick(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            (out / "forward-24h-rocket.json").write_text(json.dumps({
                "status": "SUCCESS", "verified_actionable_opportunities": [{"asset": "old"}],
                "best_immediate_entry": {"asset": "old"}, "top_rocket_execution_ready": True,
            }))
            _mark_scan_state(out, "INTERRUPTED", "2026-11-09T00:00:00Z", time.monotonic(), "run-x")
            doc = json.loads((out / "forward-24h-rocket.json").read_text())
            self.assertEqual(doc["scan_status"], "INTERRUPTED")
            self.assertEqual(doc["verified_actionable_opportunities"], [])
            self.assertIsNone(doc["best_immediate_entry"])
            self.assertFalse(doc["top_rocket_execution_ready"])

    def test_subprocess_is_cancelled_at_deadline(self):
        result = _run_bounded([sys.executable, "-c", "import time; time.sleep(5)"],
                             Path.cwd(), time.monotonic() + 0.1)
        self.assertEqual(result.returncode, 124)

    def test_windows_interrupt_requests_graceful_child_shutdown(self):
        cwd = Path.cwd()
        process = Mock()
        process._handle = 7
        process.pid = 42
        process.wait.side_effect = [KeyboardInterrupt(), None]
        process.poll.return_value = 0
        kernel32 = Mock()
        kernel32.AssignProcessToJobObject.return_value = True
        kernel32.GenerateConsoleCtrlEvent.return_value = True

        with patch("scripts.process_runner.os.name", "nt"), \
             patch("scripts.process_runner._windows_job", return_value=(kernel32, 99)), \
             patch("scripts.process_runner.subprocess.Popen", return_value=process):
            with self.assertRaises(KeyboardInterrupt):
                run_bounded(["scanner"], cwd, 10)

        kernel32.GenerateConsoleCtrlEvent.assert_called_once_with(1, process.pid)
        kernel32.TerminateJobObject.assert_not_called()
        process.wait.assert_any_call(timeout=5.0)
        kernel32.CloseHandle.assert_called_once_with(99)

    def test_windows_interrupt_force_stops_child_after_grace_period(self):
        cwd = Path.cwd()
        process = Mock()
        process._handle = 7
        process.pid = 42
        process.wait.side_effect = [
            KeyboardInterrupt(), subprocess.TimeoutExpired(["scanner"], 5.0), None,
        ]
        process.poll.return_value = None
        kernel32 = Mock()
        kernel32.AssignProcessToJobObject.return_value = True
        kernel32.GenerateConsoleCtrlEvent.return_value = True
        kernel32.TerminateJobObject.return_value = True

        with patch("scripts.process_runner.os.name", "nt"), \
             patch("scripts.process_runner._windows_job", return_value=(kernel32, 99)), \
             patch("scripts.process_runner.subprocess.Popen", return_value=process):
            with self.assertRaises(KeyboardInterrupt):
                run_bounded(["scanner"], cwd, 10)

        kernel32.TerminateJobObject.assert_called_once_with(99, 130)
        process.wait.assert_any_call(timeout=5.0)

    def test_posix_interrupt_signals_the_child_process_group(self):
        cwd = Path.cwd()
        process = Mock()
        process.pid = 42
        process.wait.side_effect = [KeyboardInterrupt(), None]
        process.poll.return_value = 0

        with patch("scripts.process_runner.os.name", "posix"), \
             patch("scripts.process_runner.os.killpg", side_effect=[None, ProcessLookupError()],
                   create=True) as killpg, \
             patch("scripts.process_runner.subprocess.Popen", return_value=process):
            with self.assertRaises(KeyboardInterrupt):
                run_bounded(["scanner"], cwd, 10)

        self.assertEqual(killpg.call_args_list[0].args, (42, signal.SIGTERM))
        self.assertEqual(killpg.call_args_list[1].args, (42, 0))

    def test_full_scan_resolves_scripts_package_outside_project_working_directory(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_full_scan.py"
        code = (
            "import runpy,sys,time; from pathlib import Path; "
            "module=runpy.run_path(sys.argv[1]); "
            "result=module['_run_bounded']([sys.executable,'-c','pass'],Path.cwd(),"
            "time.monotonic()+5); raise SystemExit(result.returncode)"
        )
        with tempfile.TemporaryDirectory() as working_directory:
            result = subprocess.run(
                [sys.executable, "-c", code, str(script)],
                cwd=working_directory, capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_deadline_kills_descendant_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "orphan-wrote-file"
            child_code = (
                "import time; from pathlib import Path; time.sleep(0.8); "
                f"Path({str(marker)!r}).write_text('unexpected', encoding='utf-8')"
            )
            parent_code = (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c',sys.argv[1]]); time.sleep(10)"
            )
            result = run_bounded(
                [sys.executable, "-c", parent_code, child_code],
                Path(temp), 0.25,
            )
            self.assertEqual(result.returncode, 124)
            time.sleep(1.0)
            self.assertFalse(marker.exists(), "descendant survived the parent deadline")

    def test_gecko_pacing_estimate_counts_paginated_network_and_registry_requests(self):
        self.assertAlmostEqual(gecko_pacing_budget_minutes(1, 12, 12), 10.2)
        self.assertAlmostEqual(gecko_pacing_budget_minutes(3, 32, 12), 23.8)


if __name__ == "__main__":
    unittest.main()
