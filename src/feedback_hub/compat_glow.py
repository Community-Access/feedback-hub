"""GLOW backward-compatibility shim.

Replace the entire content of
``web/src/acb_large_print_web/routes/feedback.py`` with::

    from feedback_hub.compat_glow import feedback_bp

That is the only change needed. All route names are preserved:
    feedback.feedback_form
    feedback.feedback_submit
    feedback.feedback_submit_api
    feedback.feedback_review

The GLOW app also imports support_hub.create_support_issue and
support_hub.load_support_hub_config in a few places. Those still work via::

    from feedback_hub.compat_glow import create_support_issue, load_support_hub_config
"""
from __future__ import annotations

import os

from feedback_hub._github import GitHubConfig, create_issue, resolve_token
from feedback_hub.flask_blueprint import make_blueprint


def _glow_version() -> str:
    try:
        from acb_large_print_web.version import get_version
        return get_version()
    except Exception:
        return ""


feedback_bp = make_blueprint(
    app_name="GLOW",
    github_repo=os.environ.get("FEEDBACK_GITHUB_REPO", "Community-Access/support"),
    github_labels=[
        l.strip()
        for l in os.environ.get("FEEDBACK_GITHUB_LABELS", "needs-triage").split(",")
        if l.strip()
    ],
    github_assignee=os.environ.get("FEEDBACK_GITHUB_ASSIGNEE", ""),
    app_version_fn=_glow_version,
    blueprint_name="feedback",
    extra_fields=[
        {"name": "task", "label": "What were you trying to do", "type": "text", "max_length": 240},
    ],
)


# Legacy API preserved for any GLOW code that imports from support_hub directly
def load_support_hub_config():
    """Backward-compatible stub returning a simple namespace."""
    class _Cfg:
        token = resolve_token()
        repo = os.environ.get("FEEDBACK_GITHUB_REPO", "Community-Access/support")
        assignee = os.environ.get("FEEDBACK_GITHUB_ASSIGNEE", "")
        labels = [
            l.strip()
            for l in os.environ.get("FEEDBACK_GITHUB_LABELS", "needs-triage").split(",")
            if l.strip()
        ]
        api_token = os.environ.get("FEEDBACK_API_TOKEN", "").strip()
    return _Cfg()


def create_support_issue(entry: dict) -> tuple:
    """Backward-compatible wrapper over the new create_issue."""
    cfg_raw = load_support_hub_config()
    cfg = GitHubConfig(
        token=cfg_raw.token,
        repo=cfg_raw.repo,
        assignee=cfg_raw.assignee,
        labels=cfg_raw.labels,
    )
    # Map old GLOW entry keys to new format
    mapped = {
        "app": entry.get("source_app", "GLOW"),
        "version": entry.get("source_version", ""),
        "platform": entry.get("platform", ""),
        "category": entry.get("category", "feedback"),
        "name": entry.get("name", ""),
        "email": entry.get("email", ""),
        "summary": entry.get("summary", ""),
        "message": entry.get("message", ""),
        "timestamp": entry.get("timestamp", ""),
        "extra_fields": {"Task": entry.get("task", "")} if entry.get("task") else None,
    }
    number, url, error = create_issue(mapped, cfg)
    return number, url, error
