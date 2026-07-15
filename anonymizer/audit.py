"""Append-only audit log for sensitive operations (re-identification, mapping
reset/rotate/erase/export). Records WHAT happened and WHEN -- never the original
values themselves, so the log is safe to keep. Lives next to the mapping DB."""

from __future__ import annotations

from datetime import datetime, timezone

from .config import app_data_dir


def audit_log_path():
    return app_data_dir() / "audit.log"


def log_event(event: str, detail: str = "") -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\t{event}\t{detail}\n"
    try:
        with open(audit_log_path(), "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def read_recent(limit: int = 50) -> list[str]:
    path = audit_log_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[-limit:][::-1]
