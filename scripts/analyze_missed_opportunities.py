#!/usr/bin/env python3
"""Retrospective missed-opportunity analysis engine.

Identifies historical candidates that subsequently gained +40%, +60%, +100%, or +300%
at the 24-hour horizon mark and investigates:
1. Did Rocket Hunter discover them?
2. At what exact detection time?
3. What detection score and discovery lanes were assigned?
4. Why were they not qualified or prioritized into the actionable watchlist?
5. What evidence or criteria separated them from actionable qualification?

Produces content-addressed, read-only analytical artifacts in research-analysis.
Does not alter scanner memory or production ranking gates.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEMORY = ROOT / "rocket-hunter-data" / "scanner-memory"
DEFAULT_OUTPUT_ROOT = ROOT / "rocket-hunter-data" / "research-analysis"
DEFAULT_EXPORTS = ROOT / "rocket-hunter-data" / "research-exports"
DEFAULT_RUNS = ROOT / "rocket-runs"
ANALYSIS_VERSION = "rocket-hunter-missed-opportunities.v1"
MANAGED_BY = "rocket-hunter-analyze-missed-opportunities"
HORIZON_HOURS = 24
MAX_OBSERVATION_LAG_HOURS = 6.0
OBSERVED_STATUS = "OBSERVED_MARK_NOT_EXECUTABLE_FILL"
TIERS_PCT = (40.0, 60.0, 100.0, 300.0)


class AnalysisError(RuntimeError):
    """Raised when source data is unsafe, missing, or altered during analysis."""


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _reject_json_constant(value: str):
    raise ValueError(f"non-standard JSON constant: {value}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _source_partitions(memory_dir: Path, dataset: str) -> list[Path]:
    directory = memory_dir / dataset
    if not directory.is_dir():
        raise AnalysisError(f"required source directory is missing: {directory}")
    paths = sorted(directory.glob("*.jsonl.gz"), key=lambda path: path.name)
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise AnalysisError(f"source partitions must be regular files: {directory}")
    return paths


def _source_snapshot(memory_dir: Path):
    partitions = {name: _source_partitions(memory_dir, name) for name in ("forecasts", "outcomes")}
    stats = {path: path.stat() for paths in partitions.values() for path in paths}
    sources = []
    digest = hashlib.sha256()
    for dataset, paths in partitions.items():
        for path in paths:
            item = {
                "dataset": dataset,
                "partition": path.name,
                "sha256": _sha256_file(path),
                "compressed_size_bytes": stats[path].st_size,
            }
            sources.append(item)
            digest.update(_json_text(item).encode("utf-8"))
    return partitions, stats, sources, digest.hexdigest()


def _verify_source_snapshot(memory_dir: Path, partitions, stats, sources) -> None:
    current_partitions = {
        name: _source_partitions(memory_dir, name) for name in ("forecasts", "outcomes")
    }
    if current_partitions != partitions:
        raise AnalysisError("source partition list changed during analysis")
    expected_hashes = {(item["dataset"], item["partition"]): item["sha256"] for item in sources}
    for path, before in stats.items():
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ) or _sha256_file(path) != expected_hashes[(path.parent.name, path.name)]:
            raise AnalysisError(f"source partition changed during analysis: {path.name}")


def _load_watchlist_identities(memory_dir: Path) -> set[str]:
    """Collect identities that reached the active or completed 24h watchlists."""
    identities = set()
    active_path = memory_dir / "active-24h-watchlist.json"
    if active_path.is_file():
        try:
            data = json.loads(active_path.read_text(encoding="utf-8"))
            for pos in data.get("positions", []):
                ident = pos.get("identity") or pos.get("pool_address")
                if isinstance(ident, str) and ident.strip():
                    identities.add(ident.strip().lower())
        except Exception:
            pass

    completed_path = memory_dir / "completed-24h-watchlist.json"
    if completed_path.is_file():
        try:
            data = json.loads(completed_path.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else data.get("positions", [])
            for pos in items:
                ident = pos.get("identity") or pos.get("pool_address")
                if isinstance(ident, str) and ident.strip():
                    identities.add(ident.strip().lower())
        except Exception:
            pass
    return identities


def _classify_miss_reason(
    forecast: dict[str, Any],
    is_actionable: bool,
) -> tuple[str, str]:
    if is_actionable:
        return "ACTIONABLE_QUALIFIED", "Captured in active/completed watchlist"

    market_type = str(forecast.get("market_type") or (forecast.get("feature_snapshot") or {}).get("market_type") or "").lower()
    identity = str(forecast.get("identity") or "").strip().lower()
    feature = forecast.get("feature_snapshot") or {}
    pool = str(feature.get("pool_address") or "").strip()
    contract = str(feature.get("contract_address") or feature.get("token_mint") or "").strip()
    raw_score = _finite_number(forecast.get("raw_score"))

    if market_type == "cex_spot" or identity.startswith("cex:"):
        return (
            "CEX_SPOT_RESEARCH_ONLY",
            "CEX spot asset; Rocket Hunter requires DEX-first verified pools for actionable BUY qualification",
        )

    if not pool or not contract:
        return (
            "INSUFFICIENT_EXACT_DEX_IDENTITY",
            "Missing exact DEX pool address or token contract mint at forecast detection",
        )

    if raw_score is not None and raw_score < 40.0:
        return (
            "LOW_HEURISTIC_SCORE",
            f"Model raw heuristic score ({raw_score:.1f}) was below the qualification threshold",
        )

    return (
        "DISCOVERY_FILTERED_BEFORE_QUALIFICATION",
        "Discovered in DEX feed but filtered by liquidity, security, quote, or age-guard requirements",
    )


def analyze_missed_opportunities(
    memory_dir: Path = DEFAULT_MEMORY,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    memory_dir = memory_dir.resolve()
    output_root = output_root.resolve()
    if not memory_dir.is_dir():
        raise AnalysisError(f"memory directory does not exist: {memory_dir}")

    partitions, stats, sources, snapshot_id = _source_snapshot(memory_dir)
    watchlist_identities = _load_watchlist_identities(memory_dir)

    # 1. Load forecasts
    forecasts: dict[str, dict[str, Any]] = {}
    for path in partitions["forecasts"]:
        try:
            with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
                for line in stream:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line, parse_constant=_reject_json_constant)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(record, dict) and record.get("forecast_id"):
                        forecasts[record["forecast_id"]] = record
        except (OSError, EOFError, UnicodeDecodeError, zlib.error):
            continue

    # 2. Load 24h outcomes and identify bounded-lag movers >= 40%
    bounded_lag_outcomes_count = 0
    movers: list[dict[str, Any]] = []

    for path in partitions["outcomes"]:
        try:
            with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
                for line in stream:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line, parse_constant=_reject_json_constant)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    if record.get("horizon_hours") != HORIZON_HOURS:
                        continue
                    if record.get("status") != OBSERVED_STATUS:
                        continue

                    forecast_id = record.get("forecast_id")
                    if not forecast_id or forecast_id not in forecasts:
                        continue

                    forecast = forecasts[forecast_id]
                    forecast_time = _parse_utc(forecast.get("snapshot_utc"))
                    observed_time = _parse_utc(record.get("observed_utc"))
                    if forecast_time is None or observed_time is None:
                        continue

                    target_time = forecast_time + dt.timedelta(hours=HORIZON_HOURS)
                    lag_hours = (observed_time - target_time).total_seconds() / 3600.0
                    if lag_hours < 0 or lag_hours > MAX_OBSERVATION_LAG_HOURS:
                        continue

                    bounded_lag_outcomes_count += 1
                    mark_return = _finite_number(record.get("mark_return_pct"))
                    if mark_return is None or mark_return < 40.0:
                        continue

                    # Classify
                    identity_key = str(forecast.get("identity") or record.get("market_key") or "").strip().lower()
                    pool_key = str((forecast.get("feature_snapshot") or {}).get("pool_address") or "").strip().lower()
                    is_actionable = (identity_key in watchlist_identities) or (pool_key and pool_key in watchlist_identities)

                    category, reason = _classify_miss_reason(forecast, is_actionable)
                    feature = forecast.get("feature_snapshot") or {}

                    movers.append({
                        "forecast_id": forecast_id,
                        "market_key": record.get("market_key") or forecast.get("market_key"),
                        "identity": forecast.get("identity"),
                        "asset": feature.get("asset") or feature.get("symbol") or record.get("market_key"),
                        "symbol": feature.get("symbol") or feature.get("asset"),
                        "chain": feature.get("chain"),
                        "venue": feature.get("venue"),
                        "market_type": feature.get("market_type") or forecast.get("market_type"),
                        "detection_utc": forecast.get("snapshot_utc"),
                        "observed_utc": record.get("observed_utc"),
                        "base_price_usd": record.get("base_price_usd"),
                        "observed_price_usd": record.get("observed_price_usd"),
                        "mark_return_pct": mark_return,
                        "raw_score": _finite_number(forecast.get("raw_score")),
                        "discovery_lanes": forecast.get("discovery_lanes") or [],
                        "model_id": forecast.get("model_id"),
                        "is_actionable": is_actionable,
                        "miss_category": category,
                        "miss_reason": reason,
                    })
        except (OSError, EOFError, UnicodeDecodeError, zlib.error):
            continue

    _verify_source_snapshot(memory_dir, partitions, stats, sources)

    # Sort movers by return percentage descending
    movers.sort(key=lambda m: m["mark_return_pct"], reverse=True)

    # Compute tier counts
    tier_counts = {}
    for tier in TIERS_PCT:
        tier_movers = [m for m in movers if m["mark_return_pct"] >= tier]
        actionable_in_tier = sum(1 for m in tier_movers if m["is_actionable"])
        missed_in_tier = len(tier_movers) - actionable_in_tier
        tier_counts[f"+{int(tier)}%"] = {
            "total_movers": len(tier_movers),
            "actionable_captured": actionable_in_tier,
            "missed": missed_in_tier,
            "capture_rate": (actionable_in_tier / len(tier_movers)) if tier_movers else 0.0,
            "miss_rate": (missed_in_tier / len(tier_movers)) if tier_movers else 0.0,
        }

    # Reason breakdown for missed opportunities
    missed_movers = [m for m in movers if not m["is_actionable"]]
    category_counts = collections.Counter(m["miss_category"] for m in missed_movers)
    category_breakdown = {k: {"count": v, "pct": round(v / len(missed_movers) * 100, 2)} for k, v in category_counts.most_common()} if missed_movers else {}

    # Score stats of missed movers
    scores = [m["raw_score"] for m in missed_movers if m["raw_score"] is not None]
    score_stats = {
        "count_scored": len(scores),
        "count_unscored": len(missed_movers) - len(scores),
        "min": min(scores) if scores else None,
        "max": max(scores) if scores else None,
        "median": round(float(sorted(scores)[len(scores) // 2]), 2) if scores else None,
        "mean": round(float(sum(scores) / len(scores)), 2) if scores else None,
    }

    report = {
        "analysis_version": ANALYSIS_VERSION,
        "managed_by": MANAGED_BY,
        "horizon_hours": HORIZON_HOURS,
        "source_snapshot_id": snapshot_id,
        "forecasts_loaded": len(forecasts),
        "bounded_lag_24h_outcomes": bounded_lag_outcomes_count,
        "total_movers_40pct_plus": len(movers),
        "actionable_captured_count": sum(1 for m in movers if m["is_actionable"]),
        "missed_count": len(missed_movers),
        "overall_miss_rate": round(len(missed_movers) / len(movers), 4) if movers else 0.0,
        "tier_summary": tier_counts,
        "miss_category_breakdown": category_breakdown,
        "missed_score_statistics": score_stats,
        "top_missed_opportunities": movers[:25],
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    # Render Markdown report
    md_lines = [
        "# Rocket Hunter Missed-Opportunity Analysis",
        "",
        f"- Analysis version: `{ANALYSIS_VERSION}`",
        f"- Source snapshot: `{snapshot_id}`",
        f"- Horizon: `{HORIZON_HOURS} hours` (bounded lag $\\le {MAX_OBSERVATION_LAG_HOURS}$ hours)",
        f"- Bounded-lag 24h outcomes examined: **{bounded_lag_outcomes_count:,}**",
        f"- Total observed gainers $\\ge +40\\%$: **{len(movers):,}**",
        f"- Missed opportunities: **{len(missed_movers):,}** ({report['overall_miss_rate'] * 100:.2f}%)",
        "",
        "## Gainer Tiers & Capture Rate",
        "",
        "| Gain Tier | Total Movers | Actionable Captured | Missed | Capture Rate | Miss Rate |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for tier_label, data in tier_counts.items():
        md_lines.append(
            f"| **{tier_label}** | {data['total_movers']:,} | {data['actionable_captured']} | {data['missed']:,} | {data['capture_rate'] * 100:.2f}% | {data['miss_rate'] * 100:.2f}% |"
        )

    md_lines.extend([
        "",
        "## Why Promising Opportunities Were Missed",
        "",
        "| Miss Category | Count | Percentage | Description |",
        "|---|---:|---:|---|",
    ])
    category_descriptions = {
        "CEX_SPOT_RESEARCH_ONLY": "CEX spot pair; scanner requires verified DEX pool for actionable trading",
        "INSUFFICIENT_EXACT_DEX_IDENTITY": "Missing exact pool address or token contract address at forecast time",
        "LOW_HEURISTIC_SCORE": "Candidate score was below qualification cutoff (< 40.0)",
        "DISCOVERY_FILTERED_BEFORE_QUALIFICATION": "Filtered by liquidity floor (< $100k), security audit, or quote check",
    }
    for cat, info in category_breakdown.items():
        desc = category_descriptions.get(cat, "Filtered during scanning")
        md_lines.append(f"| `{cat}` | {info['count']:,} | {info['pct']}% | {desc} |")

    md_lines.extend([
        "",
        "## Top Missed Opportunities (Highest 24h Observed Marks)",
        "",
        "| # | Symbol | Chain / Venue | Detection Time (UTC) | Base Price | 24h Price | 24h Gain % | Score | Primary Reason Missed |",
        "|---|---|---|---|---:|---:|---:|---:|---|",
    ])
    for idx, m in enumerate(movers[:20], 1):
        chain_venue = m.get("chain") or m.get("venue") or "unknown"
        symbol = m.get("symbol") or m.get("asset") or "unknown"
        score_str = f"{m['raw_score']:.1f}" if m.get("raw_score") is not None else "N/A"
        base_p = f"${m['base_price_usd']:.6g}" if m.get("base_price_usd") else "N/A"
        obs_p = f"${m['observed_price_usd']:.6g}" if m.get("observed_price_usd") else "N/A"
        md_lines.append(
            f"| {idx} | **{symbol}** | `{chain_venue}` | `{m['detection_utc']}` | {base_p} | {obs_p} | **+{m['mark_return_pct']:.1f}%** | {score_str} | `{m['miss_category']}` |"
        )

    md_text = "\n".join(md_lines) + "\n"

    # Write output to content-addressed directory
    output_dir = output_root / f"missed-opportunities-v1-{snapshot_id[:24]}"
    temp_dir = Path(tempfile.mkdtemp(prefix="missed-analysis-", dir=output_root))
    try:
        report_file = temp_dir / "report.json"
        md_file = temp_dir / "report.md"
        manifest_file = temp_dir / "manifest.json"

        report_file.write_text(_json_text(report), encoding="utf-8")
        md_file.write_text(md_text, encoding="utf-8")

        manifest = {
            "analysis_version": ANALYSIS_VERSION,
            "managed_by": MANAGED_BY,
            "source_snapshot_id": snapshot_id,
            "horizon_hours": HORIZON_HOURS,
            "output_files": ["report.json", "report.md"],
            "sha256": {
                "report.json": _sha256_file(report_file),
                "report.md": _sha256_file(md_file),
            },
            "generated_utc": report["generated_utc"],
        }
        manifest_file.write_text(_json_text(manifest), encoding="utf-8")

        if output_dir.exists():
            import shutil
            shutil.rmtree(output_dir)
        temp_dir.rename(output_dir)
    finally:
        if temp_dir.exists():
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze missed opportunities in Rocket Hunter historical memory.")
    parser.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY, help="scanner memory directory")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT, help="analysis output root")
    args = parser.parse_args()

    try:
        result = analyze_missed_opportunities(args.memory_dir, args.output_root)
        print(f"Missed-opportunity analysis complete: {result['total_movers_40pct_plus']} gainers analyzed, {result['missed_count']} missed.")
        return 0
    except Exception as exc:
        print(f"Error analyzing missed opportunities: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
