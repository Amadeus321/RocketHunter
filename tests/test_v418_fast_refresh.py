import unittest
from scripts.v418_fast_refresh import parse_gecko, parse_dexscreener, parse_coinpaprika


class FastRefreshParserTests(unittest.TestCase):
    def test_gecko_parser_uses_chain_contract_pool_and_timestamps(self):
        payload = {
            "data": [{
                "id": "base_0xpool", "attributes": {
                    "address": "0xpool", "name": "ABC/WETH", "base_token_price_usd": "1.25",
                    "price_change_percentage": {"h1": "5", "h6": "20", "h24": "75"},
                    "volume_usd": {"h1": "100", "h6": "500", "h24": "1000"},
                    "reserve_in_usd": "25000", "pool_created_at": "2026-11-04T00:00:00Z"
                }, "relationships": {"base_token": {"data": {"id": "base_0xtoken"}}}
            }],
            "included": [{"id": "base_0xtoken", "attributes": {"address": "0xtoken", "name": "Alpha", "symbol": "ABC"}}]
        }
        rows = parse_gecko(payload, "gecko_global_trending_1h", "2026-11-05T00:00:00Z", "https://api.example")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["chain"], "base")
        self.assertEqual(rows[0]["contract_address"], "0xtoken")
        self.assertEqual(rows[0]["pool_address"], "0xpool")
        self.assertEqual(rows[0]["change_24h_pct"], 75)
        self.assertEqual(rows[0]["liquidity_usd"], 25000)

    def test_dexscreener_boosts_are_promotional_not_demand(self):
        rows = parse_dexscreener([{"chainId": "solana", "tokenAddress": "MintABC", "amount": 10, "totalAmount": 20}], "dexscreener_latest_boosts", "2026-11-05T00:00:00Z")
        self.assertEqual(len(rows), 1)
        self.assertIn("PROMOTIONAL_FEED_LEAD_NOT_INDEPENDENT_DEMAND", rows[0]["risk_flags"])
        self.assertIsNone(rows[0]["price_usd"])

    def test_coinpaprika_price_and_change_fields(self):
        rows = parse_coinpaprika([{"id": "coin-x", "name": "Coin X", "symbol": "X", "rank": 22, "quotes": {"USD": {"price": 2, "percent_change_24h": 15, "volume_24h": 1000, "market_cap": 20000}}}], "2026-11-05T00:00:00Z")
        self.assertEqual(rows[0]["identity"], "coinpaprika:coin-x")
        self.assertEqual(rows[0]["change_24h_pct"], 15)
        self.assertEqual(rows[0]["price_usd"], 2)


if __name__ == "__main__":
    unittest.main()
