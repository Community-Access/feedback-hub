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

## Token Security Notes

**Fine-grained PATs with issues:write only are safe to bundle in desktop apps.**

- Scope: issues:write on ONE repo only
- Worst-case misuse: someone files extra issues in your repo
- Cannot access code, other repos, account settings, or billing
- Rotate annually or if compromised

**Server apps (GLOW):** Token stays in `.env`, never touches clients.

**Desktop apps (ChapterForge, QUILL):** Bundle a fine-grained PAT.
```python
# In your app's constants or settings:
CHAPTERFORGE_GITHUB_TOKEN = "github_pat_..."  # issues:write on BITS-ACB/chapterforge only
```
