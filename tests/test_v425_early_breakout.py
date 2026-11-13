"""Regression tests for v4.25.0 free early-breakout and 24h peak tracking."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import v425_early_breakout as v425
from scripts.v418_pipeline import process_snapshot, read_jsonl_gz


class EarlyBreakoutTests(unittest.TestCase):
    def test_post_scan_persists_prospective_score_and_source_provenance(self):
        snapshot_utc = "2026-11-09T12:00:00Z"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            memory = root / "memory"
            out.mkdir()
            main = {
                "created_utc": snapshot_utc,
                "dex_discovery_leads": [{
                    "identity": "dex:base:0xtoken:0xpool",
                    "market_type": "dex_pool",
                    "chain": "base",
                    "contract_address": "0xtoken",
                    "pool_address": "0xpool",
                    "early_breakout_signal_version": v425.EARLY_BREAKOUT_SIGNAL_VERSION,
                    "early_breakout_model_id": v425.EARLY_BREAKOUT_MODEL_ID,
                    "early_breakout_score": 61.25,
                    "early_breakout_components": {"1h_momentum": 5.0},
                    "source": "dexpaprika_pool_momentum_1h",
                    "source_url": "https://provider.invalid/pools/0xpool",
                    "retrieved_utc": "2026-11-09T11:59:30Z",
                    "dexpaprika_provider_timestamp": "2026-11-09T11:59:00Z",
                }],
                "source_status": [],
            }
            (out / "rocket-hunt.json").write_text(json.dumps(main), encoding="utf-8")
            (out / "scan-status.json").write_text(json.dumps({
                "run_id": "prospective-run",
                "scanner_version": "4.28.0",
                "configuration_fingerprint": "cfg-prospective",
            }), encoding="utf-8")

            process_snapshot(
                out / "rocket-hunt.json", None, out, memory, "prospective-run"
            )
            forecast_path = next((memory / "forecasts").glob("*.jsonl.gz"))
            forecasts = read_jsonl_gz(forecast_path)

        self.assertEqual(len(forecasts), 1)
        forecast = forecasts[0]
        signal = forecast["feature_snapshot"]["v425_early_breakout"]
        self.assertEqual(forecast["snapshot_utc"], snapshot_utc)
        self.assertEqual(forecast["run_id"], "prospective-run")
        self.assertEqual(forecast["scanner_version"], "4.28.0")
        self.assertEqual(forecast["configuration_fingerprint"], "cfg-prospective")
        self.assertEqual(signal["score"], 61.25)
        self.assertEqual(signal["source"], "dexpaprika_pool_momentum_1h")
        self.assertEqual(signal["source_url"], "https://provider.invalid/pools/0xpool")
        self.assertEqual(signal["retrieved_utc"], "2026-11-09T11:59:30Z")
        self.assertEqual(signal["provider_timestamp"], "2026-11-09T11:59:00Z")

    def test_accelerating_short_window_signal_outranks_flat_short_term_signal(self):
        accelerating = v425.score_candidate({
            "change_5m_pct": 8, "change_1h_pct": 32, "change_6h_pct": 35,
            "change_24h_pct": 40, "liquidity_usd": 250000,
            "volume_24h_usd": 1800000, "txns_24h": 1500, "age_hours": 4,
            "dexpaprika_1h_buy_usd": 70000, "dexpaprika_1h_sell_usd": 30000,
            "price_usd": 0.001,
        })
        flat = v425.score_candidate({
            "change_5m_pct": 0, "change_1h_pct": 0, "change_6h_pct": 35,
            "change_24h_pct": 40, "liquidity_usd": 250000,
            "volume_24h_usd": 1800000, "txns_24h": 1500, "age_hours": 4,
            "price_usd": 0.001,
        })
        self.assertGreater(accelerating["score"], flat["score"])
        self.assertIn("momentum_acceleration", accelerating["components"])
        self.assertGreater(accelerating["buy_pressure_pct"], 50)
        self.assertEqual(accelerating["signal_version"], v425.EARLY_BREAKOUT_SIGNAL_VERSION)
        self.assertEqual(accelerating["model_id"], v425.EARLY_BREAKOUT_MODEL_ID)

    def test_pool_normalization_tracks_non_stable_token_and_exact_pool(self):
        row = {
            "chain": "base", "id": "0xpool", "price_usd": 1.0,
            "price_change_percentage_5m": 5, "price_change_percentage_1h": 25,
            "price_change_percentage_6h": 40, "price_change_percentage_24h": 60,
            "volume_usd_24h": 800000, "liquidity_usd": 150000,
            "transactions_24h": 1000, "created_at": "2026-11-06T14:00:00Z",
            "tokens": [
                {"id": "0xusdc", "name": "USD Coin", "symbol": "USDC"},
                {"id": "0xalpha", "name": "Alpha", "symbol": "ALPHA",
                 "summary": {"price_usd": 0.002, "fdv": 1000000},
                 "1h": {"buy_usd": 80000, "sell_usd": 20000, "volume_usd": 100000, "txns": 250},
                 "5m": {"buy_usd": 12000, "sell_usd": 3000, "volume_usd": 15000, "txns": 40}},
            ],
        }
        record = v425._normalize_pool(
            row, "pool_momentum_1h", 1,
            lambda **kw: {**kw, **(kw.get("extra") or {}), "contract_address": kw.get("address")},
        )
        self.assertEqual(record["contract_address"], "0xalpha")
        self.assertEqual(record["quote_token_address"], "0xusdc")
        self.assertEqual(record["pair_address"], "0xpool")
        self.assertEqual(record["price_usd"], 0.002)
        self.assertEqual(record["dexpaprika_1h_buy_usd"], 80000)
        self.assertGreater(record["early_breakout_score"], 0)
        self.assertEqual(record["early_breakout_signal_version"], v425.EARLY_BREAKOUT_SIGNAL_VERSION)
        self.assertEqual(record["early_breakout_model_id"], v425.EARLY_BREAKOUT_MODEL_ID)


    def test_solana_native_sol_is_quote_not_the_tracked_asset(self):
        row = {
            "chain": "solana", "id": "sol-pool", "price_usd": 155.0,
            "price_change_percentage_5m": 4.0, "price_change_percentage_1h": 18.0,
            "price_change_percentage_6h": 35.0, "price_change_percentage_24h": 50.0,
            "volume_usd_24h": 500000, "liquidity_usd": 120000,
            "transactions_24h": 900, "created_at": "2026-11-07T10:00:00Z",
            "tokens": [
                {"id": "So11111111111111111111111111111111111111112", "name": "Wrapped SOL",
                 "symbol": "SOL", "summary": {"price_usd": 155.0}},
                {"id": "token-mint", "name": "Alpha", "symbol": "ALPHA",
                 "summary": {"price_usd": 0.00042, "fdv": 900000}},
            ],
        }
        record = v425._normalize_pool(
            row, "pool_momentum_1h", 1,
            lambda **kw: {**kw, **(kw.get("extra") or {}), "contract_address": kw.get("address")},
        )

        self.assertEqual(record["contract_address"], "token-mint")
        self.assertEqual(record["quote_token_address"], "So11111111111111111111111111111111111111112")
        self.assertEqual(record["quote_symbol"], "SOL")
        self.assertEqual(record["pair"], "ALPHA/SOL")
        self.assertTrue(record["dexpaprika_token_order_swapped_for_tracking"])
        self.assertEqual(record["price_usd"], 0.00042)

    def test_intraday_volume_burst_is_a_scored_breakout_component(self):
        steady = v425.score_candidate({
            "change_5m_pct": 4, "change_1h_pct": 12, "change_6h_pct": 30,
            "change_24h_pct": 45, "liquidity_usd": 200000,
            "volume_24h_usd": 900000, "txns_24h": 800, "age_hours": 6,
            "dexpaprika_1h_volume_usd": 12000,
            "dexpaprika_5m_volume_usd": 1000, "price_usd": 0.001,
        })
        accelerating = v425.score_candidate({
            "change_5m_pct": 4, "change_1h_pct": 12, "change_6h_pct": 30,
            "change_24h_pct": 45, "liquidity_usd": 200000,
            "volume_24h_usd": 900000, "txns_24h": 800, "age_hours": 6,
            "dexpaprika_1h_volume_usd": 12000,
            "dexpaprika_5m_volume_usd": 7000, "price_usd": 0.001,
        })
        self.assertIn("intraday_volume_acceleration", accelerating["components"])
        self.assertGreater(accelerating["components"]["intraday_volume_acceleration"],
                           steady["components"]["intraday_volume_acceleration"])

    def test_upside_projection_contribution_is_separate_from_technical_tp(self):
        identity = "dex:base:projection-pool"
        forward = {
            "candidates": [{
                "identity": identity, "asset": "Projection", "pair": "PROJ/USDC",
                "market_type": "dex_pool", "price": 0.01,
                "source_forecast_score_pre_age_guard": 20,
                "early_breakout_score": 80,
                "projected_24h_upside_pct": 250.0,
                "scenario_reward_risk_ratio": 0.8,
                "scenario_reward_risk_penalty": 4.0,
                "stop_loss_pct": 25.0,
                "target_price": 0.0102,
                "target_price_source": "7D_SWING_RESISTANCE",
            }],
        }
        result = v425.apply_forward_opportunity_ranking(forward, {})
        leader = result["forward_research_leader"]
        self.assertAlmostEqual(leader["forward_opportunity_score_breakdown"]["early_breakout_contribution"], 44.0)
        self.assertAlmostEqual(leader["forward_opportunity_score_breakdown"]["research_context_contribution"], 4.0)
        self.assertAlmostEqual(leader["forward_opportunity_score_breakdown"]["upside_projection_score"], 100.0)
        self.assertAlmostEqual(leader["forward_opportunity_score_breakdown"]["upside_projection_contribution"], 25.0)
        self.assertEqual(leader["target_price"], 0.0102)
        self.assertEqual(leader["projected_24h_upside_pct"], 250.0)
        self.assertNotIn("scenario_reward_risk_ratio", leader)
        self.assertNotIn("scenario_reward_risk_penalty", leader)
        self.assertEqual(leader["late_chase_penalty"], 0.0)
        self.assertAlmostEqual(leader["forward_opportunity_score"], 73.0)
        self.assertIn("25% volatility/momentum upside projection", leader["forward_opportunity_ranking_model"])
        self.assertIsNone(result["top_rocket_pick"])
        self.assertEqual(result["top_opportunities"], [])

    def test_rolling_request_ceiling_blocks_before_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Path(tmp)
            ledger = {
                "requests": [
                    {"reserved_utc": v425.utc_now(), "family": "test", "request_reservation": 1}
                    for _ in range(v425.SCANNER_ROLLING_REQUEST_CEILING)
                ]
            }
            v425._write_json(memory / "dexpaprika-request-ledger.json", ledger)
            ok, used = v425._reserve_request(memory, "next")
            self.assertFalse(ok)
            self.assertEqual(used, v425.SCANNER_ROLLING_REQUEST_CEILING)

    def test_provider_credit_header_reserve_stops_new_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Path(tmp)
            v425._store_quota_headers(memory, {
                "x-credits-remaining": "2500",
                "x-api-plan": "keyless",
            })
            ok, used = v425._reserve_request(memory, "next")
            self.assertFalse(ok)
            self.assertEqual(used, 0)

    def test_pool_ohlcv_receives_orientation_flag(self):
        class FakeClient:
            def __init__(self):
                self.params = None
            def get(self, path, params, family):
                self.params = params
                return [], {}, None
        client = FakeClient()
        v425._get_ohlcv(client, "base", "0xpool", position_inversed=True)
        self.assertEqual(client.params["inversed"], "true")

    def test_target_hit_is_preserved_after_later_pullback(self):
        position = {
            "entry_price": 1.0,
            "entry_timestamp_utc": "2026-11-05T00:00:00Z",
            "time_stop_utc": "2026-11-06T00:00:00Z",
            "single_take_profit": 1.2,
            "max_price_since_entry": 1.0,
            "min_price_since_entry": 1.0,
            "latest_price": 1.0,
        }
        bars = [
            {"time_open": "2026-11-05T01:00:00Z", "time_close": "2026-11-05T02:00:00Z",
             "high": 1.25, "low": 0.98, "close": 1.10},
            {"time_open": "2026-11-05T02:00:00Z", "time_close": "2026-11-05T03:00:00Z",
             "high": 1.15, "low": 1.00, "close": 1.05},
        ]
        updated = v425._update_position(position, bars, datetime(2026, 11, 5, 3, tzinfo=timezone.utc))
        self.assertTrue(updated["take_profit_hit"])
        self.assertEqual(updated["position_status"], "TP TOUCH OBSERVED — PULLBACK")
        self.assertEqual(updated["first_tp_touch_candle_utc"], "2026-11-05T01:00:00Z")
        self.assertAlmostEqual(updated["max_favorable_excursion_pct"], 25.0)
        self.assertAlmostEqual(updated["latest_return_from_entry_pct"], 5.0)

    def test_missing_final_window_candles_do_not_become_false_no_tp_result(self):
        position = {
            "entry_price": 1.0,
            "entry_timestamp_utc": "2026-11-05T00:00:00Z",
            "time_stop_utc": "2026-11-06T00:00:00Z",
            "single_take_profit": 1.2,
            "max_price_since_entry": 1.0,
            "min_price_since_entry": 1.0,
            "latest_price": 1.0,
        }
        bars = [{
            "time_open": "2026-11-05T01:00:00Z", "time_close": "2026-11-05T02:00:00Z",
            "high": 1.1, "low": 0.95, "close": 1.02,
        }]
        updated = v425._update_position(position, bars, datetime(2026, 11, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(updated["result_status"], "WINDOW_DATA_INCOMPLETE")
        self.assertEqual(updated["position_status"], "24H WINDOW CLOSED — INCOMPLETE CANDLE COVERAGE")
        self.assertFalse(updated["candle_coverage_complete"])

    def test_complete_hourly_window_can_confirm_no_tp_hit(self):
        position = {
            "entry_price": 1.0,
            "entry_timestamp_utc": "2026-11-05T00:00:00Z",
            "time_stop_utc": "2026-11-06T00:00:00Z",
            "single_take_profit": 1.2,
            "max_price_since_entry": 1.0,
            "min_price_since_entry": 1.0,
            "latest_price": 1.0,
        }
        bars = []
        for hour in range(24):
            opened = datetime(2026, 11, 5, hour, tzinfo=timezone.utc)
            closed = opened + timedelta(hours=1)
            bars.append({
                "time_open": opened.isoformat().replace("+00:00", "Z"),
                "time_close": closed.isoformat().replace("+00:00", "Z"),
                "high": 1.1, "low": 0.95, "close": 1.02,
            })
        updated = v425._update_position(position, bars, datetime(2026, 11, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(updated["result_status"], "NO_TP_HIT_IN_24H")
        self.assertEqual(updated["position_status"], "24H WINDOW CLOSED — TP NOT HIT")
        self.assertTrue(updated["candle_coverage_complete"])
        self.assertEqual(updated["observed_candle_open_count"], 24)


    def test_low_below_legacy_stop_does_not_close_reference(self):
        position = {
            "market_type": "cex_spot", "entry_price": 100.0,
            "entry_timestamp_utc": "2026-11-05T00:00:00Z",
            "time_stop_utc": "2026-11-06T00:00:00Z",
            "single_take_profit": 120.0, "initial_stop_price": 90.0,
            "atr_1h_usd": 5.0,
            "max_price_since_entry": 100.0, "min_price_since_entry": 100.0,
            "latest_price": 100.0,
        }
        bars = [{
            "time_open": "2026-11-05T01:00:00Z", "time_close": "2026-11-05T02:00:00Z",
            "high": 104.0, "low": 89.0, "close": 92.0,
        }]

        updated = v425._update_position(position, bars, datetime(2026, 11, 5, 3, tzinfo=timezone.utc))

        self.assertEqual(updated["window_status"], "OPEN")
        self.assertEqual(updated["result_status"], "OPEN")
        self.assertAlmostEqual(updated["latest_return_from_entry_pct"], -8.0)
        self.assertAlmostEqual(updated["max_adverse_excursion_pct"], -11.0)
        self.assertNotIn("initial_stop_price", updated)
        self.assertNotIn("protective_exit_type", updated)

    def test_single_tp_touch_does_not_arm_or_trigger_a_trailing_stop(self):
        position = {
            "market_type": "cex_spot", "entry_price": 100.0,
            "entry_timestamp_utc": "2026-11-05T00:00:00Z",
            "time_stop_utc": "2026-11-06T00:00:00Z",
            "single_take_profit": 105.0, "initial_stop_price": 90.0,
            "atr_1h_usd": 2.0,
            "max_price_since_entry": 100.0, "min_price_since_entry": 100.0,
            "latest_price": 100.0,
        }
        bars = [
            {"time_open": "2026-11-05T01:00:00Z", "time_close": "2026-11-05T02:00:00Z",
             "high": 110.0, "low": 99.0, "close": 109.0},
            {"time_open": "2026-11-05T02:00:00Z", "time_close": "2026-11-05T03:00:00Z",
             "high": 112.0, "low": 106.0, "close": 108.0},
        ]

        updated = v425._update_position(position, bars, datetime(2026, 11, 5, 4, tzinfo=timezone.utc))

        self.assertEqual(updated["result_status"], "TAKE_PROFIT_REACHED_WITHIN_24H")
        self.assertEqual(updated["window_status"], "OPEN")
        self.assertEqual(updated["first_tp_touch_candle_utc"], "2026-11-05T01:00:00Z")
        self.assertAlmostEqual(updated["latest_return_from_entry_pct"], 8.0)
        self.assertNotIn("trailing_stop_price", updated)
        self.assertNotIn("protective_exit_type", updated)

    def test_target_touch_and_large_drawdown_in_same_hour_only_record_the_tp_touch(self):
        position = {
            "entry_price": 100.0,
            "entry_timestamp_utc": "2026-11-05T00:00:00Z",
            "time_stop_utc": "2026-11-06T00:00:00Z",
            "single_take_profit": 110.0, "initial_stop_price": 90.0,
            "atr_1h_usd": 4.0,
            "max_price_since_entry": 100.0, "min_price_since_entry": 100.0,
            "latest_price": 100.0,
        }
        bars = [{
            "time_open": "2026-11-05T01:00:00Z", "time_close": "2026-11-05T02:00:00Z",
            "high": 112.0, "low": 88.0, "close": 95.0,
        }]

        updated = v425._update_position(position, bars, datetime(2026, 11, 5, 3, tzinfo=timezone.utc))

        self.assertEqual(updated["result_status"], "TAKE_PROFIT_REACHED_WITHIN_24H")
        self.assertTrue(updated["take_profit_hit"])
        self.assertEqual(updated["window_status"], "OPEN")
        self.assertAlmostEqual(updated["max_adverse_excursion_pct"], -12.0)

    def test_cex_ohlcv_rows_normalize_to_closed_utc_hourly_bars(self):
        now = datetime(2026, 11, 5, 4, tzinfo=timezone.utc)
        rows = [
            {"t": int(datetime(2026, 11, 5, 1, tzinfo=timezone.utc).timestamp() * 1000),
             "o": 100, "h": 105, "l": 99, "c": 104, "qv": 10000},
            {"t": int(datetime(2026, 11, 5, 4, tzinfo=timezone.utc).timestamp() * 1000),
             "o": 104, "h": 110, "l": 103, "c": 109, "qv": 12000},
        ]
        result = v425._hourly_bars_to_position_bars(rows, now)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["high"], 105.0)
        self.assertEqual(result[0]["low"], 99.0)
        self.assertTrue(result[0]["time_open"].endswith("Z"))



    def test_gecko_position_monitor_uses_exact_token_usd_candles_for_nonstable_quote(self):
        opened = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
        payload = {
            "data": {
                "attributes": {
                    "ohlcv_list": [[int(opened.timestamp()), 1.0, 2.0, 0.9, 1.5, 1000.0]],
                },
            },
        }

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, traceback):
                return False
            def read(self):
                return json.dumps(payload).encode("utf-8")

        position = {
            "chain": "solana",
            "pool_address": "pool-address",
            "contract_address": "token-mint",
            "quote_symbol": "SOL",
        }
        with patch("urllib.request.urlopen", return_value=FakeResponse()) as opener:
            bars, url, error = v425._fetch_gecko_position_bars(position)

        self.assertIsNone(error)
        self.assertEqual(len(bars), 1)
        self.assertIn("token=token-mint", url)
        self.assertIn("currency=usd", url)
        request = opener.call_args[0][0]
        self.assertIn("token=token-mint", request.full_url)
        self.assertEqual(request.get_header("Accept"), "application/json;version=20230203")

    def test_position_monitor_accepts_non_stable_quoted_dex_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outdir = root / "out"
            memory = root / "memory"
            outdir.mkdir()
            memory.mkdir()
            stamp = v425.utc_now()
            v425._write_json(outdir / "forward-24h-rocket.json", {
                "as_of_utc": stamp,
                "candidates": [{
                    "identity": "dex:solana:pool-sol-quoted",
                    "asset": "SolQuoted",
                    "symbol": "SQ",
                    "pair": "SQ/SOL",
                    "market_type": "dex_pool",
                    "chain": "solana",
                    "pool_address": "pool-sol-quoted",
                    "contract_address": "sq-mint",
                    "quote_symbol": "SOL",
                    "price": 0.00042,
                    "data_timestamp_utc": stamp,
                    "early_breakout_score": 95.0,
                    "source_forecast_score_pre_age_guard": 30.0,
                    "target_price": 0.0006,
                    "target_price_source": "7D_SWING_RESISTANCE",
                    "initial_stop_price": 0.00038,
                    "stop_loss_pct": 9.5,
                    "atr_1h_usd": 0.00002,
                }],
            })
            v425._write_json(outdir / "rocket-hunt.json", {})

            class FakeDexClient:
                request_count = 0
                def __init__(self, *args, **kwargs):
                    pass

            with patch.object(v425, "DexPaprikaClient", FakeDexClient), \
                 patch.object(v425, "_load_registry", return_value=([], {"registry_cached": True})), \
                 patch.object(v425, "_fetch_gecko_position_bars", return_value=([], "https://example.invalid/candles", None)):
                result = v425.update_position_monitor(outdir, memory, "test-run")

            state = v425._load_json(memory / "active-24h-watchlist.json", {})
            self.assertEqual(result["active_positions"], 1)
            self.assertEqual(state["positions"][0]["market_type"], "dex_pool")
            self.assertEqual(state["positions"][0]["quote_symbol"], "SOL")
            self.assertNotIn("initial_stop_price", state["positions"][0])
            self.assertNotIn("stop_loss_pct", state["positions"][0])

    def test_position_monitor_skips_targets_below_single_tp40_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outdir = root / "out"
            memory = root / "memory"
            outdir.mkdir()
            memory.mkdir()
            stamp = v425.utc_now()
            v425._write_json(outdir / "forward-24h-rocket.json", {
                "as_of_utc": stamp,
                "candidates": [{
                    "identity": "dex:solana:pool-under-tp40",
                    "asset": "UnderTP40",
                    "market_type": "dex_pool",
                    "chain": "solana",
                    "pool_address": "pool-under-tp40",
                    "contract_address": "under-tp40-mint",
                    "price": 0.42,
                    "data_timestamp_utc": stamp,
                    "target_price": 0.50,
                }],
            })
            v425._write_json(outdir / "rocket-hunt.json", {})

            class FakeDexClient:
                request_count = 0
                def __init__(self, *args, **kwargs):
                    pass

            with patch.object(v425, "DexPaprikaClient", FakeDexClient), \
                 patch.object(v425, "_load_registry", return_value=([], {"registry_cached": True})):
                result = v425.update_position_monitor(outdir, memory, "test-run")

            state = v425._load_json(memory / "active-24h-watchlist.json", {})
            self.assertEqual(result["active_positions"], 0)
            self.assertEqual(state["positions"], [])

    def test_position_monitor_accepts_cex_spot_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outdir = root / "out"
            memory = root / "memory"
            outdir.mkdir()
            memory.mkdir()
            stamp = v425.utc_now()
            v425._write_json(outdir / "forward-24h-rocket.json", {
                "as_of_utc": stamp,
                "candidates": [{
                    "identity": "cex:ALT",
                    "asset": "Alt",
                    "symbol": "ALT",
                    "pair": "ALTUSDT",
                    "market_type": "cex_spot",
                    "venue": "binance",
                    "price": 1.25,
                    "data_timestamp_utc": stamp,
                    "early_breakout_score": 80.0,
                    "source_forecast_score_pre_age_guard": 25.0,
                    "target_price": 1.75,
                    "target_price_source": "7D_SWING_RESISTANCE",
                    "initial_stop_price": 1.1,
                    "stop_loss_pct": 12.0,
                    "atr_1h_usd": 0.08,
                }],
            })
            v425._write_json(outdir / "rocket-hunt.json", {})

            class FakeDexClient:
                request_count = 0
                def __init__(self, *args, **kwargs):
                    pass

            with patch.object(v425, "DexPaprikaClient", FakeDexClient), \
                 patch.object(v425, "_load_registry", return_value=([], {"registry_cached": True})), \
                 patch.object(v425, "_fetch_cex_position_bars", return_value=([], "https://example.invalid/klines", None)):
                result = v425.update_position_monitor(outdir, memory, "test-run")

            state = v425._load_json(memory / "active-24h-watchlist.json", {})
            self.assertEqual(result["active_positions"], 1)
            self.assertEqual(state["positions"][0]["market_type"], "cex_spot")
            self.assertEqual(state["positions"][0]["venue"], "binance")
            self.assertEqual(state["positions"][0]["venue_pair"], "ALTUSDT")

    def test_cex_position_fetch_uses_exchange_normalized_symbol(self):
        class FakeAdapter:
            def __init__(self):
                self.requested_symbol = None
            def candles(self, fx, symbol, interval, limit=None):
                self.requested_symbol = symbol
                return [], "https://example.invalid/okx-klines"

        adapter = FakeAdapter()
        adapters = SimpleNamespace(VENUES={"okx": adapter})
        position = {"venue": "okx", "venue_pair": "ALT-USDT"}
        bars, url, error = v425._fetch_cex_position_bars(
            position, adapters, object(), datetime(2026, 11, 5, 4, tzinfo=timezone.utc)
        )

        self.assertIsNone(error)
        self.assertEqual(adapter.requested_symbol, "ALT-USDT")
        self.assertEqual(url, "https://example.invalid/okx-klines")
        self.assertEqual(bars, [])

    def test_compactor_preserves_v425_fields_even_when_allowlist_is_stale(self):
        fake = SimpleNamespace(
            UNIVERSE_COMPACT_FIELDS=("identity",),
            compact_universe_row=lambda row: {k: row[k] for k in ("identity",) if row.get(k) is not None},
        )
        v425.patch_v419_compactor(fake)
        row = {
            "identity": "dex:base:pool",
            "early_breakout_score": 88.5,
            "early_breakout_tier": "EARLY_BREAKOUT_LEADER",
            "dexpaprika_family": "pool_momentum_1h",
            "quote_symbol": "USDC",
        }
        compact = fake.compact_universe_row(row)
        self.assertEqual(compact["early_breakout_score"], 88.5)
        self.assertEqual(compact["dexpaprika_family"], "pool_momentum_1h")
        self.assertEqual(compact["quote_symbol"], "USDC")

    def test_young_pool_still_ranks_for_new_forward_24h_window(self):
        identity = "dex:solana:youngpool"
        forward = {
            "status": "NO_AGE_QUALIFIED_FORWARD_CANDIDATE",
            "forecast_leader": None,
            "top_rocket_pick": None,
            "age_guard_summary": {"eligible_forward_candidates": 0, "raw_candidate_count": 1},
            "candidates": [{
                "identity": identity,
                "asset": "EarlyRocket",
                "pair": "ROCKET/USDC",
                "market_type": "dex_pool",
                "chain": "solana",
                "pool_address": "YoungPool",
                "price": 0.001,
                "source_forecast_score_pre_age_guard": 20,
                "forecast_score": None,
                "forecast_rank": None,
                "return_24h_comparable": False,
                "return_window_status": "INSUFFICIENT_24H_HISTORY",
                "entry_decision": "DISCOVERY ONLY — 24H pool-age qualification unavailable",
                "age_hours_v418": 3.0,
            }],
        }
        report = {
            "discovery_rows_v421": [{
                "identity": identity,
                "early_breakout_score": 90,
                "early_breakout_tier": "EARLY_BREAKOUT_LEADER",
                "early_breakout_components": {"1h_momentum": 10},
                "early_breakout_risk_flags": ["POOL_LESS_THAN_ONE_HOUR_OLD"],
                "dexpaprika_family": "pool_momentum_1h",
                "dexpaprika_rank": 1,
                "quote_symbol": "USDC",
                "dexpaprika_token_order_swapped_for_tracking": True,
            }],
        }
        result = v425.apply_forward_opportunity_ranking(forward, report)
        leader = result["forward_research_leader"]
        self.assertEqual(leader["asset"], "EarlyRocket")
        self.assertEqual(leader["forward_opportunity_rank"], 1)
        self.assertAlmostEqual(leader["forward_opportunity_score"], 69.0)
        self.assertIn("70%", leader["forward_opportunity_ranking_model"])
        self.assertEqual(result["top_opportunities"], [])
        self.assertIsNone(result["top_rocket_pick"])
        self.assertEqual(leader["opportunity_tier"], "HIGH_UPSIDE_EARLY")
        self.assertFalse(leader["historical_24h_comparable"])
        self.assertEqual(leader["quote_symbol"], "USDC")
        self.assertTrue(leader["dexpaprika_token_order_swapped_for_tracking"])
        self.assertIsNone(leader["forecast_rank"])
        self.assertIsNone(result["age_qualified_forecast_leader"])
        self.assertIn("does not exclude a candidate", result["forward_opportunity_ranking"]["forward_horizon"])


    def test_empty_execution_shortlist_falls_back_to_ranked_market_mover_leads(self):
        report = {
            "top_1h_movers": [{
                "identity": "dex:base:token-address",
                "market_type": "dex_pool",
                "chain": "base",
                "pool_address": "pool-address",
                "contract_address": "token-address",
                "asset": "EarlyGainer",
                "symbol": "GAIN",
                "pair": "GAIN/USDC",
                "price_usd": 0.002,
                "change_5m_pct": 12.0,
                "change_1h_pct": 60.0,
                "change_6h_pct": 35.0,
                "change_24h_pct": 55.0,
                "liquidity_usd": 150000,
                "volume_24h_usd": 900000,
                "txns_24h": 1200,
                "age_hours": 4.0,
                "retrieved_utc": "2026-11-07T14:00:00Z",
                "source": "dex-scanner",
            }],
        }

        result = v425.apply_forward_opportunity_ranking({"candidates": []}, report)

        self.assertEqual(result["status"], "NEXT_24H_OPPORTUNITY_RANKED_EXECUTION_UNVERIFIED")
        self.assertTrue(result["forward_opportunity_ranking"]["fallback_market_mover_discovery_used"])
        leader = result["forward_research_leader"]
        self.assertEqual(leader["asset"], "EarlyGainer")
        self.assertEqual(leader["pair"], "GAIN/USDC")
        self.assertIsNone(leader["target_price"])
        self.assertFalse(leader["quality_gate"]["execution_ready"])
        self.assertGreater(leader["forward_opportunity_score"], 0)
        self.assertIsNone(result["top_rocket_pick"])
        self.assertEqual(result["top_opportunities"], [])

    def test_discovery_index_prefers_strongest_early_breakout_family_row(self):
        identity = "dex:base:sharedpool"
        report = {
            "universe_rows_v419": [{
                "identity": identity, "asset": "Shared", "early_breakout_score": 22,
                "early_breakout_tier": "DEVELOPING_MOMENTUM",
            }],
            "top_1h_movers": [{
                "identity": identity, "early_breakout_score": 91,
                "early_breakout_tier": "EARLY_BREAKOUT_LEADER",
                "dexpaprika_family": "pool_momentum_1h",
            }],
        }
        index = v425._report_discovery_index(report)
        self.assertEqual(index[identity]["early_breakout_score"], 91)
        self.assertEqual(index[identity]["dexpaprika_family"], "pool_momentum_1h")

    def test_hourly_candle_overlapping_entry_is_only_a_possible_touch(self):
        position = {
            "entry_price": 1.0,
            "entry_timestamp_utc": "2026-11-05T00:30:00Z",
            "time_stop_utc": "2026-11-06T00:30:00Z",
            "single_take_profit": 1.2,
            "max_price_since_entry": 1.0,
            "min_price_since_entry": 1.0,
            "latest_price": 1.0,
        }
        bars = [{
            "time_open": "2026-11-05T00:00:00Z", "time_close": "2026-11-05T01:00:00Z",
            "high": 1.25, "low": 0.95, "close": 1.1,
        }]
        updated = v425._update_position(position, bars, datetime(2026, 11, 5, 1, tzinfo=timezone.utc))
        self.assertFalse(updated["take_profit_hit"])
        self.assertEqual(updated["result_status"], "TP_TOUCH_UNCONFIRMED")
        self.assertIn("POSSIBLE TP TOUCH", updated["position_status"])


class FinalActionableGateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        self.candidate = {
            "market_type": "dex_pool", "chain": "ethereum", "pool_address": "0xpool",
            "contract_address": "0xtoken", "final_pool_address": "0xpool",
            "final_token_contract": "0xtoken", "final_quote_chain": "ethereum",
            "final_price_verified": True, "final_price_usd": 100.0,
            "final_price_timestamp_utc": self.now.isoformat().replace("+00:00", "Z"),
            "final_quote_token_orientation_verified": True,
            "dex_loss_guard_status": "PASS", "safety_verified": True,
            "executable_route_verified": True,
            "dex_loss_guard_evidence": {"status": "PASS", "checks": {
                "exact_token_and_pool_identity": True, "launch_wallet_analysis": True,
                "lp_position_audit": True, "token_security_audit": True,
                "two_way_route_quotes": True}},
            "dex_execution_quotes": {"provider": "test-router", "timestamp_utc": self.now.isoformat().replace("+00:00", "Z"),
                                     "buy_quote": {"output_amount": "1"}, "sell_quote": {"output_amount": "100"}},
            "liquidity_usd": 150000,
            "target_price": 140.0,
            "target_price_source": "7D_SWING_RESISTANCE",
            "technical_tp_validation": {
                "status": "PASS", "blockers": [],
                "chain": "ethereum", "pool_address": "0xpool",
                "token_address": "0xtoken", "token_orientation_verified": True,
                "current_price": 100.0, "target_price": 140.0,
                "target_price_source": "7D_SWING_RESISTANCE",
                "atr_1h_pct": 10.0, "maximum_tp_distance_pct": 40.0,
                "liquidity_usd": 150000,
                "candle_retrieved_utc": self.now.isoformat().replace("+00:00", "Z"),
            },
            "trade_plan": {"single_take_profit": 140.0},
        }

    def test_exact_40_percent_and_all_evidence_pass(self):
        result = v425.qualify_final_actionable_candidates([self.candidate], self.now)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tp_upside_pct"], 40.0)
        self.assertEqual(result[0]["trade_plan"]["tp_reference_price"], 100.0)

    def test_maximum_early_breakout_score_does_not_bypass_final_gates(self):
        below_tp_gate = {
            **self.candidate,
            "early_breakout_score": 100.0,
            "target_price": 139.99,
            "technical_tp_validation": {
                **self.candidate["technical_tp_validation"], "target_price": 139.99,
            },
            "trade_plan": {"single_take_profit": 139.99},
        }
        unsafe = {**self.candidate, "early_breakout_score": 100.0, "safety_verified": False}
        no_executable_quote = {
            **self.candidate,
            "early_breakout_score": 100.0,
            "executable_route_verified": False,
        }

        self.assertEqual(
            v425.qualify_final_actionable_candidates(
                [below_tp_gate, unsafe, no_executable_quote], self.now
            ),
            [],
        )

    def test_39_99_percent_is_not_actionable(self):
        row = {
            **self.candidate,
            "target_price": 139.99,
            "technical_tp_validation": {
                **self.candidate["technical_tp_validation"], "target_price": 139.99,
            },
            "trade_plan": {"single_take_profit": 139.99},
        }
        self.assertEqual(v425.qualify_final_actionable_candidates([row], self.now), [])
        self.assertIn(
            "technical single-TP upside is below +40.00%",
            row["qualification_blockers"],
        )

    def test_missing_orientation_safety_or_quotes_fail_closed(self):
        for field, value in (("final_quote_token_orientation_verified", False),
                             ("safety_verified", False), ("executable_route_verified", False),
                             ("final_price_verified", False)):
            with self.subTest(field=field):
                row = {**self.candidate, field: value}
                self.assertEqual(v425.qualify_final_actionable_candidates([row], self.now), [])

    def test_stale_final_price_and_pool_mismatch_fail_closed(self):
        old = {**self.candidate, "final_price_timestamp_utc": "2026-11-09T11:54:59Z"}
        mismatch = {**self.candidate, "final_pool_address": "0xother"}
        self.assertEqual(v425.qualify_final_actionable_candidates([old, mismatch], self.now), [])

    def test_recalculates_single_tp_with_refreshed_live_price(self):
        # Candidate has target 140.0. Refreshed live price moves from 100 to 101.
        # Recalculated upside: (140 - 101)/101 = 38.61% (< 40.0%), so it must be excluded.
        drifted_up = {
            **self.candidate,
            "final_price_usd": 101.0,
        }
        res = v425.qualify_final_actionable_candidates([drifted_up], self.now)
        self.assertEqual(res, [])
        self.assertIn("technical single-TP upside is below +40.00%", drifted_up["qualification_blockers"])

        # Refreshed live price drops to 95.0, upside becomes (140 - 95)/95 = 47.37% (>= 40.0%).
        drifted_favorable = {
            **self.candidate,
            "final_price_usd": 95.0,
            "technical_tp_validation": {
                **self.candidate["technical_tp_validation"],
                "current_price": 95.0,
                "atr_1h_pct": 10.0,
                "maximum_tp_distance_pct": 50.0,
            },
        }
        res_ok = v425.qualify_final_actionable_candidates([drifted_favorable], self.now)
        self.assertEqual(len(res_ok), 1)
        self.assertAlmostEqual(res_ok[0]["tp_upside_pct"], 47.368421, places=4)
        self.assertEqual(res_ok[0]["price"], 95.0)

    def test_complete_opportunity_data_and_exact_addresses(self):
        result = v425.qualify_final_actionable_candidates([self.candidate], self.now)
        self.assertEqual(len(result), 1)
        item = result[0]
        # Must include pair, chain, current price, TP, potential, pool address, token contract
        self.assertEqual(item["chain"], "ethereum")
        self.assertEqual(item["price"], 100.0)
        self.assertEqual(item["trade_plan"]["single_take_profit"], 140.0)
        self.assertEqual(item["tp_upside_pct"], 40.0)
        self.assertEqual(item["pool_address"], "0xpool")
        self.assertEqual(item["contract_address"], "0xtoken")
        self.assertEqual(item["verification_status"], "PASS")
        self.assertEqual(item["safety_status"], "PASS")

    def test_missing_pool_address_or_token_contract_fails_closed(self):
        no_pool = {**self.candidate, "pool_address": "", "final_pool_address": ""}
        no_contract = {**self.candidate, "contract_address": "", "final_token_contract": ""}
        self.assertEqual(v425.qualify_final_actionable_candidates([no_pool, no_contract], self.now), [])

    def test_safety_remains_explicit_and_cannot_be_bypassed(self):
        unsafe_candidate = {
            **self.candidate,
            "safety_verified": False,
            "dex_loss_guard_status": "BLOCKED_UNVERIFIED_OR_RISK",
        }
        res = v425.qualify_final_actionable_candidates([unsafe_candidate], self.now)
        self.assertEqual(res, [])
        self.assertIn("mandatory safety or loss-pattern gate did not pass", unsafe_candidate["qualification_blockers"])

    def test_dexpaprika_discovery_triggered_in_build_output_when_birdeye_omitted(self):
        class FakeHunter:
            def __init__(self):
                self.candidates = []
                self.source_status = []
            def add_optional_birdeye(self, api_key, limit=100):
                pass
            def build_output(self):
                return {"coverage": {}, "candidates": list(self.candidates)}

        fake_mod = type("FakeMod", (), {
            "RocketHunter": FakeHunter,
            "_base_record": lambda **kw: dict(kw),
            "_V425_EARLY_BREAKOUT_HOOKS_INSTALLED": False,
        })
        v425.install_early_breakout_hooks(fake_mod)
        hunter = FakeHunter()
        with unittest.mock.patch.object(v425, "_collect_dexpaprika", return_value={"network_count": 2, "provider_scope": "test"}) as mock_collect:
            data = hunter.build_output()
            mock_collect.assert_called_once()
            self.assertIn("dexpaprika_discovery_v425", data)
            self.assertEqual(data["coverage"]["provider_registries_v425"]["dexpaprika"]["network_count"], 2)


if __name__ == "__main__":
    unittest.main()
