#!/usr/bin/env python3
"""DefiLlama Ecosystem Flow & Volume-Allocation Engine for Rocket Hunter.

Collects public DefiLlama chain TVL, 24h DEX volume, and short-term capital flow
data to calculate ecosystem momentum scores. Dynamically allocates scanner
discovery budgets and inspection depth toward expanding ecosystems.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from scripts.unified_network_registry import normalize_chain

DEFILLAMA_ROOT = "https://api.llama.fi"
USER_AGENT = "crypto-prebreakout-research/v4.27.0 (read-only ecosystem flow)"


def _fetch_json(url: str, timeout: int = 8) -> tuple[Any, int | None, str | None]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=max(1, int(timeout))) as resp:
            status = int(resp.status)
            payload = json.loads(resp.read().decode("utf-8"))
        return payload, status, None
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(300).decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        return None, int(exc.code), f"HTTP {exc.code} {detail}".strip()
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def compute_ecosystem_flow_score(
    volume_24h: float,
    change_1d_pct: float | None,
    change_7d_pct: float | None,
    tvl_usd: float | None = None,
    tvl_1d_change_pct: float | None = None,
) -> tuple[float, float]:
    """Calculate the capital flow momentum score and discovery budget weight multiplier.

    Returns:
        (flow_score, discovery_budget_weight)
        flow_score: Baseline 1.0, scales upward with volume acceleration & TVL expansion.
        discovery_budget_weight: Clamped between 0.6x and 2.5x.
    """
    score = 1.0

    # Volume acceleration component
    if change_1d_pct is not None:
        if change_1d_pct > 50.0:
            score += 0.8
        elif change_1d_pct > 20.0:
            score += 0.4
        elif change_1d_pct > 0.0:
            score += 0.15
        elif change_1d_pct < -25.0:
            score -= 0.3
        elif change_1d_pct < 0.0:
            score -= 0.1

    if change_7d_pct is not None:
        if change_7d_pct > 100.0:
            score += 0.5
        elif change_7d_pct > 30.0:
            score += 0.25
        elif change_7d_pct < -30.0:
            score -= 0.2

    # TVL capital inflow component
    if tvl_1d_change_pct is not None:
        if tvl_1d_change_pct > 10.0:
            score += 0.4
        elif tvl_1d_change_pct > 3.0:
            score += 0.2
        elif tvl_1d_change_pct < -5.0:
            score -= 0.2

    # Volume magnitude floor bonus
    if volume_24h > 1_000_000_000:
        score += 0.3
    elif volume_24h > 200_000_000:
        score += 0.15
    elif volume_24h < 100_000:
        score -= 0.2

    clamped_score = round(max(0.2, min(4.0, score)), 3)
    # Map flow score to discovery budget weight [0.6x, 2.5x]
    weight = round(max(0.6, min(2.5, 0.5 + (clamped_score * 0.65))), 2)
    return clamped_score, weight


class DefiLlamaFlowEngine:
    """Fetches global DEX volume and chain TVL dynamics to rank ecosystem expansion."""

    def __init__(self, timeout: int = 8) -> None:
        self.timeout = timeout
        self.chain_flow_data: dict[str, dict[str, Any]] = {}
        self.global_dex_volume_24h: float = 0.0
        self.retrieved_utc: str = ""

    def collect_ecosystem_flows(self) -> dict[str, dict[str, Any]]:
        """Fetch chain registry and DEX volume overview to calculate per-chain momentum."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.retrieved_utc = now_iso

        dex_url = f"{DEFILLAMA_ROOT}/overview/dexs?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
        chains_url = f"{DEFILLAMA_ROOT}/v2/chains"

        dex_payload, dex_status, dex_err = _fetch_json(dex_url, timeout=self.timeout)
        chains_payload, chains_status, chains_err = _fetch_json(chains_url, timeout=self.timeout)

        # Parse TVL by chain
        tvl_by_chain: dict[str, dict[str, Any]] = {}
        if isinstance(chains_payload, list):
            for row in chains_payload:
                if not isinstance(row, dict):
                    continue
                name = row.get("name") or row.get("gecko_id") or row.get("chain")
                if not name:
                    continue
                canon = normalize_chain(name)
                tvl_val = float(row.get("tvl") or 0.0)
                prev_day = float(row.get("prevDay") or tvl_val)
                tvl_chg_1d = ((tvl_val - prev_day) / prev_day * 100.0) if prev_day > 0 else 0.0
                tvl_by_chain[canon] = {
                    "tvl_usd": tvl_val,
                    "tvl_1d_change_pct": round(tvl_chg_1d, 2),
                    "token_symbol": row.get("tokenSymbol"),
                }

        # Parse 24h DEX volumes by chain and protocol momentum
        if isinstance(dex_payload, dict):
            self.global_dex_volume_24h = float(dex_payload.get("totalVolume24h") or 0.0)
            chains_data = dex_payload.get("allChains") or []
            breakdown = dex_payload.get("chainVolume24h") or {}
            protocols = dex_payload.get("protocols") or []

            # Compute volume-weighted 1d and 7d changes per chain from protocols
            chain_vol_weighted: dict[str, float] = {}
            chain_chg1_weighted: dict[str, float] = {}
            chain_chg7_weighted: dict[str, float] = {}

            if isinstance(protocols, list):
                for proto in protocols:
                    if not isinstance(proto, dict):
                        continue
                    p_vol = float(proto.get("total24h") or proto.get("totalVolume24h") or 0.0)
                    if p_vol <= 0:
                        continue
                    p_chains = proto.get("chains") or ([proto.get("chain")] if proto.get("chain") else [])
                    if not p_chains:
                        continue
                    split_vol = p_vol / len(p_chains)
                    c1 = proto.get("change_1d")
                    c7 = proto.get("change_7d")
                    for pc in p_chains:
                        pc_canon = normalize_chain(str(pc))
                        chain_vol_weighted[pc_canon] = chain_vol_weighted.get(pc_canon, 0.0) + split_vol
                        if c1 is not None:
                            try:
                                chain_chg1_weighted[pc_canon] = chain_chg1_weighted.get(pc_canon, 0.0) + (float(c1) * split_vol)
                            except (ValueError, TypeError):
                                pass
                        if c7 is not None:
                            try:
                                chain_chg7_weighted[pc_canon] = chain_chg7_weighted.get(pc_canon, 0.0) + (float(c7) * split_vol)
                            except (ValueError, TypeError):
                                pass

            # Compute per-chain momentum and flow score
            for chain_name in chains_data:
                canon = normalize_chain(chain_name)
                vol_24h = float(breakdown.get(chain_name) or 0.0)
                tvl_info = tvl_by_chain.get(canon, {})

                # Compute genuine 1d and 7d volume momentum
                chg_1d: float | None = None
                chg_7d: float | None = None
                total_w_vol = chain_vol_weighted.get(canon, 0.0)
                if total_w_vol > 0:
                    if canon in chain_chg1_weighted:
                        chg_1d = round(chain_chg1_weighted[canon] / total_w_vol, 2)
                    if canon in chain_chg7_weighted:
                        chg_7d = round(chain_chg7_weighted[canon] / total_w_vol, 2)

                flow_score, weight = compute_ecosystem_flow_score(
                    volume_24h=vol_24h,
                    change_1d_pct=chg_1d,
                    change_7d_pct=chg_7d,
                    tvl_usd=tvl_info.get("tvl_usd"),
                    tvl_1d_change_pct=tvl_info.get("tvl_1d_change_pct"),
                )

                self.chain_flow_data[canon] = {
                    "canonical_id": canon,
                    "raw_chain_name": chain_name,
                    "volume_24h_usd": vol_24h,
                    "change_1d_pct": chg_1d,
                    "change_7d_pct": chg_7d,
                    "tvl_usd": tvl_info.get("tvl_usd"),
                    "tvl_1d_change_pct": tvl_info.get("tvl_1d_change_pct"),
                    "ecosystem_flow_score": flow_score,
                    "discovery_budget_weight": weight,
                    "retrieved_utc": now_iso,
                }

        # Rank chains by discovery priority
        ranked_chains = sorted(
            self.chain_flow_data.values(),
            key=lambda x: (x["discovery_budget_weight"], x.get("volume_24h_usd") or 0),
            reverse=True,
        )
        return {c["canonical_id"]: c for c in ranked_chains}

    def get_priority_networks(self, top_n: int = 15) -> list[str]:
        """Return the highest priority networks for discovery allocation."""
        if not self.chain_flow_data:
            self.collect_ecosystem_flows()
        ranked = sorted(
            self.chain_flow_data.values(),
            key=lambda x: (x["discovery_budget_weight"], x.get("volume_24h_usd") or 0),
            reverse=True,
        )
        return [c["canonical_id"] for c in ranked[:top_n]]
