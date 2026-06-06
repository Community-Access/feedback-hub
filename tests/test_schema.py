"""Tests for form schema system."""
from __future__ import annotations

import json

from feedback_hub._schema import build_entry, load_schema


MINIMAL_SCHEMA = {
    "app": "TestApp",
    "github_repo": "org/testrepo",
    "fields": [
        {"name": "summary", "label": "Summary", "type": "text", "required": True, "max_length": 120},
        {"name": "message", "label": "Details", "type": "textarea", "required": True, "max_length": 5000},
    ],
}


def test_load_schema_from_dict():
    schema = load_schema(MINIMAL_SCHEMA)
    assert schema.app == "TestApp"
    assert schema.github_repo == "org/testrepo"
    assert len(schema.fields) == 2


def test_load_schema_from_json_string():
    schema = load_schema(json.dumps(MINIMAL_SCHEMA))
    assert schema.app == "TestApp"


def test_validate_passes_with_required_fields():
    schema = load_schema(MINIMAL_SCHEMA)
    errors = schema.validate({"summary": "A title", "message": "Some details"})
    assert errors == []


def test_validate_fails_when_required_missing():
    schema = load_schema(MINIMAL_SCHEMA)
    errors = schema.validate({"summary": "", "message": ""})
    assert any("Summary" in e for e in errors)
    assert any("Details" in e for e in errors)


def test_validate_fails_on_max_length():
    schema = load_schema(MINIMAL_SCHEMA)
    errors = schema.validate({"summary": "x" * 200, "message": "ok"})
    assert any("120" in e for e in errors)


def test_build_entry_maps_values():
    schema = load_schema(MINIMAL_SCHEMA)
    entry = build_entry(
        schema,
        {"summary": "Crash", "message": "It broke"},
        name="Shane",
        email="shane@example.com",
        category="Bug Report",
    )
    assert entry["app"] == "TestApp"
    assert entry["name"] == "Shane"
    assert entry["summary"] == "Crash"
    assert entry["message"] == "It broke"
    assert entry["category"] == "Bug Report"


def test_platform_default_resolves():
    import sys
    schema = load_schema({
        **MINIMAL_SCHEMA,
        "fields": [
            *MINIMAL_SCHEMA["fields"],
            {"name": "platform", "label": "OS", "type": "text", "default": "__platform__"},
        ],
    })
    defaults = schema.resolve_defaults()
    assert defaults["platform"]  # should be populated with real OS string
