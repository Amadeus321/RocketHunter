#!/usr/bin/env python3
"""v4.23.0 free multi-provider Web3 discovery and provider-coverage ledger."""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DISCOVERY_SOURCES_VERSION = "4.23.0"
DEFILLAMA_ROOT = "https://api.llama.fi"
DEXTOOLS_ROOT = "https://public-api.dextools.io/v3"
REGISTRY_TTL_SECONDS = 7 * 24 * 60 * 60
DEXTOOLS_MAX_HOTPAIR_CALLS_PER_SCAN = 1
USER_AGENT = "crypto-prebreakout-research/v4.23.0 (read-only free-data discovery)"


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_json(url: str, headers: dict[str, str] | None = None, timeout: int = 8) -> tuple[Any, int | None, str | None]:
    req_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers, method="GET")
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


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("results", "items", "blockchains", "pools", "pairs"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    for key in ("results", "items", "blockchains", "pools", "pairs"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _unique_strings(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def normalize_defillama_registry(chains_payload: Any, dex_payload: Any) -> dict[str, Any]:
    chain_rows = _rows(chains_payload)
    chain_names = _unique_strings([
        row.get("name") or row.get("gecko_id") or row.get("chain")
        for row in chain_rows
    ])
    dex_all_chains: list[str] = []
    dex_protocols: list[dict[str, Any]] = []
    if isinstance(dex_payload, dict):
        raw_chains = dex_payload.get("allChains")
        if isinstance(raw_chains, list):
            dex_all_chains = _unique_strings(raw_chains)
        raw_protocols = dex_payload.get("protocols")
        if isinstance(raw_protocols, list):
            dex_protocols = [row for row in raw_protocols if isinstance(row, dict)]
    combined = _unique_strings(chain_names + dex_all_chains)
    protocol_chain_names: list[str] = []
    for protocol in dex_protocols:
        chains = protocol.get("chains")
        if isinstance(chains, list):
            protocol_chain_names.extend(chains)
    protocol_chain_names = _unique_strings(protocol_chain_names)
    return {
        "chain_tvl_registry_count": len(chain_names),
        "dex_volume_chain_count": len(dex_all_chains),
        "combined_chain_identifier_count": len(combined),
        "chains_registry": chain_names,
        "dex_volume_chains": dex_all_chains,
        "chains_referenced_by_dex_protocols": protocol_chain_names,
        "dex_protocol_count": len(dex_protocols),
        "combined_chain_identifiers": combined,
        "registry_scope": "DefiLlama chain TVL registry plus chains represented by free DEX-volume overview; this is ecosystem/protocol coverage, not a pool census.",
    }


def parse_dextools_blockchains(payload: Any) -> list[dict[str, Any]]:
    rows = _rows(payload)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        chain_id = row.get("id") or row.get("chain") or row.get("chainId")
        if not chain_id:
            continue
        key = str(chain_id).strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"id": str(chain_id).strip(), "name": str(row.get("name") or row.get("displayName") or chain_id).strip()})
    return out


def build_dextools_discovery_records(chain_id: str, hotpairs_payload: Any, base_record_fn) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in _rows(hotpairs_payload):
        pool_address = item.get("address") or item.get("poolAddress") or item.get("pairAddress")
        if not pool_address:
            continue
        main = item.get("mainToken") or item.get("baseToken") or {}
        side = item.get("sideToken") or item.get("quoteToken") or {}
        if not isinstance(main, dict):
            main = {}
        if not isinstance(side, dict):
            side = {}
        pair = "%s/%s" % (main.get("symbol") or "?", side.get("symbol") or "?")
        tokens = (("main", main), ("side", side))
        for token_role, token in tokens:
            token_address = token.get("address") or token.get("tokenAddress")
            if not token_address:
                continue
            row = base_record_fn(
                identity="dex:%s:%s" % (str(chain_id).lower(), str(token_address).lower()),
                asset=token.get("name") or token.get("symbol") or "UNKNOWN",
                symbol=token.get("symbol") or "",
                source="dextools_hotpairs",
                market_type="dex_pool",
                change_24h=None,
                price_usd=None,
                chain=str(chain_id),
                address=str(token_address),
                venue=item.get("exchangeName") or item.get("exchange"),
                pair=pair,
                pair_url=None,
                age_hours=None,
                retrieved_utc=utc_iso(),
                extra={
                    "pool_address": str(pool_address),
                    "dextools_exchange_factory": item.get("exchangeFactory"),
                    "pool_created_at": item.get("creationTime"),
                    "pool_created_block": item.get("creationBlock"),
                    "dextools_rank": item.get("rank"),
                    "dextools_fee": item.get("fee"),
                    "dextools_token_role": token_role,
                    "quote_symbol": (side if token_role == "main" else main).get("symbol"),
                    "discovery_lane": "dextools_hotpairs",
                    "discovery_evidence_status": "POOL_IDENTITY_ONLY_PRICE_AND_VOLUME_NOT_RETURNED_BY_HOTPAIRS",
                },
            )
            out.append(row)
    return out


def _memory_root(hunter: Any) -> Path:
    configured = os.environ.get("SCANNER_MEMORY_DIR")
    if configured:
        return Path(configured)
    checkpoint = getattr(hunter, "checkpoint_dir", None)
    if checkpoint:
        return Path(checkpoint) / "provider-memory"
    return Path("rocket-run/provider-memory")


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


def _collect_defillama(hunter: Any) -> dict[str, Any]:
    timeout = getattr(getattr(hunter, "fx", None), "timeout", 8)
    chains_url = DEFILLAMA_ROOT + "/v2/chains"
    dex_url = DEFILLAMA_ROOT + "/overview/dexs?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
    chains_payload, chains_status, chains_error = _fetch_json(chains_url, timeout=timeout)
    dex_payload, dex_status, dex_error = _fetch_json(dex_url, timeout=timeout)
    chains_ok = chains_payload is not None
    dex_ok = dex_payload is not None
    _record_status(hunter, "defillama_chain_registry", "OK" if chains_ok else "FAILED",
                   chains_url, len(_rows(chains_payload)) if chains_ok else 0, chains_error)
    dex_protocols = dex_payload.get("protocols") if isinstance(dex_payload, dict) else []
    _record_status(hunter, "defillama_dex_volume_overview", "OK" if dex_ok else "FAILED",
                   dex_url, len(dex_protocols) if isinstance(dex_protocols, list) else 0, dex_error)
    registry = normalize_defillama_registry(chains_payload, dex_payload)
    registry.update({
        "chains_endpoint_ok": chains_ok,
        "dex_overview_endpoint_ok": dex_ok,
        "chains_http_status": chains_status,
        "dex_http_status": dex_status,
        "registry_complete": bool(chains_ok and dex_ok),
        "data_role": "NETWORK_AND_DEX_PROTOCOL_VOLUME_CONTEXT_NOT_INDIVIDUAL_POOL_DISCOVERY",
    })
    return registry


def _load_dextools_registry(hunter: Any, api_key: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    memory = _memory_root(hunter)
    path = memory / "provider-registries" / "dextools-blockchains.json"
    now = time.time()
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            age = max(0.0, now - float(saved.get("fetched_epoch") or 0))
            chains = parse_dextools_blockchains(saved.get("payload"))
            if chains and age <= REGISTRY_TTL_SECONDS:
                _record_status(hunter, "dextools_blockchain_registry", "CACHED",
                               "https://public-api.dextools.io/v3/blockchains",
                               len(chains), reason="persistent free-tier registry cache is fresh",
                               extra={"cache_age_seconds": round(age, 1)})
                return chains, {"registry_complete": bool(saved.get("registry_complete")), "registry_cached": True,
                                "registry_pages": int(saved.get("pages") or 0), "registry_age_seconds": round(age, 1)}
        except Exception:
            pass

    all_rows: list[dict[str, Any]] = []
    pages = 0
    errors: list[str] = []
    total_pages: int | None = None
    last_payload: Any = None
    for page in range(1, 11):
        url = "https://public-api.dextools.io/v3/blockchains?" + urllib.parse.urlencode({"page": page, "pageSize": 100})
        payload, status, error = _fetch_json(url, {"X-API-Key": api_key}, timeout=getattr(getattr(hunter, "fx", None), "timeout", 8))
        if payload is None:
            errors.append(error or ("HTTP %s" % status))
            break
        last_payload = payload
        rows = parse_dextools_blockchains(payload)
        if not rows:
            break
        pages += 1
        all_rows.extend(rows)
        meta = payload.get("meta") or payload.get("pagination") or payload.get("pageInfo") if isinstance(payload, dict) else {}
        if isinstance(meta, dict):
            raw_total = meta.get("totalPages") or meta.get("total_pages") or meta.get("pages")
            try:
                if raw_total is not None:
                    total_pages = max(1, int(raw_total))
            except (TypeError, ValueError):
                pass
        if total_pages is not None and page >= total_pages:
            break
        if len(rows) < 100 and total_pages is None:
            # A short page usually indicates natural exhaustion. The registry
            # is marked complete only when a short page or explicit page count
            # confirms that no next page remains.
            break
    deduped = parse_dextools_blockchains(all_rows)
    complete = bool(deduped) and not errors and (
        (total_pages is not None and pages >= total_pages)
        or (total_pages is None and pages > 0 and len(parse_dextools_blockchains(last_payload)) < 100)
    )
    _record_status(hunter, "dextools_blockchain_registry", "OK" if deduped else "FAILED",
                   "https://public-api.dextools.io/v3/blockchains", len(deduped),
                   "; ".join(errors) if errors else None,
                   reason=None if deduped else "no blockchain identifiers returned",
                   extra={"pages": pages, "registry_complete": complete})
    if deduped:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "provider": "dextools",
            "fetched_utc": utc_iso(),
            "fetched_epoch": now,
            "pages": pages,
            "registry_complete": complete,
            "payload": {"data": deduped},
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return deduped, {"registry_complete": complete, "registry_cached": False,
                     "registry_pages": pages, "registry_errors": errors}


def _collect_dextools(hunter: Any, base_record_fn) -> dict[str, Any]:
    api_key = os.environ.get("DEXTOOLS_API_KEY", "").strip()
    if not api_key:
        _record_status(hunter, "dextools_hotpairs", "SKIPPED",
                       reason="optional free DEXTools API key not configured; set GitHub Actions secret DEXTOOLS_API_KEY")
        return {"api_key_configured": False, "chains": [], "selected_chain": None,
                "registry_complete": False, "hotpairs_ok": False, "hotpairs_rows": 0}
    chains, registry_meta = _load_dextools_registry(hunter, api_key)
    if not chains:
        _record_status(hunter, "dextools_hotpairs", "SKIPPED",
                       reason="hot-pair sweep skipped because the DEXTools blockchain registry was unavailable")
        return {"api_key_configured": True, "chains": [], "selected_chain": None,
                **registry_meta, "hotpairs_ok": False, "hotpairs_rows": 0}
    run_number = os.environ.get("GITHUB_RUN_NUMBER")
    try:
        rotation = int(run_number) if run_number and str(run_number).isdigit() else int(time.time() // 7200)
    except Exception:
        rotation = 0
    selected = chains[rotation % len(chains)]
    chain_id = str(selected["id"])
    url = "%s/features/%s/hotpairs" % (DEXTOOLS_ROOT, urllib.parse.quote(chain_id, safe=""))
    payload, status, error = _fetch_json(url, {"X-API-Key": api_key},
                                         timeout=getattr(getattr(hunter, "fx", None), "timeout", 8))
    hotpairs = _rows(payload)
    rows = build_dextools_discovery_records(chain_id, payload, base_record_fn) if payload is not None else []
    hunter.candidates.extend(rows)
    _record_status(hunter, "dextools_hotpairs", "OK" if payload is not None else "FAILED",
                   url, len(hotpairs), error,
                   extra={"selected_chain": chain_id, "discovery_records_added": len(rows),
                          "max_hotpair_calls_per_scan": DEXTOOLS_MAX_HOTPAIR_CALLS_PER_SCAN})
    return {
        "api_key_configured": True,
        "chains": [row["id"] for row in chains],
        "chain_names": chains,
        "selected_chain": chain_id,
        "rotation_index": rotation % len(chains),
        **registry_meta,
        "hotpairs_ok": payload is not None,
        "hotpairs_rows": len(hotpairs),
        "discovery_records_added": len(rows),
        "hotpairs_url": url,
        "request_budget_policy": "At most one DEXTools hotpairs request per full scan; the blockchain registry is cached for seven days. Free tier is not allowed to upgrade or incur charges.",
    }


def _observed_dexscreener_chains(hunter: Any) -> list[str]:
    values: list[Any] = []
    for row in getattr(hunter, "candidates", []) or []:
        source = str(row.get("source") or "").lower()
        if "dexscreener" in source and row.get("chain"):
            values.append(row.get("chain"))
    for row in getattr(hunter, "discovery_leads", []) or []:
        if row.get("chain"):
            values.append(row.get("chain"))
    return _unique_strings(values)


def install_provider_hooks(mod: Any) -> None:
    if getattr(mod, "_V423_PROVIDER_HOOKS_INSTALLED", False):
        return
    Hunter = getattr(mod, "RocketHunter", None)
    if Hunter is None or not hasattr(Hunter, "run"):
        raise RuntimeError("RocketHunter.run is required before installing v4.23 provider hooks")
    original_run = Hunter.run

    def patched_run(self, *args, **kwargs):
        defillama = _collect_defillama(self)
        dextools = _collect_dextools(self, getattr(mod, "_base_record"))
        result = original_run(self, *args, **kwargs)
        gecko_config = getattr(self, "discovery_config", {}) or {}
        gecko_registry = gecko_config.get("gecko_network_registry") or {}
        dss = _observed_dexscreener_chains(self)
        registries = {
            "defillama": {
                "networks": defillama.get("combined_chain_identifiers") or [],
                "network_count": defillama.get("combined_chain_identifier_count", 0),
                "dex_volume_networks": defillama.get("dex_volume_chains") or [],
                "dex_protocol_count": defillama.get("dex_protocol_count", 0),
                "registry_complete": defillama.get("registry_complete", False),
                "registry_scope": defillama.get("registry_scope"),
            },
            "dextools": {
                "networks": dextools.get("chains") or [],
                "network_count": len(dextools.get("chains") or []),
                "selected_network": dextools.get("selected_chain"),
                "registry_complete": dextools.get("registry_complete", False),
                "registry_cached": dextools.get("registry_cached", False),
                "hotpairs_ok": dextools.get("hotpairs_ok", False),
            },
            "dexscreener": {
                "networks": dss,
                "network_count": len(dss),
                "registry_available": False,
                "registry_scope": "Observed chain identifiers from live DEX Screener discovery responses; public API documentation does not expose a full blockchain registry endpoint.",
            },
            "geckoterminal": {
                "networks": gecko_config.get("gecko_networks_selected") or [],
                "network_count_selected": len(gecko_config.get("gecko_networks_selected") or []),
                "supported_network_count": gecko_registry.get("supported_network_count"),
                "registry_complete": gecko_registry.get("registry_complete", False),
                "registry_scope": "Selected rotating GeckoTerminal network IDs, not all pools on those networks.",
            },
        }

        # Build Unified Network Registry and Cross-Provider Corroboration (Phase 1)
        try:
            try:
                from scripts.unified_network_registry import UnifiedNetworkRegistry, normalize_chain
                from scripts.defillama_flow_engine import compute_ecosystem_flow_score
            except ImportError:
                from unified_network_registry import UnifiedNetworkRegistry, normalize_chain
                from defillama_flow_engine import compute_ecosystem_flow_score

            unified_reg = UnifiedNetworkRegistry()
            for ch in (defillama.get("dex_volume_chains") or []):
                unified_reg.record_provider_result(ch, "defillama", "OK")
            for ch in (defillama.get("chains_registry") or []):
                unified_reg.record_provider_result(ch, "defillama", "OK")
            for ch in dss:
                c_cnt = sum(1 for c in getattr(self, "candidates", []) if normalize_chain(c.get("chain")) == normalize_chain(ch))
                unified_reg.record_provider_result(ch, "dexscreener", "OK", pools_count=c_cnt)
            for ch in (gecko_config.get("gecko_networks_selected") or []):
                c_cnt = sum(1 for c in getattr(self, "candidates", []) if "gecko" in str(c.get("source", "")) and normalize_chain(c.get("chain")) == normalize_chain(ch))
                unified_reg.record_provider_result(ch, "geckoterminal", "OK", pools_count=c_cnt)

            # Corroborate candidates across providers
            for cand in getattr(self, "candidates", []):
                c_chain = normalize_chain(cand.get("chain"))
                c_token = str(cand.get("address") or cand.get("token_address") or "").lower()
                c_pool = str(cand.get("pool_address") or "").lower()
                c_src = str(cand.get("source") or "")
                seen_provs = set()
                for p in ("dexscreener", "gecko", "dextools", "dexpaprika"):
                    if p in c_src:
                        seen_provs.add("geckoterminal" if p == "gecko" else p)

                # Check matches with other candidate observations
                for other in getattr(self, "candidates", []):
                    if other is cand:
                        continue
                    o_src = str(other.get("source") or "")
                    o_token = str(other.get("address") or other.get("token_address") or "").lower()
                    o_pool = str(other.get("pool_address") or "").lower()
                    if (c_pool and o_pool and c_pool == o_pool) or (c_token and o_token and c_token == o_token):
                        for p in ("dexscreener", "gecko", "dextools", "dexpaprika"):
                            if p in o_src:
                                seen_provs.add("geckoterminal" if p == "gecko" else p)

                cand["cross_provider_corroboration"] = {
                    "corroborated": len(seen_provs) > 1,
                    "provider_count": len(seen_provs),
                    "providers": sorted(list(seen_provs)),
                }

            unified_summary = unified_reg.get_summary()
            registries["unified_network_registry"] = unified_summary
            result["unified_network_registry"] = unified_summary
        except Exception:
            pass
        result.setdefault("coverage", {}).update({
            "discovery_sources_version": DISCOVERY_SOURCES_VERSION,
            "provider_registries_v423": registries,
            "provider_roles_v423": {
                "defillama": "chain and DEX protocol-volume inventory; context and prioritization, not pair-level prices",
                "dextools": "optional free-tier hot-pair discovery; one chain per scan, all chains rotated",
                "dexscreener": "token/pair discovery feeds and search; observed chain IDs only",
                "geckoterminal": "rotating multi-page per-network pool discovery and OHLCV research",
            },
        })
        result["provider_coverage_v423"] = registries
        result["defillama_context_v423"] = {
            key: value for key, value in defillama.items()
            if key not in ("chains_registry", "dex_volume_chains", "chains_referenced_by_dex_protocols", "combined_chain_identifiers")
        }
        result["dextools_context_v423"] = {
            key: value for key, value in dextools.items()
            if key not in ("chains", "chain_names")
        }
        return result

    Hunter.run = patched_run
    mod._V423_PROVIDER_HOOKS_INSTALLED = True


def _merge_provider_state(outdir: Path, memory_dir: Path, run_id: str) -> dict[str, Any]:
    report_path = outdir / "rocket-hunt.json"
    main = json.loads(report_path.read_text(encoding="utf-8"))
    incoming = ((main.get("coverage") or {}).get("provider_registries_v423") or {})
    state_path = memory_dir / "provider-network-state.json"
    previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    providers = previous.get("providers") if isinstance(previous.get("providers"), dict) else {}
    now = utc_iso()
    summary: dict[str, Any] = {}
    for provider, details in incoming.items():
        details = details if isinstance(details, dict) else {}
        current = _unique_strings(details.get("networks") or [])
        old = providers.get(provider) if isinstance(providers.get(provider), dict) else {}
        cumulative = _unique_strings((old.get("cumulative_network_identifiers") or []) + current)
        new_ids = [x for x in current if x.casefold() not in {y.casefold() for y in (old.get("cumulative_network_identifiers") or [])}]
        providers[provider] = {
            "last_run_id": str(run_id),
            "last_updated_utc": now,
            "last_run_network_count": len(current),
            "cumulative_unique_network_identifier_count": len(cumulative),
            "cumulative_network_identifiers": cumulative,
            "new_network_identifiers_this_run": new_ids,
            "registry_complete_last_run": details.get("registry_complete"),
            "registry_available": details.get("registry_available", True),
            "registry_scope": details.get("registry_scope"),
            "selected_network_last_run": details.get("selected_network"),
        }
        summary[provider] = {
            "last_run_network_count": len(current),
            "cumulative_unique_network_identifier_count": len(cumulative),
            "new_network_identifiers_this_run": len(new_ids),
            "registry_complete_last_run": details.get("registry_complete"),
        }
    state = {
        "schema": "crypto-prebreakout.provider-network-coverage.v1",
        "discovery_sources_version": DISCOVERY_SOURCES_VERSION,
        "last_run_id": str(run_id),
        "last_updated_utc": now,
        "providers": providers,
        "coverage_boundary": "Provider registries use provider-specific identifiers and scopes. Counts must not be summed as unique real-world blockchains without a verified cross-provider chain-ID mapping. A registry is not proof that every pool or token has been crawled.",
    }
    memory_dir.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    coverage = main.setdefault("coverage", {})
    coverage["provider_network_coverage_v423"] = summary
    coverage["provider_network_state_file"] = "provider-network-state.json"
    main["provider_network_coverage_v423"] = summary
    main["discovery_sources_version"] = DISCOVERY_SOURCES_VERSION
    report_path.write_text(json.dumps(main, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path = memory_dir / "memory-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    manifest.update({
        "discovery_sources_version": DISCOVERY_SOURCES_VERSION,
        "last_provider_registry_run_id": str(run_id),
        "provider_network_coverage_v423": summary,
        "provider_network_state_file": "provider-network-state.json",
    })
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def install_runtime_patches(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    rocket_path = repo_root / "scripts" / "rocket_hunt.py"
    if not rocket_path.exists():
        raise FileNotFoundError("scripts/rocket_hunt.py must be extracted before installing v4.23")
    source = rocket_path.read_text(encoding="utf-8")
    marker = 'if __name__ == "__main__":'
    hook = (
        "\nimport sys as _v423_sys\n"
        "try:\n"
        "    from scripts.v423_provider_expansion import install_provider_hooks as _v423_install\n"
        "except ImportError:\n"
        "    from v423_provider_expansion import install_provider_hooks as _v423_install\n"
        "_v423_install(_v423_sys.modules[__name__])\n"
    )
    if "_v423_install(_v423_sys.modules[__name__])" not in source:
        if marker not in source:
            raise RuntimeError("could not find rocket_hunt.py main hook point")
        source = source.replace(marker, hook + "\n" + marker, 1)
        rocket_path.write_text(source, encoding="utf-8")
    return {"installed_version": DISCOVERY_SOURCES_VERSION, "patched_files": ["scripts/rocket_hunt.py"],
            "dextools_is_optional": True, "defillama_is_keyless": True}


def self_test() -> None:
    chain_payload = [
        {"name": "Ethereum"}, {"name": "Base"}, {"name": "Solana"},
    ]
    dex_payload = {
        "allChains": ["Ethereum", "Base", "Arbitrum"],
        "protocols": [{"name": "Uniswap", "chains": ["Ethereum", "Base"]}],
    }
    registry = normalize_defillama_registry(chain_payload, dex_payload)
    assert registry["combined_chain_identifier_count"] == 4
    assert registry["dex_protocol_count"] == 1
    base = lambda **kwargs: kwargs
    hotpairs = {"data": [{
        "address": "0xpool",
        "exchangeName": "ExampleDEX",
        "mainToken": {"address": "0xtoken1", "symbol": "AAA", "name": "Alpha"},
        "sideToken": {"address": "0xtoken2", "symbol": "BBB", "name": "Beta"},
        "rank": 1,
    }]}
    records = build_dextools_discovery_records("ether", hotpairs, base)
    assert len(records) == 2
    assert records[0]["extra"]["pool_address"] == "0xpool"
    assert records[1]["extra"]["dextools_token_role"] == "side"
    print("v4.23.0 provider expansion self-test passed")


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
        print(json.dumps(_merge_provider_state(args.outdir, args.memory_dir, args.run_id), sort_keys=True))
        return 0
    parser.error("choose --install, --self-test or --post-scan")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
