import json
import tempfile
import unittest
from pathlib import Path

from scripts.research_readiness import render, summarize


class ResearchReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.collections = self.root / "watchlist-observations"
        self.analyses = self.root / "research-analysis"
        self.run_dir = self.collections / "collection-fixture"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "manifest.json").write_text(json.dumps({
            "status": "PROVIDER_RATE_LIMITED",
            "record_count": 1,
            "collection_run_id": "fixture",
            "output_file": "observations.jsonl",
            "provider_rate_limit": None,
        }), encoding="utf-8")
        record = {
            "collection_run_id": "fixture",
            "source_status": "PROVIDER_RATE_LIMITED",
            "source_received_utc": "2026-11-09T15:00:00Z",
            "source_evidence": {"resolution": "1h", "bars": []},
        }
        (self.run_dir / "observations.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_reports_rate_limit_without_treating_it_as_a_target_miss(self):
        result = summarize(self.collections, self.analyses)
        text = render(result)

        self.assertEqual(result["status_counts"], {"PROVIDER_RATE_LIMITED": 1})
        self.assertEqual(result["usable_bars"], 0)
        self.assertEqual(result["provider_cooldown"], "unknown (no persisted provider cooldown record)")
        self.assertTrue(any("no complete 24-hour" in item.lower() for item in result["blockers"]))
        self.assertIn("Overall platform readiness: NOT READY", text)
        self.assertIn("HTTP 403/429", text)
        self.assertIn("not target misses", text)
        self.assertIn("Sampled marks are endpoint observations only", text)

    def test_one_cycle_and_duration_elapsed_recognized_as_finalized(self):
        one_cycle = self.collections / "collection-one-cycle"
        one_cycle.mkdir()
        (one_cycle / "manifest.json").write_text(json.dumps({
            "status": "ONE_CYCLE_COMPLETED",
            "record_count": 1,
            "collection_run_id": "cycle-id",
            "output_file": "observations.jsonl",
        }), encoding="utf-8")
        row = {
            "collection_run_id": "cycle-id",
            "source_status": "OBSERVED",
            "source_received_utc": "2026-11-09T15:00:00Z",
            "source_evidence": {"bars": [{
                "time_open": "2026-11-09T13:00:00Z",
                "time_close": "2026-11-09T14:00:00Z",
                "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 100.0,
            }]},
        }
        (one_cycle / "observations.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        result = summarize(self.collections, self.analyses)
        self.assertNotIn("collection-one-cycle", " ".join(result["pending_collection_details"]))
        self.assertEqual(result["status_counts"]["OBSERVED"], 1)
        self.assertEqual(result["usable_bars"], 1)

    def test_counts_only_completed_valid_hourly_bars(self):
        (self.run_dir / "manifest.json").write_text(json.dumps({
            "status": "COMPLETED",
            "record_count": 1,
            "collection_run_id": "fixture",
            "output_file": "observations.jsonl",
        }), encoding="utf-8")
        record = {
            "collection_run_id": "fixture",
            "source_status": "OBSERVED",
            "source_received_utc": "2026-11-09T15:00:00Z",
            "source_evidence": {
                "resolution": "1h",
                "bars": [
                    {
                        "time_open": "2026-11-09T13:00:00Z",
                        "time_close": "2026-11-09T14:00:00Z",
                        "high": 12,
                        "low": 8,
                        "close": 10,
                    },
                    {
                        "time_open": "2026-11-09T14:00:00Z",
                        "time_close": "2026-11-09T16:00:00Z",
                        "high": 12,
                        "low": 8,
                        "close": 10,
                    },
                ],
            },
        }
        (self.run_dir / "observations.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8"
        )

        result = summarize(self.collections, self.analyses)

        self.assertEqual(result["usable_bars"], 1)

    def test_counts_only_manifest_declared_records_with_matching_run_id(self):
        manifest_path = self.run_dir / "manifest.json"
        manifest_path.write_text(json.dumps({
            "status": "COMPLETED",
            "record_count": 2,
            "collection_run_id": "fixture",
            "output_file": "observations.jsonl",
        }), encoding="utf-8")
        observed = {
            "collection_run_id": "fixture",
            "source_status": "OBSERVED",
            "source_received_utc": "2026-11-09T15:00:00Z",
            "source_evidence": {"bars": [{
                "time_open": "2026-11-09T13:00:00Z",
                "time_close": "2026-11-09T14:00:00Z",
                "high": 12, "low": 8, "close": 10,
            }]},
        }
        wrong_run = dict(observed, collection_run_id="other-run")
        beyond_count = dict(observed, collection_run_id="fixture")
        (self.run_dir / "observations.jsonl").write_text(
            "\n".join(json.dumps(item) for item in (observed, wrong_run, beyond_count)) + "\n",
            encoding="utf-8",
        )

        result = summarize(self.collections, self.analyses)

        self.assertEqual(result["usable_bars"], 1)
        self.assertEqual(result["status_counts"], {"OBSERVED": 1})
        self.assertEqual(result["excluded_observation_records"], {
            "beyond_manifest_record_count": 1,
            "collection_run_id_mismatch": 1,
        })
        self.assertEqual(result["collections"][0]["excluded_records"], {
            "beyond_manifest_record_count": 1,
            "collection_run_id_mismatch": 1,
        })

    def test_missing_observation_artifact_is_reported_without_inventing_zero_bars(self):
        (self.run_dir / "manifest.json").write_text(json.dumps({
            "status": "FAILED",
            "record_count": 1,
            "collection_run_id": "fixture",
            "output_file": "observations.jsonl",
        }), encoding="utf-8")
        (self.run_dir / "observations.jsonl").unlink()

        result = summarize(self.collections, self.analyses)
        text = render(result)

        self.assertEqual(result["usable_bars"], 0)
        self.assertEqual(result["missing_observation_files"], 1)
        self.assertEqual(result["missing_observation_records"], 1)
        self.assertTrue(any("declare records" in item for item in result["blockers"]))
        self.assertIn("Missing observation files: 1 (1 with manifest-declared records)", text)

    def test_incomplete_and_manifestless_sessions_are_excluded_and_blocked(self):
        incomplete = self.collections / "collection-in-progress"
        incomplete.mkdir()
        (incomplete / "manifest.json").write_text(json.dumps({
            "status": "RUNNING",
            "record_count": 1,
            "collection_run_id": "in-progress",
            "output_file": "observations.jsonl",
        }), encoding="utf-8")
        (incomplete / "observations.jsonl").write_text(
            "partial collector output must not be read\n", encoding="utf-8"
        )

        manifestless = self.collections / "collection-without-manifest"
        manifestless.mkdir()
        (manifestless / "observations.jsonl").write_text(
            "orphaned collector output must not be read\n", encoding="utf-8"
        )

        result = summarize(self.collections, self.analyses)
        text = render(result)

        self.assertEqual(result["status_counts"], {"PROVIDER_RATE_LIMITED": 1})
        self.assertEqual(result["usable_bars"], 0)
        self.assertEqual(
            {item["directory"] for item in result["collections"]},
            {"collection-fixture"},
        )
        self.assertTrue(any("in-progress" in item.lower() for item in result["blockers"]))
        self.assertTrue(any("manifest" in item.lower() for item in result["blockers"]))
        self.assertIn("not target misses", text)

    def test_displays_cooldown_only_when_persisted(self):
        cooldown = {
            "provider": "geckoterminal",
            "http_status": 429,
            "cooldown_until_utc": "2026-11-09T15:30:00Z",
        }
        (self.collections / ".provider-cooldown.json").write_text(
            json.dumps(cooldown), encoding="utf-8"
        )

        result = summarize(self.collections, self.analyses)

        self.assertEqual(
            result["provider_cooldown"],
            "recorded: geckoterminal HTTP 429, until 2026-11-09T15:30:00Z",
        )

    def test_displays_exact_identity_coverage_and_forecast_reason_counts(self):
        report_dir = self.analyses / "candle-outcomes-v4-fixture"
        report_dir.mkdir(parents=True)
        (report_dir / "report.json").write_text(json.dumps({
            "analysis_version": "rocket-hunter-candle-outcomes.v4",
            "analysis_as_of_utc": "2026-11-09T16:00:00Z",
            "record_counts": {
                "forecasts_loaded": 12,
                "observations_loaded": 3,
                "forecasts_joined_to_observations": 2,
            },
            "candle_level_metrics": {"complete_coverage_denominator": 1},
            "excluded_forecast_reasons": {
                "no_eligible_observation_for_exact_identity": 4,
            },
            "incomplete_forecast_reasons": {"missing_elapsed_hours": 1},
        }), encoding="utf-8")

        text = render(summarize(self.collections, self.analyses))

        self.assertIn("exact_identity_joins=2; exact_identity_complete_24h_windows=1", text)
        self.assertIn('"no_eligible_observation_for_exact_identity": 4', text)
        self.assertIn('"missing_elapsed_hours": 1', text)


if __name__ == "__main__":
    unittest.main()
