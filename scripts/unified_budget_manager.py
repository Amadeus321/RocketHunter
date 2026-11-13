#!/usr/bin/env python3
"""Unified 180-Minute Scanning Budget & Lifecycle Manager for Rocket Hunter.

Allocates and enforces time budgets across all scan phases:
- Phase 1: Preflight & Network Registries (5 min)
- Phase 2: Global Multi-Chain Discovery (100 min)
- Phase 3: Candidate Enrichment & Cross-Corroboration (20 min)
- Phase 4: Deep Verification & Safety Audits (30 min)
- Phase 5: Finalization, AURA Learning & Reports (15 min)
- Safety Reserve: Margin against process limits (10 min)
Total: 180 minutes hard ceiling.
Dynamically rebalances saved time forward into downstream verification.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

HARD_MAX_RUNTIME_MINUTES = 180.0
DEFAULT_LOCAL_TARGET_MINUTES = 170.0
SAFETY_RESERVE_MINUTES = 10.0

DEFAULT_PHASE_ALLOCATIONS = {
    "preflight_and_registries": 5.0,
    "global_multichain_discovery": 100.0,
    "candidate_enrichment": 20.0,
    "deep_verification_safety": 30.0,
    "finalization_and_reports": 15.0,
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class UnifiedBudgetManager:
    """Oversees runtime budget allocation, phase transitions, and deadline enforcement."""

    def __init__(self, max_runtime_minutes: float = DEFAULT_LOCAL_TARGET_MINUTES) -> None:
        self.total_budget_minutes = max(1.0, min(HARD_MAX_RUNTIME_MINUTES, float(max_runtime_minutes)))
        self.usable_budget_minutes = max(1.0, self.total_budget_minutes - SAFETY_RESERVE_MINUTES)

        self.start_monotonic = time.monotonic()
        self.start_utc = _utc_iso()
        self.hard_deadline = self.start_monotonic + (self.total_budget_minutes * 60.0)
        self.usable_deadline = self.start_monotonic + (self.usable_budget_minutes * 60.0)

        # Scale baseline phase allowances proportionally to usable budget
        scale = self.usable_budget_minutes / sum(DEFAULT_PHASE_ALLOCATIONS.values())
        self.phase_budgets: dict[str, float] = {
            phase: round(base * scale, 2) for phase, base in DEFAULT_PHASE_ALLOCATIONS.items()
        }

        self.current_phase: str | None = None
        self.current_phase_start: float | None = None
        self.completed_phases: dict[str, dict[str, Any]] = {}

    def start_phase(self, phase_name: str) -> float:
        """Mark the beginning of a phase and return its remaining allocated seconds.
        Dynamically rolls unspent budget from earlier completed phases forward into
        downstream phases while guaranteeing minimum reserve for remaining phases.
        """
        now = time.monotonic()
        if self.current_phase and self.current_phase_start is not None:
            self.end_phase(self.current_phase)

        self.current_phase = phase_name
        self.current_phase_start = now

        # Define canonical phases order to compute downstream required reserves
        phases_order = [
            "preflight_and_registries",
            "global_multichain_discovery",
            "candidate_enrichment",
            "deep_verification_safety",
            "finalization_and_reports",
        ]
        downstream_reserve = 0.0
        if phase_name in phases_order:
            idx = phases_order.index(phase_name)
            downstream_phases = phases_order[idx + 1:]
            downstream_reserve = sum(self.phase_budgets.get(p, 10.0) for p in downstream_phases) * 60.0

        remaining_whole_scan = max(0.0, self.usable_deadline - now)
        # Usable allowance for this phase is all remaining scan time minus downstream reserve
        max_possible_allowance = max(0.0, remaining_whole_scan - downstream_reserve)
        base_allowance = self.phase_budgets.get(phase_name, 10.0) * 60.0

        # Allow this phase to use its base budget plus saved/unspent time from prior phases
        allocated_seconds = max(base_allowance, max_possible_allowance)
        allocated_seconds = min(allocated_seconds, remaining_whole_scan)
        return max(1.0, allocated_seconds)

    def end_phase(self, phase_name: str) -> dict[str, Any]:
        """Record completed phase duration and roll unused budget forward."""
        now = time.monotonic()
        start = self.current_phase_start or self.start_monotonic
        duration_sec = round(now - start, 2)
        record = {
            "phase": phase_name,
            "duration_seconds": duration_sec,
            "duration_minutes": round(duration_sec / 60.0, 2),
            "completed_utc": _utc_iso(),
        }
        self.completed_phases[phase_name] = record
        if self.current_phase == phase_name:
            self.current_phase = None
            self.current_phase_start = None
        return record

    def remaining_seconds(self) -> float:
        """Return the remaining seconds before the whole-scan usable deadline."""
        return max(0.0, self.usable_deadline - time.monotonic())

    def is_expired(self) -> bool:
        """Check whether the usable scan budget has expired."""
        return time.monotonic() >= self.usable_deadline

    def is_within_budget(self) -> bool:
        """Check whether current execution is still within the usable scan budget."""
        return not self.is_expired()

    def get_summary(self) -> dict[str, Any]:
        """Return a point-in-time status report of budget usage across all phases."""
        now = time.monotonic()
        elapsed_sec = round(now - self.start_monotonic, 2)
        remaining_sec = max(0.0, self.hard_deadline - now)
        return {
            "total_budget_minutes": self.total_budget_minutes,
            "usable_budget_minutes": self.usable_budget_minutes,
            "safety_reserve_minutes": SAFETY_RESERVE_MINUTES,
            "elapsed_seconds": elapsed_sec,
            "elapsed_minutes": round(elapsed_sec / 60.0, 2),
            "remaining_seconds": round(remaining_sec, 2),
            "remaining_minutes": round(remaining_sec / 60.0, 2),
            "current_phase": self.current_phase,
            "phase_budgets_minutes": self.phase_budgets,
            "completed_phases": self.completed_phases,
            "is_within_budget": not self.is_expired(),
            "scan_started_utc": self.start_utc,
        }
