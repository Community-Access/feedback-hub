"""SQLite-backed local storage for feedback entries.

Stores every submission locally before attempting GitHub sync,
so nothing is lost if the network call fails.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT    NOT NULL,
    app                 TEXT,
    version             TEXT,
    platform            TEXT,
    category            TEXT,
    name                TEXT,
    email               TEXT,
    summary             TEXT,
    message             TEXT    NOT NULL,
    extra_fields_json   TEXT,
    metadata_json       TEXT,
    fingerprint         TEXT,
    github_issue_number INTEGER,
    github_issue_url    TEXT,
    github_sync_status  TEXT,
    github_sync_error   TEXT,
    github_synced_at    TEXT
)
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(feedback)").fetchall()}
    additions = {
        "app": "TEXT",
        "version": "TEXT",
        "platform": "TEXT",
        "summary": "TEXT",
        "extra_fields_json": "TEXT",
        "metadata_json": "TEXT",
        # Added in 1.1.0. Existing databases gain the column here rather than
        # being migrated or recreated, so an older install keeps its history.
        "fingerprint": "TEXT",
        "github_issue_number": "INTEGER",
        "github_issue_url": "TEXT",
        "github_sync_status": "TEXT",
        "github_sync_error": "TEXT",
        "github_synced_at": "TEXT",
    }
    for col, col_type in additions.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE feedback ADD COLUMN {col} {col_type}")
    conn.commit()


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn


def save(entry: dict[str, object], db_path: Path) -> int:
    """Persist entry and return its row id."""
    extra = entry.get("extra_fields")
    metadata = entry.get("metadata")
    conn = open_db(db_path)
    cur = conn.execute(
        """INSERT INTO feedback
           (timestamp, app, version, platform, category, name, email,
            summary, message, extra_fields_json, metadata_json, fingerprint)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(entry.get("timestamp", datetime.now(UTC).isoformat())),
            str(entry.get("app", "")),
            str(entry.get("version", "")),
            str(entry.get("platform", "")),
            str(entry.get("category", "feedback")),
            str(entry.get("name", "")),
            str(entry.get("email", "")),
            str(entry.get("summary", "")),
            str(entry.get("message", "")),
            json.dumps(extra, sort_keys=True) if isinstance(extra, dict) else "",
            json.dumps(metadata, sort_keys=True) if isinstance(metadata, dict) else "",
            str(entry.get("fingerprint", "")),
        ),
    )
    row_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return row_id


def update_github_sync(
    row_id: int,
    *,
    issue_number: Optional[int],
    issue_url: Optional[str],
    error: Optional[str],
    db_path: Path,
) -> None:
    status = "synced" if issue_url else "failed"
    conn = open_db(db_path)
    conn.execute(
        """UPDATE feedback
           SET github_issue_number=?, github_issue_url=?,
               github_sync_status=?, github_sync_error=?,
               github_synced_at=?
           WHERE id=?""",
        (
            issue_number,
            issue_url,
            status,
            error,
            datetime.now(UTC).isoformat(),
            row_id,
        ),
    )
    conn.commit()
    conn.close()


def list_all(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    conn = open_db(db_path)
    cur = conn.execute("SELECT * FROM feedback ORDER BY id DESC")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()
    return rows
