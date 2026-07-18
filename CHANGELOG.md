# Changelog

All notable changes to feedback-hub are documented here.

## [1.0.2] - 2026-07-17

### Changed

- The default form's short field is now labelled **Title** (was "Summary") and the long field **Description** (was "Details"), so users understand the short field is the issue title and the long field is where the full report goes -- the "Summary caps at 120 characters, I can't paste a description" confusion (Community-Access/quill#1102). The title's length cap is raised to 200 characters, and the GitHub issue-title truncation matches, so a slightly longer title survives while the full detail always lands in the issue body.
- Fixed `__version__` in `feedback_hub/__init__.py`, which had drifted to 1.0.0 while `pyproject.toml` read 1.0.1; both now read 1.0.2.

## [1.0.1] - 2026-07-03

### Fixed
- `FeedbackDialog` crashed on construction under wxPython 4.2.5+ with a C++
  sizer assertion: the Submit Issue and Cancel buttons were parented to the
  dialog while their `StdDialogButtonSizer` was set on the inner panel.
  Host apps that caught the exception (QUILL) silently fell back to their
  legacy reporting flow; the buttons are now children of the panel.

## [1.0.0] - 2026-06-06

Initial public release.

### Added
- Multi-framework GitHub issue submission library with a unified GitHub Issues backend
- wxPython native dialog (`FeedbackDialog`) for desktop apps (ChapterForge, QUILL)
- Flask blueprint (`make_blueprint`) for web apps (GLOW)
- Headless/CLI `submit()` function for non-UI contexts
- JSON-schema-driven form definition (`load_schema`, `AppSchema`, `FieldSchema`)
- Local SQLite storage with GitHub sync tracking (`save`, `list_all`, `update_github_sync`)
- GLOW compatibility shim (`compat_glow`) for non-breaking migration from `support_hub`
- Fine-grained PAT security model — `issues:write` scope only, safe to bundle in desktop apps
- PyPI publishing via GitHub Actions OIDC trusted publisher (no token rotation required)
