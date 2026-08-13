"""Deduplication: comment on the open issue instead of filing another.

The behaviour these pin down is the one the 2026-08-12 QUILL triage needed:
eight issues that were two bugs, several of them filed seconds apart. The
tests that matter most are the failure paths -- a broken deduplicator must
never be the reason a crash report is lost.
"""
from __future__ import annotations

import json
from unittest.mock import patch
from urllib import error as urlerror

from feedback_hub._fingerprint import marker
from feedback_hub._github import (
    GitHubConfig,
    _build_payload,
    create_issue,
    find_open_issue_by_fingerprint,
)

CFG = GitHubConfig(token="t", repo="org/repo")
FP = "abc123def456"


class _Response:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _issue(number=7, body="", url=None, pull_request=False):
    issue = {
        "number": number,
        "body": body,
        "html_url": url or f"https://github.com/org/repo/issues/{number}",
    }
    if pull_request:
        issue["pull_request"] = {"url": "..."}
    return issue


class TestFindingTheOpenIssue:
    def test_finds_an_issue_carrying_the_marker(self):
        with patch(
            "feedback_hub._github.urlrequest.urlopen",
            return_value=_Response([_issue(body=f"report\n{marker(FP)}")]),
        ):
            number, url = find_open_issue_by_fingerprint(FP, CFG)

        assert number == 7
        assert url.endswith("/7")

    def test_a_different_fingerprint_is_not_a_match(self):
        with patch(
            "feedback_hub._github.urlrequest.urlopen",
            return_value=_Response([_issue(body=marker("999999999999"))]),
        ):
            assert find_open_issue_by_fingerprint(FP, CFG) == (None, None)

    def test_a_pull_request_is_never_matched(self):
        # The issues endpoint returns PRs too. A PR quoting a crash report
        # would otherwise swallow every future report of that crash.
        with patch(
            "feedback_hub._github.urlrequest.urlopen",
            return_value=_Response([_issue(body=marker(FP), pull_request=True)]),
        ):
            assert find_open_issue_by_fingerprint(FP, CFG) == (None, None)

    def test_an_empty_fingerprint_never_matches_anything(self):
        with patch("feedback_hub._github.urlrequest.urlopen") as opener:
            assert find_open_issue_by_fingerprint("", CFG) == (None, None)

        assert opener.call_count == 0  # and costs no API call

    def test_a_network_failure_reads_as_no_match(self):
        with patch(
            "feedback_hub._github.urlrequest.urlopen", side_effect=OSError("offline")
        ):
            assert find_open_issue_by_fingerprint(FP, CFG) == (None, None)

    def test_a_permissions_failure_reads_as_no_match(self):
        error = urlerror.HTTPError("u", 403, "Forbidden", {}, None)
        with patch("feedback_hub._github.urlrequest.urlopen", side_effect=error):
            assert find_open_issue_by_fingerprint(FP, CFG) == (None, None)

    def test_a_short_page_stops_the_scan(self):
        with patch(
            "feedback_hub._github.urlrequest.urlopen", return_value=_Response([_issue()])
        ) as opener:
            find_open_issue_by_fingerprint(FP, CFG)

        assert opener.call_count == 1

    def test_only_open_issues_are_considered(self):
        # A crash that was fixed, closed, and has now regressed must file a
        # fresh issue -- not comment on a closed one nobody is watching.
        urls = []

        def fake(req, timeout=0):
            urls.append(req.full_url)
            return _Response([])

        with patch("feedback_hub._github.urlrequest.urlopen", side_effect=fake):
            find_open_issue_by_fingerprint(FP, CFG)

        assert "state=open" in urls[0]

    def test_the_scan_is_newest_first(self):
        # When a crash somehow has two open issues, the newest is the live
        # conversation; commenting on the stale one would bury the report.
        urls = []

        def fake(req, timeout=0):
            urls.append(req.full_url)
            return _Response([])

        with patch("feedback_hub._github.urlrequest.urlopen", side_effect=fake):
            find_open_issue_by_fingerprint(FP, CFG)

        assert "sort=created" in urls[0]
        assert "direction=desc" in urls[0]


class TestCreateIssueDeduplicates:
    def test_a_matching_crash_comments_instead_of_filing(self):
        calls = []

        def fake(req, timeout=0):
            calls.append((req.get_method(), req.full_url))
            if req.get_method() == "GET":
                return _Response([_issue(body=marker(FP))])
            return _Response({"html_url": "https://github.com/org/repo/issues/7#c1"})

        with patch("feedback_hub._github.urlrequest.urlopen", side_effect=fake):
            number, url, error = create_issue({"message": "m", "fingerprint": FP}, CFG)

        assert error is None
        assert number == 7
        assert url.endswith("/7")  # the issue, not the comment
        assert any(m == "POST" and "comments" in u for m, u in calls)
        assert not any(m == "POST" and u.endswith("/issues") for m, u in calls)

    def test_no_match_files_a_new_issue(self):
        def fake(req, timeout=0):
            if req.get_method() == "GET":
                return _Response([])
            return _Response({"number": 12, "html_url": "https://github.com/org/repo/issues/12"})

        with patch("feedback_hub._github.urlrequest.urlopen", side_effect=fake):
            number, url, error = create_issue({"message": "m", "fingerprint": FP}, CFG)

        assert error is None
        assert number == 12

    def test_without_a_fingerprint_nothing_is_looked_up(self):
        calls = []

        def fake(req, timeout=0):
            calls.append(req.get_method())
            return _Response({"number": 1, "html_url": "u"})

        with patch("feedback_hub._github.urlrequest.urlopen", side_effect=fake):
            create_issue({"message": "m"}, CFG)

        assert calls == ["POST"]

    def test_a_failed_comment_falls_back_to_filing(self):
        # The report matters more than the tidiness of the tracker.
        def fake(req, timeout=0):
            if req.get_method() == "GET":
                return _Response([_issue(body=marker(FP))])
            if "comments" in req.full_url:
                raise OSError("comment failed")
            return _Response({"number": 99, "html_url": "https://github.com/org/repo/issues/99"})

        with patch("feedback_hub._github.urlrequest.urlopen", side_effect=fake):
            number, url, error = create_issue({"message": "m", "fingerprint": FP}, CFG)

        assert error is None
        assert number == 99

    def test_a_failed_lookup_falls_back_to_filing(self):
        def fake(req, timeout=0):
            if req.get_method() == "GET":
                raise OSError("offline")
            return _Response({"number": 5, "html_url": "https://github.com/org/repo/issues/5"})

        with patch("feedback_hub._github.urlrequest.urlopen", side_effect=fake):
            number, _url, error = create_issue({"message": "m", "fingerprint": FP}, CFG)

        assert error is None
        assert number == 5

    def test_the_comment_carries_the_new_report_not_just_a_plus_one(self):
        bodies = []

        def fake(req, timeout=0):
            if req.get_method() == "GET":
                return _Response([_issue(body=marker(FP))])
            bodies.append(json.loads(req.data.decode("utf-8"))["body"])
            return _Response({"html_url": "u"})

        entry = {
            "message": "it crashed while saving",
            "fingerprint": FP,
            "version": "1.2.3",
            "metadata": {"screen_reader": "JAWS"},
        }
        with patch("feedback_hub._github.urlrequest.urlopen", side_effect=fake):
            create_issue(entry, CFG)

        assert "it crashed while saving" in bodies[0]
        assert "1.2.3" in bodies[0]
        assert "JAWS" in bodies[0]


class TestPayload:
    def test_a_fingerprint_embeds_a_marker_and_a_label(self):
        payload = _build_payload({"message": "m", "fingerprint": FP}, CFG)

        assert marker(FP) in payload["body"]
        assert f"crash-id:{FP}" in payload["labels"]

    def test_the_marker_is_invisible_in_the_rendered_issue(self):
        payload = _build_payload({"message": "m", "fingerprint": FP}, CFG)

        assert payload["body"].strip().endswith("-->")
        assert payload["body"].count("<!--") == 1

    def test_no_fingerprint_means_no_marker_and_no_extra_label(self):
        payload = _build_payload({"message": "m"}, CFG)

        assert "feedback-hub-fingerprint" not in payload["body"]
        assert payload["labels"] == CFG.labels

    def test_the_configured_labels_are_never_mutated(self):
        before = list(CFG.labels)
        _build_payload({"message": "m", "fingerprint": FP}, CFG)

        assert CFG.labels == before

    def test_version_label_is_opt_in(self):
        without = _build_payload({"message": "m", "version": "1.2.3"}, CFG)
        with_label = _build_payload(
            {"message": "m", "version": "1.2.3", "version_label": True}, CFG
        )

        assert "reported-version: 1.2.3" not in without["labels"]
        assert "reported-version: 1.2.3" in with_label["labels"]

    def test_version_label_needs_a_version(self):
        payload = _build_payload({"message": "m", "version": "", "version_label": True}, CFG)

        assert not any(str(label).startswith("reported-version") for label in payload["labels"])
