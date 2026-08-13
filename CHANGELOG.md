# Changelog

All notable changes to feedback-hub are documented here.

## [1.1.0] - 2026-08-13

### Added

- **Crash fingerprinting: one crash, one issue.** `submit()` and `create_issue()`
  accept a `fingerprint`. When an open issue already carries it, feedback-hub
  posts a comment there and returns *that* issue instead of filing a new one —
  so the second person to hit a crash lands on the first person's report.

  This exists because of a real cost. A triage of Community-Access/quill on
  2026-08-12 closed **four** issues that were one crash (one user, three
  minutes apart), one that was a second report of another (26 seconds apart),
  and two more of the same crash filed weeks later. Eight issues, two bugs,
  every one of them read and closed by hand.

- **`compute_fingerprint()` and `fingerprint_from_traceback()`** (new
  `feedback_hub._fingerprint`, re-exported at the top level). Build an id from
  an exception class plus the innermost traceback frames — from a live
  exception, or from saved traceback text. Both produce the same id for the
  same crash, so a report filed live and one filed later from a saved crash
  file deduplicate against each other.

  Deliberately **excluded** from the id: line numbers (the same bug moves down
  a file every time somebody adds an import, and a new identity on every
  release is worst exactly when duplicates arrive fastest), the exception
  message (`KeyError: 'a3f9'` and `KeyError: 'b710'` are one defect), and
  absolute paths (they carry the reporter's username and whether they ran a
  frozen build).

- **`version_label=True`** adds a `reported-version: X` label, so "is this
  already fixed?" is answerable from the issue list rather than by opening each
  report.

- A `crash-id:<fingerprint>` label and a hidden `<!-- feedback-hub-fingerprint:
  ... -->` body marker on fingerprinted issues. The marker is what makes the
  next report findable; the label is the visible half, so a maintainer can
  filter by it in the GitHub UI.

- The local SQLite store gains a `fingerprint` column. Existing databases are
  migrated in place by the same `ALTER TABLE` path the other columns use, so
  no history is lost.

### Notes

- **Deduplication can never lose a report.** Every failure in the lookup — a
  network blip, a permissions problem, an unexpected response, a comment that
  will not post — falls through to creating a new issue. A duplicate is a minor
  annoyance; a crash report that vanished because the deduplicator broke is a
  bug nobody would ever hear about.
- **Listing, not searching.** GitHub's search index lags issue creation by up
  to a minute, and the duplicates that hurt arrive *seconds* apart. The lookup
  lists open issues (two pages of 100, newest first) so it is live.
- Pull requests are never matched, even if one quotes a crash report.
- An empty fingerprint means "do not deduplicate" and files normally. Never
  pass a placeholder: unrelated reports would collapse onto one issue.
- Fully backward compatible — omit `fingerprint` and behaviour is unchanged.

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
