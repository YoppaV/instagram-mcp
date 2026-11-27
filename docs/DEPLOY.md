# Running instagram-mcp on a home server (streamable-http + Docker)

stdio stays the default: Claude Code spawns `instagram-mcp` on your PC and
nothing listens on the network. This page is for the other setup — one
always-on box at home (a NAS) runs the server in Docker, and Claude Code on
your PC connects to it over HTTP on the LAN or Tailscale.

**Never expose it to the internet.** It is a read-only server, but it drives
a logged-in Instagram session: anyone who reaches it with the token reads
your account. No port forwarding, no `tailscale funnel`, no reverse proxy to
the outside.

## Why the server must be the only browser for that account

The request lock (`state/nav.lock`), the per-account budget and the
cooldown/blocked state are files on **one machine**. They keep every call
serialized and paced across processes on that machine; they do not cross
machines. If the PC also runs the stdio server with the same session while
the NAS runs it too, two browsers use the same cookies at the same time from
two places — exactly the pattern Instagram flags — and neither side knows the
other's budget or block state.

So once the NAS serves an account:

- remove (or don't register) the local stdio `instagram` server in Claude
  Code for that account, and point Claude Code at the NAS instead;
- don't run `social-ingest` or other library users against the same session
  from the PC;
- the PC only keeps the session for one thing: a new headed login when the
  session expires, which is then copied to the NAS again.

## 1. Log in on the PC and copy the session

The login is headed (password + 2FA by hand), so it happens on a machine
with a screen:

```bash
venv/bin/python -m scripts.auth_login --username yourhandle \
  --locale es-ES --timezone-id Europe/Madrid
```

That writes two files under `~/.instagram-mcp/sessions/`:

- `yourhandle_instagram_state.json` — the cookies (**as sensitive as a password**);
- `yourhandle_instagram_meta.json` — the fingerprint of the login (UA,
  locale, timezone, viewport; no secrets). Copy it too: the server reuses it
  so the NAS presents the same browser the login did.

Copy both into the sessions folder on the NAS and lock them down:

```bash
scp ~/.instagram-mcp/sessions/yourhandle_instagram_{state,meta}.json \
    nas:/volume2/SSD-Proyectos/Instagram-MCP/sessions/
ssh nas 'chmod 600 /volume2/SSD-Proyectos/Instagram-MCP/sessions/*.json'
```

The container mounts that folder **read-only**: the server never rewrites a
session. When it expires (`auth_status` says so, or tools fail with
`login_required`), log in again on the PC and copy the files again, then
`docker compose restart`.

## 2. Prepare the stack

Example layout (UGREEN UGOS Pro; adapt the paths):

| What | Where |
|---|---|
| compose, `.env`, `secrets/` | `/volume1/HDD-Archivo/Docker-Stacks/stacks/instagram-mcp/` |
| sessions (read-only in the container) | `/volume2/SSD-Proyectos/Instagram-MCP/sessions/` |
| state: lock, budget, block state, caches (writable) | `/volume2/SSD-Proyectos/Instagram-MCP/state/` |
| `download_media` / bulk downloads (writable) | `/volume2/SSD-Proyectos/Instagram-MCP/downloads/` |
| source to build the image from | `/volume2/SSD-Proyectos/Instagram-MCP/repo/` |

```bash
cd /volume1/HDD-Archivo/Docker-Stacks/stacks/instagram-mcp
cp /volume2/SSD-Proyectos/Instagram-MCP/repo/deploy/docker-compose.yml .
cp /volume2/SSD-Proyectos/Instagram-MCP/repo/deploy/.env.example .env   # then edit
mkdir -p secrets && (umask 077; openssl rand -hex 32 > secrets/instagram_mcp_token)
mkdir -p /volume2/SSD-Proyectos/Instagram-MCP/{sessions,state,downloads}
docker compose up -d --build
docker compose ps        # STATUS should become "healthy"
```

`.env` needs at least `PUID`/`PGID` (owner of the data folders, `id -u`,
`id -g`) and `INSTAGRAM_MCP_ALLOWED_HOSTS`.

`state/` and `downloads/` must be **writable** by `PUID:PGID`; `sessions/`
only readable. The state fails closed: if the server cannot write
`state/` (wrong owner, read-only mount) every tool call is refused with
`StateUnavailableError` instead of running without the lock and the budget.
The compose pins `INSTAGRAM_ALLOW_DEGRADED_STATE=false`; don't change that
on the server. Quick check after `up`:

```bash
docker compose exec instagram-mcp sh -c 'touch /data/state/.w /data/downloads/.w && rm /data/state/.w /data/downloads/.w && echo writable'
```

What the image and compose do:

- base `mcr.microsoft.com/playwright/python` pinned to the same Playwright
  version as `requirements.txt` (Chromium and its libraries come with it);
- non-root user, read-only root filesystem, `/tmp` as tmpfs, all
  capabilities dropped, `no-new-privileges`;
- `shm_size: 1gb` (Chromium crashes with Docker's 64 MB `/dev/shm`),
  `mem_limit: 2g` (the JS fallback can briefly reach ~1.5 GB);
- headless Chromium, closed after `BROWSER_IDLE_TIMEOUT_S` of inactivity;
- healthcheck on `GET /healthz`, which needs no token and never touches
  Instagram.

## 3. HTTP settings

| Variable | Default | Meaning |
|---|---|---|
| `INSTAGRAM_MCP_TRANSPORT` | `stdio` | `streamable-http` (or `http`) serves over HTTP. |
| `INSTAGRAM_MCP_HOST` / `INSTAGRAM_MCP_PORT` | `127.0.0.1` / `8000` | Bind address. `0.0.0.0` inside the container. |
| `INSTAGRAM_MCP_PATH` | `/mcp` | MCP endpoint path. |
| `INSTAGRAM_MCP_BEARER_TOKEN_FILE` | — | File with the token (Docker secret). Preferred. |
| `INSTAGRAM_MCP_BEARER_TOKEN` | — | The token itself (only if a file is impossible). Not both. |
| `INSTAGRAM_MCP_ALLOWED_HOSTS` | — | Extra `Host` values accepted, comma-separated (`192.168.1.48:*,nas:*`). Localhost is always allowed. `*` is refused. |
| `INSTAGRAM_MCP_ALLOWED_ORIGINS` | — | `Origin` values accepted (browsers only; Claude Code sends none). |

Rules enforced at start-up and on every request:

- HTTP mode **does not start** without a token, or with one shorter than 32
  characters (`openssl rand -hex 32`).
- Every request to the MCP path needs `Authorization: Bearer <token>`
  (compared in constant time) → otherwise `401`.
- The `Host` header must be in the allowed list → otherwise `421`
  (FastMCP's DNS-rebinding protection; bound to `0.0.0.0` it would otherwise
  either reject the LAN or accept any host).

## 4. Connect Claude Code

```bash
claude mcp remove instagram --scope user        # the local stdio one, if any
claude mcp add --transport http instagram --scope user \
  http://192.168.1.48:8765/mcp \
  --header "Authorization: Bearer $(cat /path/to/your/copy/of/the/token)"
claude mcp get instagram
```

Use the address that matches an entry of `INSTAGRAM_MCP_ALLOWED_HOSTS` (LAN
IP, NAS name, or its Tailscale name when away from home). The token ends up
in Claude Code's user config (`~/.claude.json`); keep that file private and
rotate the token by rewriting `secrets/instagram_mcp_token`, restarting the
stack and re-running `claude mcp add`.

Check it from Claude Code with `auth_status`: it should report the account
and `valid: true`.

## 5. Operating it

```bash
docker compose logs -f instagram-mcp     # stderr of the server
curl -s http://192.168.1.48:8765/healthz # {"status": "ok"}
```

`scripts/` is not part of the image. To look at or clear an account's block
state, read the JSON files under `state/` or run
`python -m scripts.account_state` from a checkout with
`INSTAGRAM_STATE_DIR` pointing at the NAS state folder. Clearing a block is a
human decision — nothing in the server does it.
