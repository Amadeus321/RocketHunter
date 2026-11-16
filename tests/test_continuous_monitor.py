import datetime as dt
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import continuous_monitor as monitor
from scripts import v425_early_breakout as phase4


UTC = dt.timezone.utc


class ContinuousMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.memory = self.root / "scanner-memory"
        self.memory.mkdir()
        self.state_root = self.root / "continuous-state"
        self.watchlist = self.root / "active-watchlist.json"
        self.now = dt.datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
        self.scanner_version = (monitor.ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.candidate = self.make_candidate()
        self.write_watchlist([self.candidate])

    def tearDown(self):
        self.temp.cleanup()

    def make_candidate(self, *, index=0, entry=None, score=90, target=1.4):
        stamp = entry or self.now - dt.timedelta(minutes=10)
        return {
            "identity": f"dex:solana:pool-{index}",
            "market_key": f"dex:solana:pool-{index}",
            "forecast_id": f"forecast-{index}-{stamp.isoformat()}",
            "market_type": "dex_pool",
            "chain": "solana",
            "token_mint": f"token-{index}",
            "contract_address": f"token-{index}",
            "pool_address": f"pool-{index}",
            "entry_price": 1.0,
            "single_take_profit": target,
            "entry_timestamp_utc": monitor.utc_iso(stamp),
            "early_breakout_score": score,
            "created_run_id": "scan-run",
            "data_timestamp_utc": monitor.utc_iso(stamp),
            "retrieved_utc": monitor.utc_iso(stamp + dt.timedelta(minutes=1)),
            "source_provenance": {
                "scanner_version": self.scanner_version,
                "configuration_fingerprint": "a" * 64,
                "source_run_id": "scan-run",
                "source_event_utc": monitor.utc_iso(stamp),
                "source_received_utc": monitor.utc_iso(stamp + dt.timedelta(minutes=1)),
            },
            "phase4_score_snapshot": {
                "schema": monitor.PHASE4_SCORE_SNAPSHOT_SCHEMA,
                "early_breakout_score": score,
                "breakout_confluence_score": 81.5,
                "breakout_engine_scores": {
                    f"engine_{number}": float(number * 10) for number in range(1, 10)
                },
                "breakout_acceleration_signals": {"engine_1_momentum_acceleration": {"evidence_status": "MEASURED"}},
                "engine_evidence_status": {"engine_9_risk_safety": "MEASURED"},
                "evidence_coverage_pct": 90.0,
                "breakout_evidence_eligible": True,
                "signal_engine_executed": True,
                "expected_return_engine_executed": True,
                "engine_result_status": "SUCCESS",
                "expected_return_status": "SUCCESS",
            },
        }

    def write_watchlist(self, candidates):
        self.watchlist.write_text(json.dumps({
            "schema": monitor.PHASE4_WATCHLIST_SCHEMA,
            "provider_version": self.scanner_version,
            "scanner_version": self.scanner_version,
            "configuration_fingerprint": "b" * 64,
            "source_scan_run_id": "watchlist-run",
            "source_provenance_status": "PRESENT",
            "updated_utc": monitor.utc_iso(self.now),
            "run_id": "watchlist-run",
            "positions": candidates,
        }), encoding="utf-8")

    def args(self, *extra):
        return monitor.build_arg_parser().parse_args([
            "--memory-dir", str(self.memory),
            "--watchlist", str(self.watchlist),
            "--state-root", str(self.state_root),
            "--duration-minutes", "2",
            "--once",
            *extra,
        ])

    def open_db(self):
        return monitor.open_state(self.state_root / "monitor-state.sqlite3")

    def test_phase4_watchlist_producer_to_monitor_contract_end_to_end(self):
        output = self.root / "phase4-output"
        output.mkdir()
        configuration_fingerprint = "d" * 64
        source_run_id = "phase4-local-run"
        stamp = phase4.utc_now()
        phase4._write_json(output / "scan-status.json", {
            "status": "IN_PROGRESS",
            "run_id": source_run_id,
            "scanner_version": self.scanner_version,
            "configuration_fingerprint": configuration_fingerprint,
        })
        phase4._write_json(output / "forward-24h-rocket.json", {
            "as_of_utc": stamp,
            "candidates": [{
                "identity": "dex:solana:integration-pool",
                "market_type": "dex_pool",
                "chain": "solana",
                "pool_address": "integration-pool",
                "contract_address": "integration-token",
                "price": 1.0,
                "data_timestamp_utc": stamp,
                "retrieved_utc": stamp,
                "early_breakout_score": 73.5,
                "breakout_confluence_score": 68.25,
                "breakout_engine_scores": {"engine_1_momentum_acceleration": 72.0},
                "breakout_acceleration_signals": {"engine_1_momentum_acceleration": {"evidence_status": "PARTIAL"}},
                "engine_evidence_status": {"engine_1_momentum_acceleration": "PARTIAL"},
                "evidence_coverage_pct": 35.0,
                "breakout_evidence_eligible": False,
                "signal_engine_executed": True,
                "expected_return_engine_executed": False,
                "engine_result_status": "DATA_UNAVAILABLE",
                "expected_return_status": "DATA_UNAVAILABLE",
                "target_price": 1.4,
                "trade_plan": {"single_take_profit": 1.4},
            }],
        })
        phase4._write_json(output / "rocket-hunt.json", {})

        class FakeDexClient:
            request_count = 0

            def __init__(self, *args, **kwargs):
                pass

        with patch.object(phase4, "DexPaprikaClient", FakeDexClient), \
             patch.object(phase4, "_load_registry", return_value=([], {"registry_cached": True})), \
             patch.object(phase4, "_fetch_gecko_position_bars", return_value=([], None, None)):
            phase4.update_position_monitor(output, self.memory, source_run_id)

        args = self.args()
        args.watchlist = self.memory / "active-24h-watchlist.json"
        now = dt.datetime.now(UTC)
        run = monitor.collect(
            args, now_fn=lambda: now,
            fetch_fn=lambda *_args: {"observations": []},
        )
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["accepted_candidate_count"], 1)
        self.assertEqual(manifest["rejected_candidate_count"], 0)
        connection = self.open_db()
        try:
            candidate_row = connection.execute(
                "SELECT score, source_run_id, source_provenance_status, "
                "phase4_score_snapshot_json FROM candidates"
            ).fetchone()
            self.assertEqual(candidate_row["score"], 73.5)
            self.assertEqual(candidate_row["source_run_id"], source_run_id)
            self.assertEqual(candidate_row["source_provenance_status"], "VERIFIED")
            phase4_snapshot = json.loads(candidate_row["phase4_score_snapshot_json"])
            self.assertEqual(phase4_snapshot["breakout_confluence_score"], 68.25)
            self.assertEqual(phase4_snapshot["breakout_engine_scores"],
                             {"engine_1_momentum_acceleration": 72.0})
            self.assertFalse(phase4_snapshot["breakout_evidence_eligible"])
            self.assertEqual(phase4_snapshot["expected_return_status"], "DATA_UNAVAILABLE")
        finally:
            connection.close()

    def test_exact_identity_and_single_tp_validation_fail_closed(self):
        invalid = dict(self.candidate, token_mint="different-token")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            monitor.validate_candidate(invalid, self.now)
        invalid = dict(self.candidate, pool_address="")
        with self.assertRaisesRegex(ValueError, "exact chain"):
            monitor.validate_candidate(invalid, self.now)
        invalid = dict(self.candidate, pair_address="different-pool")
        with self.assertRaisesRegex(ValueError, "pair_address conflicts"):
            monitor.validate_candidate(invalid, self.now)
        invalid = dict(self.candidate, single_take_profit=1.399)
        with self.assertRaisesRegex(ValueError, "single_take_profit"):
            monitor.validate_candidate(invalid, self.now)
        with self.assertRaisesRegex(ValueError, "single_take_profit"):
            monitor.validate_candidate(dict(self.candidate, single_take_profit=None), self.now)

    def test_over_capacity_candidates_remain_persistently_queued(self):
        candidates = [self.make_candidate(index=i, entry=self.now - dt.timedelta(minutes=10, seconds=i))
                      for i in range(14)]
        candidates.append(candidates[0])
        connection = self.open_db()
        try:
            accepted, rejected, duplicates = monitor.ingest_candidates(
                connection, candidates, "run-a", self.now
            )
            promoted = monitor.promote_queued(connection, self.now, 12)
            self.assertEqual((accepted, rejected, duplicates, promoted), (14, 0, 1, 12))
            states = dict(connection.execute(
                "SELECT status, COUNT(*) FROM candidates GROUP BY status"
            ).fetchall())
            self.assertEqual(states, {"ACTIVE": 12, "QUEUED": 2})
        finally:
            connection.close()
        connection = self.open_db()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 14)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM candidates WHERE status='QUEUED'"
            ).fetchone()[0], 2)
        finally:
            connection.close()

    def test_provider_payload_must_match_exact_token_and_pool(self):
        payload = {"data": {"id": "solana_pool-0", "attributes": {
            "base_token_address": "token-0",
            "quote_token_address": "wrapped-sol",
            "base_token_price_usd": "1.2",
            "reserve_in_usd": "50000",
            "volume_usd": {"h24": "10000"},
            "transactions": {"h24": {"buys": "12", "sells": "8"}},
        }}}
        identity = monitor.validate_candidate(self.candidate, self.now)["identity"]
        metrics, _ = monitor._validate_pool_payload(payload, identity)
        self.assertEqual(metrics["price_usd"], 1.2)
        self.assertEqual(metrics["liquidity_usd"], 50000)
        self.assertEqual(metrics["buy_sell_pressure"]["buys"], 12)
        self.assertIn("not USD volume", metrics["buy_sell_pressure"]["semantics"])
        wrong_token = json.loads(json.dumps(payload))
        wrong_token["data"]["attributes"]["base_token_address"] = "other-token"
        with self.assertRaisesRegex(monitor.MonitorError, "token identity mismatch"):
            monitor._validate_pool_payload(wrong_token, identity)
        wrong_pool = json.loads(json.dumps(payload))
        wrong_pool["data"]["id"] = "solana_other-pool"
        with self.assertRaisesRegex(monitor.MonitorError, "pool identity mismatch"):
            monitor._validate_pool_payload(wrong_pool, identity)

    def test_duplicate_out_of_order_and_conflicting_bars_are_retained_safely(self):
        connection = self.open_db()
        try:
            monitor.ingest_candidates(connection, [self.candidate], "run-a", self.now)
            candidate_key = connection.execute(
                "SELECT candidate_key FROM candidates"
            ).fetchone()[0]
            later = self.now - dt.timedelta(minutes=5)
            earlier = self.now - dt.timedelta(minutes=10)
            def obs(opened, high, close):
                return {
                    "provider": "geckoterminal",
                    "resolution": "5m",
                    "source_event_utc": monitor.utc_iso(opened),
                    "received_utc": monitor.utc_iso(self.now),
                    "status": "OBSERVED",
                    "payload": {
                        "time_open": monitor.utc_iso(opened),
                        "time_close": monitor.utc_iso(opened + dt.timedelta(minutes=5)),
                        "high": high, "low": 0.9, "close": close, "volume": 5,
                    },
                }
            later_bar = obs(later, 1.5, 1.4)
            earlier_bar = obs(earlier, 1.2, 1.1)
            _, dup = monitor._store_observation(connection, candidate_key, "run-a", later_bar)
            self.assertFalse(dup)
            _, dup = monitor._store_observation(connection, candidate_key, "run-a", later_bar)
            self.assertTrue(dup)
            monitor._store_observation(connection, candidate_key, "run-a", earlier_bar)
            conflicting = obs(earlier, 1.6, 1.5)
            monitor._store_observation(connection, candidate_key, "run-b", conflicting)
            state = monitor.update_lifecycle(connection, candidate_key, self.now)
            self.assertEqual(state["status"], "TARGET_TOUCHED")
            self.assertFalse(state["target_touch_is_executable_fill"])
            self.assertEqual(state["conflict_count"], 1)
            usable = monitor._bar_values(connection, candidate_key)
            self.assertEqual([bar["event_utc"] for bar in usable], [monitor.utc_iso(later)])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM observations")
        finally:
            connection.close()

    def test_expiry_boundary_requires_complete_5m_coverage_for_no_touch(self):
        entry = self.now - dt.timedelta(hours=24)
        candidate = self.make_candidate(entry=entry)
        connection = self.open_db()
        try:
            monitor.ingest_candidates(
                connection, [candidate], "run-a", entry + dt.timedelta(minutes=1)
            )
            monitor.promote_queued(connection, entry + dt.timedelta(minutes=1))
            key = connection.execute("SELECT candidate_key FROM candidates").fetchone()[0]
            for index in range(288):
                opened = entry + dt.timedelta(minutes=5 * index)
                observation = {
                    "provider": "geckoterminal",
                    "resolution": "5m",
                    "source_event_utc": monitor.utc_iso(opened),
                    "received_utc": monitor.utc_iso(self.now),
                    "status": "OBSERVED",
                    "payload": {
                        "time_open": monitor.utc_iso(opened),
                        "time_close": monitor.utc_iso(opened + dt.timedelta(minutes=5)),
                        "high": 1.2, "low": 0.9, "close": 1.1, "volume": 10,
                    },
                }
                monitor._store_observation(connection, key, "run-a", observation)
            outside = entry + dt.timedelta(hours=24)
            monitor._store_observation(connection, key, "run-a", {
                "provider": "geckoterminal",
                "resolution": "5m",
                "source_event_utc": monitor.utc_iso(outside),
                "received_utc": monitor.utc_iso(self.now),
                "status": "OBSERVED",
                "payload": {
                    "time_open": monitor.utc_iso(outside),
                    "time_close": monitor.utc_iso(outside + dt.timedelta(minutes=5)),
                    "high": 1.5, "low": 0.9, "close": 1.4, "volume": 10,
                },
            })
            result = monitor.update_lifecycle(connection, key, self.now)
            self.assertEqual(result["status"], "EXPIRED")
            self.assertEqual(result["outcome"], "EXPIRED_WITH_COMPLETE_5M_COVERAGE_NO_TARGET_TOUCH")
            self.assertEqual(result["missing_5m_intervals"], 0)
        finally:
            connection.close()

    def test_missing_interval_or_touch_at_expiry_fails_closed(self):
        entry = self.now - dt.timedelta(hours=24)
        candidate = self.make_candidate(entry=entry)
        connection = self.open_db()
        try:
            monitor.ingest_candidates(
                connection, [candidate], "run-a", entry + dt.timedelta(minutes=1)
            )
            monitor.promote_queued(connection, entry + dt.timedelta(minutes=1))
            key = connection.execute("SELECT candidate_key FROM candidates").fetchone()[0]
            for index in range(288):
                if index == 100:
                    continue
                opened = entry + dt.timedelta(minutes=5 * index)
                high = 1.4 if index == 287 else 1.2
                monitor._store_observation(connection, key, "run-a", {
                    "provider": "geckoterminal",
                    "resolution": "5m",
                    "source_event_utc": monitor.utc_iso(opened),
                    "received_utc": monitor.utc_iso(self.now),
                    "status": "OBSERVED",
                    "payload": {
                        "time_open": monitor.utc_iso(opened),
                        "time_close": monitor.utc_iso(opened + dt.timedelta(minutes=5)),
                        "high": high, "low": 0.9, "close": 1.1, "volume": 10,
                    },
                })
            result = monitor.update_lifecycle(connection, key, self.now)
            self.assertEqual(result["status"], "EXPIRED")
            self.assertEqual(result["outcome"], "EXPIRED_AFTER_TECHNICAL_TARGET_TOUCH")
            self.assertFalse(result["target_touch_is_executable_fill"])
            self.assertEqual(result["missing_5m_intervals"], 1)
            self.assertEqual(result["first_target_touch_utc"],
                             monitor.utc_iso(entry + dt.timedelta(minutes=5 * 287)))
        finally:
            connection.close()

    def test_restart_recovery_manifest_and_historical_files_are_preserved(self):
        sentinel_paths = [
            self.memory / "forecasts.jsonl.gz",
            self.memory / "outcomes.jsonl.gz",
            self.memory / "trade-journal.jsonl",
        ]
        for path in sentinel_paths:
            path.write_bytes(b"historical-records-remain-untouched\n")
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sentinel_paths}
        open_at = self.now - dt.timedelta(minutes=10)
        observation = {
            "provider": "geckoterminal",
            "resolution": "5m",
            "source_event_utc": monitor.utc_iso(open_at),
            "received_utc": monitor.utc_iso(self.now),
            "status": "OBSERVED",
            "payload": {
                "time_open": monitor.utc_iso(open_at),
                "time_close": monitor.utc_iso(open_at + dt.timedelta(minutes=5)),
                "high": 1.2, "low": 0.9, "close": 1.1, "volume": 10,
            },
        }
        fetch = lambda *_args: {"observations": [observation]}
        first = monitor.collect(
            self.args(), now_fn=lambda: self.now, fetch_fn=fetch
        )
        first_manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(first_manifest["status"], "ONE_CYCLE_COMPLETED")
        self.assertEqual(first_manifest["record_count"], 2)
        self.assertEqual(first_manifest["actual_observation_resolutions"],
                         ["DEX GeckoTerminal: 1m, 5m; CEX venue adapter: 1h"])
        self.assertEqual(first_manifest["actual_achieved_cadence"][
            f"{monitor.validate_candidate(self.candidate, self.now)['candidate_key']}:poll_attempt"
        ]["poll_count"], 1)
        claimed_hash = first_manifest.pop("manifest_sha256")
        self.assertEqual(claimed_hash, monitor._sha256(monitor._canonical_json(first_manifest)))

        later = self.now + dt.timedelta(minutes=6)
        second = monitor.collect(
            self.args(), now_fn=lambda: later, fetch_fn=fetch
        )
        second_manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(second_manifest["status"], "ONE_CYCLE_COMPLETED")
        self.assertEqual(second_manifest["record_count"], 1)
        connection = monitor.open_state(self.state_root / "monitor-state.sqlite3")
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 3)
            provenance = connection.execute(
                "SELECT configuration_fingerprint, watchlist_sha256, scanner_version, "
                "source_configuration_fingerprint, source_run_id, candidate_source_event_utc, "
                "candidate_source_received_utc, source_provenance_status, "
                "source_freshness_status, phase4_score_snapshot_json FROM observations LIMIT 1"
            ).fetchone()
            self.assertTrue(provenance["configuration_fingerprint"])
            self.assertTrue(provenance["watchlist_sha256"])
            self.assertEqual(provenance["scanner_version"], self.scanner_version)
            self.assertEqual(provenance["source_configuration_fingerprint"], "a" * 64)
            self.assertEqual(provenance["source_run_id"], "scan-run")
            self.assertEqual(provenance["candidate_source_event_utc"], self.candidate["data_timestamp_utc"])
            self.assertEqual(provenance["candidate_source_received_utc"], self.candidate["retrieved_utc"])
            self.assertEqual(provenance["source_provenance_status"], "VERIFIED")
            self.assertEqual(provenance["source_freshness_status"], "FRESH")
            self.assertEqual(
                json.loads(provenance["phase4_score_snapshot_json"]),
                self.candidate["phase4_score_snapshot"],
            )
        finally:
            connection.close()
        self.assertEqual(before, {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sentinel_paths
        })

    def test_interrupted_session_stays_manifestless_and_recovers_candidate_next_invocation(self):
        with patch.object(monitor, "_logical_state_digest") as state_digest, \
             patch.object(monitor, "_write_manifest") as write_manifest:
            interrupted = monitor.collect(
                self.args(),
                now_fn=lambda: self.now,
                fetch_fn=lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()),
            )
        state_digest.assert_not_called()
        write_manifest.assert_not_called()
        self.assertFalse((interrupted / "manifest.json").exists())
        connection = self.open_db()
        try:
            self.assertEqual(connection.execute(
                "SELECT status FROM candidates"
            ).fetchone()[0], "ACTIVE")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM observations WHERE collection_run_id=?",
                (interrupted.name.removeprefix("session-"),),
            ).fetchone()[0], 0)
        finally:
            connection.close()

        recovered = monitor.collect(
            self.args(),
            now_fn=lambda: self.now + dt.timedelta(minutes=1),
            fetch_fn=lambda *_args: {"observations": []},
        )
        recovered_manifest = json.loads(
            (recovered / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(recovered_manifest["status"], "ONE_CYCLE_COMPLETED")
        self.assertEqual(recovered_manifest["accepted_candidate_count"], 0)
        self.assertEqual(recovered_manifest["candidate_status_counts"]["ACTIVE"], 1)

    def test_heartbeat_loss_after_fetch_stops_writes_and_leaves_manifestless(self):
        error = monitor.run_contract.ResearchRunError("simulated heartbeat loss")
        fetch_called = False

        def fetch(*_args):
            nonlocal fetch_called
            fetch_called = True
            return {"observations": [], "responses": []}

        def check_health():
            if fetch_called:
                raise error

        args = self.args()
        with patch.object(
            monitor.run_contract.ResearchRunLease,
            "check_health",
            side_effect=check_health,
        ), patch.object(monitor, "_write_manifest") as write_manifest:
            with self.assertRaisesRegex(
                monitor.run_contract.ResearchRunError, "heartbeat loss",
            ):
                monitor.collect(args, now_fn=lambda: self.now, fetch_fn=fetch)

        run_dir = next(self.state_root.glob("session-*"))
        self.assertTrue(fetch_called)
        self.assertFalse((run_dir / "manifest.json").exists())
        write_manifest.assert_not_called()
        connection = self.open_db()
        try:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM observations WHERE collection_run_id=?",
                (run_dir.name.removeprefix("session-"),),
            ).fetchone()[0], 0)
        finally:
            connection.close()

    def test_bounded_session_repeats_due_polls_and_reports_achieved_cadence(self):
        args = self.args()
        args.once = False
        args.duration_minutes = 1
        args.interval_seconds = 12
        args.priority_interval_seconds = 12
        clock = {"mono": 100.0, "now": self.now}
        calls = []

        def fake_sleep(seconds):
            clock["mono"] += seconds
            clock["now"] += dt.timedelta(seconds=seconds)

        def fetch(_candidate, _identity, _deadline):
            calls.append(clock["now"])
            opened = clock["now"] - dt.timedelta(minutes=5)
            return {"observations": [{
                "provider": "geckoterminal",
                "resolution": "5m",
                "source_event_utc": monitor.utc_iso(opened),
                "received_utc": monitor.utc_iso(clock["now"]),
                "status": "OBSERVED",
                "payload": {
                    "time_open": monitor.utc_iso(opened),
                    "time_close": monitor.utc_iso(clock["now"]),
                    "high": 1.2, "low": 0.9, "close": 1.1, "volume": 10,
                },
            }]}

        with patch.object(monitor.time, "monotonic", side_effect=lambda: clock["mono"]):
            run = monitor.collect(
                args,
                now_fn=lambda: clock["now"],
                sleep_fn=fake_sleep,
                fetch_fn=fetch,
            )
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        cadence = manifest["actual_achieved_cadence"]
        candidate_key = monitor.validate_candidate(self.candidate, self.now)["candidate_key"]
        poll_cadence = cadence[f"{candidate_key}:poll_attempt"]
        self.assertEqual(manifest["status"], "DURATION_ELAPSED")
        self.assertGreaterEqual(len(calls), 4)
        self.assertEqual(poll_cadence["poll_count"], len(calls))
        self.assertEqual(poll_cadence["mean_poll_interval_seconds"], 12.0)

    def test_provider_rate_limit_stops_without_another_candidate_request(self):
        self.write_watchlist([
            self.candidate,
            self.make_candidate(index=1, entry=self.now - dt.timedelta(minutes=10), score=80),
        ])
        calls = []

        def limited(candidate, *_args):
            calls.append(candidate["pool_address"])
            raise monitor.ProviderStop(
                "provider returned HTTP 429",
                {"provider": "geckoterminal", "http_status": 429},
            )

        run = monitor.collect(
            self.args(), now_fn=lambda: self.now, fetch_fn=limited
        )
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "PROVIDER_RATE_LIMITED")
        self.assertEqual(calls, ["pool-0"])
        self.assertEqual(manifest["provider_rate_limit"]["http_status"], 429)

    def test_stale_responses_and_provider_outages_are_persisted_as_gaps(self):
        stale_bar = {
            "provider": "geckoterminal",
            "resolution": "5m",
            "source_event_utc": monitor.utc_iso(self.now - dt.timedelta(minutes=35)),
            "received_utc": monitor.utc_iso(self.now),
            "status": "OBSERVED",
            "payload": {
                "time_open": monitor.utc_iso(self.now - dt.timedelta(minutes=35)),
                "time_close": monitor.utc_iso(self.now - dt.timedelta(minutes=30)),
                "high": 1.2, "low": 0.9, "close": 1.1, "volume": 10,
            },
        }
        run = monitor.collect(
            self.args(), now_fn=lambda: self.now,
            fetch_fn=lambda *_args: {"observations": [stale_bar]},
        )
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["record_status_counts"]["stale"], 1)
        connection = self.open_db()
        try:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM observations WHERE status='STALE_PROVIDER_DATA'"
            ).fetchone()[0], 1)
            metrics = json.loads(connection.execute(
                "SELECT latest_metrics_json FROM candidates"
            ).fetchone()[0])
            self.assertEqual(metrics["price_freshness_status"], "STALE_PROVIDER_DATA")
        finally:
            connection.close()

        outage_state = self.root / "outage-state"
        outage_args = self.args()
        outage_args.state_root = outage_state

        def unavailable(*_args):
            raise monitor.MonitorError("provider unavailable")

        failed_run = monitor.collect(
            outage_args, now_fn=lambda: self.now, fetch_fn=unavailable
        )
        failure_manifest = json.loads((failed_run / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(failure_manifest["record_status_counts"]["fetch_failed"], 1)
        connection = monitor.open_state(outage_state / "monitor-state.sqlite3")
        try:
            self.assertEqual(connection.execute(
                "SELECT status FROM observations"
            ).fetchone()[0], "FETCH_FAILED")
        finally:
            connection.close()

    def test_watchlist_and_state_paths_cannot_modify_scanner_memory(self):
        args = self.args()
        args.state_root = self.memory / "monitor"
        with self.assertRaisesRegex(monitor.MonitorError, "separate"):
            monitor.collect(args, fetch_fn=lambda *_args: {"observations": []})

    def test_missing_and_stale_phase4_provenance_are_rejected_without_fetch(self):
        missing = self.make_candidate(index=1)
        missing["source_provenance"]["configuration_fingerprint"] = None
        self.write_watchlist([missing])
        calls = []
        run = monitor.collect(
            self.args(), now_fn=lambda: self.now,
            fetch_fn=lambda *_args: calls.append("fetch"),
        )
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["accepted_candidate_count"], 0)
        self.assertEqual(manifest["rejected_candidate_count"], 1)
        self.assertIn(
            "Phase 4 source configuration fingerprint is missing or invalid",
            manifest["rejected_candidate_reasons"],
        )
        self.assertEqual(calls, [])
        connection = self.open_db()
        try:
            reason = connection.execute(
                "SELECT reason FROM rejected_candidates"
            ).fetchone()[0]
            self.assertIn("configuration fingerprint", reason)
        finally:
            connection.close()

        stale_entry = self.now - dt.timedelta(hours=25)
        stale = self.make_candidate(index=2, entry=stale_entry)
        stale["source_provenance"]["source_received_utc"] = monitor.utc_iso(
            stale_entry + dt.timedelta(minutes=1)
        )
        stale["retrieved_utc"] = stale["source_provenance"]["source_received_utc"]
        self.write_watchlist([stale])
        stale_args = self.args()
        stale_args.state_root = self.root / "stale-state"
        stale_run = monitor.collect(
            stale_args, now_fn=lambda: self.now,
            fetch_fn=lambda *_args: calls.append("stale-fetch"),
        )
        stale_manifest = json.loads((stale_run / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(stale_manifest["accepted_candidate_count"], 0)
        self.assertEqual(stale_manifest["rejected_candidate_count"], 1)
        self.assertIn(
            "Phase 4 source provenance is stale",
            stale_manifest["rejected_candidate_reasons"],
        )
        self.assertEqual(calls, [])
        stale_connection = monitor.open_state(stale_args.state_root / "monitor-state.sqlite3")
        try:
            reason = stale_connection.execute(
                "SELECT reason FROM rejected_candidates"
            ).fetchone()[0]
            self.assertIn("stale", reason.lower())
        finally:
            stale_connection.close()

    def test_unversioned_phase4_watchlist_root_fails_closed(self):
        document = json.loads(self.watchlist.read_text(encoding="utf-8"))
        document.pop("configuration_fingerprint")
        self.watchlist.write_text(json.dumps(document), encoding="utf-8")
        calls = []
        with self.assertRaisesRegex(monitor.MonitorError, "configuration fingerprint"):
            monitor.collect(
                self.args(), now_fn=lambda: self.now,
                fetch_fn=lambda *_args: calls.append("fetch"),
            )
        self.assertEqual(calls, [])
        self.assertFalse(self.state_root.exists())

    def test_phase4_failure_status_is_preserved_without_recalculation(self):
        candidate = self.make_candidate(index=3)
        candidate["phase4_score_snapshot"].update({
            "breakout_engine_scores": {},
            "engine_evidence_status": {"engine_9_risk_safety": "MISSING"},
            "breakout_evidence_eligible": False,
            "signal_engine_executed": False,
            "expected_return_engine_executed": False,
            "engine_result_status": "SOFTWARE_FAILURE",
            "expected_return_status": "SOFTWARE_FAILURE",
        })
        self.write_watchlist([candidate])
        run = monitor.collect(
            self.args(), now_fn=lambda: self.now,
            fetch_fn=lambda *_args: {"observations": []},
        )
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["accepted_candidate_count"], 1)
        connection = self.open_db()
        try:
            saved = connection.execute(
                "SELECT score, phase4_score_snapshot_json FROM candidates"
            ).fetchone()
            self.assertEqual(saved["score"], 90.0)
            snapshot = json.loads(saved["phase4_score_snapshot_json"])
            self.assertEqual(snapshot["engine_result_status"], "SOFTWARE_FAILURE")
            self.assertEqual(snapshot["expected_return_status"], "SOFTWARE_FAILURE")
            self.assertFalse(snapshot["breakout_evidence_eligible"])
            self.assertEqual(snapshot["breakout_engine_scores"], {})
        finally:
            connection.close()

    def test_configured_trade_journal_directory_is_protected(self):
        user_data = self.root / "user-data"
        args = self.args()
        args.state_root = user_data / "continuous-monitoring"
        with patch.dict(os.environ, {"ROCKET_HUNTER_USER_DATA_DIR": str(user_data)}):
            with self.assertRaisesRegex(monitor.MonitorError, "separate"):
                monitor.collect(args, fetch_fn=lambda *_args: {"observations": []})
        self.assertFalse(user_data.exists())

    def test_minute_endpoints_and_required_provider_header(self):
        identity = monitor.validate_candidate(self.candidate, self.now)["identity"]
        url_1m = monitor._ohlcv_url(identity, aggregate=1, limit=60)
        url_5m = monitor._ohlcv_url(identity, aggregate=5, limit=300)
        self.assertIn("/ohlcv/minute?", url_1m)
        self.assertIn("aggregate=1", url_1m)
        self.assertIn("aggregate=5", url_5m)
        with patch.object(monitor.urllib.request, "urlopen") as urlopen:
            response = urlopen.return_value.__enter__.return_value
            response.read.return_value = b'{"data":{"attributes":{}}}'
            monitor._request_json(url_1m)
            request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Accept"), "application/json;version=20230203")


if __name__ == "__main__":
    unittest.main()
