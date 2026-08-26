"""The transport that lets an app stop carrying a GitHub token.

Every QUILL build currently compiles one into the installer, so anyone who
unzips one has it. Scoping it to issues on a single repository bounds the
damage to issue spam, which is why it has been tolerable -- but an app that
posts to the server needs no credential at all.

Both halves are tested against each other here: the client's ``relay_entry``
and the server's ``/submit/feedback``, driven through the real WSGI callable.
Testing either alone would prove the wire format matches itself.
"""

from __future__ import annotations

import io
import json

import pytest

from feedback_hub._relay import relay_entry
from feedback_hub.server import ServerConfig, create_app, labels_for, validate_entry
from feedback_hub.server import _Rejected


def config(**overrides) -> ServerConfig:
    settings = {
        "token": "test-token",
        "repo": "Community-Access/quill",
        "feedback_per_minute": 100,
        "feedback_per_day": 100,
    }
    settings.update(overrides)
    return ServerConfig(**settings)


class FakeGitHub:
    def __init__(self, *, number: int = 77, error: str | None = None) -> None:
        self.number = number
        self.error = error
        self.calls: list[tuple] = []

    def __call__(self, entry, cfg):  # noqa: ANN001, ANN204
        self.calls.append((entry, cfg))
        if self.error:
            return None, None, self.error
        return self.number, f"https://github.com/o/r/issues/{self.number}", None


def entry(**overrides) -> dict:
    base = {
        "app": "Quill Radio",
        "category": "Bug Report",
        "summary": "Browse goes silent",
        "message": "NVDA says nothing when I press Browse.",
        "version": "3.0.0",
        "platform": "Windows 11",
        "email": "listener@example.org",
        "fingerprint": "",
    }
    base.update(overrides)
    return base


def call(app, body, *, path="/submit/feedback", method="POST"):
    raw = json.dumps(body).encode("utf-8")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "REMOTE_ADDR": "203.0.113.7",
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
    }
    captured: dict = {}

    def start_response(status, headers):  # noqa: ANN001, ANN202
        captured["status"] = status
        captured["headers"] = headers

    payload = b"".join(app(environ, start_response))
    return int(captured["status"].split()[0]), json.loads(payload) if payload else None


# --------------------------------------------------------------------------
# the endpoint
# --------------------------------------------------------------------------


def test_a_report_is_filed_with_no_token_from_the_caller():
    """The whole point: the credential is the server's, not the app's."""
    github = FakeGitHub(number=1500)
    app = create_app(config(), report=github)

    status, body = call(app, {"entry": entry()})

    assert status == 200
    assert body == {"ok": True, "number": 1500, "url": "https://github.com/o/r/issues/1500"}
    filed, cfg = github.calls[0]
    assert filed["message"] == "NVDA says nothing when I press Browse."
    assert cfg.token == "test-token"  # the server's, never sent by the client


def test_the_product_and_type_labels_come_from_the_report():
    """One shared repository serves all seven applications, so product identity
    is a label. Applying it here means a report arrives already triaged by
    product without an agent typing anything."""
    github = FakeGitHub()
    app = create_app(config(), report=github)

    call(app, {"entry": entry(app="Quill Weather", category="Feature Request")})

    labels = github.calls[0][1].labels
    assert "product:weather" in labels
    assert "type:feature" in labels
    assert "source:app" in labels


def test_an_unknown_app_is_refused():
    """The name becomes a label and a title prefix in a public repository. An
    endpoint that accepts any app name accepts any junk, permanently."""
    github = FakeGitHub()
    app = create_app(config(), report=github)

    status, body = call(app, {"entry": entry(app="Definitely Not Ours")})

    assert status == 400
    assert github.calls == []


def test_a_report_with_no_description_is_refused():
    app = create_app(config(), report=FakeGitHub())
    assert call(app, {"entry": entry(message="   ")})[0] == 400


def test_an_enormous_report_is_refused():
    app = create_app(config(), report=FakeGitHub())
    assert call(app, {"entry": entry(message="x" * 25_000)})[0] == 400


def test_the_fingerprint_survives_so_crash_dedup_keeps_working():
    """Two people hitting one crash should still land on one issue."""
    github = FakeGitHub()
    app = create_app(config(), report=github)

    call(app, {"entry": entry(fingerprint="abc123", version_label=True)})

    filed = github.calls[0][0]
    assert filed["fingerprint"] == "abc123"
    assert filed["version_label"] is True


def test_github_failure_is_502_and_hides_the_detail():
    github = FakeGitHub(error="GitHub API error 401: Bad credentials")
    app = create_app(config(), report=github)

    status, body = call(app, {"entry": entry()})

    assert status == 502
    assert "credentials" not in body["error"].lower()


def test_reports_are_rate_limited_but_more_kindly_than_suggestions():
    """A person reporting a crash may legitimately send two in a minute, and
    turning that away teaches them the button does not work."""
    app = create_app(config(feedback_per_minute=2, feedback_per_day=20), report=FakeGitHub())

    assert call(app, {"entry": entry()})[0] == 200
    assert call(app, {"entry": entry()})[0] == 200
    assert call(app, {"entry": entry()})[0] == 429


def test_the_feedback_endpoint_does_not_disturb_the_picks_one():
    """Both live in one process; neither may shadow the other."""
    app = create_app(config(), submit=FakeGitHub(), report=FakeGitHub())

    assert call(app, {"entry": entry()}, path="/submit/feedback")[0] == 200
    assert call(app, {}, path="/submit/picks")[0] == 400  # reached, and refused on shape
    assert call(app, {}, path="/submit/nothing")[0] == 404
    assert call(app, {"entry": entry()}, method="GET")[0] == 405


# --------------------------------------------------------------------------
# validation, called directly
# --------------------------------------------------------------------------


def test_validate_entry_trims_and_defaults():
    clean = validate_entry(
        {"app": "QUILL", "message": " hello ", "summary": "s" * 500}, ("QUILL",)
    )
    assert clean["message"] == "hello"
    assert len(clean["summary"]) == 200
    assert clean["category"] == "feedback"


def test_validate_entry_bounds_the_extra_fields():
    """A report is a paragraph and some environment detail, not a document
    store somebody found an open endpoint for."""
    clean = validate_entry(
        {
            "app": "QUILL",
            "message": "x",
            "extra_fields": {f"k{i}": "v" for i in range(100)},
        },
        ("QUILL",),
    )
    assert len(clean["extra_fields"]) == 40


def test_validate_entry_rejects_a_non_object():
    for bad in (None, "a string", ["a", "list"], 7):
        with pytest.raises(_Rejected):
            validate_entry(bad, ("QUILL",))


def test_labels_are_deduplicated():
    labelled = labels_for({"app": "QUILL", "category": "bug"}, ["type:bug", "needs-triage"])
    assert labelled.count("type:bug") == 1


# --------------------------------------------------------------------------
# the client, against the real endpoint
# --------------------------------------------------------------------------


def test_relay_entry_speaks_the_shape_the_server_reads(monkeypatch):
    """Client and server checked against each other. Testing either alone
    proves only that the wire format matches itself."""
    github = FakeGitHub(number=99)
    app = create_app(config(), report=github)
    seen: dict = {}

    class FakeResponse:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *exc):  # noqa: ANN002, ANN204
            return False

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        status, payload = call(app, seen["body"])
        seen["status"] = status
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    number, url, error = relay_entry("https://lp.example/submit/feedback", entry())

    assert error is None
    assert number == 99 and url.endswith("/99")
    assert seen["status"] == 200
    assert seen["body"]["entry"]["app"] == "Quill Radio"


def test_relay_entry_returns_errors_rather_than_raising(monkeypatch):
    """A report that cannot be sent has still been saved locally. Raising here
    would turn 'we could not send this' into a crash in the middle of somebody
    reporting a crash."""

    def explode(request, timeout=None):  # noqa: ANN001, ANN202
        raise OSError("network is down")

    monkeypatch.setattr("urllib.request.urlopen", explode)

    number, url, error = relay_entry("https://lp.example/submit/feedback", entry())
    assert number is None and url is None
    assert "network is down" in error


def test_relay_entry_without_a_url_says_so():
    assert relay_entry("", entry())[2] == "no submission server configured"
