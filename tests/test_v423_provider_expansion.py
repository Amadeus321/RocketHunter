"""Regression tests for v4.23.0 provider expansion and network coverage ledger."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.v423_provider_expansion import (
    _merge_provider_state,
    build_dextools_discovery_records,
    install_provider_hooks,
    install_runtime_patches,
    normalize_defillama_registry,
    parse_dextools_blockchains,
)


class ProviderExpansionTests(unittest.TestCase):
    def test_defillama_registry_combines_tvl_and_dex_volume_chain_views(self):
        chains = [
            {"name": "Ethereum"}, {"name": "Base"}, {"name": "Solana"},
        ]
        overview = {
            "allChains": ["Ethereum", "Base", "Arbitrum"],
            "protocols": [
                {"name": "Uniswap", "chains": ["Ethereum", "Base"]},
                {"name": "Camelot", "chains": ["Arbitrum"]},
            ],
        }
        result = normalize_defillama_registry(chains, overview)
        self.assertEqual(result["chain_tvl_registry_count"], 3)
        self.assertEqual(result["dex_volume_chain_count"], 3)
        self.assertEqual(result["combined_chain_identifier_count"], 4)
        self.assertEqual(result["dex_protocol_count"], 2)
        self.assertIn("Solana", result["combined_chain_identifiers"])
        self.assertIn("Arbitrum", result["combined_chain_identifiers"])

    def test_dextools_chain_registry_accepts_paginated_data_envelope(self):
        payload = {"data": [{"id": "ether", "name": "Ethereum"}, {"id": "base", "name": "Base"}]}
        rows = parse_dextools_blockchains(payload)
        self.assertEqual(rows, [{"id": "ether", "name": "Ethereum"}, {"id": "base", "name": "Base"}])

    def test_dextools_hotpairs_preserve_both_token_sides_as_discovery_leads(self):
        def base_record(**kwargs):
            return kwargs
        payload = {"data": [{
            "address": "0xpool",
            "exchangeName": "ExampleDEX",
            "exchangeFactory": "0xfactory",
            "creationTime": "2026-11-01T00:00:00Z",
            "mainToken": {"address": "0xmain", "symbol": "AAA", "name": "Alpha"},
            "sideToken": {"address": "0xside", "symbol": "BBB", "name": "Beta"},
            "rank": 4,
        }]}
        rows = build_dextools_discovery_records("ether", payload, base_record)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["identity"], "dex:ether:0xmain")
        self.assertEqual(rows[1]["identity"], "dex:ether:0xside")
        self.assertEqual(rows[0]["extra"]["pool_address"], "0xpool")
        self.assertIsNone(rows[0]["price_usd"])
        self.assertIsNone(rows[0]["change_24h"])
        self.assertEqual(rows[1]["extra"]["dextools_token_role"], "side")

    def test_provider_registry_memory_is_provider_scoped_and_cumulative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outdir = root / "out"
            memory = root / "memory"
            outdir.mkdir()
            memory.mkdir()
            (outdir / "rocket-hunt.json").write_text(json.dumps({
                "coverage": {"provider_registries_v423": {
                    "defillama": {"networks": ["Ethereum", "Base"], "registry_complete": True},
                    "dexscreener": {"networks": ["solana"], "registry_available": False},
                }}
            }))
            first = _merge_provider_state(outdir, memory, "run-1")
            self.assertEqual(first["defillama"]["cumulative_unique_network_identifier_count"], 2)
            self.assertEqual(first["dexscreener"]["last_run_network_count"], 1)
            (outdir / "rocket-hunt.json").write_text(json.dumps({
                "coverage": {"provider_registries_v423": {
                    "defillama": {"networks": ["Base", "Arbitrum"], "registry_complete": True},
                    "dexscreener": {"networks": ["solana", "sui"], "registry_available": False},
                }}
            }))
            second = _merge_provider_state(outdir, memory, "run-2")
            self.assertEqual(second["defillama"]["cumulative_unique_network_identifier_count"], 3)
            self.assertEqual(second["defillama"]["new_network_identifiers_this_run"], 1)
            self.assertEqual(second["dexscreener"]["cumulative_unique_network_identifier_count"], 2)
            state = json.loads((memory / "provider-network-state.json").read_text())
            self.assertEqual(state["last_run_id"], "run-2")

    def test_runtime_installer_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            rocket = scripts / "rocket_hunt.py"
            rocket.write_text('def main():\n    pass\n\nif __name__ == "__main__":\n    main()\n')
            first = install_runtime_patches(root)
            second = install_runtime_patches(root)
            installed = rocket.read_text()
            self.assertEqual(first["installed_version"], "4.23.0")
            self.assertEqual(second["installed_version"], "4.23.0")
            self.assertEqual(installed.count("_v423_install(_v423_sys.modules[__name__])"), 1)

    def test_runtime_hook_wraps_scanner_and_adds_provider_coverage(self):
        import scripts.v423_provider_expansion as provider
        original_defi = provider._collect_defillama
        original_dextools = provider._collect_dextools

        class DummyHunter:
            def __init__(self):
                self.candidates = []
                self.discovery_leads = []
                self.source_status = []
                self.discovery_config = {
                    "gecko_networks_selected": ["base", "solana"],
                    "gecko_network_registry": {"supported_network_count": 200, "registry_complete": True},
                }
            def run(self, *args, **kwargs):
                self.candidates.append({"source": "dexscreener_search", "chain": "linea"})
                return {"coverage": {}}

        class DummyModule:
            RocketHunter = DummyHunter
            _base_record = staticmethod(lambda **kwargs: kwargs)

        try:
            provider._collect_defillama = lambda hunter: {
                "combined_chain_identifiers": ["Ethereum", "Base"],
                "combined_chain_identifier_count": 2,
                "dex_volume_chains": ["Ethereum"],
                "dex_protocol_count": 12,
                "registry_complete": True,
            }
            provider._collect_dextools = lambda hunter, base_record_fn: {
                "chains": ["ether", "bsc"],
                "selected_chain": "ether",
                "registry_complete": True,
                "hotpairs_ok": True,
            }
            install_provider_hooks(DummyModule)
            result = DummyHunter().run()
            providers = result["coverage"]["provider_registries_v423"]
            self.assertEqual(providers["defillama"]["network_count"], 2)
            self.assertEqual(providers["dextools"]["network_count"], 2)
            self.assertEqual(providers["dexscreener"]["networks"], ["linea"])
            self.assertEqual(providers["geckoterminal"]["supported_network_count"], 200)
        finally:
            provider._collect_defillama = original_defi
            provider._collect_dextools = original_dextools


if __name__ == "__main__":
    unittest.main()
