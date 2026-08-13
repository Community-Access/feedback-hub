# feedback-hub

Multi-framework GitHub issue submission library. Native UI per framework, centralized GitHub backend.

- **wxPython apps** (ChapterForge, QUILL): native dialog, direct GitHub submission
- **Flask apps** (GLOW): web form, direct GitHub submission
- **CLI / headless**: function call, direct GitHub submission

**Crash deduplication (1.1.0).** Pass a `fingerprint` and the same crash
reported twice becomes one issue with two comments, not two issues. Build one
with `compute_fingerprint()` from a live exception or
`fingerprint_from_traceback()` from a saved traceback — both produce the same
id for the same crash. Line numbers, exception messages, and absolute paths are
deliberately excluded, so a defect still matches itself across releases,
machines, and frozen builds.

See [INTEGRATING.md](INTEGRATING.md) for integration guides.
