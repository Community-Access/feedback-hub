"""Crash fingerprints: recognising the same crash twice.

One crash produces one issue. That is not what happens today.

A triage of Community-Access/quill on 2026-08-12 closed **four** issues that
were one crash (one user, three minutes apart), one that was a second report of
another (26 seconds apart), and two more of that same crash filed weeks later.
Eight issues, two bugs. Every one of those cost a human a triage cycle to read,
match by eye, and close as a duplicate.

Nothing about that is inherent. A crash report already carries everything
needed to recognise it: the exception class, and the frames it came through.
This module turns those into a short, stable identifier, and
:mod:`feedback_hub._github` uses it to comment on the existing open issue
instead of filing a new one.

Two design rules, both learned from what makes fingerprints fail in practice:

**Line numbers are excluded.** The same bug moves down a file every time
somebody adds an import above it. Fingerprinting on ``file:line`` means the
same crash gets a new identity on every release -- which is precisely when
duplicate reports arrive fastest, because that is when everyone updates.
Module and function name are stable across everything except a rename.

**The exception's message is excluded.** Messages routinely embed a path, an
index, a filename, or a value that differs per user for the same defect
(``KeyError: 'a3f9'``). Including it would give every reporter their own
fingerprint, which is the same as having none.

What is left -- exception class plus the shape of the call stack -- is what an
engineer actually uses to say "that's the same crash", so it is what this
matches on.

No dependencies beyond the standard library; safe to import anywhere.
"""

from __future__ import annotations

import hashlib
import re

#: Length of the rendered fingerprint. 12 hex characters is ~48 bits: far more
#: than enough to keep distinct crashes apart in one repository, and short
#: enough to read aloud, put in a label, and paste into a search box.
FINGERPRINT_LENGTH = 12

#: How many of the innermost frames take part. The deepest frames are the ones
#: that identify a defect; the outermost are the event loop and are identical
#: for every crash in the app. Five is enough to separate two different bugs
#: that share an immediate cause, without making the fingerprint so specific
#: that a slightly different path through the same bug misses.
DEFAULT_FRAME_DEPTH = 5

#: The marker embedded in an issue body. HTML comment, so it is invisible in
#: the rendered issue but present in the body text the API returns -- which is
#: what makes an existing issue findable without a database.
MARKER_PREFIX = "<!-- feedback-hub-fingerprint:"
MARKER_SUFFIX = "-->"

_MARKER_RE = re.compile(
    re.escape(MARKER_PREFIX) + r"\s*([0-9a-f]{4,64})\s*" + re.escape(MARKER_SUFFIX)
)

#: ``  File "C:\path\to\mod.py", line 42, in func`` -- the standard traceback
#: frame line, in the two forms Python emits (with and without ``in func``).
_FRAME_RE = re.compile(
    r'^\s*File\s+"(?P<file>[^"]+)",\s+line\s+\d+(?:,\s+in\s+(?P<func>\S+))?',
    re.MULTILINE,
)

#: ``ExceptionClass: message`` on the last non-indented line of a traceback.
#: Dotted names are kept whole (``quill.core.errors.QuillError``) and then
#: reduced to the final segment, so a module reorganisation does not change
#: the identity of the exception.
_EXC_LINE_RE = re.compile(r"^(?P<cls>[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|\w*))\s*:")


def _normalise_module(path: str) -> str:
    """A frame's file path reduced to something stable across machines.

    An absolute path contains the reporter's username, their drive layout, and
    whether they run from source or a frozen build -- none of which say
    anything about the crash, and all of which differ between two people
    hitting the identical bug. What survives is the tail of the path from the
    last recognisable package root, or failing that the bare filename.
    """
    cleaned = path.replace("\\", "/").strip()
    if not cleaned:
        return "?"
    parts = [part for part in cleaned.split("/") if part]
    # Frozen builds report paths under _internal/ or a temporary _MEI folder;
    # source runs report the real tree. Take the last three segments, which is
    # enough to tell quill/core/podcasts/queue.py from quill/ui/queue.py while
    # ignoring everything above the package.
    tail = parts[-3:] if len(parts) >= 3 else parts
    return "/".join(tail)


def _normalise_exception_class(name: str) -> str:
    """The final segment of a possibly-dotted exception class name."""
    cleaned = (name or "").strip()
    if not cleaned:
        return "Exception"
    return cleaned.rsplit(".", 1)[-1]


def compute_fingerprint(
    exception_class: str,
    frames: list[tuple[str, str]],
    *,
    depth: int = DEFAULT_FRAME_DEPTH,
) -> str:
    """A stable id for one crash, from its class and its innermost frames.

    ``frames`` is ``[(file_path, function_name), ...]`` in traceback order
    (outermost first, as Python prints them); the innermost ``depth`` are
    used. Returns :data:`FINGERPRINT_LENGTH` lowercase hex characters.

    Deterministic and pure -- the same crash on two machines, in two releases,
    produces the same string, which is the entire point.
    """
    cls = _normalise_exception_class(exception_class)
    innermost = frames[-depth:] if depth > 0 else list(frames)
    rendered = [cls]
    for file_path, function in innermost:
        rendered.append(f"{_normalise_module(file_path)}::{(function or '?').strip()}")
    digest = hashlib.sha256("\n".join(rendered).encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_LENGTH]


def parse_traceback_frames(text: str) -> list[tuple[str, str]]:
    """Extract ``(file, function)`` pairs from formatted traceback text.

    Tolerant by design: this is fed real crash reports, which may be truncated
    at the top, may carry several chained tracebacks, and may be surrounded by
    other report text. Every frame line it recognises is returned in order;
    anything it does not recognise is ignored rather than raising.
    """
    frames: list[tuple[str, str]] = []
    for match in _FRAME_RE.finditer(text or ""):
        frames.append((match.group("file") or "", match.group("func") or "?"))
    return frames


def fingerprint_from_traceback(text: str, *, depth: int = DEFAULT_FRAME_DEPTH) -> str:
    """Fingerprint a crash from its formatted traceback text alone.

    For callers holding a saved ``crash-*.txt`` rather than a live exception.
    Returns ``""`` when the text has no recognisable frames -- a caller must
    treat an empty fingerprint as "cannot deduplicate this", never as a
    fingerprint that happens to be blank, or every unparseable report would
    collapse onto one issue.
    """
    frames = parse_traceback_frames(text)
    if not frames:
        return ""
    exception_class = "Exception"
    for line in reversed((text or "").splitlines()):
        stripped = line.strip()
        if not stripped or line.startswith((" ", "\t")):
            continue
        match = _EXC_LINE_RE.match(stripped)
        if match:
            exception_class = match.group("cls")
            break
        if stripped.endswith(("Error", "Exception", "Exit")) and " " not in stripped:
            exception_class = stripped
            break
    return compute_fingerprint(exception_class, frames, depth=depth)


def marker(fingerprint: str) -> str:
    """The hidden body marker for *fingerprint*."""
    return f"{MARKER_PREFIX} {fingerprint} {MARKER_SUFFIX}"


def extract_marker(body: str) -> str:
    """The fingerprint embedded in an issue body, or ``""`` if there is none."""
    match = _MARKER_RE.search(body or "")
    return match.group(1) if match else ""


def label_for(fingerprint: str) -> str:
    """The label form, so a fingerprint is filterable in the GitHub UI."""
    return f"crash-id:{fingerprint}"


__all__ = [
    "DEFAULT_FRAME_DEPTH",
    "FINGERPRINT_LENGTH",
    "MARKER_PREFIX",
    "MARKER_SUFFIX",
    "compute_fingerprint",
    "extract_marker",
    "fingerprint_from_traceback",
    "label_for",
    "marker",
    "parse_traceback_frames",
]
