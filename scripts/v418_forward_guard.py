#!/usr/bin/env python3
"""Apply v4.18 pool-age guard to the legacy forward-24h report.

The core v4.15.3 forecast is created before the v4.18 quality overlay. This
post-overlay guard reconciles its candidate identities with the already
age-classified discovery rows, keeping young/unknown-age pools visible while
preventing them from being presented as comparable 24-hour forecast leaders.
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any

from scripts.v418_pipeline import finite_number, is_dex_pool, market_key

GUARD_VERSION = "4.18.1"
VALID_AGE_STATUS = "SOURCE_REPORTED_24H_POOL_AGE_GE_24H"
AGE_FIELDS = (
    "age_hours", "age_hours_v418", "pool_created_at", "return_window_status",
    "source_reported_change_24h_pct", "change_24h_pct", "fingerprint_cluster_id",
    "fingerprint_cluster_size", "risk_flags", "quality_gate", "source", "source_url",
)


def _dex_pair_key(row: dict[str, Any]) -> str | None:
    if not is_dex_pool(row):
        return None
    identity = str(row.get("identity") or "").strip().lower()
    pool = str(row.get("pool_address") or row.get("pair_address") or row.get("gecko_pool_id") or "").strip().lower()
    if identity and pool:
        return f"dexpair|{identity}|{pool}"
    return None


def _legacy_market_key(row: dict[str, Any]) -> str | None:
    """Read pre-v2 memory keys so existing forecast rows still get corrected."""
    identity = str(row.get("identity") or "").strip().lower()
    pool = str(row.get("pool_address") or row.get("pair_address") or row.get("gecko_pool_id") or "").strip().lower()
    contract = str(row.get("contract_address") or row.get("address") or "").strip().lower()
    kind = str(row.get("market_type") or row.get("type") or "unknown").strip().lower()
    provider = str(row.get("venue") or (row.get("source") if kind in {"cex_spot", "cex_perp", "spot", "perpetual"} else "") or "").strip().lower()
    quote = str(row.get("quote_symbol") or "").strip().lower()
    if identity and (pool or contract or provider):
        return "|".join((identity, pool, provider, quote))
    if row.get("chain") and contract:
        return "|".join((str(row.get("chain")).strip().lower(), contract, pool, provider, quote, kind))
    return None


def _build_index(main: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index exact and canonical DEX pool keys; keep strongest age evidence."""
    index: dict[str, dict[str, Any]] = {}
    preferred = (
        "new_launch_discovery_board_v418", "new_pools_under_24h",
        "comparable_24h_gainers_v418", "comparable_24h_losers_v418",
        "top_24h_gainers", "top_24h_losers", "top_24h_movers",
        "new_launch_movers", "extreme_movers_1000pct_plus",
        "extreme_movers_400pct_plus", "extreme_movers_100pct_plus",
        "high_risk_moonshot_scouts", "execution_candidate_shortlist",
        "dex_discovery_leads", "web3_trending_leads",
        "reported_top_24h_gainers_raw", "reported_top_24h_losers_raw",
        "reported_top_24h_movers_raw",
    )
    for lane in preferred:
        rows = main.get(lane)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = market_key(row)
            if not key:
                continue
            keys = [key]
            pair_key = _dex_pair_key(row)
            if pair_key:
                keys.append(pair_key)
            for lookup_key in keys:
                existing = index.get(lookup_key)
                if existing is None:
                    index[lookup_key] = dict(row)
                    continue
                # Merge market metadata, but never let a duplicate row with an
                # unknown age overwrite a known pool age. If sources disagree,
                # take the youngest observed age (conservative for 24h eligibility).
                old_age = finite_number(existing.get("age_hours_v418"))
                if old_age is None:
                    old_age = finite_number(existing.get("age_hours"))
                new_age = finite_number(row.get("age_hours_v418"))
                if new_age is None:
                    new_age = finite_number(row.get("age_hours"))
                if new_age is not None and (old_age is None or new_age < old_age):
                    for field in AGE_FIELDS:
                        if field in row and row[field] is not None:
                            existing[field] = row[field]
                else:
                    for field, value in row.items():
                        if existing.get(field) is None and value is not None:
                            existing[field] = value
                # A canonical DEX pool key is shared by duplicate lane copies.
                if lookup_key.startswith("dexpair|"):
                    chosen_age = finite_number(existing.get("age_hours_v418"))
                    if chosen_age is None:
                        chosen_age = finite_number(existing.get("age_hours"))
                    if chosen_age is None:
                        existing["return_window_status"] = "UNKNOWN_POOL_AGE_NOT_COMPARABLE"
                    elif chosen_age < 24.0:
                        existing["return_window_status"] = "INSUFFICIENT_24H_HISTORY"
                        existing["age_hours_v418"] = round(chosen_age, 4)
                    else:
                        existing["return_window_status"] = VALID_AGE_STATUS
                        existing["age_hours_v418"] = round(chosen_age, 4)
    return index


def _annotate_candidate(candidate: dict[str, Any], index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result = dict(candidate)
    key = market_key(candidate)
    pair_key = _dex_pair_key(candidate)
    match = (index.get(pair_key) if pair_key else None) or (index.get(key) if key else None)
    if match:
        for field in AGE_FIELDS:
            if field in match and match[field] is not None:
                if field == "risk_flags" and isinstance(match[field], list):
                    result[field] = list(dict.fromkeys(list(result.get(field) or []) + match[field]))
                else:
                    result[field] = match[field]
    if not (is_dex_pool(result) or (match and is_dex_pool(match))):
        result["return_24h_comparable"] = True
        result["forecast_eligibility_status"] = "NOT_A_DEX_POOL_AGE_CHECK_NOT_APPLICABLE"
        return result

    status = result.get("return_window_status")
    age = finite_number(result.get("age_hours_v418"))
    if age is None:
        age = finite_number(result.get("age_hours"))
    if status is None:
        status = VALID_AGE_STATUS if age is not None and age >= 24.0 else "UNKNOWN_POOL_AGE_NOT_COMPARABLE"
        result["return_window_status"] = status
    comparable = status == VALID_AGE_STATUS and age is not None and age >= 24.0
    result["return_24h_comparable"] = comparable
    result["age_qualified_24h_forecast"] = comparable

    if comparable:
        result["forecast_eligibility_status"] = "AGE_QUALIFIED_POOL_HISTORY"
        return result

    returns = result.get("returns_pct") if isinstance(result.get("returns_pct"), dict) else {}
    raw_return = finite_number(returns.get("24h"))
    if raw_return is None:
        raw_return = finite_number(result.get("source_reported_change_24h_pct"))
    if raw_return is not None:
        result["source_reported_return_24h_pct"] = raw_return
    result["returns_pct"] = {**returns, "24h": None}

    # Preserve the legacy score and component notes for audit, but do not expose
    # them as a valid 24h rank: the legacy score may have used the unqualified move.
    score = finite_number(result.get("forecast_score"))
    if score is not None:
        result["source_forecast_score_pre_age_guard"] = score
    if isinstance(result.get("factor_details"), list):
        result["source_factor_details_pre_age_guard"] = result["factor_details"]
    result["forecast_score"] = None
    result["forecast_rank"] = None
    result["factor_details"] = []
    result["forecast_disposition"] = "DISCOVERY_ONLY_NOT_24H_AGE_QUALIFIED"
    result["forecast_eligibility_status"] = status
    result["relative_confidence"] = "NOT RANKED — 24H pool-age qualification unavailable"
    result["entry_decision"] = "DISCOVERY ONLY — 24H pool-age qualification unavailable"
    note = str(result.get("note") or "").strip()
    suffix = "Source-reported 24h return and pre-guard score are retained for audit only; they are not used as a comparable 24h forecast rank."
    if suffix not in note:
        result["note"] = (note + " " + suffix).strip()
    flags = list(result.get("risk_flags") or [])
    flag = "24H_RETURN_NOT_COMPARABLE_POOL_AGE_UNDER_24H_OR_UNKNOWN"
    if flag not in flags:
        flags.append(flag)
    result["risk_flags"] = flags
    return result


def _format_pct(value: Any) -> str:
    n = finite_number(value)
    return "—" if n is None else f"{n:+.2f}%"


def _render_forward_markdown(forward: dict[str, Any]) -> str:
    candidates = forward.get("candidates") if isinstance(forward.get("candidates"), list) else []
    eligible = [c for c in candidates if isinstance(c, dict) and c.get("return_24h_comparable") is True]
    discoveries = forward.get("new_launch_discovery_leads_v418") if isinstance(forward.get("new_launch_discovery_leads_v418"), list) else []
    leader = forward.get("forecast_leader") if isinstance(forward.get("forecast_leader"), dict) else None
    lines = ["### Forward 24h Top Pick", ""]
    if leader:
        price = finite_number(leader.get("price"))
        trade_plan = leader.get("trade_plan") if isinstance(leader.get("trade_plan"), dict) else {}
        tp = finite_number(leader.get("target_price"))
        if tp is None:
            tp = finite_number(trade_plan.get("single_take_profit"))
        lines.extend([
            f"**{leader.get('asset') or leader.get('pair') or 'Unnamed'}** — {leader.get('entry_decision') or 'AGE-QUALIFIED DISCOVERY LEAD'}",
            "",
            "| Field | Value |", "|---|---|",
            f"| Current DEX spot reference (USD) | {('$' + format(price, '.12g')) if price is not None else '—'} |",
            f"| Price source timestamp (UTC) | {leader.get('data_timestamp_utc') or '—'} |",
            f"| Pair | {leader.get('pair') or '—'} |",
            f"| Venue / chain | {leader.get('venue') or '—'} / {leader.get('chain') or '—'} |",
            f"| Pool age (hours) | {leader.get('age_hours_v418', leader.get('age_hours', '—'))} |",
            f"| 24h history status | {leader.get('return_window_status') or '—'} |",
            f"| Pair address | `{leader.get('pair_address') or leader.get('pool_address') or '—'}` |",
            f"| Token contract / mint | `{leader.get('contract_address') or '—'}` |",
            f"| Pair page | {leader.get('pair_url') or '—'} |",
            f"| Single take-profit reference | {('$' + format(tp, '.12g')) if tp is not None else '—'} |",
            f"| Forecast score | {leader.get('forecast_score') if leader.get('forecast_score') is not None else '—'} / 100 (heuristic, not probability) |",
            f"| Evidence coverage | {leader.get('evidence_coverage_pct', '—')}% |",
            f"| Execution route verified | {leader.get('executable_route_verified', False)} |",
            f"| Safety verified | {leader.get('safety_verified', False)} |",
            "",
            "**Verification gaps:**",
        ])
        for item in leader.get("entry_blockers") or []:
            lines.append(f"- {item}")
        factors = leader.get("factor_details") if isinstance(leader.get("factor_details"), list) else []
        if factors:
            lines.extend(["", "**Rank factors (heuristic, not probability):**"])
            for factor in factors:
                lines.append(f"- {factor.get('factor')}: {factor.get('note') or 'no explanation supplied'}")
        risks = leader.get("risk_flags") if isinstance(leader.get("risk_flags"), list) else []
        if risks:
            lines.extend(["", "**Risk flags:**"])
            for risk in risks:
                lines.append(f"- {risk}")
        lines.append("")
    else:
        lines.extend([
            "**No age-qualified forward candidate in this sampled forecast cohort.**",
            "This does not mean there are no market opportunities. New/unknown-age pools remain visible in the discovery-only board below; they are not assigned a comparable 24h rank until the pool has at least 24 hours of observable age.",
            "",
        ])

    lines.extend([
        "### Forward-24h ranking (age-qualified sampled candidates)", "",
        "| Rank | Asset | Chain | Reported 24h move | Pool age (h) | Score | Status |", "|---:|---|---|---:|---:|---:|---|",
    ])
    for c in eligible:
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            c.get("forecast_rank") or "—", c.get("asset") or c.get("pair") or "—",
            c.get("chain") or "—", _format_pct((c.get("returns_pct") or {}).get("24h")),
            c.get("age_hours_v418", c.get("age_hours", "—")),
            c.get("forecast_score") if c.get("forecast_score") is not None else "—",
            c.get("entry_decision") or "AGE-QUALIFIED DISCOVERY LEAD"))
    if not eligible:
        lines.append("| — | No age-qualified candidates | — | — | — | — | Discovery leads retained below |")

    lines.extend(["", "### New-launch discovery leads (not comparable 24h forecasts)", ""])
    if discoveries:
        lines.extend([
            "The source-reported 24h move is preserved for audit where present, but is not treated as a completed 24h return or used in this ranking.",
            "",
            "| Asset | Chain | Pool age (h) | Source-reported 24h field | Disposition |", "|---|---|---:|---:|---|",
        ])
        for c in discoveries[:50]:
            lines.append("| {} | {} | {} | {} | {} |".format(
                c.get("asset") or c.get("pair") or "—", c.get("chain") or "—",
                c.get("age_hours_v418", c.get("age_hours", "unknown")),
                _format_pct(c.get("source_reported_return_24h_pct")),
                c.get("forecast_disposition") or "DISCOVERY ONLY"))
    else:
        lines.append("No candidates in the current forward cohort were removed by the pool-age guard.")
    return "\n".join(lines)


def _rewrite_current_run_memory(memory_dir: Path, run_id: str, discovery_keys: set[str]) -> int:
    if not memory_dir:
        return 0
    changed = 0
    for path in sorted((memory_dir / "forecasts").glob("*.jsonl.gz")):
        records = []
        dirty = False
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if str(record.get("run_id")) == str(run_id) and record.get("market_key") in discovery_keys:
                    original_score = record.get("raw_score")
                    if original_score is not None:
                        record["source_raw_score_pre_age_guard"] = original_score
                    record["raw_score"] = None
                    record["forecast_eligibility_status"] = "DISCOVERY_ONLY_NOT_24H_AGE_QUALIFIED"
                    record["score_is_probability"] = False
                    feature = record.get("feature_snapshot") if isinstance(record.get("feature_snapshot"), dict) else {}
                    feature["forecast_eligibility_status"] = record["forecast_eligibility_status"]
                    feature["return_24h_comparable"] = False
                    feature["source_forecast_score_pre_age_guard"] = original_score
                    feature.pop("forecast_score", None)
                    feature["change_24h_pct"] = None
                    record["feature_snapshot"] = feature
                    dirty = True
                    changed += 1
                records.append(record)
        if dirty:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as handle:
                for record in records:
                    handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            tmp.replace(path)
    return changed


def apply_forward_guard(outdir: Path, memory_dir: Path | None = None, run_id: str = "") -> dict[str, Any]:
    main_path = outdir / "rocket-hunt.json"
    forward_path = outdir / "forward-24h-rocket.json"
    if not main_path.exists() or not forward_path.exists():
        raise FileNotFoundError("both rocket-hunt.json and forward-24h-rocket.json are required")
    main = json.loads(main_path.read_text(encoding="utf-8"))
    forward = json.loads(forward_path.read_text(encoding="utf-8"))
    index = _build_index(main)

    original_candidates = forward.get("candidates") if isinstance(forward.get("candidates"), list) else []
    annotated = [_annotate_candidate(c, index) for c in original_candidates if isinstance(c, dict)]
    eligible = [c for c in annotated if c.get("return_24h_comparable") is True]
    discovery_only = [c for c in annotated if c.get("return_24h_comparable") is False]
    eligible.sort(key=lambda c: finite_number(c.get("forecast_score")) if finite_number(c.get("forecast_score")) is not None else -1e99, reverse=True)
    for rank, candidate in enumerate(eligible, 1):
        candidate["forecast_rank"] = rank
        candidate["forecast_disposition"] = "AGE_QUALIFIED_FORWARD_COHORT"
    for candidate in discovery_only:
        candidate["forecast_rank"] = None
    forward["pre_age_guard_upside_leader"] = forward.get("upside_leader") or forward.get("forecast_leader")
    forward["candidates"] = eligible + discovery_only
    forward["new_launch_discovery_leads_v418"] = discovery_only
    forward["forward_age_guard_version"] = GUARD_VERSION
    forward["age_guard_summary"] = {
        "guard_version": GUARD_VERSION,
        "eligible_forward_candidates": len(eligible),
        "discovery_only_candidates": len(discovery_only),
        "candidate_denominator": len(eligible),
        "rule": "DEX pools require observed pool age >=24 hours to enter comparable 24h forward ranking; unknown-age pools remain discovery-only.",
    }
    if eligible:
        leader = eligible[0]
        forward["forecast_leader"] = leader
        forward["upside_leader"] = leader
        forward["top_rocket_pick"] = leader
        forward["status"] = "TOP_ROCKET_REFERENCE_PLAN_READY_AGE_QUALIFIED"
        execution = "EXECUTION VERIFIED" if leader.get("executable_route_verified") and leader.get("safety_verified") else "EXECUTION UNVERIFIED"
        forward["summary"] = f"TOP ROCKET — {execution}: {leader.get('asset') or leader.get('pair')}; best-ranked age-qualified candidate in {len(eligible)} sampled forward candidates."
    else:
        forward["forecast_leader"] = None
        forward["upside_leader"] = None
        forward["top_rocket_pick"] = None
        forward["best_immediate_entry"] = None
        forward["status"] = "NO_AGE_QUALIFIED_FORWARD_CANDIDATE"
        forward["summary"] = f"No age-qualified forward candidate among {len(annotated)} sampled candidates; {len(discovery_only)} new/unknown-age leads retained as discovery-only."
    # Keep the raw leader for discovery, with its data quality status made explicit.
    raw_leader = forward.get("raw_upside_leader")
    if isinstance(raw_leader, dict):
        forward["raw_upside_leader"] = _annotate_candidate(raw_leader, index)
    raw_champion = forward.get("trailing_raw_24h_champion")
    if isinstance(raw_champion, dict):
        forward["trailing_raw_24h_champion"] = _annotate_candidate(raw_champion, index)
    forward["age_guard_summary"]["run_id"] = str(run_id)
    forward["age_guard_summary"]["raw_candidate_count"] = len(annotated)

    discovery_keys = set()
    for candidate in discovery_only:
        for key in (market_key(candidate), _legacy_market_key(candidate)):
            if key:
                discovery_keys.add(key)
    memory_corrected = _rewrite_current_run_memory(memory_dir, run_id, discovery_keys) if memory_dir and run_id else 0
    forward["age_guard_summary"]["current_run_memory_rows_corrected"] = memory_corrected
    forward_path.write_text(json.dumps(forward, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")

    quality_path = outdir / "v418-quality-audit.json"
    if quality_path.exists():
        quality = json.loads(quality_path.read_text(encoding="utf-8"))
        quality["forward_age_guard_version"] = GUARD_VERSION
        quality["forward_age_guard"] = forward["age_guard_summary"]
        quality_path.write_text(json.dumps(quality, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        main["v418_quality_audit"] = quality
    main["forward_age_guard_version"] = GUARD_VERSION
    main["forward_age_guard"] = forward["age_guard_summary"]
    main_path.write_text(json.dumps(main, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")

    md_path = outdir / "rocket-hunt.md"
    if md_path.exists():
        md = md_path.read_text(encoding="utf-8")
        start = md.find("### Forward 24h Top Pick")
        end = md.find("**Important:** the forecast", start if start >= 0 else 0)
        if start >= 0 and end > start:
            md_path.write_text(md[:start] + _render_forward_markdown(forward) + "\n\n" + md[end:], encoding="utf-8")
    return forward["age_guard_summary"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", type=Path, default=Path("rocket-run"))
    parser.add_argument("--memory-dir", type=Path, default=None)
    parser.add_argument("--run-id", default="")
    args = parser.parse_args(argv)
    summary = apply_forward_guard(args.outdir, args.memory_dir, args.run_id)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
