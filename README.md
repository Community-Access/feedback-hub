# feedback-hub

Multi-framework GitHub issue submission library. Native UI per framework, centralized GitHub backend.

- **wxPython apps** (ChapterForge, QUILL): native dialog, direct GitHub submission
- **Flask apps** (GLOW): web form, direct GitHub submission
- **CLI / headless**: function call, direct GitHub submission
- **Static sites** (quillforall.org): POST to the submission server, which holds
  the only token -- so the visitor needs no GitHub account at all

**The submission server (1.2.0).** `feedback_hub.server` is a zero-dependency
WSGI application that accepts a submission over HTTP and files the issue
itself. Every other client here carries its own token, which is fine on a
desktop and impossible on a web page: a token in a public page is extracted,
and GitHub's secret scanning revokes it within minutes. So a static site could
only hand the visitor to GitHub's own new-issue form, and that final press needs
an account. This is the piece that removes it.

```bash
FEEDBACK_HUB_GITHUB_TOKEN=github_pat_... python -m feedback_hub.server
# or, in production
gunicorn --bind 127.0.0.1:8095 feedback_hub.server:application
```

Scope is deliberately one endpoint -- `POST /submit/picks`, for QUILL's
Community Picks list. Apps still submit directly for now; they move to the
server once it has been up a while, and can then stop shipping a credential at
all. See [deploy/README.md](deploy/README.md) for the runbook.

**Crash deduplication (1.1.0).** Pass a `fingerprint` and the same crash
reported twice becomes one issue with two comments, not two issues. Build one
with `compute_fingerprint()` from a live exception or
`fingerprint_from_traceback()` from a saved traceback — both produce the same
id for the same crash. Line numbers, exception messages, and absolute paths are
deliberately excluded, so a defect still matches itself across releases,
machines, and frozen builds.

See [INTEGRATING.md](INTEGRATING.md) for integration guides.
