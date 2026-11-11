#!/usr/bin/env python3
"""Show read-only local machine and storage diagnostics without exposing secrets."""
from __future__ import annotations

import csv
import ctypes
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "rocket-hunter-data"
_WINDOWS_DRIVE_TYPES = {
    0: "Unknown",
    1: "No root directory",
    2: "Removable",
    3: "Fixed",
    4: "Network",
    5: "Optical",
    6: "RAM disk",
}


def _available_ram_bytes() -> int | None:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullAvailPhys)
        return None

    try:
        return int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _volume_type(path: Path) -> str:
    if os.name != "nt":
        return "Unavailable (Windows drive type lookup is OS-specific)"
    root = path.resolve().anchor
    if not root:
        return "Unavailable (drive root could not be determined)"
    try:
        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(root))
    except (AttributeError, OSError, TypeError, ValueError):
        return "Unavailable (Windows drive type query failed)"
    return _WINDOWS_DRIVE_TYPES.get(drive_type, "Unknown")


def _drive_info(path: Path) -> dict[str, int | str]:
    try:
        free_bytes = int(shutil.disk_usage(path).free)
    except OSError:
        free_bytes = "Unavailable (disk space query failed)"
    return {"volume_type": _volume_type(path), "free_bytes": free_bytes}


def _nvidia_gpus() -> list[dict[str, str]] | None:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    command = [
        executable,
        "--query-gpu=name,memory.total,memory.free,driver_version",
        "--format=csv,noheader",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None

    gpus = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) != 4:
            continue
        name, total_memory, free_memory, driver_version = (value.strip() for value in row)
        if name:
            gpus.append({
                "name": name,
                "memory_total": total_memory,
                "memory_free": free_memory,
                "driver_version": driver_version,
            })
    return gpus or None


def collect_diagnostics(
    project_root: Path = ROOT,
    data_dir: Path = DATA_DIR,
) -> dict:
    processor = platform.processor().strip() or "Unavailable"
    cpu_count = os.cpu_count()
    available_ram = _available_ram_bytes()
    return {
        "cpu_model": processor,
        "logical_processors": cpu_count,
        "available_ram_bytes": available_ram,
        "python_version": platform.python_version(),
        "project_drive": _drive_info(Path(project_root)),
        "data_drive": _drive_info(Path(data_dir)),
        "nvidia_gpus": _nvidia_gpus(),
    }


def _format_bytes(value: int | str | None) -> str:
    if value is None:
        return "Unavailable"
    if not isinstance(value, int):
        return value
    return f"{value / (1024 ** 3):.2f} GiB"


def format_diagnostics(info: dict) -> str:
    cpu_count = info["logical_processors"]
    cpu_count_text = str(cpu_count) if cpu_count is not None else "Unavailable"
    lines = [
        f"CPU: {info['cpu_model']}; logical processors: {cpu_count_text}",
        f"Available RAM: {_format_bytes(info['available_ram_bytes'])}",
        f"Python: {info['python_version']}",
    ]
    for label in ("project_drive", "data_drive"):
        drive = info[label]
        display_name = "Project drive" if label == "project_drive" else "Data drive"
        lines.append(
            f"{display_name}: volume type={drive['volume_type']}; "
            f"free space={_format_bytes(drive['free_bytes'])}"
        )

    gpus = info["nvidia_gpus"]
    if not gpus:
        lines.append("NVIDIA GPU: Unavailable (not detected or nvidia-smi could not query it)")
    else:
        for index, gpu in enumerate(gpus, start=1):
            lines.append(
                f"NVIDIA GPU {index}: {gpu['name']}; "
                f"memory={gpu['memory_free']} free / {gpu['memory_total']} total; "
                f"driver={gpu['driver_version']}"
            )
    return "\n".join(lines)


def main() -> int:
    print(format_diagnostics(collect_diagnostics()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
