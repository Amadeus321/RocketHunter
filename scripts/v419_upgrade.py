#!/usr/bin/env python3
"""v4.19.0 Collector, 223-Network Rotation, Full-Universe Memory & Probability Calibration."""
from __future__ import annotations
import argparse, copy, gzip, hashlib, json, math, os, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
UPGRADE_VERSION = "4.19.0"
DISCOVERY_ARCHITECTURE_VERSION = "4.22.0"
BASE_CORE_RUNTIME_VERSION = "4.15.3"
MIN_TOTAL_OUTCOMES_FOR_CALIBRATION = 25
MIN_BUCKET_OBSERVATIONS_FOR_PROBABILITY = 10
CALIBRATION_MAX_OBSERVATION_LAG_HOURS = 6.0
CORE_ANCHOR_NETWORKS = ("solana", "base", "bsc", "eth", "arbitrum", "polygon_pos", "avax", "optimism", "sui-network", "ton", "aptos", "blast")
UNIVERSE_COMPACT_FIELDS = (
    "identity", "asset", "name", "symbol", "chain", "market_type", "contract_address", "pool_address", "pair_address",
    "gecko_pool_id", "pair", "venue", "quote_symbol", "price_usd", "change_5m_pct", "change_1h_pct", "change_6h_pct",
    "change_24h_pct", "volume_1h_usd", "volume_6h_usd", "volume_24h_usd", "liquidity_usd", "best_liquidity_pool_usd",
    "age_hours", "age_hours_v418", "pool_created_at", "rank_score", "forecast_score", "source", "source_url",
    "retrieved_utc", "return_window_status", "source_reported_change_24h_pct", "quality_gate", "risk_tier", "risk_flags",
    "discovery_lane", "birdeye_family", "birdeye_rank", "birdeye_volume_24h_usd",
    "birdeye_liquidity_usd", "birdeye_last_trade_unix_time", "birdeye_source_id",
)
SCORE_BUCKETS = (("bucket_0_35", 0.0, 35.0), ("bucket_35_50", 35.0, 50.0), ("bucket_50_65", 50.0, 65.0), ("bucket_65_80", 65.0, 80.0), ("bucket_80_100", 80.0, 1000.0))

def finite_float(v: Any) -> float | None:
    if isinstance(v, bool) or v is None or v == "": return None
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError, OverflowError): return None

def compact_universe_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in UNIVERSE_COMPACT_FIELDS if row.get(k) is not None}


def compact_discovery_row(row: dict[str, Any]) -> dict[str, Any]:
    """Retain discoverable markets even when forecast inputs are incomplete."""
    compact = compact_universe_row(row)
    price_ok = finite_float(row.get("price_usd")) is not None and finite_float(row.get("price_usd")) > 0
    return_24h_ok = finite_float(row.get("change_24h_pct")) is not None
    has_market_identity = bool(row.get("identity") or row.get("pool_address") or row.get("pair_address") or row.get("contract_address"))
    missing = []
    if not price_ok:
        missing.append("price_usd")
    if not return_24h_ok:
        missing.append("change_24h_pct")
    if not has_market_identity:
        missing.append("market_identity")
    compact["discovery_architecture_version"] = DISCOVERY_ARCHITECTURE_VERSION
    compact["discovery_status"] = "FORECAST_INPUT_READY" if price_ok and return_24h_ok else "DISCOVERY_ONLY_INCOMPLETE_MARKET_DATA"
    compact["discovery_missing_fields"] = missing
    return compact

def is_testnet_slug(net: str) -> bool:
    s = str(net or "").strip().lower()
    return any(t in s for t in ("testnet", "devnet", "sepolia", "goerli", "holesky"))

def resolve_rotation_slot(explicit_slot: int | None = None, memory_dir: Path | None = None) -> int:
    if explicit_slot is not None and explicit_slot >= 0: return int(explicit_slot)
    env_s = os.environ.get("GECKO_ROTATION_SLOT") or os.environ.get("GITHUB_RUN_NUMBER")
    if env_s and str(env_s).isdigit(): return int(env_s)
    if memory_dir and (memory_dir / "network-rotation-state.json").exists():
        try:
            st = json.loads((memory_dir / "network-rotation-state.json").read_text(encoding="utf-8"))
            if isinstance(st.get("last_rotation_slot"), int): return st["last_rotation_slot"] + 1
        except Exception: pass
    return max(0, int(datetime.now(timezone.utc).timestamp() // 14400))

def select_rotating_gecko_networks(supported_order: list[str], preferred_networks: Any = None, limit: int = 32, rotation_slot: int | None = None, anchor_count: int = 12) -> tuple[list[str], dict[str, Any]]:
    target = max(0, int(limit))
    pref = [str(x).strip().lower() for x in (preferred_networks or CORE_ANCHOR_NETWORKS) if str(x).strip()]
    live = [str(x).strip() for x in supported_order if str(x).strip() and not is_testnet_slug(str(x))]
    if not live:
        sel = list(dict.fromkeys(pref))[:target]
        return sel, {"registry_complete": False, "supported_network_count": 0, "selected_network_count": len(sel), "anchor_networks": sel, "rotated_frontier_networks": [], "frontier_pool_size": 0, "rotation_slot": 0, "rotation_offset": 0, "rotation_mode": "FALLBACK_PREFERRED_ONLY", "fallback_used": True}
    sup_set = set(live)
    ord_anc = list(dict.fromkeys([n for n in list(CORE_ANCHOR_NETWORKS) + pref if n in sup_set]))
    eff_anc = min(len(ord_anc), max(1, min(anchor_count, target // 2 if target >= 2 else target)))
    anchors = ord_anc[:eff_anc]
    anc_set = set(anchors)
    frontier = list(dict.fromkeys([n for n in pref + live if n in sup_set and n not in anc_set]))
    quota, slot = max(0, target - len(anchors)), resolve_rotation_slot(explicit_slot=rotation_slot)
    offset = (slot * quota) % len(frontier) if (frontier and quota > 0) else 0
    rotated = (frontier + frontier)[offset : offset + min(quota, len(frontier))] if (frontier and quota > 0) else []
    sel = (anchors + rotated)[:target]
    return sel, {"registry_complete": True, "supported_network_count": len(live), "selected_network_count": len(sel), "anchor_networks": anchors, "rotated_frontier_networks": rotated, "frontier_pool_size": len(frontier), "rotation_slot": slot, "rotation_offset": offset, "rotation_mode": "ANCHOR_PLUS_ROTATING_SWEEP_V419", "missing_preferred": [x for x in pref if x not in sup_set], "fallback_used": False}

def update_network_rotation_ledger(
    memory_dir: Path,
    selected_networks: list[str],
    supported_count: int,
    rotation_meta: dict[str, Any],
    run_id: str,
    snapshot_utc: str,
) -> dict[str, Any]:
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = memory_dir / "network-rotation-state.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    swept = set(previous.get("cumulative_networks_swept") or [])
    swept.update(str(network) for network in selected_networks if network)

    known_registry_size = int(supported_count or 0) > 0 and rotation_meta.get("registry_complete") is True
    denominator = int(supported_count) if known_registry_size else max(len(swept), 1)
    coverage_pct = (
        round(min(100.0, len(swept) / denominator * 100.0), 2)
        if known_registry_size else None
    )

    state = {
        "schema": "crypto-prebreakout.network-rotation.v1",
        "pipeline_version": UPGRADE_VERSION,
        "collector_overlay_version": UPGRADE_VERSION,
        "last_run_id": str(run_id),
        "last_updated_utc": snapshot_utc,
        "last_rotation_slot": rotation_meta.get("rotation_slot", 0),
        "last_rotation_offset": rotation_meta.get("rotation_offset", 0),
        "registry_size_known": known_registry_size,
        "discovered_networks_total": denominator if known_registry_size else None,
        "last_selected_network_count": len(selected_networks),
        "last_selected_networks": list(selected_networks),
        "cumulative_unique_networks_swept_count": len(swept),
        "cumulative_networks_swept": sorted(swept),
        "cumulative_network_coverage_pct": coverage_pct,
        "registry_complete": bool(rotation_meta.get("registry_complete")),
        "rotation_mode": "ANCHOR_PLUS_ROTATING_SWEEP_V419",
    }
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return state


def _score_bucket_name(score: float) -> str:
    # Scores are defined on a 0–100 scale. Clamp before bucketing so a
    # malformed negative value cannot be assigned to the highest bucket.
    bounded = max(0.0, min(100.0, float(score)))
    for name, lower, upper in SCORE_BUCKETS:
        if lower <= bounded < upper:
            return name
    return SCORE_BUCKETS[-1][0]


def _prior_breakout_probability(row: dict[str, Any], raw_score: float) -> tuple[float, float]:
    z = -1.65 + 0.028 * (max(0.0, min(100.0, raw_score)) - 50.0)
    liq, vol, age = finite_float(row.get("liquidity_usd") or row.get("best_liquidity_pool_usd")), finite_float(row.get("volume_24h_usd")), finite_float(row.get("age_hours_v418") or row.get("age_hours"))
    z += 0.22 if (liq and liq >= 250_000) else (0.08 if (liq and liq >= 50_000) else -0.35)
    z += 0.18 if (vol and vol >= 500_000) else (-0.25 if (not vol or vol < 25_000) else 0.0)
    if age is not None and age < 24.0: z -= 1.0
    if isinstance(row.get("quality_gate"), dict) and row["quality_gate"].get("status") == "FOLLOW_UP_SCREEN_PASSED_NOT_EXECUTION_READY": z += 0.20
    if row.get("fingerprint_cluster_id") or row.get("fingerprint_v418"): z -= 0.85
    return max(0.01, min(0.65, 1.0 / (1.0 + math.exp(-max(-6.0, min(6.0, z)))))), max(0.05, min(0.80, 1.0 / (1.0 + math.exp(-max(-6.0, min(6.0, z + 0.55))))))

def _beta_ci90(a: float, b: float) -> list[float]:
    tot = a + b
    m, sd = a / tot, math.sqrt(max(0.0, (a * b) / (tot * tot * (tot + 1.0))))
    return [max(0.001, round(m - 1.64485 * sd, 4)), min(0.999, round(m + 1.64485 * sd, 4))]

def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def build_calibration_model(
    forecasts: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    as_of_utc: str | None = None,
    prior_strength: float = 20.0,
    breakout_threshold_pct: float = 10.0,
) -> dict[str, Any]:
    if not math.isfinite(prior_strength) or prior_strength <= 0:
        raise ValueError("prior_strength must be positive and finite")
    if not math.isfinite(breakout_threshold_pct):
        raise ValueError("breakout_threshold_pct must be finite")

    cutoff = _parse_utc_timestamp(as_of_utc) if as_of_utc else None
    if as_of_utc and cutoff is None:
        raise ValueError("as_of_utc must be a valid ISO-8601 timestamp")

    fc_map = {str(f["forecast_id"]): f for f in forecasts if f.get("forecast_id")}
    buckets = {
        name: {
            "bucket": name,
            "score_min": lower,
            "score_max": upper,
            "n_resolved_24h": 0,
            "n_breakout_10pct": 0,
            "n_positive_24h": 0,
            "sum_pred_prob": 0.0,
        }
        for name, lower, upper in SCORE_BUCKETS
    }
    brier: list[float] = []
    logloss: list[float] = []
    n_resolved = 0

    max_utc = datetime.max.replace(tzinfo=timezone.utc)
    ordered_outcomes = sorted(
        outcomes,
        key=lambda item: (
            _parse_utc_timestamp(item.get("observed_utc")) or max_utc,
            str(item.get("forecast_id") or ""),
        ),
    )
    for outcome in ordered_outcomes:
        if int(outcome.get("horizon_hours") or 0) != 24:
            continue
        if outcome.get("status") != "OBSERVED_MARK_NOT_EXECUTABLE_FILL":
            continue

        forecast = fc_map.get(str(outcome.get("forecast_id") or ""))
        if not forecast:
            continue

        # Both timestamps are mandatory. Missing chronology cannot be made
        # safe by assuming that the observations were available at forecast time.
        observed_at = _parse_utc_timestamp(outcome.get("observed_utc"))
        forecast_at = _parse_utc_timestamp(forecast.get("snapshot_utc"))
        if observed_at is None or forecast_at is None:
            continue
        outcome_age_hours = (observed_at - forecast_at).total_seconds() / 3600.0
        if not 24.0 <= outcome_age_hours <= 24.0 + CALIBRATION_MAX_OBSERVATION_LAG_HOURS:
            continue
        if cutoff is not None and (observed_at > cutoff or forecast_at > cutoff):
            continue

        mark_return = finite_float(outcome.get("mark_return_pct"))
        if mark_return is None:
            continue
        snapshot = forecast.get("feature_snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        source_price_at = _parse_utc_timestamp(snapshot.get("retrieved_utc"))
        source_price_age = (
            (forecast_at - source_price_at).total_seconds() if source_price_at is not None else None
        )
        source_price = finite_float(snapshot.get("price_usd"))
        if (source_price is None or source_price <= 0 or source_price_age is None
                or source_price_age < -60.0 or source_price_age > 30.0 * 60.0):
            continue

        # A future 24h return is eligible for calibration only when the DEX
        # pool already had a full 24h history at the original forecast time.
        # Never let a later mature pool age retroactively qualify an immature
        # launch lead captured at the forecast snapshot.
        market_type = str(snapshot.get("market_type") or "").lower()
        is_dex = (
            market_type.startswith("dex")
            or bool(snapshot.get("pool_address") or snapshot.get("pair_address") or snapshot.get("gecko_pool_id"))
        )
        if is_dex and not (
            snapshot.get("chain")
            and snapshot.get("contract_address")
            and (snapshot.get("pool_address") or snapshot.get("pair_address") or snapshot.get("gecko_pool_id"))
        ):
            continue
        age_at_forecast = finite_float(snapshot.get("age_hours_v418"))
        if age_at_forecast is None:
            age_at_forecast = finite_float(snapshot.get("age_hours"))
        age_status = snapshot.get("return_window_status")
        if is_dex and (
            age_at_forecast is None
            or age_at_forecast < 24.0
            or age_status in {"INSUFFICIENT_24H_HISTORY", "UNKNOWN_POOL_AGE_NOT_COMPARABLE"}
        ):
            continue

        raw_score = finite_float(forecast.get("raw_score"))
        if raw_score is None:
            raw_score = finite_float(snapshot.get("forecast_score"))
        if raw_score is None:
            raw_score = finite_float(snapshot.get("rank_score"))
        if raw_score is None:
            continue

        prior_breakout, prior_positive = _prior_breakout_probability(snapshot, raw_score)
        breakout = 1 if mark_return >= breakout_threshold_pct else 0
        positive = 1 if mark_return > 0.0 else 0
        bucket = buckets[_score_bucket_name(raw_score)]

        # Prequential scoring: predict using only outcomes that appeared
        # earlier in this loop, then incorporate the current observation.
        prediction = (
            bucket["n_breakout_10pct"] + prior_strength * prior_breakout
        ) / (bucket["n_resolved_24h"] + prior_strength)
        prediction = max(0.001, min(0.999, prediction))
        brier.append((prediction - breakout) ** 2)
        logloss.append(
            -(breakout * math.log(prediction) + (1 - breakout) * math.log(1 - prediction))
        )

        bucket["n_resolved_24h"] += 1
        bucket["n_breakout_10pct"] += breakout
        bucket["n_positive_24h"] += positive
        bucket["sum_pred_prob"] += prediction
        n_resolved += 1

    reliability_bins = []
    for name, _, _ in SCORE_BUCKETS:
        bucket = buckets[name]
        count = bucket["n_resolved_24h"]
        reliability_bins.append({
            "bucket": name,
            "n_resolved_24h": count,
            "empirical_breakout_10pct_rate": (
                round(bucket["n_breakout_10pct"] / count, 4) if count else None
            ),
            "mean_predicted_prob": (
                round(bucket["sum_pred_prob"] / count, 4) if count else None
            ),
        })

    return {
        "schema": "crypto-prebreakout.calibration-model.v1",
        "model_version": f"v{UPGRADE_VERSION}-eb-beta-binomial",
        "as_of_utc": cutoff.isoformat().replace("+00:00", "Z") if cutoff else None,
        "calibration_mode": (
            "EMPIRICAL_POSTERIOR_CALIBRATED"
            if n_resolved >= MIN_TOTAL_OUTCOMES_FOR_CALIBRATION
            else "EMPIRICAL_BAYES_PRIOR_SHRINKAGE"
        ),
        "prior_strength_pseudo_count": prior_strength,
        "breakout_threshold_pct": breakout_threshold_pct,
        "minimum_total_outcomes_for_calibration": MIN_TOTAL_OUTCOMES_FOR_CALIBRATION,
        "minimum_bucket_observations_for_probability": MIN_BUCKET_OBSERVATIONS_FOR_PROBABILITY,
        "resolved_24h_outcomes_used": n_resolved,
        "brier_score_24h_breakout_10pct": round(sum(brier) / len(brier), 6) if brier else None,
        "log_loss_24h_breakout_10pct": round(sum(logloss) / len(logloss), 6) if logloss else None,
        "interval_method": "NORMAL_APPROXIMATION_TO_BETA_POSTERIOR",
        "buckets": buckets,
        "reliability_bins": reliability_bins,
        "time_boundary_policy": "24H_OUTCOME_OBSERVED_WITHIN_6H_LAG; PRICE_AND_FORECAST_PROVENANCE_REQUIRED; NO_FUTURE_DATA",
    }


def calibrate_candidate_row(row: dict[str, Any], cal_model: dict[str, Any]) -> dict[str, Any]:
    status = row.get("return_window_status")
    age = finite_float(row.get("age_hours_v418"))
    if age is None:
        age = finite_float(row.get("age_hours"))
    is_dex = (
        str(row.get("market_type") or "").startswith("dex")
        or bool(row.get("pool_address") or row.get("pair_address"))
    )

    if (
        status in {"INSUFFICIENT_24H_HISTORY", "UNKNOWN_POOL_AGE_NOT_COMPARABLE"}
        or (is_dex and (age is None or age < 24.0))
    ):
        row.update({
            "calibrated_prob_24h": None,
            "calibrated_directional_prob_24h": None,
            "calibrated_prob_ci90": None,
            "prior_breakout_estimate_24h": None,
            "score_is_probability": False,
            "calibration_status": "DISCOVERY_ONLY_UNQUALIFIED_FOR_24H_PROBABILITY",
        })
        return row

    raw_score = finite_float(row.get("forecast_score"))
    if raw_score is None:
        raw_score = finite_float(row.get("rank_score"))
    if raw_score is None:
        raw_score = finite_float(row.get("raw_score"))
    if raw_score is None:
        row.update({
            "calibrated_prob_24h": None,
            "calibrated_directional_prob_24h": None,
            "calibrated_prob_ci90": None,
            "prior_breakout_estimate_24h": None,
            "score_is_probability": False,
            "calibration_status": "NO_RAW_SCORE_AVAILABLE",
        })
        return row

    prior_strength = float(cal_model.get("prior_strength_pseudo_count") or 20.0)
    prior_breakout, prior_positive = _prior_breakout_probability(row, raw_score)
    bucket = (cal_model.get("buckets") or {}).get(_score_bucket_name(raw_score)) or {}
    bucket_n = int(bucket.get("n_resolved_24h") or 0)
    bucket_breakouts = int(bucket.get("n_breakout_10pct") or 0)
    bucket_positive = int(bucket.get("n_positive_24h") or 0)

    alpha = bucket_breakouts + prior_strength * prior_breakout
    beta = (bucket_n - bucket_breakouts) + prior_strength * (1.0 - prior_breakout)
    posterior_estimate = alpha / (alpha + beta)
    posterior_interval = _beta_ci90(alpha, beta)
    total_n = int(cal_model.get("resolved_24h_outcomes_used") or 0)
    enough_evidence = (
        total_n >= MIN_TOTAL_OUTCOMES_FOR_CALIBRATION
        and bucket_n >= MIN_BUCKET_OBSERVATIONS_FOR_PROBABILITY
    )

    row.update({
        "raw_heuristic_score": raw_score,
        "prior_breakout_estimate_24h": round(posterior_estimate, 4),
        "prior_breakout_interval90_approx": posterior_interval,
        "calibration_observations_total": total_n,
        "calibration_observations_in_bucket": bucket_n,
        "calibrated_prob_ci90_method": "NORMAL_APPROXIMATION_TO_BETA_POSTERIOR",
        "calibrated_prob_24h": round(posterior_estimate, 4) if enough_evidence else None,
        "calibrated_directional_prob_24h": (
            round((bucket_positive + prior_strength * prior_positive) / (bucket_n + prior_strength), 4)
            if enough_evidence else None
        ),
        "calibrated_prob_ci90": posterior_interval if enough_evidence else None,
        "score_is_probability": enough_evidence,
        "calibration_status": (
            "EMPIRICAL_BUCKET_SUPPORTED"
            if enough_evidence else "INSUFFICIENT_BUCKET_SAMPLE_PRIOR_ONLY"
        ),
    })
    return row


def _fallback_market_key(row: dict[str, Any]) -> str:
    ident, pool, chain = str(row.get("identity") or "").strip().lower(), str(row.get("pool_address") or row.get("pair_address") or "").strip().lower(), str(row.get("chain") or row.get("venue") or "").strip().lower()
    return f"{ident}|{pool}" if (ident and pool and pool not in ident) else (ident or (f"dex:{chain}:{pool}" if chain and pool else ""))

def _read_jsonl_gz(path: Path) -> list[dict[str, Any]]:
    if not path.exists(): return []
    with gzip.open(path, "rt", encoding="utf-8") as h: return [json.loads(line) for line in h if line.strip()]

def _write_jsonl_gz(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as h:
        for r in records: h.write(json.dumps(r, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")

def install_Collector_hooks_into_rocket_hunt(mod: Any) -> None:
    if getattr(mod, "_V419_HOOKS_INSTALLED", False): return
    mod.VERSION = UPGRADE_VERSION
    mod.GECKO_NETWORK_REGISTRY_PAGES = max(12, int(getattr(mod, "GECKO_NETWORK_REGISTRY_PAGES", 3)))
    HunterCls = getattr(mod, "RocketHunter", None)
    if HunterCls is None: return
    orig_build = HunterCls.build_output
    def patched_discover(self, preferred_networks=None, limit=32):
        aliases = getattr(mod, "GECKO_NETWORK_ALIASES", {})
        defaults = getattr(mod, "DEFAULT_NETWORKS", CORE_ANCHOR_NETWORKS)
        root = getattr(mod, "GECKO_ROOT", "https://api.geckoterminal.com/api/v2")
        items_fn = getattr(mod, "_items", lambda payload: (payload or {}).get("data") or [])
        preferred = [aliases.get(str(x).lower(), str(x).lower()) for x in (preferred_networks or defaults)]
        supported = []
        pages_ok = 0
        errors = []
        exhausted = False
        failed = False
        page_cap = int(getattr(mod, "GECKO_NETWORK_REGISTRY_PAGES", 12))

        # Continue until the API returns an empty page. A hard cap prevents an
        # unexpected provider pagination response from generating an unbounded scan.
        for page in range(1, page_cap + 1):
            payload = self._fetch_gecko_json(
                f"gecko_network_registry_page_{page}",
                f"{root}/networks?page={page}",
            )
            if payload is None:
                errors.append(f"page {page} request failed")
                failed = True
                break
            ids = [
                str(item.get("id"))
                for item in items_fn(payload)
                if isinstance(item, dict) and item.get("id")
            ]
            if not ids:
                exhausted = True
                break
            pages_ok += 1
            for network_id in ids:
                if network_id not in supported:
                    supported.append(network_id)
            # GeckoTerminal returns a short final page (e.g. 24 rows after two
            # 100-row pages). Treat that as natural exhaustion; probing the next
            # page can return HTTP 400 and incorrectly mark a complete registry partial.
            if len(ids) < 100:
                exhausted = True
                break

        selected, meta = select_rotating_gecko_networks(
            supported,
            preferred,
            limit=int(limit),
        )
        meta.update({
            "registry_complete": bool(supported) and not failed and exhausted,
            "registry_exhausted": exhausted,
            "registry_page_cap_reached": bool(supported) and not failed and not exhausted,
            "registry_pages_ok": pages_ok,
            "registry_pages_requested": page_cap,
            "registry_errors": errors,
        })
        return selected, meta

    def patched_build(self):
        data = orig_build(self)
        data["skill_version"] = data["collector_version"] = UPGRADE_VERSION
        n_fn = getattr(mod, "n", finite_float)
        source_rows = copy.deepcopy(getattr(self, "candidates", []) or [])
        # Preserve the legacy forecast-input snapshot separately from the broader
        # discovery ledger. Missing price/history must never erase a market lead.
        u_rows = [compact_universe_row(r) for r in source_rows if n_fn(r.get("change_24h_pct")) is not None and (n_fn(r.get("price_usd")) or 0) > 0]
        discovery_rows = [compact_discovery_row(r) for r in source_rows if r.get("identity") or r.get("pool_address") or r.get("pair_address") or r.get("contract_address")]
        data["universe_rows_v419"] = u_rows
        data["discovery_rows_v421"] = discovery_rows
        coverage = data.setdefault("coverage", {})
        coverage["exported_universe_rows_v419"] = len(u_rows)
        coverage["discovery_architecture_version"] = DISCOVERY_ARCHITECTURE_VERSION
        coverage["discovery_rows_v421"] = len(discovery_rows)
        coverage["discovery_rows_missing_price_v421"] = sum("price_usd" in r.get("discovery_missing_fields", []) for r in discovery_rows)
        coverage["discovery_rows_missing_24h_change_v421"] = sum("change_24h_pct" in r.get("discovery_missing_fields", []) for r in discovery_rows)
        return data
    HunterCls.discover_gecko_networks, HunterCls.build_output, mod._V419_HOOKS_INSTALLED = patched_discover, patched_build, True

def _merge_universe_market_row(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge duplicate lane copies while retaining the youngest known DEX age."""
    merged = copy.deepcopy(existing)
    for field, value in incoming.items():
        if merged.get(field) is None and value is not None:
            merged[field] = copy.deepcopy(value)

    is_dex = (
        str(merged.get("market_type") or incoming.get("market_type") or "").startswith("dex")
        or bool(merged.get("pool_address") or incoming.get("pool_address"))
        or bool(merged.get("pair_address") or incoming.get("pair_address"))
    )
    if is_dex:
        ages = [
            finite_float(merged.get("age_hours_v418")),
            finite_float(merged.get("age_hours")),
            finite_float(incoming.get("age_hours_v418")),
            finite_float(incoming.get("age_hours")),
        ]
        known_ages = [age for age in ages if age is not None and age >= 0]
        youngest = min(known_ages) if known_ages else None
        if youngest is None:
            merged["age_hours_v418"] = None
            merged["return_window_status"] = "UNKNOWN_POOL_AGE_NOT_COMPARABLE"
            reported = merged.get("change_24h_pct")
            if reported is not None and merged.get("source_reported_change_24h_pct") is None:
                merged["source_reported_change_24h_pct"] = reported
            merged["change_24h_pct"] = None
        else:
            merged["age_hours_v418"] = round(youngest, 4)
            merged["age_hours"] = round(youngest, 4)
            if youngest < 24.0:
                reported = merged.get("change_24h_pct")
                if reported is not None and merged.get("source_reported_change_24h_pct") is None:
                    merged["source_reported_change_24h_pct"] = reported
                merged["change_24h_pct"] = None
                merged["return_window_status"] = "INSUFFICIENT_24H_HISTORY"
            else:
                merged["return_window_status"] = "SOURCE_REPORTED_24H_POOL_AGE_GE_24H"
    return merged


def apply_v419_post_scan(outdir: Path, memory_dir: Path, run_id: str, now_override: datetime | None = None) -> dict[str, Any]:
    main_path, forward_path, audit_path = outdir / "rocket-hunt.json", outdir / "forward-24h-rocket.json", outdir / "v418-quality-audit.json"
    main = json.loads(main_path.read_text(encoding="utf-8"))
    forward = json.loads(forward_path.read_text(encoding="utf-8")) if forward_path.exists() else None
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else {}
    try:
        from scripts import v418_pipeline as v418
        mkey_fn, age_fn, make_fc_fn, grade_fn, utc_dt_fn, iso_fn = v418.market_key, v418.age_classify, v418.make_forecast_record, v418.grade_outcomes, v418.utc_dt, v418.iso_utc
        run_provenance = v418._run_provenance(outdir, run_id)
    except Exception:
        mkey_fn, age_fn, make_fc_fn, grade_fn, utc_dt_fn = _fallback_market_key, None, None, None, lambda s: None
        iso_fn = lambda dt: dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_provenance = {}
    snap_dt = now_override or utc_dt_fn(main.get("created_utc")) or datetime.now(timezone.utc)
    snap_iso = iso_fn(snap_dt)
    full_u, u_lanes = {}, {}
    for lane in ("universe_rows_v419", "top_24h_gainers", "top_24h_losers", "top_24h_movers", "new_pools_under_24h", "comparable_24h_gainers_v418", "new_launch_discovery_board_v418", "execution_candidate_shortlist"):
        for item in main.get(lane) or []:
            if isinstance(item, dict) and mkey_fn(item):
                k, rc = mkey_fn(item), copy.deepcopy(item)
                if age_fn: age_fn(rc, snap_dt)
                if k in full_u:
                    full_u[k] = _merge_universe_market_row(full_u[k], rc)
                else:
                    full_u[k] = rc
                u_lanes.setdefault(k, set()).add(lane)
    # Append a durable point-in-time discovery ledger independently of forecast
    # eligibility. Each run retains every identity-bearing market row, including
    # rows without a usable price or comparable 24h return.
    discovery_rows = main.get("discovery_rows_v421") or []
    discovery_path = memory_dir / "discovery" / f"{snap_dt:%Y-%m-%d}.jsonl.gz"
    existing_discovery = _read_jsonl_gz(discovery_path)
    seen_discovery = {r.get("discovery_id") for r in existing_discovery}
    added_discovery = []
    for row in discovery_rows:
        if not isinstance(row, dict):
            continue
        market_key = mkey_fn(row) or str(row.get("identity") or row.get("pool_address") or row.get("pair_address") or "")
        if not market_key:
            continue
        discovery_id = hashlib.sha256(f"{run_id}|{market_key}".encode()).hexdigest()
        if discovery_id in seen_discovery:
            continue
        seen_discovery.add(discovery_id)
        added_discovery.append({
            "schema": "crypto-prebreakout.discovery-ledger.v1",
            "discovery_architecture_version": DISCOVERY_ARCHITECTURE_VERSION,
            "discovery_id": discovery_id,
            "run_id": str(run_id),
            "snapshot_utc": snap_iso,
            "market_key": market_key,
            "discovery_snapshot": row,
        })
    if added_discovery:
        _write_jsonl_gz(discovery_path, existing_discovery + added_discovery)
    discovery_total = len(existing_discovery) + len(added_discovery)

    fc_path = memory_dir / "forecasts" / f"{snap_dt:%Y-%m-%d}.jsonl.gz"
    existing_today = _read_jsonl_gz(fc_path)
    seen_ids = {r.get("forecast_id") for r in existing_today}
    added_u = []
    for k, row in sorted(full_u.items()):
        rec = make_fc_fn(
            k, row, u_lanes.get(k, {"universe_rows_v419"}), snap_dt, run_id,
            run_provenance,
        ) if make_fc_fn else {"schema": "crypto-prebreakout.forecast-ledger.v1", "pipeline_version": UPGRADE_VERSION, "core_runtime_version": BASE_CORE_RUNTIME_VERSION, "forecast_id": hashlib.sha256(f"{run_id}|{k}".encode()).hexdigest(), "run_id": str(run_id), "snapshot_utc": snap_iso, "market_key": k, "raw_score": finite_float(row.get("forecast_score") if row.get("forecast_score") is not None else row.get("rank_score")), "score_is_probability": False, "feature_snapshot": compact_universe_row(row)}
        rec["pipeline_version"] = UPGRADE_VERSION
        rec["collector_overlay_version"] = UPGRADE_VERSION
        # The base collector code remains v4.15.3; v4.19.0 is the active patch overlay.
        rec["core_runtime_version"] = BASE_CORE_RUNTIME_VERSION
        if rec["forecast_id"] not in seen_ids:
            seen_ids.add(rec["forecast_id"]); added_u.append(rec)
    if added_u: _write_jsonl_gz(fc_path, existing_today + added_u)
    grading = grade_fn(memory_dir, snap_dt, full_u, run_id) if grade_fn else (audit.get("learning") or {})
    all_fc = [r for p in sorted((memory_dir / "forecasts").glob("*.jsonl.gz")) for r in _read_jsonl_gz(p)]
    all_out = [r for p in sorted((memory_dir / "outcomes").glob("*.jsonl.gz")) for r in _read_jsonl_gz(p)]
    cal_model = build_calibration_model(all_fc, all_out, as_of_utc=snap_iso)
    (memory_dir / "calibration-state.json").write_text(json.dumps(cal_model, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for lane in ("top_24h_gainers", "comparable_24h_gainers_v418", "execution_candidate_shortlist", "universe_rows_v419"):
        for item in main.get(lane) or []:
            if isinstance(item, dict): calibrate_candidate_row(item, cal_model)
    if isinstance(forward, dict):
        for lk in ("candidates", "eligible_24h_candidates", "new_launch_discovery_leads_v418", "excluded_candidates"):
            for c in forward.get(lk) or []:
                if isinstance(c, dict): calibrate_candidate_row(c, cal_model)
        for lk in ("forecast_leader", "upside_leader"):
            if isinstance(forward.get(lk), dict): calibrate_candidate_row(forward[lk], cal_model)
        forward.update({
            "pipeline_version": UPGRADE_VERSION,
            "collector_overlay_version": UPGRADE_VERSION,
            "core_runtime_version": BASE_CORE_RUNTIME_VERSION,
            "guard_version": "4.18.1",
            "learning_is_probability": False,
            "probability_semantics": "Per-row only: score_is_probability must be true; otherwise calibrated_prob_24h is null and the prior estimate is research-only.",
            "calibration_summary": cal_model,
        })
        forward_path.write_text(json.dumps(forward, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if fc_path.exists():
        t_recs = _read_jsonl_gz(fc_path)
        for rec in t_recs:
            if str(rec.get("run_id")) == str(run_id):
                snap = rec.get("feature_snapshot") if isinstance(rec.get("feature_snapshot"), dict) else {}
                tmp = dict(snap)
                if rec.get("raw_score") is not None and tmp.get("forecast_score") is None: tmp["raw_score"] = rec.get("raw_score")
                calibrate_candidate_row(tmp, cal_model)
                rec.update({
                    "calibrated_prob_24h": tmp.get("calibrated_prob_24h"),
                    "calibrated_prob_ci90": tmp.get("calibrated_prob_ci90"),
                    "score_is_probability": bool(tmp.get("score_is_probability")),
                    "calibration_status": tmp.get("calibration_status"),
                    "calibration_observations_in_bucket": tmp.get("calibration_observations_in_bucket", 0),
                    "collector_overlay_version": UPGRADE_VERSION,
                    "core_runtime_version": BASE_CORE_RUNTIME_VERSION,
                    "pipeline_version": UPGRADE_VERSION,
                    "model_id": f"v{UPGRADE_VERSION}-eb-beta-binomial",
                })
        _write_jsonl_gz(fc_path, t_recs)
    net_reg = ((main.get("coverage") or {}).get("discovery_config") or {}).get("gecko_network_registry") or {}
    sel_nets = list(net_reg.get("anchor_networks") or []) + list(net_reg.get("rotated_frontier_networks") or []) or list(CORE_ANCHOR_NETWORKS)
    rot_state = update_network_rotation_ledger(memory_dir, sel_nets, int(net_reg.get("supported_network_count") or 0), net_reg, run_id, snap_iso)
    manifest_path = memory_dir / "memory-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    today_cnt = sum(1 for r in _read_jsonl_gz(fc_path) if str(r.get("run_id")) == str(run_id)) if fc_path.exists() else 0
    manifest.update({
        "schema": "crypto-prebreakout.memory-manifest.v1",
        "pipeline_version": UPGRADE_VERSION,
        "collector_overlay_version": UPGRADE_VERSION,
        "core_runtime_version": BASE_CORE_RUNTIME_VERSION,
        "last_run_id": str(run_id),
        "last_snapshot_utc": snap_iso,
        "historical_forecasts": len(all_fc),
        "historical_outcomes": len(all_out),
        "new_forecast_snapshots": today_cnt,
        "discovery_architecture_version": DISCOVERY_ARCHITECTURE_VERSION,
        "discovery_rows_this_run": len(added_discovery),
        "discovery_ledger_rows_in_daily_partition": discovery_total,
        "discovery_ledger_file": str(discovery_path.relative_to(memory_dir)),
        "discovery_coverage_policy": "Identity-bearing discovered markets are retained even when price or comparable 24h history is missing; those records remain DISCOVERY_ONLY and are not promoted into forecast inputs.",
        "full_universe_market_rows_snapshotted_v419": len(full_u),
        "full_universe_assets_snapshotted_v419": len(full_u),  # compatibility alias; count is pool/market-key based
        "idempotency_key": str(run_id),
        "coverage_boundary": "All detailed priced rows with a source 24h return exposed by the collector are exported before leaderboard truncation, then deduplicated by market key. This is not a claim to enumerate every asset or every pool on the internet.",
        "calibration_summary": {
            "calibration_mode": cal_model["calibration_mode"],
            "resolved_24h_outcomes_used": cal_model["resolved_24h_outcomes_used"],
            "brier_score_24h_breakout_10pct": cal_model["brier_score_24h_breakout_10pct"],
            "minimum_bucket_observations_for_probability": MIN_BUCKET_OBSERVATIONS_FOR_PROBABILITY,
        },
        "network_rotation_summary": {
            "discovered_networks_total": rot_state["discovered_networks_total"],
            "last_selected_network_count": rot_state["last_selected_network_count"],
            "cumulative_unique_networks_swept_count": rot_state["cumulative_unique_networks_swept_count"],
            "cumulative_network_coverage_pct": rot_state["cumulative_network_coverage_pct"],
            "registry_complete": rot_state["registry_complete"],
            "registry_size_known": rot_state["registry_size_known"],
        },
    })
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    audit.update({
        "pipeline_version": UPGRADE_VERSION,
        "collector_overlay_version": UPGRADE_VERSION,
        "core_runtime_version": BASE_CORE_RUNTIME_VERSION,
        "discovery_architecture_version": DISCOVERY_ARCHITECTURE_VERSION,
        "discovery_rows_this_run": len(added_discovery),
        "discovery_ledger_rows_in_daily_partition": discovery_total,
        "full_universe_market_rows_snapshotted_v419": len(full_u),
        "full_universe_assets_snapshotted_v419": len(full_u),  # compatibility alias; market-key count
        "calibration_v419": cal_model,
        "network_rotation_v419": rot_state,
        "learning": grading,
    })
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    main.update({
        "skill_version": UPGRADE_VERSION,
        "pipeline_version": UPGRADE_VERSION,
        "collector_overlay_version": UPGRADE_VERSION,
        "core_runtime_version": BASE_CORE_RUNTIME_VERSION,
        "discovery_architecture_version": DISCOVERY_ARCHITECTURE_VERSION,
        "v418_quality_audit": audit,
    })
    main_path.write_text(json.dumps(main, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"upgrade_version": UPGRADE_VERSION, "run_id": str(run_id), "full_universe_assets_snapshotted": len(full_u), "total_run_forecast_snapshots": today_cnt, "calibration_mode": cal_model["calibration_mode"], "gecko_networks_selected": rot_state["last_selected_network_count"], "gecko_cumulative_networks_swept": rot_state["cumulative_unique_networks_swept_count"]}

def _replace_required_text(path: Path, replacements: list[tuple[str, str]]) -> None:
    if not path.exists():
        raise FileNotFoundError(f"required runtime file is missing: {path}")
    content = path.read_text(encoding="utf-8")
    changed = False
    for old, new in replacements:
        if old in content:
            content = content.replace(old, new, 1)
            changed = True
        elif old == "networks = default_networks[:a.network_sweep_limit] + extra_networks" and "networks = scheduled_networks[:a.network_sweep_limit] + extra_networks" in content:
            content = content.replace("networks = scheduled_networks[:a.network_sweep_limit] + extra_networks", new, 1)
            changed = True
        elif ("max_network_limit" in content and ("network_sweep_limit" in old or "network-sweep-limit" in old)) or new in content:
            continue
        else:
            raise RuntimeError(f"expected upgrade marker was not found in {path}: {old!r}")
    if changed:
        path.write_text(content, encoding="utf-8")


def install_runtime_patches(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    version_path = repo_root / "VERSION"
    rocket_hunt_path = repo_root / "scripts" / "rocket_hunt.py"
    full_scan_path = repo_root / "scripts" / "run_full_scan.py"

    if not rocket_hunt_path.exists() or not full_scan_path.exists():
        raise FileNotFoundError("runtime scripts must be extracted before installing v4.19.0")

    _replace_required_text(rocket_hunt_path, [
        (
            "networks = default_networks[:a.network_sweep_limit] + extra_networks",
            "networks = default_networks + extra_networks",
        ),
        (
            "network_sweep_count = len(networks)",
            "network_sweep_count = (max(0, a.network_sweep_limit) + len(extra_networks)) if a.network_sweep_limit > 0 else 0",
        ),
        (
            '        if network_names and per_network > 0:\n            for network in network_names:\n                for endpoint in ("trending_pools", "new_pools", "pools"):\n                    query = {"page": 1, "include": "base_token,quote_token,dex"}\n                    if endpoint == "trending_pools":\n                        query["duration"] = "24h"\n                    elif endpoint == "pools":\n                        query["sort"] = "h24_volume_usd_desc"\n                    routes.append(("gecko:%s:%s" % (network, endpoint),\n                                   "%s/networks/%s/%s?%s" % (GECKO_ROOT, quote(network, safe=""), endpoint, urlencode(query))))',
            '        if network_names and per_network > 0:\n            network_pages = max(1, min(10, int(pages)))\n            for network in network_names:\n                for endpoint in ("trending_pools", "new_pools", "pools"):\n                    for page in range(1, network_pages + 1):\n                        query = {"page": page, "include": "base_token,quote_token,dex"}\n                        if endpoint == "trending_pools":\n                            query["duration"] = "24h"\n                        elif endpoint == "pools":\n                            query["sort"] = "h24_volume_usd_desc"\n                        routes.append(("gecko:%s:%s:page:%s" % (network, endpoint, page),\n                                       "%s/networks/%s/%s?%s" % (GECKO_ROOT, quote(network, safe=""), endpoint, urlencode(query))))',
        ),
        (
            "gecko_network_calls = len(selected_networks) * 3 if include_gecko and gecko_network_sweep else 0",
            "gecko_network_calls = len(selected_networks) * 3 * max(1, min(10, gecko_pages)) if include_gecko and gecko_network_sweep else 0",
        ),
        (
            "routes = routes[:max(4, len(base_routes) + (len(network_names or []) * 3 if per_network else 0))]",
            "routes = routes[:max(4, len(base_routes) + (len(network_names or []) * 3 * max(1, min(10, int(pages))) if per_network else 0))]",
        ),
        (
            'p.add_argument("--gecko-pages", type=int, default=3, help="global GeckoTerminal pages per timeframe/list (1-10)")',
            'p.add_argument("--gecko-pages", type=int, default=3, help="GeckoTerminal pages per global list and per-network pool family (1-10)")',
        ),
    ])

    # The top-level scan wrapper previously rejected values above 24, which
    # made the documented 32-network sweep fail before the collector started.
    _replace_required_text(full_scan_path, [
        (
            "p.add_argument(\"--network-sweep-limit\", type=int, default=24)",
            "p.add_argument(\"--network-sweep-limit\", type=int, default=32)",
        ),
        (
            "if not 0 <= args.network_sweep_limit <= 24:",
            "if not 0 <= args.network_sweep_limit <= 64:",
        ),
        (
            'p.error("--network-sweep-limit must be between 0 and 24")',
            'p.error("--network-sweep-limit must be between 0 and 64")',
        ),
    ])

    text = rocket_hunt_path.read_text(encoding="utf-8")
    hook = (
        "\nimport sys as _s\n"
        "try:\n"
        "    from scripts.v419_upgrade import install_Collector_hooks_into_rocket_hunt as _iv419\n"
        "except ImportError:\n"
        "    from v419_upgrade import install_Collector_hooks_into_rocket_hunt as _iv419\n"
        "_iv419(_s.modules[__name__])\n"
    )
    if "_iv419" not in text:
        marker = 'if __name__ == "__main__":'
        if marker not in text:
            raise RuntimeError(f"cannot locate __main__ hook point in {rocket_hunt_path}")
        rocket_hunt_path.write_text(text.replace(marker, hook + "\n" + marker, 1), encoding="utf-8")

    version_path.write_text(f"{UPGRADE_VERSION}\n", encoding="utf-8")
    return {
        "installed_version": UPGRADE_VERSION,
        "collector_overlay_version": UPGRADE_VERSION,
        "base_core_runtime_version": BASE_CORE_RUNTIME_VERSION,
        "network_sweep_limit_supported": 64,
        "patched_files": ["VERSION", "scripts/rocket_hunt.py", "scripts/run_full_scan.py"],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--install", action="store_true"); p.add_argument("--repo-root", type=Path, default=Path(".")); p.add_argument("--outdir", type=Path, default=Path("rocket-run")); p.add_argument("--memory-dir", type=Path, default=None); p.add_argument("--run-id", type=str, default=os.environ.get("GITHUB_RUN_ID", "local-run")); p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.install: print(json.dumps(install_runtime_patches(a.repo_root.resolve()), sort_keys=True)); return 0
    if a.self_test:
        nets, meta = select_rotating_gecko_networks([f"c_{i}" for i in range(223)], limit=32, rotation_slot=2)
        assert len(nets) == 32 and meta["supported_network_count"] == 223
        print("v4.19.0 self-test passed"); return 0
    if a.memory_dir is None: p.error("--memory-dir required")
    print(json.dumps(apply_v419_post_scan(a.outdir, a.memory_dir, a.run_id), sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())