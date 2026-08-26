# FreeScout on lp.csedesigns.com

The help desk behind `support@community-access.org`, per the
*Community Access Support, FreeScout, Postmark, and GitHub Integration Plan*
(v4, free-software baseline).

**Status: installed and running; not yet public, and not yet receiving mail.**
It answers on the shared Docker network as `helpdesk-app:80`. What remains is a
DNS record, a Caddy block, and Postmark — in that order, and each one needs
something only Jeff has.

Everything here is a container. `jeffbis` is in the `docker` group but has **no
passwordless sudo**, so a design needing root would have stalled on the first
step. FreeScout, its PHP, its nginx and its database all run unprivileged; the
only thing outside a container is one Caddy site block.

---

## What is deployed

| | |
| --- | --- |
| FreeScout | 1.8.219 (Laravel 5.5), image pinned by digest |
| Database | MariaDB 11.4, `helpdesk-db`, private network only |
| Web | nginx 1.28 + PHP 8.2 inside `helpdesk-app`, port 80, no host ports |
| Scheduler | the image's cron, ON — it is what fetches mail and sends replies |
| Caps | app 1 GB / 2 CPU, db 512 MB / 1 CPU |
| Admin | `jeff@jeffbishop.com`, created and verified |

```bash
cd ~/helpdesk
docker compose ps
docker compose logs -f app --tail 50
```

---

## The three steps left, in order

### 1. DNS — needs Jeff

`community-access.org` is on Namecheap (`dns1/dns2.registrar-servers.com`).
Add:

```
helpdesk    A    107.175.91.158
```

Then confirm before going further:

```bash
dig +short helpdesk.community-access.org     # must print 107.175.91.158
```

### 2. The Caddy route

Only once the record above resolves. See [`caddy-snippet.conf`](caddy-snippet.conf),
which carries the commands and the reason the order matters: Caddy asks Let's
Encrypt for a certificate the moment it loads a site block, and a name that
resolves to nothing produces a *failed validation* — rate limited far more
tightly (5 per hostname per hour) than a successful one. Adding the block early
does not queue it up; it spends the budget you will want when the record is
real.

### 3. Postmark — needs Jeff

Outbound is configured **in FreeScout's own interface**, not in `.env`, so
rotating the token never needs a container restart. *Manage → Settings → Mail*:

```
Driver:     SMTP
Host:       smtp.postmarkapp.com
Port:       587
Encryption: TLS
Username:   <Postmark Server API token>
Password:   <the same token>
From:       support@community-access.org
```

Send the test message before telling anybody the address exists.

**Inbound is not IMAP against a hosted mailbox**, because Postmark has none. It
posts the raw RFC-822 message to a small bridge, the bridge writes it into a
local Maildir, a localhost-only Dovecot exposes that Maildir, and FreeScout
fetches it with its ordinary IMAP mechanism. Feeding FreeScout real email is
what preserves its threading — `Message-ID`, `In-Reply-To`, `References`,
duplicate detection, auto-reply and bounce handling, conversation reactivation.
That logic is the reason to use FreeScout at all, and creating tickets through
its API instead would mean reimplementing every part of it.

The bridge is **built**. Two more containers do this:

| Container | Job |
| --- | --- |
| `helpdesk-mailbridge` | Receives the Postmark webhook, writes the raw message into a Maildir. `feedback_hub.mailbridge`, no dependencies but gunicorn. |
| `helpdesk-imap` | Dovecot, exposing that one Maildir to FreeScout. No published ports and **not** on the shared edge network, so only the containers on this project's private network can reach it. |

Generate the credentials once, then read what to paste where:

```bash
cd ~/feedback-hub/deploy/helpdesk
./make-credentials.sh
```

It prints the webhook URL for Postmark and the IMAP settings for FreeScout. It
is idempotent and refuses to overwrite a value that is already set, because
regenerating the webhook credential breaks inbound mail until the URL in
Postmark is changed to match — and mail that stops arriving is the hardest kind
of failure to notice.

### What the bridge does and does not do

It is a **delivery adapter, not a second help desk**. It writes the raw RFC-822
message out byte for byte and never parses it, rewrites a header, strips quoted
text, or touches `Message-ID`, `In-Reply-To` or `References`. Everything that
looks like mail handling — threading, duplicate detection, customer-versus-agent
replies, attachments, auto-replies, bounces, reactivating a closed conversation
— is FreeScout's, which is the entire reason for feeding it real email.

Three behaviours worth knowing before reading its logs:

- **A retry is answered 200 and writes nothing.** Postmark retries up to ten
  times over about ten and a half hours whenever it does not get a 200, so the
  bridge is idempotent on Postmark's `MessageID`, falling back to a hash of the
  message. The Maildir filename is derived from that hash, so even a crash
  between the write and the database update cannot produce a second copy.
- **Status codes are instructions to Postmark.** `403` stops it retrying and is
  used only for a recipient this bridge does not serve. Everything an
  administrator could fix — a missing `RawEmail`, a full disk, an unwritable
  mailbox — is a `5xx`, so the retry lands once it is fixed. A `4xx` there would
  discard the message before anybody noticed.
- **Spam is scored, logged, and delivered anyway.** Postmark's SpamAssassin
  verdict is recorded and acted on by nobody. A support inbox for accessibility
  work is full of long quoted threads, unusual markup and assistive-technology
  jargon — the shapes a filter mistrusts — so agents mark spam by hand until
  there is evidence for a threshold.

The audit trail is a SQLite file in the `bridge-state` volume. It records the
Postmark MessageID, the Maildir filename, the spam score and the **length** of
the subject rather than the subject itself: an administrator reads this table,
and message content belongs in the help desk where access is controlled, not in
a second store that would also have to be secured and backed up.

---

## The trap that cost this deployment an hour

**Symptom:** every URL 302-redirects to `/install.php`, and `APP_KEY` is empty
in FreeScout's `.env`. Nothing anywhere says "database".

**Cause:** the image normalises `DB_TYPE` — *both* `mariadb` and `mysql` — to
the literal string `mariadb`, then writes `DB_CONNECTION=mariadb` into
FreeScout's `.env`. FreeScout 1.8.219 runs on **Laravel 5.5**, whose
`config/database.php` defines `sqlite`, `mysql`, `pgsql` and `sqlsrv` and no
`mariadb`; that name only became a Laravel connection in version 11. So the
config cannot load, `key:generate` produces nothing, migrations do not run, and
the app concludes it is not installed. Two symptoms, one cause, and neither
mentions the real one.

No environment variable avoids it, and the image's custom-scripts hook runs
*before* the block that writes `DB_CONNECTION`, so it cannot be fixed there
either.

### The second half, which is worse

Repairing the file by hand is not enough, and finding out why is the reason
this section exists.

The image writes `.env` itself and stamps it *"Automatically Generated File —
Upon container restart any settings will reset!"*. A plain `restart` does in
fact preserve it. A **recreate** does not — and a recreate is what
`docker compose up -d` performs after any change to the compose file, and what
every image update performs. On recreate the FreeScout source is copied over
the top, the symlink into `/data` is replaced by a fresh file, the init script
finds no `APP_URL`, and takes the first-install path again: `DB_CONNECTION`
goes back to `mariadb` and **`APP_KEY` is written empty**.

An empty `APP_KEY` is not a cosmetic reset. FreeScout encrypts stored mailbox
passwords with it, so a rotated key does not merely sign everybody out — the
saved Postmark credentials become undecryptable and **inbound mail silently
stops**, at a moment that looks unrelated to whatever was updated. In a help
desk, that is mail nobody knows they are not receiving.

**The real fix is therefore ownership of the file, not repair of it.**
`docker-compose.yml` bind-mounts `./freescout.env` over `/www/html/.env`, so
the host copy is authoritative. The init script then finds `APP_URL` present,
takes its *update* path instead, and touches only the handful of keys it
manages — leaving `APP_KEY` and `DB_CONNECTION` alone. Verified by recreating
the container and comparing the file's checksum before and after: byte for
byte identical.

`fix-db-driver.sh` remains as the repair for a from-scratch build, where the
host file does not exist yet:

```bash
cd ~/helpdesk && ./fix-db-driver.sh
```

It rewrites `DB_CONNECTION` only when the current value names a connection this
FreeScout does not define — so the day upstream ships a real `mariadb`
connection, it correctly stops doing anything. It generates `APP_KEY` only when
empty, because regenerating an existing one causes exactly the damage described
above.

### Reading the config

`freescout.env` is owned by uid 80 — the container's `nginx` — at mode 600, so
`jeffbis` cannot read it directly. That is deliberate: it holds `APP_KEY`, the
database password, and eventually nothing else that should be casually
readable on a shared box.

```bash
docker exec helpdesk-app cat /www/html/.env          # read
docker compose exec app vi /www/html/.env            # edit, then restart app
```

(The ownership was set from a throwaway root container — `docker run --rm -v
"$PWD":/mnt alpine chown 80:82 /mnt/freescout.env` — because `chown` to another
uid needs root and this box has no passwordless sudo. That is the general
escape hatch on this host: `docker` *is* the root you do not otherwise have.)

---

## Day to day

```bash
# Health
docker compose ps
docker compose exec -u nginx app sh -c 'cd /www/html && php artisan freescout:clear-cache'

# Add an agent (or do it in Manage > Users, which is the accessible route)
docker compose exec -u nginx app sh -c \
  'cd /www/html && php artisan freescout:create-user --role=user \
     --firstName=X --lastName=Y --email=x@example.org --password=...'

# Stop it. Nothing else on the box is affected.
docker compose down
```

### Updating

Deliberate, never automatic — `ENABLE_AUTO_UPDATE` and FreeScout's in-app
updater are both off, because an unattended upgrade of a help desk is how a
Monday morning starts badly.

```bash
# 1. Back up first (see below). The image migrates the schema on boot.
# 2. Change the digest in docker-compose.yml to the new one.
docker compose pull && docker compose up -d
docker compose logs -f app        # watch the migrations
./fix-db-driver.sh                # no-op unless the driver bug is back
```

### Backups

The database is the whole help desk; the volumes hold attachments and settings.

```bash
docker compose exec -T db mariadb-dump -ufreescout -p"$DB_PASS" \
    --single-transaction --routines freescout | gzip > ~/backups/helpdesk-$(date +%F).sql.gz
docker run --rm -v helpdesk_app-data:/data -v ~/backups:/out alpine \
    tar czf /out/helpdesk-app-data-$(date +%F).tar.gz -C /data .
```

Restoring has to be *tested*, not assumed. An untested backup is a belief.

---

## Accessibility

The plan makes this a first-class requirement rather than a later pass, and it
is the reason FreeScout was chosen over a hosted product nobody could fix. Test
the workflows that agents actually live in — reading a conversation, replying,
assigning, adding a note, closing — with NVDA and JAWS, and treat a defect
found there as a defect, not a preference.

Two things already set here that matter: `APPLICATION_NAME` becomes the page
`<title>`, which is the first thing a screen reader announces on every page
load; and the container's timezone is set, so timestamps read as local time
rather than UTC that everyone has to convert in their head.

---

## What is deliberately not here

**No paid FreeScout modules.** Phase 1 of the plan is a zero-new-cost baseline:
FreeScout core, the community GitHub integration, and Postmark as the only
accepted existing service cost. Tags, Workflows, the 2FA module and the API &
Webhooks module are all deferred — not forgotten, deferred, and the plan says
which each one would buy.

**No customer data in GitHub.** When a conversation escalates to engineering,
an agent writes a *sanitized summary* into `Community-Access/quill` with one
`product:*` label, one or more `type:*` labels and `source:support` — never a
pasted transcript, never the customer's address. That repository is public and
the plan makes the rule mandatory. The labels already exist.

**The AI issue-drafting features stay off.** Ticket text may contain personal
information, and the human sanitising step is the point of the workflow rather
than an obstacle to it.
