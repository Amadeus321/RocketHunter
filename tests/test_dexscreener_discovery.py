import unittest
from scripts.dexscreener_discovery import (
    DexScreenerDiscoveryEngine,
    MAX_BATCH_SIZE,
    parse_dexscreener_pair_record,
)


class DexScreenerDiscoveryTests(unittest.TestCase):
    def test_parse_dexscreener_pair_record(self):
        sample_pair = {
            "chainId": "solana",
            "dexId": "raydium",
            "url": "https://dexscreener.com/solana/pool123",
            "pairAddress": "PoolAddress111",
            "baseToken": {
                "address": "TokenAddressAAA",
                "name": "Alpha Token",
                "symbol": "ALPHA",
            },
            "quoteToken": {
                "address": "So11111111111111111111111111111111111111112",
                "symbol": "SOL",
            },
            "priceUsd": "1.25",
            "volume": {
                "h24": 500000.0,
                "h6": 120000.0,
                "h1": 25000.0,
                "m5": 3000.0,
            },
            "priceChange": {
                "h24": 42.5,
                "h1": 5.2,
                "m5": 1.1,
            },
            "liquidity": {
                "usd": 150000.0,
            },
            "txns": {
                "h24": {"buys": 350, "sells": 120},
            },
            "pairCreatedAt": 1700000000000,
        }

        record = parse_dexscreener_pair_record(
            sample_pair,
            source_lane="dexscreener_test",
            promo_flags={"latest_boost"},
            extra_meta={"boost_amount": 25.0},
        )
        self.assertIsNotNone(record)
        self.assertEqual(record["identity"], "dex:solana:tokenaddressaaa")
        self.assertEqual(record["asset"], "Alpha Token")
        self.assertEqual(record["symbol"], "ALPHA")
        self.assertEqual(record["pair"], "ALPHA/SOL")
        self.assertEqual(record["chain"], "solana")
        self.assertEqual(record["price_usd"], 1.25)
        self.assertEqual(record["volume_24h"], 500000.0)
        self.assertEqual(record["liquidity_usd"], 150000.0)
        self.assertEqual(record["extra"]["buys_24h"], 350)
        self.assertIn("latest_boost", record["extra"]["promo_flags"])
        self.assertEqual(record["extra"]["boost_amount"], 25.0)

    def test_batch_size_chunking(self):
        engine = DexScreenerDiscoveryEngine(workers=2)
        # Create 75 mock addresses for a chain
        addresses = [f"Addr_{i}" for i in range(75)]
        addresses_by_chain = {"solana": addresses}

        # Verify chunking matches MAX_BATCH_SIZE (30)
        unique_addrs = list(dict.fromkeys(addresses))
        chunks = [unique_addrs[i : i + MAX_BATCH_SIZE] for i in range(0, len(unique_addrs), MAX_BATCH_SIZE)]
        self.assertEqual(len(chunks), 3)
        self.assertEqual(len(chunks[0]), 30)
        self.assertEqual(len(chunks[1]), 30)
        self.assertEqual(len(chunks[2]), 15)


if __name__ == "__main__":
    unittest.main()
