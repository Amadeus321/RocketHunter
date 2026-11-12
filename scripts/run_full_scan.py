#!/usr/bin/env python3
"""Preflight network access, then run the configured DEX-first/CEX-second scan.

Can run locally or from a hosted runner. It uses public/read-only market
endpoints and never submits orders. It stops early when every source is
unreachable instead of pacing requests that cannot succeed.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_VENUES = ("binance", "bybit", "okx", "mexc", "bitget", "gate", "kucoin")


def any_market_source_available(preflight):
    """True when at least one preflighted market endpoint returned data."""
    availability = preflight.get("market_availability") or {}
    if availability:
        return any(bool(availability.get(k)) for k in ("dex_discovery_ok", "aggregator_ok", "cex_spot_ok"))
    # Backward compatibility with older preflight output.
    return any(bool(row.get("market_ok")) for row in (preflight.get("summary") or {}).values())


def build_scan_command(args, outdir):
    venues = args.venue or list(DEFAULT_VENUES)
    cmd = [sys.executable, str(SCRIPTS / "rocket_hunt.py"),
           "--out", str(outdir / "rocket-hunt.json"),
           "--md", str(outdir / "rocket-hunt.md"),
           "--timeout", str(args.timeout),
           "--workers", str(args.workers),
           "--gecko-pages", str(args.gecko_pages),
           "--network-sweep-limit", str(args.network_sweep_limit),
           "--gecko-request-interval", str(args.gecko_request_interval),
           "--retries", str(args.retries),
           "--dex-search-limit", str(args.dex_search_limit),
           "--dex-lookup-limit", str(args.dex_lookup_limit),
           "--checkpoint-dir", str(outdir / "checkpoints")]
    for venue in venues:
        cmd += ["--venue", venue]
    if args.resume:
        cmd += ["--resume", "--resume-max-age-seconds", str(args.resume_max_age_seconds)]
    if args.birdeye:
        # Let the scanner write an explicit SKIPPED source row when the
        # optional key is absent, rather than hiding the requested lane.
        cmd.append("--birdeye")
    return cmd


def build_forecast_command(outdir, timeout=8, gecko_interval=12.0):
    """Build the scan-start-anchored forward-24h DEX Top Rocket/report stage."""
    return [sys.executable, str(SCRIPTS / "forward_24h_report.py"),
            "--report", str(outdir / "rocket-hunt.json"),
            "--markdown", str(outdir / "rocket-hunt.md"),
            "--out", str(outdir / "forward-24h-rocket.json"),
            "--timeout", str(timeout), "--gecko-interval", str(gecko_interval)]


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_bounded(command, cwd, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return subprocess.CompletedProcess(command, 124)
    from scripts.process_runner import run_bounded
    return run_bounded(command, cwd, remaining)


def gecko_pacing_budget_minutes(pages, network_limit, request_interval, registry_page_cap=12):
    """Upper bound scheduled Gecko requests, including global and registry pages."""
    page_count = max(1, min(10, int(pages)))
    network_count = max(0, int(network_limit))
    planned_calls = page_count * (4 + network_count * 3) + max(0, int(registry_page_cap))
    return max(0, planned_calls - 1) * max(0.0, float(request_interval)) / 60.0


def _mark_scan_state(outdir, status, started_utc, started_mono, run_id=None):
    finished = _utc_now() if status != "IN_PROGRESS" else None
    duration = round(max(0.0, time.monotonic() - started_mono), 3) if finished else None
    state = {"status": status, "scan_started_utc": started_utc,
             "scan_finished_utc": finished, "scan_duration_seconds": duration,
             "run_id": run_id}
    status_path = outdir / "scan-status.json"
    try:
        previous_state = json.loads(status_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        previous_state = {}
    if isinstance(previous_state, dict) and run_id and previous_state.get("run_id") == run_id:
        for key in ("process_id", "scanner_version", "python_runtime_version",
                    "configuration_fingerprint", "local_configuration"):
            if key in previous_state:
                state[key] = previous_state[key]
    status_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    for name in ("rocket-hunt.json", "forward-24h-rocket.json"):
        path = outdir / name
        if not path.exists():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        document.update({key: value for key, value in state.items() if key != "status"})
        document["scan_status"] = status
        if status != "COMPLETED":
            document["verified_actionable_opportunities"] = []
            document["best_immediate_entry"] = None
            document["top_rocket_execution_ready"] = False
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--outdir", default="rocket-run", help="directory for preflight, JSON/Markdown, logs and checkpoints")
    p.add_argument("--venue", action="append", choices=DEFAULT_VENUES, help="CEX spot venue; repeatable (default: all seven)")
    p.add_argument("--preflight-timeout", type=float, default=5.0, help="hard-bounded preflight timeout per endpoint")
    p.add_argument("--preflight-workers", type=int, default=16)
    p.add_argument("--timeout", type=int, default=8, help="per-request timeout for the full scan")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--gecko-pages", type=int, default=3)
    p.add_argument("--network-sweep-limit", type=int, default=24)
    p.add_argument("--gecko-request-interval", type=float, default=12.0)
    p.add_argument("--retries", type=int, default=2, help="transient HTTP retry attempts (0-5)")
    p.add_argument("--dex-search-limit", type=int, default=12)
    p.add_argument("--dex-lookup-limit", type=int, default=160)
    p.add_argument("--resume", action="store_true", help="reuse successful checkpoint responses that are still fresh")
    p.add_argument("--resume-max-age-seconds", type=int, default=900)
    p.add_argument("--birdeye", action="store_true", help="enable Birdeye only when BIRDEYE_API_KEY is present")
    p.add_argument("--max-runtime-minutes", type=float, default=170.0,
                   help="hard end-to-end deadline; must be <=180 minutes, with discovery stopped before finalization reserve")
    p.add_argument("--defer-completion", action="store_true",
                   help="leave status IN_PROGRESS for a workflow that has additional final pipeline stages")
    p.add_argument("--mark-complete", action="store_true",
                   help="mark an already-finished workflow scan completed after all downstream stages succeeded")
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if args.mark_complete:
        outdir = Path(args.outdir).resolve()
        status_path = outdir / "scan-status.json"
        try:
            state = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            p.error("cannot finalize: scan-status.json is missing or invalid")
        if state.get("status") != "IN_PROGRESS":
            p.error("cannot finalize a scan that is not IN_PROGRESS")
        start_dt = datetime.fromisoformat(str(state["scan_started_utc"]).replace("Z", "+00:00"))
        finish_dt = datetime.now(timezone.utc).replace(microsecond=0)
        state.update({"status": "COMPLETED", "scan_finished_utc": finish_dt.isoformat().replace("+00:00", "Z"),
                      "scan_duration_seconds": round((finish_dt - start_dt).total_seconds(), 3)})
        status_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        for name in ("rocket-hunt.json", "forward-24h-rocket.json"):
            path = outdir / name
            if path.exists():
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(document, dict):
                    document.update({key: value for key, value in state.items() if key != "status"})
                    document["scan_status"] = "COMPLETED"
                    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    if not 1 <= args.max_runtime_minutes <= 180:
        p.error("--max-runtime-minutes must be between 1 and 180")
    started_mono = time.monotonic()
    started_utc = _utc_now()
    deadline = started_mono + args.max_runtime_minutes * 60.0
    if not 1 <= args.gecko_pages <= 10:
        p.error("--gecko-pages must be between 1 and 10")
    from scripts.unified_network_registry import CANONICAL_NETWORKS
    max_network_limit = max(64, len(CANONICAL_NETWORKS))
    if not 0 <= args.network_sweep_limit <= max_network_limit:
        p.error(f"--network-sweep-limit must be between 0 and {max_network_limit}")
    if args.preflight_timeout <= 0 or args.timeout <= 0 or args.workers <= 0 or args.preflight_workers <= 0:
        p.error("timeouts and worker counts must be positive")
    if args.gecko_request_interval < 0:
        p.error("--gecko-request-interval must be non-negative")
    if not 0 <= args.retries <= 5:
        p.error("--retries must be between 0 and 5")
    if not 1 <= args.dex_search_limit <= 12 or args.dex_lookup_limit < 0:
        p.error("--dex-search-limit must be 1-12 and --dex-lookup-limit must be non-negative")

    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    run_id = os.environ.get("ROCKET_HUNTER_RUN_ID") or os.environ.get("GITHUB_RUN_ID")
    _mark_scan_state(outdir, "IN_PROGRESS", started_utc, started_mono, run_id)
    preflight_path = outdir / "preflight.json"
    pf_cmd = [sys.executable, str(SCRIPTS / "preflight.py"),
              "--timeout", str(args.preflight_timeout),
              "--workers", str(args.preflight_workers),
              "--json", "--out", str(preflight_path)]
    for venue in (args.venue or DEFAULT_VENUES):
        pf_cmd += ["--venue", venue]

    print("[1/2] Checking market-source network access (read-only probes)...", flush=True)
    pf_result = _run_bounded(pf_cmd, ROOT, deadline)
    if pf_result.returncode == 124:
        _mark_scan_state(outdir, "INTERRUPTED", started_utc, started_mono, run_id)
        print("Preflight exceeded the scan time budget; no opportunity report is qualified.", file=sys.stderr)
        return 124
    if not preflight_path.exists():
        print("Preflight did not create its report; refusing to start the long scan.", file=sys.stderr)
        return pf_result.returncode or 2
    try:
        preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("Cannot parse preflight report: %s" % exc, file=sys.stderr)
        return 2

    if not any_market_source_available(preflight):
        _mark_scan_state(outdir, "FAILED", started_utc, started_mono, run_id)
        print("NO MARKET DATA ENDPOINT IS REACHABLE. Full scan was not started. "
              "See %s for endpoint-level errors." % preflight_path, file=sys.stderr, flush=True)
        return 2

    pacing_minutes = gecko_pacing_budget_minutes(
        args.gecko_pages, args.network_sweep_limit, args.gecko_request_interval,
    )
    print("[2/2] Running configured DEX-first discovery and seven-venue CEX pass. "
          "The maximum GeckoTerminal pacing budget is about %.1f minutes before network/processing overhead." %
          pacing_minutes,
          flush=True)
    birdeye_key_present = bool(os.environ.get("BIRDEYE_API_KEY"))
    if args.birdeye and not birdeye_key_present:
        print("Birdeye requested but BIRDEYE_API_KEY is missing; Birdeye will be skipped and logged as unavailable.",
              file=sys.stderr, flush=True)
    scan_cmd = build_scan_command(args, outdir)
    reserve = min(30.0, max(5.0, args.max_runtime_minutes * 0.18)) * 60.0
    scan_deadline = min(deadline - reserve, time.monotonic() + max(1.0, args.max_runtime_minutes * 0.82 * 60.0))
    scan_result = _run_bounded(scan_cmd, ROOT, scan_deadline)
    report_path = outdir / "rocket-hunt.json"
    if scan_result.returncode == 124:
        _mark_scan_state(outdir, "INTERRUPTED", started_utc, started_mono, run_id)
        print("Discovery reached its bounded time slice; final verification/report stages were not started.", file=sys.stderr)
        return 124
    audit_result = None
    if report_path.exists():
        # Always attempt the read-only post-scan audit, even on a partial scan.
        # It emits explicit SKIPPED/UNVERIFIED rows when optional API keys are absent.
        audit_cmd = [sys.executable, str(SCRIPTS / "post_scan_audit.py"),
                     "--report", str(report_path), "--markdown", str(outdir / "rocket-hunt.md"),
                     "--timeout", str(args.timeout)]
        print("[post-scan] Auditing top candidates for CEX order-book depth, contract risk and optional router quotes.",
              flush=True)
        audit_result = _run_bounded(audit_cmd, ROOT, deadline)
        if audit_result.returncode == 124:
            _mark_scan_state(outdir, "INTERRUPTED", started_utc, started_mono, run_id)
            return 124
    if scan_result.returncode != 0:
        print("Scanner returned %s. Inspect coverage in %s and %s." %
              (scan_result.returncode, report_path, outdir / "rocket-hunt.md"),
              file=sys.stderr, flush=True)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        coverage = report.get("coverage") or {}
        print("Coverage status: %s; requested %d CEX venues; %d sources OK, %d failed, %d skipped." % (
            coverage.get("status", "UNKNOWN"), len(coverage.get("cex_venues_requested") or []),
            coverage.get("sources_ok", 0), coverage.get("sources_failed", 0), coverage.get("sources_skipped", 0)),
            flush=True)
        post = report.get("post_scan_audit") or {}
        print("Post-scan audits: DEX sampled CVD=%s; CEX books=%s; GoPlus=%s; 0x EVM=%s; Jupiter Solana=%s." % (
            (post.get("gecko_spot_cvd_audit") or {}).get("status", "MISSING"),
            (post.get("cex_orderbook_audit") or {}).get("status", "MISSING"),
            (post.get("goplus_security_audit") or {}).get("status", "MISSING"),
            (post.get("dex_router_quote_audit") or {}).get("status", "MISSING"),
            (post.get("solana_router_quote_audit") or {}).get("status", "MISSING")), flush=True)
    except (OSError, ValueError):
        print("Coverage summary unavailable; inspect the JSON report.", flush=True)
    forecast_result = None
    if report_path.exists() and scan_result.returncode == 0 and (audit_result is None or audit_result.returncode == 0):
        forecast_cmd = build_forecast_command(outdir, args.timeout, args.gecko_request_interval)
        print("[forward-24h] Building scan-start-anchored DEX Top Rocket, current price, copyable pool/contract and one candle-derived TP.",
              flush=True)
        forecast_result = _run_bounded(forecast_cmd, ROOT, deadline)
        if forecast_result.returncode == 124:
            _mark_scan_state(outdir, "INTERRUPTED", started_utc, started_mono, run_id)
            return 124
    if audit_result is not None and audit_result.returncode != 0:
        _mark_scan_state(outdir, "FAILED", started_utc, started_mono, run_id)
        return audit_result.returncode
    if forecast_result is not None and forecast_result.returncode != 0:
        print("Forward-24h report failed; discovery outputs are preserved, but no final Top Rocket was emitted.",
              file=sys.stderr, flush=True)
        _mark_scan_state(outdir, "FAILED", started_utc, started_mono, run_id)
        return forecast_result.returncode
    if scan_result.returncode == 0 and (audit_result is None or audit_result.returncode == 0) and (forecast_result is None or forecast_result.returncode == 0) and not args.defer_completion:
        _mark_scan_state(outdir, "COMPLETED", started_utc, started_mono, run_id)
    elif scan_result.returncode != 0:
        _mark_scan_state(outdir, "FAILED", started_utc, started_mono, run_id)
    return scan_result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
