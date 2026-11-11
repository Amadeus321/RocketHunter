"""Regression tests for the v4.26 DEX loss-pattern entry guard."""
import unittest
from datetime import datetime, timezone

from scripts import dex_loss_guard as guard


NOW = datetime(2026, 11, 7, 0, 0, tzinfo=timezone.utc)
NOW_STR = "2026-11-07T00:00:00Z"
TOKEN = "0x1111111111111111111111111111111111111111"
POOL = "0x2222222222222222222222222222222222222222"


def fixture():
    row = {
        "market_type": "dex_pool",
        "chain": "ethereum",
        "contract_address": TOKEN,
        "pool_address": POOL,
        "age_hours": 30,
        "loss_guard_evidence": {
            "expected_notional_usd": 100.0,
            "pool_identity": {
                "status": "VERIFIED", "source": "independent-pool-indexer",
                "as_of_utc": NOW_STR, "chain": "ethereum",
                "contract_address": TOKEN, "pool_address": POOL,
                "quote_token_address": "0x3333333333333333333333333333333333333333",
            },
            "launch_wallet": {
                "status": "VERIFIED", "source": "launch-wallet-graph",
                "as_of_utc": NOW_STR, "coverage_pct": 95.0,
                "deployer_initial_supply_pct": 5.0,
                "largest_linked_wallet_cluster_pct": 10.0,
            },
            "lp_position": {
                "status": "VERIFIED", "source": "lp-position-indexer",
                "as_of_utc": NOW_STR, "pool_address": POOL,
                "position_identity_verified": True,
                "control_type": "TIMELOCKED_MULTISIG",
                "withdrawal_delay_hours": 72.0,
            },
        },
    }
    security = {
        "chain": "ethereum", "contract_address": TOKEN,
        "source": "GoPlus",
        "status": "PROVIDER_CHECK_RETURNED_REVIEW_REQUIRED",
        "retrieved_utc": NOW_STR,
        "audit": {"data": {
            "is_honeypot": "0", "cannot_buy": "0", "cannot_sell_all": "0",
            "hidden_owner": "0", "can_take_back_ownership": "0",
            "owner_change_balance": "0", "selfdestruct": "0",
            "is_mintable": "0", "transfer_pausable": "0",
            "is_blacklisted": "0", "is_open_source": "1", "is_proxy": "0",
            "buy_tax": "0", "sell_tax": "0",
        }, "risk_flags": []},
    }
    route = {
        "chain": "ethereum", "contract_address": TOKEN,
        "provider": "0x", "status": "BUY_AND_SELL_FIRM_QUOTES_RETURNED",
        "timestamp_utc": NOW_STR, "notional_usd": 100.0,
        "buy_quote": {"buy_amount_token_raw": "1000000"},
        "sell_quote": {"buy_amount_usdc_raw": "99000000"},
    }
    return row, security, route


class DexLossGuardTests(unittest.TestCase):
    def test_complete_verified_evidence_passes(self):
        row, security, route = fixture()
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["entry_ready"])
        self.assertEqual(result["blockers"], [])

    def test_forward_trade_plan_is_blocked_without_loss_guard_pass(self):
        from scripts import forward_24h_report
        top = {
            "market_type": "dex_pool", "pool_address": POOL,
            "contract_address": TOKEN, "price": 1.0,
            "data_fresh": True, "market_verified": True,
            "target_price": 1.2, "target_price_source": "CANDLE_DERIVED",
            "dex_loss_guard_status": "BLOCKED_UNVERIFIED_OR_RISK",
        }
        market_meta = {
            "pool_address": POOL, "contract_address": TOKEN,
            "target_price": 1.2, "target_price_source": "CANDLE_DERIVED",
            "_candles_count": 20,
        }
        proposed_plan = {"trade_plan": {"entry_price": 1.0, "single_take_profit": 1.2}}
        plan, missing = forward_24h_report.enforce_dex_loss_guard(top, proposed_plan, [])
        self.assertIsNone(plan)
        self.assertTrue(any("loss-pattern guard PASS" in item for item in missing))

    def test_missing_wallet_history_fails_closed(self):
        row, security, route = fixture()
        del row["loss_guard_evidence"]["launch_wallet"]
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertTrue(any("launch-allocation" in x for x in result["blockers"]))

    def test_solana_token_and_pool_addresses_remain_case_sensitive(self):
        row, security, route = fixture()
        row.update({"chain": "solana", "contract_address": "MintAbC", "pool_address": "PoolXyZ"})
        evidence = row["loss_guard_evidence"]
        evidence["pool_identity"].update({
            "chain": "solana", "contract_address": "mintabc",
            "pool_address": "PoolXyZ", "quote_token_address": "Quote123",
        })
        evidence["lp_position"]["pool_address"] = "PoolXyZ"
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertIn("pool identity token contract does not match candidate contract", result["blockers"])

    def test_concentrated_deployer_initial_allocation_is_blocked(self):
        row, security, route = fixture()
        row["loss_guard_evidence"]["launch_wallet"]["deployer_initial_supply_pct"] = 79.3
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertTrue(any("deployer initial-supply allocation exceeds 15%" in x for x in result["blockers"]))

    def test_connected_wallet_cluster_concentration_is_blocked(self):
        row, security, route = fixture()
        row["loss_guard_evidence"]["launch_wallet"]["largest_linked_wallet_cluster_pct"] = 40.1
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertTrue(any("connected-wallet cluster controls more than 20%" in x for x in result["blockers"]))

    def test_proxy_requires_implementation_and_admin_review(self):
        row, security, route = fixture()
        security["audit"]["data"]["is_proxy"] = "1"
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertTrue(any("proxy implementation/admin review missing" in x for x in result["blockers"]))

    def test_lp_evidence_must_match_exact_pool(self):
        row, security, route = fixture()
        row["loss_guard_evidence"]["lp_position"]["pool_address"] = "0x4444444444444444444444444444444444444444"
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertTrue(any("exact candidate pool" in x for x in result["blockers"]))

    def test_sell_route_is_mandatory(self):
        row, security, route = fixture()
        route["status"] = "BUY_ROUTE_ONLY"
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertTrue(any("two-way buy and sell" in x for x in result["blockers"]))

    def test_quote_must_match_intended_position_size(self):
        row, security, route = fixture()
        route["notional_usd"] = 25.0
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertTrue(any("differs from intended position size" in x for x in result["blockers"]))

    def test_very_young_pool_is_blocked(self):
        row, security, route = fixture()
        row["age_hours"] = 0.5
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertTrue(any("younger than 2 hours" in x for x in result["blockers"]))

    def test_burned_lp_below_95_percent_is_not_verified(self):
        row, security, route = fixture()
        row["loss_guard_evidence"]["lp_position"].update({
            "control_type": "BURNED", "burned_pct": 94.99, "locked_pct": 94.99,
        })
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertTrue(any("burned LP share is below 95%" in x for x in result["blockers"]))

    def test_burned_lp_at_95_percent_can_pass_when_all_other_lanes_pass(self):
        row, security, route = fixture()
        row["loss_guard_evidence"]["lp_position"].update({
            "control_type": "BURNED", "burned_pct": 95.0, "locked_pct": 95.0,
        })
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["entry_ready"])

    def test_cannot_sell_all_contract_flag_is_blocked(self):
        row, security, route = fixture()
        security["audit"]["data"]["cannot_sell_all"] = "1"
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertTrue(any("cannot_sell_all" in x for x in result["blockers"]))

    def test_stale_router_quote_is_blocked(self):
        row, security, route = fixture()
        route["timestamp_utc"] = "2026-11-06T23:54:00Z"
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertTrue(any("older than 5 minutes" in x for x in result["blockers"]))

    def test_stale_launch_wallet_evidence_is_blocked(self):
        row, security, route = fixture()
        row["loss_guard_evidence"]["launch_wallet"]["as_of_utc"] = "2026-11-06T22:59:00Z"
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertTrue(any("launch-wallet analysis: evidence is future-dated or stale" in x
                            for x in result["blockers"]))


if __name__ == "__main__":
    unittest.main()
