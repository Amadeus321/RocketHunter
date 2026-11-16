"""Shared bounded run leases and collection-manifest evidence contract."""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ACTIVITY_DB = (
    ROOT / "rocket-hunter-data" / "research-data"
    / "continuous-monitoring" / "monitor-state.sqlite3"
)
MAX_RUN_SECONDS = 180 * 60
DEFAULT_HEARTBEAT_SECONDS = 10.0
DEFAULT_STALE_AFTER_SECONDS = 60.0
DB_TIMEOUT_SECONDS = 3.0

FINAL_COLLECTION_STATUSES = frozenset({
    "COMPLETED",
    "DURATION_ELAPSED",
    "FAILED",
    "NO_REQUESTS_AVAILABLE",
    "ONE_CYCLE_COMPLETED",
    "PROVIDER_ACCESS_DENIED",
    "PROVIDER_COOLDOWN_ACTIVE",
    "PROVIDER_RATE_LIMITED",
    "REQUEST_LIMIT_REACHED",
})
FAILED_COLLECTION_STATUSES = frozenset({
    "FAILED",
    "PROVIDER_ACCESS_DENIED",
    "PROVIDER_COOLDOWN_ACTIVE",
    "PROVIDER_RATE_LIMITED",
})


class ResearchRunError(RuntimeError):
    """Raised when the shared run lease or evidence contract is unsafe."""


class DuplicateResearchRun(ResearchRunError):
    """Raised when another live scanner or collector owns the shared lease."""


class CollectionManifestError(ResearchRunError):
    """Raised when a finalized collection manifest is malformed."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_iso(value: dt.datetime | None = None) -> str:
    stamp = value or utc_now()
    return stamp.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(dt.timezone.utc)


def open_activity_db(path: Path) -> sqlite3.Connection:
    path = Path(path)
    if path.is_symlink():
        raise ResearchRunError(f"shared research state database must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=DB_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS research_run_history (
            run_id TEXT PRIMARY KEY,
            run_type TEXT NOT NULL,
            status TEXT NOT NULL,
            started_utc TEXT NOT NULL,
            heartbeat_utc TEXT NOT NULL,
            finished_utc TEXT,
            process_id INTEGER NOT NULL,
            host_name TEXT NOT NULL,
            max_runtime_seconds INTEGER NOT NULL,
            recovery_count INTEGER NOT NULL DEFAULT 0,
            detail TEXT
        );
        CREATE TABLE IF NOT EXISTS research_run_lease (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            run_id TEXT NOT NULL UNIQUE REFERENCES research_run_history(run_id),
            run_type TEXT NOT NULL,
            process_id INTEGER NOT NULL,
            host_name TEXT NOT NULL,
            started_utc TEXT NOT NULL,
            heartbeat_utc TEXT NOT NULL,
            deadline_utc TEXT NOT NULL
        );
    """)
    connection.commit()
    return connection


def ensure_activity_db_disjoint(path: Path, protected_paths: list[Path]) -> None:
    activity_path = Path(path).resolve()
    for protected_path in protected_paths:
        protected = Path(protected_path).resolve()
        if (
            activity_path == protected
            or activity_path in protected.parents
            or protected in activity_path.parents
        ):
            raise ResearchRunError(
                f"shared research state database overlaps protected data: {activity_path}"
            )


def process_is_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if os.name == "nt":
        return _windows_process_is_alive(process_id)
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _windows_process_is_alive(process_id: int, kernel32=None, last_error=None) -> bool:
    import ctypes
    from ctypes import wintypes

    still_active = 259
    process_query_limited_information = 0x1000
    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.SetLastError.argtypes = (wintypes.DWORD,)
    kernel32.SetLastError.restype = None
    kernel32.SetLastError(0)
    handle = kernel32.OpenProcess(
        process_query_limited_information, False, process_id
    )
    if not handle:
        error = last_error() if last_error is not None else ctypes.get_last_error()
        return error != 87
    try:
        exit_code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == still_active
        error = last_error() if last_error is not None else ctypes.get_last_error()
        return error != 87
    finally:
        kernel32.CloseHandle(handle)


class ResearchRunLease:
    """One cross-process lease; stale recovery never resumes or edits run files."""

    def __init__(
        self,
        path: Path,
        run_id: str,
        run_type: str,
        max_runtime_seconds: int,
        *,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        clock=utc_now,
        pid: int | None = None,
        host_name: str | None = None,
        is_process_alive=process_is_alive,
    ):
        if not run_id or not run_type:
            raise ResearchRunError("run ID and run type are required")
        if isinstance(max_runtime_seconds, bool) or not 1 <= max_runtime_seconds <= MAX_RUN_SECONDS:
            raise ResearchRunError(f"run deadline must be between 1 and {MAX_RUN_SECONDS} seconds")
        if heartbeat_seconds <= 0 or stale_after_seconds <= heartbeat_seconds:
            raise ResearchRunError("heartbeat and stale thresholds are invalid")
        self.path = Path(path)
        self.run_id = run_id
        self.run_type = run_type
        self.max_runtime_seconds = int(max_runtime_seconds)
        self.heartbeat_seconds = heartbeat_seconds
        self.stale_after_seconds = stale_after_seconds
        self.clock = clock
        self.pid = os.getpid() if pid is None else pid
        self.host_name = host_name or socket.gethostname()
        self.is_process_alive = is_process_alive
        self._stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_error: BaseException | None = None
        self._acquired = False

    def acquire(self) -> "ResearchRunLease":
        started = self.clock()
        connection = open_activity_db(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT * FROM research_run_lease WHERE slot=1"
            ).fetchone()
            if active is not None:
                heartbeat = parse_utc(active["heartbeat_utc"])
                owner_host = active["host_name"]
                owner_pid = active["process_id"]
                if heartbeat is None:
                    raise ResearchRunError("active research lease has an invalid heartbeat")
                stale = (started - heartbeat).total_seconds() > self.stale_after_seconds
                owner_live = (
                    owner_host == self.host_name and self.is_process_alive(owner_pid)
                )
                if not stale or owner_live:
                    raise DuplicateResearchRun(
                        f"{active['run_type']} run {active['run_id']} owns the shared research lease"
                    )
                connection.execute("""
                    UPDATE research_run_history
                    SET status='STALE', finished_utc=?,
                        recovery_count=recovery_count+1,
                        detail='stale heartbeat; not resumed'
                    WHERE run_id=? AND status='RUNNING'
                """, (utc_iso(started), active["run_id"]))
                connection.execute(
                    "DELETE FROM research_run_lease WHERE slot=1 AND run_id=?",
                    (active["run_id"],),
                )
            duplicate = connection.execute(
                "SELECT 1 FROM research_run_history WHERE run_id=?", (self.run_id,)
            ).fetchone()
            if duplicate:
                raise DuplicateResearchRun(f"run ID {self.run_id} has already been used")
            started_iso = utc_iso(started)
            deadline_iso = utc_iso(started + dt.timedelta(seconds=self.max_runtime_seconds))
            connection.execute("""
                INSERT INTO research_run_history (
                    run_id, run_type, status, started_utc, heartbeat_utc, process_id,
                    host_name, max_runtime_seconds
                ) VALUES (?, ?, 'RUNNING', ?, ?, ?, ?, ?)
            """, (
                self.run_id, self.run_type, started_iso, started_iso, self.pid,
                self.host_name, self.max_runtime_seconds,
            ))
            connection.execute("""
                INSERT INTO research_run_lease (
                    slot, run_id, run_type, process_id, host_name,
                    started_utc, heartbeat_utc, deadline_utc
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self.run_id, self.run_type, self.pid, self.host_name,
                started_iso, started_iso, deadline_iso,
            ))
            connection.commit()
            self._acquired = True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"research-heartbeat-{self.run_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()
        return self

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                self.heartbeat()
            except BaseException as exc:
                self._heartbeat_error = exc
                self._stop.set()
                return

    def heartbeat(self, now: dt.datetime | None = None) -> None:
        if not self._acquired:
            raise ResearchRunError("cannot heartbeat an unowned research lease")
        stamp = now or self.clock()
        connection = open_activity_db(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("""
                UPDATE research_run_lease SET heartbeat_utc=?
                WHERE slot=1 AND run_id=? AND process_id=? AND host_name=?
            """, (utc_iso(stamp), self.run_id, self.pid, self.host_name))
            if cursor.rowcount != 1:
                raise ResearchRunError(f"research lease for run {self.run_id} was lost")
            connection.execute(
                "UPDATE research_run_history SET heartbeat_utc=? WHERE run_id=?",
                (utc_iso(stamp), self.run_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def check_health(self) -> None:
        if self._heartbeat_error is not None:
            raise ResearchRunError(
                f"research lease heartbeat failed: {self._heartbeat_error}"
            ) from self._heartbeat_error
        if not self._acquired:
            raise ResearchRunError("research run no longer owns its shared lease")
        if self.path.is_symlink() or not self.path.is_file():
            raise ResearchRunError("research lease database is unavailable")
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT heartbeat_utc, deadline_utc FROM research_run_lease
                WHERE slot=1 AND run_id=? AND process_id=? AND host_name=?
                """,
                (self.run_id, self.pid, self.host_name),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ResearchRunError("unable to verify shared research lease") from exc
        finally:
            connection.close()
        heartbeat = parse_utc(row["heartbeat_utc"]) if row else None
        if heartbeat is None:
            raise ResearchRunError(f"research lease for run {self.run_id} was lost")
        if (self.clock() - heartbeat).total_seconds() > self.stale_after_seconds:
            raise ResearchRunError(f"research lease heartbeat for run {self.run_id} expired")
        deadline = parse_utc(row["deadline_utc"])
        if deadline is None or self.clock() > deadline:
            raise ResearchRunError("research run exceeded its shared lease deadline")

    def finish(self, status: str, detail: str | None = None) -> None:
        if not self._acquired:
            return
        self._stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=max(1.0, self.heartbeat_seconds * 2))
        finished = utc_iso(self.clock())
        connection = open_activity_db(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                UPDATE research_run_history SET status=?, heartbeat_utc=?, finished_utc=?, detail=?
                WHERE run_id=? AND status='RUNNING'
            """, (status, finished, finished, detail, self.run_id))
            connection.execute(
                "DELETE FROM research_run_lease WHERE slot=1 AND run_id=?", (self.run_id,)
            )
            connection.commit()
            self._acquired = False
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def read_collection_manifest(path: Path) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise CollectionManifestError(f"collection manifest must be a regular file: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectionManifestError(f"cannot read collection manifest: {path}") from exc
    if not isinstance(manifest, dict):
        raise CollectionManifestError(f"collection manifest must contain an object: {path}")
    status = manifest.get("status")
    if not isinstance(status, str) or status not in FINAL_COLLECTION_STATUSES:
        return {
            "manifest": manifest,
            "status": status if isinstance(status, str) else "UNKNOWN",
            "finalized": False,
            "run_id": manifest.get("collection_run_id"),
            "record_count": manifest.get("record_count")
            if isinstance(manifest.get("record_count"), int)
            and not isinstance(manifest.get("record_count"), bool)
            and manifest.get("record_count") >= 0 else None,
            "evidence_status": "PENDING",
        }
    run_id = manifest.get("collection_run_id")
    record_count = manifest.get("record_count")
    if not isinstance(run_id, str) or (not run_id and record_count != 0):
        raise CollectionManifestError(f"collection manifest has no collection_run_id: {path}")
    if isinstance(record_count, bool) or not isinstance(record_count, int) or record_count < 0:
        raise CollectionManifestError(f"collection manifest has invalid record_count: {path}")
    return {
        "manifest": manifest,
        "status": status,
        "finalized": True,
        "run_id": run_id,
        "record_count": record_count,
        "evidence_status": (
            "FAILED" if status in FAILED_COLLECTION_STATUSES else "COMPLETE"
        ),
    }


def collection_evidence_status(
    status: str,
    declared_count: int | None,
    actual_count: int | None,
    *,
    finalized: bool,
    file_present: bool,
    conflicting_records: int = 0,
    stale_records: int = 0,
    failed_records: int = 0,
) -> str:
    if not finalized:
        return "PENDING"
    if status in FAILED_COLLECTION_STATUSES:
        return "FAILED"
    if failed_records:
        return "FAILED"
    if not file_present and declared_count:
        return "MISSING"
    if conflicting_records > 0 or (
        actual_count is not None and declared_count is not None
        and actual_count > declared_count
    ):
        return "CONFLICTING"
    if (
        declared_count is not None and actual_count is not None
        and actual_count < declared_count
    ):
        return "MISSING"
    if stale_records:
        return "STALE"
    return "COMPLETE"


def count_jsonl_records(path: Path, run_id: str) -> tuple[int, int]:
    count = conflicts = 0
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                count += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ResearchRunError(
                        f"invalid JSONL record at {path}:{line_number}"
                    ) from exc
                if not isinstance(record, dict):
                    raise ResearchRunError(f"JSONL record must be an object at {path}:{line_number}")
                if record.get("collection_run_id") != run_id:
                    conflicts += 1
    except (OSError, UnicodeDecodeError) as exc:
        raise ResearchRunError(f"cannot verify collection records: {path}") from exc
    return count, conflicts
