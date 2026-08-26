"""Submitting through a server instead of carrying a credential.

Every other transport in this package hands GitHub a token. That is fine on a
desktop right up until you notice where the token comes from: it is compiled
into the installer, so anyone who unzips one has it. Scoping it to issues on a
single repository bounds the damage to issue spam, which is why it was
tolerable -- but "tolerable" is not the same as "necessary", and it stops being
necessary the moment a server can hold the credential instead.

So this module is one function. The client builds the same entry it always
built and posts it; the server files the issue. The app ships no secret at all,
and the token can be rotated by editing one file on one machine rather than by
shipping a release to every installed copy and waiting for people to take it.

**The other reason this exists is the one that matters later.** Once an app
posts to a URL rather than calling GitHub, *where the report ends up stops
being the app's business*. Moving Report a Bug from a GitHub issue to a support
conversation in a help desk then becomes a change on the server -- no release,
no version skew, no installed copy left behind still filing into the wrong
place. The seam is the point; GitHub is just what is behind it today.

The wire format is deliberately the same ``entry`` dictionary that
:func:`feedback_hub.submit` already builds and that ``_github.create_issue``
already renders. One shape, so there is nothing to keep in step.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

#: Long enough for a slow link on a bad day, short enough that a wedged server
#: does not leave a dialog frozen with no way out. The caller has already saved
#: the report locally by this point, so a timeout costs the submission and not
#: the words the person typed.
DEFAULT_TIMEOUT = 30


def relay_entry(
    server_url: str,
    entry: dict[str, Any],
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Post *entry* to a feedback-hub server. ``(number, url, error)``.

    Returns the same triple as :func:`feedback_hub._github.create_issue`, so a
    caller can swap one for the other without knowing which it has.

    Every failure is returned rather than raised. A report that cannot be
    submitted has still been saved locally by the caller, and an exception here
    would turn "we could not send this right now" into a crash in the middle of
    somebody reporting a crash.
    """
    if not server_url:
        return None, None, "no submission server configured"

    payload = json.dumps({"entry": entry}).encode("utf-8")
    request = urlrequest.Request(
        server_url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Named so the server's logs distinguish an app from the website
            # without the app having to identify itself in the body.
            "User-Agent": "feedback-hub-client",
        },
    )
    try:
        with urlrequest.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        return None, None, _readable_error(exc)
    except Exception as exc:  # noqa: BLE001 - a network is allowed to fail
        return None, None, f"could not reach the submission server: {exc}"

    if not isinstance(body, dict):
        return None, None, "the submission server returned something unexpected"
    if body.get("error"):
        return None, None, str(body["error"])
    return body.get("number"), body.get("url"), None


def _readable_error(exc: urlerror.HTTPError) -> str:
    """The server's own words when it has any, and never a bare status code.

    The server answers refusals in plain English precisely so they can be shown
    to the person who typed the report -- "too many just now, try again
    shortly" is actionable in a way that "HTTP 429" is not.
    """
    try:
        body = json.loads(exc.read().decode("utf-8"))
        message = body.get("error") if isinstance(body, dict) else None
        if message:
            return str(message)
    except Exception:  # noqa: BLE001 - fall through to the generic wording
        pass
    if exc.code == 429:
        return "too many reports have been sent from here just now; try again shortly"
    if 500 <= exc.code < 600:
        return "the submission server is having trouble; your report was saved locally"
    return f"the submission server refused it (HTTP {exc.code})"


__all__ = ["DEFAULT_TIMEOUT", "relay_entry"]
