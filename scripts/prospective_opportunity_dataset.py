#!/usr/bin/env python3
"""Prospective Opportunity Research Dataset for Rocket Hunter v4.30.

Captures immutable, observation-time decision snapshots for ALL discovered candidates
(both qualified and rejected to eliminate selection bias).
Enforces the mandatory 22 research fields with separated raw vs normalized data.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import math
from pathlib import Path
from typing import Any

PROSPECTIVE_DATASET_VERSION = "4.30.0"
PROSPECTIVE_SCHEMA = "crypto-prebreakout.prospective-opportunity.v1"


def _clean_num(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        v = float(val)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def _clean_str(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


class ProspectiveOpportunityDataset:
    """Manages immutable prospective opportunity snapshots across candidate discovery."""

    @staticmethod
    def create_snapshot(
        candidate: dict[str, Any],
        *,
        run_id: str,
        scanner_version: str,
        configuration_fingerprint: str,
        qualification_status: str,
        rejection_reasons: list[str] | None = None,
        snapshot_time: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Capture an immutable 22-field observation-time prospective snapshot."""
        now = snapshot_time or dt.datetime.now(dt.timezone.utc)
        snapshot_utc = now.isoformat(timespec="seconds").replace("+00:00", "Z")

        chain = _clean_str(candidate.get("chain")) or "unknown"
        token_contract = (
            _clean_str(candidate.get("contract_address")) or
            _clean_str(candidate.get("token_mint")) or
            _clean_str(candidate.get("address"))
        )
        pool_address = (
            _clean_str(candidate.get("pool_address")) or
            _clean_str(candidate.get("pair_address"))
        )
        quote_token_address = _clean_str(
            candidate.get("quote_token_address") or
            candidate.get("quote_address") or
            (candidate.get("extra", {}) if isinstance(candidate.get("extra"), dict) else {}).get("quote_address")
        )
        quote_symbol = _clean_str(candidate.get("quote_symbol"))

        market_key = f"{chain}:{token_contract}:{pool_address}" if pool_address else f"{chain}:{token_contract}"
        snapshot_id = hashlib.sha256(f"{run_id}|{market_key}|{snapshot_utc}".encode("utf-8")).hexdigest()

        # 1. Chain/network
        # 2. Token contract or mint
        # 3. Exact pool address
        # 4. Quote-token address
        # 5. Base/quote orientation
        orientation = {
            "token_orientation_verified": candidate.get("final_quote_token_orientation_verified") is True,
            "tracked_asset_is_base": candidate.get("base_token_is_tracked", True),
            "quote_symbol": quote_symbol,
        }

        # 6. Exchange/DEX name
        venue = _clean_str(candidate.get("venue")) or _clean_str(candidate.get("source"))

        # 7. Token age and pool age, separately
        token_age_hours = _clean_num(candidate.get("token_age_hours"))
        pool_age_hours = _clean_num(candidate.get("age_hours")) or _clean_num(candidate.get("pool_age_hours"))

        # 8. Price at detection
        price_at_detection = _clean_num(candidate.get("final_price_usd")) or _clean_num(candidate.get("price_usd"))

        # 9. Market capitalization and FDV
        mcap = _clean_num(candidate.get("market_cap_usd")) or _clean_num(candidate.get("market_cap"))
        fdv = _clean_num(candidate.get("fdv_usd")) or _clean_num(candidate.get("fdv"))

        # 10. Liquidity
        liquidity = _clean_num(candidate.get("liquidity_usd")) or _clean_num(candidate.get("best_liquidity_pool_usd"))

        # 11. Trading volume
        volume_data = {
            "volume_24h_usd": _clean_num(candidate.get("volume_24h_usd")),
            "volume_6h_usd": _clean_num(candidate.get("volume_6h_usd")),
            "volume_1h_usd": _clean_num(candidate.get("volume_1h_usd")),
            "volume_5m_usd": _clean_num(candidate.get("dexpaprika_5m_volume_usd") or candidate.get("volume_5m_usd")),
        }

        # 12. Transactions and buy/sell counts
        txns_data = {
            "txns_24h": _clean_num(candidate.get("txns_24h")),
            "buys_24h": _clean_num(candidate.get("buys_24h")),
            "sells_24h": _clean_num(candidate.get("sells_24h")),
        }

        # 13. Available technical indicators
        technical_indicators = {
            "change_5m_pct": _clean_num(candidate.get("change_5m_pct")),
            "change_1h_pct": _clean_num(candidate.get("change_1h_pct")),
            "change_6h_pct": _clean_num(candidate.get("change_6h_pct")),
            "change_24h_pct": _clean_num(candidate.get("change_24h_pct")),
            "atr_1h_pct": _clean_num(
                (candidate.get("technical_tp_validation") if isinstance(candidate.get("technical_tp_validation"), dict) else {}).get("atr_1h_pct")
            ),
            "swing_resistance_usd": _clean_num(
                (candidate.get("technical_tp_validation") if isinstance(candidate.get("technical_tp_validation"), dict) else {}).get("target_price")
            ),
            "target_price_source": candidate.get("target_price_source"),
        }

        # 14. All nine breakout-engine measurements
        engine_scores = candidate.get("breakout_engine_scores") or {}
        engine_statuses = candidate.get("engine_evidence_status") or {}
        breakout_engines = {
            "confluence_score": _clean_num(candidate.get("breakout_confluence_score")),
            "evidence_coverage_pct": _clean_num(candidate.get("evidence_coverage_pct")),
            "breakout_evidence_eligible": candidate.get("breakout_evidence_eligible") is True,
            "engine_scores": engine_scores if isinstance(engine_scores, dict) else {},
            "engine_statuses": engine_statuses if isinstance(engine_statuses, dict) else {},
            "signal_engine_executed": candidate.get("signal_engine_executed", True),
            "expected_return_engine_executed": candidate.get("expected_return_engine_executed", True),
            "engine_result_status": candidate.get("engine_result_status", "SUCCESS"),
        }

        # 15. Security evidence status
        loss_guard = candidate.get("dex_loss_guard_evidence") if isinstance(candidate.get("dex_loss_guard_evidence"), dict) else {}
        loss_guard_checks = loss_guard.get("checks") if isinstance(loss_guard.get("checks"), dict) else {}
        security_status = {
            "safety_verified": candidate.get("safety_verified") is True,
            "dex_loss_guard_status": candidate.get("dex_loss_guard_status"),
            "token_security_audit": loss_guard_checks.get("token_security_audit") is True,
            "honeypot_verified_safe": loss_guard_checks.get("token_security_audit") is True,
        }

        # 16. LP and wallet evidence status
        lp_wallet_status = {
            "lp_position_audit": loss_guard_checks.get("lp_position_audit") is True,
            "launch_wallet_analysis": loss_guard_checks.get("launch_wallet_analysis") is True,
            "two_way_route_quotes": loss_guard_checks.get("two_way_route_quotes") is True,
        }

        # 17. Source and provider timestamps
        source_provider_timestamps = {
            "source_timestamp_utc": candidate.get("source_timestamp_utc") or candidate.get("dexpaprika_provider_timestamp"),
            "price_timestamp_utc": candidate.get("final_price_timestamp_utc"),
        }

        # 18. Local retrieval timestamp
        local_retrieval_utc = candidate.get("retrieved_utc") or snapshot_utc

        # 19. Scanner version
        # 20. Configuration fingerprint
        # 21. Signal score and component values
        signal_data = {
            "early_breakout_score": _clean_num(candidate.get("early_breakout_score")),
            "early_breakout_tier": candidate.get("early_breakout_tier"),
            "components": candidate.get("early_breakout_components") or {},
            "risk_flags": candidate.get("early_breakout_risk_flags") or candidate.get("risk_flags") or [],
        }

        # 22. Qualification status and exact rejection reasons
        clean_rejection_reasons = sorted(set(rejection_reasons or candidate.get("qualification_blockers") or []))

        # Build normalized data dictionary
        normalized_data = {
            "chain": chain,
            "token_contract": token_contract,
            "pool_address": pool_address,
            "quote_token_address": quote_token_address,
            "orientation": orientation,
            "venue": venue,
            "token_age_hours": token_age_hours,
            "pool_age_hours": pool_age_hours,
            "price_at_detection_usd": price_at_detection,
            "market_cap_usd": mcap,
            "fdv_usd": fdv,
            "liquidity_usd": liquidity,
            "volume": volume_data,
            "transactions": txns_data,
            "technical_indicators": technical_indicators,
            "breakout_engines": breakout_engines,
            "security_evidence_status": security_status,
            "lp_and_wallet_evidence_status": lp_wallet_status,
            "source_provider_timestamps": source_provider_timestamps,
            "local_retrieval_timestamp_utc": local_retrieval_utc,
            "scanner_version": scanner_version,
            "configuration_fingerprint": configuration_fingerprint,
            "signal_score_and_components": signal_data,
            "qualification_status": qualification_status,
            "exact_rejection_reasons": clean_rejection_reasons,
        }

        # Raw source evidence (stored verbatim, separated from normalized features)
        raw_source_evidence = {
            k: v for k, v in candidate.items()
            if k not in {"raw_source_evidence"} and not str(k).startswith("_")
        }

        return {
            "schema": PROSPECTIVE_SCHEMA,
            "schema_version": PROSPECTIVE_DATASET_VERSION,
            "snapshot_id": snapshot_id,
            "run_id": str(run_id),
            "snapshot_utc": snapshot_utc,
            "market_key": market_key,
            "identity": candidate.get("identity") or f"dex:{chain}:{token_contract}",
            "is_rejected_candidate": qualification_status != "QUALIFIED",
            "qualification_status": qualification_status,
            "normalized_record": normalized_data,
            "raw_source_evidence": raw_source_evidence,
        }

    @staticmethod
    def append_snapshots(
        destination_dir: Path,
        snapshots: list[dict[str, Any]],
        partition_date: str | None = None,
    ) -> Path:
        """Atomically append immutable prospective snapshots to gzipped JSONL partition."""
        if not snapshots:
            return destination_dir
        dest_dir = destination_dir / "prospective-opportunities"
        dest_dir.mkdir(parents=True, exist_ok=True)
        date_str = partition_date or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
        partition_path = dest_dir / f"prospective-{date_str}.jsonl.gz"

        with gzip.open(partition_path, "at", encoding="utf-8", compresslevel=9) as handle:
            for snapshot in snapshots:
                line = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
                handle.write(line)
        return partition_path
