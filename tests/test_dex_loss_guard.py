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
    def test_provider_lp_status_and_percentage_alone_never_verify_a_lock(self):
        row, security, route = fixture()
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertEqual(result["status"], "BLOCKED_UNVERIFIED_OR_RISK")
        self.assertFalse(result["entry_ready"])
        self.assertEqual(result["lp_authentication_status"], "UNVERIFIED")
        self.assertTrue(any("cryptographic on-chain proof" in x for x in result["blockers"]))

    def test_lp_claims_require_exact_position_beneficiary_and_authenticated_evidence(self):
        for mutation, expected_blocker in (
            (lambda lp: lp.update({"pool_address": "0x4444444444444444444444444444444444444444"}), "exact candidate pool"),
            (lambda lp: lp.update({"lp_token_address": "0x5555555555555555555555555555555555555555"}), "identity does not match"),
            (lambda lp: lp.pop("beneficiary"), "beneficiary is missing"),
            (lambda lp: lp.update({"authenticated_on_chain_evidence": True}), "cryptographic on-chain proof"),
        ):
            with self.subTest(expected_blocker=expected_blocker):
                row, security, route = fixture()
                row["loss_guard_evidence"]["pool_identity"]["lp_token_address"] = "0x6666666666666666666666666666666666666666"
                lp = row["loss_guard_evidence"]["lp_position"]
                lp.update({
                    "chain": "ethereum",
                    "lp_token_address": "0x6666666666666666666666666666666666666666",
                    "lock_contract_address": "0x7777777777777777777777777777777777777777",
                    "lock_mechanism": "timelock",
                    "mechanism_verified": True,
                    "beneficiary": "0x8888888888888888888888888888888888888888",
                    "locked_amount": 95.0,
                    "total_supply": 100.0,
                    "unlock_conditions": "after timelock expiry",
                    "unlock_timestamp_utc": "2026-11-10T00:00:00Z",
                    "control_type": "LOCKED",
                    "locked_pct": 95.0,
                })
                mutation(lp)
                result = guard.evaluate_dex_candidate(row, security, route, NOW)
                self.assertFalse(result["entry_ready"])
                self.assertTrue(any(expected_blocker in item for item in result["blockers"]))

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

    def test_burned_lp_at_95_percent_still_requires_exact_authenticated_proof(self):
        row, security, route = fixture()
        row["loss_guard_evidence"]["lp_position"].update({
            "control_type": "BURNED", "burned_pct": 95.0, "locked_pct": 95.0,
        })
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertTrue(any("exact LP burn transaction" in x for x in result["blockers"]))
        self.assertTrue(any("cryptographic on-chain proof" in x for x in result["blockers"]))

    def test_cannot_sell_all_contract_flag_is_blocked(self):
        row, security, route = fixture()
        security["audit"]["data"]["cannot_sell_all"] = "1"
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertTrue(any("cannot_sell_all" in x for x in result["blockers"]))
        self.assertTrue(result["hard_reject"])
        self.assertTrue(result["confirmed_security_failures"])

    def test_missing_blacklist_is_a_gap_not_a_clean_security_result(self):
        row, security, route = fixture()
        del security["audit"]["data"]["is_blacklisted"]
        result = guard.evaluate_dex_candidate(row, security, route, NOW)
        self.assertFalse(result["entry_ready"])
        self.assertFalse(result["hard_reject"])
        self.assertTrue(any("is_blacklisted" in x for x in result["blockers"]))

    def test_unknown_boolean_and_nonfinite_or_out_of_range_taxes_do_not_pass(self):
        for field, value in (
            ("is_blacklisted", "unknown"),
            ("buy_tax", float("nan")),
            ("sell_tax", float("inf")),
            ("buy_tax", -0.1),
            ("sell_tax", 100.1),
        ):
            with self.subTest(field=field, value=value):
                row, security, route = fixture()
                security["audit"]["data"][field] = value
                result = guard.evaluate_dex_candidate(row, security, route, NOW)
                self.assertFalse(result["entry_ready"])
                self.assertTrue(any(field in item for item in result["blockers"]))
                self.assertFalse(result["hard_reject"])

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
