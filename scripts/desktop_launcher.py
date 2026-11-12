#!/usr/bin/env python3
"""Desktop launcher for Rocket Hunter.

Compiles into RocketHunter.exe for 1-click execution without any coding.
"""
from __future__ import annotations

import json
import os
import sys
import time
import subprocess
import webbrowser
import datetime as dt
from pathlib import Path

# Find real root directory (where RocketHunter resides)
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parents[1]

SCRIPTS_DIR = ROOT / "scripts"
RUNS_DIR = ROOT / "rocket-runs"
LIVE_MONITOR_HTML = ROOT / "live-monitor.html"
REPORT_HTML = ROOT / "forward-24h-rocket.html"
DASHBOARD_HTML = ROOT / "status-dashboard.html"


def load_env() -> None:
    env_file = ROOT / ".env"
    if env_file.is_file():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                if k and k not in os.environ:
                    os.environ[k] = v
        except Exception:
            pass


def get_python_exe() -> str:
    # Look for python in system or path
    candidates = [
        sys.executable,
        r"C:\Users\user\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe",
        "python.exe",
        "py.exe",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c).resolve())
    return "python"


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def banner() -> None:
    ver_file = ROOT / "VERSION"
    ver = ver_file.read_text(encoding="utf-8").strip() if ver_file.exists() else "v4.26.7"
    print("═" * 72)
    print(f"       🚀 ROCKET HUNTER {ver} — DESKTOP SCANNER LAUNCHER")
    print("           Algorithmic DeFi & Microcap Opportunity Discovery")
    print("═" * 72)
    print("  • Strict Single Take-Profit (≥ +40.00% Potential)")
    print("  • Hard $15,000 Liquidity Floor + RugCheck & GoPlus Defense")
    print("  • Scan-End Live DEX Price Refresh (Israel Clock Timestamps)")
    print("═" * 72)


def start_scan() -> None:
    clear_screen()
    banner()
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outdir = RUNS_DIR / f"local-run-{ts}"
    outdir.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n[+] Launching new production scan in background...")
    print(f"    Output folder: {outdir}")

    py_exe = get_python_exe()
    scan_script = SCRIPTS_DIR / "run_local_scan.py"
    monitor_script = SCRIPTS_DIR / "monitor_scan.py"

    # Start scan process
    scan_cmd = [py_exe, str(scan_script), "--outdir", str(outdir), "--max-runtime-minutes", "170"]
    subprocess.Popen(scan_cmd, cwd=str(ROOT), creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0)

    # Give it 2 seconds to initialize status
    time.sleep(2)

    # Start monitor updater in background
    subprocess.Popen([py_exe, str(monitor_script)], cwd=str(ROOT))

    # Open live monitor in browser
    time.sleep(1)
    if LIVE_MONITOR_HTML.exists():
        print(f"[+] Opening Live Monitor in your default browser...")
        webbrowser.open(str(LIVE_MONITOR_HTML.resolve().as_uri()))

    print("\n" + "─" * 72)
    print("  ✔ Scan is actively running!")
    print(f"  ✔ Live Monitor page opened in your browser: {LIVE_MONITOR_HTML.name}")
    print("  ✔ When complete, forward-24h-rocket.html will automatically finalize.")
    print("─" * 72)
    input("\nPress Enter to return to main menu or close this window...")


def open_live_monitor() -> None:
    py_exe = get_python_exe()
    monitor_script = SCRIPTS_DIR / "monitor_scan.py"
    subprocess.Popen([py_exe, str(monitor_script)], cwd=str(ROOT))
    time.sleep(1)
    if LIVE_MONITOR_HTML.exists():
        print(f"\n[+] Opening {LIVE_MONITOR_HTML} in your browser...")
        webbrowser.open(str(LIVE_MONITOR_HTML.resolve().as_uri()))
    else:
        print("\n[!] live-monitor.html not generated yet. Start a scan first.")
        time.sleep(2)


def find_latest_completed_report() -> Path | None:
    """Finds the latest verified scan report from completed runs."""
    if RUNS_DIR.exists():
        run_dirs = sorted([d for d in RUNS_DIR.iterdir() if d.is_dir()], reverse=True)
        for rd in run_dirs:
            report_file = rd / "forward-24h-rocket.html"
            if not report_file.exists():
                continue
            manifest_file = rd / "manifest.json"
            if manifest_file.exists():
                try:
                    mdata = json.loads(manifest_file.read_text(encoding="utf-8"))
                    if mdata.get("status") == "COMPLETED" or mdata.get("scan_status") == "COMPLETED":
                        return report_file
                except Exception:
                    pass
            if report_file.stat().st_size > 1000:
                return report_file

    if REPORT_HTML.exists() and REPORT_HTML.stat().st_size > 1000:
        return REPORT_HTML
    return None


def open_results() -> None:
    target = find_latest_completed_report()
    if target and target.exists():
        print(f"\n[+] Opening verified report {target.name} ({target.parent.name}) in your browser...")
        webbrowser.open(str(target.resolve().as_uri()))
    else:
        print("\n[!] No completed scan report found yet. Run [1] to generate the latest verified scan report.")
        time.sleep(3)


def open_dashboard() -> None:
    if DASHBOARD_HTML.exists():
        print(f"\n[+] Opening {DASHBOARD_HTML} in your browser...")
        webbrowser.open(str(DASHBOARD_HTML.resolve().as_uri()))
    else:
        py_exe = get_python_exe()
        subprocess.run([py_exe, str(SCRIPTS_DIR / "status_dashboard.py")], cwd=str(ROOT))
        if DASHBOARD_HTML.exists():
            webbrowser.open(str(DASHBOARD_HTML.resolve().as_uri()))


def main() -> None:
    load_env()
    while True:
        clear_screen()
        banner()
        print("\n  PLEASE SELECT AN OPTION:\n")
        print("    [1] 🚀 START NEW PRODUCTION SCAN (1-Click Run)")
        print("        • Launches multi-chain sweep across 9,000+ pools")
        print("        • Opens live ticking stopwatch and progress monitor in browser")
        print("        • Produces verified +40% actionable opportunities\n")
        print("    [2] ⏱️  OPEN LIVE SCAN MONITOR")
        print("        • Opens real-time live-monitor.html in your browser\n")
        print("    [3] 📈 OPEN LATEST VERIFIED RESULTS")
        print("        • Opens forward-24h-rocket.html report\n")
        print("    [4] 🛠️  VIEW SYSTEM STATUS DASHBOARD")
        print("        • Opens status-dashboard.html\n")
        print("    [0] ❌ EXIT\n")
        print("═" * 72)
        try:
            choice = input("  Enter option [1-4, or 0 to exit] (Default: 1): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

        if choice in ("", "1"):
            start_scan()
        elif choice == "2":
            open_live_monitor()
        elif choice == "3":
            open_results()
        elif choice == "4":
            open_dashboard()
        elif choice == "0":
            print("\nGoodbye!")
            break
        else:
            print("\nInvalid selection. Please try again.")
            time.sleep(1)


if __name__ == "__main__":
    main()
