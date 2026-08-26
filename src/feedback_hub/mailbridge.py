"""The Postmark-to-Maildir bridge: how a support email reaches FreeScout.

**What this is for.** `support@community-access.org` is handled by FreeScout,
which expects to fetch mail over IMAP. Postmark, which receives the mail, has
no IMAP -- it posts inbound messages to an HTTPS webhook as JSON. This is the
adapter between those two facts.

    Postmark inbound  --HTTPS webhook-->  this bridge  --writes-->  Maildir
                                                                      |
                                            Dovecot on the private network
                                                                      |
                                              FreeScout, over IMAP, normally

**It is a delivery adapter, not a second help desk.** The alternative -- having
this create FreeScout tickets through its API -- was considered and rejected,
because FreeScout's mail ingestion already does a great deal that would then
have to be rebuilt here: duplicate `Message-ID` detection, `In-Reply-To` and
`References` matching, conversation threading, customer-versus-agent reply
detection, attachment parsing, auto-reply detection, bounce handling, and
reactivating a conversation when a customer replies. That logic is most of the
reason to run FreeScout at all. Feeding it a normal RFC-822 email through its
normal path lets it do the job it was written for.

So the rule that governs every line below:

    **The raw message is written out byte for byte. Nothing here parses it,
    rewrites a header, strips quoted text, or touches Message-ID,
    In-Reply-To or References.**

The payload is also treated as hostile throughout. `RawEmail` is a string a
stranger composed; nothing derived from it ever becomes a filename, a path, or
a shell argument.

**Idempotency is the requirement that shapes the design.** Postmark retries an
inbound webhook up to ten times over about ten and a half hours whenever it
does not get a 200. A bridge that is merely *usually* idempotent turns one
customer email into eleven tickets on the day the disk fills up. See
:func:`deliver` for how the crash windows are closed.

**Status codes carry meaning to Postmark and must be chosen deliberately:**

===========  ==============================================================
``200``      Delivered, or recognised as an already-delivered duplicate.
             Returned only after the message is durable on disk.
``401``      Bad or missing webhook credentials.
``403``      A recipient this bridge does not serve. Postmark documents that
             403 **stops retries**, which is exactly right here -- retrying
             cannot make an address we do not handle become one we do.
``413``      Larger than the configured cap.
``5xx``      Anything transient, *and* anything that a configuration change
             would fix -- a missing ``RawEmail``, an unwritable Maildir. A
             retry after the fix then succeeds, which is why these must never
             be 4xx.
===========  ==============================================================

Configured entirely from the environment, because credentials must never live
in a file inside an image:

=================================  ========================================
``MAILBRIDGE_WEBHOOK_USER``        HTTP Basic user Postmark presents.
``MAILBRIDGE_WEBHOOK_PASSWORD``    Its password. Both required; without them
                                   the bridge refuses every request rather
                                   than running open.
``MAILBRIDGE_RECIPIENTS``          Comma-separated allowlist. Default
                                   ``support@community-access.org``.
``MAILBRIDGE_MAILDIR``             Maildir root. Default ``/maildir``.
``MAILBRIDGE_STATE_DB``            SQLite audit and dedup store. Default
                                   ``/state/inbound.sqlite3``.
``MAILBRIDGE_PATH``                Path the webhook answers on. Default
                                   ``/postmark/inbound``.
``MAILBRIDGE_MAX_BYTES``           Request cap. Default 52428800 (50 MB).
``MAILBRIDGE_TRUSTED_IPS``         Optional comma-separated allowlist of
                                   Postmark source addresses; empty disables
                                   the check.
=================================  ========================================

Run it::

    gunicorn --bind 0.0.0.0:8096 feedback_hub.mailbridge:application

**Deliberately not rate limited.** Every other endpoint in this package is,
and this one must not be: Postmark's retry schedule is the mechanism that
protects mail from a transient failure, and a limiter is precisely the thing
that would convert a busy hour into silently lost support requests. The
protections here are the size cap, the credentials and the recipient
allowlist -- none of which punishes a legitimate retry.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import socket
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

#: Postmark's documented inbound ceiling is well under this; the cap exists so
#: that a malformed or hostile request cannot be streamed into memory
#: unbounded, not to express a policy about attachment sizes.
_DEFAULT_MAX_BYTES = 50 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_messages (
    dedup_key         TEXT PRIMARY KEY,
    postmark_message_id TEXT,
    raw_sha256        TEXT NOT NULL,
    recipient         TEXT,
    from_address      TEXT,
    subject_length    INTEGER,
    spam_status       TEXT,
    spam_score        TEXT,
    maildir_filename  TEXT NOT NULL,
    status            TEXT NOT NULL,
    received_at       REAL NOT NULL,
    delivered_at      REAL
);
CREATE INDEX IF NOT EXISTS inbound_received_at ON inbound_messages (received_at);
"""


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    """Everything the bridge needs, resolved once at start-up."""

    webhook_user: str = ""
    webhook_password: str = ""
    recipients: frozenset[str] = field(default_factory=lambda: frozenset({"support@community-access.org"}))
    maildir: Path = Path("/maildir")
    state_db: Path = Path("/state/inbound.sqlite3")
    path: str = "/postmark/inbound"
    max_bytes: int = _DEFAULT_MAX_BYTES
    trusted_ips: frozenset[str] = frozenset()
    client_ip_header: str = "X-Forwarded-For"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BridgeConfig":
        source = os.environ if env is None else env

        def listed(name: str, default: str = "") -> frozenset[str]:
            raw = source.get(name, default)
            return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())

        return cls(
            webhook_user=source.get("MAILBRIDGE_WEBHOOK_USER", "").strip(),
            webhook_password=source.get("MAILBRIDGE_WEBHOOK_PASSWORD", "").strip(),
            recipients=listed("MAILBRIDGE_RECIPIENTS", "support@community-access.org"),
            maildir=Path(source.get("MAILBRIDGE_MAILDIR", "/maildir")),
            state_db=Path(source.get("MAILBRIDGE_STATE_DB", "/state/inbound.sqlite3")),
            path=source.get("MAILBRIDGE_PATH", "/postmark/inbound").strip() or "/postmark/inbound",
            max_bytes=_int(source.get("MAILBRIDGE_MAX_BYTES"), _DEFAULT_MAX_BYTES),
            trusted_ips=listed("MAILBRIDGE_TRUSTED_IPS"),
            client_ip_header=source.get("MAILBRIDGE_CLIENT_IP_HEADER", "X-Forwarded-For").strip(),
        )


def _int(value: str | None, fallback: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return fallback


@dataclass
class _Refused(Exception):
    """A request turned away, with the status Postmark should see.

    ``retryable`` decides between a 4xx that stops Postmark and a 5xx that
    keeps it trying. Getting this backwards is how mail is lost: a 403 on a
    problem an administrator could have fixed means the message is gone before
    anyone notices, and a 500 on a permanently wrong recipient means ten
    pointless retries over half a day.
    """

    status: int
    message: str


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


class DeliveryLog:
    """The audit trail, and the authority on what has already been delivered.

    SQLite because it is in the standard library, it is a single file to back
    up, and its commit is genuinely durable -- which is the entire property
    being relied on. A dictionary in memory would forget every delivery on
    restart, and "restart" includes the crash that Postmark is retrying after.
    """

    __slots__ = ("_path", "_lock")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path, timeout=30)
        # WAL so a reader never blocks the writer; FULL synchronous because a
        # "delivered" row that a power cut can un-write would let a retry
        # deliver the same message twice, which is the one thing this table
        # exists to prevent.
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def lookup(self, dedup_key: str) -> tuple[str, str] | None:
        """``(status, maildir_filename)`` for *dedup_key*, or ``None``."""
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT status, maildir_filename FROM inbound_messages WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def begin(self, dedup_key: str, filename: str, facts: dict[str, Any]) -> None:
        """Record an attempt before the write, so a crash leaves a trace.

        ``INSERT OR REPLACE`` rather than ``INSERT``: a row already sitting at
        ``receiving`` is the fingerprint of an attempt that died mid-flight,
        and the right response to that is to try again, not to refuse.
        """
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO inbound_messages
                    (dedup_key, postmark_message_id, raw_sha256, recipient, from_address,
                     subject_length, spam_status, spam_score, maildir_filename, status,
                     received_at, delivered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'receiving', ?, NULL)
                """,
                (
                    dedup_key,
                    facts.get("postmark_message_id"),
                    facts["raw_sha256"],
                    facts.get("recipient"),
                    facts.get("from_address"),
                    facts.get("subject_length"),
                    facts.get("spam_status"),
                    facts.get("spam_score"),
                    filename,
                    time.time(),
                ),
            )
            db.commit()

    def complete(self, dedup_key: str) -> None:
        """Mark delivered. The commit is what makes the 200 honest."""
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE inbound_messages SET status = 'delivered', delivered_at = ? "
                "WHERE dedup_key = ?",
                (time.time(), dedup_key),
            )
            db.commit()

    def count(self, status: str | None = None) -> int:
        with self._lock, self._connect() as db:
            if status is None:
                row = db.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()
            else:
                row = db.execute(
                    "SELECT COUNT(*) FROM inbound_messages WHERE status = ?", (status,)
                ).fetchone()
        return int(row[0])


# ---------------------------------------------------------------------------
# maildir
# ---------------------------------------------------------------------------


def ensure_maildir(root: Path) -> None:
    """Create ``tmp``, ``new`` and ``cur`` if they are not already there."""
    for leaf in ("tmp", "new", "cur"):
        (root / leaf).mkdir(parents=True, exist_ok=True)


def maildir_filename(raw_sha256: str, *, host: str | None = None) -> str:
    """A Maildir name derived from the message, not from anything it contains.

    Deterministic on the message hash on purpose: a Postmark retry therefore
    produces the *same* filename, so re-delivering after a crash overwrites one
    file atomically instead of adding a second copy. That property is what
    makes the delivery safe even in the window where the database has not yet
    been updated.

    Nothing here is taken from the payload. A sender chooses their own subject,
    their own display name and their own attachment filenames, and none of them
    reaches the filesystem -- the name is a timestamp, a hex digest and the
    hostname. No colon, because Maildir gives ``:`` a meaning in ``cur``.
    """
    stamp = int(time.time())
    machine = (host or socket.gethostname() or "bridge").replace("/", "_").replace(":", "_")
    return f"{stamp}.{raw_sha256[:32]}.{machine}"


def deliver(root: Path, filename: str, raw: bytes) -> Path:
    """Write *raw* into the Maildir, atomically and durably.

    The sequence is the one Maildir specifies, with the two fsyncs that are
    usually left out:

    1. Write the whole message into ``tmp``.
    2. ``fsync`` the file, so its *contents* are on the disk.
    3. ``rename`` into ``new`` -- atomic within a filesystem, so a reader never
       sees a half-written message. This is FR-5, and it is why the message is
       not simply written into ``new`` directly.
    4. ``fsync`` the ``new`` *directory*, so the rename itself is on the disk.

    Step 4 is the one that gets forgotten. A rename is a directory operation,
    and syncing the file does not commit the directory entry that points at it
    -- so without it, a power cut can leave a message that was reported as
    delivered and is not there. Returning 200 to Postmark at that point retires
    the only other copy of the customer's email.
    """
    tmp_path = root / "tmp" / filename
    new_path = root / "new" / filename

    with open(tmp_path, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(tmp_path, new_path)

    if os.name == "posix":
        # POSIX only, and deliberately not wrapped in a try/except there: if
        # this fails on the deployment target, the delivery is not durable and
        # the caller must hear about it rather than return 200.
        #
        # Windows has no equivalent -- opening a directory as a file descriptor
        # is refused outright -- so the step is skipped on developer machines.
        # That is a genuine gap in the guarantee, and it is acceptable only
        # because the bridge runs in a Linux container; do not "fix" it by
        # catching the error on both platforms, which would silence the case
        # that matters.
        directory = os.open(root / "new", os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return new_path


# ---------------------------------------------------------------------------
# the payload
# ---------------------------------------------------------------------------


def raw_email_bytes(payload: dict[str, Any]) -> bytes:
    """The original message as bytes, unaltered. Raises :class:`_Refused`.

    ``RawEmail`` arrives as a JSON string, and JSON is Unicode while an email
    is bytes. A message carrying a non-UTF-8 byte -- an old mailer putting
    Latin-1 straight into a header, which is not rare in a support inbox --
    survives that trip only if both ends of it agree.

    They do: :func:`_read_json` decodes the request body with
    ``errors="surrogateescape"``, which parks each undecodable byte in a lone
    surrogate, and this encodes with the same handler, which is its exact
    inverse and puts the original byte back.

    The pairing is the whole point and is easy to get subtly wrong.
    ``surrogatepass`` looks like it belongs here and does not: it would encode
    U+DCE9 as the three UTF-8 bytes of that codepoint rather than restoring the
    single byte 0xE9. ``errors="replace"`` is worse still -- it silently
    corrupts the message, and with it any DKIM signature computed over the
    original bytes, so the damage surfaces as mail that mysteriously fails
    authentication rather than as an error anybody can trace back to here.

    A missing ``RawEmail`` is a **retryable** failure, not a rejection: it
    means the Postmark inbound server does not have "Include raw email content"
    switched on, and that is a setting somebody can fix -- after which the
    retry succeeds and the message is not lost. FR-3.
    """
    raw = payload.get("RawEmail")
    if not isinstance(raw, str) or not raw:
        raise _Refused(
            503,
            "RawEmail missing: enable raw email content on the Postmark inbound "
            "server, and this message will be retried into place",
        )
    return raw.encode("utf-8", "surrogateescape")


def recipients_of(payload: dict[str, Any]) -> set[str]:
    """Every address this message was addressed to, lowercased.

    Reads Postmark's parsed fields rather than the raw headers, and reads all
    of them -- ``To``, ``Cc`` and ``Bcc`` -- because a message that reaches the
    support address by being Cc'd is still a message for support. Restricting
    the check to ``To`` would silently drop them.

    ``OriginalRecipient`` matters most and is checked first: it is the envelope
    address Postmark actually delivered to, which is the truth about where this
    mail was sent. Header fields are what the sender typed.
    """
    found: set[str] = set()

    original = payload.get("OriginalRecipient")
    if isinstance(original, str) and original.strip():
        found.add(original.strip().lower())

    for key in ("ToFull", "CcFull", "BccFull"):
        entries = payload.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                address = entry.get("Email")
                if isinstance(address, str) and address.strip():
                    found.add(address.strip().lower())
    return found


def spam_facts(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Postmark's SpamAssassin verdict, recorded and *not* acted upon.

    Deliberately advisory. The plan's opening policy is to let Postmark do its
    analysis, log the score and let agents mark spam by hand, because a support
    inbox for accessibility work receives messages full of unusual markup,
    long quoted threads, screen-reader transcripts and assistive-technology
    jargon -- exactly the shapes a spam filter mistrusts. Discarding a borderline
    message here would lose a real request and leave no evidence it existed.
    """
    status = score = None
    headers = payload.get("Headers")
    if isinstance(headers, list):
        for header in headers:
            if not isinstance(header, dict):
                continue
            name = str(header.get("Name", "")).lower()
            value = header.get("Value")
            if name == "x-spam-status" and isinstance(value, str):
                status = value[:200]
            elif name == "x-spam-score" and isinstance(value, str):
                score = value[:64]
    return status, score


# ---------------------------------------------------------------------------
# the application
# ---------------------------------------------------------------------------


def check_credentials(header_value: str, config: BridgeConfig) -> None:
    """HTTP Basic, compared in constant time. Raises :class:`_Refused`.

    Postmark does not sign its inbound webhooks, so the credentials embedded in
    the webhook URL are the whole of the authentication. Two consequences are
    written into the code rather than a wiki:

    * ``hmac.compare_digest``, because a plain ``==`` on a secret leaks its
      length and prefix through timing, and this endpoint is reachable by
      anyone who guesses the path.
    * The bridge **refuses to run open**. If no credentials are configured,
      every request is rejected rather than accepted -- a webhook that quietly
      served the world because an environment variable was missing is a far
      worse failure than one that visibly does not work.
    """
    if not config.webhook_user or not config.webhook_password:
        raise _Refused(500, "webhook credentials are not configured")

    if not header_value.lower().startswith("basic "):
        raise _Refused(401, "authentication required")
    try:
        decoded = base64.b64decode(header_value[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise _Refused(401, "authentication required") from exc
    user, _, password = decoded.partition(":")

    # Both compared, and neither short-circuited, so a wrong username costs the
    # same time as a wrong password.
    user_ok = hmac.compare_digest(user, config.webhook_user)
    password_ok = hmac.compare_digest(password, config.webhook_password)
    if not (user_ok and password_ok):
        raise _Refused(401, "authentication required")


def client_address(environ: dict[str, Any], header: str) -> str:
    """The caller's address as seen through the reverse proxy.

    The **last** entry of the forwarded chain, not the first: a client can send
    an ``X-Forwarded-For`` of its own and the proxy appends to it, so the first
    entry is whatever the caller claimed. Reading the first would let anyone
    walk straight through an IP allowlist by asserting a Postmark address.
    """
    if header:
        key = "HTTP_" + header.upper().replace("-", "_")
        raw = str(environ.get(key, "")).strip()
        if raw:
            return raw.split(",")[-1].strip()
    return str(environ.get("REMOTE_ADDR", "")).strip()


def create_app(
    config: BridgeConfig | None = None,
    *,
    log: DeliveryLog | None = None,
) -> Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]:
    """Build the WSGI application."""
    settings = config or BridgeConfig.from_env()
    ensure_maildir(settings.maildir)
    store = log or DeliveryLog(settings.state_db)

    def application(environ, start_response):  # noqa: ANN001, ANN202
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = (environ.get("PATH_INFO", "") or "/").rstrip("/") or "/"

        if path in ("/health", "/healthz"):
            # Says nothing about credentials, recipients or any message. A
            # health endpoint is read by monitoring and by whoever finds the
            # URL, and those are the same reader as far as this is concerned.
            return _reply(start_response, 200, {"ok": True, "delivered": store.count("delivered")})

        if path != (settings.path.rstrip("/") or "/"):
            return _reply(start_response, 404, {"error": "no such endpoint"})
        if method != "POST":
            return _reply(start_response, 405, {"error": "POST only"})

        try:
            _authorise(environ, settings)
            payload = _read_json(environ, settings.max_bytes)
            result = _accept(payload, settings, store)
        except _Refused as refusal:
            if refusal.status >= 500:
                # Logged, because a 5xx means Postmark will come back and
                # somebody needs to know why before the retries run out.
                print(f"mailbridge: {refusal.status} {refusal.message}", file=sys.stderr, flush=True)
            return _reply(start_response, refusal.status, {"error": refusal.message})
        except Exception as error:  # noqa: BLE001 - an unexpected failure must still be retryable
            # Never let an unexpected exception become a 4xx. The default WSGI
            # behaviour on an unhandled error is a 500, which is right, but
            # going through here means it is logged in one recognisable shape.
            print(f"mailbridge: unhandled error: {error!r}", file=sys.stderr, flush=True)
            return _reply(start_response, 500, {"error": "internal error; please retry"})

        return _reply(start_response, 200, result)

    return application


def _authorise(environ: dict[str, Any], settings: BridgeConfig) -> None:
    check_credentials(str(environ.get("HTTP_AUTHORIZATION", "")), settings)
    if settings.trusted_ips:
        caller = client_address(environ, settings.client_ip_header).lower()
        if caller not in settings.trusted_ips:
            # 401 rather than 403: 403 stops Postmark retrying, and an address
            # that is not on the list today may be one Postmark starts using
            # tomorrow. Better that the mail waits than that it is discarded.
            raise _Refused(401, "authentication required")


def _accept(payload: dict[str, Any], settings: BridgeConfig, store: DeliveryLog) -> dict[str, Any]:
    """Validate, deduplicate and deliver. Returns the body for the 200."""
    addressed = recipients_of(payload)
    if not addressed:
        raise _Refused(503, "no recipient in the payload; retrying")
    if not (addressed & settings.recipients):
        # 403 on purpose, and it is the one place a 4xx is right: Postmark
        # documents that 403 stops retries, and no number of retries will make
        # an address this bridge does not serve into one it does.
        raise _Refused(403, "not a recipient this bridge serves")

    raw = raw_email_bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()

    # Postmark's own MessageID when there is one, because that is what a retry
    # of *this webhook* carries. The content hash is the fallback, and is also
    # stored either way so a duplicate can be recognised from either side.
    postmark_id = payload.get("MessageID")
    postmark_id = postmark_id.strip() if isinstance(postmark_id, str) else None
    dedup_key = f"pm:{postmark_id}" if postmark_id else f"sha:{digest}"

    existing = store.lookup(dedup_key)
    if existing and existing[0] == "delivered":
        # FR-4. The retry is answered 200 and nothing is written -- which is
        # the entire point, and is why 200 must never be returned early
        # anywhere else in this function.
        return {"ok": True, "status": "duplicate", "filename": existing[1]}

    # A row left at 'receiving' is a previous attempt that died. Reuse its
    # filename so the retry overwrites that attempt rather than adding to it.
    filename = existing[1] if existing else maildir_filename(digest)
    spam_status, spam_score = spam_facts(payload)
    sender = payload.get("From")
    subject = payload.get("Subject")

    store.begin(
        dedup_key,
        filename,
        {
            "postmark_message_id": postmark_id,
            "raw_sha256": digest,
            "recipient": sorted(addressed & settings.recipients)[0],
            "from_address": sender if isinstance(sender, str) else None,
            # The subject's LENGTH, not the subject. This table is an audit
            # trail an administrator reads; the message content belongs in the
            # help desk, where access is controlled, and not in a second store
            # that also has to be secured and backed up.
            "subject_length": len(subject) if isinstance(subject, str) else None,
            "spam_status": spam_status,
            "spam_score": spam_score,
        },
    )

    try:
        ensure_maildir(settings.maildir)
        deliver(settings.maildir, filename, raw)
    except OSError as error:
        # Disk full, permissions, a missing mount. All transient in the sense
        # that matters: somebody can fix them and the retry will land.
        raise _Refused(503, f"could not write to the mailbox: {error.strerror or error}") from error

    store.complete(dedup_key)
    print(
        f"mailbridge: delivered {filename} "
        f"(postmark_id={postmark_id or '-'} spam_status={spam_status or '-'})",
        flush=True,
    )
    return {"ok": True, "status": "delivered", "filename": filename}


def _read_json(environ: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """The request body as a JSON object. Raises :class:`_Refused`."""
    try:
        declared = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > max_bytes:
        raise _Refused(413, "that message is larger than this endpoint accepts")

    stream = environ.get("wsgi.input")
    raw = stream.read(declared if declared > 0 else max_bytes) if stream else b""
    if len(raw) > max_bytes:
        raise _Refused(413, "that message is larger than this endpoint accepts")
    try:
        # surrogateescape, not strict: a byte the body cannot express in UTF-8
        # is parked in a lone surrogate rather than raising, and
        # raw_email_bytes puts it back with the same handler. Decoding strictly
        # here would reject the whole message -- and a support email is not
        # something to discard because one header used Latin-1.
        payload = json.loads(raw.decode("utf-8", "surrogateescape"))
    except ValueError as exc:
        # 400, not 5xx: a body that is not JSON will not become JSON on the
        # eleventh attempt. This is the one malformed-input case where
        # retrying is genuinely pointless.
        raise _Refused(400, "that was not JSON") from exc
    if not isinstance(payload, dict):
        raise _Refused(400, "that was not a JSON object")
    return payload


def _reply(
    start_response: Callable[..., Any],
    status: int,
    value: dict[str, Any],
) -> Iterable[bytes]:
    body = json.dumps(value).encode("utf-8")
    reason = {
        200: "200 OK",
        400: "400 Bad Request",
        401: "401 Unauthorized",
        403: "403 Forbidden",
        404: "404 Not Found",
        405: "405 Method Not Allowed",
        413: "413 Payload Too Large",
        500: "500 Internal Server Error",
        503: "503 Service Unavailable",
    }.get(status, f"{status} Error")
    headers = [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ]
    if status == 401:
        headers.append(("WWW-Authenticate", 'Basic realm="postmark-inbound"'))
    start_response(reason, headers)
    return [body]


class _LazyApplication:
    """Built on first request, so importing this module binds nothing."""

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

    parser = argparse.ArgumentParser(description="Postmark-to-Maildir bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8096)
    args = parser.parse_args(argv)

    settings = BridgeConfig.from_env()
    if not settings.webhook_user or not settings.webhook_password:
        print(
            "No webhook credentials. Set MAILBRIDGE_WEBHOOK_USER and "
            "MAILBRIDGE_WEBHOOK_PASSWORD -- every request will be refused without them.",
            flush=True,
        )
    print(
        f"mailbridge on http://{args.host}:{args.port}{settings.path} "
        f"-> {settings.maildir} (recipients: {', '.join(sorted(settings.recipients))})",
        flush=True,
    )
    with make_server(args.host, args.port, create_app(settings)) as server:
        server.serve_forever()
    return 0


__all__ = [
    "BridgeConfig",
    "DeliveryLog",
    "application",
    "check_credentials",
    "client_address",
    "create_app",
    "deliver",
    "ensure_maildir",
    "main",
    "maildir_filename",
    "raw_email_bytes",
    "recipients_of",
    "spam_facts",
]


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
