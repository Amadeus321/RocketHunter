#!/usr/bin/env python3
"""Dedicated live terminal and HTML monitor for Rocket Hunter scans.

Runs outside of the IDE in a dedicated console window or updates a live browser page.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "rocket-runs"
MONITOR_HTML = ROOT / "live-monitor.html"

STAGE_NAMES = [
    ("1. Preflight System & Network Probes", ["preflight.json", "[1/2] Checking market-source"]),
    ("2. Market Sweep & Universe Discovery", ["rocket-hunt.json", "Rocket Hunt 4."]),
    ("3. Post-Scan Audits (CVD, Depth, Security)", ["Post-scan audits:", "Post-scan audit:"]),
    ("4. 24h Forward & Single-TP Modeling", ["Building scan-start-anchored DEX Top Rocket", "forward-24h-rocket.json"]),
    ("5. Early Breakout & Live Price Refresh", ["[v425] Early-breakout", "classify_final_candidates"]),
    ("6. Deterministic Qualification Gate (+40%)", ["qualify_final_actionable_candidates", "verified_actionable_opportunities"]),
    ("7. Final Report Finalization", ["Wrote finalized forward watchlist", "Updated root forward HTML watchlist"]),
]


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == 259  # STILL_ACTIVE
            return False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def get_latest_completed_run_dir() -> Path | None:
    if not RUNS_DIR.exists():
        return None
    candidates = []
    for p in RUNS_DIR.iterdir():
        if p.is_dir() and not p.name.startswith("_") and (p / "scan-status.json").exists():
            try:
                st = json.loads((p / "scan-status.json").read_text(encoding="utf-8"))
                if st.get("status") == "COMPLETED":
                    candidates.append((str(st.get("scan_finished_utc") or ""), p))
            except Exception:
                pass
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def get_latest_run_dir() -> Path | None:
    if not RUNS_DIR.exists():
        return None
    candidates = []
    for p in RUNS_DIR.iterdir():
        if p.is_dir() and not p.name.startswith("_") and (p / "scan-status.json").exists():
            candidates.append(p)
    if not candidates:
        return None

    def _run_sort_key(p: Path) -> tuple[str, str, str]:
        finished = ""
        started = ""
        try:
            st = json.loads((p / "scan-status.json").read_text(encoding="utf-8"))
            finished = str(st.get("scan_finished_utc") or "")
            started = str(st.get("started_utc") or st.get("scan_started_utc") or "")
        except Exception:
            pass
        return (finished, started, p.name)

    for p in sorted(candidates, key=_run_sort_key, reverse=True):
        try:
            data = json.loads((p / "scan-status.json").read_text(encoding="utf-8"))
            if data.get("status") == "IN_PROGRESS":
                pid = data.get("process_id")
                pid_file = p / "scan-pid.txt"
                if pid_file.exists():
                    try:
                        pid = int(pid_file.read_text(encoding="utf-8").strip())
                    except Exception:
                        pass
                if pid and is_pid_running(int(pid)):
                    return p
        except Exception:
            pass

    latest_completed = get_latest_completed_run_dir()
    if latest_completed:
        return latest_completed

    candidates.sort(key=_run_sort_key, reverse=True)
    return candidates[0]


def parse_utc_time(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        clean = s.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(clean)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except Exception:
        return None


def to_israel_time_str(utc_dt: dt.datetime | None) -> str:
    if not utc_dt:
        return "Unknown"
    israel_tz = dt.timezone(dt.timedelta(hours=2))
    local_dt = utc_dt.astimezone(israel_tz)
    return local_dt.strftime("%H:%M:%S IST")


def format_duration(seconds: float) -> str:
    total_sec = int(max(0, seconds))
    hrs = total_sec // 3600
    mins = (total_sec % 3600) // 60
    secs = total_sec % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


def determine_stage(log_text: str, is_completed: bool) -> tuple[int, str]:
    if is_completed:
        return 7, "COMPLETED"
    if "Wrote finalized forward watchlist" in log_text or "Updated root forward HTML" in log_text:
        return 7, "Writing Final Reports"
    if "Early-breakout" in log_text or "classify_final" in log_text:
        return 5, "Live Price Refresh & Qualification"
    if "forward-24h" in log_text or "Building scan-start-anchored" in log_text:
        return 4, "24h Forward & Single-TP Modeling"
    if "Post-scan audit" in log_text or "Auditing top candidates" in log_text:
        return 3, "Post-Scan Auditing (CVD, Depth, Security)"
    if "Rocket Hunt" in log_text or "dexscreener" in log_text or "gecko" in log_text:
        return 2, "Market Sweep & Universe Discovery"
    return 1, "Preflight & Health Checks"


def write_live_html(run_name: str, run_id_display: str, version_display: str, ranked_count: int,
                    status: str, elapsed_str: str, started_ist: str,
                    now_ist: str, stage_idx: int, stage_desc: str, pct: int,
                    log_lines: list[str]) -> None:
    is_scan_done = (status == "COMPLETED" or pct >= 100 or stage_desc == "COMPLETED")
    pct_val = 100 if is_scan_done else pct
    progress_bar = f'<div style="background:#1e293b;border-radius:8px;overflow:hidden;height:24px;position:relative;margin:16px 0;"><div style="background:linear-gradient(90deg,#06b6d4,#10b981);width:{pct_val}%;height:100%;transition:width 0.5s;"></div><span style="position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);font-weight:700;font-size:12px;color:#fff;">{pct_val}% Completed</span></div>'

    stages_html = []
    stage_labels = [
        "1. Preflight System & Network Probes",
        "2. Market Sweep & Universe Discovery (9,000+ Assets)",
        "3. Post-Scan Audits (CVD, Depth, Security, Routers)",
        "4. 24h Forward & Single-TP Modeling (OHLCV Candles)",
        "5. Early Breakout & Scan-End Live Price Refresh",
        "6. Deterministic Qualification Gate (+40% single-TP)",
        "7. Final Report Finalization & Results",
    ]
    for i, label in enumerate(stage_labels, 1):
        if is_scan_done or i < stage_idx:
            badge = '<span style="color:#10b981;font-weight:700;">✔ COMPLETED</span>'
            row_style = "color:#94a3b8;"
        elif i == stage_idx:
            badge = '<span style="color:#06b6d4;font-weight:700;animation:blink 1.5s infinite;">▶ IN PROGRESS</span>'
            row_style = "color:#f8fafc;font-weight:600;background:rgba(6,182,212,0.1);border-left:3px solid #06b6d4;padding-left:8px;"
        else:
            badge = '<span style="color:#64748b;">QUEUED</span>'
            row_style = "color:#475569;"
        stages_html.append(f'<div style="display:flex;justify-content:space-between;padding:8px 6px;{row_style}"><span>{label}</span>{badge}</div>')

    log_box = "".join(f"<div>{line}</div>" for line in log_lines[-12:])
    active_stage_str = "7/7 (Done)" if is_scan_done else f"{stage_idx}/7"
    badge_bg = "rgba(16, 185, 129, 0.2)" if is_scan_done else "#065f46"
    badge_color = "#34d399" if is_scan_done else "#38bdf8"

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="2">
<title>🚀 Rocket Hunter — Live Monitor</title>
<style>
body {{ background:#0b1120; color:#e2e8f0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; margin:0; padding:24px; }}
.card {{ background:#111827; border:1px solid #1e293b; border-radius:12px; padding:20px; max-width:880px; margin:0 auto 20px auto; box-shadow:0 10px 25px rgba(0,0,0,0.5); }}
.header {{ display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #1e293b; padding-bottom:14px; margin-bottom:16px; }}
.title {{ font-size:22px; font-weight:800; color:#38bdf8; display:flex; align-items:center; gap:8px; }}
.badge-status {{ background:#065f46; color:#34d399; padding:4px 12px; border-radius:999px; font-weight:700; font-size:12px; }}
.grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:16px; }}
.stat {{ background:#0f172a; padding:12px; border-radius:8px; border:1px solid #1e293b; text-align:center; }}
.stat-val {{ font-size:20px; font-weight:800; color:#f8fafc; margin-top:4px; }}
.stat-lbl {{ font-size:11px; color:#94a3b8; text-transform:uppercase; font-weight:600; }}
.terminal {{ background:#050811; border:1px solid #1e293b; border-radius:8px; padding:14px; font-family:Consolas,monospace; font-size:12px; color:#a5f3fc; line-height:1.6; max-height:220px; overflow-y:auto; }}
@keyframes blink {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.5; }} }}
</style>
</head>
<body>
<div class="card">
  <div style="font-size:13px;font-weight:700;color:#38bdf8;background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:9px 14px;margin-bottom:14px;letter-spacing:0.02em;">
    Scan: id ({run_id_display}) version({version_display}) · {ranked_count} Ranked Candidates counted
  </div>
  <div class="header">
    <div class="title">🚀 Rocket Hunter — Live Scan Monitor</div>
    <div><span class="badge-status" style="background:{badge_bg};color:{badge_color};">{status}</span></div>
  </div>
  <div class="grid">
    <div class="stat"><div class="stat-lbl">Elapsed Time</div><div class="stat-val" style="color:#38bdf8;">⏱️ {elapsed_str}</div></div>
    <div class="stat"><div class="stat-lbl">Started (Israel Time)</div><div class="stat-val">{started_ist}</div></div>
    <div class="stat"><div class="stat-lbl">Clock (Israel Time)</div><div class="stat-val">{now_ist}</div></div>
    <div class="stat"><div class="stat-lbl">Active Stage</div><div class="stat-val" style="color:#10b981;">{active_stage_str}</div></div>
  </div>
  {progress_bar}
  <div style="background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:12px;margin-bottom:16px;">
    {''.join(stages_html)}
  </div>
  <div style="font-size:12px;font-weight:700;color:#94a3b8;margin-bottom:6px;">LIVE SCAN LOG STREAM:</div>
  <div class="terminal">{log_box}</div>
  <div style="margin-top:16px;text-align:center;">
    <a href="forward-24h-rocket.html" onclick="if(window.parent&&window.parent!=window){{window.parent.postMessage({{action:'navigate',page:'rocket'}},'*');return false;}}" style="display:inline-block;background:#0284c7;color:#fff;text-decoration:none;font-weight:700;padding:10px 20px;border-radius:8px;font-size:13px;cursor:pointer;">Open Final Report (forward-24h-rocket.html) ↗</a>
  </div>
</div>
</body>
</html>"""
    try:
        MONITOR_HTML.write_text(html_content, encoding="utf-8")
    except Exception:
        pass


def run_loop(target_run_dir: Path | None = None, loop: bool = True) -> None:
    os.system("cls" if os.name == "nt" else "clear")
    while True:
        run_dir = target_run_dir or get_latest_run_dir()
        if not run_dir:
            print("Searching for active Rocket Hunter scan...", end="\r")
            time.sleep(1)
            continue

        status_file = run_dir / "scan-status.json"
        log_file = run_dir / "local-run.log"

        status_data = {}
        if status_file.exists():
            try:
                status_data = json.loads(status_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        scan_status = status_data.get("status", "IN_PROGRESS")
        is_completed = (scan_status == "COMPLETED")

        started_utc = parse_utc_time(status_data.get("scan_started_utc"))
        started_ist = to_israel_time_str(started_utc)

        finished_utc = parse_utc_time(status_data.get("scan_finished_utc"))
        now_utc = dt.datetime.now(dt.timezone.utc)
        now_ist = to_israel_time_str(now_utc)

        elapsed_sec = 0.0
        if is_completed:
            if "scan_duration_seconds" in status_data and status_data["scan_duration_seconds"] is not None:
                try:
                    elapsed_sec = float(status_data["scan_duration_seconds"])
                except Exception:
                    pass
            elif finished_utc and started_utc:
                elapsed_sec = max(0.0, (finished_utc - started_utc).total_seconds())
            elif started_utc:
                elapsed_sec = max(0.0, (now_utc - started_utc).total_seconds())
        elif started_utc:
            elapsed_sec = max(0.0, (now_utc - started_utc).total_seconds())

        elapsed_str = format_duration(elapsed_sec)

        log_lines = []
        if log_file.exists():
            try:
                text = log_file.read_text(encoding="utf-8", errors="replace")
                log_lines = [l.strip() for l in text.splitlines() if l.strip()]
            except Exception:
                pass

        last_log = "\n".join(log_lines[-40:]) if log_lines else ""
        stage_idx, stage_desc = determine_stage(last_log, is_completed)

        pct_map = {1: 15, 2: 35, 3: 50, 4: 70, 5: 85, 6: 95, 7: 100}
        pct = pct_map.get(stage_idx, 50)
        if is_completed:
            pct = 100

        # Determine run folder name and project version
        # Use exact run folder name so the user can easily locate it in rocket-runs/
        run_id_val = run_dir.name
        version_val = status_data.get("scanner_version")
        if not version_val:
            try:
                version_val = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
            except Exception:
                version_val = "v4.26.6"
        if not version_val.startswith("v"):
            version_val = f"v{version_val}"

        # Determine count of ranked candidates
        ranked_count = 0
        forward_path = run_dir / "forward-24h-rocket.json"
        hunt_path = run_dir / "rocket-hunt.json"
        if forward_path.exists():
            try:
                fdoc = json.loads(forward_path.read_text(encoding="utf-8"))
                ranking = fdoc.get("forward_opportunity_ranking")
                if isinstance(ranking, dict) and ranking.get("candidate_count") is not None:
                    ranked_count = int(ranking["candidate_count"])
                elif isinstance(fdoc.get("candidates"), list):
                    ranked_count = len(fdoc["candidates"])
                elif isinstance(fdoc.get("unverified_research_watchlist"), list):
                    ranked_count = len(fdoc["unverified_research_watchlist"]) + len(fdoc.get("verified_actionable_opportunities") or [])
            except Exception:
                pass
        elif hunt_path.exists():
            try:
                hdoc = json.loads(hunt_path.read_text(encoding="utf-8"))
                for k in ("execution_candidate_shortlist", "top_24h_movers", "most_actionable_liquidity_screen"):
                    if isinstance(hdoc.get(k), list) and len(hdoc[k]) > ranked_count:
                        ranked_count = len(hdoc[k])
            except Exception:
                pass

        # Update HTML file with requested top line
        write_live_html(run_dir.name, run_id_val, version_val, ranked_count, scan_status,
                        elapsed_str, started_ist, now_ist, stage_idx, stage_desc, pct, log_lines)

        # Draw Console Screen
        bar_len = 36
        filled = int(bar_len * (pct / 100.0))
        bar = "█" * filled + "░" * (bar_len - filled)

        output = []
        output.append("═" * 70)
        output.append("       🚀 ROCKET HUNTER — LIVE SCAN PROCESS & TIME MONITOR")
        output.append("═" * 70)
        output.append(f"  Scan: id ({run_id_val}) version({version_val}) · {ranked_count} Ranked Candidates counted")
        output.append("─" * 70)
        output.append(f"  Run Directory : {run_dir.name}")
        output.append(f"  Status        : {scan_status}")
        output.append(f"  Scan Started  : {started_ist}")
        output.append(f"  Current Clock : {now_ist} (Israel Time)")
        output.append(f"  ⏱ Elapsed Time: {elapsed_str}")
        output.append("─" * 70)
        output.append(f"  Progress: [{bar}] {pct}%")
        output.append(f"  Active Stage  : Step {stage_idx}/7 — {stage_desc}")
        output.append("─" * 70)
        output.append("  Pipeline Stages:")
        labels = [
            "1. Preflight System & Network Probes",
            "2. Market Sweep & Universe Discovery (9,000+ assets)",
            "3. Post-Scan Audits (CVD, Depth, Security, Routers)",
            "4. 24h Forward & Single-TP Modeling (OHLCV Candles)",
            "5. Early Breakout & Scan-End Live Price Refresh",
            "6. Deterministic Qualification Gate (+40% single-TP)",
            "7. Final Report Finalization & OKX DEX Execution Links",
        ]
        for i, lbl in enumerate(labels, 1):
            if i < stage_idx or is_completed:
                mark = "[✔ COMPLETED]"
            elif i == stage_idx:
                mark = "[▶ RUNNING  ]"
            else:
                mark = "[  QUEUED   ]"
            output.append(f"    {mark} {lbl}")
        output.append("─" * 70)
        output.append("  Live Log Tail:")
        for l in log_lines[-4:]:
            output.append(f"    | {l[:64]}")
        output.append("═" * 70)
        output.append("  Browser Dashboard: open live-monitor.html in Chrome/Edge")
        output.append("  (Press Ctrl+C to close this monitor at any time)")

        sys.stdout.write("\033[H" + "\n".join(output) + "\n")
        sys.stdout.flush()

        if not loop:
            break
        time.sleep(1)


if __name__ == "__main__":
    run_loop()
