import time
import unittest
from scripts.unified_budget_manager import (
    HARD_MAX_RUNTIME_MINUTES,
    SAFETY_RESERVE_MINUTES,
    UnifiedBudgetManager,
)


class UnifiedBudgetManagerTests(unittest.TestCase):
    def test_budget_initialization_and_ceiling(self):
        # Even if 250 minutes requested, it must be clamped to 180 hard ceiling
        mgr = UnifiedBudgetManager(max_runtime_minutes=250.0)
        self.assertEqual(mgr.total_budget_minutes, HARD_MAX_RUNTIME_MINUTES)
        self.assertEqual(mgr.usable_budget_minutes, HARD_MAX_RUNTIME_MINUTES - SAFETY_RESERVE_MINUTES)
        self.assertTrue(mgr.is_within_budget())
        self.assertFalse(mgr.is_expired())

    def test_phase_transitions_and_tracking(self):
        mgr = UnifiedBudgetManager(max_runtime_minutes=170.0)

        # Start phase 1
        sec1 = mgr.start_phase("preflight_and_registries")
        self.assertEqual(mgr.current_phase, "preflight_and_registries")
        self.assertGreater(sec1, 60.0)

        time.sleep(0.05)

        # Transition to phase 2
        sec2 = mgr.start_phase("global_multichain_discovery")
        self.assertEqual(mgr.current_phase, "global_multichain_discovery")
        self.assertIn("preflight_and_registries", mgr.completed_phases)
        self.assertGreater(mgr.completed_phases["preflight_and_registries"]["duration_seconds"], 0.0)
        self.assertGreater(sec2, 1000.0)

        summary = mgr.get_summary()
        self.assertEqual(summary["total_budget_minutes"], 170.0)
        self.assertIn("preflight_and_registries", summary["completed_phases"])
        self.assertEqual(summary["current_phase"], "global_multichain_discovery")
        self.assertTrue(summary["is_within_budget"])


if __name__ == "__main__":
    unittest.main()
