import sqlite3
from datetime import datetime, timezone


def log_quality(conn: sqlite3.Connection, check_name: str, status: str, message: str) -> None:
    """Insert one data-quality check result. status: 'ok' | 'warning' | 'error'."""
    conn.execute(
        "INSERT INTO data_quality_checks (checked_at, check_name, status, message)"
        " VALUES (?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), check_name, status, message),
    )
    conn.commit()


def log_run(conn: sqlite3.Connection, step: str, status: str, message: str) -> None:
    """Insert one pipeline-step log entry. status: 'ok' | 'warning' | 'error'."""
    conn.execute(
        "INSERT INTO run_log (run_at, step, status, message) VALUES (?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), step, status, message),
    )
    conn.commit()
