#!/usr/bin/env python3
"""Modern Desktop GUI for Rocket Hunter.

Provides an accessible, beautiful Windows desktop interface with buttons ("clickers"),
real-time status cards, live progress meters, and 1-click browser integration.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

# Resolve root directory
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parents[1]

SCRIPTS_DIR = ROOT / "scripts"
RUNS_DIR = ROOT / "rocket-runs"
LIVE_MONITOR_HTML = ROOT / "live-monitor.html"
REPORT_HTML = ROOT / "forward-24h-rocket.html"
DASHBOARD_HTML = ROOT / "status-dashboard.html"


def get_version() -> str:
    ver_file = ROOT / "VERSION"
    if ver_file.exists():
        try:
            v = ver_file.read_text(encoding="utf-8").strip()
            return f"v{v}" if not v.startswith("v") else v
        except Exception:
            pass
    return "v4.26.6"


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

    # Prioritize any actively running scan whose PID is genuinely alive
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


def format_duration(seconds: float) -> str:
    total_sec = int(max(0, seconds))
    hrs = total_sec // 3600
    mins = (total_sec % 3600) // 60
    secs = total_sec % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


def get_python_exe() -> str:
    candidates = [
        sys.executable,
        r"C:\Users\user\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe",
        "python.exe",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c).resolve())
    return "python"


class RocketHunterGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Rocket Hunter — Desktop Control Center")
        self.geometry("860x700")
        self.minsize(800, 640)
        self.configure(bg="#0b1120")

        self.version = get_version()
        self.active_run_name = "None"
        self.active_status = "IDLE"
        self.elapsed_str = "00:00:00"
        self.stage_text = "Idle — Ready to scan"
        self.stage_progress = 0

        self._setup_styles()
        self._build_ui()
        self._schedule_refresh()

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        # Configure colors
        style.configure("TProgressbar", thickness=16, troughcolor="#1e293b", background="#0ea5e9")

    def _build_ui(self):
        # Main container with padding
        container = tk.Frame(self, bg="#0b1120", padx=24, pady=20)
        container.pack(fill=tk.BOTH, expand=True)

        # ── TOP HEADER ────────────────────────────────────────────────────────
        header_frame = tk.Frame(container, bg="#0b1120")
        header_frame.pack(fill=tk.X, pady=(0, 16))

        title_label = tk.Label(
            header_frame,
            text=f"🚀 ROCKET HUNTER  {self.version}",
            font=("Segoe UI", 18, "bold"),
            fg="#38bdf8",
            bg="#0b1120",
        )
        title_label.pack(side=tk.LEFT)

        self.status_badge = tk.Label(
            header_frame,
            text="● SCAN ACTIVE",
            font=("Segoe UI", 10, "bold"),
            fg="#34d399",
            bg="#064e3b",
            padx=12,
            pady=4,
        )
        self.status_badge.pack(side=tk.RIGHT)

        # ── STATUS CARD ───────────────────────────────────────────────────────
        card = tk.Frame(container, bg="#111827", highlightbackground="#1e293b", highlightthickness=1, padx=20, pady=16)
        card.pack(fill=tk.X, pady=(0, 18))

        # Top line in card as requested by user
        self.top_line_label = tk.Label(
            card,
            text=f"Scan: id (Searching...) version({self.version}) · 0 Ranked Candidates counted",
            font=("Segoe UI", 11, "bold"),
            fg="#38bdf8",
            bg="#0f172a",
            padx=12,
            pady=6,
            anchor="w",
        )
        self.top_line_label.pack(fill=tk.X, pady=(0, 12))

        # Grid stats row
        stats_frame = tk.Frame(card, bg="#111827")
        stats_frame.pack(fill=tk.X, pady=(0, 12))

        # Stat 1: Elapsed
        self.elapsed_box = self._create_stat_box(stats_frame, "⏱️ ELAPSED TIME", "00:00:00", "#38bdf8")
        self.elapsed_box.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        # Stat 2: Started IST
        self.started_box = self._create_stat_box(stats_frame, "CLOCK (ISRAEL TIME)", "--:--:--", "#f8fafc")
        self.started_box.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        # Stat 3: Pipeline Stage
        self.stage_box = self._create_stat_box(stats_frame, "PIPELINE STAGE", "Stage 4/7", "#10b981")
        self.stage_box.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Progress bar
        pbar_frame = tk.Frame(card, bg="#111827")
        pbar_frame.pack(fill=tk.X, pady=(6, 4))

        self.pbar_label = tk.Label(pbar_frame, text="Active Progress: 70%", font=("Segoe UI", 9, "bold"), fg="#94a3b8", bg="#111827")
        self.pbar_label.pack(anchor="w", pady=(0, 4))

        self.progressbar = ttk.Progressbar(pbar_frame, style="TProgressbar", mode="determinate")
        self.progressbar.pack(fill=tk.X)
        self.progressbar["value"] = 70

        # Stage detail
        self.stage_detail = tk.Label(card, text="Step 4/7 — 24h Forward & Single-TP Modeling", font=("Segoe UI", 9), fg="#94a3b8", bg="#111827")
        self.stage_detail.pack(anchor="w", pady=(6, 0))

        # ── ACTION BUTTONS ("CLICKERS") ───────────────────────────────────────
        btn_container = tk.Frame(container, bg="#0b1120")
        btn_container.pack(fill=tk.X, pady=(0, 16))

        # Primary Scan Button
        self.btn_scan = tk.Button(
            btn_container,
            text="🚀  Start New Production Scan",
            font=("Segoe UI", 12, "bold"),
            bg="#0284c7",
            fg="#ffffff",
            activebackground="#0369a1",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            padx=16,
            pady=10,
            cursor="hand2",
            command=self.on_start_scan,
        )
        self.btn_scan.pack(fill=tk.X, pady=(0, 10))

        # 3 Secondary Action Buttons Row
        sec_btn_frame = tk.Frame(btn_container, bg="#0b1120")
        sec_btn_frame.pack(fill=tk.X)

        self.btn_monitor = self._create_styled_button(
            sec_btn_frame, "⏱️ Open Live Web Monitor", "#0f172a", "#38bdf8", self.on_open_monitor
        )
        self.btn_monitor.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        self.btn_results = self._create_styled_button(
            sec_btn_frame, "📈 View Verified Results", "#0f172a", "#10b981", self.on_open_results
        )
        self.btn_results.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        self.btn_folder = self._create_styled_button(
            sec_btn_frame, "📁 Open Runs Folder", "#0f172a", "#e2e8f0", self.on_open_folder
        )
        self.btn_folder.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ── LIVE LOG CONSOLE BOX ──────────────────────────────────────────────
        log_frame = tk.Frame(container, bg="#111827", highlightbackground="#1e293b", highlightthickness=1, padx=14, pady=12)
        log_frame.pack(fill=tk.BOTH, expand=True)

        log_hdr = tk.Label(log_frame, text="LIVE SCAN LOG STREAM", font=("Segoe UI", 9, "bold"), fg="#64748b", bg="#111827")
        log_hdr.pack(anchor="w", pady=(0, 6))

        self.log_text = tk.Text(
            log_frame,
            bg="#050811",
            fg="#a5f3fc",
            font=("Consolas", 9),
            relief=tk.FLAT,
            height=10,
            wrap=tk.WORD,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.insert(tk.END, "Initializing live scanner monitor...\n")
        self.log_text.configure(state=tk.DISABLED)

    def _create_stat_box(self, parent, label: str, val: str, val_color: str):
        frame = tk.Frame(parent, bg="#0f172a", highlightbackground="#1e293b", highlightthickness=1, padx=12, pady=10)
        lbl = tk.Label(frame, text=label, font=("Segoe UI", 8, "bold"), fg="#94a3b8", bg="#0f172a")
        lbl.pack(anchor="w")
        v = tk.Label(frame, text=val, font=("Segoe UI", 14, "bold"), fg=val_color, bg="#0f172a")
        v.pack(anchor="w", pady=(3, 0))
        frame.val_label = v
        return frame

    def _create_styled_button(self, parent, text: str, bg: str, fg: str, command):
        return tk.Button(
            parent,
            text=text,
            font=("Segoe UI", 10, "bold"),
            bg=bg,
            fg=fg,
            activebackground="#1e293b",
            activeforeground=fg,
            relief=tk.FLAT,
            highlightbackground="#1e293b",
            highlightthickness=1,
            padx=12,
            pady=8,
            cursor="hand2",
            command=command,
        )

    def _schedule_refresh(self):
        self.update_live_data()
        self.after(1000, self._schedule_refresh)

    def update_live_data(self):
        run_dir = get_latest_run_dir()
        if not run_dir:
            self.top_line_label.config(text=f"Scan: id (No scans found) version({self.version}) · 0 Ranked Candidates counted")
            self.status_badge.config(text="○ IDLE", bg="#1e293b", fg="#94a3b8")
            return

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

        # Elapsed time
        started_str = status_data.get("scan_started_utc")
        now_utc = dt.datetime.now(dt.timezone.utc)
        israel_tz = dt.timezone(dt.timedelta(hours=2))
        now_ist_str = now_utc.astimezone(israel_tz).strftime("%H:%M:%S IST")

        elapsed_sec = 0.0
        if started_str:
            try:
                started_dt = dt.datetime.fromisoformat(started_str.replace("Z", "+00:00"))
                elapsed_sec = (now_utc - started_dt).total_seconds()
            except Exception:
                pass

        elapsed_str = format_duration(elapsed_sec)

        # Count ranked candidates
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

        # Update Top line banner
        run_name = run_dir.name
        self.top_line_label.config(
            text=f"Scan: id ({run_name}) version({self.version}) · {ranked_count} Ranked Candidates counted"
        )

        # Status badge
        if is_completed:
            self.status_badge.config(text="✔ COMPLETED", bg="#064e3b", fg="#34d399")
        else:
            self.status_badge.config(text="● SCAN ACTIVE", bg="#064e3b", fg="#34d399")

        # Stat boxes
        self.elapsed_box.val_label.config(text=elapsed_str)
        self.started_box.val_label.config(text=now_ist_str)

        # Determine stage and progress
        log_lines = []
        if log_file.exists():
            try:
                text = log_file.read_text(encoding="utf-8", errors="replace")
                log_lines = [l.strip() for l in text.splitlines() if l.strip()]
            except Exception:
                pass

        last_log = "\n".join(log_lines[-40:]) if log_lines else ""
        if is_completed:
            stage_idx, stage_desc, pct = 7, "Scan Completed Successfully", 100
        elif "Wrote finalized forward watchlist" in last_log or "Updated root forward HTML" in last_log:
            stage_idx, stage_desc, pct = 7, "Writing Final Reports", 98
        elif "Early-breakout" in last_log or "classify_final" in last_log:
            stage_idx, stage_desc, pct = 5, "Live Price Refresh & Qualification", 85
        elif "forward-24h" in last_log or "Building scan-start-anchored" in last_log:
            stage_idx, stage_desc, pct = 4, "24h Forward & Single-TP Modeling", 70
        elif "Post-scan audit" in last_log or "Auditing top candidates" in last_log:
            stage_idx, stage_desc, pct = 3, "Post-Scan Audits (CVD, Depth, Security)", 50
        elif "Rocket Hunt" in last_log or "dexscreener" in last_log:
            stage_idx, stage_desc, pct = 2, "Market Sweep & Discovery", 35
        else:
            stage_idx, stage_desc, pct = 1, "Preflight System Checks", 15

        self.stage_box.val_label.config(text=f"Step {stage_idx}/7")
        self.stage_detail.config(text=f"Step {stage_idx}/7 — {stage_desc}")
        self.pbar_label.config(text=f"Active Progress: {pct}%")
        self.progressbar["value"] = pct

        # Update log stream text
        if log_lines:
            tail = "\n".join(log_lines[-8:]) + "\n"
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.delete("1.0", tk.END)
            self.log_text.insert(tk.END, tail)
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)

    def on_start_scan(self):
        confirm = messagebox.askyesno(
            "Start Production Scan",
            "Are you ready to launch a new multi-chain Rocket Hunter production scan?\n\n"
            "This will sweep 9,000+ pools across Solana, BSC, Base, and Ethereum, "
            "enforce the strict +40% Take-Profit threshold, and open the live web monitor.",
            parent=self,
        )
        if not confirm:
            return

        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        outdir = RUNS_DIR / f"local-run-{ts}"
        outdir.parent.mkdir(parents=True, exist_ok=True)

        py_exe = get_python_exe()
        scan_script = SCRIPTS_DIR / "run_local_scan.py"
        monitor_script = SCRIPTS_DIR / "monitor_scan.py"

        # Launch scanner in separate process
        subprocess.Popen(
            [py_exe, str(scan_script), "--outdir", str(outdir), "--max-runtime-minutes", "170"],
            cwd=str(ROOT),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        # Launch monitor updater
        subprocess.Popen(
            [py_exe, str(monitor_script)],
            cwd=str(ROOT),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        time.sleep(1)
        if LIVE_MONITOR_HTML.exists():
            webbrowser.open(str(LIVE_MONITOR_HTML.resolve().as_uri()))

        messagebox.showinfo(
            "Scan Launched 🚀",
            f"Production scan successfully started!\n\n"
            f"Folder: {outdir.name}\n"
            f"The live monitor has been opened in your browser.",
            parent=self,
        )

    def on_open_monitor(self):
        py_exe = get_python_exe()
        monitor_script = SCRIPTS_DIR / "monitor_scan.py"
        subprocess.Popen(
            [py_exe, str(monitor_script)],
            cwd=str(ROOT),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        time.sleep(1)
        if LIVE_MONITOR_HTML.exists():
            webbrowser.open(str(LIVE_MONITOR_HTML.resolve().as_uri()))
        else:
            messagebox.showinfo("Live Monitor", "live-monitor.html is initializing. Please try again in a few seconds.", parent=self)

    def on_open_results(self):
        if REPORT_HTML.exists():
            webbrowser.open(str(REPORT_HTML.resolve().as_uri()))
        else:
            messagebox.showinfo("Results", "forward-24h-rocket.html not found yet. Please complete a scan first.", parent=self)

    def on_open_folder(self):
        if RUNS_DIR.exists():
            if os.name == "nt":
                os.startfile(str(RUNS_DIR.resolve()))
            else:
                subprocess.Popen(["xdg-open", str(RUNS_DIR)])
        else:
            messagebox.showinfo("Runs Folder", "No rocket-runs folder found yet.", parent=self)


def main():
    app = RocketHunterGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
