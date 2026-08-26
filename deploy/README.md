# Deploying the submission server

The endpoint that lets somebody file an issue **without a GitHub account**.

One process, one path, holding the only token. Everything else in feedback-hub
is a client that carries its own credential; this is the one that means they no
longer have to. See `src/feedback_hub/server.py` for the reasoning.

Live at **`https://lp.csedesigns.com/submit/picks`**, serving the suggestion
form at <https://quillforall.org/picks/suggest/>.

---

## What you need

- A host with Docker, behind a reverse proxy that terminates TLS.
- A GitHub **fine-grained** personal access token, scoped to the single target
  repository, with **Issues: read and write** and *nothing else*. This process
  accepts anonymous input from the public internet; the worst an abused
  endpoint can do should be to file issues.

That is the whole list. There is no database, no queue and no object store —
the rate limiter counts in memory and forgets everything within a day, which is
the right failure for a limit whose job is to blunt bursts rather than to
punish anyone.

---

## First deployment

These are the exact steps taken on `lp.csedesigns.com` ("bishoplink"), which
already runs three other applications behind a shared Caddy. Adjust the paths
if your edge is somewhere else.

### 1. Get the code onto the host

```bash
cd ~
git clone https://github.com/Community-Access/feedback-hub.git
cd feedback-hub
```

### 2. Put the token in place

```bash
cp deploy/.env.example deploy/.env
nano deploy/.env          # set FEEDBACK_HUB_GITHUB_TOKEN
chmod 600 deploy/.env
```

`deploy/.env` is gitignored and is the only place the token exists on the host.
It is never baked into the image, so rotating the token is `nano` plus a
restart — no rebuild, and no release of any app.

### 3. Build and start

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml ps
```

Wait for `healthy`. The container publishes no host ports: it joins the
existing `web_default` network and is reachable only by the shared Caddy, by
container name.

### 4. Add the route to Caddy

Paste the block in [`caddy-snippet.conf`](caddy-snippet.conf) into the
`lp.csedesigns.com, glow.bits-acb.org, www.letitglow.app` site block in
`/home/jeffbis/app/web/Caddyfile`, **above the `redir` line**, then validate
before reloading:

```bash
cd ~/app/web
cp Caddyfile Caddyfile.bak.$(date +%F)
nano Caddyfile
docker compose -f docker-compose.prod.yml exec -T caddy \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose -f docker-compose.prod.yml exec -T caddy \
    caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

`reload`, not `restart`: a reload swaps the configuration in place and drops no
connections, on a box where the same Caddy is also serving three production
applications.

**Above the `redir`** matters. That site block ends with a permanent redirect
of everything to `letitglow.app`. A 301 in front of this endpoint would not
merely be untidy — the browser would follow it to a different origin, the CORS
check would then be made against an origin the server does not allow, and the
form would fail in a way that looks exactly like the server being down.

### 5. Prove it

```bash
# The health check, through the public edge.
curl -si https://lp.csedesigns.com/submit/healthz | head -1

# A browser preflight from the allowed origin.
curl -si -X OPTIONS https://lp.csedesigns.com/submit/picks \
     -H 'Origin: https://quillforall.org' | head -1

# A foreign origin is refused request-side, not only response-side.
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
     https://lp.csedesigns.com/submit/picks \
     -H 'Origin: https://evil.example' -H 'Content-Type: application/json' \
     -d '{"title":"x","body":"x"}'      # expect 403
```

Then submit once from the real form and confirm the issue appears with the
`pick:suggestion` label. Close it afterwards; it has done its job.

---

## Day to day

```bash
# What it is doing
docker compose -f deploy/docker-compose.yml logs -f --tail 100

# Update to a new release
git pull && docker compose -f deploy/docker-compose.yml up -d --build

# Rotate the token: edit deploy/.env, then
docker compose -f deploy/docker-compose.yml restart

# Stop it. The form falls back to telling the visitor it could not be sent;
# the in-app route in Quill Radio is unaffected and still works.
docker compose -f deploy/docker-compose.yml down
```

Everything is configurable by environment variable — the list is in the module
docstring of `src/feedback_hub/server.py`, and the ones that matter are made
explicit in `docker-compose.yml` so `docker inspect` shows what the endpoint
does without anyone reading the source.

---

## Things worth knowing before you change it

**The rate limit is per process.** It counts in memory, so the two gunicorn
workers each keep their own counters and the effective limit is twice what is
configured. That is deliberate — the alternative is a store to run, back up and
reason about, for counters that all expire within a day. Restarting forgives
everybody, which is the right failure here. If this ever needs to be exact,
that is the moment to add Redis, not before.

**Do not raise the worker count to solve slowness.** The limit scales with it.
Two workers exist so that one slow GitHub call does not block the next
visitor's submission; that is the whole reason for the number.

**`PICKS_CLIENT_IP_HEADER` must stay set** behind a proxy. Blank it and the
socket peer is always Caddy, so every visitor shares one bucket and the first
submission of the minute locks out everybody else.

**`handle`, not `handle_path`.** `handle_path` strips the matched prefix, so
`/submit/picks` would arrive as `/picks` and 404 against the app's own
configured `PICKS_PATH`.

**If a spam challenge is ever needed: Turnstile, never reCAPTCHA.** Turnstile
is usually invisible and needs no puzzle. reCAPTCHA's image grids are precisely
the barrier this project exists to remove — a spam control that locks out blind
users to keep out bots has failed at the only job that matters here. Set
`TURNSTILE_SECRET` and the endpoint requires a token on every submission.

---

## Troubleshooting

| What you see | What it usually is |
| --- | --- |
| Form says "could not be sent", server is up | The page's own Content-Security-Policy. `docs/site/picks/suggest/index.html` needs `connect-src https://lp.csedesigns.com`, or the browser blocks the request before it leaves. |
| `502` from the endpoint | GitHub refused. The real reason is in the container log — it is kept out of the response on purpose, because it can carry rate-limit details and token hints the visitor could do nothing with. |
| `403` from a browser | The `Origin` is not in `PICKS_ALLOWED_ORIGINS`. |
| `429` immediately | One submission a minute per address, twenty a day. Expected while testing. |
| `404` at `/submit/picks` | Either the Caddy block is below the `redir`, or `handle_path` is stripping the prefix. |
| Container unhealthy at boot | It has no token and says so in the log. Every submission is refused without one. |
