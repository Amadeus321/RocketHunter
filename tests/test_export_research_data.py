import gzip
import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    pa = pq = None

from scripts import export_research_data
from scripts.export_research_data import ExportError, export_ledgers


def write_partition(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True) + "\n")


def file_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


class ResearchExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.memory = self.root / "scanner-memory"
        self.export_root = self.root / "exports"
        self.run_root = self.root / "runs"
        self.run_root.mkdir()
        self.forecast = {
            "schema": "crypto-prebreakout.forecast-ledger.v1",
            "pipeline_version": "4.18.1",
            "core_runtime_version": "4.15.3",
            "forecast_id": "forecast-1",
            "run_id": "run-1",
            "snapshot_utc": "2026-11-05T00:00:00Z",
            "market_key": "dex|solana|token:MintAbC|pool:PoolX",
            "identity": "dex:solana:MintAbC",
            "discovery_lanes": ["dex_discovery", "forward_forecast_candidate"],
            "model_id": "baseline",
            "raw_score": 72.5,
            "score_is_probability": False,
            "feature_snapshot": {
                "chain": "solana",
                "market_type": "dex_pool",
                "contract_address": "MintAbC",
                "pool_address": "PoolX",
                "pair_address": "PoolX",
                "pair": "TOKEN/SOL",
                "quote_symbol": "SOL",
                "price_usd": 0.25,
                "retrieved_utc": "2026-11-05T00:00:00Z",
            },
            "future_compatible_field": {"preserved": True},
        }
        self.outcome = {
            "schema": "crypto-prebreakout.outcome-ledger.v1",
            "pipeline_version": "4.18.1",
            "forecast_id": "forecast-1",
            "run_id": "run-2",
            "market_key": self.forecast["market_key"],
            "horizon_hours": 24,
            "forecast_utc": "2026-11-05T00:00:00Z",
            "observed_utc": "2026-11-06T00:05:00Z",
            "observation_lag_hours": 0.0833,
            "status": "OBSERVED_MARK_NOT_EXECUTABLE_FILL",
            "base_price_usd": 0.25,
            "observed_price_usd": 0.3,
            "mark_return_pct": 20.0,
            "mfe_mae_status": "UNAVAILABLE_NO_INTRAHORIZON_CANDLES",
            "fees_slippage_status": "NOT_MODELED",
        }
        write_partition(self.memory / "forecasts" / "2026-11-05.jsonl.gz", [self.forecast])
        write_partition(self.memory / "outcomes" / "2026-11-06.jsonl.gz", [self.outcome])

    def tearDown(self):
        self.temp.cleanup()

    def export(self, output_format=None):
        options = {"export_root": self.export_root, "run_root": self.run_root}
        if output_format is not None:
            options["output_format"] = output_format
        return export_ledgers(self.memory, **options)

    def test_csv_compatibility_mode_does_not_load_pyarrow(self):
        before = file_hashes(self.memory)
        with patch.object(export_research_data, "_load_pyarrow",
                          side_effect=AssertionError("CSV export must not load PyArrow")):
            output = self.export("csv")
        self.assertEqual(file_hashes(self.memory), before)
        self.assertEqual(json.loads((output / "manifest.json").read_text(encoding="utf-8"))["format"], "csv")
        forecast_path = next((output / "record_type=forecast").rglob("*.csv.gz"))
        with gzip.open(forecast_path, "rt", encoding="utf-8", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["chain"], "solana")
        self.assertEqual(row["contract_address"], "MintAbC")
        self.assertEqual(json.loads(row["source_record_json"]), self.forecast)

    def test_missing_pyarrow_does_not_block_csv_export(self):
        with patch.object(export_research_data, "_load_pyarrow",
                          side_effect=ExportError("Parquet export requires PyArrow")):
            with self.assertRaisesRegex(ExportError, "requires PyArrow"):
                self.export("parquet")
            csv_output = self.export("csv")
        self.assertTrue(csv_output.is_dir())

    @unittest.skipIf(pq is None, "PyArrow is not installed")
    def test_csv_and_parquet_use_separate_write_once_snapshots(self):
        csv_output = self.export("csv")
        parquet_output = self.export("parquet")
        self.assertNotEqual(csv_output, parquet_output)
        self.assertTrue(next(csv_output.rglob("*.csv.gz")).is_file())
        self.assertTrue(next(parquet_output.rglob("*.parquet")).is_file())

    def test_csv_is_the_default_export_format(self):
        output = self.export()
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["format"], "csv")
        self.assertTrue(next(output.rglob("*.csv.gz")).is_file())
        self.assertEqual(export_research_data.build_arg_parser().parse_args([]).format, "csv")

    @unittest.skipIf(pq is None, "PyArrow is not installed")
    def test_export_preserves_records_identity_provenance_and_source_bytes(self):
        before = file_hashes(self.memory)
        output = self.export("parquet")
        self.assertEqual(file_hashes(self.memory), before)
        forecast_path = next((output / "record_type=forecast").rglob("*.parquet"))
        outcome_path = next((output / "record_type=outcome").rglob("*.parquet"))
        row = pq.read_table(forecast_path).to_pylist()[0]
        self.assertEqual(row["chain"], "solana")
        self.assertEqual(row["contract_address"], "MintAbC")
        self.assertEqual(row["pool_address"], "PoolX")
        self.assertEqual(row["pair"], "TOKEN/SOL")
        self.assertEqual(row["quote_symbol"], "SOL")
        self.assertEqual(row["source_partition"], "2026-11-05.jsonl.gz")
        self.assertEqual(json.loads(row["source_record_json"]), self.forecast)
        outcome_row = pq.read_table(outcome_path).to_pylist()[0]
        self.assertEqual(outcome_row["status"], "OBSERVED_MARK_NOT_EXECUTABLE_FILL")
        self.assertEqual(outcome_row["mark_return_pct"], 20.0)
        self.assertEqual(json.loads(outcome_row["source_record_json"]), self.outcome)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_access"], "read-only")
        self.assertEqual(manifest["datasets"]["forecasts"]["record_counts"]["exported_records"], 1)
        self.assertEqual(manifest["datasets"]["outcomes"]["record_counts"]["exported_records"], 1)

    @unittest.skipIf(pq is None, "PyArrow is not installed")
    def test_configuration_provenance_is_joined_by_run_id(self):
        run_status = self.run_root / "one" / "scan-status.json"
        run_status.parent.mkdir(parents=True)
        run_status.write_text(json.dumps({
            "run_id": "run-1",
            "scanner_version": "4.26.6",
            "configuration_fingerprint": "cfg-123",
            "local_configuration": {"scanner_workers": 4, "secret_value": "not-allowed"},
        }), encoding="utf-8")
        before = file_hashes(self.memory)
        output = self.export("parquet")
        self.assertEqual(file_hashes(self.memory), before)
        row = pq.read_table(next((output / "record_type=forecast").rglob("*.parquet"))).to_pylist()[0]
        outcome_row = pq.read_table(next((output / "record_type=outcome").rglob("*.parquet"))).to_pylist()[0]
        self.assertEqual(row["scanner_version"], "4.26.6")
        self.assertEqual(row["configuration_fingerprint"], "cfg-123")
        self.assertEqual(row["local_configuration_json"], '{"scanner_workers":4}')
        self.assertIsNone(outcome_row["configuration_fingerprint"])

    @unittest.skipIf(pq is None, "PyArrow is not installed")
    def test_missing_values_remain_null_and_unavailable_outcomes_are_not_zero(self):
        forecast = {"forecast_id": "forecast-missing", "feature_snapshot": {}}
        outcome = {
            "forecast_id": "forecast-missing",
            "horizon_hours": 4,
            "status": "UNAVAILABLE_NO_VALID_PRICE_MARK",
            "base_price_usd": None,
            "observed_price_usd": None,
            "mark_return_pct": None,
        }
        write_partition(self.memory / "forecasts" / "2026-11-07.jsonl.gz", [forecast])
        write_partition(self.memory / "outcomes" / "2026-11-07.jsonl.gz", [outcome])
        output = self.export("parquet")
        forecast_path = next((output / "record_type=forecast" / "partition_date=2026-11-07").glob("part-*.parquet"))
        outcome_path = next((output / "record_type=outcome" / "partition_date=2026-11-07").glob("part-*.parquet"))
        forecast_row = pq.read_table(forecast_path).to_pylist()[0]
        outcome_row = pq.read_table(outcome_path).to_pylist()[0]
        self.assertIsNone(forecast_row["chain"])
        self.assertIsNone(forecast_row["ingested_utc"])
        self.assertIsNone(outcome_row["base_price_usd"])
        self.assertIsNone(outcome_row["observed_price_usd"])
        self.assertIsNone(outcome_row["mark_return_pct"])
        self.assertEqual(outcome_row["status"], "UNAVAILABLE_NO_VALID_PRICE_MARK")

    @unittest.skipIf(pq is None, "PyArrow is not installed")
    def test_malformed_records_are_rejected_with_validation_reasons(self):
        path = self.memory / "forecasts" / "2026-11-05.jsonl.gz"
        write_partition(path, [self.forecast])
        with gzip.open(path, "at", encoding="utf-8") as stream:
            stream.write('{"forecast_id":\n[]\n{"feature_snapshot":{}}\n')
        outcome_path = self.memory / "outcomes" / "2026-11-06.jsonl.gz"
        write_partition(outcome_path, [self.outcome, {**self.outcome, "horizon_hours": "24"}])
        output = self.export("parquet")
        summary = json.loads((output / "validation-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["rejections_by_reason"]["malformed_json"], 1)
        self.assertEqual(summary["rejections_by_reason"]["non_object_record"], 1)
        self.assertEqual(summary["rejections_by_reason"]["missing_forecast_id"], 1)
        self.assertEqual(summary["rejections_by_reason"]["invalid_horizon_hours"], 1)
        rejections = (output / "validation-rejections.jsonl").read_text(encoding="utf-8")
        self.assertIn("2026-11-05.jsonl.gz", rejections)
        self.assertIn("invalid_horizon_hours", rejections)

    @unittest.skipIf(pq is None, "PyArrow is not installed")
    def test_repeat_export_reuses_same_snapshot_without_duplicate_rows(self):
        before = file_hashes(self.memory)
        first = self.export("parquet")
        first_export_hashes = file_hashes(first)
        second = self.export("parquet")
        self.assertEqual(first, second)
        self.assertEqual(first_export_hashes, file_hashes(second))
        self.assertEqual(file_hashes(self.memory), before)
        manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["datasets"]["forecasts"]["record_counts"]["exported_records"], 1)
        self.assertEqual(manifest["datasets"]["outcomes"]["record_counts"]["exported_records"], 1)

    @unittest.skipIf(pq is None, "PyArrow is not installed")
    def test_repeat_export_rejects_modified_snapshot_files(self):
        output = self.export("parquet")
        data_file = next(output.rglob("*.parquet"))
        data_file.write_bytes(data_file.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ExportError, "checksum validation"):
            self.export("parquet")

    def test_existing_unmanaged_export_is_never_overwritten(self):
        existing = self.export_root / "research-existing"
        existing.mkdir(parents=True)
        sentinel = existing / "keep.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        with self.assertRaisesRegex(ExportError, "refusing to overwrite"):
            export_ledgers(self.memory, existing, self.export_root, self.run_root)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_export_destination_cannot_overlap_scanner_memory(self):
        with self.assertRaisesRegex(ExportError, "must be separate"):
            export_ledgers(self.memory, export_root=self.memory / "exports", run_root=self.run_root)

    @unittest.skipIf(pq is None, "PyArrow is not installed")
    def test_corrupt_partition_is_counted_with_reason(self):
        path = self.memory / "outcomes" / "2026-11-07.jsonl.gz"
        path.write_bytes(b"not gzip")
        output = self.export("parquet")
        summary = json.loads((output / "validation-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["rejections_by_reason"]["malformed_gzip_partition"], 1)
        self.assertEqual(summary["record_counts"]["outcomes"]["rejected_records"], 1)

    @unittest.skipIf(pq is None, "PyArrow is not installed")
    def test_partition_added_during_export_aborts_instead_of_mixing_snapshots(self):
        original = export_research_data._write_dataset

        def add_partition_after_dataset(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[4] == "forecasts":
                write_partition(self.memory / "outcomes" / "2026-11-07.jsonl.gz", [self.outcome])
            return result

        with patch.object(export_research_data, "_write_dataset", side_effect=add_partition_after_dataset):
            with self.assertRaisesRegex(ExportError, "partition list changed"):
                self.export("parquet")
        self.assertEqual(list(self.export_root.glob("research-*")), [])


if __name__ == "__main__":
    unittest.main()
