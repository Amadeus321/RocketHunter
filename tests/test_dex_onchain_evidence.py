"""Offline regression tests for the read-only DEX on-chain evidence adapters."""
import unittest
from datetime import datetime, timezone

from scripts import dex_onchain_evidence as evidence
from scripts import dex_loss_guard


NOW = datetime(2026, 11, 7, 0, 0, tzinfo=timezone.utc)
NOW_STR = "2026-11-07T00:00:00Z"
TOKEN = "0x1111111111111111111111111111111111111111"
POOL = "0x2222222222222222222222222222222222222222"
QUOTE = "0x3333333333333333333333333333333333333333"


class OnchainEvidenceTests(unittest.TestCase):
    def test_rugcheck_holder_percentage_is_not_scaled_as_fraction(self):
        self.assertEqual(evidence._pct(0.5), 0.5)
        self.assertEqual(evidence._pct("79.3"), 79.3)

    def test_connected_wallet_graph_sums_holder_supply_by_component(self):
        report = {
            "topHolders": [
                {"owner": "wallet-A", "pct": 42.0},
                {"owner": "wallet-B", "pct": 38.0},
                {"owner": "wallet-C", "pct": 10.0},
            ]
        }
        graph = {
            "nodes": [
                {"id": "a", "address": "wallet-A"},
                {"id": "b", "address": "wallet-B"},
                {"id": "c", "address": "wallet-C"},
            ],
            "links": [{"source": "a", "target": "b"}],
        }
        metrics = evidence._cluster_metrics(report, graph)
        self.assertAlmostEqual(metrics["coverage_pct"], 90.0)
        self.assertAlmostEqual(metrics["largest_linked_wallet_cluster_pct"], 80.0)

    def test_case_sensitive_wallet_ids_are_not_lowercased_or_merged(self):
        report = {
            "topHolders": [
                {"owner": "AbCWallet", "pct": 40.0},
                {"owner": "abcwallet", "pct": 35.0},
            ]
        }
        graph = {
            "nodes": [
                {"id": "AbCWallet"},
                {"id": "abcwallet"},
            ],
            "links": [],
        }
        metrics = evidence._cluster_metrics(report, graph, case_sensitive=True)
        self.assertAlmostEqual(metrics["coverage_pct"], 75.0)
        self.assertAlmostEqual(metrics["largest_linked_wallet_cluster_pct"], 40.0)

    def test_blockscout_pro_registry_resolves_chain_slug_dynamically(self):
        original_http = evidence._http_json
        original_cache = evidence._BLOCKSCOUT_PRO_REGISTRY_CACHE
        original_logs = evidence._BLOCKSCOUT_PRO_REGISTRY_LOGS

        def fake_http(url, timeout=8, headers=None):
            if url == evidence.BLOCKSCOUT_PRO_CONFIG_URL:
                return {"chains": {"1": {}, "8453": {}, "42161": {}}}, {"ok": True}
            if "chain_ids=" in url:
                return {
                    "1": {"name": "Ethereum", "isTestnet": False},
                    "8453": {"name": "Base", "isTestnet": False},
                    "42161": {"name": "Arbitrum One", "isTestnet": False},
                }, {"ok": True}
            raise AssertionError(url)

        try:
            evidence._http_json = fake_http
            evidence._BLOCKSCOUT_PRO_REGISTRY_CACHE = None
            evidence._BLOCKSCOUT_PRO_REGISTRY_LOGS = []
            self.assertEqual(evidence._blockscout_pro_chain_id("base", timeout=1), "8453")
            self.assertEqual(evidence._blockscout_pro_chain_id("arbitrum", timeout=1), "42161")
            self.assertEqual(evidence._blockscout_pro_chain_id("ethereum", timeout=1), "1")
        finally:
            evidence._http_json = original_http
            evidence._BLOCKSCOUT_PRO_REGISTRY_CACHE = original_cache
            evidence._BLOCKSCOUT_PRO_REGISTRY_LOGS = original_logs

    def test_blockscout_pro_page_uses_bearer_authentication(self):
        original_http = evidence._http_json
        original_key = evidence.os.environ.get("BLOCKSCOUT_PRO_API_KEY")
        captured = {}

        def fake_http(url, timeout=8, headers=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            return {"items": [], "next_page_params": None}, {"ok": True}

        try:
            evidence.os.environ["BLOCKSCOUT_PRO_API_KEY"] = "proapi_test_key"
            evidence._http_json = fake_http
            evidence._blockscout_page(
                "https://api.blockscout.com/8453",
                "/api/v2/addresses/0x" + "a" * 40 + "/transactions",
                timeout=1,
            )
        finally:
            evidence._http_json = original_http
            if original_key is None:
                evidence.os.environ.pop("BLOCKSCOUT_PRO_API_KEY", None)
            else:
                evidence.os.environ["BLOCKSCOUT_PRO_API_KEY"] = original_key
        self.assertEqual(captured["headers"]["Authorization"], "Bearer proapi_test_key")
        self.assertIn("/8453/api/v2/addresses/", captured["url"])

    def test_unknown_evm_chain_stays_unverified_without_pro_registry_support(self):
        original_hosts = evidence.BLOCKSCOUT_HOSTS.copy()
        original_key = evidence.os.environ.get("BLOCKSCOUT_PRO_API_KEY")
        original_http = evidence._http_json
        original_cache = evidence._BLOCKSCOUT_PRO_REGISTRY_CACHE
        original_logs = evidence._BLOCKSCOUT_PRO_REGISTRY_LOGS

        def fake_http(url, timeout=8, headers=None):
            if url == evidence.BLOCKSCOUT_PRO_CONFIG_URL:
                return {"chains": {"1": {}}}, {"ok": True}
            if "chain_ids=" in url:
                return {"1": {"name": "Ethereum", "isTestnet": False}}, {"ok": True}
            raise AssertionError(url)

        try:
            evidence.os.environ["BLOCKSCOUT_PRO_API_KEY"] = "proapi_test_key"
            evidence._http_json = fake_http
            evidence._BLOCKSCOUT_PRO_REGISTRY_CACHE = None
            evidence._BLOCKSCOUT_PRO_REGISTRY_LOGS = []
            base, provider = evidence._blockscout_base_for_chain("mystery-chain", timeout=1)
        finally:
            evidence.BLOCKSCOUT_HOSTS = original_hosts
            evidence._http_json = original_http
            evidence._BLOCKSCOUT_PRO_REGISTRY_CACHE = original_cache
            evidence._BLOCKSCOUT_PRO_REGISTRY_LOGS = original_logs
            if original_key is None:
                evidence.os.environ.pop("BLOCKSCOUT_PRO_API_KEY", None)
            else:
                evidence.os.environ["BLOCKSCOUT_PRO_API_KEY"] = original_key
        self.assertIsNone(base)
        self.assertEqual(provider, "No Blockscout index configured for this chain")

    def test_blockscout_common_funder_graph_detects_shared_unlabelled_eoa(self):
        original = evidence._blockscout_page
        holder_a = "0x" + "a" * 40
        holder_b = "0x" + "b" * 40
        funder = "0x" + "f" * 40

        def fake_page(base, path, params=None, timeout=8):
            if path.endswith("/addresses/" + holder_a + "/transactions") or path.endswith("/addresses/" + holder_b + "/transactions"):
                holder = path.split("/addresses/")[1].split("/")[0]
                return {
                    "items": [{
                        "hash": "0x" + ("1" if holder == holder_a else "2") * 64,
                        "block_number": 10,
                        "value": "1000000000000000000",
                        "from": {"hash": funder, "is_contract": False, "name": "Personal wallet"},
                        "to": {"hash": holder},
                    }],
                    "next_page_params": None,
                }, {"ok": True}
            raise AssertionError(path)

        try:
            evidence._blockscout_page = fake_page
            result = evidence._blockscout_common_funder_graph(
                "https://mock.blockscout", [
                    {"address": {"hash": holder_a, "is_contract": False}},
                    {"address": {"hash": holder_b, "is_contract": False}},
                ], {holder_a: 45.0, holder_b: 40.0}, timeout=1)
        finally:
            evidence._blockscout_page = original
        self.assertEqual(result["status"], "VERIFIED")
        self.assertAlmostEqual(result["coverage_pct"], 85.0)
        self.assertAlmostEqual(result["largest_linked_wallet_cluster_pct"], 85.0)
        self.assertEqual(result["shared_funder_cluster_count"], 1)

    def test_blockscout_known_exchange_funder_is_not_counted_as_ownership_cluster(self):
        original = evidence._blockscout_page
        holder_a = "0x" + "a" * 40
        holder_b = "0x" + "b" * 40
        funder = "0x" + "f" * 40

        def fake_page(base, path, params=None, timeout=8):
            if path.endswith("/addresses/" + holder_a + "/transactions") or path.endswith("/addresses/" + holder_b + "/transactions"):
                holder = path.split("/addresses/")[1].split("/")[0]
                return {
                    "items": [{
                        "block_number": 10, "value": "100",
                        "from": {"hash": funder, "is_contract": False, "name": "Centralized Exchange Hot Wallet"},
                        "to": {"hash": holder},
                    }],
                    "next_page_params": None,
                }, {"ok": True}
            raise AssertionError(path)

        try:
            evidence._blockscout_page = fake_page
            result = evidence._blockscout_common_funder_graph(
                "https://mock.blockscout", [
                    {"address": {"hash": holder_a, "is_contract": False}},
                    {"address": {"hash": holder_b, "is_contract": False}},
                ], {holder_a: 45.0, holder_b: 40.0}, timeout=1)
        finally:
            evidence._blockscout_page = original
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["largest_linked_wallet_cluster_pct"], 0.0)
        self.assertEqual(result["shared_funder_cluster_count"], 0)

    def test_blockscout_paginated_holder_history_cannot_claim_wallet_coverage(self):
        original = evidence._blockscout_page
        holder_a = "0x" + "a" * 40
        holder_b = "0x" + "b" * 40
        funder = "0x" + "f" * 40

        def fake_page(base, path, params=None, timeout=8):
            holder = path.split("/addresses/")[1].split("/")[0]
            return {
                "items": [{
                    "block_number": 10, "value": "100",
                    "from": {"hash": funder, "is_contract": False},
                    "to": {"hash": holder},
                }],
                "next_page_params": {"block_number": 1},
            }, {"ok": True}

        try:
            evidence._blockscout_page = fake_page
            result = evidence._blockscout_common_funder_graph(
                "https://mock.blockscout", [
                    {"address": {"hash": holder_a, "is_contract": False}},
                    {"address": {"hash": holder_b, "is_contract": False}},
                ], {holder_a: 45.0, holder_b: 40.0}, timeout=1)
        finally:
            evidence._blockscout_page = original
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["coverage_pct"], 0.0)
        self.assertEqual(result["shared_funder_cluster_count"], 0)

    def test_evm_token_transfer_links_do_not_qualify_as_common_funder_clusters(self):
        original = evidence._blockscout_page
        original_registry = evidence._blockscout_pro_registry

        def fake_page(base, path, params=None, timeout=8):
            if path.endswith("/api/v2/tokens/" + TOKEN):
                return {
                    "total_supply": "1000", "decimals": "0",
                    "creator_address_hash": "0x" + "a" * 40,
                }, {"ok": True}
            if path.endswith("/holders"):
                return {
                    "items": [{"address": {"hash": "0x" + "b" * 40}, "value": "900"}],
                    "next_page_params": None,
                }, {"ok": True}
            if path.endswith("/transfers"):
                return {
                    "items": [{
                        "from": {"hash": "0x" + "b" * 40},
                        "to": {"hash": "0x" + "c" * 40},
                        "total": {"value": "100"},
                        "block_number": 1,
                        "transaction_hash": "0x" + "d" * 64,
                    }],
                    "next_page_params": None,
                }, {"ok": True}
            raise AssertionError(path)

        try:
            evidence._blockscout_page = fake_page
            evidence._blockscout_pro_registry = lambda timeout=8: ({}, [])
            launch, _ = evidence._blockscout_holder_history("ethereum", TOKEN)
        finally:
            evidence._blockscout_page = original
            evidence._blockscout_pro_registry = original_registry
        self.assertEqual(launch["status"], "UNVERIFIED")
        self.assertEqual(launch["coverage_pct"], 0.0)
        self.assertEqual(launch["largest_linked_wallet_cluster_pct"], 0.0)
        self.assertEqual(launch["common_funder_cluster_status"], "UNVERIFIED")
        self.assertAlmostEqual(launch["transfer_graph_holder_coverage_pct"], 90.0)

    def test_initial_mint_scanner_skips_initialize_transaction(self):
        original = evidence._rpc_post
        calls = []
        signatures = [
            {"signature": "latest-trade"},
            {"signature": "initial-mint"},
            {"signature": "initialize-mint"},
        ]

        def fake_rpc(url, method, params, timeout):
            calls.append((method, params))
            if method == "getSignaturesForAddress":
                return signatures, {"ok": True}
            if method == "getTokenSupply":
                return {"value": {"amount": "1000"}}, {"ok": True}
            signature = params[0]
            if signature == "initialize-mint":
                return {"meta": {"preTokenBalances": [], "postTokenBalances": []}}, {"ok": True}
            if signature == "initial-mint":
                return {
                    "meta": {
                        "preTokenBalances": [],
                        "postTokenBalances": [
                            {"mint": "Mint111", "owner": "Creator111",
                             "uiTokenAmount": {"amount": "793"}},
                            {"mint": "Mint111", "owner": "Other111",
                             "uiTokenAmount": {"amount": "207"}},
                        ],
                    }
                }, {"ok": True}
            return {"meta": {"preTokenBalances": [], "postTokenBalances": []}}, {"ok": True}

        try:
            evidence._rpc_post = fake_rpc
            result = evidence._solana_initial_creator_allocation("Mint111", "Creator111", timeout=1)
        finally:
            evidence._rpc_post = original
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["first_mint_transaction"], "initial-mint")
        self.assertAlmostEqual(result["deployer_initial_supply_pct"], 79.3)

    def test_initial_mint_allocation_is_unverified_when_supply_does_not_reconcile(self):
        original = evidence._rpc_post
        signatures = [{"signature": "initial-mint"}, {"signature": "initialize-mint"}]

        def fake_rpc(url, method, params, timeout):
            if method == "getSignaturesForAddress":
                return signatures, {"ok": True}
            if method == "getTokenSupply":
                return {"value": {"amount": "1001"}}, {"ok": True}
            if params[0] == "initial-mint":
                return {
                    "meta": {
                        "preTokenBalances": [],
                        "postTokenBalances": [
                            {"mint": "Mint111", "owner": "Creator111",
                             "uiTokenAmount": {"amount": "793"}},
                            {"mint": "Mint111", "owner": "Other111",
                             "uiTokenAmount": {"amount": "207"}},
                        ],
                    }
                }, {"ok": True}
            return {"meta": {"preTokenBalances": [], "postTokenBalances": []}}, {"ok": True}

        try:
            evidence._rpc_post = fake_rpc
            result = evidence._solana_initial_creator_allocation("Mint111", "Creator111", timeout=1)
        finally:
            evidence._rpc_post = original
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertIn("does not exactly reconcile", result["reason"])

    def test_solana_lp_dead_address_lock_matches_exact_lp_mint(self):
        original_rpc = evidence._rpc_post
        original_url = evidence._solana_rpc

        def fake_rpc(url, method, params, timeout):
            if method == "getTokenSupply":
                return {"value": {"amount": "1000"}}, {"ok": True}
            if method == "getTokenLargestAccounts":
                return {"value": [{"address": "LPAccount111"}]}, {"ok": True}
            if method == "getMultipleAccounts":
                return {"value": [{
                    "data": {"parsed": {"info": {
                        "owner": evidence.SOLANA_INCINERATOR,
                        "mint": "LpMint111",
                        "tokenAmount": {"amount": "950"},
                    }}}
                }]}, {"ok": True}
            raise AssertionError("Unexpected RPC method " + method)

        try:
            evidence._solana_rpc = lambda timeout=8: "mock-rpc"
            evidence._rpc_post = fake_rpc
            result = evidence._solana_lp_lock_share("Pool111", "LpMint111", timeout=1)
        finally:
            evidence._rpc_post = original_rpc
            evidence._solana_rpc = original_url
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["control_type"], "LOCKED")
        self.assertAlmostEqual(result["locked_pct"], 95.0)
        self.assertIn("not burned", result["note"])

    def test_evm_pool_identity_requires_exact_token_to_be_one_of_pool_tokens(self):
        original = evidence._eth_call
        try:
            def fake_call(to, data, chain, timeout):
                if data == "0x0dfe1681":
                    return int(TOKEN, 16), {"ok": True}
                if data == "0xd21220a7":
                    return int(QUOTE, 16), {"ok": True}
                return None, {"ok": False, "error": "unexpected selector"}
            evidence._eth_call = fake_call
            result = evidence._evm_pool_identity({
                "chain": "ethereum", "contract_address": TOKEN, "pool_address": POOL,
            })
        finally:
            evidence._eth_call = original
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["quote_token_address"], QUOTE)

    def test_evm_lp_dead_address_lock_is_measured_against_exact_pair_supply(self):
        original = evidence._eth_call
        try:
            def fake_call(to, data, chain, timeout):
                if data == "0x18160ddd":
                    return 1000, {"ok": True}
                if data.endswith("dead"):
                    return 950, {"ok": True}
                if data.endswith("0000"):
                    return 0, {"ok": True}
                return 0, {"ok": True}
            evidence._eth_call = fake_call
            result = evidence._evm_lp_lock_share(POOL, "ethereum", timeout=1)
        finally:
            evidence._eth_call = original
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["control_type"], "LOCKED")
        self.assertAlmostEqual(result["locked_pct"], 95.0)
        self.assertIn("not burned", result["note"])

    def test_unsupported_chain_never_fabricates_verified_wallet_or_lp_data(self):
        result = evidence.enrich_candidate({
            "chain": "unsupported-chain", "contract_address": TOKEN, "pool_address": POOL,
        })
        self.assertEqual(result["summary"]["status"], "UNVERIFIED")
        self.assertIsNone(result["loss_guard_evidence"]["expected_notional_usd"])
        self.assertNotIn("launch_wallet", result["loss_guard_evidence"])

    def test_rugcheck_security_requires_authorities_metadata_and_risk_list(self):
        report = {
            "mintAuthority": None, "freezeAuthority": None,
            "tokenMeta": {"mutable": False},
            "risks": [],
        }
        result = evidence._solana_security(report, {}, NOW_STR)
        self.assertEqual(result["solana_security"]["status"], "VERIFIED")
        blockers = []
        dex_loss_guard._security_checks(
            result, {"rugcheck_security": result["solana_security"]}, NOW, blockers)
        self.assertEqual(blockers, [])

    def test_active_solana_mint_authority_blocks_security_pass(self):
        report = {
            "token": {"mintAuthority": "Creator111", "freezeAuthority": None},
            "tokenMeta": {"mutable": False},
            "risks": [],
        }
        result = evidence._solana_security(report, {}, NOW_STR)
        blockers = []
        dex_loss_guard._security_checks(
            result, {"rugcheck_security": result["solana_security"]}, NOW, blockers)
        self.assertTrue(any("mint authority remains active" in x for x in blockers))

    def test_missing_rugcheck_mutability_field_is_not_verified(self):
        report = {
            "token": {"mintAuthority": None, "freezeAuthority": None},
            "tokenMeta": {},
            "risks": [],
        }
        result = evidence._solana_security(report, {}, NOW_STR)
        self.assertEqual(result["solana_security"]["status"], "UNVERIFIED")

    def test_empty_string_authority_is_not_treated_as_revoked(self):
        report = {
            "token": {"mintAuthority": "", "freezeAuthority": None},
            "tokenMeta": {"mutable": False},
            "risks": [],
        }
        result = evidence._solana_security(report, {}, NOW_STR)
        blockers = []
        dex_loss_guard._security_checks(
            result, {"rugcheck_security": result["solana_security"]}, NOW, blockers)
        self.assertTrue(any("mint authority remains active or malformed" in x for x in blockers))

    def test_string_true_metadata_flag_is_not_treated_as_immutable(self):
        report = {
            "token": {"mintAuthority": None, "freezeAuthority": None},
            "tokenMeta": {"mutable": "true"},
            "risks": [],
        }
        result = evidence._solana_security(report, {}, NOW_STR)
        blockers = []
        dex_loss_guard._security_checks(
            result, {"rugcheck_security": result["solana_security"]}, NOW, blockers)
        self.assertTrue(any("mutable or not explicitly immutable" in x for x in blockers))


if __name__ == "__main__":
    unittest.main()
