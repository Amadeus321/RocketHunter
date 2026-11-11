"""Canonical single-take-profit qualification shared by report gates."""
from __future__ import annotations

import math
from typing import Any

MIN_TP_UPSIDE_PCT = 40.0


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def canonical_tp_upside(reference_price: Any, take_profit: Any) -> float | None:
    """Return TP upside from one refreshed reference price, or None if invalid."""
    reference = number(reference_price)
    target = number(take_profit)
    if reference is None or target is None or reference <= 0 or target <= 0:
        return None
    # Collapse binary floating-point noise at threshold boundaries (for example,
    # 140 / 100 can otherwise evaluate as 39.99999999999999).
    return round((target / reference - 1.0) * 100.0, 10)


def passes_tp40(reference_price: Any, take_profit: Any) -> bool:
    upside = canonical_tp_upside(reference_price, take_profit)
    return upside is not None and upside >= MIN_TP_UPSIDE_PCT
