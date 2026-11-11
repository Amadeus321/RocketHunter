"""Fail-closed DEX entry guard for launch, wallet, proxy and LP rug-pull patterns.

This module does not discover on-chain wallet graphs itself. It validates fresh,
source-attributed evidence produced by a chain-aware provider. Missing evidence
blocks entry readiness but does not remove a candidate from research ranking.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

try:
    from scripts.address_identity import same_address
except ImportError:
    from address_identity import same_address

GUARD_VERSION = "1.0.0"
MAX_EVIDENCE_AGE_MINUTES = 60.0
MAX_ROUTE_AGE_MINUTES = 5.0
MIN_POOL_AGE_HOURS = 2.0
MIN_WALLET_GRAPH_COVERAGE_PCT = 80.0
MAX_DEPLOYER_INITIAL_SUPPLY_PCT = 15.0
MAX_LINKED_WALLET_CLUSTER_PCT = 20.0
MIN_LOCKED_LP_PCT = 95.0
MIN_TIMELOCK_HOURS = 24.0

ROUTE_OK_STATUSES = {
    "BUY_AND_SELL_FIRM_QUOTES_RETURNED",
    "BUY_AND_SELL_ROUTES_RETURNED",
}
LP_CONTROL_TYPES = {"BURNED", "LOCKED", "TIMELOCKED_MULTISIG"}
PROXY_UPGRADE_CONTROLS = {"IMMUTABLE", "TIMELOCKED_MULTISIG"}

REQUIRED_SECURITY_ZERO_FIELDS = (
    "is_honeypot", "cannot_buy", "cannot_sell_all", "hidden_owner",
    "can_take_back_ownership", "owner_change_balance", "selfdestruct",
    "is_mintable", "transfer_pausable", "is_blacklisted",
)
REQUIRED_SECURITY_FIELDS = REQUIRED_SECURITY_ZERO_FIELDS + (
    "is_open_source", "is_proxy", "buy_tax", "sell_tax",
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _truth(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "verified", "pass"}


def _explicit_false(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    return isinstance(value, str) and value.strip().lower() in {"0", "false"}


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _fresh(evidence: dict[str, Any], now: datetime, max_age_minutes: float, label: str,
           blockers: list[str]) -> bool:
    if not isinstance(evidence, dict) or not evidence.get("source"):
        blockers.append(label + ": source attribution missing")
        return False
    timestamp = _utc(evidence.get("as_of_utc") or evidence.get("retrieved_utc") or evidence.get("timestamp_utc"))
    if timestamp is None:
        blockers.append(label + ": timestamp missing or invalid")
        return False
    age = (now - timestamp).total_seconds() / 60.0
    if age < -2.0 or age > max_age_minutes:
        blockers.append(label + ": evidence is future-dated or stale")
        return False
    return True


def _verified(evidence: Any) -> bool:
    return isinstance(evidence, dict) and str(evidence.get("status") or "").upper() in {"VERIFIED", "PASS"}


def _security_checks(security_item: dict[str, Any] | None, evidence: dict[str, Any],
                     now: datetime, blockers: list[str]) -> None:
    if not isinstance(security_item, dict):
        blockers.append("independent token-security result missing")
        return
    if str(security_item.get("source") or "").lower() == "rugcheck":
        sol = evidence.get("rugcheck_security") or (security_item.get("solana_security") or {})
        if str(sol.get("status") or "").upper() != "VERIFIED":
            blockers.append("RugCheck Solana security report is incomplete")
            return
        _fresh(sol, now, MAX_EVIDENCE_AGE_MINUTES, "RugCheck token-security audit", blockers)
        if not sol.get("mint_authority_present"):
            blockers.append("RugCheck mint-authority field missing")
        elif sol.get("mint_authority") is not None:
            blockers.append("Solana mint authority remains active or malformed")
        if not sol.get("freeze_authority_present"):
            blockers.append("RugCheck freeze-authority field missing")
        elif sol.get("freeze_authority") is not None:
            blockers.append("Solana freeze authority remains active or malformed")
        if not sol.get("metadata_mutability_present"):
            blockers.append("RugCheck metadata-mutability field missing")
        elif not _explicit_false(sol.get("metadata_mutable")):
            blockers.append("Solana token metadata is mutable or not explicitly immutable")
        critical = sol.get("critical_risks") or []
        if critical:
            blockers.append("RugCheck reports critical token risks: " + ", ".join(
                str(x.get("name") or x.get("risk") or x.get("description") or "CRITICAL_RISK")
                if isinstance(x, dict) else str(x) for x in critical[:8]))
        if not sol.get("risks_field_present"):
            blockers.append("RugCheck risk list is missing")
        return
    audit = security_item.get("audit") or {}
    data = audit.get("data") or {}
    if not isinstance(data, dict) or not data:
        blockers.append("independent token-security result has no structured fields")
        return
    _fresh(security_item, now, MAX_EVIDENCE_AGE_MINUTES, "token-security audit", blockers)
    missing = [field for field in REQUIRED_SECURITY_FIELDS if field not in data or data.get(field) is None]
    if missing:
        blockers.append("token-security fields incomplete: " + ", ".join(missing))
    for field in REQUIRED_SECURITY_ZERO_FIELDS:
        if field in data and str(data.get(field)).strip() not in {"0", "0.0", "false", "False"}:
            blockers.append("token-security flag is active: " + field)
    if "is_open_source" in data and str(data.get("is_open_source")).strip() not in {"1", "1.0", "true", "True"}:
        blockers.append("token source is not verified open-source")
    for field in ("buy_tax", "sell_tax"):
        tax = _number(data.get(field))
        if tax is None:
            blockers.append(field + " is missing or unparsable")
        elif tax > 10.0:
            blockers.append(field + " exceeds 10%")
    if str(data.get("is_proxy", "0")).strip() in {"1", "1.0", "true", "True"}:
        proxy = evidence.get("proxy_implementation") or {}
        if not _verified(proxy):
            blockers.append("proxy implementation/admin review missing")
        else:
            _fresh(proxy, now, MAX_EVIDENCE_AGE_MINUTES, "proxy implementation review", blockers)
            if not proxy.get("implementation_address"):
                blockers.append("proxy implementation address missing")
            if not _truth(proxy.get("admin_verified")):
                blockers.append("proxy admin permissions not independently verified")
            if str(proxy.get("upgradeability_control") or "").upper() not in PROXY_UPGRADE_CONTROLS:
                blockers.append("proxy upgradeability is not immutably or time-lock controlled")
            if str(proxy.get("upgradeability_control") or "").upper() == "TIMELOCKED_MULTISIG":
                delay = _number(proxy.get("timelock_hours"))
                if delay is None or delay < MIN_TIMELOCK_HOURS:
                    blockers.append("proxy timelock is missing or shorter than 24 hours")


def evaluate_dex_candidate(row: dict[str, Any], security_item: dict[str, Any] | None,
                           route_item: dict[str, Any] | None,
                           now: datetime | None = None) -> dict[str, Any]:
    """Return PASS only when every required DEX loss-pattern evidence lane is verified."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    blockers: list[str] = []
    evidence = row.get("loss_guard_evidence") or {}

    chain = str(row.get("chain") or "").strip().lower()
    contract = str(row.get("contract_address") or row.get("token_address") or "").strip()
    pool = str(row.get("pool_address") or row.get("pair_address") or "").strip()
    if not chain:
        blockers.append("chain identity missing")
    if not contract:
        blockers.append("exact token contract/mint missing")
    if not pool:
        blockers.append("exact pool address missing")
    if contract and pool and same_address(chain, contract, pool):
        blockers.append("token contract/mint and pool address are not distinct identities")

    age = _number(row.get("age_hours"))
    if age is None:
        blockers.append("pool age unverified")
    elif age < MIN_POOL_AGE_HOURS:
        blockers.append("pool younger than 2 hours; entry blocked pending persistence")

    pool_identity = evidence.get("pool_identity") or {}
    if not _verified(pool_identity):
        blockers.append("independent token-to-pool identity verification missing")
    else:
        _fresh(pool_identity, current, MAX_EVIDENCE_AGE_MINUTES, "pool identity verification", blockers)
        if str(pool_identity.get("chain") or "").strip().lower() != chain:
            blockers.append("pool identity chain does not match candidate chain")
        if not same_address(chain, pool_identity.get("contract_address"), contract):
            blockers.append("pool identity token contract does not match candidate contract")
        if not same_address(chain, pool_identity.get("pool_address"), pool):
            blockers.append("pool identity address does not match exact candidate pool")
        quote = str(pool_identity.get("quote_token_address") or "").strip()
        if not quote or same_address(chain, quote, contract):
            blockers.append("quote-token identity missing or ambiguous")

    launch = evidence.get("launch_wallet") or {}
    if not _verified(launch):
        blockers.append("launch-allocation and connected-wallet analysis missing")
    else:
        _fresh(launch, current, MAX_EVIDENCE_AGE_MINUTES, "launch-wallet analysis", blockers)
        coverage = _number(launch.get("coverage_pct"))
        deployer_pct = _number(launch.get("deployer_initial_supply_pct"))
        cluster_pct = _number(launch.get("largest_linked_wallet_cluster_pct"))
        if coverage is None or coverage < MIN_WALLET_GRAPH_COVERAGE_PCT:
            blockers.append("wallet graph coverage below 80% or unreported")
        if deployer_pct is None:
            blockers.append("deployer initial-supply allocation unreported")
        elif deployer_pct > MAX_DEPLOYER_INITIAL_SUPPLY_PCT:
            blockers.append("deployer initial-supply allocation exceeds 15%")
        if cluster_pct is None:
            blockers.append("largest connected-wallet supply cluster unreported")
        elif cluster_pct > MAX_LINKED_WALLET_CLUSTER_PCT:
            blockers.append("connected-wallet cluster controls more than 20% of supply")

    lp = evidence.get("lp_position") or {}
    if not _verified(lp):
        blockers.append("exact LP-position ownership/withdrawal audit missing")
    else:
        _fresh(lp, current, MAX_EVIDENCE_AGE_MINUTES, "LP-position audit", blockers)
        if not same_address(chain, lp.get("pool_address"), pool):
            blockers.append("LP-position audit does not identify the exact candidate pool")
        if not _truth(lp.get("position_identity_verified")):
            blockers.append("LP position identity is not independently verified")
        control = str(lp.get("control_type") or "").upper()
        if control not in LP_CONTROL_TYPES:
            blockers.append("LP withdrawal control is not verified as burned, locked or timelocked multisig")
        elif control == "LOCKED":
            locked_pct = _number(lp.get("locked_pct"))
            if locked_pct is None or locked_pct < MIN_LOCKED_LP_PCT:
                blockers.append("verified locked LP share is below 95% or unreported")
        elif control == "TIMELOCKED_MULTISIG":
            delay = _number(lp.get("withdrawal_delay_hours"))
            if delay is None or delay < MIN_TIMELOCK_HOURS:
                blockers.append("LP withdrawal timelock is missing or shorter than 24 hours")
        elif control == "BURNED":
            burned_pct = _number(lp.get("burned_pct") if lp.get("burned_pct") is not None else lp.get("locked_pct"))
            if burned_pct is None or burned_pct < MIN_LOCKED_LP_PCT:
                blockers.append("verified burned LP share is below 95% or unreported")

    _security_checks(security_item, evidence, current, blockers)

    if not isinstance(route_item, dict) or route_item.get("status") not in ROUTE_OK_STATUSES:
        blockers.append("fresh two-way buy and sell route quotes missing")
    else:
        route_ts = _utc(route_item.get("timestamp_utc") or route_item.get("retrieved_utc"))
        if route_ts is None:
            blockers.append("router quote timestamp missing")
        else:
            route_age = (current - route_ts).total_seconds() / 60.0
            if route_age < -1.0 or route_age > MAX_ROUTE_AGE_MINUTES:
                blockers.append("router quotes are future-dated or older than 5 minutes")
        buy = route_item.get("buy_quote") or {}
        sell = route_item.get("sell_quote") or {}
        if not isinstance(buy, dict) or not buy:
            blockers.append("buy-side quote payload missing")
        if not isinstance(sell, dict) or not sell:
            blockers.append("sell-side quote payload missing")
        if isinstance(buy, dict) and not any(_number(buy.get(k)) and _number(buy.get(k)) > 0
                                             for k in ("outAmount", "buy_amount_token_raw", "output_amount_token_raw")):
            blockers.append("buy-side quote has no positive token output")
        if isinstance(sell, dict) and not any(_number(sell.get(k)) and _number(sell.get(k)) > 0
                                              for k in ("outAmount", "buy_amount_usdc_raw", "output_amount_usdc_raw")):
            blockers.append("sell-side quote has no positive stablecoin output")
        route_size = _number(route_item.get("notional_usd"))
        expected_size = _number(evidence.get("expected_notional_usd"))
        if route_size is None or expected_size is None:
            blockers.append("intended position size is not bound to the router quote")
        elif abs(route_size - expected_size) > max(0.01, expected_size * 0.01):
            blockers.append("router quote size differs from intended position size")

    blockers = list(dict.fromkeys(blockers))
    return {
        "schema": "crypto-prebreakout.dex-loss-pattern-guard.v1",
        "guard_version": GUARD_VERSION,
        "status": "PASS" if not blockers else "BLOCKED_UNVERIFIED_OR_RISK",
        "entry_ready": not blockers,
        "blockers": blockers,
        "checks": {
            "exact_token_and_pool_identity": bool(chain and contract and pool and not same_address(chain, contract, pool)),
            "launch_wallet_analysis": _verified(launch),
            "lp_position_audit": _verified(lp),
            "token_security_audit": bool(isinstance(security_item, dict) and (security_item.get("audit") or {}).get("data")),
            "two_way_route_quotes": bool(isinstance(route_item, dict) and route_item.get("status") in ROUTE_OK_STATUSES),
        },
        "policy": {
            "max_deployer_initial_supply_pct": MAX_DEPLOYER_INITIAL_SUPPLY_PCT,
            "max_linked_wallet_cluster_pct": MAX_LINKED_WALLET_CLUSTER_PCT,
            "min_wallet_graph_coverage_pct": MIN_WALLET_GRAPH_COVERAGE_PCT,
            "min_pool_age_hours": MIN_POOL_AGE_HOURS,
            "max_evidence_age_minutes": MAX_EVIDENCE_AGE_MINUTES,
            "max_route_age_minutes": MAX_ROUTE_AGE_MINUTES,
        },
        "note": "Research ranking is unaffected. Missing, stale, mismatched or adverse evidence blocks DEX entry readiness; this is not a guarantee against loss.",
    }
