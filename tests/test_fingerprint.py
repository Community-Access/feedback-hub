"""Crash fingerprints: stability, and what they deliberately ignore."""
from __future__ import annotations

from feedback_hub._fingerprint import (
    FINGERPRINT_LENGTH,
    compute_fingerprint,
    extract_marker,
    fingerprint_from_traceback,
    label_for,
    marker,
    parse_traceback_frames,
)

FRAMES = [
    ("C:/Users/alice/quill/quill/ui/main_frame.py", "on_save"),
    ("C:/Users/alice/quill/quill/core/document.py", "write"),
    ("C:/Users/alice/quill/quill/io/markdown.py", "dump"),
]


class TestShape:
    def test_is_the_documented_length_and_hex(self):
        fp = compute_fingerprint("ValueError", FRAMES)
        assert len(fp) == FINGERPRINT_LENGTH
        assert all(c in "0123456789abcdef" for c in fp)

    def test_is_deterministic(self):
        assert compute_fingerprint("ValueError", FRAMES) == compute_fingerprint(
            "ValueError", FRAMES
        )

    def test_no_frames_still_produces_one(self):
        assert compute_fingerprint("ValueError", [])


class TestWhatItIgnores:
    """Each of these is a real reason fingerprints fail in the wild."""

    def test_the_reporters_home_directory_does_not_change_it(self):
        theirs = [(p.replace("/Users/alice/", "/Users/bob/"), f) for p, f in FRAMES]

        assert compute_fingerprint("ValueError", theirs) == compute_fingerprint(
            "ValueError", FRAMES
        )

    def test_a_frozen_build_matches_a_source_run(self):
        frozen = [
            ("C:/Program Files/Quill/_internal/quill/ui/main_frame.py", "on_save"),
            ("C:/Program Files/Quill/_internal/quill/core/document.py", "write"),
            ("C:/Program Files/Quill/_internal/quill/io/markdown.py", "dump"),
        ]

        assert compute_fingerprint("ValueError", frozen) == compute_fingerprint(
            "ValueError", FRAMES
        )

    def test_windows_and_posix_separators_agree(self):
        windows = [(p.replace("/", "\\"), f) for p, f in FRAMES]

        assert compute_fingerprint("ValueError", windows) == compute_fingerprint(
            "ValueError", FRAMES
        )

    def test_a_dotted_exception_class_matches_its_bare_name(self):
        assert compute_fingerprint("quill.core.errors.QuillError", FRAMES) == (
            compute_fingerprint("QuillError", FRAMES)
        )

    def test_line_numbers_are_not_part_of_it(self):
        # The whole point: adding an import above the bug must not give the
        # same crash a new identity on the next release.
        moved = """Traceback (most recent call last):
  File "quill/ui/main_frame.py", line 999, in on_save
    doc.write()
ValueError: bad
"""
        original = """Traceback (most recent call last):
  File "quill/ui/main_frame.py", line 12, in on_save
    doc.write()
ValueError: bad
"""
        assert fingerprint_from_traceback(moved) == fingerprint_from_traceback(original)

    def test_the_exception_message_is_not_part_of_it(self):
        # KeyError: 'a3f9' vs KeyError: 'b710' is one defect, not two.
        first = """Traceback (most recent call last):
  File "quill/core/cache.py", line 5, in get
    return self._items[key]
KeyError: 'a3f9'
"""
        second = first.replace("a3f9", "b710")

        assert fingerprint_from_traceback(first) == fingerprint_from_traceback(second)


class TestWhatItSeparates:
    def test_a_different_exception_class_is_a_different_crash(self):
        assert compute_fingerprint("ValueError", FRAMES) != compute_fingerprint(
            "TypeError", FRAMES
        )

    def test_a_different_function_is_a_different_crash(self):
        other = [*FRAMES[:-1], (FRAMES[-1][0], "load")]

        assert compute_fingerprint("ValueError", other) != compute_fingerprint(
            "ValueError", FRAMES
        )

    def test_a_different_module_is_a_different_crash(self):
        other = [*FRAMES[:-1], ("quill/io/docx.py", FRAMES[-1][1])]

        assert compute_fingerprint("ValueError", other) != compute_fingerprint(
            "ValueError", FRAMES
        )

    def test_only_the_innermost_frames_count(self):
        # A deeper entry point into the same failing code is the same crash.
        deeper = [("quill/ui/menu.py", "dispatch"), *FRAMES]

        assert compute_fingerprint("ValueError", deeper, depth=3) == (
            compute_fingerprint("ValueError", FRAMES, depth=3)
        )


class TestParsingRealTracebacks:
    TRACEBACK = """Traceback (most recent call last):
  File "C:\\\\Users\\\\alice\\\\quill\\\\quill\\\\ui\\\\main_frame.py", line 4150, in _send
    self._do()
  File "C:\\\\Users\\\\alice\\\\quill\\\\quill\\\\core\\\\document.py", line 88, in write
    raise ValueError("nope")
ValueError: nope
"""

    def test_frames_come_out_in_order(self):
        frames = parse_traceback_frames(self.TRACEBACK)

        assert len(frames) == 2
        assert frames[0][1] == "_send"
        assert frames[1][1] == "write"

    def test_a_traceback_yields_a_fingerprint(self):
        assert len(fingerprint_from_traceback(self.TRACEBACK)) == FINGERPRINT_LENGTH

    def test_text_with_no_frames_yields_empty_not_a_hash(self):
        # An empty fingerprint means "cannot deduplicate". If this returned a
        # hash of nothing, every unparseable report would collapse onto one
        # issue -- which is worse than filing duplicates.
        assert fingerprint_from_traceback("something went wrong") == ""
        assert fingerprint_from_traceback("") == ""

    def test_surrounding_report_text_does_not_break_it(self):
        wrapped = f"QUILL crash report\n\nEnvironment\n  x\n\n{self.TRACEBACK}\n\nLocal file: x"

        assert fingerprint_from_traceback(wrapped) == fingerprint_from_traceback(self.TRACEBACK)

    def test_a_chained_traceback_uses_the_innermost_cause(self):
        chained = self.TRACEBACK + """
During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "quill/core/recover.py", line 3, in fix
    raise RuntimeError("worse")
RuntimeError: worse
"""
        assert fingerprint_from_traceback(chained) != fingerprint_from_traceback(self.TRACEBACK)


class TestMarker:
    def test_round_trips(self):
        assert extract_marker(marker("abc123def456")) == "abc123def456"

    def test_is_found_inside_a_real_body(self):
        body = f"## Feedback Report\n\nblah blah\n\n---\n{marker('deadbeef1234')}\n"

        assert extract_marker(body) == "deadbeef1234"

    def test_absent_marker_reads_as_empty(self):
        assert extract_marker("## Feedback Report\n\nno marker here") == ""
        assert extract_marker("") == ""

    def test_label_is_filterable_and_stable(self):
        assert label_for("abc123") == "crash-id:abc123"
