#!/usr/bin/env python3
"""Unified Desktop Application for Rocket Hunter.

Houses Control Center, Live Monitor, Forward 24h Rocket, and Status Dashboard
inside one standalone window with a permanent side navigation menu. No external apps opened.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import datetime as dt
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

# Resolve root directory
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parents[1]

SCRIPTS_DIR = ROOT / "scripts"
RUNS_DIR = ROOT / "rocket-runs"

try:
    from scripts import user_trade_learning
except ImportError:
    import user_trade_learning

try:
    from scripts import notification_manager
except ImportError:
    import notification_manager


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.c_size_t),
        ('th32ModuleID', wintypes.DWORD),
        ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD),
        ('pcPriClassBase', wintypes.LONG),
        ('dwFlags', wintypes.DWORD),
        ('szExeFile', ctypes.c_char * 260)
    ]


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    STILL_ACTIVE = 259
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == STILL_ACTIVE
        return False
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def get_process_tree(parent_pid: int) -> list[int]:
    if parent_pid <= 0 or os.name != "nt":
        return [parent_pid] if parent_pid > 0 else []
    TH32CS_SNAPPROCESS = 0x00000002
    hSnapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if hSnapshot == -1:
        return [parent_pid]
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
    parent_map = {}
    res = ctypes.windll.kernel32.Process32First(hSnapshot, ctypes.byref(pe))
    while res:
        parent_map.setdefault(pe.th32ParentProcessID, []).append(pe.th32ProcessID)
        res = ctypes.windll.kernel32.Process32Next(hSnapshot, ctypes.byref(pe))
    ctypes.windll.kernel32.CloseHandle(hSnapshot)

    result = []
    queue = [parent_pid]
    while queue:
        curr = queue.pop(0)
        if curr not in result:
            result.append(curr)
        for child in parent_map.get(curr, []):
            if child not in result:
                queue.append(child)
    return result


def suspend_process(pid: int) -> bool:
    if os.name != "nt":
        return False
    PROCESS_ALL_ACCESS = 0x1F0FFF
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not handle:
        return False
    try:
        res = ctypes.CDLL("ntdll.dll").NtSuspendProcess(handle)
        return res == 0
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def resume_process(pid: int) -> bool:
    if os.name != "nt":
        return False
    PROCESS_ALL_ACCESS = 0x1F0FFF
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not handle:
        return False
    try:
        res = ctypes.CDLL("ntdll.dll").NtResumeProcess(handle)
        return res == 0
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def suspend_process_tree(parent_pid: int) -> int:
    pids = get_process_tree(parent_pid)
    count = 0
    # Suspend from children up to root
    for p in reversed(pids):
        if is_pid_running(p):
            if suspend_process(p):
                count += 1
    return count


def resume_process_tree(parent_pid: int) -> int:
    pids = get_process_tree(parent_pid)
    count = 0
    # Resume root then children
    for p in pids:
        if is_pid_running(p):
            if resume_process(p):
                count += 1
    return count


def get_version() -> str:
    ver_file = ROOT / "VERSION"
    if ver_file.exists():
        try:
            v = ver_file.read_text(encoding="utf-8").strip()
            return f"v{v}" if not v.startswith("v") else v
        except Exception:
            pass
    return "v4.26.6"


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

    # 1. Prioritize any actively running scan (IN_PROGRESS) whose process is genuinely still alive
    for p in sorted(candidates, key=_run_sort_key, reverse=True):
        try:
            data = json.loads((p / "scan-status.json").read_text(encoding="utf-8"))
            if data.get("status") == "IN_PROGRESS":
                pid = find_scan_pid(p)
                if pid and is_pid_running(pid):
                    return p
                else:
                    # Stale or dead process: fail closed and mark INTERRUPTED
                    now_str = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                    data["status"] = "INTERRUPTED"
                    data["failure_reason"] = "Process terminated unexpectedly or computer rebooted; fail closed."
                    data["scan_finished_utc"] = now_str
                    (p / "scan-status.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    # 2. If no scan is actively running, prefer the latest completed scan for reporting/display
    latest_completed = get_latest_completed_run_dir()
    if latest_completed:
        return latest_completed

    candidates.sort(key=_run_sort_key, reverse=True)
    return candidates[0]


def format_duration(seconds: float) -> str:
    total_sec = int(max(0, seconds))
    hrs = total_sec // 3600
    mins = (total_sec % 3600) // 60
    secs = total_sec % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


def get_python_exe() -> str:
    if not getattr(sys, "frozen", False):
        if Path(sys.executable).is_file() and not sys.executable.lower().endswith("rockethunter.exe"):
            return str(Path(sys.executable).resolve())

    candidates = [
        shutil.which("python"),
        shutil.which("python3"),
        r"C:\Users\user\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe",
        r"C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe",
        r"C:\Program Files\Python313\python.exe",
        r"C:\Program Files\Python312\python.exe",
        r"C:\Program Files\Python311\python.exe",
    ]
    for c in candidates:
        if c and Path(c).is_file() and not c.lower().endswith("rockethunter.exe"):
            return str(Path(c).resolve())
    return "python"


def sync_root_reports():
    run_dir = get_latest_run_dir()
    if run_dir:
        src_html = run_dir / "forward-24h-rocket.html"
        dst_html = ROOT / "forward-24h-rocket.html"
        if src_html.exists():
            try:
                if not dst_html.exists() or src_html.stat().st_mtime > dst_html.stat().st_mtime:
                    shutil.copy2(src_html, dst_html)
            except Exception:
                pass
    # Keep status-dashboard.html scan history fresh if stale
    dst_dash = ROOT / "status-dashboard.html"
    status_py = SCRIPTS_DIR / "status_dashboard.py"
    if status_py.exists():
        need_refresh = False
        if not dst_dash.exists():
            need_refresh = True
        elif (time.time() - dst_dash.stat().st_mtime) > 60:
            need_refresh = True
        if need_refresh:
            try:
                py_exe = get_python_exe()
                subprocess.Popen(
                    [py_exe, str(status_py), "--output", str(dst_dash)],
                    cwd=str(ROOT),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
            except Exception:
                pass


def find_scan_pid(run_dir: Path) -> int | None:
    pid_file = run_dir / "scan-pid.txt"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            if is_pid_running(pid):
                return pid
        except Exception:
            pass

    status_file = run_dir / "scan-status.json"
    if status_file.exists():
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            pid = data.get("process_id")
            if pid and is_pid_running(int(pid)):
                return int(pid)
        except Exception:
            pass
    return None


def pause_scan() -> dict:
    run_dir = get_latest_run_dir()
    if not run_dir:
        return {"status": "ERROR", "message": "No scan directory found."}
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    if not (run_dir / ".paused").exists():
        (run_dir / ".paused").write_text(now_iso, encoding="utf-8")
    (run_dir / "scan-pause.flag").write_text("1", encoding="utf-8")

    pid = find_scan_pid(run_dir)
    if pid:
        suspended = suspend_process_tree(pid)
        return {"status": "OK", "message": f"Scan paused ({suspended} processes suspended)"}
    return {"status": "OK", "message": "Scan marked as paused."}


def resume_scan() -> dict:
    run_dir = get_latest_run_dir()
    if not run_dir:
        return {"status": "ERROR", "message": "No scan directory found."}
    flag1 = run_dir / ".paused"
    flag2 = run_dir / "scan-pause.flag"
    accum_file = run_dir / ".accumulated_pause"

    if flag1.exists():
        try:
            pause_start_str = flag1.read_text(encoding="utf-8").strip()
            pause_start_dt = dt.datetime.fromisoformat(pause_start_str.replace("Z", "+00:00"))
            now_dt = dt.datetime.now(dt.timezone.utc)
            paused_duration = max(0.0, (now_dt - pause_start_dt).total_seconds())

            current_accum = 0.0
            if accum_file.exists():
                try:
                    current_accum = float(accum_file.read_text(encoding="utf-8").strip())
                except Exception:
                    current_accum = 0.0
            accum_file.write_text(str(current_accum + paused_duration), encoding="utf-8")
        except Exception:
            pass
        flag1.unlink(missing_ok=True)
    if flag2.exists():
        flag2.unlink(missing_ok=True)

    pid = find_scan_pid(run_dir)
    if pid:
        resumed = resume_process_tree(pid)
        return {"status": "OK", "message": f"Scan continued ({resumed} processes resumed)"}
    return {"status": "OK", "message": "Scan continued."}


def get_live_status_payload() -> dict:
    sync_root_reports()
    version = get_version()
    run_dir = get_latest_run_dir()
    if not run_dir:
        return {
            "top_line": f"Last Finished Scan: None · version({version})",
            "last_finished_banner": f"Last Finished Scan: None · version({version})",
            "time_started": "--:--:--",
            "elapsed": "00:00:00",
            "clock_ist": "--:--:-- IST",
            "stage_idx": 0,
            "stage_desc": "Idle — Ready to scan",
            "progress_pct": 0,
            "is_completed": True,
            "is_paused": False,
            "ranked_count": 0,
            "stages": [],
            "log_tail": "No active scan logs found.",
        }

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
    is_paused = (run_dir / ".paused").exists() or (run_dir / "scan-pause.flag").exists()

    started_str = status_data.get("scan_started_utc")
    finished_str = status_data.get("scan_finished_utc")
    now_utc = dt.datetime.now(dt.timezone.utc)
    israel_tz = dt.timezone(dt.timedelta(hours=2))
    now_ist_str = now_utc.astimezone(israel_tz).strftime("%H:%M:%S IST")

    started_ist_str = "--:--:--"
    if started_str:
        try:
            s_dt = dt.datetime.fromisoformat(started_str.replace("Z", "+00:00"))
            started_ist_str = s_dt.astimezone(israel_tz).strftime("%H:%M:%S IST")
        except Exception:
            pass

    accum_paused_sec = 0.0
    accum_file = run_dir / ".accumulated_pause"
    if accum_file.exists():
        try:
            accum_paused_sec = float(accum_file.read_text(encoding="utf-8").strip())
        except Exception:
            accum_paused_sec = 0.0

    elapsed_sec = 0.0
    if is_completed:
        if "scan_duration_seconds" in status_data and status_data["scan_duration_seconds"] is not None:
            try:
                elapsed_sec = float(status_data["scan_duration_seconds"])
            except Exception:
                pass
        elif finished_str and started_str:
            try:
                f_dt = dt.datetime.fromisoformat(finished_str.replace("Z", "+00:00"))
                s_dt = dt.datetime.fromisoformat(started_str.replace("Z", "+00:00"))
                elapsed_sec = max(0.0, (f_dt - s_dt).total_seconds())
            except Exception:
                pass
    elif started_str:
        try:
            started_dt = dt.datetime.fromisoformat(started_str.replace("Z", "+00:00"))
            if is_paused:
                pause_file = run_dir / ".paused"
                if pause_file.exists():
                    try:
                        p_str = pause_file.read_text(encoding="utf-8").strip()
                        freeze_dt = dt.datetime.fromisoformat(p_str.replace("Z", "+00:00"))
                    except Exception:
                        freeze_dt = now_utc
                else:
                    freeze_dt = now_utc
                elapsed_sec = max(0.0, (freeze_dt - started_dt).total_seconds() - accum_paused_sec)
            else:
                elapsed_sec = max(0.0, (now_utc - started_dt).total_seconds() - accum_paused_sec)
        except Exception:
            pass

    elapsed_str = format_duration(elapsed_sec)

    finished_ist_str = "unavailable"
    if finished_str:
        try:
            f_dt = dt.datetime.fromisoformat(finished_str.replace("Z", "+00:00"))
            finished_ist_str = f_dt.astimezone(israel_tz).strftime("%H:%M:%S IST")
        except Exception:
            pass

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

    run_name = run_dir.name
    last_finished_banner = f"Last Finished Scan: id ({run_name}) version({version}), finished {finished_ist_str} · {ranked_count} Ranked Candidates"

    log_lines = []
    if log_file.exists():
        try:
            text = log_file.read_text(encoding="utf-8", errors="replace")
            log_lines = [l.strip() for l in text.splitlines() if l.strip()]
        except Exception:
            pass

    last_log = "\n".join(log_lines[-40:]) if log_lines else ""
    if is_completed:
        stage_idx, stage_desc, pct = 7, "All Stages Finalized & Verified", 100
        top_line = last_finished_banner
    elif scan_status == "INTERRUPTED":
        stage_idx, stage_desc, pct = 4, f"Scan Interrupted ({status_data.get('failure_reason', 'Stopped')})", 70
        top_line = f"Interrupted Scan: id ({run_name}) version({version}) · INTERRUPTED"
    elif scan_status == "FAILED":
        stage_idx, stage_desc, pct = 1, f"Scan Failed ({status_data.get('failure_reason', 'Error')})", 0
        top_line = f"Failed Scan: id ({run_name}) version({version}) · FAILED"
    elif is_paused:
        stage_idx, stage_desc, pct = 4, "Scan Paused by User — Click 'Continue' to Resume", 70
        top_line = f"Paused Scan: id ({run_name}) version({version}) · PAUSED"
    elif "Wrote finalized forward watchlist" in last_log or "Updated root forward HTML" in last_log:
        stage_idx, stage_desc, pct = 7, "Writing Final Reports", 98
        top_line = f"Active Scan: id ({run_name}) version({version}) · Step {stage_idx}/7 ({stage_desc})"
    elif "Early-breakout" in last_log or "classify_final" in last_log:
        stage_idx, stage_desc, pct = 5, "Live Price Refresh & Qualification", 85
        top_line = f"Active Scan: id ({run_name}) version({version}) · Step {stage_idx}/7 ({stage_desc})"
    elif "forward-24h" in last_log or "Building scan-start-anchored" in last_log:
        stage_idx, stage_desc, pct = 4, "24h Forward & Single-TP Modeling", 70
        top_line = f"Active Scan: id ({run_name}) version({version}) · Step {stage_idx}/7 ({stage_desc})"
    elif "Post-scan audit" in last_log or "Auditing top candidates" in last_log:
        stage_idx, stage_desc, pct = 3, "Post-Scan Audits (CVD, Depth, Security)", 50
        top_line = f"Active Scan: id ({run_name}) version({version}) · Step {stage_idx}/7 ({stage_desc})"
    elif "Rocket Hunt" in last_log or "dexscreener" in last_log:
        stage_idx, stage_desc, pct = 2, "Market Sweep & Discovery", 35
        top_line = f"Active Scan: id ({run_name}) version({version}) · Step {stage_idx}/7 ({stage_desc})"
    else:
        stage_idx, stage_desc, pct = 1, "Preflight System Checks", 15
        top_line = f"Active Scan: id ({run_name}) version({version}) · Step {stage_idx}/7 ({stage_desc})"

    stage_labels = [
        "1. Preflight System & Network Probes",
        "2. Market Sweep & Universe Discovery (9,000+ Assets)",
        "3. Post-Scan Audits (CVD, Depth, Security, Routers)",
        "4. 24h Forward & Single-TP Modeling (OHLCV Candles)",
        "5. Early Breakout & Scan-End Live Price Refresh",
        "6. Deterministic Qualification Gate (+40% single-TP)",
        "7. Final Report Finalization & Results",
    ]
    stages_data = []
    for i, lbl in enumerate(stage_labels, 1):
        if is_completed:
            status_tag = "completed"
        elif is_paused and i == stage_idx:
            status_tag = "in_progress"
        elif i < stage_idx:
            status_tag = "completed"
        elif i == stage_idx:
            status_tag = "in_progress"
        else:
            status_tag = "queued"
        stages_data.append({"name": lbl, "status": status_tag})

    tail = "\n".join(log_lines[-10:]) if log_lines else "Waiting for scanner output..."

    return {
        "top_line": top_line,
        "last_finished_banner": last_finished_banner,
        "time_started": started_ist_str,
        "elapsed": "00:00:00" if is_completed else elapsed_str,
        "active_elapsed": elapsed_str,
        "clock_ist": now_ist_str,
        "stage_idx": 0 if is_completed else stage_idx,
        "stage_desc": "Idle — Ready to scan" if is_completed else stage_desc,
        "progress_pct": 0 if is_completed else pct,
        "active_pct": pct,
        "is_completed": is_completed,
        "is_paused": is_paused,
        "ranked_count": ranked_count,
        "stages": stages_data,
        "log_tail": tail,
    }


def launch_new_scan() -> str:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outdir = RUNS_DIR / f"local-run-{ts}"
    outdir.parent.mkdir(parents=True, exist_ok=True)

    py_exe = get_python_exe()
    scan_script = SCRIPTS_DIR / "run_local_scan.py"
    monitor_script = SCRIPTS_DIR / "monitor_scan.py"

    # Detach process and give it its own process group so closing GUI window (X) does NOT kill the scan
    flags = 0
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS

    proc = subprocess.Popen(
        [py_exe, str(scan_script), "--outdir", str(outdir), "--max-runtime-minutes", "170"],
        cwd=str(ROOT),
        creationflags=flags,
    )
    try:
        (outdir / "scan-pid.txt").write_text(str(proc.pid), encoding="utf-8")
    except Exception:
        pass

    subprocess.Popen(
        [py_exe, str(monitor_script)],
        cwd=str(ROOT),
        creationflags=flags,
    )
    return f"Scan {outdir.name} successfully launched in background (PID {proc.pid})!"


def trigger_dashboard_refresh():
    def _run():
        try:
            from scripts import status_dashboard
            status_dashboard.main([])
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


class CustomRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        if self.path.startswith("/api/status"):
            payload = get_live_status_payload()
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        elif self.path.startswith("/api/journal"):
            journal = user_trade_learning.get_journal()
            data = json.dumps(journal).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        elif self.path.startswith("/api/notifications/config"):
            cfg = notification_manager.load_config()
            safe_cfg = dict(cfg)
            if safe_cfg.get("smtp_password"):
                safe_cfg["has_password"] = True
                safe_cfg["smtp_password"] = "******"
            data = json.dumps(safe_cfg).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            req_data = json.loads(body) if body.strip() else {}
        except Exception:
            req_data = {}

        if self.path.startswith("/api/notifications/config"):
            current = notification_manager.load_config()
            for k in ("enabled", "email_enabled", "recipient_email", "mailtrap_token", "sender_email", "sender_name", "smtp_server", "smtp_port", "smtp_user", "use_tls", "notify_on_status"):
                if k in req_data:
                    current[k] = req_data[k]
            if "smtp_password" in req_data and req_data["smtp_password"] != "******":
                current["smtp_password"] = req_data["smtp_password"]
            notification_manager.save_config(current)
            data = json.dumps({"status": "OK", "message": "Mailtrap notification settings saved successfully"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        elif self.path.startswith("/api/notifications/test"):
            res = notification_manager.test_notification()
            data = json.dumps(res).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        elif self.path.startswith("/api/start_scan"):
            msg = launch_new_scan()
            data = json.dumps({"status": "OK", "message": msg}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        elif self.path.startswith("/api/pause_scan"):
            res = pause_scan()
            data = json.dumps(res).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        elif self.path.startswith("/api/resume_scan"):
            res = resume_scan()
            data = json.dumps(res).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        elif self.path.startswith("/api/journal/record"):
            res = user_trade_learning.record_position(req_data)
            trigger_dashboard_refresh()
            data = json.dumps(res).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        elif self.path.startswith("/api/journal/update"):
            pos_id = req_data.get("id", "")
            res = user_trade_learning.update_position(pos_id, req_data)
            trigger_dashboard_refresh()
            data = json.dumps(res).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        elif self.path.startswith("/api/journal/delete"):
            pos_id = req_data.get("id", "")
            res = user_trade_learning.delete_position(pos_id)
            trigger_dashboard_refresh()
            data = json.dumps(res).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        elif self.path.startswith("/api/journal/reset_ai"):
            res = user_trade_learning.reset_ai_memory()
            trigger_dashboard_refresh()
            data = json.dumps(res).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        super().do_POST()

    def log_message(self, format, *args):
        # Suppress noisy HTTP logs
        pass


class DesktopAPI:
    def start_scan(self):
        return launch_new_scan()

    def pause_scan(self):
        return pause_scan()

    def resume_scan(self):
        return resume_scan()

    def get_status(self):
        return get_live_status_payload()

    def get_journal(self):
        return user_trade_learning.get_journal()

    def record_position(self, pos):
        return user_trade_learning.record_position(pos)

    def update_position(self, pos_id, updates):
        return user_trade_learning.update_position(pos_id, updates)

    def delete_position(self, pos_id):
        return user_trade_learning.delete_position(pos_id)

    def reset_ai_memory(self):
        return user_trade_learning.reset_ai_memory()


class ThreadingReuseServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_local_server() -> tuple[http.server.HTTPServer, int]:
    handler = CustomRequestHandler
    for port in (8765, 8766, 8767, 8770, 8771, 8790, 8791, 0):
        try:
            server = ThreadingReuseServer(("127.0.0.1", port), handler)
            actual_port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            return server, actual_port
        except OSError:
            continue
    raise RuntimeError("Could not bind local server to any available port.")


def main():
    import webview

    server, port = start_local_server()
    api = DesktopAPI()
    url = f"http://127.0.0.1:{port}/app_shell.html"

    # Create native standalone application window
    window = webview.create_window(
        title="Rocket Hunter — Autonomous Control Center",
        url=url,
        js_api=api,
        width=1320,
        height=880,
        min_size=(1000, 680),
        background_color="#0b1120",
    )
    webview.start()


if __name__ == "__main__":
    main()
