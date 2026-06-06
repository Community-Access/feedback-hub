"""Tests for SQLite storage."""
from __future__ import annotations

from feedback_hub._storage import list_all, open_db, save, update_github_sync


def test_save_and_retrieve(tmp_path):
    db = tmp_path / "feedback.db"
    entry = {
        "app": "ChapterForge",
        "version": "1.0.0",
        "platform": "Windows 11",
        "category": "Bug Report",
        "name": "Shane",
        "email": "shane@example.com",
        "summary": "Crash on startup",
        "message": "It crashes every time",
        "timestamp": "2026-06-05T12:00:00+00:00",
    }
    row_id = save(entry, db)
    assert row_id == 1

    rows = list_all(db)
    assert len(rows) == 1
    assert rows[0]["app"] == "ChapterForge"
    assert rows[0]["message"] == "It crashes every time"


def test_update_github_sync(tmp_path):
    db = tmp_path / "feedback.db"
    entry = {"app": "ChapterForge", "message": "test", "timestamp": "2026-06-05T00:00:00+00:00"}
    row_id = save(entry, db)

    update_github_sync(row_id, issue_number=42, issue_url="https://github.com/issues/42", error=None, db_path=db)

    rows = list_all(db)
    assert rows[0]["github_issue_number"] == 42
    assert rows[0]["github_issue_url"] == "https://github.com/issues/42"
    assert rows[0]["github_sync_status"] == "synced"


def test_update_github_sync_failed(tmp_path):
    db = tmp_path / "feedback.db"
    entry = {"app": "ChapterForge", "message": "test", "timestamp": "2026-06-05T00:00:00+00:00"}
    row_id = save(entry, db)

    update_github_sync(row_id, issue_number=None, issue_url=None, error="timeout", db_path=db)

    rows = list_all(db)
    assert rows[0]["github_sync_status"] == "failed"
    assert rows[0]["github_sync_error"] == "timeout"


def test_list_all_empty(tmp_path):
    db = tmp_path / "missing.db"
    assert list_all(db) == []
