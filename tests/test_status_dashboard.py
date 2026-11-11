import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.status_dashboard import DashboardError, main, render, summarize


class StatusDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runs = self.root / "rocket-runs" / "run-1"
        self.runs.mkdir(parents=True)
        self.collections = (
            self.root / "rocket-hunter-data" / "research-data"
            / "watchlist-observations" / "collection-fixture"
        )
        self.collections.mkdir(parents=True)
        self.analysis_root = self.root / "rocket-hunter-data" / "research-analysis"
        (self.analysis_root / "candle-fixture").mkdir(parents=True)
        (self.analysis_root / "marks-fixture").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def _write_json(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def _write_inputs(self):
        self._write_json(self.runs / "rocket-hunt.json", {
            "scan_status": "COMPLETED",
            "run_id": "run-<img src=x onerror=alert(1)>",
            "scan_started_utc": "2026-11-09T12:00:00Z",
            "scan_finished_utc": "2026-11-09T12:01:00Z",
            "api_key": "must-not-be-rendered",
        })
        older_run = self.runs.parent / "run-older"
        older_run.mkdir()
        self._write_json(older_run / "rocket-hunt.json", {
            "scan_status": "INTERRUPTED",
            "run_id": "older-run",
            "scan_started_utc": "2026-11-08T12:00:00Z",
            "scan_finished_utc": "2026-11-08T12:01:00Z",
        })
        self._write_json(self.collections / "manifest.json", {
            "status": "PROVIDER_RATE_LIMITED",
            "started_utc": "2026-11-09T12:59:00Z",
            "finished_utc": "2026-11-09T13:00:00Z",
            "record_count": 1,
            "collection_run_id": "fixture",
            "requests_started": 1,
            "output_file": "observations.jsonl",
            "provider_rate_limit": {
                "provider": "provider-fixture",
                "http_status": 429,
                "cooldown_until_utc": "2026-11-09T14:00:00Z",
            },
        })
        observation = {
            "collection_run_id": "fixture",
            "source_status": "OBSERVED",
            "source_received_utc": "2026-11-09T15:00:00Z",
            "source_evidence": {
                "bars": [{
                    "time_open": "2026-11-09T13:00:00Z",
                    "time_close": "2026-11-09T14:00:00Z",
                    "high": 12,
                    "low": 8,
                    "close": 10,
                }],
            },
        }
        (self.collections / "observations.jsonl").write_text(
            json.dumps(observation) + "\n", encoding="utf-8"
        )
        self._write_json(self.analysis_root / "candle-fixture" / "report.json", {
            "analysis_version": "rocket-hunter-candle-outcomes.v3",
            "analysis_as_of_utc": "2026-11-09T15:01:00Z",
            "record_counts": {
                "forecasts_loaded": 22,
                "observations_loaded": 1,
                "forecasts_joined_to_observations": 1,
            },
            "candle_level_metrics": {"complete_coverage_denominator": 1},
            "excluded_forecast_reasons": {"no_eligible_observation_for_exact_identity": 4},
            "incomplete_forecast_reasons": {"missing_elapsed_hours": 1},
        })
        self._write_json(self.analysis_root / "marks-fixture" / "report.json", {
            "analysis_version": "rocket-hunter-outcome-analysis.v1.1",
            "analysis_as_of_utc": "2026-11-09T15:02:00Z",
            "forecast_detection_count": 22,
            "horizon_metrics": {
                "24": {
                    "observed_mark_count": 4,
                    "bounded_lag_mark_count": 3,
                    "missing_outcome_record_count": 18,
                    "unavailable_outcome_count": 1,
                    "adverse_endpoint_marks": {
                        "below_base_mark_count": 2,
                        "below_base_mark_rate": 0.5,
                    },
                    "targets": {
                        "+40%": {"observed_mark_attainments": 1, "rate": 1 / 3},
                        "+60%": {"observed_mark_attainments": 0, "rate": 0},
                        "+100%": {"observed_mark_attainments": 0, "rate": 0},
                    },
                },
            },
        })

    def test_renders_local_status_coverage_and_sampled_mark_caveat(self):
        self._write_inputs()
        report = render(summarize(self.root))

        self.assertIn("COMPLETED", report)
        self.assertIn("<h2>Scan history</h2>", report)
        self.assertIn("IST", report)
        self.assertIn("INTERRUPTED", report)
        self.assertIn("older-run", report)
        self.assertIn("Adaptive AI Learning", report)
        self.assertNotIn("Visual Telemetry", report)
        # Verify that all 6 obsolete/messy sections are strictly deleted
        self.assertNotIn("<h2>Collection sessions</h2>", report)
        self.assertNotIn("<h2>Hourly observation coverage</h2>", report)
        self.assertNotIn("<h2>Sampled-mark summary", report)
        self.assertNotIn("<h2>Missed-opportunity analysis", report)
        self.assertNotIn("<h2>Readiness blockers</h2>", report)
        self.assertNotIn("<h2>Phase 6 promotion evidence", report)
        self.assertNotIn("must-not-be-rendered", report)
        self.assertIn("run-&lt;img src=x onerror=alert(1)&gt;", report)
        self.assertNotIn("<img src=x", report)
        self.assertNotIn("https://", report)

    def test_available_bar_total_survives_empty_failed_collection_artifact(self):
        self._write_inputs()
        empty_run = self.collections.parent / "collection-empty"
        empty_run.mkdir()
        self._write_json(empty_run / "manifest.json", {
            "status": "FAILED",
            "record_count": 0,
            "requests_started": 0,
            "output_file": "observations.jsonl",
        })

        summary = summarize(self.root)
        report = render(summary)

        self.assertEqual(summary["usable_bars"], 1)
        self.assertEqual(summary["missing_observation_files"], 1)
        self.assertEqual(summary["missing_observation_records"], 0)
        self.assertNotIn("<h2>Collection sessions</h2>", report)

    def test_unfinalized_collection_observations_are_not_read(self):
        self._write_inputs()
        active_run = self.collections.parent / "collection-active"
        active_run.mkdir()
        self._write_json(active_run / "manifest.json", {
            "status": "RUNNING",
            "record_count": 1,
            "collection_run_id": "active",
            "requests_started": 1,
            "output_file": "observations.jsonl",
        })
        (active_run / "observations.jsonl").write_text(
            "partial data that must not be read\n", encoding="utf-8"
        )

        summary = summarize(self.root)
        report = render(summary)

        active = next(item for item in summary["collections"] if item["directory"] == "collection-active")
        self.assertEqual(active["lifecycle"], "Not finalized; observations not read")
        self.assertIsNone(active["usable_bars"])
        self.assertEqual(summary["pending_observation_sessions"], 1)

    def test_collection_without_manifest_is_pending_and_observations_are_not_read(self):
        self._write_inputs()
        interrupted_run = self.collections.parent / "collection-interrupted"
        interrupted_run.mkdir()
        (interrupted_run / "observations.jsonl").write_text(
            "partial data that must not be read\n", encoding="utf-8"
        )

        summary = summarize(self.root)
        report = render(summary)

        pending = next(
            item for item in summary["collections"]
            if item["directory"] == "collection-interrupted"
        )
        self.assertEqual(pending["status"], "PENDING_FINALIZATION")
        self.assertEqual(
            pending["lifecycle"],
            "Not finalized; manifest unavailable; observations not read",
        )
        self.assertIsNone(pending["record_count"])
        self.assertIsNone(pending["usable_bars"])
        self.assertFalse(pending["observations_available"])
        self.assertEqual(summary["pending_observation_sessions"], 1)

    def test_missing_values_are_unknown_or_unavailable_not_zero_misses(self):
        summary = summarize(self.root)
        report = render(summary)

        self.assertEqual(summary["cooldown"], "Unknown (no persisted provider cooldown record)")
        self.assertIn("Unavailable", report)
        self.assertNotIn("0 misses", report.lower())
        self.assertNotIn("target misses</td><td>0", report.lower())
        self.assertNotIn("0 misses", report.lower())
        self.assertNotIn("target misses</td><td>0", report.lower())

    def test_renders_read_only_point_in_time_system_diagnostics(self):
        diagnostics = {
            "cpu_model": "Fixture CPU",
            "logical_processors": 8,
            "available_ram_bytes": 4 * 1024 ** 3,
            "python_version": "3.fixture",
            "project_drive": {"volume_type": "Fixed", "free_bytes": 2 * 1024 ** 3},
            "data_drive": {"volume_type": "Fixed", "free_bytes": 3 * 1024 ** 3},
            "nvidia_gpus": None,
        }
        with patch("scripts.status_dashboard.local_diagnostics.collect_diagnostics",
                   return_value=diagnostics):
            report = render(summarize(self.root))

        self.assertIn("System health · point-in-time diagnostics", report)
        self.assertIn("CPU: Fixture CPU; logical processors: 8", report)
        self.assertIn("Available RAM: 4.00 GiB", report)
        self.assertIn("no health thresholds are applied", report)

    def test_refuses_collection_output_path_escape(self):
        self._write_json(self.collections / "manifest.json", {
            "output_file": "..\\..\\..\\outside.jsonl",
        })

        with self.assertRaises(DashboardError):
            summarize(self.root)

    def test_counts_only_manifest_declared_records_with_matching_run_id(self):
        self._write_inputs()
        self._write_json(self.collections / "manifest.json", {
            "status": "COMPLETED",
            "finished_utc": "2026-11-09T13:00:00Z",
            "record_count": 2,
            "collection_run_id": "fixture",
            "requests_started": 2,
            "output_file": "observations.jsonl",
        })
        first = json.loads((self.collections / "observations.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()[0])
        mismatched = dict(first, collection_run_id="other-run")
        beyond_count = dict(first, collection_run_id="fixture")
        (self.collections / "observations.jsonl").write_text(
            "\n".join(json.dumps(item) for item in (first, mismatched, beyond_count)) + "\n",
            encoding="utf-8",
        )

        summary = summarize(self.root)

        self.assertEqual(summary["usable_bars"], 1)
        self.assertEqual(summary["collections"][0]["statuses"], {"OBSERVED": 1})
        self.assertEqual(summary["collections"][0]["excluded_rows"], {
            "beyond_manifest_record_count": 1,
            "collection_run_id_mismatch": 1,
        })

    def test_cli_writes_static_html_without_overwriting_source_artifacts(self):
        self._write_inputs()
        output = self.root / "dashboard.html"

        self.assertEqual(main(["--root", str(self.root), "--output", str(output)]), 0)
        self.assertTrue(output.is_file())
        self.assertIn("Rocket Hunter status", output.read_text(encoding="utf-8"))

        with self.assertRaises(DashboardError):
            main(["--root", str(self.root), "--output", str(self.collections / "observations.jsonl")])


if __name__ == "__main__":
    unittest.main()
