import unittest
from scripts.defillama_flow_engine import compute_ecosystem_flow_score


class DefiLlamaFlowEngineTests(unittest.TestCase):
    def test_high_volume_acceleration_boosts_flow_weight(self):
        score, weight = compute_ecosystem_flow_score(
            volume_24h=500_000_000.0,
            change_1d_pct=65.0,  # > +50% volume surge
            change_7d_pct=120.0,
            tvl_usd=2_000_000_000.0,
            tvl_1d_change_pct=5.5,  # positive TVL inflow
        )
        self.assertGreater(score, 2.0)
        self.assertGreater(weight, 1.8)
        self.assertLessEqual(weight, 2.5)

    def test_contracting_volume_lowers_flow_weight(self):
        score, weight = compute_ecosystem_flow_score(
            volume_24h=50_000.0,
            change_1d_pct=-35.0,
            change_7d_pct=-40.0,
            tvl_usd=100_000.0,
            tvl_1d_change_pct=-8.0,
        )
        self.assertLess(score, 1.0)
        self.assertLessEqual(weight, 0.9)
        self.assertGreaterEqual(weight, 0.6)

    def test_neutral_baseline(self):
        score, weight = compute_ecosystem_flow_score(
            volume_24h=50_000_000.0,
            change_1d_pct=0.0,
            change_7d_pct=0.0,
            tvl_usd=500_000_000.0,
            tvl_1d_change_pct=0.0,
        )
        self.assertAlmostEqual(score, 1.0, places=1)
        self.assertAlmostEqual(weight, 1.15, places=1)


if __name__ == "__main__":
    unittest.main()
