"""Tests for the submission server -- the endpoint that removes the account.

These exercise the WSGI callable itself rather than a wrapper around it, so
what is tested is what gunicorn will run: real ``environ`` dictionaries, a real
``start_response``, real status lines. The only thing replaced is the GitHub
call, which is injected through ``create_app(submit=...)`` precisely so these
tests never need the network or a token.

The refusals matter more than the happy path here. A submission that is
accepted but malformed looks fine in the review queue and publishes nothing
when approved, so the failure surfaces days later as "why is my station not in
the list?" -- which is why every refusal below has a test rather than a comment.
"""
from __future__ import annotations

import io
import json

import pytest

from feedback_hub.server import (
    RateLimiter,
    ServerConfig,
    _Rejected,
    client_address,
    create_app,
    validate_suggestion,
)

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

FENCE = "```json pick"

PICK = {
    "type": "stream",
    "title": "Team FM",
    "description": "Community radio from the north east.",
    "stream_url": "http://stream.teamfm.example/live",
}


def body_for(pick: dict | None = None, *, blocks: int = 1, block_text: str | None = None) -> str:
    """A suggestion body in the shape ``pick_suggestion.parse_issue_body`` reads."""
    payload = json.dumps(PICK if pick is None else pick, indent=2)
    text = "**Team FM** -- suggested for the Community Picks list.\n\n"
    for _ in range(blocks):
        text += FENCE + "\n" + (block_text if block_text is not None else payload) + "\n```\n"
    return text


class FakeGitHub:
    """Stands in for ``create_raw_issue``; records exactly what it was handed."""

    def __init__(self, *, number: int = 4242, error: str | None = None) -> None:
        self.number = number
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, *, title, body, labels, config):  # noqa: ANN001, ANN204
        self.calls.append({"title": title, "body": body, "labels": labels, "config": config})
        if self.error:
            return None, None, self.error
        return self.number, "https://github.com/o/r/issues/%d" % self.number, None


def config(**overrides) -> ServerConfig:
    """A permissive config, so a test only opts in to the limit it is testing."""
    settings = {
        "token": "test-token",
        "repo": "Community-Access/quill",
        "label": "pick:suggestion",
        "per_minute": 100,
        "per_day": 100,
    }
    settings.update(overrides)
    return ServerConfig(**settings)


def call(
    app,
    method="POST",
    path="/submit/picks",
    payload=None,
    *,
    origin=None,
    raw=None,
    headers=None,
    content_length=None,
):
    """Drive the WSGI callable and return ``(status_code, headers, parsed_body)``."""
    if raw is None:
        raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "REMOTE_ADDR": "203.0.113.7",
        "CONTENT_LENGTH": str(len(raw) if content_length is None else content_length),
        "wsgi.input": io.BytesIO(raw),
    }
    if origin:
        environ["HTTP_ORIGIN"] = origin
    environ.update(headers or {})

    captured: dict = {}

    def start_response(status, response_headers):  # noqa: ANN001, ANN202
        captured["status"] = status
        captured["headers"] = response_headers

    body = b"".join(app(environ, start_response))
    parsed = json.loads(body) if body else None
    return int(captured["status"].split()[0]), dict(captured["headers"]), parsed


def submission(pick: dict | None = None) -> dict:
    return {"title": "[Pick] Station: Team FM", "body": body_for(pick)}


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------


def test_healthz_answers_without_a_token():
    """Monitoring must get a green light, and learn nothing about the token."""
    app = create_app(config(token=""), submit=FakeGitHub())
    status, _, body = call(app, method="GET", path="/healthz")
    assert status == 200
    assert body == {"ok": True}


def test_unknown_path_is_404():
    app = create_app(config(), submit=FakeGitHub())
    status, _, _ = call(app, method="POST", path="/submit/anything-else")
    assert status == 404


def test_options_preflight_is_204():
    app = create_app(config(), submit=FakeGitHub())
    status, headers, _ = call(app, method="OPTIONS", origin="https://quillforall.org")
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == "https://quillforall.org"
    assert "POST" in headers["Access-Control-Allow-Methods"]


def test_get_is_405():
    app = create_app(config(), submit=FakeGitHub())
    status, _, body = call(app, method="GET")
    assert status == 405
    assert "POST" in body["error"]


def test_trailing_slash_still_reaches_the_endpoint():
    """Somebody will configure the proxy with a slash. It should still work."""
    app = create_app(config(), submit=FakeGitHub())
    status, _, _ = call(app, path="/submit/picks/", payload=submission())
    assert status == 200


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_valid_submission_files_the_issue_verbatim():
    """The body must arrive at GitHub unchanged: picks-build.yml parses it."""
    github = FakeGitHub(number=1450)
    app = create_app(config(), submit=github)
    payload = submission()
    status, _, body = call(app, payload=payload)

    assert status == 200
    assert body == {
        "ok": True,
        "number": 1450,
        "url": "https://github.com/o/r/issues/1450",
    }
    assert len(github.calls) == 1
    assert github.calls[0]["title"] == payload["title"]
    # Verbatim but for surrounding whitespace, which the endpoint trims. The
    # fenced block and every line of prose inside it must survive untouched:
    # picks-build.yml parses this body, not a re-rendered one.
    assert github.calls[0]["body"] == payload["body"].strip()
    assert FENCE in github.calls[0]["body"]
    assert github.calls[0]["labels"] == ["pick:suggestion"]
    assert github.calls[0]["config"].repo == "Community-Access/quill"
    assert github.calls[0]["config"].token == "test-token"


def test_http_addresses_are_accepted():
    """41% of the most-played stations are http-only. Refusing them would have
    excluded exactly the community stations this catalogue exists for."""
    app = create_app(config(), submit=FakeGitHub())
    status, _, _ = call(app, payload=submission())
    assert status == 200


def test_podcast_feed_url_is_accepted():
    app = create_app(config(), submit=FakeGitHub())
    pick = {"type": "podcast", "title": "A Show", "feed_url": "https://example.org/feed.xml"}
    status, _, _ = call(app, payload=submission(pick))
    assert status == 200


def test_nothing_is_cacheable():
    """A cached 'thank you' would be a lie to the next visitor."""
    app = create_app(config(), submit=FakeGitHub())
    _, headers, _ = call(app, payload=submission())
    assert headers["Cache-Control"] == "no-store"


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_body_without_a_pick_block_is_refused():
    """The important one: such an issue publishes nothing when approved."""
    app = create_app(config(), submit=FakeGitHub())
    status, _, body = call(app, payload={"title": "T", "body": "Please add my station."})
    assert status == 400
    assert "expected shape" in body["error"]


def test_two_pick_blocks_are_refused():
    """Ambiguity a person should resolve, not a machine."""
    app = create_app(config(), submit=FakeGitHub())
    status, _, _ = call(app, payload={"title": "T", "body": body_for(blocks=2)})
    assert status == 400


def test_unparseable_pick_block_is_refused():
    app = create_app(config(), submit=FakeGitHub())
    status, _, _ = call(app, payload={"title": "T", "body": body_for(block_text="{nope")})
    assert status == 400


def test_pick_block_that_is_not_an_object_is_refused():
    app = create_app(config(), submit=FakeGitHub())
    status, _, _ = call(app, payload={"title": "T", "body": body_for(block_text="[1, 2]")})
    assert status == 400


@pytest.mark.parametrize(
    "pick, fragment",
    [
        ({"type": "video", "title": "X", "stream_url": "https://a.example"}, "station or a podcast"),
        ({"type": "", "title": "X", "stream_url": "https://a.example"}, "station or a podcast"),
        ({"type": "stream", "title": "   ", "stream_url": "https://a.example"}, "name is required"),
        ({"type": "stream", "title": "X"}, "address is required"),
        ({"type": "stream", "title": "X", "stream_url": "javascript:alert(1)"}, "https://"),
        ({"type": "stream", "title": "X", "stream_url": "file:///etc/passwd"}, "https://"),
        ({"type": "stream", "title": "X", "stream_url": "data:text/html,x"}, "https://"),
        ({"type": "stream", "title": "X", "stream_url": "https://a.example/ b"}, "space in it"),
    ],
)
def test_malformed_picks_are_refused(pick, fragment):
    app = create_app(config(), submit=FakeGitHub())
    status, _, body = call(app, payload=submission(pick))
    assert status == 400
    assert fragment in body["error"]


def test_a_refused_submission_never_reaches_github():
    github = FakeGitHub()
    app = create_app(config(), submit=github)
    call(app, payload={"title": "T", "body": "no block here"})
    assert github.calls == []


def test_non_json_is_refused():
    app = create_app(config(), submit=FakeGitHub())
    status, _, body = call(app, raw=b"title=Team+FM&body=hello")
    assert status == 400
    assert "JSON" in body["error"]


def test_json_that_is_not_an_object_is_refused():
    app = create_app(config(), submit=FakeGitHub())
    status, _, _ = call(app, raw=b'["title", "body"]')
    assert status == 400


def test_missing_title_or_body_is_refused():
    app = create_app(config(), submit=FakeGitHub())
    assert call(app, payload={"body": body_for()})[0] == 400
    assert call(app, payload={"title": "[Pick] Station: X"})[0] == 400


def test_an_oversized_declared_length_is_refused_before_it_is_read():
    """An unbounded read is how a small process becomes a memory bug."""
    app = create_app(config(), submit=FakeGitHub())
    status, _, _ = call(app, payload=submission(), content_length=10_000_000)
    assert status == 413


def test_an_oversized_body_is_refused():
    app = create_app(config(), submit=FakeGitHub())
    huge = json.dumps({"title": "X", "body": "y" * 40_000}).encode("utf-8")
    status, _, _ = call(app, raw=huge)
    assert status == 413


def test_a_long_body_within_the_read_cap_is_still_refused():
    app = create_app(config(), submit=FakeGitHub())
    padded = body_for() + ("filler " * 1500)
    status, _, body = call(app, payload={"title": "T", "body": padded})
    assert status == 400
    assert "longer than" in body["error"]


def test_github_failure_is_502_and_hides_the_detail():
    """GitHub's error can carry rate-limit details and token hints. The visitor
    could do nothing with either."""
    github = FakeGitHub(error="GitHub API error 401: Bad credentials")
    app = create_app(config(), submit=github)
    status, _, body = call(app, payload=submission())
    assert status == 502
    assert "credentials" not in body["error"].lower()
    assert "token" not in body["error"].lower()


# --------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------


def test_allowed_origin_is_echoed_not_starred():
    app = create_app(config(), submit=FakeGitHub())
    _, headers, _ = call(app, payload=submission(), origin="https://quillforall.org")
    assert headers["Access-Control-Allow-Origin"] == "https://quillforall.org"
    assert headers["Vary"] == "Origin"


def test_a_foreign_origin_is_refused_request_side():
    """A browser would refuse the response anyway. Refusing the request as well
    means an unwanted origin never files an issue."""
    github = FakeGitHub()
    app = create_app(config(), submit=github)
    status, _, _ = call(app, payload=submission(), origin="https://evil.example")
    assert status == 403
    assert github.calls == []


def test_no_origin_header_is_allowed():
    """curl, the in-app dialog, and anything that is not a browser send none."""
    app = create_app(config(), submit=FakeGitHub())
    status, _, _ = call(app, payload=submission())
    assert status == 200


def test_a_second_configured_origin_is_allowed():
    app = create_app(
        config(allowed_origins=("https://quillforall.org", "https://staging.example")),
        submit=FakeGitHub(),
    )
    status, headers, _ = call(app, payload=submission(), origin="https://staging.example")
    assert status == 200
    assert headers["Access-Control-Allow-Origin"] == "https://staging.example"


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------


def test_a_second_submission_from_one_address_is_refused():
    app = create_app(config(per_minute=1, per_day=20), submit=FakeGitHub())
    assert call(app, payload=submission())[0] == 200
    status, _, body = call(app, payload=submission())
    assert status == 429
    assert "try again" in body["error"]


def test_another_address_is_unaffected():
    app = create_app(config(per_minute=1, per_day=20), submit=FakeGitHub())
    assert call(app, payload=submission())[0] == 200
    status, _, _ = call(app, payload=submission(), headers={"HTTP_X_FORWARDED_FOR": "198.51.100.4"})
    assert status == 200


def test_a_refused_submission_does_not_count_against_the_limit():
    """Being over the minute limit must not push somebody over the day limit
    for retrying, and a malformed body must not consume their one attempt."""
    app = create_app(config(per_minute=1, per_day=20), submit=FakeGitHub())
    assert call(app, payload={"title": "T", "body": "no block"})[0] == 400
    assert call(app, payload=submission())[0] == 200


def test_rate_limiter_forgets_the_previous_day():
    limiter = RateLimiter(per_minute=1, per_day=2)
    assert limiter.allow("a", now=1000.0)
    assert not limiter.allow("a", now=1001.0)  # inside the minute
    assert limiter.allow("a", now=1000.0 + 120)  # minute has passed
    assert not limiter.allow("a", now=1000.0 + 240)  # day limit of 2 reached
    assert limiter.allow("a", now=1000.0 + 90_000)  # a day later, forgiven


def test_rate_limiter_does_not_leak_addresses():
    """Without the sweep the dictionary is one entry per address, forever."""
    limiter = RateLimiter(per_minute=1, per_day=20)
    limiter.allow("a", now=1000.0)
    limiter.allow("b", now=1000.0)
    limiter.allow("c", now=1000.0 + 90_000)
    assert set(limiter._hits) == {"c"}


def test_rate_limiter_can_be_switched_off():
    limiter = RateLimiter(per_minute=0, per_day=0)
    assert all(limiter.allow("a", now=1000.0 + n) for n in range(50))


# --------------------------------------------------------------------------
# client address
# --------------------------------------------------------------------------


def test_client_address_takes_the_last_forwarded_entry():
    """A client can send an X-Forwarded-For of its own and the proxy appends to
    it, so the first entry is whatever the client claimed. Reading the first
    would make the rate limit evadable by anyone who read the file."""
    environ = {"HTTP_X_FORWARDED_FOR": "1.1.1.1, 2.2.2.2, 198.51.100.9", "REMOTE_ADDR": "10.0.0.1"}
    assert client_address(environ, "X-Forwarded-For") == "198.51.100.9"


def test_client_address_falls_back_to_the_socket_peer():
    assert client_address({"REMOTE_ADDR": "10.0.0.1"}, "X-Forwarded-For") == "10.0.0.1"
    spoofed = {"HTTP_X_FORWARDED_FOR": "1.1.1.1", "REMOTE_ADDR": "10.0.0.1"}
    assert client_address(spoofed, "") == "10.0.0.1"


def test_client_address_is_never_empty():
    """An empty key would put every anonymous request in one bucket, which is a
    rate limit that locks out everybody the moment one person submits."""
    assert client_address({}, "X-Forwarded-For") == "unknown"


def test_a_spoofed_forwarded_header_cannot_evade_the_limit():
    app = create_app(config(per_minute=1, per_day=20), submit=FakeGitHub())
    chain = "HTTP_X_FORWARDED_FOR"
    assert call(app, payload=submission(), headers={chain: "1.2.3.4, 198.51.100.9"})[0] == 200
    # Same real peer, a different claimed first entry: still the same bucket.
    status, _, _ = call(app, payload=submission(), headers={chain: "9.9.9.9, 198.51.100.9"})
    assert status == 429


# --------------------------------------------------------------------------
# validation, called directly
# --------------------------------------------------------------------------


def test_validate_suggestion_accepts_a_good_one():
    validate_suggestion("[Pick] Station: Team FM", body_for())


def test_validate_suggestion_raises_rejected_with_readable_words():
    with pytest.raises(_Rejected) as caught:
        validate_suggestion("[Pick] Station: X", "nothing machine readable here")
    assert caught.value.status == 400
    assert "expected shape" in caught.value.message


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def test_config_defaults_point_at_the_picks_repo():
    settings = ServerConfig.from_env({})
    assert settings.repo == "Community-Access/quill"
    assert settings.label == "pick:suggestion"
    assert settings.path == "/submit/picks"
    assert settings.allowed_origins == ("https://quillforall.org",)
    assert settings.per_minute == 1
    assert settings.per_day == 20
    assert settings.turnstile_secret == ""


def test_config_reads_the_environment():
    settings = ServerConfig.from_env(
        {
            "FEEDBACK_HUB_GITHUB_TOKEN": "tok",
            "PICKS_REPO": "org/other",
            "PICKS_LABEL": "pick:new",
            "PICKS_PATH": "/x/y",
            "PICKS_ALLOWED_ORIGINS": "https://a.example, https://b.example",
            "PICKS_PER_MINUTE": "5",
            "PICKS_PER_DAY": "50",
            "TURNSTILE_SECRET": "s",
        }
    )
    assert settings.token == "tok"
    assert settings.repo == "org/other"
    assert settings.label == "pick:new"
    assert settings.path == "/x/y"
    assert settings.allowed_origins == ("https://a.example", "https://b.example")
    assert settings.per_minute == 5
    assert settings.per_day == 50
    assert settings.turnstile_secret == "s"


def test_config_survives_nonsense_numbers():
    """A typo in an env var should not take the endpoint down."""
    settings = ServerConfig.from_env({"PICKS_PER_MINUTE": "lots", "PICKS_PER_DAY": ""})
    assert settings.per_minute == 1
    assert settings.per_day == 20


def test_config_path_cannot_be_emptied():
    settings = ServerConfig.from_env({"PICKS_PATH": "   "})
    assert settings.path == "/submit/picks"


def test_a_configured_path_is_the_one_that_answers():
    app = create_app(config(path="/hooks/picks"), submit=FakeGitHub())
    assert call(app, path="/hooks/picks", payload=submission())[0] == 200
    assert call(app, path="/submit/picks", payload=submission())[0] == 404
