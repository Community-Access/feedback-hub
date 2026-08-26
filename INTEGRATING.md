# Integrating feedback-hub

## ChapterForge (wxPython)

### 1. Add to requirements

```
feedback-hub>=1.0
```

### 2. Add menu item to Help menu in app.py

```python
# In the Help menu setup:
report_item = help_menu.Append(wx.ID_ANY, "Report an Issue")
self.Bind(wx.EVT_MENU, self.on_report_issue, report_item)
```

### 3. Add handler

```python
def on_report_issue(self, _event):
    from pathlib import Path
    from feedback_hub import load_schema
    from feedback_hub.wx_dialog import FeedbackDialog

    schema = load_schema(Path(__file__).parent / "schemas" / "chapterforge.json")
    dlg = FeedbackDialog(
        self,
        schema=schema,
        github_token=CHAPTERFORGE_GITHUB_TOKEN,  # bundled fine-grained PAT
        app_version=__version__,
    )
    dlg.ShowModal()
    dlg.Destroy()
```

### 4. Token setup

Create a GitHub fine-grained PAT:
- Go to GitHub Settings > Developer Settings > Personal Access Tokens > Fine-grained
- Repository access: BITS-ACB/chapterforge only
- Permissions: Issues = Read and Write (everything else = None)
- Store as `CHAPTERFORGE_GITHUB_TOKEN` in a config or bundle in the app

---

## GLOW (Flask) - Non-breaking migration

### 1. Add to requirements.txt

```
feedback-hub>=1.0
```

### 2. Replace feedback.py (one line change)

```python
# web/src/acb_large_print_web/routes/feedback.py
# Replace entire file with:
from feedback_hub.compat_glow import feedback_bp
```

### 3. Any code importing from support_hub still works

```python
# This still works unchanged:
from acb_large_print_web.support_hub import create_support_issue, load_support_hub_config
# OR directly:
from feedback_hub.compat_glow import create_support_issue, load_support_hub_config
```

### 4. Environment variables

No changes. GLOW's existing env vars still work:
- `FEEDBACK_GITHUB_TOKEN`
- `FEEDBACK_GITHUB_REPO`
- `FEEDBACK_GITHUB_LABELS`
- `FEEDBACK_GITHUB_ASSIGNEE`
- `FEEDBACK_PASSWORD`
- `FEEDBACK_API_TOKEN`

---

## QUILL (wxPython)

Same pattern as ChapterForge. Replace `report_bug()` in main_frame.py:

```python
def report_bug(self) -> None:
    from pathlib import Path
    from feedback_hub import load_schema
    from feedback_hub.wx_dialog import FeedbackDialog

    schema = load_schema(Path(__file__).parent.parent / "schemas" / "quill.json")
    dlg = FeedbackDialog(
        self.frame,
        schema=schema,
        github_token=QUILL_GITHUB_TOKEN,
        app_version=__version__,
    )
    result = dlg.ShowModal()
    dlg.Destroy()
    if result == wx.ID_OK:
        self._set_status("Bug report submitted")
```

This replaces the clipboard+browser approach with direct GitHub submission.

---

## Crash reports: deduplicate them (1.1.0)

If your app files issues automatically from a crash handler, pass a
`fingerprint` and feedback-hub will comment on the open issue for that crash
instead of filing another one.

From a live exception, in an `excepthook`:

```python
import traceback
from feedback_hub import compute_fingerprint, submit

def handler(exc_type, exc_value, exc_tb):
    frames = [(f.filename, f.name) for f in traceback.extract_tb(exc_tb)]
    issue_url, error = submit(
        app="MyApp",
        github_repo="org/repo",
        github_token=TOKEN,
        summary=f"{exc_type.__name__}: {exc_value}",
        message=build_report(),          # your redacted body
        app_version=__version__,
        fingerprint=compute_fingerprint(exc_type.__name__, frames),
        version_label=True,              # adds `reported-version: X`
    )
```

From a traceback you saved earlier — a `crash-*.txt` written by a previous
session, say — use `fingerprint_from_traceback(text)` instead. **The two agree**
for the same crash, so a report filed live and one filed later from the saved
file deduplicate against each other rather than becoming two issues.

Three rules worth knowing:

- **An empty fingerprint means "do not deduplicate"** and files normally. Never
  pass a placeholder or a constant — unrelated reports would collapse onto one
  issue, which is much worse than duplicates.
- **`fingerprint_from_traceback` returns `""` when it finds no frames.** Log
  text is not a stable identity. Treat empty as "file this one normally".
- **Dedup never loses a report.** Any failure in the lookup falls through to
  creating a new issue.

What the fingerprint deliberately ignores, so the same defect matches itself:
line numbers (they shift on every release), the exception message (it embeds
per-user values), and absolute paths (they carry usernames and frozen-build
layout).

---

## Static sites and anything else with no place to hide a token (1.2.0)

A web page cannot hold a GitHub token. Not "should not" -- cannot: the page is
readable by everyone, so the token is extractable by everyone, and GitHub's own
secret scanning revokes a published one within minutes. A static site could
therefore only ever hand the visitor to GitHub's pre-filled new-issue form, and
that final press needs a GitHub account.

`feedback_hub.server` is the piece that removes the account. It is a
zero-dependency WSGI application: one process, holding the only token, in front
of which any number of accountless clients can stand.

### 1. Run it

```bash
FEEDBACK_HUB_GITHUB_TOKEN=github_pat_... python -m feedback_hub.server
# in production, behind a proxy that terminates TLS:
gunicorn --bind 127.0.0.1:8095 feedback_hub.server:application
```

See [deploy/README.md](deploy/README.md) for a container, a Caddy route and the
runbook. Configuration is entirely by environment variable -- the full list is
in the module docstring -- because the token must never live in a file inside an
image.

### 2. Post to it from the page

```js
fetch("https://lp.csedesigns.com/submit/picks", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ title: title, body: body })
})
  .then(function (response) { return response.json().then(function (data) {
    if (response.ok) { /* data.number is the issue number */ }
    else { /* data.error is plain English, safe to show */ }
  }); });
```

The response is always JSON. `200` carries `{ok, number, url}`; a refusal
carries `{error}` in words a visitor can act on, and never GitHub's own error
text -- that can include rate-limit details and token hints they could do
nothing with.

### 3. Two things that will catch you out

**The page's own Content-Security-Policy.** A `default-src 'none'` policy with
no `connect-src` blocks the `fetch` before it leaves the browser, and the
failure looks exactly like the server being down. Add the endpoint's origin:

```html
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; script-src 'self'; style-src 'self';
               connect-src https://lp.csedesigns.com; form-action 'none'; base-uri 'none'">
```

**CORS is a list, not a wildcard.** `PICKS_ALLOWED_ORIGINS` must name the site
posting to it. A disallowed origin is refused request-side as well as
response-side, so an unwanted origin never files an issue at all.

### 4. What it refuses, and why that is the useful part

The endpoint validates before it files anything. The refusal that earns its
keep is a body missing the machine-readable block the downstream workflow
parses: such an issue looks fine in the review queue and publishes *nothing*
when approved, so the failure would otherwise surface days later as "why is my
station not in the list?".

Also refused: two such blocks (ambiguity a person should resolve, not a
machine); a missing name or address; an address whose scheme is not the web, so
`javascript:`, `file:` and `data:` are out -- which is also how an attacker would
try to get script onto a review page that displays submissions; and anything
over 32 KB.

`http://` addresses are **accepted on purpose**. Refusing them would exclude
exactly the small community radio stations the catalogue exists for.

### 5. Rate limiting

One submission a minute and twenty a day per address, counted in memory, with
no store to administer. A *refused* attempt is not counted, so being over the
minute limit cannot push somebody over the day limit for retrying. Behind a
proxy the client address is the **last** `X-Forwarded-For` entry, not the
first: a client can send a header of its own and the proxy appends to it, so
the first entry is whatever the client claimed.

The limit is per process. With N workers the effective limit is N per minute
per address. Say so in the deployment rather than pretending otherwise.

### 6. Spam challenges: Turnstile, never reCAPTCHA

Set `TURNSTILE_SECRET` and every submission must carry a valid Turnstile token.
Turnstile is usually invisible and needs no puzzle. reCAPTCHA's image grids are
precisely the barrier this library's projects exist to remove -- a spam control
that locks out blind users to keep out bots has failed at the only job that
matters.

---

## Token Security Notes

**Fine-grained PATs with issues:write only are safe to bundle in desktop apps.**

- Scope: issues:write on ONE repo only
- Worst-case misuse: someone files extra issues in your repo
- Cannot access code, other repos, account settings, or billing
- Rotate annually or if compromised

**Server apps (GLOW):** Token stays in `.env`, never touches clients.

**The submission server (1.2.0):** same rule, and it is the direction of
travel. Once a client posts through the server it needs no credential at all,
and the token can be rotated by editing one `.env` and restarting -- rather than
by shipping a release to every installed copy. A bundled token is extractable
by anyone who unzips an installer; that is tolerable for issues-only scope, but
it is not better than not shipping one.

**Desktop apps (ChapterForge, QUILL):** Bundle a fine-grained PAT.
```python
# In your app's constants or settings:
CHAPTERFORGE_GITHUB_TOKEN = "github_pat_..."  # issues:write on BITS-ACB/chapterforge only
```
