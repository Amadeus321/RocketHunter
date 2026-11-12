import unittest
from scripts.unified_network_registry import (
    CANONICAL_NETWORKS,
    UnifiedNetworkRegistry,
    get_provider_chain_id,
    is_provider_supported,
    normalize_chain,
)


class UnifiedNetworkRegistryTests(unittest.TestCase):
    def test_normalize_chain_aliases(self):
        self.assertEqual(normalize_chain("sol"), "solana")
        self.assertEqual(normalize_chain("solana"), "solana")
        self.assertEqual(normalize_chain("eth"), "ethereum")
        self.assertEqual(normalize_chain("mainnet"), "ethereum")
        self.assertEqual(normalize_chain("bsc"), "bsc")
        self.assertEqual(normalize_chain("binance-smart-chain"), "bsc")
        self.assertEqual(normalize_chain("base"), "base")
        self.assertEqual(normalize_chain("matic"), "polygon")
        self.assertEqual(normalize_chain("polygon_pos"), "polygon")
        self.assertEqual(normalize_chain("arb-arbitrum"), "arbitrum")
        self.assertEqual(normalize_chain("avax"), "avalanche")
        self.assertEqual(normalize_chain("sui-network"), "sui")

    def test_provider_chain_id_mapping(self):
        self.assertEqual(get_provider_chain_id("solana", "dexscreener"), "solana")
        self.assertEqual(get_provider_chain_id("solana", "defillama"), "Solana")
        self.assertEqual(get_provider_chain_id("solana", "geckoterminal"), "solana")
        self.assertEqual(get_provider_chain_id("solana", "dexpaprika"), "sol-solana")

        self.assertEqual(get_provider_chain_id("ethereum", "geckoterminal"), "eth")
        self.assertEqual(get_provider_chain_id("bsc", "dexpaprika"), "bnb-binance-smart-chain")
        self.assertEqual(get_provider_chain_id("arbitrum", "dexscreener"), "arbitrum")

    def test_is_provider_supported(self):
        self.assertTrue(is_provider_supported("solana", "dexscreener"))
        self.assertTrue(is_provider_supported("ethereum", "defillama"))
        self.assertFalse(is_provider_supported("nonexistent_chain_xyz", "dexscreener"))

    def test_registry_tracking_and_summary(self):
        reg = UnifiedNetworkRegistry()
        reg.record_provider_result("solana", "dexscreener", "OK", pools_count=15)
        reg.record_provider_result("solana", "geckoterminal", "OK", pools_count=20)
        reg.record_provider_result("solana", "defillama", "OK", pools_count=0)
        reg.record_provider_result("ethereum", "dexscreener", "OK", pools_count=10)

        reg.update_flow_weight("solana", flow_score=2.2, weight=1.85)

        summary = reg.get_summary()
        self.assertGreaterEqual(summary["total_registered_networks"], 20)
        self.assertGreaterEqual(summary["networks_with_usable_pools"], 2)

        sol_data = summary["networks"]["solana"]
        self.assertEqual(sol_data["total_usable_pools"], 35)
        self.assertEqual(sol_data["ecosystem_flow_score"], 2.2)
        self.assertEqual(sol_data["discovery_budget_weight"], 1.85)
        self.assertEqual(sol_data["providers"]["dexscreener"]["pools_count"], 15)


if __name__ == "__main__":
    unittest.main()
