"""GitHub Issues API client.

Framework-agnostic. Accepts a config and a prepared entry dict,
posts it as a GitHub issue, returns (issue_number, issue_url, error).

**Deduplication.** When an entry carries a ``fingerprint`` (see
:mod:`feedback_hub._fingerprint`), this client first looks for an open issue
already bearing that fingerprint. If it finds one it adds a comment there and
returns that issue, so the second person to hit a crash lands on the first
person's report instead of opening a new one.

Two decisions worth knowing about:

*Listing, not searching.* The obvious implementation is the search API, but
GitHub's search index lags issue creation by up to a minute -- and the
duplicates that actually hurt arrive **seconds** apart (two reports of the same
crash, 26 seconds, in the triage that motivated this). Listing open issues is
live, so it catches exactly the case search would miss.

*Dedup can never lose a report.* Every failure in the lookup -- a network
blip, a permissions problem, an unexpected response -- falls through to
creating a new issue. A duplicate issue is a minor annoyance; a crash report
that vanished because the deduplicator broke is a bug nobody will ever hear
about.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from feedback_hub._fingerprint import extract_marker, label_for, marker

#: How many open issues to scan for a matching fingerprint, newest first.
#: Two pages of 100. A duplicate crash matches a *recent* issue in practice,
#: and bounding this keeps a busy repository from turning one crash report
#: into a dozen API calls.
_DEDUP_SCAN_PAGES = 2
_DEDUP_PAGE_SIZE = 100
_TIMEOUT_SECONDS = 20


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


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "feedback-hub/1.1",
    }


def _api(
    url: str,
    config: GitHubConfig,
    *,
    payload: dict[str, object] | None = None,
    method: str = "GET",
) -> object:
    """One GitHub API call, returning the decoded JSON. Raises on failure."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urlrequest.Request(url, data=data, method=method, headers=_headers(config.token))
    with urlrequest.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_open_issue_by_fingerprint(
    fingerprint: str, config: GitHubConfig
) -> tuple[Optional[int], Optional[str]]:
    """The open issue already carrying *fingerprint*, or ``(None, None)``.

    Scans open issues newest-first for the hidden body marker. Pull requests
    are skipped -- the issues endpoint returns them too, and a PR that quoted a
    crash report would otherwise swallow every future report of that crash.

    Never raises: any failure means "no match", which sends the caller down
    the ordinary create-an-issue path.
    """
    if not fingerprint or not config.token:
        return None, None
    for page in range(1, _DEDUP_SCAN_PAGES + 1):
        query = urlparse.urlencode({
            "state": "open",
            "per_page": _DEDUP_PAGE_SIZE,
            "page": page,
            "sort": "created",
            "direction": "desc",
        })
        try:
            issues = _api(
                f"https://api.github.com/repos/{config.repo}/issues?{query}", config
            )
        except Exception:  # noqa: BLE001 - a failed lookup is "no match", never an error
            return None, None
        if not isinstance(issues, list) or not issues:
            return None, None
        for issue in issues:
            if not isinstance(issue, dict) or issue.get("pull_request"):
                continue
            if extract_marker(str(issue.get("body") or "")) == fingerprint:
                return issue.get("number"), issue.get("html_url")
        if len(issues) < _DEDUP_PAGE_SIZE:
            break
    return None, None


def add_comment(
    issue_number: int, body: str, config: GitHubConfig
) -> tuple[Optional[str], Optional[str]]:
    """Comment on an existing issue. Returns ``(comment_url, error)``."""
    try:
        data = _api(
            f"https://api.github.com/repos/{config.repo}/issues/{issue_number}/comments",
            config,
            payload={"body": body},
            method="POST",
        )
    except urlerror.HTTPError as exc:
        return None, f"GitHub API error {exc.code}: {_read_error(exc)}"
    except Exception as exc:  # noqa: BLE001
        return None, f"GitHub request failed: {exc}"
    url = data.get("html_url") if isinstance(data, dict) else None
    return url, None


def create_issue(
    entry: dict[str, object],
    config: GitHubConfig,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Post entry as a GitHub issue, or comment on the one already open for it.

    Returns (issue_number, issue_url, error_message).
    On success error_message is None; on failure number and url are None.

    When ``entry["fingerprint"]`` is set and an open issue already carries it,
    this posts a comment there instead and returns that issue -- so the caller
    still gets a real issue URL to show the reporter, and the reporter lands on
    the conversation about their crash rather than starting a new one.
    """
    if not config.token:
        return None, None, "GitHub token not configured"

    fingerprint = str(entry.get("fingerprint") or "").strip()
    if fingerprint:
        existing_number, existing_url = find_open_issue_by_fingerprint(fingerprint, config)
        if existing_number is not None:
            _comment_url, comment_error = add_comment(
                int(existing_number), _build_duplicate_comment(entry), config
            )
            if comment_error is None:
                return existing_number, existing_url, None
            # The issue exists but the comment failed. Fall through and file a
            # new issue: a duplicate is recoverable, a lost report is not.

    payload = _build_payload(entry, config)
    try:
        data = _api(
            f"https://api.github.com/repos/{config.repo}/issues",
            config,
            payload=payload,
            method="POST",
        )
    except urlerror.HTTPError as exc:
        return None, None, f"GitHub API error {exc.code}: {_read_error(exc)}"
    except Exception as exc:  # noqa: BLE001
        return None, None, f"GitHub request failed: {exc}"
    if not isinstance(data, dict):
        return None, None, "GitHub returned an unexpected response"
    return data.get("number"), data.get("html_url"), None


def _read_error(exc: urlerror.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8")
    except Exception:  # noqa: BLE001
        return str(exc)


def _build_duplicate_comment(entry: dict[str, object]) -> str:
    """The comment posted when a report matches an already-open issue.

    Deliberately a full report rather than "+1": the second occurrence often
    carries the detail the first one lacked -- a different version, a different
    screen reader, a description of what the user was doing. Losing that to
    save a few lines would defeat the point of collecting it.

    Shares its section builders with :func:`_build_payload` so the two renderings
    of the same entry cannot drift: a field added to one appears in both.
    """
    lines = [
        "## Another report of this crash",
        "",
        f"- **App**: `{str(entry.get('app', 'Unknown')).strip()}`",
        f"- **Version**: `{str(entry.get('version') or 'unknown').strip()}`",
        f"- **Platform**: `{entry.get('platform', 'unknown')}`",
        f"- **Submitted**: `{entry.get('timestamp', 'unknown')}`",
    ]
    lines += _submitter_lines(entry)
    lines += _details_lines(entry)
    lines += _environment_lines(entry)
    lines += ["", "---", "_Matched to this issue by feedback-hub crash fingerprint._"]
    return "\n".join(lines)


def _submitter_lines(entry: dict[str, object]) -> list[str]:
    """The optional "who reported it" block, or nothing."""
    name = str(entry.get("name", "")).strip()
    email = str(entry.get("email", "")).strip()
    if not name and not email:
        return []
    lines = ["", "### Submitter"]
    if name:
        lines.append(f"- **Name**: {name}")
    if email:
        lines.append(f"- **Email**: {email}")
    return lines


def _details_lines(entry: dict[str, object]) -> list[str]:
    """The report body the user wrote, or nothing when it is empty."""
    message = str(entry.get("message", "")).strip()
    if not message:
        return []
    return ["", "### Details", "", message]


def _environment_lines(entry: dict[str, object]) -> list[str]:
    """The metadata block, rendered as sorted JSON, or nothing."""
    metadata = entry.get("metadata")
    if not isinstance(metadata, dict) or not metadata:
        return []
    return [
        "",
        "### Environment",
        "```",
        json.dumps(metadata, indent=2, sort_keys=True),
        "```",
    ]


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

    lines += _submitter_lines(entry)
    lines += _details_lines(entry)

    extra_fields = entry.get("extra_fields")
    if isinstance(extra_fields, dict):
        rendered = []
        for label, value in extra_fields.items():
            if value and str(value).strip():
                rendered.append(f"**{label}**: {value}")
        if rendered:
            lines += ["", "### Additional Information", ""]
            lines += rendered

    lines += _environment_lines(entry)

    lines += [
        "",
        "---",
        "_Submitted via feedback-hub_",
    ]

    labels = list(cfg.labels)

    # The fingerprint marker is what makes the *next* report of this crash
    # findable. It is an HTML comment, so it is invisible in the rendered
    # issue; the matching label is the visible half, so a maintainer can
    # filter by it in the GitHub UI without knowing the marker exists.
    fingerprint = str(entry.get("fingerprint") or "").strip()
    if fingerprint:
        lines += ["", marker(fingerprint)]
        labels.append(label_for(fingerprint))

    # "Is this already fixed?" is the first question asked of every incoming
    # report, and answering it currently means reading the body. A label makes
    # it answerable from the issue list.
    version = str(entry.get("version") or "").strip()
    if version and entry.get("version_label"):
        labels.append(f"reported-version: {version}")

    payload: dict[str, object] = {
        "title": title,
        "body": "\n".join(lines),
        "labels": labels,
    }
    if cfg.assignee:
        payload["assignees"] = [cfg.assignee]
    return payload


def create_raw_issue(
    *,
    title: str,
    body: str,
    labels: list[str],
    config: GitHubConfig,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """File an issue with the title and body **exactly as given**.

    :func:`create_issue` renders a feedback entry into feedback-hub's own
    report layout. Some callers already know the shape the issue has to have --
    :mod:`feedback_hub.server` relays a Community Picks suggestion whose body
    carries a machine-readable block that a downstream workflow parses, and
    reformatting it would break that workflow.

    Kept here rather than in the caller so every GitHub request in this package
    goes out through one place: one set of headers, one timeout, one error
    shape. Returns ``(issue_number, issue_url, error_message)``.
    """
    if not config.token:
        return None, None, "GitHub token not configured"
    payload: dict[str, object] = {"title": title, "body": body, "labels": list(labels)}
    if config.assignee:
        payload["assignees"] = [config.assignee]
    try:
        data = _api(
            f"https://api.github.com/repos/{config.repo}/issues",
            config,
            payload=payload,
            method="POST",
        )
    except urlerror.HTTPError as exc:
        return None, None, f"GitHub API error {exc.code}: {_read_error(exc)}"
    except Exception as exc:  # noqa: BLE001
        return None, None, f"GitHub request failed: {exc}"
    if not isinstance(data, dict):
        return None, None, "GitHub returned an unexpected response"
    return data.get("number"), data.get("html_url"), None
