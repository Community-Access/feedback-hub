"""Flask Blueprint adapter.

Drop-in replacement for GLOW's feedback.py routes.  Registers a blueprint
that serves:
  GET  /          -- feedback form (HTML)
  POST /          -- form submission
  POST /api       -- JSON submission (Bearer token auth)
  GET  /review    -- protected review dashboard

GLOW migration (non-breaking)::

    # web/src/acb_large_print_web/routes/feedback.py
    # Replace the entire file with:
    from feedback_hub.flask_blueprint import make_blueprint
    feedback_bp = make_blueprint(
        app_name="GLOW",
        github_repo="Community-Access/support",
        app_version_fn=lambda: get_version(),
    )

All existing route names (feedback.feedback_form, feedback.feedback_submit,
feedback.feedback_submit_api, feedback.feedback_review) are preserved.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)


def make_blueprint(
    *,
    app_name: str,
    github_repo: str,
    github_labels: Optional[list[str]] = None,
    github_assignee: str = "",
    github_token: str = "",
    app_version_fn: Optional[Callable[[], str]] = None,
    db_path: Optional[Path] = None,
    url_prefix: str = "/feedback",
    blueprint_name: str = "feedback",
    categories: Optional[list[str]] = None,
    extra_fields: Optional[list[dict]] = None,
):
    """Create and return a configured Flask Blueprint.

    Parameters
    ----------
    app_name:
        Human name shown on the form and in GitHub issue titles.
    github_repo:
        ``owner/repo`` slug (e.g. ``BITS-ACB/chapterforge``).
    github_token:
        Optional token override. Falls back to env vars automatically.
    app_version_fn:
        Callable returning the current app version string.
    db_path:
        SQLite database path. Defaults to Flask's ``instance/feedback.db``.
    categories:
        Issue category choices shown in the form drop-down.
    extra_fields:
        List of ``{"name": ..., "label": ..., "type": ..., "required": ...}``
        dicts for app-specific fields beyond the defaults.
    """
    from flask import (
        Blueprint,
        abort,
        current_app,
        jsonify,
        render_template,
        request,
    )

    from feedback_hub._github import GitHubConfig, create_issue, resolve_token
    from feedback_hub._schema import AppSchema, FieldSchema, build_entry, load_schema
    from feedback_hub._storage import (
        save as _save,
        update_github_sync as _update_sync,
    )

    bp = Blueprint(blueprint_name, __name__, template_folder="templates")

    _categories = categories or [
        "Bug Report",
        "Feature Request",
        "Accessibility Issue",
        "Documentation",
        "Other",
    ]
    _schema = AppSchema(
        app=app_name,
        github_repo=github_repo,
        github_labels=github_labels or ["needs-triage"],
        github_assignee=github_assignee,
        categories=_categories,
        fields=[
            FieldSchema("summary", "Summary", "text", required=True, max_length=120),
            FieldSchema("message", "Details", "textarea", required=True, max_length=5000),
            *(
                FieldSchema(
                    f["name"],
                    f.get("label", f["name"].title()),
                    f.get("type", "text"),
                    required=f.get("required", False),
                    max_length=f.get("max_length", 0),
                    options=f.get("options", []),
                    default=f.get("default", ""),
                )
                for f in (extra_fields or [])
            ),
        ],
    )

    def _resolved_token() -> str:
        return resolve_token(
            github_token,
            os.environ.get("SUPPORT_HUB_GITHUB_TOKEN", ""),
            os.environ.get("FEEDBACK_GITHUB_TOKEN", ""),
        )

    def _get_db() -> Path:
        if db_path:
            return db_path
        try:
            return Path(current_app.instance_path) / "feedback.db"
        except RuntimeError:
            return Path("instance") / "feedback.db"

    def _get_version() -> str:
        if app_version_fn:
            try:
                return app_version_fn()
            except Exception:
                pass
        return ""

    def _submit(entry: dict) -> tuple[Optional[str], Optional[str]]:
        """Store locally then sync to GitHub. Returns (issue_url, error)."""
        try:
            row_id = _save(entry, _get_db())
        except Exception as exc:
            log.exception("Failed to save feedback to SQLite")
            return None, str(exc)

        token = _resolved_token()
        if not token:
            return None, "GitHub token not configured"

        cfg = GitHubConfig(
            token=token,
            repo=_schema.github_repo,
            assignee=_schema.github_assignee,
            labels=_schema.github_labels,
        )
        number, url, error = create_issue(entry, cfg)
        try:
            _update_sync(row_id, issue_number=number, issue_url=url, error=error, db_path=_get_db())
        except Exception:
            pass
        return url, error

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @bp.route("/", methods=["GET"])
    def feedback_form():
        return render_template(
            "feedback_hub/form.html",
            schema=_schema,
            error="",
            values={},
        )

    @bp.route("/", methods=["POST"])
    def feedback_submit():
        values = {
            f.name: request.form.get(f.name, "").strip()
            for f in _schema.fields
        }
        errors = _schema.validate(values)
        if errors:
            return render_template(
                "feedback_hub/form.html",
                schema=_schema,
                error=" ".join(errors),
                values=values,
            ), 400

        entry = build_entry(
            _schema,
            values,
            name=request.form.get("name", "").strip(),
            email=request.form.get("email", "").strip(),
            category=request.form.get("category", _categories[0]),
            app_version=_get_version(),
            metadata={
                "source_channel": "web",
                "user_agent": request.headers.get("User-Agent", ""),
            },
        )
        issue_url, _error = _submit(entry)
        return render_template(
            "feedback_hub/thanks.html",
            schema=_schema,
            issue_url=issue_url,
        )

    @bp.route("/api", methods=["POST"])
    def feedback_submit_api():
        api_token = os.environ.get("FEEDBACK_HUB_API_TOKEN", "").strip() or \
                    os.environ.get("FEEDBACK_API_TOKEN", "").strip()
        if not api_token:
            abort(404)
        auth = request.headers.get("Authorization", "")
        if not auth or not hmac.compare_digest(auth, f"Bearer {api_token}"):
            return jsonify({"error": "Unauthorized"}), 403

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "JSON object required"}), 400

        values = {f.name: str(payload.get(f.name, "")).strip() for f in _schema.fields}
        errors = _schema.validate(values)
        if errors:
            return jsonify({"error": " ".join(errors)}), 400

        entry = build_entry(
            _schema,
            values,
            name=str(payload.get("name", "")),
            email=str(payload.get("email", "")),
            category=str(payload.get("category", _categories[0])),
            app_version=str(payload.get("version", _get_version())),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        )
        issue_url, sync_error = _submit(entry)
        return jsonify({
            "status": "accepted",
            "issue_url": issue_url,
            "github_sync_status": "synced" if issue_url else "failed",
            "github_sync_error": sync_error,
        }), 202

    @bp.route("/review")
    def feedback_review():
        from feedback_hub._storage import list_all

        password = os.environ.get("FEEDBACK_PASSWORD", "").strip()
        if not password:
            abort(404)
        provided = request.args.get("key", "")
        if not provided or not hmac.compare_digest(provided, password):
            abort(403)

        rows = list_all(_get_db())
        return render_template("feedback_hub/review.html", schema=_schema, rows=rows)

    return bp
