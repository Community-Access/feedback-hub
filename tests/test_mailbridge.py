"""Tests for the Postmark-to-Maildir bridge.

Two properties carry the whole design and both are tested against the real WSGI
callable, a real SQLite file and a real directory, because both are about what
survives a crash and neither can be checked with a mock:

* **Idempotency.** Postmark retries an inbound webhook up to ten times over
  about ten and a half hours whenever it does not receive a 200. A bridge that
  is merely usually idempotent turns one customer email into eleven tickets on
  the day the disk fills up.

* **Status codes.** They are instructions to Postmark, not decoration. A 403
  stops retrying; a 5xx keeps it. Choosing the wrong one loses mail quietly --
  which is the only way support mail ever gets lost.

The third rule, that the raw message is written out byte for byte, is checked
by comparing bytes rather than by trusting the absence of parsing code.
"""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path

import pytest

from feedback_hub.mailbridge import (
    BridgeConfig,
    DeliveryLog,
    _Refused,
    check_credentials,
    client_address,
    create_app,
    deliver,
    ensure_maildir,
    maildir_filename,
    raw_email_bytes,
    recipients_of,
    spam_facts,
)

USER = "postmark-hook"
PASSWORD = "a-long-random-local-credential"

RAW = (
    "Return-Path: <listener@example.org>\r\n"
    "Message-ID: <abc123@example.org>\r\n"
    "In-Reply-To: <earlier@example.org>\r\n"
    "References: <first@example.org> <earlier@example.org>\r\n"
    "From: A Listener <listener@example.org>\r\n"
    "To: support@community-access.org\r\n"
    "Subject: Quill Radio will not read the station list\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "NVDA goes quiet when I press Browse.\r\n"
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def config(tmp_path: Path, **overrides) -> BridgeConfig:
    settings = {
        "webhook_user": USER,
        "webhook_password": PASSWORD,
        "recipients": frozenset({"support@community-access.org"}),
        "maildir": tmp_path / "maildir",
        "state_db": tmp_path / "state" / "inbound.sqlite3",
    }
    settings.update(overrides)
    return BridgeConfig(**settings)


def payload(**overrides) -> dict:
    body = {
        "MessageID": "0f6e1a2b-3c4d-5e6f-7a8b-9c0d1e2f3a4b",
        "OriginalRecipient": "support@community-access.org",
        "From": "listener@example.org",
        "Subject": "Quill Radio will not read the station list",
        "ToFull": [{"Email": "support@community-access.org", "Name": "Support"}],
        "RawEmail": RAW,
        "Headers": [
            {"Name": "X-Spam-Status", "Value": "No, score=-1.2"},
            {"Name": "X-Spam-Score", "Value": "-1.2"},
        ],
    }
    body.update(overrides)
    return body


def auth(user: str = USER, password: str = PASSWORD) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def call(app, body=None, *, method="POST", path="/postmark/inbound", header=None, raw=None,
         extra=None, content_length=None):
    if raw is None:
        raw = b"" if body is None else json.dumps(body).encode("utf-8")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "REMOTE_ADDR": "50.31.156.6",
        "CONTENT_LENGTH": str(len(raw) if content_length is None else content_length),
        "wsgi.input": io.BytesIO(raw),
    }
    if header is not None:
        environ["HTTP_AUTHORIZATION"] = header
    environ.update(extra or {})

    captured: dict = {}

    def start_response(status, headers):  # noqa: ANN001, ANN202
        captured["status"] = status
        captured["headers"] = headers

    payload_bytes = b"".join(app(environ, start_response))
    parsed = json.loads(payload_bytes) if payload_bytes else None
    return int(captured["status"].split()[0]), dict(captured["headers"]), parsed


def messages_in(root: Path) -> list[Path]:
    return sorted((root / "new").iterdir())


# --------------------------------------------------------------------------
# the happy path, and the rule that governs everything
# --------------------------------------------------------------------------


def test_the_raw_message_is_written_byte_for_byte(tmp_path):
    """FR-6 and FR-7 together, and the reason this bridge exists.

    FreeScout's threading reads Message-ID, In-Reply-To and References, and its
    duplicate detection reads them too. If the bridge rewrote so much as a line
    ending, replies would start new conversations instead of joining them --
    which looks like FreeScout being bad at threading rather than like a bug
    here.
    """
    app = create_app(config(tmp_path))
    status, _, body = call(app, payload(), header=auth())

    assert status == 200
    assert body["status"] == "delivered"

    written = messages_in(config(tmp_path).maildir)
    assert len(written) == 1
    assert written[0].read_bytes() == RAW.encode("utf-8")
    assert b"In-Reply-To: <earlier@example.org>" in written[0].read_bytes()


def test_nothing_is_left_in_tmp(tmp_path):
    """A message stranded in tmp is one FreeScout will never fetch."""
    settings = config(tmp_path)
    app = create_app(settings)
    call(app, payload(), header=auth())

    assert list((settings.maildir / "tmp").iterdir()) == []
    assert len(messages_in(settings.maildir)) == 1


def test_the_filename_comes_from_the_hash_and_never_from_the_message(tmp_path):
    """Sender-chosen text must never reach the filesystem.

    A subject of "../../etc/cron.d/evil" is a thing a stranger can send.
    """
    app = create_app(config(tmp_path))
    hostile = payload(
        Subject="../../../etc/cron.d/evil",
        From='"../../x" <x@example.org>',
    )
    status, _, body = call(app, hostile, header=auth())

    assert status == 200
    assert "/" not in body["filename"]
    assert ".." not in body["filename"]
    assert ":" not in body["filename"]  # Maildir gives ':' a meaning in cur/
    assert len(messages_in(config(tmp_path).maildir)) == 1


def test_health_says_nothing_about_secrets_or_messages(tmp_path):
    app = create_app(config(tmp_path))
    status, _, body = call(app, method="GET", path="/health", header=None)

    assert status == 200
    assert body["ok"] is True
    assert "password" not in json.dumps(body).lower()
    assert "support@" not in json.dumps(body)


# --------------------------------------------------------------------------
# idempotency -- FR-4
# --------------------------------------------------------------------------


def test_a_retry_delivers_once(tmp_path):
    """The requirement that shapes the design."""
    settings = config(tmp_path)
    app = create_app(settings)

    first = call(app, payload(), header=auth())
    assert first[0] == 200 and first[2]["status"] == "delivered"

    for _ in range(10):  # Postmark's documented retry count
        again = call(app, payload(), header=auth())
        assert again[0] == 200
        assert again[2]["status"] == "duplicate"

    assert len(messages_in(settings.maildir)) == 1


def test_a_retry_after_a_crash_between_write_and_record_does_not_duplicate(tmp_path):
    """The window the deterministic filename exists to close.

    Simulates the ugliest ordering: the message reached the Maildir and the
    process died before the database was updated. Postmark retries. Without a
    filename derived from the message, the retry would write a second copy and
    the customer's one email would become two conversations.
    """
    settings = config(tmp_path)
    store = DeliveryLog(settings.state_db)
    app = create_app(settings, log=store)

    # First attempt: crash immediately after the durable write.
    class CrashAfterWrite(DeliveryLog):
        def complete(self, dedup_key):  # noqa: ANN001, ANN202
            raise RuntimeError("power cut")

    crashing = CrashAfterWrite(settings.state_db)
    crashed_app = create_app(settings, log=crashing)
    status, _, _ = call(crashed_app, payload(), header=auth())
    assert status == 500  # Postmark is told to retry
    assert len(messages_in(settings.maildir)) == 1

    # The retry, against a healthy process.
    status, _, body = call(app, payload(), header=auth())
    assert status == 200
    assert len(messages_in(settings.maildir)) == 1, "the retry wrote a second copy"


def test_a_different_message_is_not_treated_as_a_duplicate(tmp_path):
    settings = config(tmp_path)
    app = create_app(settings)

    call(app, payload(), header=auth())
    other = payload(MessageID="ffffffff-0000-0000-0000-000000000000",
                    RawEmail=RAW.replace("abc123", "def456"))
    status, _, body = call(app, other, header=auth())

    assert status == 200 and body["status"] == "delivered"
    assert len(messages_in(settings.maildir)) == 2


def test_dedup_falls_back_to_the_content_hash(tmp_path):
    """Postmark always sends a MessageID, but a payload without one must still
    be safe rather than delivered on every retry."""
    settings = config(tmp_path)
    app = create_app(settings)
    without_id = payload()
    del without_id["MessageID"]

    assert call(app, without_id, header=auth())[2]["status"] == "delivered"
    assert call(app, without_id, header=auth())[2]["status"] == "duplicate"
    assert len(messages_in(settings.maildir)) == 1


# --------------------------------------------------------------------------
# status codes are instructions to Postmark
# --------------------------------------------------------------------------


def test_a_recipient_we_do_not_serve_stops_the_retries(tmp_path):
    """403 on purpose: Postmark documents that it stops retrying, and no
    number of retries makes an address we do not handle into one we do."""
    settings = config(tmp_path)
    app = create_app(settings)
    stranger = payload(
        OriginalRecipient="sales@community-access.org",
        ToFull=[{"Email": "sales@community-access.org"}],
    )
    status, _, _ = call(app, stranger, header=auth())

    assert status == 403
    assert messages_in(settings.maildir) == []


def test_a_missing_raw_email_is_retryable_not_rejected(tmp_path):
    """FR-3. It means "Include raw email content" is off on the Postmark
    inbound server -- a setting somebody can fix, after which the retry lands.
    Returning 4xx here would discard the message before anyone noticed."""
    settings = config(tmp_path)
    app = create_app(settings)
    without_raw = payload()
    del without_raw["RawEmail"]

    status, _, body = call(app, without_raw, header=auth())

    assert 500 <= status < 600
    assert "raw email" in body["error"].lower()
    assert messages_in(settings.maildir) == []


def test_an_unwritable_mailbox_is_retryable(tmp_path):
    """Disk full, a bad mount, wrong permissions. All fixable, so all 5xx."""
    settings = config(tmp_path)
    app = create_app(settings)
    ensure_maildir(settings.maildir)

    class Unwritable:
        def __truediv__(self, other):  # noqa: ANN001, ANN204
            raise OSError(28, "No space left on device")

    status, _, body = call(
        create_app(config(tmp_path, maildir=tmp_path / "maildir")),
        payload(),
        header=auth(),
    )
    assert status == 200  # sanity: the real path still works

    # Now make the write fail for real.
    settings2 = config(tmp_path / "second")
    app2 = create_app(settings2)
    ensure_maildir(settings2.maildir)
    (settings2.maildir / "new").rmdir()
    (settings2.maildir / "new").write_text("not a directory", encoding="utf-8")

    status, _, body = call(app2, payload(), header=auth())
    assert 500 <= status < 600


def test_a_body_that_is_not_json_is_refused_outright(tmp_path):
    """The one malformed-input case where retrying is genuinely pointless."""
    app = create_app(config(tmp_path))
    status, _, _ = call(app, raw=b"MessageID=1&RawEmail=hello", header=auth())
    assert status == 400


def test_an_oversized_request_is_refused_before_it_is_read(tmp_path):
    app = create_app(config(tmp_path, max_bytes=1024))
    status, _, _ = call(app, payload(), header=auth(), content_length=10_000_000)
    assert status == 413


def test_wrong_method_and_wrong_path(tmp_path):
    app = create_app(config(tmp_path))
    assert call(app, payload(), method="GET", header=auth())[0] == 405
    assert call(app, payload(), path="/somewhere-else", header=auth())[0] == 404


# --------------------------------------------------------------------------
# authentication -- FR-1
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer something", "Basic not-base64!!", auth("wrong", PASSWORD),
     auth(USER, "wrong"), auth("", "")],
)
def test_bad_credentials_are_refused(tmp_path, header):
    settings = config(tmp_path)
    app = create_app(settings)
    status, headers, _ = call(app, payload(), header=header)

    assert status == 401
    assert headers.get("WWW-Authenticate", "").startswith("Basic")
    assert messages_in(settings.maildir) == []


def test_the_bridge_refuses_to_run_open(tmp_path):
    """No credentials configured must mean "reject everything", never
    "accept everything". A webhook that quietly served the world because an
    environment variable was missing is the worse of the two failures."""
    settings = config(tmp_path, webhook_user="", webhook_password="")
    app = create_app(settings)
    status, _, _ = call(app, payload(), header=auth())

    assert status == 500
    assert messages_in(settings.maildir) == []


def test_check_credentials_rejects_a_prefix(tmp_path):
    settings = config(tmp_path)
    with pytest.raises(_Refused):
        check_credentials(auth(USER, PASSWORD[:-1]), settings)
    with pytest.raises(_Refused):
        check_credentials(auth(USER, PASSWORD + "x"), settings)
    check_credentials(auth(), settings)  # the right one does not raise


def test_an_ip_allowlist_refuses_with_401_not_403(tmp_path):
    """401 keeps Postmark retrying. An address that is not on the list today
    may be one Postmark starts using tomorrow, and mail waiting is better than
    mail discarded."""
    settings = config(tmp_path, trusted_ips=frozenset({"50.31.156.6"}))
    app = create_app(settings)

    allowed = call(app, payload(), header=auth(),
                   extra={"HTTP_X_FORWARDED_FOR": "1.2.3.4, 50.31.156.6"})
    assert allowed[0] == 200

    blocked = call(app, payload(MessageID="other"), header=auth(),
                   extra={"HTTP_X_FORWARDED_FOR": "50.31.156.6, 9.9.9.9"})
    assert blocked[0] == 401


def test_client_address_reads_the_last_forwarded_entry():
    """A caller can send an X-Forwarded-For of its own and the proxy appends to
    it, so the first entry is whatever the caller claimed. Reading the first
    would let anyone walk through the allowlist by asserting a Postmark
    address."""
    environ = {"HTTP_X_FORWARDED_FOR": "50.31.156.6, 203.0.113.9", "REMOTE_ADDR": "10.0.0.1"}
    assert client_address(environ, "X-Forwarded-For") == "203.0.113.9"
    assert client_address({"REMOTE_ADDR": "10.0.0.1"}, "X-Forwarded-For") == "10.0.0.1"


# --------------------------------------------------------------------------
# the payload
# --------------------------------------------------------------------------


def test_a_message_that_is_only_cc_to_support_is_accepted(tmp_path):
    """Being Cc'd is still being written to. Checking only ToFull would drop
    them silently, which is the worst way to fail."""
    settings = config(tmp_path)
    app = create_app(settings)
    cced = payload(
        OriginalRecipient="support@community-access.org",
        ToFull=[{"Email": "someone@example.org"}],
        CcFull=[{"Email": "support@community-access.org"}],
    )
    assert call(app, cced, header=auth())[0] == 200


def test_non_utf8_bytes_in_the_message_survive(tmp_path):
    """An old mailer using Latin-1 in a header is not rare in a support inbox.

    json.loads hands such a byte back as a lone surrogate; encoding with plain
    UTF-8 raises and errors="replace" would corrupt the message and any
    signature over it. surrogatepass restores the original bytes exactly.
    """
    latin1 = b"Subject: caf\xe9\r\nFrom: x@example.org\r\n\r\nbody\r\n"
    as_json = json.dumps({"RawEmail": latin1.decode("utf-8", "surrogateescape")})
    recovered = raw_email_bytes(json.loads(as_json))
    assert recovered == latin1


def test_recipients_prefers_the_envelope_and_reads_every_header():
    found = recipients_of(
        {
            "OriginalRecipient": "Support@Community-Access.ORG",
            "ToFull": [{"Email": "Someone@Example.org"}],
            "CcFull": [{"Email": "other@example.org"}],
            "BccFull": [{"Email": "hidden@example.org"}],
        }
    )
    assert "support@community-access.org" in found
    assert "hidden@example.org" in found
    assert all(address == address.lower() for address in found)


def test_spam_verdict_is_recorded_and_not_acted_on(tmp_path):
    """Opening policy: log the score, let agents mark spam by hand. A support
    inbox for accessibility work is full of long quoted threads, unusual markup
    and assistive-technology jargon -- the shapes a filter mistrusts."""
    settings = config(tmp_path)
    app = create_app(settings)
    spammy = payload(
        Headers=[
            {"Name": "X-Spam-Status", "Value": "Yes, score=14.2 required=5.0"},
            {"Name": "X-Spam-Score", "Value": "14.2"},
        ]
    )
    status, _, _ = call(app, spammy, header=auth())

    assert status == 200, "a high spam score must not discard a support request"
    assert len(messages_in(settings.maildir)) == 1

    status_text, score = spam_facts(spammy)
    assert status_text.startswith("Yes")
    assert score == "14.2"


def test_the_audit_row_stores_the_subject_length_not_the_subject(tmp_path):
    """This table is read by an administrator; message content belongs in the
    help desk, where access is controlled -- not in a second store that would
    also have to be secured and backed up."""
    import sqlite3

    settings = config(tmp_path)
    app = create_app(settings)
    call(app, payload(), header=auth())

    db = sqlite3.connect(settings.state_db)
    row = db.execute(
        "SELECT postmark_message_id, maildir_filename, status, subject_length, spam_score "
        "FROM inbound_messages"
    ).fetchone()
    db.close()

    assert row[0] == "0f6e1a2b-3c4d-5e6f-7a8b-9c0d1e2f3a4b"  # FR-8
    assert row[1] and row[2] == "delivered"
    assert row[3] == len(payload()["Subject"])
    assert row[4] == "-1.2"

    dumped = json.dumps(row)
    assert "station list" not in dumped
    assert "NVDA goes quiet" not in dumped


# --------------------------------------------------------------------------
# maildir mechanics
# --------------------------------------------------------------------------


def test_deliver_is_atomic_and_leaves_tmp_clean(tmp_path):
    root = tmp_path / "maildir"
    ensure_maildir(root)
    written = deliver(root, "1700000000.deadbeef.host", b"hello\r\n")

    assert written.parent.name == "new"
    assert written.read_bytes() == b"hello\r\n"
    assert list((root / "tmp").iterdir()) == []


def test_deliver_twice_with_one_name_leaves_one_message(tmp_path):
    """The property the retry path depends on."""
    root = tmp_path / "maildir"
    ensure_maildir(root)
    deliver(root, "1700000000.deadbeef.host", b"first\r\n")
    deliver(root, "1700000000.deadbeef.host", b"first\r\n")

    assert len(list((root / "new").iterdir())) == 1


def test_ensure_maildir_is_idempotent(tmp_path):
    root = tmp_path / "maildir"
    ensure_maildir(root)
    ensure_maildir(root)
    assert {p.name for p in root.iterdir()} == {"tmp", "new", "cur"}


def test_maildir_filename_is_deterministic_on_the_digest():
    a = maildir_filename("a" * 64, host="box")
    b = maildir_filename("a" * 64, host="box")
    assert a.split(".", 1)[1] == b.split(".", 1)[1]
    assert maildir_filename("b" * 64, host="box").split(".", 1)[1] != a.split(".", 1)[1]


def test_maildir_filename_escapes_an_awkward_hostname():
    name = maildir_filename("c" * 64, host="weird/host:1")
    assert "/" not in name and ":" not in name


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def test_config_defaults():
    settings = BridgeConfig.from_env({})
    assert settings.recipients == frozenset({"support@community-access.org"})
    assert settings.path == "/postmark/inbound"
    assert settings.maildir == Path("/maildir")
    assert settings.trusted_ips == frozenset()
    assert settings.webhook_user == "" and settings.webhook_password == ""


def test_config_reads_the_environment():
    settings = BridgeConfig.from_env(
        {
            "MAILBRIDGE_WEBHOOK_USER": "u",
            "MAILBRIDGE_WEBHOOK_PASSWORD": "p",
            "MAILBRIDGE_RECIPIENTS": "a@x.org, B@X.org",
            "MAILBRIDGE_MAILDIR": "/var/mail/support",
            "MAILBRIDGE_STATE_DB": "/srv/state.db",
            "MAILBRIDGE_PATH": "/hook",
            "MAILBRIDGE_MAX_BYTES": "1024",
            "MAILBRIDGE_TRUSTED_IPS": "1.1.1.1, 2.2.2.2",
        }
    )
    assert settings.webhook_user == "u" and settings.webhook_password == "p"
    assert settings.recipients == frozenset({"a@x.org", "b@x.org"})  # lowercased
    assert settings.maildir == Path("/var/mail/support")
    assert settings.path == "/hook"
    assert settings.max_bytes == 1024
    assert settings.trusted_ips == frozenset({"1.1.1.1", "2.2.2.2"})


def test_config_survives_a_nonsense_size():
    assert BridgeConfig.from_env({"MAILBRIDGE_MAX_BYTES": "lots"}).max_bytes == 50 * 1024 * 1024


def test_a_configured_path_is_the_one_that_answers(tmp_path):
    app = create_app(config(tmp_path, path="/hook"))
    assert call(app, payload(), path="/hook", header=auth())[0] == 200
    assert call(app, payload(), path="/postmark/inbound", header=auth())[0] == 404


def test_latin1_bytes_survive_the_whole_request(tmp_path):
    """The helper test above checks one half. This checks the pair.

    The request body is decoded with surrogateescape and RawEmail is encoded
    back with the same handler, so the byte that arrived is the byte written to
    the Maildir. Anything else corrupts the message -- and with it any DKIM
    signature computed over the original bytes, which surfaces later as mail
    that mysteriously fails authentication rather than as an error traceable to
    this bridge.
    """
    settings = config(tmp_path)
    app = create_app(settings)

    latin1 = b"Message-ID: <l1@example.org>\r\nSubject: caf\xe9 accessibility\r\n\r\nbody\r\n"
    body = json.dumps(
        {
            "MessageID": "latin-1-test",
            "OriginalRecipient": "support@community-access.org",
            "RawEmail": latin1.decode("utf-8", "surrogateescape"),
        }
    ).encode("utf-8", "surrogateescape")

    status, _, _ = call(app, raw=body, header=auth())

    assert status == 200
    written = messages_in(settings.maildir)
    assert len(written) == 1
    assert written[0].read_bytes() == latin1
