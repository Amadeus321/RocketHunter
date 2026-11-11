#!/usr/bin/env python3
"""Run the complete Rocket Hunter pipeline locally, with isolated run folders.

Each process has its own output and memory snapshot. The append-only learning
ledgers are merged under a cross-process lock after successful qualification.
Provider request ledgers with hard local ceilings use the shared memory root.
"""
from __future__ import annotations

import argparse
import hashlib
import gzip
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_MEMORY = ROOT / "rocket-hunter-data" / "scanner-memory"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _prevent_system_sleep(enable: bool = True) -> None:
    """Request Windows OS to stay awake and not enter sleep mode while scan is running."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_AWAYMODE_REQUIRED = 0x00000040
        if enable:
            # Prevent idle sleep while keeping screen power savings intact
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
            )
        else:
            # Restore normal sleep behavior
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass


def _run(cmd: list[str], deadline: float, env: dict[str, str], log_path: Path, cwd: Path) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 124
    from scripts.process_runner import run_bounded

    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n$ " + subprocess.list2cmdline(cmd) + "\n")
        log.flush()
        result = run_bounded(cmd, cwd, remaining, env=env, stdout=log, stderr=subprocess.STDOUT)
        return result.returncode


def _run_required(cmd: list[str], deadline: float, env: dict[str, str],
                  log_path: Path, cwd: Path, failure: str) -> None:
    returncode = _run(cmd, deadline, env, log_path, cwd)
    if returncode == 124:
        raise TimeoutError(f"{failure}; its deadline expired and child processes were stopped")
    if returncode:
        raise RuntimeError(failure)


def _safe_extract_memory_archive(archive: bytes, destination: Path) -> None:
    import io
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        root = destination.resolve()
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError("unsafe path in scanner-memory archive")
            if not (member.isfile() or member.isdir()):
                raise RuntimeError("unexpected link or special file in scanner-memory archive")
        tar.extractall(destination, filter="data")


def ensure_memory(memory_dir: Path) -> None:
    if memory_dir.exists():
        if not memory_dir.is_dir():
            raise RuntimeError(f"memory path is not a directory: {memory_dir}")
        if any(memory_dir.iterdir()):
            # Existing non-empty memory is authoritative, even when it
            # predates the current manifest schema. Never replace it.
            return
        memory_dir.rmdir()
    memory_dir.parent.mkdir(parents=True, exist_ok=True)
    probe = subprocess.run(["git", "rev-parse", "--verify", "origin/scanner-memory"], cwd=ROOT,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if probe.returncode:
        raise RuntimeError("No local scanner-memory directory or origin/scanner-memory ref exists. "
                           "Refusing to start with empty learning history.")
    archive = subprocess.check_output(["git", "archive", "--format=tar", "origin/scanner-memory"], cwd=ROOT)
    staging = memory_dir.with_name(memory_dir.name + ".initializing-" + uuid.uuid4().hex)
    try:
        staging.mkdir(parents=True)
        _safe_extract_memory_archive(archive, staging)
        staging.replace(memory_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _project_fingerprint() -> str:
    digest = hashlib.sha256()
    for folder in ("scripts", "source", "tests"):
        root = ROOT / folder
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
            digest.update(path.read_bytes())
    digest.update((ROOT / "VERSION").read_bytes())
    return digest.hexdigest()[:20]


def build_run_metadata(args: argparse.Namespace) -> dict:
    configuration = {
        "max_runtime_minutes": args.max_runtime_minutes,
        "gecko_pages": args.gecko_pages,
        "network_sweep_limit": args.network_sweep_limit,
        "scanner_workers": args.workers,
        "preflight_workers": args.preflight_workers,
        "birdeye": args.birdeye,
        "gecko_request_interval_seconds": 12,
        "retries": 2,
        "dex_search_limit": 12,
        "dex_lookup_limit": 160,
    }
    fingerprint_input = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
    return {
        "scanner_version": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        "python_runtime_version": platform.python_version(),
        "configuration_fingerprint": hashlib.sha256(
            fingerprint_input.encode("utf-8")
        ).hexdigest(),
        "local_configuration": configuration,
    }


def _inject_runtime_hook(path: Path, tag: str, hook: str) -> None:
    text = path.read_text(encoding="utf-8")
    if tag in text:
        return
    marker = '\nif __name__ == "__main__":'
    index = text.rfind(marker)
    if index < 0:
        raise RuntimeError(f"cannot install {tag}: main hook point is missing in {path}")
    path.write_text(text[:index] + "\n" + hook + text[index:] + "\n", encoding="utf-8")


def prepare_runtime(runtime_store: Path, deadline: float, log_path: Path) -> Path:
    """Build an isolated, versioned runtime with the archived overlays installed."""
    from scripts.file_lock import locked_file

    runtime_store = Path(runtime_store).resolve()
    fingerprint = _project_fingerprint()
    runtime = runtime_store / fingerprint
    manifest_path = runtime / "local-runtime.json"
    lock_path = runtime_store / ".prepare.lock"
    runtime_store.mkdir(parents=True, exist_ok=True)
    with locked_file(lock_path):
        if manifest_path.exists():
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            if saved.get("fingerprint") == fingerprint:
                return runtime
            raise RuntimeError("runtime cache manifest does not match its folder")
        if runtime.exists():
            raise RuntimeError(f"incomplete runtime cache exists; preserving it for inspection: {runtime}")
        staging = runtime_store / (fingerprint + ".building-" + uuid.uuid4().hex)
        staging.mkdir(parents=True)
        try:
            for folder in ("scripts", "source", "tests"):
                src = ROOT / folder
                if src.exists():
                    shutil.copytree(src, staging / folder,
                                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copy2(ROOT / "VERSION", staging / "VERSION")
            env = os.environ.copy()
            installers = (
                [sys.executable, "-m", "scripts.v419_upgrade", "--install", "--repo-root", str(staging)],
                [sys.executable, "-m", "scripts.v423_provider_expansion", "--install", "--repo-root", str(staging)],
                [sys.executable, "-m", "scripts.v424_birdeye_free", "--install", "--repo-root", str(staging)],
                [sys.executable, "-m", "scripts.v425_early_breakout", "--install", "--repo-root", str(staging)],
            )
            for cmd in installers:
                if time.monotonic() >= deadline:
                    raise TimeoutError("time budget expired while preparing the local runtime")
                from scripts.process_runner import run_bounded
                with log_path.open("a", encoding="utf-8") as log:
                    log.write("\n$ " + subprocess.list2cmdline(cmd) + "\n")
                    result = run_bounded(
                        cmd, staging, max(0.1, deadline - time.monotonic()),
                        env=env, stdout=log, stderr=subprocess.STDOUT,
                    )
                if result.returncode:
                    if result.returncode == 124:
                        raise TimeoutError("time budget expired while preparing the local runtime")
                    raise RuntimeError("runtime overlay installation failed; inspect the run log")
            v420_hooks = (
                ("forward_24h_report.py", "_v420_install_forward", '''\n# v4.20 forward research overlay (installed in this isolated runtime).
try:
    import sys as _v420_sys
    from scripts.v420_upgrade import install_forward_hooks as _v420_install_forward
except ImportError:
    from v420_upgrade import install_forward_hooks as _v420_install_forward
_v420_install_forward(_v420_sys.modules[__name__])\n'''),
                ("v418_forward_guard.py", "_v420_install_guard", '''\n# v4.20 forward-age guard overlay (installed in this isolated runtime).
try:
    import sys as _v420_sys
    from scripts.v420_upgrade import install_guard_hooks as _v420_install_guard
except ImportError:
    from v420_upgrade import install_guard_hooks as _v420_install_guard
_v420_install_guard(_v420_sys.modules[__name__])\n'''),
                ("v418_fast_refresh.py", "_v420_install_fast", '''\n# v4.20 fast-refresh overlay (installed in this isolated runtime).
try:
    import sys as _v420_sys
    from scripts.v420_upgrade import install_fast_refresh_hooks as _v420_install_fast
except ImportError:
    from v420_upgrade import install_fast_refresh_hooks as _v420_install_fast
_v420_install_fast(_v420_sys.modules[__name__])\n'''),
                ("v419_upgrade.py", "_v420_install_memory", '''\n# v4.20 full/fast learning-memory provenance overlay.
try:
    import sys as _v420_sys
    from scripts.v420_upgrade import install_v419_memory_hooks as _v420_install_memory
except ImportError:
    from v420_upgrade import install_v419_memory_hooks as _v420_install_memory
_v420_install_memory(_v420_sys.modules[__name__])\n'''),
            )
            for filename, tag, hook in v420_hooks:
                _inject_runtime_hook(staging / "scripts" / filename, tag, hook)
            # The v4.19 installer writes its overlay version to VERSION. Restore
            # the project release label after hooks have captured their own versions.
            shutil.copy2(ROOT / "VERSION", staging / "VERSION")
            (staging / "local-runtime.json").write_text(json.dumps({
                "fingerprint": fingerprint, "built_utc": utc_now(),
                "overlays": ["v4.19", "v4.20-hooks", "v4.23", "v4.24", "v4.25"],
            }, indent=2) + "\n", encoding="utf-8")
            staging.replace(runtime)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
    return runtime


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".local-merge-tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def _record_key(kind: str, row: dict) -> str:
    if kind == "forecasts":
        return str(row.get("forecast_id") or "")
    if kind == "outcomes":
        return f"{row.get('forecast_id')}:{row.get('horizon_hours')}"
    return str(row.get("discovery_id") or "")


def merge_memory(snapshot: Path, memory_dir: Path, lock_path: Path, baseline: Path | None = None,
                 deadline: float | None = None) -> None:
    from scripts.file_lock import locked_file
    from scripts import v418_pipeline, v419_upgrade

    with locked_file(lock_path):
        def check_time() -> None:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("whole-run time budget expired during learning-memory merge")

        check_time()
        memory_dir.mkdir(parents=True, exist_ok=True)
        for kind in ("discovery", "forecasts", "outcomes"):
            source_root, target_root = snapshot / kind, memory_dir / kind
            if not source_root.exists():
                continue
            target_root.mkdir(parents=True, exist_ok=True)
            for src in sorted(source_root.glob("*.jsonl.gz")):
                check_time()
                dst = target_root / src.name
                old = _read_jsonl(dst)
                known = {_record_key(kind, row) for row in old}
                additions = [row for row in _read_jsonl(src)
                             if _record_key(kind, row) and _record_key(kind, row) not in known]
                if additions:
                    merged = old + additions
                    temp = dst.with_name(dst.name + ".merge-tmp")
                    with gzip.open(temp, "wt", encoding="utf-8", compresslevel=9) as handle:
                        for row in merged:
                            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
                    temp.replace(dst)

        registries = snapshot / "provider-registries"
        if registries.exists():
            for src in registries.rglob("*"):
                if src.is_file():
                    dst = memory_dir / src.relative_to(snapshot)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.exists():
                        shutil.copy2(src, dst)

        # Merge provider coverage as a union. Per-run rotation choices can repeat,
        # but no known chain identifier is discarded by concurrent commits.
        for filename in ("provider-network-state.json",):
            check_time()
            incoming = snapshot / filename
            target = memory_dir / filename
            if incoming.exists():
                a = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
                b = json.loads(incoming.read_text(encoding="utf-8"))
                providers = a.get("providers", {})
                for provider, detail in b.get("providers", {}).items():
                    prev = providers.get(provider, {})
                    ids = list(dict.fromkeys((prev.get("cumulative_network_identifiers") or []) +
                                             (detail.get("cumulative_network_identifiers") or [])))
                    latest = detail if (detail.get("last_updated_utc") or "") >= (prev.get("last_updated_utc") or "") else prev
                    providers[provider] = {**prev, **latest,
                        "cumulative_network_identifiers": ids,
                        "cumulative_unique_network_identifier_count": len(ids)}
                a.update({k: v for k, v in b.items() if k != "providers"})
                a["providers"] = providers
                _write_json_atomic(target, a)

        # Preserve latest full-universe pointer and monotonically union swept networks.
        for filename in ("network-rotation-state.json",):
            check_time()
            incoming, target = snapshot / filename, memory_dir / filename
            if incoming.exists():
                b = json.loads(incoming.read_text(encoding="utf-8"))
                a = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
                current_slot = int(a.get("last_rotation_slot") or 0)
                swept = list(dict.fromkeys((a.get("cumulative_networks_swept") or []) +
                                           (b.get("cumulative_networks_swept") or [])))
                if (b.get("last_updated_utc") or "") >= (a.get("last_updated_utc") or ""):
                    a.update(b)
                if baseline and (baseline / filename).exists():
                    base = json.loads((baseline / filename).read_text(encoding="utf-8"))
                    delta = max(0, int(b.get("last_rotation_slot") or 0) - int(base.get("last_rotation_slot") or 0))
                    a["last_rotation_slot"] = current_slot + delta
                a["cumulative_networks_swept"] = swept
                a["cumulative_unique_networks_swept_count"] = len(swept)
                _write_json_atomic(target, a)

        # Operational state is last-writer-wins by its own timestamp; the raw
        # forecast/outcome ledgers above remain unioned and are never reset.
        state_files = ("hyperliquid-market-state.json", "birdeye-rotation-state.json",
                       "dexpaprika-request-ledger.json", "active-24h-watchlist.json",
                       "completed-24h-watchlist.json")
        for filename in state_files:
            check_time()
            incoming, target = snapshot / filename, memory_dir / filename
            if not incoming.exists():
                continue
            if filename == "dexpaprika-request-ledger.json":
                # The authoritative request reservations are written atomically
                # to the shared root during requests; do not replace with a stale snapshot.
                continue
            b = json.loads(incoming.read_text(encoding="utf-8"))
            if filename == "birdeye-rotation-state.json" and target.exists():
                a = json.loads(target.read_text(encoding="utf-8"))
                if int(a.get("rotation_slots_total") or 0) == int(b.get("rotation_slots_total") or 0):
                    base_path = (baseline / filename) if baseline else None
                    base = json.loads(base_path.read_text(encoding="utf-8")) if base_path and base_path.exists() else a
                    total = max(1, int(b.get("rotation_slots_total") or 1))
                    delta = (int(b.get("next_slot") or 0) - int(base.get("next_slot") or 0)) % total
                    b["next_slot"] = (int(a.get("next_slot") or 0) + delta) % total
            if filename == "active-24h-watchlist.json" and target.exists():
                a = json.loads(target.read_text(encoding="utf-8"))
                positions = {str(x.get("identity")): x for x in a.get("positions", []) if x.get("identity")}
                for row in b.get("positions", []):
                    identity = str(row.get("identity") or "")
                    if identity and (identity not in positions or
                                     (row.get("last_checked_utc") or "") >= (positions[identity].get("last_checked_utc") or "")):
                        positions[identity] = row
                b["positions"] = list(positions.values())[-int(b.get("max_positions") or 12):]
                b["position_count"] = len(b["positions"])
            if filename == "completed-24h-watchlist.json" and target.exists():
                a = json.loads(target.read_text(encoding="utf-8"))
                seen = {str(x.get("identity")) + ":" + str(x.get("entry_timestamp_utc")) for x in a}
                b = a + [x for x in b if str(x.get("identity")) + ":" + str(x.get("entry_timestamp_utc")) not in seen]
            _write_json_atomic(target, b)

        check_time()
        forecasts = v418_pipeline.all_forecasts(memory_dir)
        outcomes = v418_pipeline.all_outcomes(memory_dir)
        check_time()
        calibration = v419_upgrade.build_calibration_model(forecasts, outcomes, as_of_utc=utc_now())
        check_time()
        _write_json_atomic(memory_dir / "calibration-state.json", calibration)
        manifest_path = memory_dir / "memory-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        snapshot_manifest_path = snapshot / "memory-manifest.json"
        snapshot_manifest = json.loads(snapshot_manifest_path.read_text(encoding="utf-8")) if snapshot_manifest_path.exists() else {}
        if (snapshot_manifest.get("last_snapshot_utc") or "") >= (manifest.get("last_snapshot_utc") or ""):
            for key in ("last_run_id", "last_snapshot_utc", "last_full_scan", "last_fast_refresh",
                        "latest_writer", "pipeline_version", "core_runtime_version", "collector_overlay_version",
                        "discovery_architecture_version", "discovery_sources_version", "idempotency_key"):
                if key in snapshot_manifest:
                    manifest[key] = snapshot_manifest[key]
        manifest.update({
            "historical_forecasts": len(forecasts), "historical_outcomes": len(outcomes),
            "forecast_partitions": len(list((memory_dir / "forecasts").glob("*.jsonl.gz"))),
            "outcome_partitions": len(list((memory_dir / "outcomes").glob("*.jsonl.gz"))),
            "calibration_summary": {k: calibration.get(k) for k in (
                "calibration_mode", "resolved_24h_outcomes_used", "brier_score_24h_breakout_10pct",
                "log_loss_24h_breakout_10pct", "minimum_bucket_observations_for_probability",
                "time_boundary_policy", "as_of_utc")},
            "local_memory_commit_utc": utc_now(),
        })
        _write_json_atomic(manifest_path, manifest)


def build_scan_command(args: argparse.Namespace, outdir: Path, base_budget: float) -> list[str]:
    command = [
        sys.executable, "scripts/run_full_scan.py", "--outdir", str(outdir),
        "--max-runtime-minutes", str(base_budget), "--defer-completion", "--resume",
        "--gecko-pages", str(args.gecko_pages), "--network-sweep-limit", str(args.network_sweep_limit),
        "--gecko-request-interval", "12", "--retries", "2", "--dex-search-limit", "12",
        "--dex-lookup-limit", "160", "--workers", str(args.workers),
        "--preflight-workers", str(args.preflight_workers),
    ]
    if args.birdeye:
        command.append("--birdeye")
    return command


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--outdir", type=Path, required=True, help="unique output directory for this run")
    p.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY, help="persistent local learning memory")
    p.add_argument("--max-runtime-minutes", type=float, default=170,
                   help="whole local run deadline; maximum 180 minutes")
    p.add_argument("--gecko-pages", type=int, default=1,
                   help="GeckoTerminal pages per list (1-3; default 1)")
    p.add_argument("--network-sweep-limit", type=int, default=12,
                   help="rotating GeckoTerminal networks per scan (0-24; default 12)")
    p.add_argument("--workers", type=int, default=4,
                   help="maximum scanner request threads (1-12; local default 4)")
    p.add_argument("--preflight-workers", type=int, default=4,
                   help="maximum parallel preflight probes (1-12; local default 4)")
    p.add_argument("--birdeye", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_arg_parser()
    args = p.parse_args(argv)
    if not 1 <= args.max_runtime_minutes <= 180:
        p.error("--max-runtime-minutes must be between 1 and 180")
    if not 1 <= args.gecko_pages <= 3:
        p.error("--gecko-pages must be between 1 and 3 for a bounded local scan")
    if not 0 <= args.network_sweep_limit <= 24:
        p.error("--network-sweep-limit must be between 0 and 24")
    if not 1 <= args.workers <= 12 or not 1 <= args.preflight_workers <= 12:
        p.error("--workers and --preflight-workers must be between 1 and 12")
    outdir, memory_dir = args.outdir.resolve(), args.memory_dir.resolve()
    if outdir.exists() and any(outdir.iterdir()):
        p.error(f"output directory must be new or empty: {outdir}")
    outdir.mkdir(parents=True, exist_ok=True)
    run_id = "local-" + uuid.uuid4().hex
    started, started_utc = time.monotonic(), utc_now()
    deadline = started + args.max_runtime_minutes * 60
    log_path = outdir / "local-run.log"
    metadata = build_run_metadata(args)
    lock_path = memory_dir.parent / (memory_dir.name + ".lock")
    snapshot = outdir / "memory-snapshot"
    baseline = outdir / "memory-baseline"
    status_path = outdir / "scan-status.json"
    state = {
        "status": "IN_PROGRESS", "run_id": run_id, "scan_started_utc": started_utc,
        "process_id": os.getpid(),
        **metadata,
    }
    status_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    _prevent_system_sleep(True)
    try:
        (outdir / "scan-pid.txt").write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    # Immediately render status-dashboard.html so Scan History shows this scan as IN_PROGRESS
    try:
        subprocess.run(
            [sys.executable, "scripts/status_dashboard.py", "--output", str(repo_root / "status-dashboard.html")],
            cwd=str(repo_root),
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass
    try:
        from scripts.file_lock import locked_file
        with locked_file(lock_path):
            ensure_memory(memory_dir)
            shutil.copytree(memory_dir, snapshot)
            baseline.mkdir(parents=True, exist_ok=True)
            for name in ("network-rotation-state.json", "birdeye-rotation-state.json"):
                source = memory_dir / name
                if source.exists():
                    shutil.copy2(source, baseline / name)
        runtime_root = prepare_runtime(memory_dir.parent / "runtime", deadline, log_path)
        env = os.environ.copy()
        env["SCANNER_MEMORY_DIR"] = str(snapshot)
        env["ROCKET_HUNTER_SHARED_MEMORY_DIR"] = str(memory_dir)
        env["ROCKET_HUNTER_RUN_ID"] = run_id
        base_budget = max(1.0, min(150.0, args.max_runtime_minutes * 0.72))
        cmds = [build_scan_command(args, outdir, base_budget)]
        _run_required(cmds[0], deadline, env, log_path, runtime_root,
                      "bounded discovery/preflight/report stage failed")
        stages = [
            [sys.executable, "-m", "scripts.v418_pipeline", "--outdir", str(outdir), "--memory-dir", str(snapshot), "--run-id", run_id],
            [sys.executable, "-m", "scripts.v418_forward_guard", "--outdir", str(outdir), "--memory-dir", str(snapshot), "--run-id", run_id],
            [sys.executable, "-m", "scripts.v419_upgrade", "--outdir", str(outdir), "--memory-dir", str(snapshot), "--run-id", run_id],
            [sys.executable, "scripts/post_scan_audit.py", "--report", str(outdir / "rocket-hunt.json"),
             "--markdown", str(outdir / "rocket-hunt.md"), "--timeout", "8", "--gecko-cvd-limit", "0", "--book-limit", "0", "--security-limit", "24", "--router-limit", "24"],
        ]
        for cmd in stages:
            _run_required(cmd, deadline, env, log_path, runtime_root,
                          "quality, memory, or safety-audit stage failed")
        forward = [sys.executable, "scripts/forward_24h_report.py", "--report", str(outdir / "rocket-hunt.json"),
                   "--markdown", str(outdir / "forward-24h-rocket.md"), "--out", str(outdir / "forward-24h-rocket.json"),
                   "--dex-limit", "140", "--cex-limit", "25", "--timeout", "8", "--gecko-interval", "12.0"]
        (outdir / "forward-24h-rocket.md").write_text("", encoding="utf-8")
        _run_required(forward, deadline, env, log_path, runtime_root,
                      "forward report generation failed")
        post = [
            [sys.executable, "-m", "scripts.v418_forward_guard", "--outdir", str(outdir), "--memory-dir", str(snapshot), "--run-id", run_id],
            [sys.executable, "-m", "scripts.v419_upgrade", "--outdir", str(outdir), "--memory-dir", str(snapshot), "--run-id", run_id],
            [sys.executable, "-m", "scripts.v423_provider_expansion", "--post-scan", "--outdir", str(outdir), "--memory-dir", str(snapshot), "--run-id", run_id],
            [sys.executable, "-m", "scripts.v424_birdeye_free", "--post-scan", "--outdir", str(outdir), "--memory-dir", str(snapshot), "--run-id", run_id],
            [sys.executable, "-m", "scripts.v425_early_breakout", "--post-scan", "--outdir", str(outdir), "--memory-dir", str(snapshot), "--run-id", run_id],
            [sys.executable, "scripts/render_forward_watchlist_markdown.py", "--input", str(outdir / "forward-24h-rocket.json"), "--output", str(outdir / "forward-24h-rocket.md")],
        ]
        for cmd in post:
            _run_required(cmd, deadline, env, log_path, runtime_root,
                          "final qualification, memory, or rendering stage failed")
        if not (outdir / "forward-24h-rocket.json").is_file() or not (outdir / "forward-24h-rocket.md").is_file():
            raise RuntimeError("final forward report is incomplete")
        merge_memory(snapshot, memory_dir, lock_path, baseline, deadline)
        final = [sys.executable, "scripts/run_full_scan.py", "--mark-complete", "--outdir", str(outdir)]
        _run_required(final, deadline, env, log_path, runtime_root,
                      "could not finalize scan state")
        state.update(json.loads(status_path.read_text(encoding="utf-8")))
        state.update({"status": "COMPLETED", "scan_finished_utc": utc_now(),
                      "scan_duration_seconds": round(time.monotonic() - started, 3)})
        status_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        for name in ("rocket-hunt.json", "forward-24h-rocket.json"):
            path = outdir / name
            if path.exists():
                doc = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(doc, dict):
                    doc.update({key: value for key, value in state.items() if key != "status"})
                    doc["scan_status"] = "COMPLETED"
                    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if (outdir / "forward-24h-rocket.json").is_file():
            render_final = [
                sys.executable, "scripts/render_forward_watchlist_markdown.py",
                "--input", str(outdir / "forward-24h-rocket.json"),
                "--output", str(outdir / "forward-24h-rocket.md"),
            ]
            _run(render_final, deadline, env, log_path, runtime_root)
        try:
            if (outdir / "forward-24h-rocket.html").exists():
                shutil.copy2(outdir / "forward-24h-rocket.html", repo_root / "forward-24h-rocket.html")
            if (outdir / "forward-24h-rocket.md").exists():
                shutil.copy2(outdir / "forward-24h-rocket.md", repo_root / "forward-24h-rocket.md")
        except Exception:
            pass

        # Automatically update status-dashboard.html so scan history always shows all completed scans
        try:
            render_dash = [
                sys.executable, "scripts/status_dashboard.py",
                "--output", str(repo_root / "status-dashboard.html"),
            ]
            _run(render_dash, deadline, env, log_path, runtime_root)
        except Exception:
            pass

        try:
            from scripts import notification_manager
            fw_path = outdir / "forward-24h-rocket.json"
            act_cnt = 0
            rnk_cnt = 0
            top_name = "—"
            if fw_path.exists():
                fw_data = json.loads(fw_path.read_text(encoding="utf-8"))
                act_cnt = len(fw_data.get("verified_actionable_opportunities") or [])
                rnk_cnt = len(fw_data.get("unverified_research_watchlist") or []) + act_cnt
                if act_cnt > 0:
                    first = fw_data["verified_actionable_opportunities"][0]
                    top_name = f"{first.get('pair')} (+{first.get('tp_upside_pct')}%)"
            f_utc = state.get("scan_finished_utc") or utc_now()
            s_utc = state.get("scan_started_utc") or utc_now()
            ist_tz = timezone(timedelta(hours=2))
            f_ist = datetime.fromisoformat(f_utc.replace("Z", "+00:00")).astimezone(ist_tz).strftime("%Y-%m-%d %H:%M:%S IST")
            s_ist = datetime.fromisoformat(s_utc.replace("Z", "+00:00")).astimezone(ist_tz).strftime("%Y-%m-%d %H:%M:%S IST")
            dur = f"{int(state.get('scan_duration_seconds', 0) // 60)}m {int(state.get('scan_duration_seconds', 0) % 60)}s"

            notif_res = notification_manager.send_scan_completed_notification(
                run_id=outdir.name,
                status="COMPLETED",
                started_ist=s_ist,
                finished_ist=f_ist,
                duration_str=dur,
                ranked_count=rnk_cnt,
                actionable_count=act_cnt,
                top_candidate=top_name
            )
            print(f"Scan completion notification dispatched: {notif_res}")
        except Exception as e:
            print(f"Notification check: {e}")

        print(f"Completed local scan: {outdir}")
        return 0
    except KeyboardInterrupt:
        state["status"] = "INTERRUPTED"
        state["failure_reason"] = "interrupted by user"
        returncode = 130
    except TimeoutError as exc:
        state["status"] = "INTERRUPTED"
        state["failure_reason"] = str(exc)
        print(f"Local scan interrupted: {exc}. See {log_path}", file=sys.stderr)
        returncode = 124
    except Exception as exc:
        state["status"] = "INTERRUPTED" if time.monotonic() >= deadline else "FAILED"
        state["failure_reason"] = str(exc)
        print(f"Local scan {state['status'].lower()}: {exc}. See {log_path}", file=sys.stderr)
        returncode = 124 if state["status"] == "INTERRUPTED" else 1
    finally:
        if state.get("status") != "COMPLETED":
            state["scan_finished_utc"] = utc_now()
            state["scan_duration_seconds"] = round(time.monotonic() - started, 3)
            status_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            for name in ("rocket-hunt.json", "forward-24h-rocket.json"):
                path = outdir / name
                if path.exists():
                    try:
                        doc = json.loads(path.read_text(encoding="utf-8"))
                        if isinstance(doc, dict):
                            doc.update({key: value for key, value in state.items()
                                        if key in {"run_id", "scan_started_utc", "scan_finished_utc",
                                                   "scan_duration_seconds", "scanner_version",
                                                   "configuration_fingerprint", "local_configuration"}})
                            doc["scan_status"] = state["status"]
                            doc["verified_actionable_opportunities"] = []
                            doc["best_immediate_entry"] = None
                            doc["top_rocket_execution_ready"] = False
                            path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                    except (OSError, ValueError):
                        pass

            # Notify user on scan failure or interrupt if configured
            try:
                from scripts import notification_manager
                f_utc = state.get("scan_finished_utc") or utc_now()
                s_utc = state.get("scan_started_utc") or utc_now()
                ist_tz = timezone(timedelta(hours=2))
                f_ist = datetime.fromisoformat(f_utc.replace("Z", "+00:00")).astimezone(ist_tz).strftime("%Y-%m-%d %H:%M:%S IST")
                s_ist = datetime.fromisoformat(s_utc.replace("Z", "+00:00")).astimezone(ist_tz).strftime("%Y-%m-%d %H:%M:%S IST")
                dur = f"{int(state.get('scan_duration_seconds', 0) // 60)}m {int(state.get('scan_duration_seconds', 0) % 60)}s"
                notification_manager.send_scan_completed_notification(
                    run_id=outdir.name,
                    status=state["status"],
                    started_ist=s_ist,
                    finished_ist=f_ist,
                    duration_str=dur,
                    ranked_count=0,
                    actionable_count=0,
                    top_candidate=state.get("failure_reason", state["status"])
                )
            except Exception:
                pass

        # Always re-render status-dashboard.html to ensure scan history is never stale
        try:
            subprocess.run(
                [sys.executable, "scripts/status_dashboard.py", "--output", str(repo_root / "status-dashboard.html")],
                cwd=str(repo_root),
                capture_output=True,
                timeout=15,
            )
        except Exception:
            pass

        if snapshot.exists():
            shutil.rmtree(snapshot)
        _prevent_system_sleep(False)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
