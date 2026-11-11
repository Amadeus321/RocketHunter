import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from tp_gate import canonical_tp_upside, passes_tp40


class CanonicalTakeProfitGateTests(unittest.TestCase):
    def test_threshold_boundaries(self):
        self.assertAlmostEqual(canonical_tp_upside(100, 139.99), 39.99)
        self.assertFalse(passes_tp40(100, 139.99))
        self.assertAlmostEqual(canonical_tp_upside(100, 140), 40.0)
        self.assertTrue(passes_tp40(100, 140))
        self.assertTrue(passes_tp40(100, 140.01))

    def test_missing_invalid_and_nonfinite_prices_fail_closed(self):
        for reference, target in ((None, 140), (0, 140), (-1, 140), (100, None),
                                  (100, 0), (100, float("nan")), (float("inf"), 140)):
            with self.subTest(reference=reference, target=target):
                self.assertIsNone(canonical_tp_upside(reference, target))
                self.assertFalse(passes_tp40(reference, target))

    def test_price_orientation_or_price_basis_is_not_guessed(self):
        # Upside is only arithmetic here; callers must supply their independently
        # verified tracked-token USD reference, never the pool's inverse quote.
        self.assertFalse(passes_tp40(None, 140))
        self.assertFalse(passes_tp40(100, 99))


if __name__ == "__main__":
    unittest.main()
