import datetime as dt
import ctypes
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import research_run_contract as contract


UTC = dt.timezone.utc


class ResearchRunContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "research.sqlite3"
        self.now = dt.datetime(2026, 11, 16, 12, 0, tzinfo=UTC)

    def tearDown(self):
        self.temp.cleanup()

    def lease(self, run_id, **kwargs):
        return contract.ResearchRunLease(
            self.db, run_id, "test-collector", 120,
            heartbeat_seconds=100, stale_after_seconds=110,
            clock=lambda: self.now, **kwargs,
        )

    def test_scanner_and_collector_share_one_duplicate_run_gate(self):
        active = contract.ResearchRunLease(
            self.db, "scan-a", "local-scanner", 120,
            heartbeat_seconds=100, stale_after_seconds=110,
            clock=lambda: self.now, pid=1001, host_name="test-host",
        ).acquire()
        try:
            with self.assertRaises(contract.DuplicateResearchRun):
                contract.ResearchRunLease(
                    self.db, "collector-b", "watchlist-collector", 120,
                    heartbeat_seconds=100, stale_after_seconds=110,
                    clock=lambda: self.now, pid=1002, host_name="test-host",
                ).acquire()
        finally:
            active.finish("COMPLETED")

    def test_stale_dead_owner_is_marked_stale_and_not_resumed(self):
        abandoned = self.lease(
            "interrupted-run", pid=1001, host_name="old-host",
            is_process_alive=lambda _pid: False,
        ).acquire()
        abandoned._stop.set()
        old_heartbeat = self.now - dt.timedelta(seconds=120)
        connection = sqlite3.connect(self.db)
        connection.execute(
            "UPDATE research_run_lease SET heartbeat_utc=?", (contract.utc_iso(old_heartbeat),)
        )
        connection.execute(
            "UPDATE research_run_history SET heartbeat_utc=? WHERE run_id='interrupted-run'",
            (contract.utc_iso(old_heartbeat),),
        )
        connection.commit()
        connection.close()

        fresh = self.lease(
            "new-run", pid=1002, host_name="new-host",
            is_process_alive=lambda _pid: False,
        ).acquire()
        try:
            connection = sqlite3.connect(self.db)
            prior = connection.execute(
                "SELECT status, recovery_count, detail FROM research_run_history "
                "WHERE run_id='interrupted-run'"
            ).fetchone()
            connection.close()
            self.assertEqual(prior, ("STALE", 1, "stale heartbeat; not resumed"))
            self.assertEqual(
                fresh.run_id, "new-run",
                "recovery claims a fresh run and never resumes the abandoned run",
            )
        finally:
            fresh.finish("COMPLETED")

    def test_stale_heartbeat_with_live_local_process_fails_closed(self):
        active = self.lease("still-alive", pid=1001, host_name="test-host").acquire()
        active._stop.set()
        old_heartbeat = self.now - dt.timedelta(seconds=120)
        connection = sqlite3.connect(self.db)
        connection.execute(
            "UPDATE research_run_lease SET heartbeat_utc=?", (contract.utc_iso(old_heartbeat),)
        )
        connection.commit()
        connection.close()
        try:
            with self.assertRaises(contract.DuplicateResearchRun):
                self.lease(
                    "blocked-run", pid=1002, host_name="test-host",
                    is_process_alive=lambda pid: pid == 1001,
                ).acquire()
        finally:
            active.finish("INTERRUPTED")

    def test_run_deadline_and_heartbeat_parameters_are_bounded(self):
        with self.assertRaises(contract.ResearchRunError):
            contract.ResearchRunLease(self.db, "too-long", "scanner", 10801)
        with self.assertRaises(contract.ResearchRunError):
            contract.ResearchRunLease(
                self.db, "bad-heartbeat", "scanner", 60,
                heartbeat_seconds=60, stale_after_seconds=60,
            )

    def test_check_health_fails_closed_after_heartbeat_loss_or_lease_removal(self):
        active = self.lease("health-check").acquire()
        try:
            active._heartbeat_error = OSError("simulated heartbeat write failure")
            with self.assertRaisesRegex(contract.ResearchRunError, "heartbeat failed"):
                active.check_health()

            active._heartbeat_error = None
            connection = sqlite3.connect(self.db)
            connection.execute(
                "DELETE FROM research_run_lease WHERE run_id='health-check'"
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(contract.ResearchRunError, "lease.*lost"):
                active.check_health()
        finally:
            active.finish("INTERRUPTED")

    def test_windows_pid_probe_uses_mocked_read_only_api_and_never_os_kill(self):
        from ctypes import wintypes

        kernel32 = Mock()
        kernel32.OpenProcess.return_value = ctypes.c_void_p(0x1234)

        def set_active(_handle, exit_code_pointer):
            ctypes.cast(
                exit_code_pointer, ctypes.POINTER(wintypes.DWORD)
            ).contents.value = 259
            return True

        kernel32.GetExitCodeProcess.side_effect = set_active
        self.assertTrue(contract._windows_process_is_alive(
            4321, kernel32=kernel32, last_error=lambda: 0,
        ))
        self.assertEqual(
            ctypes.sizeof(wintypes.HANDLE), ctypes.sizeof(ctypes.c_void_p)
        )
        kernel32.OpenProcess.assert_called_once_with(0x1000, False, 4321)
        kernel32.CloseHandle.assert_called_once()
        self.assertEqual(kernel32.CloseHandle.call_args.args[0].value, 0x1234)

        with patch.object(contract.os, "name", "nt"), \
             patch.object(contract.os, "kill") as posix_probe, \
             patch.object(contract, "_windows_process_is_alive", return_value=True) as win_probe:
            self.assertTrue(contract.process_is_alive(4321))
        win_probe.assert_called_once_with(4321)
        posix_probe.assert_not_called()

    def test_windows_pid_probe_treats_only_invalid_parameter_as_definitively_dead(self):
        kernel32 = Mock()
        kernel32.OpenProcess.return_value = None

        self.assertFalse(contract._windows_process_is_alive(
            4321, kernel32=kernel32, last_error=lambda: 87,
        ))
        self.assertTrue(contract._windows_process_is_alive(
            4321, kernel32=kernel32, last_error=lambda: 5,
        ))
        self.assertTrue(contract._windows_process_is_alive(
            4321, kernel32=kernel32, last_error=lambda: 12345,
        ))

        kernel32.OpenProcess.return_value = ctypes.c_void_p(0x1234)
        kernel32.GetExitCodeProcess.return_value = False
        self.assertTrue(contract._windows_process_is_alive(
            4321, kernel32=kernel32, last_error=lambda: 5,
        ))
        self.assertFalse(contract._windows_process_is_alive(
            4321, kernel32=kernel32, last_error=lambda: 87,
        ))

    def test_final_manifest_counts_and_evidence_states_are_explicit(self):
        path = Path(self.temp.name) / "manifest.json"
        path.write_text(json.dumps({
            "status": "ONE_CYCLE_COMPLETED",
            "collection_run_id": "run-1",
            "record_count": 2,
        }), encoding="utf-8")
        manifest = contract.read_collection_manifest(path)
        self.assertTrue(manifest["finalized"])
        self.assertEqual(manifest["evidence_status"], "COMPLETE")
        self.assertEqual(contract.collection_evidence_status(
            "ONE_CYCLE_COMPLETED", 2, 1, finalized=True, file_present=True,
        ), "MISSING")
        self.assertEqual(contract.collection_evidence_status(
            "ONE_CYCLE_COMPLETED", 2, 3, finalized=True, file_present=True,
        ), "CONFLICTING")
        self.assertEqual(contract.collection_evidence_status(
            "PROVIDER_RATE_LIMITED", 2, 2, finalized=True, file_present=True,
        ), "FAILED")
        self.assertEqual(contract.collection_evidence_status(
            "RUNNING", 2, None, finalized=False, file_present=False,
        ), "PENDING")
        failed_path = Path(self.temp.name) / "failed-manifest.json"
        failed_path.write_text(json.dumps({
            "status": "FAILED",
            "collection_run_id": "failed-run",
            "record_count": 1,
        }), encoding="utf-8")
        failed = contract.read_collection_manifest(failed_path)
        self.assertTrue(failed["finalized"])
        self.assertEqual(failed["evidence_status"], "FAILED")
        self.assertEqual(contract.collection_evidence_status(
            "FAILED", 1, 1, finalized=True, file_present=True,
        ), "FAILED")

    def test_malformed_final_manifest_and_corrupt_records_fail_closed(self):
        manifest_path = Path(self.temp.name) / "manifest.json"
        manifest_path.write_text(json.dumps({
            "status": "COMPLETED",
            "collection_run_id": "run-1",
            "record_count": True,
        }), encoding="utf-8")
        with self.assertRaises(contract.CollectionManifestError):
            contract.read_collection_manifest(manifest_path)

        records_path = Path(self.temp.name) / "observations.jsonl"
        records_path.write_text("{broken json\n", encoding="utf-8")
        with self.assertRaises(contract.ResearchRunError):
            contract.count_jsonl_records(records_path, "run-1")

    def test_pending_manifest_never_requires_record_fields_or_final_counts(self):
        manifest_path = Path(self.temp.name) / "pending.json"
        manifest_path.write_text(json.dumps({"status": "IN_PROGRESS"}), encoding="utf-8")
        pending = contract.read_collection_manifest(manifest_path)
        self.assertFalse(pending["finalized"])
        self.assertEqual(pending["evidence_status"], "PENDING")
        self.assertIsNone(pending["record_count"])


if __name__ == "__main__":
    unittest.main()
