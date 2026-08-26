"""The small server that lets somebody submit without a GitHub account.

**Why this exists.** Every other part of feedback-hub is a client: a wx dialog,
a Flask blueprint, a function call. Each one carries its own GitHub token, and
that is fine on a desktop where the worst case is issue spam in one repo. It is
not fine on a web page, because a page is readable by everyone -- a token in it
would be extracted, and GitHub's own secret scanning would revoke it within
minutes, rightly.

So a static site cannot file an issue for a visitor. It has to hand them to
GitHub's own new-issue form, and that final press needs an account. For
`https://quillforall.org/picks/suggest/` that account requirement is the whole
problem: the project exists so that **nobody should ever require a GitHub
account** to suggest a radio station.

This module is the smallest honest answer. One process, one endpoint, holding
the only token, in front of which any number of accountless clients can stand::

    quillforall.org/picks/suggest/  --POST-->  this server  --> GitHub issue

**Scope, deliberately.** Today it serves Community Picks suggestions and
nothing else. Report a Bug still submits directly from each app, because moving
that at the same time would make the first deployment also the riskiest one.
The shape here is the one those clients will move to later, once it has been up
a while: same process, more endpoints.

**No dependencies.** A plain WSGI callable on the standard library, so it runs
under gunicorn, waitress, mod_wsgi or the bundled development server, and
`feedback-hub` stays installable with nothing else. Run it behind a reverse
proxy that terminates TLS -- see ``deploy/`` in this repository.

Quick start::

    FEEDBACK_HUB_GITHUB_TOKEN=ghp_... python -m feedback_hub.server

    # or, in production
    gunicorn --bind 127.0.0.1:8095 feedback_hub.server:application

Everything is configured from the environment, because the token must never
live in a file inside an image:

===============================  =========================================
``FEEDBACK_HUB_GITHUB_TOKEN``    Fine-grained PAT, Issues: read/write on the
                                 target repo and nothing else. Required.
``PICKS_REPO``                   ``owner/name``. Default
                                 ``Community-Access/quill``.
``PICKS_LABEL``                  Label applied to every filed suggestion.
                                 Default ``pick:suggestion``.
``PICKS_PATH``                   Path the endpoint answers on. Default
                                 ``/submit/picks``.
``PICKS_ALLOWED_ORIGINS``        Comma-separated. Default
                                 ``https://quillforall.org``.
``PICKS_PER_MINUTE``             Per-IP burst limit. Default 1.
``PICKS_PER_DAY``                Per-IP daily limit. Default 20.
``PICKS_CLIENT_IP_HEADER``       Header carrying the client address behind the
                                 proxy. Default ``X-Forwarded-For``; set empty
                                 to use the socket peer.
``TURNSTILE_SECRET``             Optional. When set, a Turnstile token is
                                 required on every submission.
===============================  =========================================

**On spam control, once and permanently: Turnstile, never reCAPTCHA.**
Turnstile is usually invisible and needs no puzzle. reCAPTCHA's image grids are
precisely the barrier this project exists to remove -- a spam control that locks
out blind users to keep out bots has failed at the only job that matters here.
Written into the code rather than a wiki because it is the kind of decision that
gets made hastily at 2am.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib import parse as urlparse
from urllib import request as urlrequest

from feedback_hub._github import GitHubConfig, create_raw_issue, resolve_token

#: The fence that marks the machine-readable block inside a suggestion body.
#: It has to match ``quill.core.pick_suggestion`` exactly, because
#: ``picks-build.yml`` parses whatever arrives here with that module's reader --
#: the in-app dialog and the web form deliberately produce one format so the
#: pipeline has one shape to understand.
_PICK_BLOCK = "```json pick"
_PICK_BLOCK_RE = re.compile(r"```json pick\s*(.*?)\s*```", re.S)

#: The kinds a suggestion may claim. Mirrors ``pick_suggestion.SUGGESTABLE_TYPES``.
_SUGGESTABLE_TYPES = ("stream", "podcast")

#: Bounds. A title longer than this is a pasted paragraph, not a name, and a
#: body longer than this is not a radio station.
_MAX_TITLE = 200
_MAX_BODY = 8000
#: Read cap, applied before parsing: an unbounded read is how a small process
#: is turned into a memory exhaustion bug.
_MAX_REQUEST_BYTES = 32_768

_TURNSTILE_VERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_TURNSTILE_TIMEOUT = 10


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Everything the endpoint needs, resolved once at start-up."""

    token: str = ""
    repo: str = "Community-Access/quill"
    label: str = "pick:suggestion"
    path: str = "/submit/picks"
    allowed_origins: tuple[str, ...] = ("https://quillforall.org",)
    per_minute: int = 1
    per_day: int = 20
    client_ip_header: str = "X-Forwarded-For"
    turnstile_secret: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ServerConfig":
        source = os.environ if env is None else env
        origins = tuple(
            origin.strip()
            for origin in source.get("PICKS_ALLOWED_ORIGINS", "https://quillforall.org").split(",")
            if origin.strip()
        )
        return cls(
            token=resolve_token(source.get("FEEDBACK_HUB_GITHUB_TOKEN", "")),
            repo=source.get("PICKS_REPO", "Community-Access/quill").strip(),
            label=source.get("PICKS_LABEL", "pick:suggestion").strip(),
            path=source.get("PICKS_PATH", "/submit/picks").strip() or "/submit/picks",
            allowed_origins=origins,
            per_minute=_int(source.get("PICKS_PER_MINUTE"), 1),
            per_day=_int(source.get("PICKS_PER_DAY"), 20),
            client_ip_header=source.get("PICKS_CLIENT_IP_HEADER", "X-Forwarded-For").strip(),
            turnstile_secret=source.get("TURNSTILE_SECRET", "").strip(),
        )


def _int(value: str | None, fallback: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return fallback


class RateLimiter:
    """Per-address counters, in memory, with no storage to administer.

    One suggestion a minute and twenty a day means a bad afternoon costs a
    handful of closed issues rather than a repo full of them.

    In memory on purpose. The alternative is a database or a KV store to run,
    back up and reason about, for counters whose entire value expires within a
    day; restarting the process forgives everybody, which is the right failure
    for a limit whose job is to blunt bursts rather than to punish anyone. If
    this is ever run as more than one process, the limit becomes per-process --
    say so in the deployment rather than pretending otherwise.
    """

    __slots__ = ("_per_minute", "_per_day", "_hits", "_lock")

    def __init__(self, per_minute: int, per_day: int) -> None:
        self._per_minute = per_minute
        self._per_day = per_day
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Record an attempt from *key*; ``False`` when it is over a limit.

        A refused attempt is **not** recorded, so somebody who is over the
        minute limit is not thereby pushed over the day limit for retrying.
        """
        moment = time.time() if now is None else now
        if self._per_minute <= 0 and self._per_day <= 0:
            return True
        with self._lock:
            self._forget_old(moment)
            stamps = self._hits.setdefault(key, [])
            recent = sum(1 for stamp in stamps if stamp > moment - 60)
            if self._per_minute > 0 and recent >= self._per_minute:
                return False
            if self._per_day > 0 and len(stamps) >= self._per_day:
                return False
            stamps.append(moment)
            return True

    def _forget_old(self, moment: float) -> None:
        """Drop everything older than a day, addresses included.

        Without this the dictionary is a slow leak: one entry per address that
        ever submitted, kept for the life of the process.
        """
        cutoff = moment - 86_400
        for key in list(self._hits):
            kept = [stamp for stamp in self._hits[key] if stamp > cutoff]
            if kept:
                self._hits[key] = kept
            else:
                del self._hits[key]


@dataclass
class _Rejected(Exception):
    """A submission turned away, with the words the visitor will read."""

    status: int
    message: str


def validate_suggestion(title: str, body: str) -> None:
    """Refuse anything the pipeline could not act on. Raises :class:`_Rejected`.

    The important refusal is the missing ``json pick`` block. An issue without
    one looks fine in the review queue and publishes **nothing** when approved,
    so the failure would surface days later as "why is my station not in the
    list?". Turning it away at the door keeps the queue honest.

    Everything checked here is checked in the browser first. This is not
    redundancy for its own sake: the browser check is a courtesy to somebody
    typing, and this one is the rule, because anything at all can POST here.
    """
    if not title or not body:
        raise _Rejected(400, "a title and a body are required")
    if len(title) > _MAX_TITLE or len(body) > _MAX_BODY:
        raise _Rejected(400, "that submission is longer than this form accepts")
    if _PICK_BLOCK not in body:
        raise _Rejected(400, "that submission is not in the expected shape")

    blocks = _PICK_BLOCK_RE.findall(body)
    if len(blocks) != 1:
        # Exactly one, as ``pick_suggestion.parse_issue_body`` requires: two
        # blocks is ambiguity a person should resolve, not a machine.
        raise _Rejected(400, "that submission is not in the expected shape")
    try:
        payload = json.loads(blocks[0])
    except ValueError as exc:
        raise _Rejected(400, "that submission is not in the expected shape") from exc
    if not isinstance(payload, dict):
        raise _Rejected(400, "that submission is not in the expected shape")

    kind = str(payload.get("type", "")).strip()
    if kind not in _SUGGESTABLE_TYPES:
        raise _Rejected(400, "say whether this is a radio station or a podcast")
    if not str(payload.get("title", "")).strip():
        raise _Rejected(400, "a name is required")

    url = str(payload.get("feed_url") or payload.get("stream_url") or "").strip()
    if not url:
        raise _Rejected(400, "an address is required")
    if not url.lower().startswith(("https://", "http://")):
        # http is accepted on purpose: 41% of the most-played stations in the
        # directory Quill Radio already browses are http-only, among them the
        # small community stations this catalogue exists for. What is refused
        # is a scheme that is not the web -- javascript:, file:, data: -- which
        # is also how an attacker would try to get script onto the review page
        # that displays these suggestions.
        raise _Rejected(400, "the address should start with https:// or http://")
    if " " in url:
        raise _Rejected(400, "the address has a space in it")


def _verify_turnstile(token: str, secret: str, remote_ip: str) -> bool:
    """Ask Cloudflare whether the challenge token is good. Failure means no."""
    if not token:
        return False
    form = urlparse.urlencode(
        {"secret": secret, "response": token, "remoteip": remote_ip}
    ).encode("utf-8")
    request = urlrequest.Request(_TURNSTILE_VERIFY, data=form, method="POST")
    try:
        with urlrequest.urlopen(request, timeout=_TURNSTILE_TIMEOUT) as response:
            outcome = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - an unverifiable token is an unverified one
        return False
    return bool(isinstance(outcome, dict) and outcome.get("success"))


def client_address(environ: dict[str, Any], header: str) -> str:
    """The submitter's address, as seen through the reverse proxy.

    Takes the **last** entry of the forwarded chain, not the first. A client
    may send an ``X-Forwarded-For`` of its own and the proxy appends to it, so
    the first entry is whatever the client claimed and the last is the peer the
    proxy actually saw. Reading the first would make the rate limit trivially
    evadable by anyone who read this file.
    """
    if header:
        key = "HTTP_" + header.upper().replace("-", "_")
        raw = str(environ.get(key, "")).strip()
        if raw:
            return raw.split(",")[-1].strip()
    return str(environ.get("REMOTE_ADDR", "")).strip() or "unknown"


def create_app(
    config: ServerConfig | None = None,
    *,
    submit: Callable[..., tuple[Any, Any, Any]] | None = None,
) -> Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]:
    """Build the WSGI application.

    *submit* exists for tests and for anyone wiring a different backend; it
    defaults to :func:`feedback_hub._github.create_raw_issue`.
    """
    settings = config or ServerConfig.from_env()
    limiter = RateLimiter(settings.per_minute, settings.per_day)
    file_issue = submit or create_raw_issue

    def application(environ, start_response):  # noqa: ANN001, ANN202
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "") or "/"
        origin = str(environ.get("HTTP_ORIGIN", "")).strip()
        headers = _cors_headers(origin, settings.allowed_origins)

        if path.rstrip("/") in ("/healthz", "/submit/healthz"):
            # Deliberately says nothing about the token: a health check is read
            # by monitoring, and by anyone who finds the URL.
            return _reply(start_response, 200, {"ok": True}, headers)
        if path.rstrip("/") != settings.path.rstrip("/"):
            return _reply(start_response, 404, {"error": "no such endpoint"}, headers)
        if method == "OPTIONS":
            return _reply(start_response, 204, None, headers)
        if method != "POST":
            return _reply(start_response, 405, {"error": "POST only"}, headers)
        if origin and origin not in settings.allowed_origins:
            # A browser would have refused the response anyway; refusing the
            # request as well means an unwanted origin never files an issue.
            return _reply(start_response, 403, {"error": "not allowed from there"}, headers)

        try:
            payload = _read_json(environ)
            title = str(payload.get("title", "")).strip()
            body = str(payload.get("body", "")).strip()
            validate_suggestion(title, body)

            if settings.turnstile_secret and not _verify_turnstile(
                str(payload.get("turnstile", "")),
                settings.turnstile_secret,
                client_address(environ, settings.client_ip_header),
            ):
                raise _Rejected(400, "could not verify that you are a person")

            address = client_address(environ, settings.client_ip_header)
            if not limiter.allow(address):
                raise _Rejected(429, "too many suggestions just now; try again shortly")
        except _Rejected as rejection:
            return _reply(start_response, rejection.status, {"error": rejection.message}, headers)

        number, url, error = file_issue(
            title=title,
            body=body,
            labels=[settings.label],
            config=GitHubConfig(token=settings.token, repo=settings.repo, labels=[settings.label]),
        )
        if error:
            # The visitor is told the truth without being shown GitHub's error:
            # it can carry rate-limit details and token hints, and there is
            # nothing they could do with either.
            print(f"feedback-hub: GitHub refused a picks submission: {error}", flush=True)
            return _reply(
                start_response, 502, {"error": "GitHub would not accept it just now"}, headers
            )
        return _reply(start_response, 200, {"ok": True, "number": number, "url": url}, headers)

    return application


def _cors_headers(origin: str, allowed: tuple[str, ...]) -> list[tuple[str, str]]:
    """CORS for exactly the origins configured, and no wildcard ever.

    The allowed origin is echoed rather than starred so that adding a second
    site is a configuration change somebody made on purpose.
    """
    headers = [
        ("Access-Control-Allow-Methods", "POST, OPTIONS"),
        ("Access-Control-Allow-Headers", "Content-Type"),
        ("Access-Control-Max-Age", "86400"),
        ("Vary", "Origin"),
    ]
    if origin and origin in allowed:
        headers.append(("Access-Control-Allow-Origin", origin))
    elif allowed:
        headers.append(("Access-Control-Allow-Origin", allowed[0]))
    return headers


def _read_json(environ: dict[str, Any]) -> dict[str, Any]:
    """The request body as a JSON object. Raises :class:`_Rejected`."""
    try:
        declared = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > _MAX_REQUEST_BYTES:
        raise _Rejected(413, "that submission is longer than this form accepts")
    stream = environ.get("wsgi.input")
    raw = stream.read(declared if declared > 0 else _MAX_REQUEST_BYTES) if stream else b""
    if len(raw) > _MAX_REQUEST_BYTES:
        raise _Rejected(413, "that submission is longer than this form accepts")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _Rejected(400, "that was not JSON") from exc
    if not isinstance(payload, dict):
        raise _Rejected(400, "that was not JSON")
    return payload


def _reply(
    start_response: Callable[..., Any],
    status: int,
    value: dict[str, Any] | None,
    headers: list[tuple[str, str]],
) -> Iterable[bytes]:
    body = b"" if value is None else json.dumps(value).encode("utf-8")
    reason = {
        200: "200 OK",
        204: "204 No Content",
        400: "400 Bad Request",
        403: "403 Forbidden",
        404: "404 Not Found",
        405: "405 Method Not Allowed",
        413: "413 Payload Too Large",
        429: "429 Too Many Requests",
        502: "502 Bad Gateway",
    }.get(status, f"{status} Error")
    sent = list(headers)
    sent.append(("Content-Type", "application/json; charset=utf-8"))
    sent.append(("Content-Length", str(len(body))))
    # Nothing here is cacheable, and a cached "thank you" would be a lie.
    sent.append(("Cache-Control", "no-store"))
    start_response(reason, sent)
    return [body]


#: The module-level callable a WSGI server is pointed at, e.g.
#: ``gunicorn feedback_hub.server:application``. Built lazily so importing this
#: module (for the tests, or for ``ServerConfig``) does not read the
#: environment or bind anything.
class _LazyApplication:
    __slots__ = ("_app", "_lock")

    def __init__(self) -> None:
        self._app: Callable[..., Iterable[bytes]] | None = None
        self._lock = threading.Lock()

    def __call__(self, environ, start_response):  # noqa: ANN001, ANN204
        if self._app is None:
            with self._lock:
                if self._app is None:
                    self._app = create_app()
        return self._app(environ, start_response)


application = _LazyApplication()


def main(argv: list[str] | None = None) -> int:
    """Run the development server. Not for production -- put a proxy in front."""
    import argparse
    from wsgiref.simple_server import make_server

    parser = argparse.ArgumentParser(description="feedback-hub submission server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8095)
    args = parser.parse_args(argv)

    settings = ServerConfig.from_env()
    if not settings.token:
        print(
            "No GitHub token. Set FEEDBACK_HUB_GITHUB_TOKEN -- every submission "
            "will be refused without it.",
            flush=True,
        )
    print(
        f"feedback-hub server on http://{args.host}:{args.port}{settings.path} "
        f"-> {settings.repo} ({settings.label})",
        flush=True,
    )
    with make_server(args.host, args.port, create_app(settings)) as server:
        server.serve_forever()
    return 0


__all__ = [
    "RateLimiter",
    "ServerConfig",
    "application",
    "client_address",
    "create_app",
    "main",
    "validate_suggestion",
]


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
