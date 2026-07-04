"""wxPython native feedback dialog.

Usage (ChapterForge, QUILL, any wxPython app)::

    from feedback_hub.wx_dialog import FeedbackDialog
    from feedback_hub import AppSchema, load_schema

    schema = load_schema(Path("schemas/chapterforge.json"))
    dlg = FeedbackDialog(parent, schema=schema, github_token=TOKEN)
    dlg.ShowModal()
    dlg.Destroy()

The dialog submits directly to GitHub Issues -- no browser, no clipboard.
"""
from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Any, Optional

from feedback_hub._github import GitHubConfig, create_issue, resolve_token
from feedback_hub._schema import AppSchema, build_entry
from feedback_hub._storage import save as save_entry, update_github_sync


class FeedbackDialog:
    """Accessible wxPython dialog that submits issues directly to GitHub.

    Requires wxPython to be importable. It is imported lazily so the
    rest of feedback_hub works in environments without wx (e.g. GLOW).
    """

    def __init__(
        self,
        parent: Any,
        *,
        schema: AppSchema,
        github_token: str = "",
        db_path: Optional[Path] = None,
        app_version: str = "",
    ) -> None:
        import wx

        self._wx = wx
        self._schema = schema
        self._app_version = app_version or schema.version
        self._db_path = db_path or (
            Path.home() / "AppData" / "Roaming" / schema.app / "feedback.db"
            if sys.platform == "win32"
            else Path.home() / ".local" / "share" / schema.app.lower() / "feedback.db"
        )
        self._token = resolve_token(github_token)
        self._dialog = wx.Dialog(
            parent,
            title=f"Report an Issue - {schema.app}",
            size=(720, 680),
        )
        self._field_controls: dict[str, Any] = {}
        self._build_ui()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def ShowModal(self) -> int:
        return self._dialog.ShowModal()

    def Destroy(self) -> None:
        self._dialog.Destroy()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        wx = self._wx
        schema = self._schema
        dlg = self._dialog
        panel = wx.Panel(dlg)
        root = wx.BoxSizer(wx.VERTICAL)

        # Instruction text
        intro = wx.StaticText(
            panel,
            label=(
                f"Describe your issue and it will be submitted directly to the "
                f"{schema.app} issue tracker. No account required."
            ),
        )
        intro.Wrap(660)
        root.Add(intro, 0, wx.ALL | wx.EXPAND, 8)

        # Name / email row
        name_row = wx.BoxSizer(wx.HORIZONTAL)
        name_label = wx.StaticText(panel, label="Your name (optional)")
        self._name_ctrl = wx.TextCtrl(panel)
        self._name_ctrl.SetName("Your name")
        email_label = wx.StaticText(panel, label="Email (optional)")
        self._email_ctrl = wx.TextCtrl(panel)
        self._email_ctrl.SetName("Email address")
        name_row.Add(name_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        name_row.Add(self._name_ctrl, 1, wx.RIGHT, 16)
        name_row.Add(email_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        name_row.Add(self._email_ctrl, 1)
        root.Add(name_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        # Category
        cat_label = wx.StaticText(panel, label="Category")
        self._cat_ctrl = wx.Choice(panel, choices=schema.categories)
        self._cat_ctrl.SetName("Issue category")
        self._cat_ctrl.SetSelection(0)
        root.Add(cat_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        root.Add(self._cat_ctrl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)

        # Schema-driven fields
        defaults = schema.resolve_defaults(self._app_version)
        for f in schema.fields:
            label = wx.StaticText(panel, label=f.label + (" *" if f.required else ""))
            root.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
            if f.type == "textarea":
                ctrl = wx.TextCtrl(panel, style=wx.TE_MULTILINE, size=(-1, 90))
            elif f.type == "select" and f.options:
                ctrl = wx.Choice(panel, choices=f.options)
            else:
                ctrl = wx.TextCtrl(panel)

            ctrl.SetName(f.label)
            if isinstance(ctrl, wx.TextCtrl) and defaults.get(f.name):
                ctrl.SetValue(defaults[f.name])
            if f.placeholder and isinstance(ctrl, wx.TextCtrl):
                ctrl.SetHint(f.placeholder)
            root.Add(ctrl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 8)
            self._field_controls[f.name] = ctrl

        # Buttons. Parent them on the panel: their sizer is set on the panel,
        # and wxPython (>= 4.2.5 asserts on this) requires sizer-managed
        # windows to be children of the sizer's associated window.
        buttons = wx.StdDialogButtonSizer()
        self._submit_btn = wx.Button(panel, wx.ID_OK, label="Submit Issue")
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, label="Cancel")
        buttons.AddButton(self._submit_btn)
        buttons.AddButton(cancel_btn)
        buttons.Realize()
        root.Add(buttons, 0, wx.ALL | wx.EXPAND, 8)

        panel.SetSizer(root)
        panel.Layout()

        self._submit_btn.Bind(wx.EVT_BUTTON, self._on_submit)
        self._name_ctrl.SetFocus()

    def _on_submit(self, _event: Any) -> None:
        wx = self._wx
        values = {}
        for name, ctrl in self._field_controls.items():
            if isinstance(ctrl, wx.Choice):
                sel = ctrl.GetSelection()
                values[name] = ctrl.GetString(sel) if sel != wx.NOT_FOUND else ""
            else:
                values[name] = ctrl.GetValue().strip()

        cat_sel = self._cat_ctrl.GetSelection()
        category = (
            self._cat_ctrl.GetString(cat_sel)
            if cat_sel != wx.NOT_FOUND
            else self._schema.categories[0]
        )

        errors = self._schema.validate(values)
        if errors:
            wx.MessageBox(
                "\n".join(errors),
                f"{self._schema.app} - Validation Error",
                wx.OK | wx.ICON_WARNING,
                self._dialog,
            )
            return

        entry = build_entry(
            self._schema,
            values,
            name=self._name_ctrl.GetValue().strip(),
            email=self._email_ctrl.GetValue().strip(),
            category=category,
            app_version=self._app_version,
            metadata={
                "python": sys.version.splitlines()[0],
                "platform": platform.platform(),
            },
        )

        # Store locally first
        try:
            row_id = save_entry(entry, self._db_path)
        except Exception:
            row_id = None

        # Submit to GitHub
        if not self._token:
            wx.MessageBox(
                "Your report was saved locally but could not be submitted to GitHub - "
                "no token is configured.",
                f"{self._schema.app} - Saved Locally",
                wx.OK | wx.ICON_INFORMATION,
                self._dialog,
            )
            self._dialog.EndModal(wx.ID_OK)
            return

        cfg = GitHubConfig(
            token=self._token,
            repo=self._schema.github_repo,
            assignee=self._schema.github_assignee,
            labels=self._schema.github_labels,
        )
        issue_number, issue_url, error = create_issue(entry, cfg)

        if row_id is not None:
            try:
                update_github_sync(
                    row_id,
                    issue_number=issue_number,
                    issue_url=issue_url,
                    error=error,
                    db_path=self._db_path,
                )
            except Exception:
                pass

        if issue_url:
            wx.MessageBox(
                f"Your report has been submitted successfully.\n\nIssue: {issue_url}",
                f"{self._schema.app} - Thank You",
                wx.OK | wx.ICON_INFORMATION,
                self._dialog,
            )
        else:
            wx.MessageBox(
                "Your report was saved but could not be submitted to GitHub right now.\n"
                "We will retry automatically on next launch.\n\n"
                f"Error: {error}",
                f"{self._schema.app} - Saved",
                wx.OK | wx.ICON_WARNING,
                self._dialog,
            )

        self._dialog.EndModal(wx.ID_OK)
