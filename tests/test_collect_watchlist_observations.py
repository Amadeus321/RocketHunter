import datetime as dt
import email.utils
import hashlib
import json
import tempfile
import time
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import collect_watchlist_observations as collector


class WatchlistObservationCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.memory = self.root / "scanner-memory"
        self.memory.mkdir()
        self.watchlist = self.memory / "active-24h-watchlist.json"
        self.output_root = self.root / "research-observations"
        self.positions = [
            {
                "identity": "dex:solana:token-a",
                "market_type": "dex_pool",
                "chain": "solana",
                "contract_address": "token-a",
                "pool_address": "pool-a",
                "base_asset": "token-a",
                "quote_asset": "wrapped-sol",
                "quote_symbol": "SOL",
                "pair_orientation": "token-a/wrapped-sol",
                "created_run_id": "scanner-run-a",
            },
            {
                "identity": "cex:binance:asset-usdt",
                "market_type": "cex_spot",
                "venue": "binance",
                "venue_pair": "ASSETUSDT",
                "base_asset": "ASSET",
                "quote_asset": "USDT",
                "pair_orientation": "ASSET/USDT",
            },
            {
                "identity": "dex:missing:pool",
                "market_type": "dex_pool",
                "pool_address": "pool-without-token-or-chain",
            },
        ]
        self.write_watchlist(self.positions)

    def tearDown(self):
        self.temp.cleanup()

    def write_watchlist(self, positions):
        self.watchlist.write_text(json.dumps({"positions": positions}), encoding="utf-8")

    def args(self, *extra):
        return collector.build_arg_parser().parse_args([
            "--memory-dir", str(self.memory),
            "--watchlist", str(self.watchlist),
            "--output-root", str(self.output_root),
            "--duration-minutes", "2",
            "--interval-seconds", "60",
            "--max-requests", "10",
            *extra,
        ])

    def test_duration_over_three_hour_limit_is_rejected(self):
        with patch.object(collector, "collect") as collect:
            with self.assertRaises(SystemExit) as raised:
                collector.main([
                    "--memory-dir", str(self.memory),
                    "--watchlist", str(self.watchlist),
                    "--output-root", str(self.output_root),
                    "--duration-minutes", "181",
                ])
        self.assertEqual(raised.exception.code, 2)
        collect.assert_not_called()

    @staticmethod
    def fake_bars(*_args):
        return ([{
            "time_open": "2026-11-09T14:00:00Z",
            "time_close": "2026-11-09T15:00:00Z",
            "high": 1.6,
            "low": 0.8,
            "close": 1.2,
        }], "https://provider.example/ohlcv", None)

    @classmethod
    def fake_gecko_bars(cls, *_args):
        bars, url, error = cls.fake_bars()
        return bars, url, error, None

    @staticmethod
    def fake_rate_limited(*_args):
        return (
            [], "https://provider.example/ohlcv", "HTTPError: HTTP Error 429: Too Many Requests",
            {
                "provider": "geckoterminal",
                "http_status": 429,
                "retry_after_seconds": 120.0,
                "cooldown_until_utc": "2026-11-09T15:02:00Z",
                "retry_after_clamped": False,
            },
        )

    def test_once_uses_existing_dex_and_cex_adapters_and_persists_provenance(self):
        before = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.memory.iterdir() if path.is_file()
        }
        with (
            patch.object(collector.monitor, "_fetch_gecko_position_bars_with_metadata",
                         return_value=self.fake_gecko_bars()) as gecko,
            patch.object(collector.monitor, "_fetch_cex_position_bars",
                         return_value=self.fake_bars()) as cex,
        ):
            run_dir = collector.collect(
                self.args("--once"),
                now_fn=lambda: dt.datetime(2026, 11, 9, 15, 0, tzinfo=dt.timezone.utc),
                rate_limit_fn=lambda *_args: None,
            )
        self.assertEqual(gecko.call_count, 1)
        self.assertEqual(cex.call_count, 1)
        records = [json.loads(line) for line in (run_dir / "observations.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()]
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["schema"], "crypto-prebreakout.market-observation.v1")
        self.assertEqual(records[0]["source_status"], "OBSERVED")
        self.assertEqual(records[0]["collection_run_id"], run_dir.name[len("collection-"):])
        self.assertEqual(records[0]["source_scanner_run_id"], "scanner-run-a")
        self.assertEqual(records[0]["identity_fields"]["quote_asset"], "wrapped-sol")
        self.assertEqual(records[0]["identity_fields"]["pair_orientation"], "token-a/wrapped-sol")
        self.assertEqual(records[0]["source_evidence"]["bars"][0]["low"], 0.8)
        self.assertEqual(records[1]["identity_fields"]["venue"], "binance")
        self.assertEqual(records[2]["source_status"], "SKIPPED_INCOMPLETE_IDENTITY")
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "ONE_CYCLE_COMPLETED")
        self.assertEqual(manifest["requests_started"], 1)
        self.assertTrue(manifest["configuration_fingerprint"])
        self.assertEqual(manifest["request_pacing_seconds"], 12.0)
        self.assertIn("executable fills", " ".join(manifest["metrics_still_unsupported"]))
        self.assertEqual(before, {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.memory.iterdir() if path.is_file()
        })

    def test_dry_run_never_calls_providers_or_creates_output(self):
        with (
            patch.object(collector.monitor, "_fetch_gecko_position_bars_with_metadata",
                         side_effect=AssertionError("network must not be used")),
            patch.object(collector.monitor, "_fetch_cex_position_bars",
                         side_effect=AssertionError("network must not be used")),
        ):
            run_dir = collector.collect(self.args("--dry-run", "--once"))
        self.assertIsNone(run_dir)
        self.assertFalse(self.output_root.exists())

    def test_candidate_limit_and_duplicate_identity_are_enforced(self):
        self.write_watchlist([self.positions[0], self.positions[0], self.positions[1]])
        selected, _ = collector._load_watchlist(self.watchlist, 1)
        self.assertEqual(len(selected), 1)

    def test_request_limit_stops_before_an_extra_provider_call(self):
        with (
            patch.object(collector.monitor, "_fetch_gecko_position_bars_with_metadata",
                         return_value=self.fake_gecko_bars()) as gecko,
            patch.object(collector.monitor, "_fetch_cex_position_bars",
                         return_value=self.fake_bars()) as cex,
        ):
            run_dir = collector.collect(
                self.args("--max-requests", "1"),
                now_fn=lambda: dt.datetime(2026, 11, 9, 15, 0, tzinfo=dt.timezone.utc),
                rate_limit_fn=lambda *_args: None,
            )
        self.assertEqual(gecko.call_count + cex.call_count, 1)
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "REQUEST_LIMIT_REACHED")
        self.assertEqual(manifest["requests_started"], 1)
        records = [json.loads(line) for line in (run_dir / "observations.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()]
        self.assertIn("SKIPPED_REQUEST_LIMIT", {row["source_status"] for row in records})

    def test_gecko_rate_limit_stops_the_session_without_retrying_candidates(self):
        with (
            patch.object(collector.monitor, "_fetch_gecko_position_bars_with_metadata",
                         return_value=self.fake_rate_limited()) as gecko,
            patch.object(collector.monitor, "_fetch_cex_position_bars",
                         return_value=self.fake_bars()) as cex,
        ):
            run_dir = collector.collect(
                self.args("--once"),
                now_fn=lambda: dt.datetime(2026, 11, 9, 15, 0, tzinfo=dt.timezone.utc),
                rate_limit_fn=lambda *_args: None,
            )
        self.assertEqual(gecko.call_count, 1)
        self.assertEqual(cex.call_count, 0)
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "PROVIDER_RATE_LIMITED")
        records = [json.loads(line) for line in (run_dir / "observations.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()]
        self.assertEqual(records[0]["source_status"], "PROVIDER_RATE_LIMITED")
        self.assertEqual(records[0]["provider_rate_limit"]["provider"], "geckoterminal")
        self.assertEqual(records[0]["provider_rate_limit"]["http_status"], 429)
        self.assertEqual(records[0]["provider_rate_limit"]["retry_after_seconds"], 120.0)
        self.assertEqual(records[1]["source_status"], "SKIPPED_PROVIDER_RATE_LIMIT")
        self.assertEqual(records[1]["provider_rate_limit"]["cooldown_until_utc"],
                         "2026-11-09T15:02:00Z")
        self.assertEqual(records[2]["source_status"], "SKIPPED_INCOMPLETE_IDENTITY")
        self.assertEqual(manifest["record_status_counts"]["skipped_provider_rate_limit"], 1)
        self.assertEqual(manifest["record_status_counts"]["skipped_incomplete_identity"], 1)
        self.assertEqual(manifest["provider_rate_limit"]["http_status"], 429)
        self.assertEqual(
            manifest["provider_rate_limit"]["cooldown_until_utc"],
            "2026-11-09T15:02:00Z",
        )
        self.assertEqual(
            records[0]["provider_rate_limit"]["cooldown_until_utc"],
            "2026-11-09T15:02:00Z",
        )
        cooldown = json.loads((self.output_root / ".provider-cooldown.json").read_text(
            encoding="utf-8"
        ))
        self.assertEqual(cooldown["cooldown_source"], "provider Retry-After response header")
        self.assertEqual(cooldown["cooldown_until_utc"], "2026-11-09T15:02:00Z")
        self.assertNotIn("headers", cooldown)

    def test_persisted_retry_after_cooldown_skips_without_requesting(self):
        self.output_root.mkdir(parents=True)
        (self.output_root / ".provider-cooldown.json").write_text(json.dumps({
            "provider": "geckoterminal",
            "http_status": 429,
            "observed_utc": "2026-11-09T15:00:00Z",
            "retry_after_seconds": 120,
            "cooldown_until_utc": "2026-11-09T15:02:00Z",
            "retry_after_clamped": False,
            "cooldown_source": "provider Retry-After response header",
        }), encoding="utf-8")
        with (
            patch.object(collector.monitor, "_fetch_gecko_position_bars_with_metadata",
                         side_effect=AssertionError("cooldown must prevent requests")),
            patch.object(collector.monitor, "_fetch_cex_position_bars",
                         side_effect=AssertionError("cooldown must prevent requests")),
        ):
            run_dir = collector.collect(
                self.args("--once"),
                now_fn=lambda: dt.datetime(2026, 11, 9, 15, 1, tzinfo=dt.timezone.utc),
                rate_limit_fn=lambda *_args: None,
            )
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        records = [json.loads(line) for line in (run_dir / "observations.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()]
        self.assertEqual(manifest["status"], "PROVIDER_COOLDOWN_ACTIVE")
        self.assertEqual(manifest["requests_started"], 0)
        self.assertEqual(manifest["record_status_counts"]["skipped_provider_cooldown"], 2)
        self.assertEqual([row["source_status"] for row in records], [
            "SKIPPED_PROVIDER_COOLDOWN", "SKIPPED_PROVIDER_COOLDOWN",
            "SKIPPED_INCOMPLETE_IDENTITY",
        ])

    def test_gecko_adapter_extracts_retry_after_without_retaining_headers(self):
        error = urllib.error.HTTPError(
            "https://api.geckoterminal.com/example", 429, "Too Many Requests",
            {"Retry-After": "120", "Set-Cookie": "sensitive=value"}, None,
        )
        with patch.object(collector.monitor.urllib.request, "urlopen", side_effect=error):
            bars, url, message, metadata = collector.monitor._fetch_gecko_position_bars_with_metadata(
                self.positions[0], timeout=1
            )
        self.assertEqual(bars, [])
        self.assertIn("429", message)
        self.assertIn("geckoterminal", url)
        self.assertEqual(metadata["provider"], "geckoterminal")
        self.assertEqual(metadata["http_status"], 429)
        self.assertEqual(metadata["retry_after_seconds"], 120.0)
        self.assertNotIn("headers", metadata)
        self.assertNotIn("Set-Cookie", json.dumps(metadata))

    def test_gecko_403_without_retry_after_preserves_status_without_inventing_cooldown(self):
        error = urllib.error.HTTPError(
            "https://api.geckoterminal.com/example", 403, "Forbidden", {}, None,
        )
        with patch.object(collector.monitor.urllib.request, "urlopen", side_effect=error):
            _, _, message, metadata = collector.monitor._fetch_gecko_position_bars_with_metadata(
                self.positions[0], timeout=1
            )
        self.assertIn("403", message)
        self.assertEqual(metadata["provider"], "geckoterminal")
        self.assertEqual(metadata["http_status"], 403)
        self.assertFalse(metadata["retry_after_available"])
        self.assertEqual(metadata["cooldown_status"], "retry_after_unavailable")
        self.assertNotIn("cooldown_until_utc", metadata)
        self.assertNotIn("retry_after_header", metadata)

    def test_gecko_403_keeps_only_bounded_retry_after_header_metadata(self):
        error = urllib.error.HTTPError(
            "https://api.geckoterminal.com/example", 403, "Forbidden",
            {"Retry-After": "120", "Set-Cookie": "sensitive=value"}, None,
        )
        with patch.object(collector.monitor.urllib.request, "urlopen", side_effect=error):
            _, _, _, metadata = collector.monitor._fetch_gecko_position_bars_with_metadata(
                self.positions[0], timeout=1
            )
        self.assertEqual(metadata["http_status"], 403)
        self.assertEqual(metadata["retry_after_header"], "120")
        self.assertTrue(metadata["retry_after_available"])
        self.assertEqual(metadata["retry_after_seconds"], 120.0)
        self.assertIn("cooldown_until_utc", metadata)
        self.assertNotIn("Set-Cookie", json.dumps(metadata))

    def test_gecko_adapter_reads_retry_after_header_case_insensitively(self):
        error = urllib.error.HTTPError(
            "https://api.geckoterminal.com/example", 429, "Too Many Requests",
            {"retry-after": "120"}, None,
        )
        with patch.object(collector.monitor.urllib.request, "urlopen", side_effect=error):
            _, _, _, metadata = collector.monitor._fetch_gecko_position_bars_with_metadata(
                self.positions[0], timeout=1
            )
        self.assertTrue(metadata["retry_after_available"])
        self.assertEqual(metadata["retry_after_seconds"], 120.0)
        self.assertIn("cooldown_until_utc", metadata)

    def test_gecko_adapter_derives_retry_after_http_date(self):
        now = dt.datetime(2026, 11, 9, 15, 0, tzinfo=dt.timezone.utc)
        retry_at = now + dt.timedelta(minutes=3)
        header = email.utils.format_datetime(retry_at, usegmt=True)

        metadata = collector.monitor._retry_after_metadata(header, now=now)

        self.assertEqual(metadata["retry_after_seconds"], 180.0)
        self.assertEqual(metadata["cooldown_until_utc"], "2026-11-09T15:03:00Z")

    def test_new_rate_limit_does_not_shorten_a_later_persisted_cooldown(self):
        self.output_root.mkdir(parents=True)
        cooldown_path = self.output_root / ".provider-cooldown.json"
        cooldown_path.write_text(json.dumps({
            "provider": "geckoterminal",
            "http_status": 429,
            "observed_utc": "2026-11-09T15:00:00Z",
            "retry_after_seconds": 600,
            "cooldown_until_utc": "2026-11-09T15:10:00Z",
            "retry_after_clamped": False,
            "cooldown_status": "bounded_retry_after",
            "cooldown_source": "provider Retry-After response header",
        }), encoding="utf-8")

        collector._write_provider_cooldown(
            cooldown_path,
            {
                "provider": "geckoterminal",
                "http_status": 429,
                "retry_after_seconds": 120,
                "cooldown_until_utc": "2026-11-09T15:04:00Z",
            },
            "2026-11-09T15:02:00Z",
        )

        cooldown = json.loads(cooldown_path.read_text(encoding="utf-8"))
        self.assertEqual(cooldown["cooldown_until_utc"], "2026-11-09T15:10:00Z")
        self.assertEqual(cooldown["retry_after_seconds"], 600)

    def test_existing_gecko_adapter_keeps_its_three_value_contract(self):
        detailed = self.fake_gecko_bars()
        with patch.object(
            collector.monitor, "_fetch_gecko_position_bars_with_metadata",
            return_value=detailed,
        ):
            result = collector.monitor._fetch_gecko_position_bars(self.positions[0], timeout=1)
        self.assertEqual(result, detailed[:3])

    def test_gecko_429_without_retry_after_does_not_invent_cooldown(self):
        error = urllib.error.HTTPError(
            "https://api.geckoterminal.com/example", 429, "Too Many Requests", {}, None,
        )
        with patch.object(collector.monitor.urllib.request, "urlopen", side_effect=error):
            _, _, _, metadata = collector.monitor._fetch_gecko_position_bars_with_metadata(
                self.positions[0], timeout=1
            )
        self.assertEqual(metadata["provider"], "geckoterminal")
        self.assertEqual(metadata["http_status"], 429)
        self.assertFalse(metadata["retry_after_available"])
        self.assertEqual(metadata["cooldown_status"], "retry_after_unavailable")
        self.assertNotIn("cooldown_until_utc", metadata)
        self.assertIsNone(collector.monitor._retry_after_metadata(None))

    def test_missing_retry_after_still_stops_and_skips_without_persisted_cooldown(self):
        metadata = {
            "provider": "geckoterminal",
            "http_status": 429,
            "retry_after_available": False,
            "cooldown_status": "retry_after_unavailable",
        }
        with (
            patch.object(collector.monitor, "_fetch_gecko_position_bars_with_metadata",
                         return_value=(
                             [], "https://provider.example/ohlcv",
                             "HTTPError: HTTP Error 429: Too Many Requests", metadata,
                         )) as gecko,
            patch.object(collector.monitor, "_fetch_cex_position_bars") as cex,
        ):
            run_dir = collector.collect(
                self.args("--once"),
                now_fn=lambda: dt.datetime(2026, 11, 9, 15, 0, tzinfo=dt.timezone.utc),
                rate_limit_fn=lambda *_args: None,
            )
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        records = [json.loads(line) for line in (run_dir / "observations.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()]
        self.assertEqual(gecko.call_count, 1)
        self.assertEqual(cex.call_count, 0)
        self.assertEqual(manifest["status"], "PROVIDER_RATE_LIMITED")
        self.assertEqual(manifest["provider_rate_limit"]["cooldown_status"],
                         "retry_after_unavailable")
        self.assertFalse((self.output_root / ".provider-cooldown.json").exists())
        self.assertEqual(records[1]["source_status"], "SKIPPED_PROVIDER_RATE_LIMIT")

    def test_gecko_access_denied_stops_session_and_is_not_reported_as_rate_limit(self):
        metadata = {
            "provider": "geckoterminal",
            "http_status": 403,
            "retry_after_available": False,
            "cooldown_status": "retry_after_unavailable",
        }
        with (
            patch.object(collector.monitor, "_fetch_gecko_position_bars_with_metadata",
                         return_value=(
                             [], "https://provider.example/ohlcv",
                             "HTTPError: HTTP Error 403: Forbidden", metadata,
                         )) as gecko,
            patch.object(collector.monitor, "_fetch_cex_position_bars") as cex,
        ):
            run_dir = collector.collect(
                self.args("--once"),
                now_fn=lambda: dt.datetime(2026, 11, 9, 15, 0, tzinfo=dt.timezone.utc),
                rate_limit_fn=lambda *_args: None,
            )
        self.assertEqual(gecko.call_count, 1)
        self.assertEqual(cex.call_count, 0)
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        records = [json.loads(line) for line in (run_dir / "observations.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()]
        self.assertEqual(manifest["status"], "PROVIDER_ACCESS_DENIED")
        self.assertEqual(manifest["requests_started"], 1)
        self.assertEqual(manifest["provider_response"], metadata)
        self.assertIsNone(manifest["provider_rate_limit"])
        self.assertEqual(records[0]["source_status"], "PROVIDER_ACCESS_DENIED")
        self.assertEqual(records[0]["provider_response"], metadata)
        self.assertIsNone(records[0]["provider_rate_limit"])
        self.assertEqual(records[1]["source_status"], "SKIPPED_PROVIDER_ACCESS_DENIED")
        self.assertEqual(records[1]["provider_response"], metadata)
        self.assertEqual(records[2]["source_status"], "SKIPPED_INCOMPLETE_IDENTITY")
        self.assertFalse((self.output_root / ".provider-cooldown.json").exists())

    def test_403_with_retry_after_persists_provider_supplied_retry_time(self):
        metadata = {
            "provider": "geckoterminal",
            "http_status": 403,
            "retry_after_header": "120",
            "retry_after_available": True,
            "retry_after_seconds": 120.0,
            "cooldown_until_utc": "2026-11-09T15:02:00Z",
            "retry_after_clamped": False,
            "cooldown_status": "bounded_retry_after",
        }
        with patch.object(
            collector.monitor, "_fetch_gecko_position_bars_with_metadata",
            return_value=(
                [], "https://provider.example/ohlcv",
                "HTTPError: HTTP Error 403: Forbidden", metadata,
            ),
        ):
            run_dir = collector.collect(
                self.args("--once"),
                now_fn=lambda: dt.datetime(2026, 11, 9, 15, 0, tzinfo=dt.timezone.utc),
                rate_limit_fn=lambda *_args: None,
            )
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        records = [json.loads(line) for line in (run_dir / "observations.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()]
        cooldown = json.loads((self.output_root / ".provider-cooldown.json").read_text(
            encoding="utf-8"
        ))
        self.assertEqual(manifest["provider_response"], metadata)
        self.assertEqual(records[0]["provider_response"], metadata)
        self.assertEqual(cooldown["http_status"], 403)
        self.assertEqual(cooldown["cooldown_until_utc"], "2026-11-09T15:02:00Z")

    def test_retry_after_cooldown_is_bounded(self):
        metadata = collector.monitor._retry_after_metadata(
            str(collector.monitor.GECKO_MAX_RETRY_AFTER_SECONDS * 2),
            now=dt.datetime(2026, 11, 9, 15, 0, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(metadata["retry_after_seconds"],
                         collector.monitor.GECKO_MAX_RETRY_AFTER_SECONDS)
        self.assertTrue(metadata["retry_after_clamped"])
        self.assertEqual(metadata["cooldown_until_utc"], "2026-11-16T15:00:00Z")

    def test_ctrl_c_writes_interrupted_manifest_without_touching_source(self):
        with patch.object(collector.monitor, "_fetch_gecko_position_bars_with_metadata",
                          side_effect=KeyboardInterrupt):
            run_dir = collector.collect(
                self.args("--once"),
                now_fn=lambda: dt.datetime(2026, 11, 9, 15, 0, tzinfo=dt.timezone.utc),
                rate_limit_fn=lambda *_args: None,
            )
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "INTERRUPTED")
        self.assertEqual(manifest["requests_started"], 1)
        self.assertEqual((run_dir / "observations.jsonl").read_text(encoding="utf-8"), "")

    def test_output_must_remain_separate_from_scanner_memory(self):
        args = self.args("--dry-run")
        args.output_root = self.memory / "research"
        with self.assertRaisesRegex(collector.CollectorError, "separate"):
            collector.collect(args)

    def test_existing_venue_client_counts_and_paces_each_http_call(self):
        requests = []
        fetcher = collector._BoundedVenueFetcher(
            self.output_root / "rate.json", 1,
            lambda path, interval: requests.append((path, interval)),
            time.monotonic() + 60,
        )
        with patch.object(collector.venues.Fetcher, "_request",
                          return_value={"candles": []}) as request:
            fetcher.get("https://exchange.example/first")
            with self.assertRaises(collector._RequestLimitReached):
                fetcher.get("https://exchange.example/second")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(fetcher.requests_started, 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][1], max(
            collector.PACING_SECONDS,
            collector.monitor.GECKO_POSITION_INTERVAL_SECONDS,
        ))


    def test_calculate_simulated_execution_metrics(self):
        bars = [
            {"open": 1.0, "high": 1.2, "low": 0.95, "close": 1.15},
            {"open": 1.15, "high": 1.48, "low": 1.10, "close": 1.42},
        ]
        res = collector.calculate_simulated_execution(
            entry_price=1.0,
            target_price=1.40,
            bars_5m=bars,
            liquidity_usd=120_000.0,
            fee_bps=30.0,
        )
        self.assertEqual(res["status"], "SIMULATED")
        self.assertTrue(res["target_hit"])
        self.assertGreaterEqual(res["mfe_pct"], 48.0)
        self.assertLess(res["mae_pct"], 0.0)
        self.assertGreater(res["net_simulated_return_pct"], 40.0)
        self.assertIn("slippage_drag_pct", res)
        self.assertIn("fee_drag_pct", res)


if __name__ == "__main__":
    unittest.main()
