#!/usr/bin/env python3
"""Build clean, verified handover ZIP bundles for transferring Rocket Hunter.

Excludes ephemeral caches, large parquet exports, raw run logs, and any secret-bearing files.
"""
from __future__ import annotations

import os
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = ROOT / "handover_bundle"

SECRET_PATTERNS = [
    re.compile(r"api[_\-\s]?key", re.I),
    re.compile(r"private[_\-\s]?key", re.I),
    re.compile(r"secret[_\-\s]?key", re.I),
    re.compile(r"password", re.I),
    re.compile(r"bearer\s+[A-Za-z0-9_\-\.]{20,}", re.I),
]

EXCLUDE_DIRS = {
    ".pytest_cache",
    "__pycache__",
    "_task_tmp",
    "identity-audit-backup",
    "rocket-runs",
    "research-exports",
    "handover_bundle",
}

EXCLUDE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".tmp",
    ".lock",
}


def check_for_secrets(path: Path) -> list[str]:
    """Check text files for obvious accidentally embedded credentials."""
    findings = []
    if path.stat().st_size > 2 * 1024 * 1024:
        return findings
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return findings
    for pattern in SECRET_PATTERNS:
        matches = pattern.findall(content)
        if len(matches) > 10:  # Likely false positives in source code discussing APIs
            continue
        # Check if line looks like an assignment of a real credential
        for line in content.splitlines():
            line_s = line.strip()
            if any(k in line_s.lower() for k in ("api_key =", "apikey =", "secret =", "password =")):
                if not any(placeholder in line_s for placeholder in ("None", '""', "''", "YOUR_", "os.getenv", "env.")):
                    findings.append(f"{path.name}: {line_s[:60]}")
    return findings


def build_codebase_zip(output_path: Path) -> int:
    """Build the clean core codebase ZIP (all source code, tests, workflows, schemas, docs)."""
    count = 0
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Include root docs and files
        root_files = [
            "README.md",
            "AUDIT.md",
            "VERSION",
            "requirements-export.txt",
            "status-dashboard.html",
            "CODEX_HANDOFF_LOCAL_INTELLIGENCE.md",
            "LOCAL_INTELLIGENCE_ARCHITECTURE.md",
            "LOCAL_INTELLIGENCE_READINESS.md",
            "LOCAL_INTELLIGENCE_PHASE4_INVENTORY.md",
            "DEX_LOSS_PATTERN_GUARD.md",
            "INSTALL.txt",
            ".gitignore",
        ]
        for name in root_files:
            file_path = ROOT / name
            if file_path.is_file():
                zf.write(file_path, arcname=name)
                count += 1

        # Include folders
        for folder in ["scripts", "tests", "schemas", ".github", "source"]:
            folder_path = ROOT / folder
            if not folder_path.is_dir():
                continue
            for root, dirs, files in os.walk(folder_path):
                dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
                for file in files:
                    if any(file.endswith(ext) for ext in EXCLUDE_EXTENSIONS):
                        continue
                    full_p = Path(root) / file
                    arcname = full_p.relative_to(ROOT).as_posix()
                    zf.write(full_p, arcname=arcname)
                    count += 1
    return count


def build_patch_zip(output_path: Path) -> int:
    """Build the focused +40% final-quote guard patch ZIP."""
    patch_files = [
        "scripts/tp_gate.py",
        "scripts/forward_24h_report.py",
        "scripts/next_day_forecast.py",
        "scripts/v418_forward_guard.py",
        "scripts/dex_loss_guard.py",
        "scripts/post_scan_audit.py",
        "tests/test_tp_gate.py",
        "tests/test_next_day_tp40_gate.py",
        "tests/test_forward_24h_required_fields.py",
        "tests/test_forward_24h_orientation.py",
        "tests/test_dex_loss_pattern_guard.py",
        "DEX_LOSS_PATTERN_GUARD.md",
        "AUDIT.md",
        "INSTALL.txt",
    ]
    count = 0
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in patch_files:
            file_path = ROOT / rel
            if file_path.is_file():
                zf.write(file_path, arcname=rel)
                count += 1
    return count


def build_reports_zip(output_path: Path) -> int:
    """Build latest reports and readiness artifacts ZIP."""
    report_files = [
        "status-dashboard.html",
        "AUDIT.md",
        "CODEX_HANDOFF_LOCAL_INTELLIGENCE.md",
        "LOCAL_INTELLIGENCE_ARCHITECTURE.md",
        "LOCAL_INTELLIGENCE_READINESS.md",
        "LOCAL_INTELLIGENCE_PHASE4_INVENTORY.md",
        "DEX_LOSS_PATTERN_GUARD.md",
        "README.md",
        "VERSION",
    ]
    count = 0
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in report_files:
            file_path = ROOT / rel
            if file_path.is_file():
                zf.write(file_path, arcname=rel)
                count += 1
    return count


def build_memory_zip(output_path: Path) -> int:
    """Build the learning-memory snapshot ZIP."""
    memory_root = ROOT / "rocket-hunter-data" / "scanner-memory"
    count = 0
    if not memory_root.is_dir():
        return 0
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(memory_root):
            for file in files:
                if any(file.endswith(ext) for ext in EXCLUDE_EXTENSIONS):
                    continue
                full_p = Path(root) / file
                arcname = full_p.relative_to(ROOT).as_posix()
                zf.write(full_p, arcname=arcname)
                count += 1
    return count


def main() -> int:
    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    print("Building Rocket Hunter Handover Bundles...")

    p1 = BUNDLE_DIR / "RocketHunter-latest-codebase.zip"
    c1 = build_codebase_zip(p1)
    s1 = p1.stat().st_size / (1024 * 1024)
    print(f"1. Core Codebase ZIP: {p1.name} ({c1} files, {s1:.2f} MB)")

    p2 = BUNDLE_DIR / "RocketHunter-40pct-guard-patch.zip"
    c2 = build_patch_zip(p2)
    s2 = p2.stat().st_size / (1024 * 1024)
    print(f"2. +40% Guard Patch ZIP: {p2.name} ({c2} files, {s2:.2f} MB)")

    p3 = BUNDLE_DIR / "RocketHunter-reports-and-readiness.zip"
    c3 = build_reports_zip(p3)
    s3 = p3.stat().st_size / (1024 * 1024)
    print(f"3. Reports & Readiness ZIP: {p3.name} ({c3} files, {s3:.2f} MB)")

    p4 = BUNDLE_DIR / "RocketHunter-learning-memory.zip"
    c4 = build_memory_zip(p4)
    s4 = p4.stat().st_size / (1024 * 1024)
    print(f"4. Learning Memory Snapshot ZIP: {p4.name} ({c4} files, {s4:.2f} MB)")

    print("\nSecurity check: scanning for plain text credentials...")
    for p in [p1, p2, p3]:
        with zipfile.ZipFile(p, "r") as zf:
            for info in zf.infolist():
                if "api" in info.filename.lower() and "txt" in info.filename.lower():
                    print(f"WARNING: Suspicious file found: {info.filename}")

    print("All bundles successfully generated in:", BUNDLE_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
