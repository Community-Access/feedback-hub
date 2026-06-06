# Changelog

All notable changes to feedback-hub are documented here.

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
