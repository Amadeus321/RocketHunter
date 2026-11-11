import json
import os
import platform
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import local_diagnostics, run_full_scan, run_local_scan


class LocalDiagnosticsTests(unittest.TestCase):
    def test_format_marks_unavailable_hardware_details(self):
        info = {
            "cpu_model": "Unavailable",
            "logical_processors": None,
            "available_ram_bytes": None,
            "python_version": "3.test",
            "project_drive": {
                "volume_type": "Unavailable (query failed)",
                "free_bytes": "Unavailable (query failed)",
            },
            "data_drive": {
                "volume_type": "Fixed",
                "free_bytes": 1024 ** 3,
            },
            "nvidia_gpus": None,
        }

        output = local_diagnostics.format_diagnostics(info)

        self.assertIn("CPU: Unavailable; logical processors: Unavailable", output)
        self.assertIn("Available RAM: Unavailable", output)
        self.assertIn("Project drive: volume type=Unavailable (query failed); free space=Unavailable (query failed)", output)
        self.assertIn("Data drive: volume type=Fixed; free space=1.00 GiB", output)
        self.assertIn("NVIDIA GPU: Unavailable", output)

    def test_nvidia_query_returns_device_details_without_raw_errors(self):
        completed = type("Completed", (), {
            "returncode": 0,
            "stdout": "NVIDIA GeForce Test, 8192 MiB, 4096 MiB, 555.1\n",
        })()
        with patch.object(local_diagnostics.shutil, "which", return_value="nvidia-smi"), \
             patch.object(local_diagnostics.subprocess, "run", return_value=completed):
            gpus = local_diagnostics._nvidia_gpus()

        self.assertEqual(gpus, [{
            "name": "NVIDIA GeForce Test",
            "memory_total": "8192 MiB",
            "memory_free": "4096 MiB",
            "driver_version": "555.1",
        }])

    def test_nvidia_details_are_unavailable_when_tool_is_missing(self):
        with patch.object(local_diagnostics.shutil, "which", return_value=None):
            self.assertIsNone(local_diagnostics._nvidia_gpus())

    def test_diagnostics_do_not_print_environment_secrets(self):
        secret = "local-diagnostics-test-secret"
        with patch.dict(os.environ, {"ROCKET_HUNTER_TEST_SECRET": secret}):
            output = local_diagnostics.format_diagnostics({
                "cpu_model": "Test CPU",
                "logical_processors": 1,
                "available_ram_bytes": 1024,
                "python_version": "3.test",
                "project_drive": {"volume_type": "Fixed", "free_bytes": 1024},
                "data_drive": {"volume_type": "Fixed", "free_bytes": 1024},
                "nvidia_gpus": None,
            })
        self.assertNotIn(secret, output)

    def test_run_metadata_fingerprint_is_stable_and_tracks_configuration(self):
        parser = run_local_scan.build_arg_parser()
        first_args = parser.parse_args(["--outdir", "run-one"])
        same_args = parser.parse_args(["--outdir", "run-two"])
        changed_args = parser.parse_args(["--outdir", "run-three", "--workers", "3"])

        first = run_local_scan.build_run_metadata(first_args)
        same = run_local_scan.build_run_metadata(same_args)
        changed = run_local_scan.build_run_metadata(changed_args)

        self.assertEqual(first["scanner_version"], (run_local_scan.ROOT / "VERSION").read_text().strip())
        self.assertEqual(first["python_runtime_version"], platform.python_version())
        self.assertEqual(first["configuration_fingerprint"], same["configuration_fingerprint"])
        self.assertNotEqual(first["configuration_fingerprint"], changed["configuration_fingerprint"])
        self.assertNotIn("api_key", str(first["local_configuration"]).lower())

    def test_scan_state_updates_preserve_local_run_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp)
            metadata = {
                "run_id": "run-test",
                "scanner_version": "4.26.6",
                "python_runtime_version": platform.python_version(),
                "configuration_fingerprint": "config-hash",
                "local_configuration": {"scanner_workers": 4},
            }
            (outdir / "scan-status.json").write_text(json.dumps(metadata), encoding="utf-8")

            run_full_scan._mark_scan_state(outdir, "IN_PROGRESS", "2026-11-09T00:00:00Z",
                                           time.monotonic(), run_id="run-test")

            status = json.loads((outdir / "scan-status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["scanner_version"], metadata["scanner_version"])
            self.assertEqual(status["python_runtime_version"], metadata["python_runtime_version"])
            self.assertEqual(status["configuration_fingerprint"], metadata["configuration_fingerprint"])
            self.assertEqual(status["local_configuration"], metadata["local_configuration"])

            run_full_scan._mark_scan_state(outdir, "IN_PROGRESS", "2026-11-09T00:00:00Z",
                                           time.monotonic(), run_id="hosted-run")
            hosted_status = json.loads((outdir / "scan-status.json").read_text(encoding="utf-8"))
            self.assertNotIn("scanner_version", hosted_status)
            self.assertNotIn("python_runtime_version", hosted_status)


if __name__ == "__main__":
    unittest.main()
