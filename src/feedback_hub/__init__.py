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
from feedback_hub._github import GitHubConfig, resolve_token
from feedback_hub._schema import AppSchema, FieldSchema, build_entry, load_schema
from feedback_hub._storage import list_all, save

__version__ = "1.0.0"

__all__ = [
    "AppSchema",
    "FieldSchema",
    "GitHubConfig",
    "build_entry",
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
) -> tuple:
    """Headless submission -- no UI required.

    Returns ``(issue_url, error_message)``.
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
    }

    _db = db_path or Path.home() / ".local" / "share" / "feedback-hub" / "feedback.db"
    try:
        row_id = _save(entry, Path(_db))
    except Exception:
        row_id = None

    if not token:
        return None, "GitHub token not configured"

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
