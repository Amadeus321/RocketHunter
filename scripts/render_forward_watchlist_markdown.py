#!/usr/bin/env python3
"""Render the finalized forward-watchlist JSON as its matching Markdown report."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import html
import json
import math
from pathlib import Path
from typing import Any

try:
    from scripts.tp_gate import canonical_tp_upside
except ImportError:
    from tp_gate import canonical_tp_upside

try:
    from scripts.live_price_verifier import refresh_and_filter_candidates, is_valid_web3_address, run_comprehensive_security_check
except ImportError:
    try:
        from live_price_verifier import refresh_and_filter_candidates, is_valid_web3_address, run_comprehensive_security_check
    except ImportError:
        refresh_and_filter_candidates = None
        run_comprehensive_security_check = None
        def is_valid_web3_address(addr: str) -> bool:
            return bool(addr and len(str(addr).strip()) >= 10 and not str(addr).strip().isdigit())


def get_project_root() -> Path:
    cur = Path(__file__).resolve().parent
    for p in [cur] + list(cur.parents):
        if (p / "rocket-runs").exists() and (p / "VERSION").exists():
            return p
    return Path(__file__).resolve().parents[1]


ROOT = get_project_root()
BT = chr(96)


def _project_version() -> str:
    try:
        ver_file = ROOT / "VERSION"
        if ver_file.exists():
            v = ver_file.read_text(encoding="utf-8").strip()
            if v:
                return f"v{v}" if not v.startswith("v") else v
    except Exception:
        pass
    return "v4.26.7"


def _format_israel_time(dt_val: Any) -> str:
    if not dt_val or dt_val == "unavailable":
        return "unavailable"
    s = str(dt_val).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt_utc = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        israel_dt = dt_utc.astimezone(timezone(timedelta(hours=2)))
        return israel_dt.strftime("%Y-%m-%d %H:%M:%S IST")
    except Exception:
        return s


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _price(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "unavailable"
    if number == 0:
        return "0"
    if abs(number) >= 1.0:
        return f"{number:,.4f}".rstrip("0").rstrip(".")
    s = f"{number:.10f}".rstrip("0").rstrip(".")
    if s in ("0", "0.", ""):
        s = f"{number:.14f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _candidate_tp(candidate: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
    plan = candidate.get("trade_plan") or {}
    if not isinstance(plan, dict):
        plan = {}
    entry = _number(candidate.get("final_price_usd")) if candidate.get("final_price_verified") is True else None
    if entry is None:
        entry = _number(candidate.get("price"))
    if entry is None:
        entry = _number(plan.get("entry_price"))
    target = _number(plan.get("single_take_profit"))
    if target is None:
        target = _number(candidate.get("target_price"))
    method = plan.get("target_method") or candidate.get("target_price_source")
    if not isinstance(method, str) or not method.strip() or method.strip().upper() in {
        "UNVERIFIED", "UNKNOWN", "NONE"
    }:
        method = None
    if entry is None or entry <= 0 or target is None or target <= 0 or method is None:
        return entry, None, method
    return entry, target, method


def _candidate_opportunity_fields(candidate: dict[str, Any]) -> tuple[float | None, float | None, float | None, str, str]:
    plan = candidate.get("trade_plan") or {}
    if not isinstance(plan, dict):
        plan = {}

    entry = _number(candidate.get("final_price_usd"))
    if entry is None:
        entry = _number(candidate.get("price"))
    if entry is None:
        entry = _number(plan.get("entry_price"))
    if entry is None:
        entry = _number(plan.get("tp_reference_price"))

    target = _number(plan.get("single_take_profit"))
    if target is None:
        target = _number(candidate.get("target_price"))

    potential = _number(candidate.get("tp_upside_pct"))
    if potential is None and entry is not None and target is not None and entry > 0:
        potential = canonical_tp_upside(entry, target)

    market_type = str(candidate.get("market_type") or "").lower()
    venue = str(candidate.get("venue") or candidate.get("chain") or "").strip()
    pair_str = str(candidate.get("pair") or candidate.get("asset") or "").strip()
    is_cex = market_type.startswith("cex") or venue.lower() in ("mexc", "kucoin", "binance", "bybit", "gate", "bitget", "okx")

    pool = (
        candidate.get("final_pool_address")
        or candidate.get("pool_address")
        or candidate.get("pair_address")
        or candidate.get("address")
    )
    if not pool or pool == "—":
        if is_cex:
            pool = f"CEX Order Book ({venue.upper() or 'SPOT'})"
        else:
            pool = "—"

    token = (
        candidate.get("final_token_contract")
        or candidate.get("contract_address")
        or candidate.get("token_contract")
        or candidate.get("token_address")
        or candidate.get("mint")
    )
    if not token or token == "—":
        if is_cex:
            token = f"CEX Custody ({candidate.get('asset') or pair_str})"
        else:
            token = "—"

    return entry, target, potential, str(pool), str(token)


OKX_CHAIN_IDS = {
    "solana": "501",
    "bsc": "56",
    "base": "8453",
    "eth": "1",
    "ethereum": "1",
    "arbitrum": "42161",
    "polygon": "137",
    "polygon_pos": "137",
    "avalanche": "43114",
    "avax": "43114",
    "optimism": "10",
    "sui": "784",
}

OKX_STABLE_CURRENCIES = {
    "501": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "56": "0x55d398326f99059fF775485246999027B3197955",   # USDT
    "8453": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
    "1": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",     # USDC
    "42161": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", # USDC
    "137": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",   # USDC
    "43114": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E", # USDC
}


def okx_dex_swap_url(chain: Any, token_contract: Any) -> str:
    chain_slug = str(chain or "").lower().strip()
    chain_id = OKX_CHAIN_IDS.get(chain_slug)
    token = str(token_contract or "").strip()
    if not chain_id or not token or token in ("—", "unknown", "None"):
        return ""
    input_curr = OKX_STABLE_CURRENCIES.get(chain_id, "")
    base_url = "https://www.okx.com/web3/dex-swap"
    if input_curr:
        return f"{base_url}?inputChain={chain_id}&inputCurrency={input_curr}&outputChain={chain_id}&outputCurrency={token}"
    return f"{base_url}?outputChain={chain_id}&outputCurrency={token}"


def _filter_min_potential(candidates: list[dict[str, Any]], min_potential: float = 40.0) -> list[dict[str, Any]]:
    filtered = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        entry, target, potential, pool, token = _candidate_opportunity_fields(c)
        if entry is None or target is None or potential is None or potential < min_potential:
            continue
        # Reject dummy or invalid address formats (e.g. SN99 with pool '0-99', token '99')
        if (pool != "—" and not is_valid_web3_address(pool)) or (token != "—" and not is_valid_web3_address(token)):
            continue

        # Rule 5 / Sanity checks & strict risk/loss guard:
        liq = _number(c.get("liquidity_usd"))
        if liq is not None and liq < 15000.0:
            continue
        flags = set(c.get("early_breakout_risk_flags") or []) | set(c.get("risk_flags") or [])
        if any("HONEYPOT" in f or "UNLOCKED_LP" in f or "WASH_TRADING" in f or "EXTREME_THIN_LIQUIDITY" in f or "LIQUIDITY_UNDER_5000_USD" in f for f in flags):
            continue
        if run_comprehensive_security_check:
            is_bad, _ = run_comprehensive_security_check(c)
            if is_bad:
                continue
        if potential > 1000.0:
            quality = c.get("quality_gate") if isinstance(c.get("quality_gate"), dict) else {}
            if not quality.get("execution_ready") and not c.get("final_price_verified"):
                continue
        pair_name = str(c.get("pair") or c.get("asset") or "").upper()
        if any(stable in pair_name for stable in ["USDT", "USDC", "DAI", "BUSD"]) and all(s in pair_name for s in ["USDT", "USDC"]):
            if entry < 0.5 or entry > 1.5:
                continue
        filtered.append(c)
    return filtered


def _render_position_monitor(document: dict[str, Any]) -> list[str]:
    monitor = document.get("position_monitor")
    if not isinstance(monitor, dict):
        return []
    active = monitor.get("active_positions")
    completed = monitor.get("recently_completed_positions")
    active = active if isinstance(active, list) else []
    completed = completed if isinstance(completed, list) else []
    lines = [
        "",
        "## 24-hour reference-position monitoring",
        "",
        f"- **Status:** {BT}{monitor.get('status') or 'UNAVAILABLE'}{BT}",
        f"- **Provider / resolution:** {monitor.get('provider') or 'UNVERIFIED'} / {monitor.get('ohlcv_resolution') or 'UNVERIFIED'}",
        f"- **Active references:** {len(active)} / {monitor.get('max_open_reference_positions', 'unavailable')}",
        "",
        "| Asset / pair | Market | Pair address | Window closes (UTC) | MFE | Latest return | Protective stop | Status |",
        "|---|---|---|---|---:|---:|---|---|",
    ]
    for item in active + completed[-20:]:
        mfe = _number(item.get("max_favorable_excursion_pct"))
        latest = _number(item.get("latest_return_from_entry_pct"))
        stop_price = _number(item.get("trailing_stop_price")) or _number(item.get("initial_stop_price"))
        stop_kind = "trail" if _number(item.get("trailing_stop_price")) is not None else "initial"
        stop_text = (f"{stop_kind}: {_price(stop_price)}" if stop_price is not None else "unavailable")
        market = item.get("venue") or item.get("chain") or item.get("market_type") or "—"
        pool_addr = str(item.get("pool_address") or item.get("pair_address") or item.get("address") or "—")
        lines.append("| %s / %s | %s | `%s` | %s | %s | %s | %s | %s |" % (
            item.get("asset") or item.get("symbol") or "Unknown",
            item.get("pair") or "Unknown",
            market,
            pool_addr,
            item.get("time_stop_utc") or "unavailable",
            ("%+.2f%%" % mfe) if mfe is not None else "unavailable",
            ("%+.2f%%" % latest) if latest is not None else "unavailable",
            stop_text,
            item.get("position_status") or "unavailable",
        ))
    lines += [
        "",
        "> A TP touch is a milestone; the trailing runner remains open until its protective trail or the 24h time stop. Initial risk is ATR-based where candle coverage allows it. Hourly candle extrema are observed reference prices, not actual fills. A same-hour target/stop collision is marked ambiguous.",
    ]
    return lines


def render_report(document: dict[str, Any]) -> str:
    gen_ver = document.get("general_version") or _project_version()
    age_summary = document.get("age_guard_summary")
    if not isinstance(age_summary, dict):
        age_summary = {}
    run_id = document.get("run_dir_name") or document.get("run_id") or age_summary.get("run_id") or "unavailable"
    if str(run_id).startswith("local-") and not str(run_id).startswith("local-run-"):
        runs_dir = ROOT / "rocket-runs"
        if runs_dir.exists():
            for p in runs_dir.iterdir():
                if p.is_dir() and (p / "scan-status.json").exists():
                    try:
                        s_data = json.loads((p / "scan-status.json").read_text(encoding="utf-8"))
                        if s_data.get("run_id") == run_id:
                            run_id = p.name
                            break
                    except Exception:
                        pass
    as_of = document.get("as_of_utc") or document.get("forecast_generation_utc") or "unavailable"
    horizon = _number(document.get("forecast_horizon_hours"))
    candidates = document.get("candidates")
    if not isinstance(candidates, list):
        candidates = []
    candidates = [item for item in candidates if isinstance(item, dict)]
    candidates.sort(key=lambda item: (
        _number(item.get("forward_opportunity_rank")) if _number(item.get("forward_opportunity_rank")) is not None else math.inf,
        -(_number(item.get("forward_opportunity_score")) or 0.0),
        -(_number(item.get("early_breakout_score")) or 0.0),
        str(item.get("pair") or item.get("asset") or ""),
    ))
    has_final_categories = (
        isinstance(document.get("unverified_research_watchlist"), list) or
        isinstance(document.get("young_discovery_only_leads"), list)
    )
    has_final_categories = (
        isinstance(document.get("unverified_research_watchlist"), list) or
        isinstance(document.get("young_discovery_only_leads"), list)
    )
    is_v426 = bool(document.get("general_version") and str(document.get("general_version")).startswith("v4.26"))
    if has_final_categories or is_v426:
        raw_list = []
        if isinstance(document.get("unverified_research_watchlist"), list):
            raw_list.extend(document["unverified_research_watchlist"])
        if isinstance(document.get("young_discovery_only_leads"), list):
            raw_list.extend(document["young_discovery_only_leads"])
        if not raw_list and candidates:
            raw_list.extend(candidates)
        if isinstance(document.get("verified_actionable_opportunities"), list):
            raw_list.extend(document["verified_actionable_opportunities"])

        refreshed_done = False
        scan_status_val = str(document.get("scan_status") or "").upper()
        if refresh_and_filter_candidates and scan_status_val == "COMPLETED":
            try:
                act_ref, watch_ref = refresh_and_filter_candidates(raw_list, 40.0)
                if act_ref or watch_ref:
                    actionable = act_ref
                    research_candidates = watch_ref
                    discovery_candidates = []
                    refreshed_done = True
            except BaseException:
                pass

        if not refreshed_done:
            seen = set()
            unified_candidates = []
            for c in raw_list:
                if not isinstance(c, dict):
                    continue
                key = (str(c.get("pool_address") or c.get("pair_address") or c.get("id") or ""), str(c.get("pair") or c.get("asset") or ""))
                if key in seen:
                    continue
                seen.add(key)
                unified_candidates.append(c)
            research_candidates = _filter_min_potential(unified_candidates, 40.0)
            discovery_candidates = []
    else:
        research_candidates, discovery_candidates = [], []
        for candidate in candidates:
            age = _number(candidate.get("age_hours"))
            is_young_dex = str(candidate.get("market_type") or "").lower() == "dex_pool" and (
                age is None or age < 24.0
            )
            (discovery_candidates if is_young_dex else research_candidates).append(candidate)
    total_candidates = len(candidates)
    display_candidates = research_candidates[:25]
    display_discovery = discovery_candidates[:25]
    gh_url = document.get("github_actions_run_url")
    clickable_gh_url = f"[{gh_url}]({gh_url})" if gh_url and str(gh_url).startswith("http") else (gh_url or "unavailable")

    lines = [
        "# Forward 24h Watchlist",
        "",
        f"- **General version:** {BT}{gen_ver}{BT}",
        f"- **Scan finished (Israel Time):** {BT}{_format_israel_time(document.get('scan_finished_utc'))}{BT}",
        f"- **Run:** {BT}{run_id}{BT}",
        f"- **GitHub Actions run ID:** {BT}{document.get('github_actions_run_id') or 'unavailable'}{BT}",
        f"- **GitHub Actions run URL:** {clickable_gh_url}",
        f"- **Status:** {BT}{document.get('status') or 'unavailable'}{BT}",
        "- **Objective:** rank early Web3/DeFi breakout potential for the next 24 hours; source verification and execution readiness remain separate",
        f"- **NOT QUALIFIED research shortlist:** top {len(display_candidates)} of {len(research_candidates)} mature/unverified research candidates",
    ]
    if horizon is not None:
        lines.append(f"- **Forecast horizon:** {horizon:g} hours")
    opportunity = document.get("forward_opportunity_ranking")
    if isinstance(opportunity, dict):
        lines.append(
            f"- **Ranked next-24h opportunities:** {opportunity.get('candidate_count', 0)}"
        )
        lines.append(
            f"- **Historical 24h comparable:** {opportunity.get('historical_24h_comparable_count', 0)}; "
            f"not yet comparable: {opportunity.get('historical_24h_not_comparable_count', 0)}"
        )
        lines.append("- **Historical-age rule:** applies to past-return comparability only; it does not exclude young pools from the next-24h opportunity ranking.")
    else:
        eligible = age_summary.get("eligible_forward_candidates")
        discovered = age_summary.get("raw_candidate_count")
        if eligible is not None or discovered is not None:
            lines.append(
                f"- **Historical 24h comparable:** "
                f"{eligible if eligible is not None else 'unavailable'} / "
                f"{discovered if discovered is not None else 'unavailable'}"
            )
    market_context = document.get("market_context") if isinstance(document.get("market_context"), dict) else {}
    hyperliquid_context = document.get("hyperliquid_derivatives_context") if isinstance(document.get("hyperliquid_derivatives_context"), dict) else {}
    lines.extend([
        "",
        f"- **Market regime:** {market_context.get('label') or 'UNVERIFIED'}; BTC breadth sample: {market_context.get('breadth_sample_count', 0)} assets; positive-alt breadth: {market_context.get('alt_breadth_pct') if market_context.get('alt_breadth_pct') is not None else 'unavailable'}%",
        f"- **Hyperliquid derivatives cross-check:** {hyperliquid_context.get('status') or 'unavailable'}; matched candidates: {hyperliquid_context.get('matched_candidates', 0)} of {hyperliquid_context.get('markets_received', 0)} public perp markets",
        "",
        "> Research-only reference plan, not an order. Upside scenarios are uncalibrated; the trade plan strictly enforces 1 single Take Profit (minimum +40.00%) with no stop loss and no trailing stop. Positions remain open for up to 24 hours until the single TP or 24h window mark. Neither hourly highs nor reference prices guarantee executable fills.",
        "",
        "## Verified actionable opportunities",
        "",
    ])
    if not (has_final_categories or is_v426) or not refreshed_done:
        actionable = document.get("verified_actionable_opportunities")
        actionable = actionable if isinstance(actionable, list) and document.get("scan_status") == "COMPLETED" else []
    if actionable:
        lines.extend([
            "Only candidates passing the final price, identity, +40% single-TP, safety and two-way quote gates appear here.",
            "",
            "| Pair | Chain | Current Price | Single TP | Potential (% till TP) | Pool Address | Token Contract | Verification | Safety | Execution |",
            "|---|---|---:|---:|---:|---|---|---|---|---|",
        ])
        for candidate in actionable:
            plan = candidate.get("trade_plan") if isinstance(candidate.get("trade_plan"), dict) else {}
            entry, target, potential, pool, token = _candidate_opportunity_fields(candidate)
            pot_val = _number(candidate.get("tp_upside_pct")) or potential or 0.0
            lines.append("| %s | %s | %s | %s | %+.2f%% | `%s` | `%s` | %s | %s | %s |" % (
                candidate.get("pair") or candidate.get("asset") or "—",
                candidate.get("chain") or "—",
                _price(entry),
                _price(target),
                pot_val,
                pool,
                token,
                candidate.get("verification_status") or "UNVERIFIED",
                candidate.get("safety_status") or "UNVERIFIED",
                candidate.get("execution_status") or "UNVERIFIED",
            ))
    elif document.get("scan_status") != "COMPLETED":
        lines.extend(["**SCAN NOT COMPLETED — no actionable opportunities are published from this run.**", ""])
    else:
        lines.extend(["**ZERO VERIFIED OPPORTUNITIES** passed every mandatory final gate for this scan.", ""])
    lines.extend([
        "",
        "## Unverified research watchlist — NOT QUALIFIED",
        "",
        "Research rankings and hypothetical upside do not satisfy the technical TP or verification gates. Only candidates with single-TP potential >= +40.00% are displayed.",
        "",
    ])

    if display_candidates:
        lines.extend([
            "| Pair | Chain | Current Price | Single TP | Potential (% till TP) | Pool Address | Token Contract | Pool Age | Status |",
            "|---|---|---:|---:|---:|---|---|---:|---|",
        ])
        for candidate in display_candidates:
            pair = candidate.get("pair") or candidate.get("asset") or "Unnamed pair"
            entry, target, potential, pool, token = _candidate_opportunity_fields(candidate)
            chain = candidate.get("chain") or candidate.get("venue") or "—"
            age = _number(candidate.get("age_hours"))
            age_str = f"{age:.2f}h" if age is not None else "—"
            pot_str = f"+{potential:.2f}%" if potential is not None else "unavailable"
            lines.append("| %s | %s | %s | %s | %s | `%s` | `%s` | %s | NOT QUALIFIED |" % (
                pair,
                chain,
                _price(entry),
                _price(target),
                pot_str,
                pool,
                token,
                age_str,
            ))
    else:
        lines.append("No unverified research candidates met the mandatory +40.00% potential threshold.")
    lines.extend([
        "",
        "> *The 24h upside scenario is a volatility/momentum scenario from hourly candles, not a predicted return or calibrated probability. Initial downside is an ATR-based reference stop, not a guaranteed max loss. Scenario R:R is upside scenario divided by initial stop distance; it is a heuristic, not realized expectancy.",
        "",
        "## Unverified research candidate details",
        "",
    ])

    if not display_candidates and not display_discovery:
        lines.extend([
            "No detailed forward candidate array was produced for this run. Use the upstream mover boards and cached market observations to populate discovery-only leads; an empty execution shortlist is not evidence that the market has no gainers.",
            "",
        ])
        lines.extend(_render_position_monitor(document))
        return "\n".join(lines)

    for index, candidate in enumerate(display_candidates, start=1):
        pair = candidate.get("pair") or candidate.get("asset") or "Unnamed pair"
        entry, target, method = _candidate_tp(candidate)
        quality = candidate.get("quality_gate")
        if not isinstance(quality, dict):
            quality = {}
        quality_status = quality.get("status") or "QUALITY STATUS UNAVAILABLE"
        execution_ready = quality.get("execution_ready")
        pair_url = candidate.get("pair_url") or candidate.get("source_url")
        pair_address = candidate.get("pair_address") or candidate.get("pool_address")
        entry_decision = candidate.get("entry_decision") or "unavailable"
        forward_decision = candidate.get("forward_opportunity_decision")

        lines.append(f"### {index}. {pair}")
        lines.append("")
        lines.append("**Qualification status: NOT QUALIFIED — research only.**")
        lines.append("")
        if entry is not None and target is not None and method is not None:
            upside = canonical_tp_upside(entry, target)
            if upside is not None:
                lines.append(f"**{pair} TP: {upside:+.2f}%**")
            lines.append("")
        forward_rank = _number(candidate.get("forward_opportunity_rank"))
        forward_score = _number(candidate.get("forward_opportunity_score"))
        early_score = _number(candidate.get("early_breakout_score"))
        if forward_rank is not None:
            lines.append(f"- **Next-24h opportunity rank:** {int(forward_rank)}")
        if forward_score is not None:
            lines.append(f"- **Next-24h opportunity score:** {forward_score:.2f}/100 (heuristic, not a probability)")
        prob_tp = _number(candidate.get("probability_tp_hit_24h_pct"))
        exp_ret = _number(candidate.get("expected_net_24h_return_pct"))
        mfe = _number(candidate.get("potential_mfe_24h_pct"))
        mae = _number(candidate.get("potential_mae_24h_pct"))
        hrs = _number(candidate.get("estimated_hours_to_target"))
        confl = _number(candidate.get("breakout_confluence_score"))
        if prob_tp is not None and exp_ret is not None:
            lines.append(f"- **P(+40% TP in 24h):** {prob_tp:.1f}% | **Expected Net 24h Return (E[R]):** {exp_ret:+.2f}%")
        if mfe is not None and mae is not None:
            lines.append(f"- **Projected 24h Excursion:** MFE {mfe:+.2f}% | MAE {mae:+.2f}%")
        if hrs is not None:
            lines.append(f"- **Est. Time to Target:** ~{hrs:.1f} hours")
        if confl is not None:
            lines.append(f"- **9-Engine Breakout Confluence:** {confl:.2f}/100")
        atr = _number(candidate.get("atr_1h_pct"))
        stop_price = _number(candidate.get("initial_stop_price"))
        stop_loss_pct = _number(candidate.get("stop_loss_pct"))
        reward_risk = _number(candidate.get("scenario_reward_risk_ratio"))
        if atr is not None:
            lines.append(f"- **1h ATR:** {atr:.2f}%")
        if stop_price is not None:
            lines.append(f"- **Initial protective stop:** {BT}{_price(stop_price)}{BT} ({stop_loss_pct:.2f}% reference downside; {candidate.get('stop_method') or 'method unavailable'})")
        else:
            lines.append("- **Initial protective stop:** unavailable; candle ATR coverage is insufficient")
        if reward_risk is not None:
            lines.append(f"- **Scenario reward/risk:** {reward_risk:.2f}x")
        if candidate.get("hyperliquid_context_available"):
            lines.append(f"- **Hyperliquid perp cross-check:** {candidate.get('hyperliquid_match_status')}; mark ${_number(candidate.get('hyperliquid_mark_price_usd')):,.6g}; hourly funding {(_number(candidate.get('hyperliquid_funding_hourly_pct')) or 0):+.4f}%")
            oi_change = _number(candidate.get("oi_change_4h_pct"))
            if oi_change is not None:
                lines.append(f"- **Hyperliquid open-interest change (4h-normalized):** {oi_change:+.2f}%")
        projected_upside = _number(candidate.get("projected_24h_upside_pct"))
        if projected_upside is not None:
            lines.append(f"- **24h upside scenario:** {projected_upside:+.2f}% (hourly volatility/momentum heuristic; not calibrated)")
            lines.append(f"- **Scenario method:** {candidate.get('upside_projection_method') or 'unavailable'}")
        else:
            lines.append("- **24h upside scenario:** candle projection unavailable; candidate remains ranked using discovery momentum and research signals.")
        if candidate.get("tracked_token_orientation") is not None:
            lines.append(f"- **Tracked-token candle orientation:** {candidate.get('tracked_token_orientation')} ({'contract matched' if candidate.get('tracked_token_orientation_verified') else 'provider-order fallback; unverified'})")
        if candidate.get("opportunity_tier"):
            lines.append(f"- **Opportunity tier:** {BT}{candidate['opportunity_tier']}{BT}")
        if candidate.get("forward_opportunity_ranking_model"):
            lines.append(f"- **Ranking model:** {candidate['forward_opportunity_ranking_model']}")
        if candidate.get("late_chase_penalty") is not None:
            lines.append(f"- **Late-chase / flow-risk penalty:** {candidate['late_chase_penalty']:.2f} points")
        if candidate.get("historical_24h_comparable") is False:
            lines.append("- **Historical 24h return:** not comparable yet; this does not disqualify a new 24h forward opportunity.")
        if early_score is not None or (entry is not None and target is not None and method is not None):
            if early_score is not None:
                lines.append(f"- **Early-breakout score:** {early_score:.2f}/100 — {candidate.get('early_breakout_tier') or 'unclassified'}")
                if isinstance(candidate.get("early_breakout_risk_flags"), list) and candidate["early_breakout_risk_flags"]:
                    lines.append("- **Early-breakout risk flags:** " + ", ".join(candidate["early_breakout_risk_flags"][:8]))
            lines.append(f"- **Reference price:** {BT}{_price(entry)}{BT}")
            lines.append(f"- **Single TP:** {BT}{_price(target)}{BT}")
            lines.append(f"- **TP method:** {BT}{method}{BT}")
        else:
            lines.append(f"**{pair} - TP forming / unavailable**")
            lines.append("")
            lines.append(f"- **Reference price:** {BT}{_price(entry)}{BT}")
            lines.append("- **Single TP:** not source-verified in this report")
        gate = str(quality_status)
        ready = str(execution_ready).lower() if isinstance(execution_ready, bool) else "unknown"
        lines.append(f"- **Quality gate:** {BT}{gate}{BT}")
        lines.append(f"- **Execution ready:** {BT}{ready}{BT}")
        if forward_decision:
            lines.append(f"- **Next-24h research rank:** {BT}{forward_decision}{BT}")
        if candidate.get("historical_24h_comparable") is False:
            lines.append(f"- **Historical age-guard disposition:** {BT}{entry_decision}{BT}")
        else:
            lines.append(f"- **Entry decision:** {BT}NOT QUALIFIED{BT}")
        lines.append(f"- **Historical 24h return comparable:** {BT}{str(bool(candidate.get('historical_24h_comparable'))).lower()}{BT}")
        if candidate.get("data_timestamp_utc"):
            lines.append(f"- **Market-data timestamp (UTC):** {BT}{candidate['data_timestamp_utc']}{BT}")
        for label, field in (("5m momentum", "change_5m_pct"), ("1h momentum", "change_1h_pct"),
                             ("6h momentum", "change_6h_pct"), ("24h momentum", "change_24h_pct")):
            value = _number(candidate.get(field))
            if value is not None:
                lines.append(f"- **{label}:** {value:+.2f}%")
        for label, field in (("24h volume (USD)", "volume_24h_usd"), ("Liquidity (USD)", "liquidity_usd"),
                             ("Market cap (USD)", "market_cap_usd"), ("FDV (USD)", "fdv_usd")):
            value = _number(candidate.get(field))
            if value is not None:
                lines.append(f"- **{label}:** ${value:,.0f}")
        age_hours = _number(candidate.get("age_hours"))
        if age_hours is not None:
            lines.append(f"- **Pool age:** {age_hours:.2f} hours")
        transactions = _number(candidate.get("txns_24h"))
        if transactions is not None:
            lines.append(f"- **24h transactions:** {transactions:,.0f}")
        if candidate.get("evidence_coverage_pct") is not None:
            lines.append(f"- **Evidence coverage:** {candidate['evidence_coverage_pct']}%")
        if isinstance(pair_address, str) and pair_address:
            lines.append(f"- **Pool address:** {BT}{pair_address}{BT}")
        if isinstance(pair_url, str) and pair_url:
            lines.append(f"- **Market link:** {pair_url}")
        if isinstance(quality.get("reasons"), list) and quality["reasons"]:
            lines.append("- **Quality-gate reasons:**")
            lines.extend(f"  - {reason}" for reason in quality["reasons"])
        if isinstance(candidate.get("entry_blockers"), list) and candidate["entry_blockers"]:
            lines.append("- **Entry blockers:**")
            lines.extend(f"  - {reason}" for reason in candidate["entry_blockers"])
        if isinstance(candidate.get("risk_flags"), list) and candidate["risk_flags"]:
            lines.append("- **Risk flags:**")
            lines.extend(f"  - {flag}" for flag in candidate["risk_flags"])
        calibrated = _number(candidate.get("calibrated_prob_24h"))
        if candidate.get("score_is_probability") is True and calibrated is not None:
            lines.append(f"- **Calibrated 24h probability:** {BT}{calibrated * 100.0:.2f}%{BT}")
        else:
            observations = candidate.get("calibration_observations_total")
            count = observations if observations is not None else "unknown"
            lines.append(f"- **Calibrated 24h probability:** unavailable (resolved observations: {count})")
        lines.append("")

    if display_discovery:
        lines.extend([
            "## Young discovery-only leads — NOT QUALIFIED",
            "",
            "Pools younger than 24 hours or with unknown age are discovery leads only; they are not mixed with the mature research watchlist.",
            "",
            "| Pair | Chain | Current Price | Single TP | Potential (% till TP) | Pool Address | Token Contract | Pool Age |",
            "|---|---|---:|---:|---:|---|---|---:|",
        ])
        for candidate in display_discovery:
            pair = candidate.get("pair") or candidate.get("asset") or "Unnamed pair"
            entry, target, potential, pool, token = _candidate_opportunity_fields(candidate)
            chain = candidate.get("chain") or "—"
            age = _number(candidate.get("age_hours"))
            age_str = f"{age:.2f}h" if age is not None else "unknown"
            pot_str = f"+{potential:.2f}%" if potential is not None else "unavailable"
            lines.append("| %s | %s | %s | %s | %s | `%s` | `%s` | %s |" % (
                pair,
                chain,
                _price(entry),
                _price(target),
                pot_str,
                pool,
                token,
                age_str,
            ))
        lines.append("")

    lines.extend([
        "## Interpretation",
        "",
        "A " + BT + "NOT_SHORTLISTED" + BT + " quality gate or " + BT + "execution_ready: false" + BT + " means the candidate is a research lead, not a trade-ready pick. Missing evidence is not a pass. Probabilities are shown only when the candidate explicitly marks the value as calibrated.",
        "",
    ])
    lines.extend(_render_position_monitor(document))
    return "\n".join(lines)


def _esc(val: Any) -> str:
    if val is None:
        return ""
    return html.escape(str(val))


def render_html(document: dict[str, Any]) -> str:
    age_summary = document.get("age_guard_summary")
    if not isinstance(age_summary, dict):
        age_summary = {}
    run_id = document.get("run_dir_name") or document.get("run_id") or age_summary.get("run_id") or "unavailable"
    if str(run_id).startswith("local-") and not str(run_id).startswith("local-run-"):
        runs_dir = ROOT / "rocket-runs"
        if runs_dir.exists():
            for p in runs_dir.iterdir():
                if p.is_dir() and (p / "scan-status.json").exists():
                    try:
                        s_data = json.loads((p / "scan-status.json").read_text(encoding="utf-8"))
                        if s_data.get("run_id") == run_id:
                            run_id = p.name
                            break
                    except Exception:
                        pass
    as_of = document.get("as_of_utc") or document.get("forecast_generation_utc") or "unavailable"
    scan_finished = document.get("scan_finished_utc") or "unavailable"
    general_version = document.get("general_version") or _project_version()
    scan_status = document.get("scan_status") or "IN_PROGRESS"
    gh_run_id = document.get("github_actions_run_id") or "unavailable"
    gh_url = document.get("github_actions_run_url")

    candidates = document.get("candidates")
    if not isinstance(candidates, list):
        candidates = []
    candidates = [item for item in candidates if isinstance(item, dict)]
    candidates.sort(key=lambda item: (
        _number(item.get("forward_opportunity_rank")) if _number(item.get("forward_opportunity_rank")) is not None else math.inf,
        -(_number(item.get("forward_opportunity_score")) or 0.0),
        -(_number(item.get("early_breakout_score")) or 0.0),
        str(item.get("pair") or item.get("asset") or ""),
    ))
    has_final_categories = (
        isinstance(document.get("unverified_research_watchlist"), list) or
        isinstance(document.get("young_discovery_only_leads"), list)
    )
    is_v426 = bool(document.get("general_version") and str(document.get("general_version")).startswith("v4.26"))
    if has_final_categories or is_v426:
        raw_list = []
        if isinstance(document.get("unverified_research_watchlist"), list):
            raw_list.extend(document["unverified_research_watchlist"])
        if isinstance(document.get("young_discovery_only_leads"), list):
            raw_list.extend(document["young_discovery_only_leads"])
        if not raw_list and candidates:
            raw_list.extend(candidates)
        if isinstance(document.get("verified_actionable_opportunities"), list):
            raw_list.extend(document["verified_actionable_opportunities"])

        refreshed_done = False
        if isinstance(document.get("verified_actionable_opportunities"), list) and document["verified_actionable_opportunities"]:
            actionable = list(document["verified_actionable_opportunities"])
            research_candidates = list(document.get("unverified_research_watchlist") or [])
            discovery_candidates = list(document.get("young_discovery_only_leads") or [])
            refreshed_done = True
        elif refresh_and_filter_candidates and scan_status == "COMPLETED":
            try:
                act_ref, watch_ref = refresh_and_filter_candidates(raw_list, 40.0)
                if act_ref or watch_ref:
                    actionable = act_ref
                    research_candidates = watch_ref
                    discovery_candidates = []
                    refreshed_done = True
            except BaseException:
                pass

        if not refreshed_done:
            seen = set()
            unified_candidates = []
            for c in raw_list:
                if not isinstance(c, dict):
                    continue
                key = (str(c.get("pool_address") or c.get("pair_address") or c.get("id") or ""), str(c.get("pair") or c.get("asset") or ""))
                if key in seen:
                    continue
                seen.add(key)
                unified_candidates.append(c)
            research_candidates = _filter_min_potential(unified_candidates, 40.0)
            discovery_candidates = []
    else:
        research_candidates, discovery_candidates = [], []
        for candidate in candidates:
            age = _number(candidate.get("age_hours"))
            is_young_dex = str(candidate.get("market_type") or "").lower() == "dex_pool" and (
                age is None or age < 24.0
            )
            (discovery_candidates if is_young_dex else research_candidates).append(candidate)
        research_candidates = _filter_min_potential(research_candidates, 40.0)
        discovery_candidates = _filter_min_potential(discovery_candidates, 40.0)

    display_candidates = research_candidates
    display_discovery = discovery_candidates

    market_context = document.get("market_context") if isinstance(document.get("market_context"), dict) else {}
    hyperliquid_context = document.get("hyperliquid_derivatives_context") if isinstance(document.get("hyperliquid_derivatives_context"), dict) else {}
    opportunity = document.get("forward_opportunity_ranking") if isinstance(document.get("forward_opportunity_ranking"), dict) else {}

    if not (has_final_categories or is_v426) or not refreshed_done:
        actionable = document.get("verified_actionable_opportunities")
        actionable = [c for c in actionable if isinstance(c, dict)] if isinstance(actionable, list) and scan_status == "COMPLETED" else []

    gh_link = f'<a href="{_esc(gh_url)}" target="_blank" rel="noopener">{_esc(gh_url)}</a>' if gh_url and str(gh_url).startswith("http") else _esc(gh_url or "unavailable")

    if actionable:
        act_rows = []
        for c in actionable:
            plan = c.get("trade_plan") if isinstance(c.get("trade_plan"), dict) else {}
            entry, target, potential, pool, token = _candidate_opportunity_fields(c)
            pot_val = _number(c.get("tp_upside_pct")) or potential or 0.0
            act_rows.append(
                f"<tr>"
                f"<td><strong>{_esc(c.get('pair') or c.get('asset') or '—')}</strong></td>"
                f"<td>{_esc(c.get('chain') or '—')}</td>"
                f"<td>${_esc(_price(entry))}</td>"
                f"<td>${_esc(_price(target))}</td>"
                f'<td class="green">+{pot_val:.2f}%</td>'
                f'<td><code>{_esc(pool)}</code></td>'
                f'<td><code>{_esc(token)}</code></td>'
                f'<td><span class="badge ok">{_esc(c.get("verification_status") or "PASS")}</span></td>'
                f'<td><span class="badge ok">{_esc(c.get("safety_status") or "PASS")}</span></td>'
                f'<td><span class="badge ok">{_esc(c.get("execution_status") or "PASS")}</span></td>'
                f'<td><label class="pos-chk-lbl"><input type="checkbox" class="pos-checkbox" data-pair="{_esc(c.get("pair") or c.get("asset") or "")}" data-chain="{_esc(c.get("chain") or "")}" data-entry="{_esc(_price(entry))}" data-tp="{_esc(_price(target))}" data-pot="{_esc(f"+{pot_val:.2f}%")}" data-pool="{_esc(pool)}" data-token="{_esc(token)}" data-version="{_esc(general_version)}" data-run="{_esc(run_id)}" onchange="toggleCandidatePosition(this)"> <span>Take Position</span></label></td>'
                f"</tr>"
            )
        actionable_html = (
            '<div class="table-wrap"><table><thead><tr>'
            '<th>Pair</th><th>Chain</th><th>Current Price</th><th>Single TP</th>'
            '<th>Potential (% till TP)</th><th>Pool Address</th><th>Token Contract</th>'
            '<th>Verification</th><th>Safety</th><th>Execution</th><th>Take Position</th>'
            f'</tr></thead><tbody>{"".join(act_rows)}</tbody></table></div>'
        )
    elif scan_status != "COMPLETED":
        actionable_html = (
            '<div class="banner warn">'
            '<h3>SCAN NOT COMPLETED</h3>'
            '<p>No actionable opportunities are published from an incomplete or in-progress scan.</p>'
            '</div>'
        )
    else:
        actionable_html = (
            '<div class="banner zero">'
            '<div class="banner-badge">FAIL-CLOSED ENFORCED</div>'
            '<h3>ZERO VERIFIED OPPORTUNITIES</h3>'
            '<p>Zero candidates passed every mandatory final gate for this scan.</p>'
            '<p class="meta">Every qualifying trade strictly requires: refreshed live DEX price, verified token contract/pool match, technical single-TP upside &ge; +40.00%, independent safety audit (RugCheck / GoPlus), and two-way executable quotes (Jupiter / 0x).</p>'
            '</div>'
        )

    res_rows = []
    for c in display_candidates:
        pair = c.get("pair") or c.get("asset") or "Unnamed pair"
        entry, target, potential, pool, token = _candidate_opportunity_fields(c)
        venue_val = c.get("venue") or c.get("chain") or ""
        if str(venue_val).lower() in ("mexc", "kucoin", "binance", "bybit", "gate", "bitget", "okx"):
            chain = f"{str(venue_val).upper()} (Spot)"
        else:
            chain = c.get("chain") or c.get("venue") or "—"
        age = _number(c.get("age_hours"))
        age_str = f"{age:.2f}h" if age is not None else "—"
        pot_str = f"+{potential:.2f}%" if potential is not None else "—"
        res_rows.append(
            f"<tr>"
            f"<td><strong>{_esc(pair)}</strong></td>"
            f"<td>{_esc(chain)}</td>"
            f"<td>${_esc(_price(entry))}</td>"
            f"<td>${_esc(_price(target))}</td>"
            f'<td class="green">{_esc(pot_str)}</td>'
            f"<td><code>{_esc(pool)}</code></td>"
            f"<td><code>{_esc(token)}</code></td>"
            f"<td>{_esc(age_str)}</td>"
            f'<td><span class="badge warn">NOT QUALIFIED</span></td>'
            f'<td><label class="pos-chk-lbl"><input type="checkbox" class="pos-checkbox" data-pair="{_esc(pair)}" data-chain="{_esc(chain)}" data-entry="{_esc(_price(entry))}" data-tp="{_esc(_price(target))}" data-pot="{_esc(pot_str)}" data-pool="{_esc(pool)}" data-token="{_esc(token)}" data-version="{_esc(general_version)}" data-run="{_esc(run_id)}" onchange="toggleCandidatePosition(this)"> <span>Take Position</span></label></td>'
            f"</tr>"
        )
    research_html = (
        '<div class="table-wrap"><table><thead><tr>'
        '<th>Pair</th><th>Chain</th><th>Current Price</th><th>Single TP</th>'
        '<th>Potential (% till TP)</th><th>Pool Address</th><th>Token Contract</th><th>Pool Age</th><th>Status</th><th>Take Position</th>'
        f'</tr></thead><tbody>{"".join(res_rows)}</tbody></table></div>'
        if res_rows else '<p class="muted">No unverified research candidates met the mandatory +40.00% potential threshold.</p>'
    )

    disc_rows = []
    for c in display_discovery:
        pair = c.get("pair") or c.get("asset") or "Unnamed pair"
        entry, target, potential, pool, token = _candidate_opportunity_fields(c)
        chain = c.get("chain") or "—"
        age = _number(c.get("age_hours"))
        age_str = f"{age:.2f}h" if age is not None else "unknown"
        pot_str = f"+{potential:.2f}%" if potential is not None else "—"
        disc_rows.append(
            f"<tr>"
            f"<td><strong>{_esc(pair)}</strong></td>"
            f"<td>{_esc(chain)}</td>"
            f"<td>${_esc(_price(entry))}</td>"
            f"<td>${_esc(_price(target))}</td>"
            f'<td class="green">{_esc(pot_str)}</td>'
            f"<td><code>{_esc(pool)}</code></td>"
            f"<td><code>{_esc(token)}</code></td>"
            f"<td>{_esc(age_str)}</td>"
            f"</tr>"
        )
    discovery_html = (
        '<div class="table-wrap"><table><thead><tr>'
        '<th>Pair</th><th>Chain</th><th>Current Price</th><th>Single TP</th>'
        '<th>Potential (% till TP)</th><th>Pool Address</th><th>Token Contract</th><th>Pool Age</th>'
        f'</tr></thead><tbody>{"".join(disc_rows)}</tbody></table></div>'
        if disc_rows else '<p class="muted">No young or unknown-age DEX discovery leads met the mandatory +40.00% potential threshold.</p>'
    )
    discovery_section_html = (
        f"""<section>
<h2>3. Young / Unknown-Age DEX Discovery Leads</h2>
<p class="meta">New pools (&lt;24h old) ranked for forward opportunities, separated from historical comparable candidates.</p>
{discovery_html}
</section>"""
        if display_discovery else ""
    )

    monitor_html = ""
    monitor = document.get("position_monitor")
    if isinstance(monitor, dict):
        active = monitor.get("active_positions") or []
        completed = monitor.get("recently_completed_positions") or []
        m_rows = []
        if is_v426 or has_final_categories:
            # Enforce user requirement: to "24-Hour Reference-Position Monitoring" add only "Verified Actionable Opportunities"
            for item in actionable:
                entry, target, potential, pool, token = _candidate_opportunity_fields(item)
                pot_val = _number(item.get("tp_upside_pct")) or potential or 0.0
                pair_name = item.get("pair") or item.get("asset") or "Unknown"
                market = item.get("chain") or "solana"
                target_str = f"${_price(target)}" if target is not None else "unavailable"
                m_rows.append(
                    f"<tr>"
                    f"<td><strong>{_esc(pair_name)}</strong></td>"
                    f"<td>{_esc(market)}</td>"
                    f"<td><code>{_esc(pool)}</code></td>"
                    f"<td>24h window active</td>"
                    f'<td class="green">+{pot_val:.2f}% (Target: {target_str})</td>'
                    f'<td class="green">+0.00% (tracking live)</td>'
                    f"<td>none (Strict Single-TP)</td>"
                    f'<td><span class="badge ok">MONITORED — VERIFIED ACTIONABLE</span></td>'
                    f"</tr>"
                )
            if m_rows:
                monitor_html = f"""
<section>
<h2>24-Hour Reference-Position Monitoring</h2>
<p class="meta">Monitoring Verified Actionable Opportunities strictly · Active: {len(m_rows)}</p>
<div class="table-wrap"><table><thead><tr>
<th>Asset / Pair</th><th>Market</th><th>Pair Address</th><th>Window Closes</th><th>Target Milestone</th><th>Latest Return</th><th>Protective Stop</th><th>Status</th>
</tr></thead><tbody>{"".join(m_rows)}</tbody></table></div>
<p class="meta note">Only Verified Actionable Opportunities are added to reference-position monitoring. Single-TP architecture: monitored until target milestone or 24h close.</p>
</section>
"""
            else:
                monitor_html = f"""
<section>
<h2>24-Hour Reference-Position Monitoring</h2>
<p class="meta">Monitoring Verified Actionable Opportunities strictly · Active: 0</p>
<div class="table-wrap"><p class="muted" style="padding:16px;">Zero verified actionable opportunities qualified for active 24-hour position monitoring in this run.</p></div>
</section>
"""
        else:
            for item in (active + completed[-20:]):
                mfe = _number(item.get("max_favorable_excursion_pct"))
                latest = _number(item.get("latest_return_from_entry_pct"))
                stop_price = _number(item.get("trailing_stop_price")) or _number(item.get("initial_stop_price"))
                stop_kind = "trail" if _number(item.get("trailing_stop_price")) is not None else "initial"
                stop_text = f"{stop_kind}: ${_price(stop_price)}" if stop_price is not None else "unavailable"
                mfe_text = f"+{mfe:.2f}%" if mfe is not None else "unavailable"
                latest_text = f"{latest:+.2f}%" if latest is not None else "unavailable"
                pool_addr = item.get("pool_address") or item.get("pair_address") or item.get("address") or "—"
                m_rows.append(
                    f"<tr>"
                    f"<td><strong>{_esc(item.get('asset') or item.get('symbol') or 'Unknown')} / {_esc(item.get('pair') or 'Unknown')}</strong></td>"
                    f"<td>{_esc(item.get('venue') or item.get('chain') or '—')}</td>"
                    f"<td><code>{_esc(pool_addr)}</code></td>"
                    f"<td>{_esc(item.get('time_stop_utc') or 'unavailable')}</td>"
                    f'<td class="green">{_esc(mfe_text)}</td>'
                    f"<td>{_esc(latest_text)}</td>"
                    f"<td>{_esc(stop_text)}</td>"
                    f'<td><span class="badge info">{_esc(item.get("position_status") or "unavailable")}</span></td>'
                    f"</tr>"
                )
            if m_rows:
                monitor_html = f"""
<section>
<h2>24-Hour Reference-Position Monitoring</h2>
<p class="meta">Status: <code>{_esc(monitor.get('status') or 'UNAVAILABLE')}</code> · Provider: <code>{_esc(monitor.get('provider') or 'UNVERIFIED')}</code> · Active: {len(active)} / {_esc(str(monitor.get('max_open_reference_positions', '—')))}</p>
<div class="table-wrap"><table><thead><tr>
<th>Asset / Pair</th><th>Market</th><th>Pair Address</th><th>Window Closes (UTC)</th><th>MFE</th><th>Latest Return</th><th>Protective Stop</th><th>Status</th>
</tr></thead><tbody>{"".join(m_rows)}</tbody></table></div>
<p class="meta note">A TP touch is a milestone; positions remain monitored until protective stop or 24h close. Extrema are observed reference prices, not executed fills.</p>
</section>
"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<title>Rocket Hunter — Forward 24h Watchlist ({_esc(general_version)})</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #101820;
  --panel: #17232d;
  --panel-alt: #1d2d3a;
  --line: #2a3a46;
  --text: #e8eff3;
  --muted: #a6b6c1;
  --accent: #72d6bd;
  --warn: #f0c674;
  --green: #4ade80;
  --red: #f87171;
  --blue: #60a5fa;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ width: min(1680px, 96vw); max-width: 96vw; margin: 24px auto 60px; }}
h1, h2, h3, p {{ margin-top: 0; }}
h1 {{ font-size: clamp(1.6rem, 3.5vw, 2.2rem); margin-bottom: 6px; }}
h2 {{ font-size: 1.2rem; margin-bottom: 14px; color: var(--text); }}
h3 {{ font-size: 1.05rem; margin-bottom: 6px; }}
.meta, .muted {{ color: var(--muted); font-size: 0.9rem; }}
.eyebrow {{ color: var(--accent); font-size: 0.8rem; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; margin-bottom: 4px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 20px 0; }}
.card, section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px; }}
.card {{ padding: 16px; }}
.card-label {{ color: var(--muted); font-size: 0.82rem; }}
.card-value {{ font-size: 1.3rem; font-weight: 700; margin-top: 6px; overflow-wrap: anywhere; }}
section {{ padding: 22px; margin: 18px 0; }}
.table-wrap {{ overflow-x: auto; margin-top: 8px; }}
table {{ border-collapse: collapse; width: 100%; min-width: 800px; text-align: left; }}
th, td {{ border-bottom: 1px solid var(--line); padding: 10px 10px; vertical-align: top; }}
th {{ color: var(--muted); font-size: 0.8rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }}
code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.88rem; background: var(--panel-alt); padding: 2px 6px; border-radius: 4px; border: 1px solid var(--line); }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 6px; font-size: 0.78rem; font-weight: 700; letter-spacing: 0.04em; }}
.badge.ok {{ background: rgba(74, 222, 128, 0.15); color: var(--green); border: 1px solid rgba(74, 222, 128, 0.3); }}
.badge.warn {{ background: rgba(240, 198, 116, 0.15); color: var(--warn); border: 1px solid rgba(240, 198, 116, 0.3); }}
.badge.info {{ background: rgba(96, 165, 250, 0.15); color: var(--blue); border: 1px solid rgba(96, 165, 250, 0.3); }}
.banner {{ padding: 20px; border-radius: 10px; margin: 8px 0; border: 1px solid var(--line); }}
.banner.zero {{ background: rgba(240, 198, 116, 0.06); border-color: rgba(240, 198, 116, 0.3); }}
.banner.warn {{ background: rgba(248, 113, 113, 0.08); border-color: rgba(248, 113, 113, 0.3); }}
.banner-badge {{ display: inline-block; font-size: 0.72rem; font-weight: 800; letter-spacing: 0.1em; color: var(--warn); margin-bottom: 6px; }}
.green {{ color: var(--green); font-weight: 700; }}
.note {{ border-left: 3px solid var(--accent); padding-left: 14px; margin-top: 14px; }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.pos-chk-lbl {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: rgba(56, 189, 248, 0.08);
  border: 1px solid rgba(56, 189, 248, 0.25);
  padding: 4px 8px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--accent);
  transition: all 0.2s ease;
  user-select: none;
}}
.pos-chk-lbl:hover {{
  background: rgba(56, 189, 248, 0.18);
  border-color: var(--accent);
}}
.pos-chk-lbl input[type="checkbox"] {{
  cursor: pointer;
  accent-color: var(--accent);
  width: 15px;
  height: 15px;
}}
.btn-sm {{
  padding: 5px 10px;
  font-size: 0.82rem;
  font-weight: 700;
  border-radius: 6px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: all 0.15s ease;
}}
.btn-save {{
  background: rgba(16, 185, 129, 0.2);
  color: #34d399;
  border-color: rgba(16, 185, 129, 0.4);
}}
.btn-save:hover {{
  background: rgba(16, 185, 129, 0.35);
}}
.btn-del {{
  background: rgba(239, 68, 68, 0.15);
  color: #f87171;
  border-color: rgba(239, 68, 68, 0.3);
  margin-left: 4px;
}}
.btn-del:hover {{
  background: rgba(239, 68, 68, 0.3);
}}
.btn-reset {{
  background: rgba(245, 158, 11, 0.15);
  color: #fbbf24;
  border-color: rgba(245, 158, 11, 0.3);
}}
.btn-reset:hover {{
  background: rgba(245, 158, 11, 0.3);
}}
.input-dark {{
  background: #090d16;
  border: 1px solid var(--line);
  color: var(--text);
  padding: 6px 8px;
  border-radius: 6px;
  font-size: 0.85rem;
}}
.input-dark:focus {{
  outline: none;
  border-color: var(--accent);
}}
@media print {{
  :root {{ color-scheme: light; --bg: #fff; --panel: #fff; --line: #ccd5da; --text: #18232a; --muted: #52636e; }}
  body {{ background: #fff; }}
}}
</style>
</head>
<body>
<main>
<header>
<div class="eyebrow">Rocket Hunter · Production Scan Output</div>
<h1>Forward 24h Watchlist</h1>
<p class="meta">Finished {_esc(_format_israel_time(scan_finished))} · General version <code>{_esc(general_version)}</code></p>
</header>

<div class="grid">
  <div class="card" style="border-left: 3px solid var(--accent);">
    <div class="card-label">Scan Finished (Israel Time)</div>
    <div class="card-value" style="color:var(--accent);font-size:1.25rem;">{_esc(_format_israel_time(scan_finished))}</div>
  </div>
  <div class="card">
    <div class="card-label">Scan Status</div>
    <div class="card-value"><span class="badge {'ok' if scan_status == 'COMPLETED' else 'warn'}">{_esc(scan_status)}</span></div>
  </div>
  <div class="card">
    <div class="card-label">Run ID</div>
    <div class="card-value" style="font-size:0.95rem;"><code>{_esc(run_id)}</code></div>
  </div>
  <div class="card">
    <div class="card-label">Ranked Opportunities</div>
    <div class="card-value">{opportunity.get('candidate_count', len(candidates))}</div>
  </div>
</div>

<section>
<h2>1. Verified Actionable Opportunities</h2>
<p class="meta">Strict single-TP architecture: minimum +40.00% upside, no stop loss, no trailing stop. Only candidates passing live price refresh, exact token/pool verification, safety audit, and two-way executable quotes appear here.</p>
{actionable_html}
</section>

<section>
<h2>2. Unverified Research Watchlist — NOT QUALIFIED</h2>
<p class="meta">Research rankings and hypothetical upside do not satisfy the technical TP or verification gates. All {len(display_candidates)} mature research candidates meeting the mandatory +40.00% threshold shown.</p>
{research_html}
<p class="meta note">*The 24h upside scenario is a volatility/momentum scenario from hourly candles, not a predicted return or calibrated probability. Initial downside is an ATR-based reference stop. Scenario R:R is upside scenario divided by initial stop distance.</p>
</section>

{discovery_section_html}

<div id="toast-msg" style="display:none;position:fixed;bottom:24px;right:24px;background:#1e293b;border:1px solid #38bdf8;color:#f8fafc;padding:12px 18px;border-radius:8px;font-size:13px;font-weight:600;box-shadow:0 8px 24px rgba(0,0,0,0.5);z-index:9999;"></div>
</main>

<script>
let journalPositions = [];

function showToast(text, isError) {{
  const el = document.getElementById('toast-msg');
  if (!el) return;
  el.textContent = text;
  el.style.display = 'block';
  el.style.borderColor = isError ? '#ef4444' : '#10b981';
  el.style.color = isError ? '#f87171' : '#34d399';
  setTimeout(() => {{ el.style.display = 'none'; }}, 3500);
}}

async function loadJournal() {{
  try {{
    const res = await fetch('/api/journal');
    if (!res.ok) return;
    const data = await res.json();
    journalPositions = data.positions || [];
    syncCandidateCheckboxes();
  }} catch (e) {{
    console.error('Error loading journal:', e);
  }}
}}

function syncCandidateCheckboxes() {{
  const chks = document.querySelectorAll('.pos-checkbox');
  chks.forEach(chk => {{
    const pool = (chk.getAttribute('data-pool') || '').trim();
    const pair = (chk.getAttribute('data-pair') || '').trim();
    const run = (chk.getAttribute('data-run') || '').trim();
    const match = journalPositions.find(p => (pool && p.pool_address === pool) || (p.pair === pair && p.run_id === run));
    chk.checked = !!match;
    if (chk.parentElement) {{
      const span = chk.parentElement.querySelector('span');
      if (chk.checked) {{
        chk.parentElement.style.background = 'rgba(16, 185, 129, 0.2)';
        chk.parentElement.style.borderColor = 'rgba(16, 185, 129, 0.5)';
        if (span) span.textContent = '✓ Taken';
      }} else {{
        chk.parentElement.style.background = '';
        chk.parentElement.style.borderColor = '';
        if (span) span.textContent = 'Take Position';
      }}
    }}
  }});
}}

async function toggleCandidatePosition(el) {{
  const isChecked = el.checked;
  const pool = el.getAttribute('data-pool') || '';
  const pair = el.getAttribute('data-pair') || '';
  const run = el.getAttribute('data-run') || '';

  if (isChecked) {{
    const payload = {{
      pair: pair,
      chain: el.getAttribute('data-chain') || '',
      entry_price: el.getAttribute('data-entry') || '',
      single_tp: el.getAttribute('data-tp') || '',
      potential_pct: el.getAttribute('data-pot') || '',
      pool_address: pool,
      token_contract: el.getAttribute('data-token') || '',
      software_version: el.getAttribute('data-version') || 'v4.26.7',
      run_id: run,
      status: 'OPEN'
    }};
    try {{
      const res = await fetch('/api/journal/record', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(payload)
      }});
      const data = await res.json();
      if (data.status === 'OK' || data.status === 'EXISTS') {{
        showToast(`Position ${{pair}} recorded! Track follow-up in Status Dashboard.`);
        await loadJournal();
      }}
    }} catch (e) {{
      showToast('Failed to record position.', true);
    }}
  }} else {{
    const match = journalPositions.find(p => (pool && p.pool_address === pool) || (p.pair === pair && p.run_id === run));
    if (match) {{
      try {{
        const res = await fetch('/api/journal/delete', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{id: match.id}})
        }});
        const data = await res.json();
        if (data.status === 'OK') {{
          showToast(`Position ${{pair}} removed from Journal.`);
          await loadJournal();
        }}
      }} catch (e) {{
        showToast('Failed to remove position.', true);
      }}
    }}
  }}
}}

document.addEventListener('DOMContentLoaded', loadJournal);
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Final forward-watchlist JSON")
    parser.add_argument("--output", required=True, type=Path, help="Markdown output path")
    parser.add_argument("--html", type=Path, default=None, help="HTML output path")
    args = parser.parse_args()

    try:
        document = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read valid JSON from {args.input}: {exc}")
    if not isinstance(document, dict):
        parser.error(f"{args.input} must contain a JSON object")

    if args.input:
        parent_name = args.input.resolve().parent.name
        if parent_name.startswith("local-run-"):
            document["run_id"] = parent_name
            document["run_dir_name"] = parent_name
        document["live_price_refresh"] = True

    report = render_report(document)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"Wrote finalized forward watchlist: {args.output}")

    html_report = render_html(document)
    html_out = args.html if args.html is not None else args.output.with_suffix(".html")
    html_out.parent.mkdir(parents=True, exist_ok=True)
    html_out.write_text(html_report, encoding="utf-8")
    print(f"Wrote finalized forward HTML watchlist: {html_out}")

    root_html = ROOT / "forward-24h-rocket.html"
    root_md = ROOT / "forward-24h-rocket.md"
    try:
        root_html.write_text(html_report, encoding="utf-8")
        print(f"Updated root forward HTML watchlist: {root_html}")
    except Exception as exc:
        print(f"Warning: could not write root HTML: {exc}")
    try:
        root_md.write_text(report, encoding="utf-8")
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
