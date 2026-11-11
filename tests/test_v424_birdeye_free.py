"""Regression tests for v4.24.0 free-tier Birdeye multichain discovery."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.v424_birdeye_free as provider


class BirdeyeFreeDiscoveryTests(unittest.TestCase):
    def test_supported_network_registry_parses_and_deduplicates_api_chain_slugs(self):
        networks = provider.parse_birdeye_networks({
            "data": [
                {"chain": "solana", "name": "Solana"},
                {"chain": "ethereum", "name": "Ethereum"},
                {"chain": "base", "name": "Base"},
                {"chain": "solana", "name": "Solana"},
            ]
        })
        self.assertEqual([row["chain"] for row in networks], ["solana", "ethereum", "base"])

    def test_supported_network_registry_accepts_string_list(self):
        networks = provider.parse_birdeye_networks({"data": ["solana", "base", "arbitrum"]})
        self.assertEqual([row["chain"] for row in networks], ["solana", "base", "arbitrum"])

    def test_trending_token_rows_are_normalized(self):
        rows = provider.parse_birdeye_token_rows({
            "data": {"tokens": [{
                "address": "0xaaa", "name": "Alpha", "symbol": "AAA", "price": 0.2,
                "priceChange24hPercent": 18, "volume24hUSD": 250000, "liquidity": 80000,
                "mc": 1000000, "fdv": 1200000,
            }]}
        })
        self.assertEqual(len(rows), 1)
        normalized = provider.normalize_birdeye_token(
            rows[0], "base", "token_trending", lambda **kwargs: kwargs, "birdeye_token_trending"
        )
        self.assertEqual(normalized["identity"], "dex:base:0xaaa")
        self.assertEqual(normalized["change_24h"], 18.0)
        self.assertEqual(normalized["volume_24h_usd"], 250000.0)
        self.assertEqual(normalized["liquidity_usd"], 80000.0)
        self.assertEqual(normalized["extra"]["birdeye_family"], "token_trending")

    def test_new_listing_rows_without_price_remain_identity_bearing_discoveries(self):
        rows = provider.parse_birdeye_token_rows({
            "data": {"items": [{"address": "0xnew", "name": "New", "symbol": "NEW"}]}
        })
        normalized = provider.normalize_birdeye_token(
            rows[0], "arbitrum", "new_listing", lambda **kwargs: kwargs, "birdeye_new_listing"
        )
        self.assertEqual(normalized["identity"], "dex:arbitrum:0xnew")
        self.assertIsNone(normalized["price_usd"])
        self.assertIsNone(normalized["change_24h"])

    def test_rotation_sweeps_three_discovery_families_and_skips_sui_new_listing(self):
        networks = provider.parse_birdeye_networks({
            "data": [{"chain": "solana"}, {"chain": "ethereum"}, {"chain": "sui"}]
        })
        slots = provider._make_rotation_slots(networks)
        self.assertEqual(len(slots), 8)
        self.assertEqual([family for network, family in slots if network["chain"] == "solana"],
                         ["token_trending", "new_listing", "tokenlist"])
        self.assertNotIn(("sui", "new_listing"), [(network["chain"], family) for network, family in slots])

    def test_monthly_cu_ledger_has_a_hard_internal_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Path(tmp)
            self.assertTrue(provider._reserve_cu(memory, 19980, "tokenlist"))
            self.assertFalse(provider._reserve_cu(memory, 40, "token_trending"))
            saved = provider._read_cu_ledger(memory)
            self.assertEqual(saved["estimated_cu_reserved"], 19980)
            self.assertEqual(saved["scanner_monthly_cu_ceiling"], 20000)

    def test_registry_cache_is_written_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            class FX:
                timeout = 4
            class Hunter:
                fx = FX()
                source_status = []
                checkpoint_dir = Path(tmp) / "checkpoints"
            hunter = Hunter()
            payload = {"data": [{"chain": "solana"}, {"chain": "base"}]}
            with patch.object(provider, "_fetch_json", return_value=(payload, 200, None)) as fetch:
                first, meta1 = provider._load_network_registry(hunter, "not-a-real-key")
                second, meta2 = provider._load_network_registry(hunter, "not-a-real-key")
            self.assertEqual([row["chain"] for row in first], ["solana", "base"])
            self.assertEqual(second, first)
            self.assertFalse(meta1["registry_cached"])
            self.assertTrue(meta2["registry_cached"])
            self.assertEqual(fetch.call_count, 1)

    def test_post_scan_initializes_ledgers_when_optional_key_is_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outdir = root / "out"
            memory = root / "memory"
            outdir.mkdir()
            (outdir / "rocket-hunt.json").write_text(json.dumps({"coverage": {}}))
            result = provider._merge_birdeye_post_scan(outdir, memory, "no-key-run")
            self.assertEqual(result["estimated_cu_reserved_this_month"], 0)
            self.assertTrue((memory / "birdeye-cu-ledger.json").exists())
            rotation = json.loads((memory / "birdeye-rotation-state.json").read_text())
            self.assertEqual(rotation["last_request_status"], "NOT_CONFIGURED")
            manifest = json.loads((memory / "memory-manifest.json").read_text())
            self.assertEqual(manifest["birdeye_provider_version"], "4.24.0")

    def test_runtime_installer_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            rocket = scripts / "rocket_hunt.py"
            rocket.write_text('def main():\n    pass\n\nif __name__ == "__main__":\n    main()\n')
            first = provider.install_runtime_patches(root)
            second = provider.install_runtime_patches(root)
            installed = rocket.read_text()
            self.assertEqual(first["installed_version"], "4.24.0")
            self.assertEqual(second["installed_version"], "4.24.0")
            self.assertEqual(installed.count("_v424_install(_v424_sys.modules[__name__])"), 1)

    def test_runtime_hook_runs_one_rotating_multichain_family_and_adds_coverage(self):
        original_registry = provider._load_network_registry
        original_fetch = provider._fetch_json
        class FX:
            timeout = 4
        class DummyHunter:
            def __init__(self):
                self.fx = FX()
                self.candidates = []
                self.source_status = []
                self.checkpoint_dir = None
            def add_optional_birdeye(self, api_key, limit=100):
                raise AssertionError("patched handler should replace the legacy Solana-only method")
            def run(self, *args, **kwargs):
                self.add_optional_birdeye(os.environ.get("BIRDEYE_API_KEY"))
                return {"coverage": {}}
        class DummyModule:
            RocketHunter = DummyHunter
            _base_record = staticmethod(lambda **kwargs: kwargs)
        try:
            provider._load_network_registry = lambda hunter, key: (
                [{"chain": "ethereum", "display_name": "Ethereum"},
                 {"chain": "base", "display_name": "Base"}],
                {"registry_complete": True, "registry_cached": True},
            )
            provider._fetch_json = lambda url, api_key, chain=None, timeout=8: (
                {"data": {"tokens": [{
                    "address": "0xabc", "name": "Alpha", "symbol": "AAA",
                    "price": 1.25, "priceChange24hPercent": 12.0,
                }]}}, 200, None
            )
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["SCANNER_MEMORY_DIR"] = tmp
                os.environ["BIRDEYE_API_KEY"] = "test-key-not-real"
                hunter = DummyHunter()
                provider.install_birdeye_hooks(DummyModule)
                result = hunter.run()
                self.assertEqual(len(hunter.candidates), 1)
                self.assertEqual(hunter.candidates[0]["source"], "birdeye_token_trending")
                registry = result["coverage"]["provider_registries_v423"]["birdeye"]
                self.assertEqual(registry["network_count"], 2)
                self.assertEqual(registry["selected_family"], "token_trending")
                self.assertEqual(registry["request_status"], "OK")
                self.assertEqual(hunter.birdeye_coverage_v424["estimated_cu_reserved"], 40)
                rotation = json.loads((Path(tmp) / "birdeye-rotation-state.json").read_text())
                self.assertEqual(rotation["next_slot"], 1)
        finally:
            provider._load_network_registry = original_registry
            provider._fetch_json = original_fetch
            os.environ.pop("SCANNER_MEMORY_DIR", None)
            os.environ.pop("BIRDEYE_API_KEY", None)


if __name__ == "__main__":
    unittest.main()
