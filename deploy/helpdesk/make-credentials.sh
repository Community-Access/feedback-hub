#!/bin/sh
# Generate the inbound-mail credentials, once, into .env and dovecot-users.
#
# Three secrets are needed and none of them is ever typed by a person: the two
# halves of the webhook's HTTP Basic credential, and the password FreeScout
# uses to sign in to the local IMAP service. Generating them here means they
# are long and random rather than memorable, which is the correct trade for
# credentials no human reads.
#
# Idempotent: it refuses to overwrite values that are already set, because
# regenerating the webhook credential silently breaks inbound mail until the
# URL in Postmark is updated to match -- and mail that stops arriving is the
# hardest kind of failure to notice.
set -eu

cd "$(dirname "$0")"
[ -f .env ] || { echo "No .env here. Copy .env.example first."; exit 1; }

random() { head -c 32 /dev/urandom | base64 | tr -d '/+=' | cut -c1-40; }

set_once() {
    key="$1"
    current=$(grep -m1 "^${key}=" .env 2>/dev/null | cut -d= -f2- || true)
    if [ -n "$current" ]; then
        echo "$key is already set -- left alone."
        return
    fi
    value=$(random)
    if grep -q "^${key}=" .env; then
        sed -i "s|^${key}=.*|${key}=${value}|" .env
    else
        printf '%s=%s\n' "$key" "$value" >> .env
    fi
    echo "$key generated."
}

set_once MAILBRIDGE_WEBHOOK_USER
set_once MAILBRIDGE_WEBHOOK_PASSWORD
set_once IMAP_PASSWORD

imap_user=$(grep -m1 '^IMAP_USER=' .env | cut -d= -f2-)
imap_pass=$(grep -m1 '^IMAP_PASSWORD=' .env | cut -d= -f2-)
printf '%s:%s\n' "${imap_user:-freescout-support}" "$imap_pass" > dovecot-users
chmod 600 dovecot-users .env
echo "dovecot-users written."

hook_user=$(grep -m1 '^MAILBRIDGE_WEBHOOK_USER=' .env | cut -d= -f2-)
hook_pass=$(grep -m1 '^MAILBRIDGE_WEBHOOK_PASSWORD=' .env | cut -d= -f2-)
cat <<NOTE

Paste this as the inbound webhook URL in Postmark (Servers, then your inbound
server, then Settings). The credentials travel in the URL because that is what
Postmark supports; it is served only over HTTPS.

  https://${hook_user}:${hook_pass}@helpdesk.community-access.org/postmark/inbound

Then, in FreeScout (Manage, Mailboxes, your mailbox, Connection Settings, Fetching):

  Protocol: IMAP     Server: helpdesk-imap     Port: 143
  Encryption: none (the connection never leaves a private Docker network)
  Username: ${imap_user:-freescout-support}
  Password: ${imap_pass}

NOTE
