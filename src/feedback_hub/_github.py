"""GitHub Issues API client.

Framework-agnostic. Accepts a config and a prepared entry dict,
posts it as a GitHub issue, returns (issue_number, issue_url, error).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    token: str
    repo: str
    assignee: str = ""
    labels: list[str] = field(default_factory=lambda: ["needs-triage"])


def resolve_token(*candidates: str) -> str:
    """Resolve GitHub token from candidates in priority order.

    Candidates are tried first. Then standard env vars are checked.
    This lets each app pass its own bundled fine-grained PAT as the
    last candidate while still allowing env var overrides.

    Safe to bundle issues-only fine-grained PATs in desktop apps:
    worst case misuse is filing extra issues, not code/repo access.
    """
    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip()
    for env_var in (
        "FEEDBACK_HUB_GITHUB_TOKEN",
        "SUPPORT_HUB_GITHUB_TOKEN",
        "FEEDBACK_GITHUB_TOKEN",
    ):
        value = os.environ.get(env_var, "").strip()
        if value:
            return value
    return ""


def create_issue(
    entry: dict[str, object],
    config: GitHubConfig,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Post entry as a GitHub issue.

    Returns (issue_number, issue_url, error_message).
    On success error_message is None; on failure number and url are None.
    """
    if not config.token:
        return None, None, "GitHub token not configured"

    payload = _build_payload(entry, config)
    req = urlrequest.Request(
        f"https://api.github.com/repos/{config.repo}/issues",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {config.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "feedback-hub/1.0",
        },
    )
    try:
        with urlrequest.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("number"), data.get("html_url"), None
    except urlerror.HTTPError as exc:
        try:
            details = exc.read().decode("utf-8")
        except Exception:
            details = str(exc)
        return None, None, f"GitHub API error {exc.code}: {details}"
    except Exception as exc:  # noqa: BLE001
        return None, None, f"GitHub request failed: {exc}"


def _build_payload(entry: dict[str, object], cfg: GitHubConfig) -> dict[str, object]:
    app = str(entry.get("app", "Unknown")).strip()
    category = str(entry.get("category", "feedback")).strip().lower()
    # The "summary" field is the issue title; the "message"/Description field
    # carries the full detail in the body (below). Titles are bounded so a
    # pasted paragraph can't become a runaway GitHub title (#1102).
    summary = str(entry.get("summary") or entry.get("message", ""))[:200].strip()
    title = f"[{app}] {category}: {summary}"

    lines = [
        "## Feedback Report",
        "",
        f"- **App**: `{app}`",
        f"- **Version**: `{entry.get('version', 'unknown')}`",
        f"- **Platform**: `{entry.get('platform', 'unknown')}`",
        f"- **Category**: `{category}`",
        f"- **Submitted**: `{entry.get('timestamp', 'unknown')}`",
    ]

    submitter_name = str(entry.get("name", "")).strip()
    submitter_email = str(entry.get("email", "")).strip()
    if submitter_name or submitter_email:
        lines.append("")
        lines.append("### Submitter")
        if submitter_name:
            lines.append(f"- **Name**: {submitter_name}")
        if submitter_email:
            lines.append(f"- **Email**: {submitter_email}")

    lines += ["", "### Details", "", str(entry.get("message", "")).strip()]

    extra_fields = entry.get("extra_fields")
    if isinstance(extra_fields, dict):
        rendered = []
        for label, value in extra_fields.items():
            if value and str(value).strip():
                rendered.append(f"**{label}**: {value}")
        if rendered:
            lines += ["", "### Additional Information", ""]
            lines += rendered

    metadata = entry.get("metadata")
    if isinstance(metadata, dict) and metadata:
        lines += [
            "",
            "### Environment",
            "```",
            json.dumps(metadata, indent=2, sort_keys=True),
            "```",
        ]

    lines += [
        "",
        "---",
        "_Submitted via feedback-hub_",
    ]

    payload: dict[str, object] = {
        "title": title,
        "body": "\n".join(lines),
        "labels": cfg.labels,
    }
    if cfg.assignee:
        payload["assignees"] = [cfg.assignee]
    return payload
