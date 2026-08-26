"""feedback-hub - Multi-framework GitHub issue submission library.

Each app uses its native UI; all submit directly to GitHub Issues.

Quick start (wxPython)::

    from feedback_hub import load_schema
    from feedback_hub.wx_dialog import FeedbackDialog

    schema = load_schema({"app": "MyApp", "github_repo": "org/repo", "fields": [...]})
    dlg = FeedbackDialog(parent, schema=schema, github_token=TOKEN)
    dlg.ShowModal()
    dlg.Destroy()

Quick start (Flask)::

    from feedback_hub.flask_blueprint import make_blueprint

    feedback_bp = make_blueprint(app_name="MyApp", github_repo="org/repo")
    app.register_blueprint(feedback_bp, url_prefix="/feedback")

Quick start (headless / CLI)::

    from feedback_hub import submit

    issue_url, error = submit(
        app="MyApp",
        github_repo="org/repo",
        github_token=TOKEN,
        summary="Something is broken",
        message="Details here",
        category="Bug Report",
    )
"""
from feedback_hub._fingerprint import (
    compute_fingerprint,
    fingerprint_from_traceback,
)
from feedback_hub._github import GitHubConfig, resolve_token
from feedback_hub._schema import AppSchema, FieldSchema, build_entry, load_schema
from feedback_hub._storage import list_all, save

__version__ = "1.1.0"

__all__ = [
    "AppSchema",
    "FieldSchema",
    "GitHubConfig",
    "build_entry",
    "compute_fingerprint",
    "fingerprint_from_traceback",
    "list_all",
    "load_schema",
    "resolve_token",
    "save",
    "submit",
]


def submit(
    *,
    app: str,
    github_repo: str,
    github_token: str = "",
    summary: str = "",
    message: str,
    category: str = "feedback",
    name: str = "",
    email: str = "",
    app_version: str = "",
    github_labels: list[str] | None = None,
    github_assignee: str = "",
    metadata: dict | None = None,
    db_path=None,
    fingerprint: str = "",
    version_label: bool = False,
    server_url: str = "",
) -> tuple:
    """Headless submission -- no UI required.

    Returns ``(issue_url, error_message)``.

    ``fingerprint`` deduplicates: when an open issue already carries it, this
    comments there and returns that issue's URL instead of filing a new one.
    Compute one with :func:`feedback_hub.compute_fingerprint` (from a live
    exception) or :func:`feedback_hub.fingerprint_from_traceback` (from saved
    traceback text). An empty fingerprint means "do not deduplicate" -- never
    pass a placeholder, or unrelated reports collapse onto one issue.

    ``version_label`` adds a ``reported-version: X`` label, so "is this
    already fixed?" is answerable from the issue list rather than by reading
    each body.

    ``server_url`` submits through a feedback-hub server instead of calling
    GitHub, and is the preferred transport: the caller then needs **no token at
    all**. A desktop app that carries one is carrying it inside its installer,
    where anybody who unzips it can read it. Set this and leave
    ``github_token`` empty.

    It also decides where reports go *later*: once submission is a POST to a
    URL, moving from GitHub issues to a help desk is a change on the server
    rather than a release to every installed copy.
    """
    from datetime import UTC, datetime
    from pathlib import Path

    from feedback_hub._github import create_issue
    from feedback_hub._storage import save as _save, update_github_sync

    token = resolve_token(github_token)
    entry = {
        "app": app,
        "version": app_version,
        "platform": "",
        "category": category,
        "name": name,
        "email": email,
        "summary": summary,
        "message": message,
        "metadata": metadata,
        "timestamp": datetime.now(UTC).isoformat(),
        "fingerprint": fingerprint,
        "version_label": version_label,
    }

    _db = db_path or Path.home() / ".local" / "share" / "feedback-hub" / "feedback.db"
    try:
        row_id = _save(entry, Path(_db))
    except Exception:
        row_id = None

    if server_url:
        # The server holds the credential, so this path needs none. Checked
        # first: when both are configured the server wins, because the token is
        # then only a fallback somebody forgot to remove.
        from feedback_hub._relay import relay_entry

        number, url, error = relay_entry(server_url, entry)
    elif not token:
        return None, "GitHub token not configured"
    else:
        cfg = GitHubConfig(
            token=token,
            repo=github_repo,
            assignee=github_assignee,
            labels=github_labels or ["needs-triage"],
        )
        number, url, error = create_issue(entry, cfg)

    if row_id is not None:
        try:
            update_github_sync(row_id, issue_number=number, issue_url=url, error=error, db_path=Path(_db))
        except Exception:
            pass

    return url, error
