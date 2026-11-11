#!/usr/bin/env python3
"""v4.24.0 — free-tier Birdeye multichain discovery with a hard monthly CU guard."""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BIRDEYE_VERSION = "4.24.0"
API_ROOT = "https://public-api.birdeye.so"
REGISTRY_TTL_SECONDS = 7 * 24 * 60 * 60
# Standard (Free) currently includes 30,000 CUs/month. The scanner stops at
# 20,000 estimated CUs to reserve at least 10,000 for account-level variance.
SCANNER_MONTHLY_CU_CEILING = 20_000
ENDPOINT_COSTS = {"token_trending": 40, "new_listing": 30, "tokenlist": 30}
DISCOVERY_FAMILIES = ("token_trending", "new_listing", "tokenlist")
USER_AGENT = "crypto-prebreakout-research/v4.24.0 (read-only free-tier discovery)"


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _memory_root(hunter: Any) -> Path:
    configured = os.environ.get("SCANNER_MEMORY_DIR")
    if configured:
        return Path(configured)
    checkpoint = getattr(hunter, "checkpoint_dir", None)
    if checkpoint:
        return Path(checkpoint) / "provider-memory"
    return Path("rocket-run/provider-memory")


def _fetch_json(url: str, api_key: str, chain: str | None = None,
                timeout: int = 8) -> tuple[Any, int | None, str | None]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "X-API-KEY": api_key,
    }
    if chain:
        headers["x-chain"] = chain
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=max(1, int(timeout))) as response:
            status = int(response.status)
            payload = json.loads(response.read().decode("utf-8"))
        return payload, status, None
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(500).decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        return None, int(exc.code), ("HTTP %s %s" % (exc.code, detail)).strip()
    except Exception as exc:
        return None, None, "%s: %s" % (type(exc).__name__, exc)


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("tokens", "items", "results", "list", "records", "networks", "chains"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    for key in ("tokens", "items", "results", "list", "records", "networks", "chains"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def parse_birdeye_networks(payload: Any) -> list[dict[str, str]]:
    rows = _extract_records(payload)
    if not rows and isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            rows = [{"name": value} for value in data if isinstance(value, str)]
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        value = None
        for key in ("chain", "network", "chainId", "chain_id", "slug", "id", "name", "value"):
            if row.get(key) is not None:
                value = row.get(key)
                break
        if value is None:
            continue
        if isinstance(value, dict):
            value = value.get("slug") or value.get("id") or value.get("name") or value.get("chain")
        chain = str(value or "").strip()
        key = chain.casefold()
        if chain and key not in seen:
            seen.add(key)
            out.append({"chain": chain, "display_name": str(row.get("displayName") or row.get("display_name") or row.get("name") or chain)})
    return out


def parse_birdeye_token_rows(payload: Any) -> list[dict[str, Any]]:
    return _extract_records(payload)


def _number(*values: Any) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            number = float(value)
            if number == number and abs(number) != float("inf"):
                return number
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def normalize_birdeye_token(row: dict[str, Any], chain: str, family: str,
                             base_record_fn, source: str) -> dict[str, Any] | None:
    token = row.get("token") if isinstance(row.get("token"), dict) else {}
    merged = dict(token)
    merged.update(row)
    address = (merged.get("address") or merged.get("tokenAddress")
               or merged.get("token_address") or merged.get("mint"))
    if not address:
        return None
    name = merged.get("name") or merged.get("tokenName") or merged.get("symbol") or "UNKNOWN"
    symbol = merged.get("symbol") or merged.get("tokenSymbol") or ""
    price = _number(merged.get("price"), merged.get("priceUsd"), merged.get("price_usd"))
    change_24h = _number(
        merged.get("priceChange24hPercent"), merged.get("price_change_24h_percent"),
        merged.get("v24hChangePercent"), merged.get("change24hPercent"),
        merged.get("change_24h_pct"), merged.get("priceChange24h"),
    )
    volume_24h = _number(
        merged.get("volume24hUSD"), merged.get("v24hUSD"), merged.get("volume24h"),
        merged.get("volume_24h_usd"), merged.get("volume_24h"), merged.get("volumeUSD"),
    )
    liquidity = _number(merged.get("liquidity"), merged.get("liquidityUsd"), merged.get("liquidity_usd"))
    market_cap = _number(merged.get("mc"), merged.get("marketcap"), merged.get("marketCap"), merged.get("market_cap"))
    fdv = _number(merged.get("fdv"), merged.get("fdvUsd"), merged.get("fdv_usd"))
    extra = {
        "birdeye_family": family,
        "birdeye_rank": merged.get("rank"),
        "birdeye_volume_24h_usd": volume_24h,
        "birdeye_liquidity_usd": liquidity,
        "birdeye_last_trade_unix_time": merged.get("lastTradeUnixTime"),
        "birdeye_source_id": merged.get("id"),
        "discovery_lane": "birdeye_%s" % family,
    }
    return base_record_fn(
        identity="dex:%s:%s" % (chain.lower(), str(address).lower()),
        asset=name,
        symbol=symbol,
        source=source,
        market_type="dex_token",
        change_24h=change_24h,
        price_usd=price,
        volume_24h_usd=volume_24h,
        liquidity_usd=liquidity,
        market_cap_usd=market_cap,
        fdv_usd=fdv,
        chain=chain,
        address=str(address),
        retrieved_utc=utc_iso(),
        extra=extra,
    )


def _load_network_registry(hunter: Any, api_key: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
    memory = _memory_root(hunter)
    path = memory / "provider-registries" / "birdeye-networks.json"
    now = time.time()
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            age = max(0.0, now - float(saved.get("fetched_epoch") or 0))
            networks = parse_birdeye_networks(saved.get("payload"))
            if networks and age <= REGISTRY_TTL_SECONDS:
                _record_status(hunter, "birdeye_supported_networks", "CACHED",
                               "https://public-api.birdeye.so/defi/networks", len(networks),
                               reason="seven-day persistent provider registry cache",
                               extra={"cache_age_seconds": round(age, 1)})
                return networks, {"registry_complete": True, "registry_cached": True,
                                  "registry_age_seconds": round(age, 1)}
        except Exception:
            pass

    url = API_ROOT + "/defi/networks"
    # Network registry costs 1 CU. Reserve it before sending the request so a
    # failed response cannot be used to evade the conservative usage ceiling.
    if not _reserve_cu(memory, 1, "supported_networks"):
        _record_status(hunter, "birdeye_supported_networks", "SKIPPED", url,
                       reason="v4.24 scanner CU ceiling reached")
        return [], {"registry_complete": False, "registry_cached": False, "budget_blocked": True}
    payload, status, error = _fetch_json(url, api_key, timeout=getattr(getattr(hunter, "fx", None), "timeout", 8))
    networks = parse_birdeye_networks(payload)
    _record_status(hunter, "birdeye_supported_networks", "OK" if networks else "FAILED",
                   url, len(networks), error,
                   extra={"http_status": status, "registry_complete": bool(networks)})
    if networks:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "provider": "birdeye",
            "fetched_utc": utc_iso(),
            "fetched_epoch": now,
            "registry_complete": True,
            "payload": [{"chain": item["chain"], "display_name": item["display_name"]} for item in networks],
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return networks, {"registry_complete": bool(networks), "registry_cached": False,
                      "registry_age_seconds": 0, "registry_http_status": status}


def _read_cu_ledger(memory: Path) -> dict[str, Any]:
    path = memory / "birdeye-cu-ledger.json"
    month = utc_month()
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("month_utc") == month:
                return saved
        except Exception:
            pass
    return {
        "schema": "crypto-prebreakout.birdeye-cu-ledger.v1",
        "month_utc": month,
        "estimated_cu_reserved": 0,
        "scanner_monthly_cu_ceiling": SCANNER_MONTHLY_CU_CEILING,
        "request_count": 0,
        "requests_by_family": {},
        "last_updated_utc": utc_iso(),
    }


def _reserve_cu(memory: Path, cost: int, family: str) -> bool:
    memory.mkdir(parents=True, exist_ok=True)
    shared = Path(os.environ.get("ROCKET_HUNTER_SHARED_MEMORY_DIR") or memory).resolve()
    shared.mkdir(parents=True, exist_ok=True)
    try:
        from scripts.file_lock import locked_file
    except ImportError:
        from file_lock import locked_file
    with locked_file(shared / ".birdeye-cu.lock"):
        state = _read_cu_ledger(shared)
        used = int(state.get("estimated_cu_reserved") or 0)
        if used + int(cost) > SCANNER_MONTHLY_CU_CEILING:
            return False
        state["estimated_cu_reserved"] = used + int(cost)
        state["request_count"] = int(state.get("request_count") or 0) + 1
        by_family = state.get("requests_by_family") if isinstance(state.get("requests_by_family"), dict) else {}
        by_family[family] = int(by_family.get(family) or 0) + 1
        state["requests_by_family"] = by_family
        state["last_updated_utc"] = utc_iso()
        encoded = json.dumps(state, indent=2, sort_keys=True) + "\n"
        for target in {shared, Path(memory).resolve()}:
            target.mkdir(parents=True, exist_ok=True)
            (target / "birdeye-cu-ledger.json").write_text(encoded, encoding="utf-8")
        return True


def _record_status(hunter: Any, source: str, status: str, url: str | None = None,
                   row_count: int | None = None, error: str | None = None,
                   reason: str | None = None, extra: dict[str, Any] | None = None) -> None:
    record = {
        "source": source,
        "status": status,
        "url": url,
        "row_count": row_count,
        "retrieved_utc": utc_iso(),
        "error": error,
        "reason": reason,
    }
    if extra:
        record.update(extra)
    hunter.source_status.append(record)


def _make_rotation_slots(networks: list[dict[str, str]]) -> list[tuple[dict[str, str], str]]:
    slots: list[tuple[dict[str, str], str]] = []
    for network in networks:
        chain = network["chain"]
        for family in DISCOVERY_FAMILIES:
            if family == "new_listing" and chain.casefold() == "sui":
                continue
            slots.append((network, family))
    return slots


def _family_url(family: str) -> str:
    if family == "token_trending":
        params = {"sort_by": "rank", "sort_type": "asc", "interval": "1h", "offset": 0, "limit": 50}
        return API_ROOT + "/defi/token_trending?" + urllib.parse.urlencode(params)
    if family == "new_listing":
        return API_ROOT + "/defi/v2/tokens/new_listing?" + urllib.parse.urlencode({"limit": 20})
    params = {
        "sort_by": "v24hChangePercent",
        "sort_type": "desc",
        "offset": 0,
        "limit": 50,
        "min_liquidity": 0,
    }
    return API_ROOT + "/defi/tokenlist?" + urllib.parse.urlencode(params)


def install_birdeye_hooks(mod: Any) -> None:
    if getattr(mod, "_V424_BIRDEYE_HOOKS_INSTALLED", False):
        return
    Hunter = getattr(mod, "RocketHunter", None)
    if Hunter is None or not hasattr(Hunter, "run") or not hasattr(Hunter, "add_optional_birdeye"):
        raise RuntimeError("RocketHunter.run/add_optional_birdeye required before installing v4.24 hooks")
    original_add = Hunter.add_optional_birdeye
    original_run = Hunter.run

    def patched_add_optional_birdeye(self, api_key: str | None, limit: int = 50):
        if not api_key:
            _record_status(
                self, "birdeye_multichain_discovery", "SKIPPED",
                reason="optional free Standard API key not configured; set GitHub Actions secret BIRDEYE_API_KEY; no paid tier is used",
            )
            self.birdeye_coverage_v424 = {
                "api_key_configured": False,
                "registry_complete": False,
                "supported_network_count": 0,
                "selected_chain": None,
                "selected_family": None,
                "request_status": "SKIPPED",
            }
            return

        memory = _memory_root(self)
        networks, registry_meta = _load_network_registry(self, api_key)
        if not networks:
            self.birdeye_coverage_v424 = {
                "api_key_configured": True,
                "registry_complete": False,
                "supported_network_count": 0,
                "selected_chain": None,
                "selected_family": None,
                "request_status": "REGISTRY_UNAVAILABLE",
                **registry_meta,
            }
            return

        slots = _make_rotation_slots(networks)
        rotation_path = memory / "birdeye-rotation-state.json"
        try:
            rotation_state = json.loads(rotation_path.read_text(encoding="utf-8")) if rotation_path.exists() else {}
        except Exception:
            rotation_state = {}
        slot_index = int(rotation_state.get("next_slot") or 0) % max(1, len(slots))
        selected_network, family = slots[slot_index]
        chain = selected_network["chain"]
        url = _family_url(family)
        cost = ENDPOINT_COSTS[family]
        if not _reserve_cu(memory, cost, family):
            _record_status(self, "birdeye_%s:%s" % (family, chain), "SKIPPED", url,
                           reason="v4.24 internal free-tier safety ceiling of %s CU/month reached" % SCANNER_MONTHLY_CU_CEILING,
                           extra={"selected_chain": chain, "estimated_cu": cost})
            self.birdeye_coverage_v424 = {
                "api_key_configured": True,
                "registry_complete": True,
                "supported_network_count": len(networks),
                "selected_chain": chain,
                "selected_family": family,
                "request_status": "BUDGET_BLOCKED",
                "estimated_cu_reserved": _read_cu_ledger(memory).get("estimated_cu_reserved"),
                **registry_meta,
            }
            return

        try:
            from scripts.file_lock import shared_rate_limit
        except ImportError:
            from file_lock import shared_rate_limit
        shared_memory = Path(os.environ.get("ROCKET_HUNTER_SHARED_MEMORY_DIR") or memory).resolve()
        shared_rate_limit(shared_memory / ".birdeye-request-rate.json", 1.05)
        payload, status, error = _fetch_json(
            url, api_key, chain=chain,
            timeout=getattr(getattr(self, "fx", None), "timeout", 8),
        )
        rows = parse_birdeye_token_rows(payload)
        base_record_fn = getattr(mod, "_base_record")
        added = 0
        for item in rows:
            record = normalize_birdeye_token(
                item, chain, family, base_record_fn,
                source="birdeye_%s" % family,
            )
            if record is None:
                continue
            self.candidates.append(record)
            added += 1

        _record_status(
            self, "birdeye_%s:%s" % (family, chain),
            "OK" if payload is not None else "FAILED",
            url, len(rows), error,
            extra={
                "http_status": status,
                "selected_chain": chain,
                "discovery_family": family,
                "records_added": added,
                "estimated_cu_reserved": cost,
                "rotation_slot": slot_index,
                "rotation_slots_total": len(slots),
            },
        )
        rotation_state.update({
            "schema": "crypto-prebreakout.birdeye-rotation.v1",
            "provider_version": BIRDEYE_VERSION,
            "last_run_id": os.environ.get("GITHUB_RUN_ID", "local-run"),
            "last_updated_utc": utc_iso(),
            "supported_network_count": len(networks),
            "rotation_slots_total": len(slots),
            "last_slot": slot_index,
            "last_selected_chain": chain,
            "last_selected_family": family,
            "next_slot": (slot_index + 1) % max(1, len(slots)),
            "last_request_status": "OK" if payload is not None else "FAILED",
            "last_record_count": len(rows),
            "last_records_added": added,
        })
        memory.mkdir(parents=True, exist_ok=True)
        rotation_path.write_text(json.dumps(rotation_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.birdeye_coverage_v424 = {
            "api_key_configured": True,
            "registry_complete": bool(registry_meta.get("registry_complete")),
            "registry_cached": bool(registry_meta.get("registry_cached")),
            "supported_network_count": len(networks),
            "networks": [network["chain"] for network in networks],
            "selected_chain": chain,
            "selected_family": family,
            "request_status": "OK" if payload is not None else "FAILED",
            "records_received": len(rows),
            "records_added": added,
            "rotation_slot": slot_index,
            "rotation_slots_total": len(slots),
            "estimated_cu_reserved": cost,
            "scanner_monthly_cu_ceiling": SCANNER_MONTHLY_CU_CEILING,
            "registry_meta": registry_meta,
        }

    def patched_run(self, *args, **kwargs):
        result = original_run(self, *args, **kwargs)
        details = getattr(self, "birdeye_coverage_v424", None)
        if isinstance(details, dict):
            coverage = result.setdefault("coverage", {})
            registries = coverage.setdefault("provider_registries_v423", {})
            registries["birdeye"] = {
                "networks": details.get("networks") or [],
                "network_count": details.get("supported_network_count", 0),
                "selected_network": details.get("selected_chain"),
                "selected_family": details.get("selected_family"),
                "registry_complete": details.get("registry_complete", False),
                "registry_available": bool(details.get("api_key_configured")),
                "registry_scope": "Birdeye /defi/networks supported-chain registry. One rotating trending/new-listing/tokenlist discovery page is queried per full scan; registry presence is not token/pool census.",
                "request_status": details.get("request_status"),
            }
            roles = coverage.setdefault("provider_roles_v423", {})
            roles["birdeye"] = "free Standard tier multichain trending, newly listed and token-directory feeds; one chain/family slot per full scan, rotating across the supported-chain registry"
            coverage["birdeye_discovery_v424"] = {
                key: value for key, value in details.items() if key != "networks"
            }
            result["birdeye_discovery_v424"] = coverage["birdeye_discovery_v424"]
        return result

    Hunter.add_optional_birdeye = patched_add_optional_birdeye
    Hunter.run = patched_run
    mod._V424_BIRDEYE_HOOKS_INSTALLED = True


def _merge_birdeye_post_scan(outdir: Path, memory_dir: Path, run_id: str) -> dict[str, Any]:
    report_path = outdir / "rocket-hunt.json"
    main = json.loads(report_path.read_text(encoding="utf-8"))
    coverage = main.get("coverage") if isinstance(main.get("coverage"), dict) else {}
    summary = coverage.get("birdeye_discovery_v424") if isinstance(coverage.get("birdeye_discovery_v424"), dict) else {}
    memory_dir.mkdir(parents=True, exist_ok=True)
    cu_state = _read_cu_ledger(memory_dir)
    cu_path = memory_dir / "birdeye-cu-ledger.json"
    if not cu_path.exists():
        cu_path.write_text(json.dumps(cu_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rotation_path = memory_dir / "birdeye-rotation-state.json"
    rotation_state = json.loads(rotation_path.read_text(encoding="utf-8")) if rotation_path.exists() else {
        "schema": "crypto-prebreakout.birdeye-rotation.v1",
        "provider_version": BIRDEYE_VERSION,
        "next_slot": 0,
        "rotation_slots_total": 0,
        "last_request_status": "NOT_CONFIGURED",
    }
    if not rotation_path.exists():
        rotation_path.write_text(json.dumps(rotation_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary["cu_ledger_month_utc"] = cu_state.get("month_utc")
    summary["estimated_cu_reserved_this_month"] = cu_state.get("estimated_cu_reserved")
    summary["scanner_monthly_cu_ceiling"] = SCANNER_MONTHLY_CU_CEILING
    summary["cu_budget_policy"] = "One discovery request per full scan; 30/40 CU depending on family; hard stop at 20,000 estimated CU/month to reserve a 10,000-CU buffer against the current 30,000-CU Standard allowance. This local ledger cannot see other applications using the same key."
    main["birdeye_discovery_v424"] = summary
    coverage["birdeye_discovery_v424"] = summary
    main["coverage"] = coverage
    report_path.write_text(json.dumps(main, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path = memory_dir / "memory-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    manifest.update({
        "birdeye_provider_version": BIRDEYE_VERSION,
        "birdeye_last_run_id": str(run_id),
        "birdeye_cu_month_utc": cu_state.get("month_utc"),
        "birdeye_estimated_cu_reserved": cu_state.get("estimated_cu_reserved"),
        "birdeye_scanner_monthly_cu_ceiling": SCANNER_MONTHLY_CU_CEILING,
        "birdeye_rotation_slots_total": rotation_state.get("rotation_slots_total"),
        "birdeye_next_rotation_slot": rotation_state.get("next_slot"),
    })
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def install_runtime_patches(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    rocket_path = repo_root / "scripts" / "rocket_hunt.py"
    if not rocket_path.exists():
        raise FileNotFoundError("scripts/rocket_hunt.py must be extracted before installing v4.24")
    source = rocket_path.read_text(encoding="utf-8")
    marker = 'if __name__ == "__main__":'
    hook = (
        "\nimport sys as _v424_sys\n"
        "try:\n"
        "    from scripts.v424_birdeye_free import install_birdeye_hooks as _v424_install\n"
        "except ImportError:\n"
        "    from v424_birdeye_free import install_birdeye_hooks as _v424_install\n"
        "_v424_install(_v424_sys.modules[__name__])\n"
    )
    if "_v424_install(_v424_sys.modules[__name__])" not in source:
        if marker not in source:
            raise RuntimeError("could not find rocket_hunt.py main hook point")
        source = source.replace(marker, hook + "\n" + marker, 1)
        rocket_path.write_text(source, encoding="utf-8")
    return {
        "installed_version": BIRDEYE_VERSION,
        "patched_files": ["scripts/rocket_hunt.py"],
        "standard_tier_only": True,
        "scanner_monthly_cu_ceiling": SCANNER_MONTHLY_CU_CEILING,
        "paid_plan_fallback": False,
    }


def self_test() -> None:
    networks = parse_birdeye_networks({
        "data": [
            {"chain": "solana", "name": "Solana"},
            {"chain": "ethereum", "name": "Ethereum"},
            {"chain": "base", "name": "Base"},
            {"chain": "solana", "name": "Solana"},
        ]
    })
    assert [item["chain"] for item in networks] == ["solana", "ethereum", "base"]
    records = parse_birdeye_token_rows({
        "data": {
            "tokens": [
                {"address": "0xaaa", "name": "Alpha", "symbol": "AAA", "price": 0.2,
                 "priceChange24hPercent": 18.0, "volume24hUSD": 250000, "liquidity": 80000},
            ]
        }
    })
    assert len(records) == 1
    base = lambda **kwargs: kwargs
    row = normalize_birdeye_token(records[0], "base", "token_trending", base, "birdeye_test")
    assert row["identity"] == "dex:base:0xaaa"
    assert row["change_24h"] == 18.0
    assert row["volume_24h_usd"] == 250000
    assert len(_make_rotation_slots(networks)) == 9
    with __import__("tempfile").TemporaryDirectory() as tmp:
        memory = Path(tmp)
        assert _reserve_cu(memory, 40, "token_trending")
        assert _read_cu_ledger(memory)["estimated_cu_reserved"] == 40
    print("v4.24.0 Birdeye free multichain self-test passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--post-scan", action="store_true")
    parser.add_argument("--outdir", type=Path, default=Path("rocket-run"))
    parser.add_argument("--memory-dir", type=Path, default=None)
    parser.add_argument("--run-id", type=str, default=os.environ.get("GITHUB_RUN_ID", "local-run"))
    args = parser.parse_args(argv)
    if args.install:
        print(json.dumps(install_runtime_patches(args.repo_root), sort_keys=True))
        return 0
    if args.self_test:
        self_test()
        return 0
    if args.post_scan:
        if args.memory_dir is None:
            parser.error("--memory-dir is required with --post-scan")
        print(json.dumps(_merge_birdeye_post_scan(args.outdir, args.memory_dir, args.run_id), sort_keys=True))
        return 0
    parser.error("choose --install, --self-test or --post-scan")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
