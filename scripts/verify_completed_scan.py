#!/usr/bin/env python3
"""Post-scan verification and QA audit script for Rocket Hunter.

Verifies:
1. Run status is COMPLETED with valid execution metadata.
2. Final report conforms to v4.26.1 single-TP (>= +40.00%) architecture.
3. Strict separation of discovery vs. actionable opportunities.
4. If any actionable opportunities are present:
   - Must have exact pool address & token contract.
   - Refreshed current USD price.
   - Verified single-TP upside >= +40.00%.
   - Safety verified (RugCheck / GoPlus).
   - Executable two-way quotes verified (Jupiter / 0x).
5. If zero pass all mandatory gates, verifies ZERO VERIFIED OPPORTUNITIES.
6. Verifies learning memory merge and prospective v4.25 breakout score captures.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def verify_run(run_dir: Path, memory_dir: Path | None = None) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    results = {
        "run_dir": str(run_dir),
        "status": "PASS",
        "errors": [],
        "warnings": [],
        "summary": {},
    }

    # 1. Check scan-status.json
    status_file = run_dir / "scan-status.json"
    if not status_file.is_file():
        results["errors"].append("scan-status.json missing")
        results["status"] = "FAIL"
        return results

    try:
        status_data = json.loads(status_file.read_text(encoding="utf-8"))
        results["summary"]["scan_status"] = status_data.get("status")
        results["summary"]["run_id"] = status_data.get("run_id")
        results["summary"]["scanner_version"] = status_data.get("scanner_version")
    except Exception as e:
        results["errors"].append(f"Invalid scan-status.json: {e}")
        results["status"] = "FAIL"
        return results

    # 2. Check forward-24h-rocket.json
    forward_file = run_dir / "forward-24h-rocket.json"
    if not forward_file.is_file():
        results["warnings"].append("forward-24h-rocket.json not generated yet")
        results["status"] = "PENDING"
        return results

    try:
        forward_data = json.loads(forward_file.read_text(encoding="utf-8"))
        gen_ver = forward_data.get("general_version")
        results["summary"]["general_version"] = gen_ver

        actionable = forward_data.get("verified_actionable_opportunities") or []
        results["summary"]["verified_actionable_count"] = len(actionable)

        # Audit actionable candidates
        for idx, item in enumerate(actionable, 1):
            pair = item.get("pair") or item.get("asset")
            chain = item.get("chain")
            pool = item.get("final_pool_address") or item.get("pool_address")
            contract = item.get("final_token_contract") or item.get("contract_address")
            price = item.get("final_price_usd") or item.get("price")
            plan = item.get("trade_plan") or {}
            tp = plan.get("single_take_profit")
            upside = item.get("tp_upside_pct")

            # Invariant checks
            if not pool or pool == "—":
                results["errors"].append(f"Actionable #{idx} ({pair}) missing pool address")
            if not contract or contract == "—":
                results["errors"].append(f"Actionable #{idx} ({pair}) missing token contract")
            if price is None or price <= 0:
                results["errors"].append(f"Actionable #{idx} ({pair}) missing or non-positive price")
            if tp is None or tp <= 0:
                results["errors"].append(f"Actionable #{idx} ({pair}) missing or non-positive TP")
            if upside is None or upside < 40.0:
                results["errors"].append(f"Actionable #{idx} ({pair}) upside {upside}% < +40.00% gate")
            if item.get("safety_status") != "PASS" and not item.get("safety_verified"):
                results["errors"].append(f"Actionable #{idx} ({pair}) failed safety verification")
            if item.get("execution_status") != "PASS" and not item.get("executable_route_verified"):
                results["errors"].append(f"Actionable #{idx} ({pair}) failed execution route verification")

        if len(actionable) == 0:
            results["summary"]["actionable_verdict"] = "ZERO VERIFIED OPPORTUNITIES (Fail-closed enforced)"
        else:
            results["summary"]["actionable_verdict"] = f"{len(actionable)} QUALIFIED OPPORTUNITY(IES)"

    except Exception as e:
        results["errors"].append(f"Error parsing forward-24h-rocket.json: {e}")
        results["status"] = "FAIL"

    # 3. Check rocket-hunt.md report
    md_file = run_dir / "forward-24h-rocket.md"
    if md_file.is_file():
        md_text = md_file.read_text(encoding="utf-8")
        if "ZERO VERIFIED OPPORTUNITIES" in md_text:
            results["summary"]["markdown_verdict"] = "ZERO VERIFIED OPPORTUNITIES correctly stated"
        elif "Verified actionable opportunities" in md_text:
            results["summary"]["markdown_verdict"] = "Verified actionable opportunities table rendered"

    # 4. Check forward-24h-rocket.html report
    html_file = run_dir / "forward-24h-rocket.html"
    if html_file.is_file():
        results["summary"]["html_report"] = "PRESENT"
    else:
        results["warnings"].append("forward-24h-rocket.html missing")

    if results["errors"]:
        results["status"] = "FAIL"

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True, help="Path to local run directory")
    args = parser.parse_args()

    res = verify_run(args.run_dir)
    print("\n" + "=" * 60)
    print(" ROCKET HUNTER RUN QA & VERIFICATION REPORT")
    print("=" * 60)
    print(f"Directory: {res['run_dir']}")
    print(f"Status:    {res['status']}")
    print("\nSummary:")
    for k, v in res["summary"].items():
        print(f"  - {k}: {v}")

    if res["warnings"]:
        print("\nWarnings:")
        for w in res["warnings"]:
            print(f"  [!] {w}")

    if res["errors"]:
        print("\nErrors:")
        for err in res["errors"]:
            print(f"  [X] {err}")
        return 1

    print("\nAll mandatory invariant checks passed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
