"""Tests for GitHub API client."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from feedback_hub._github import GitHubConfig, _build_payload, create_issue, resolve_token


def test_resolve_token_prefers_explicit():
    assert resolve_token("explicit-token", "other") == "explicit-token"


def test_resolve_token_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("FEEDBACK_HUB_GITHUB_TOKEN", "env-token")
    assert resolve_token("") == "env-token"


def test_resolve_token_returns_empty_when_none(monkeypatch):
    monkeypatch.delenv("FEEDBACK_HUB_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("SUPPORT_HUB_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("FEEDBACK_GITHUB_TOKEN", raising=False)
    assert resolve_token("") == ""


def test_create_issue_returns_error_without_token():
    cfg = GitHubConfig(token="", repo="org/repo")
    num, url, err = create_issue({"message": "test"}, cfg)
    assert num is None
    assert url is None
    assert "token" in err.lower()


def test_build_payload_title_format():
    entry = {
        "app": "ChapterForge",
        "category": "Bug Report",
        "summary": "Crash on startup",
        "message": "It crashes",
        "version": "1.0.0",
        "platform": "Windows 11",
    }
    cfg = GitHubConfig(token="t", repo="org/repo", labels=["bug"])
    payload = _build_payload(entry, cfg)
    assert payload["title"] == "[ChapterForge] bug report: Crash on startup"
    assert "It crashes" in payload["body"]
    assert "bug" in payload["labels"]


def test_build_payload_includes_extra_fields():
    entry = {
        "app": "ChapterForge",
        "category": "Bug Report",
        "summary": "Test",
        "message": "Details",
        "extra_fields": {"Screen reader": "NVDA", "Steps": "1. Open app"},
    }
    cfg = GitHubConfig(token="t", repo="org/repo")
    payload = _build_payload(entry, cfg)
    assert "NVDA" in payload["body"]
    assert "1. Open app" in payload["body"]


def test_build_payload_title_bounded_at_200_and_body_keeps_full_detail():
    # Community-Access/quill#1102: the Title is the (bounded) issue title; the
    # Description/message carries the full report in the body, untruncated.
    long_summary = "x" * 500
    long_message = "y" * 4000
    entry = {
        "app": "Quill",
        "category": "Bug Report",
        "summary": long_summary,
        "message": long_message,
    }
    cfg = GitHubConfig(token="t", repo="org/repo")
    payload = _build_payload(entry, cfg)
    assert payload["title"].endswith("x" * 200)
    assert "x" * 201 not in payload["title"]
    assert long_message in payload["body"]


def test_build_payload_includes_submitter_info():
    entry = {
        "app": "ChapterForge",
        "category": "Bug",
        "summary": "Test",
        "message": "Details",
        "name": "Shane",
        "email": "shane@example.com",
    }
    cfg = GitHubConfig(token="t", repo="org/repo")
    payload = _build_payload(entry, cfg)
    assert "Shane" in payload["body"]
    assert "shane@example.com" in payload["body"]
