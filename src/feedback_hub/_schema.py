"""Dynamic form schema system.

Each app defines a schema (dict or JSON file) describing which fields
to show and how to validate them.  The schema drives:
  - wxPython dialog field generation
  - Flask form rendering
  - Validation rules
  - GitHub issue formatting

Minimal schema example::

    {
        "app": "ChapterForge",
        "version": "1.0.0",
        "github_repo": "BITS-ACB/chapterforge",
        "github_labels": ["bug", "needs-triage"],
        "categories": ["Bug Report", "Feature Request", "Accessibility Issue", "Other"],
        "fields": [
            {"name": "summary",  "label": "Summary",          "type": "text",     "required": true,  "max_length": 120},
            {"name": "message",  "label": "What happened",    "type": "textarea", "required": true,  "max_length": 5000},
            {"name": "expected", "label": "What you expected","type": "textarea", "required": false},
            {"name": "steps",    "label": "Steps to reproduce","type": "textarea","required": false},
            {"name": "version",  "label": "App version",      "type": "text",     "required": false, "default": "__app_version__"},
            {"name": "platform", "label": "Operating system", "type": "text",     "required": false, "default": "__platform__"}
        ]
    }
"""
from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class FieldSchema:
    name: str
    label: str
    type: str = "text"          # text | textarea | select | checkbox
    required: bool = False
    max_length: int = 0         # 0 = no limit
    options: list[str] = field(default_factory=list)  # for select
    default: str = ""
    placeholder: str = ""


@dataclass
class AppSchema:
    app: str
    github_repo: str
    github_labels: list[str] = field(default_factory=lambda: ["needs-triage"])
    github_assignee: str = ""
    version: str = ""
    categories: list[str] = field(default_factory=lambda: [
        "Bug Report", "Feature Request", "Accessibility Issue", "Other"
    ])
    fields: list[FieldSchema] = field(default_factory=list)

    def resolve_defaults(self, app_version: str = "") -> dict[str, str]:
        """Return a dict of field name -> resolved default value."""
        os_str = f"{platform.system()} {platform.release()}"
        py_str = sys.version.splitlines()[0]
        magic = {
            "__app_version__": app_version or self.version,
            "__platform__": os_str,
            "__python__": py_str,
        }
        return {
            f.name: magic.get(f.default, f.default)
            for f in self.fields
        }

    def validate(self, values: dict[str, str]) -> list[str]:
        errors = []
        for f in self.fields:
            val = values.get(f.name, "").strip()
            if f.required and not val:
                errors.append(f"{f.label} is required.")
            if f.max_length and len(val) > f.max_length:
                errors.append(f"{f.label} must be {f.max_length} characters or fewer.")
            if f.type == "select" and f.options and val and val not in f.options:
                errors.append(f"{f.label} has an invalid value.")
        return errors


def load_schema(source: str | dict | Path) -> AppSchema:
    """Load a schema from a dict, JSON string, or Path to a JSON file."""
    if isinstance(source, Path):
        raw = json.loads(source.read_text(encoding="utf-8"))
    elif isinstance(source, str):
        raw = json.loads(source)
    else:
        raw = source

    fields = [
        FieldSchema(
            name=f["name"],
            label=f.get("label", f["name"].title()),
            type=f.get("type", "text"),
            required=f.get("required", False),
            max_length=f.get("max_length", 0),
            options=f.get("options", []),
            default=f.get("default", ""),
            placeholder=f.get("placeholder", ""),
        )
        for f in raw.get("fields", [])
    ]

    return AppSchema(
        app=raw["app"],
        github_repo=raw["github_repo"],
        github_labels=raw.get("github_labels", ["needs-triage"]),
        github_assignee=raw.get("github_assignee", ""),
        version=raw.get("version", ""),
        categories=raw.get("categories", [
            "Bug Report", "Feature Request", "Accessibility Issue", "Other"
        ]),
        fields=fields,
    )


def build_entry(
    schema: AppSchema,
    values: dict[str, str],
    *,
    name: str = "",
    email: str = "",
    category: str = "",
    app_version: str = "",
    metadata: Optional[dict[str, Any]] = None,
    timestamp: str = "",
) -> dict[str, object]:
    """Build a normalized entry dict from submitted form values."""
    from datetime import UTC, datetime

    # The fields listed in the schema go into extra_fields so the
    # GitHub formatter can render them with their human labels.
    extra_fields = {
        schema_field.label: values.get(schema_field.name, "")
        for schema_field in schema.fields
        if schema_field.name not in ("summary", "message", "version", "platform")
        and values.get(schema_field.name, "").strip()
    }

    return {
        "app": schema.app,
        "version": values.get("version", app_version or schema.version),
        "platform": values.get("platform", ""),
        "category": category or "feedback",
        "name": name,
        "email": email,
        "summary": values.get("summary", ""),
        "message": values.get("message", ""),
        "extra_fields": extra_fields or None,
        "metadata": metadata,
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
    }
