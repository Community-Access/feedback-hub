# Changelog

All notable changes to feedback-hub are documented here.

## [1.3.0] - 2026-08-26

### Added

- **`POST /submit/feedback`, so no app has to carry a GitHub token.** The
  server already held the credential for Community Picks; it now accepts an
  ordinary report as well, from any client, and files the issue itself.

  This is the fix for a real exposure rather than a theoretical one. Every
  QUILL build compiles a fine-grained token into its installer, so anybody who
  unzips one has it. Scoping it to issues on a single repository bounds the
  damage to issue spam, which is why it has been tolerable -- but an app that
  posts to the server needs no credential at all, and the token can then be
  rotated by editing one file on one machine instead of by shipping a release
  to every installed copy and waiting for people to take it.

- **`server_url=` on `submit()` and on `FeedbackDialog`.** Set it and leave
  `github_token` empty. The dialog is otherwise untouched -- same fields, same
  button, same words -- because only the transport changed, and a person
  reporting a problem should not be able to tell that anything moved.

  When both are configured the server wins: a token sitting alongside a server
  URL is one somebody forgot to remove.

- **`feedback_hub._relay`** -- the client half. It returns the same
  `(number, url, error)` triple as `create_issue`, so a caller can swap one for
  the other without knowing which it has, and it returns every failure rather
  than raising: a report that cannot be sent has still been saved locally, and
  an exception there would turn "we could not send this" into a crash in the
  middle of somebody reporting a crash.

### Notes

- **The seam matters more than the token.** Once submission is a POST to a URL,
  *where a report ends up stops being the app's business*. Moving Report a Bug
  from a GitHub issue to a support conversation in a help desk becomes a change
  on the server -- no release, no version skew, and no installed copy left
  behind still filing into the wrong place. That is the migration this unlocks;
  GitHub is simply what is behind the endpoint today.

- **Reports arrive already triaged by product.** The app name maps to a
  `product:*` label and the category to a `type:*` label, from the taxonomy in
  the Community Access support plan, plus `source:app`. One shared repository
  serves all seven QUILL applications, so product identity is a label rather
  than a repository, and applying it at the door means nobody types it later.

- **The app name is an allowlist, not free text.** It becomes a label and a
  title prefix in a public repository; an endpoint that accepts any app name
  accepts any junk, permanently. Same reasoning as the picks endpoint's shape
  check.

- **Crash deduplication survives the move.** The `fingerprint` and
  `version_label` fields are relayed intact, so the second person to hit a
  crash still lands on the first person's issue.

- **A separate, kinder rate limit.** Four a minute and forty a day, against the
  picks endpoint's one and twenty. Somebody reporting a crash may legitimately
  send two in a minute, and turning that away teaches them the button does not
  work. Sharing one limiter would also have meant a crash report consuming the
  suggestion budget of everybody behind the same address.

- **No CORS handling on this endpoint, deliberately.** It is reached by desktop
  applications, which send no `Origin`. A browser allowlist here would advertise
  a protection that is not present; what is present is the app allowlist, the
  size caps and the rate limit.

## [1.2.0] - 2026-08-26

### Added

- **A server, so that nobody needs a GitHub account.** `feedback_hub.server`
  is a zero-dependency WSGI application that accepts a submission over HTTP and
  files the issue itself, holding the only token.

  Until now "centralized GitHub backend" meant *GitHub is the backend* and
  every client carried its own token. That is fine on a desktop, where the
  worst case is issue spam in one repo. It is impossible on a web page: a token
  in a public page is extracted, and GitHub's own secret scanning revokes it
  within minutes, rightly. So a static site could only hand the visitor to
  GitHub's own new-issue form -- and that final press needs an account.

  `https://quillforall.org/picks/suggest/` is the first client. Anyone can now
  suggest a radio station for QUILL's Community Picks list without signing in
  to anything.

  It solves a second problem on the way. The bundled, issues-only token ships
  inside every QUILL installer, so anyone who unzips one can extract it. Once
  submission goes through a server, apps can stop carrying a credential at all,
  and the token can be rotated without shipping a release.

- **`create_raw_issue()`** in `feedback_hub._github`. `create_issue()` renders
  an entry into feedback-hub's own report layout, which would destroy a
  Community Picks suggestion: its body carries a machine-readable block that a
  downstream workflow parses. `create_raw_issue()` files a title and body
  verbatim. It lives with the other GitHub calls so every request in the
  package still goes out through one place -- one set of headers, one timeout,
  one error shape.

- **`deploy/`** -- Dockerfile, a compose service that joins an existing shared
  Caddy edge network, the Caddy snippet, and the runbook. See
  [deploy/README.md](deploy/README.md).

### Notes

- **Scope is deliberately one endpoint.** `POST /submit/picks` serves Community
  Picks and nothing else. Report a Bug still submits directly from each app,
  because migrating it at the same time would make the first deployment also
  the riskiest one. The shape is the one those clients move to later: same
  process, more endpoints.

- **What the endpoint refuses**, and why each refusal has a test rather than a
  comment: a body with no ```` ```json pick ```` block (such an issue looks
  fine in the review queue and publishes *nothing* when approved, so the
  failure would surface days later as "why is my station not in the list?");
  two such blocks, which is ambiguity a person should resolve; a kind that is
  not `stream` or `podcast`; a missing name or address; an address whose scheme
  is not the web; a request over 32 KB.

- **`http://` addresses are accepted on purpose.** 41% of the 400 most-played
  stations in the directory Quill Radio browses are http-only, among them the
  small community stations the catalogue exists for. An https-only rule written
  to protect listeners would have quietly excluded exactly them. The protection
  belongs where it helps: the catalogue itself arrives over https and signed.
  Still refused everywhere: `javascript:`, `file:`, `data:` -- which is also how
  an attacker would try to get script onto the review page that displays these.

- **The rate limit reads the last `X-Forwarded-For` entry, not the first.** A
  client can send a forwarded header of its own and the proxy appends to it, so
  the first entry is whatever the client claimed. Reading the first would make
  the limit evadable by anyone who read the source. A *refused* attempt is not
  counted, so being over the minute limit cannot push somebody over the day
  limit for retrying.

- **On spam control, once and permanently: Turnstile, never reCAPTCHA.**
  Turnstile is usually invisible and needs no puzzle. reCAPTCHA's image grids
  are precisely the barrier this project exists to remove -- a spam control that
  locks out blind users to keep out bots has failed at the only job that
  matters here. Written into the module docstring rather than a wiki because it
  is the kind of decision that gets made hastily at 2am.

### Changed

- `pytest` now puts `src/` on the path, so the suite tests the checkout rather
  than whatever happens to be installed in site-packages. Without it a source
  tree with a new module tests green against the last release and red the
  moment anybody else runs it.

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
