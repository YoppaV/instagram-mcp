# instagram-mcp

A read-only [MCP](https://modelcontextprotocol.io) server that lets Claude
(Code or Desktop) query **your own Instagram account** in real time — saved
posts, home feed, profiles, posts and reels.

It is read-only and personal. There are no write/mutation tools. Data is read
by driving a logged-in Playwright browser and intercepting the JSON responses
Instagram's own web app fires — no hand-crafted, signed API requests.

> ⚠️ Instagram detects automation aggressively. This server is deliberately
> slow and conservative (randomized pauses, low default limits, one serialized
> browser). See [Anti-bot precautions](#anti-bot-precautions).

## Tools

| Tool | Parameters |
|---|---|
| `auth_status` | — |
| `get_saved_posts` | `collection_id` (optional), `limit` (default 24) |
| `get_saved_collections` | — |
| `get_home_feed` | `limit` (default 15) |
| `get_user_profile` | `username` |
| `get_user_posts` | `username`, `limit` (default 24), `include_reels` (default false) |
| `get_post` | `id_or_url` (shortcode or post/reel URL) |
| `get_user_reels` | `username`, `limit` (default 12) |

## Setup

```bash
# 1. Create the virtualenv and install dependencies
uv venv venv --python 3.12
uv pip install --python venv/bin/python -r requirements.txt

# 2. Download the Chromium binary Playwright drives
venv/bin/python -m playwright install chromium
# If Chromium fails to launch on WSL2/Linux, install its system libs:
#   sudo venv/bin/python -m playwright install-deps chromium

# 3. Configure your handle
cp .env.example .env       # then set INSTAGRAM_USERNAME=yourhandle
```

## Authenticate

```bash
venv/bin/python -m scripts.auth_login --username yourhandle
```

A Chromium window opens on the Instagram login page. Log in manually (password,
2FA, "save login info", any checkpoints). Once the session cookies appear, the
storage state is saved to `sessions/<handle>_instagram_state.json`. The MCP
server reads that file on every call; re-run this command when the session
expires.

## Run

```bash
venv/bin/python -m instagram_sdk.server
```

Inspect it interactively with the MCP Inspector:

```bash
venv/bin/python -m mcp dev instagram_sdk.server
```

## Connect Claude

**Claude Code** — `.mcp.json` is committed at the repo root; open Claude Code
in this directory and the `instagram` server is auto-discovered.

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "instagram": {
      "command": "/home/juanr/projects/Instagram-mcp/venv/bin/python",
      "args": ["-m", "instagram_sdk.server"],
      "env": {
        "PYTHONPATH": "/home/juanr/projects/Instagram-mcp/src",
        "INSTAGRAM_SESSION_FILE": "/home/juanr/projects/Instagram-mcp/sessions/yourhandle_instagram_state.json"
      }
    }
  }
}
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `INSTAGRAM_USERNAME` | — | Handle used to name the session file. |
| `INSTAGRAM_SESSION_FILE` | auto-discovered | Explicit path to the storage_state JSON. |
| `BROWSER_IDLE_TIMEOUT_S` | `180` | Seconds Chromium stays warm between calls. |
| `BROWSER_HEADLESS` | `true` | `false` launches a visible browser (harder to detect). |

## Anti-bot precautions

Instagram is much more aggressive than other networks at flagging automation.
This server mitigates that by design: randomized multi-second pauses between
scrolls and navigations, low default `limit` values, a single browser instance
with serialized (never parallel) tool calls, capped scroll budgets, and a light
`navigator.webdriver` scrub. Keep requests small. If the account starts seeing
challenges, lower limits further and try `BROWSER_HEADLESS=false`.

## Architecture

```
src/instagram_sdk/
├── server.py      FastMCP entrypoint — 8 read-only tools
├── browser.py     Playwright singleton (lazy, idle timeout, asyncio.Lock)
├── auth.py        storage_state + session helpers + SessionExpiredError
├── models.py      Post · MediaItem · Profile · Collection · User · Reel
├── parsers.py     pure Instagram-JSON → dataclass extractors
├── scraper.py     scroll_collect() + intercept_single_response()
└── endpoints/     _ids · saved · home · profile · post
```

See `.claude/CLAUDE.md` for the full design notes and the "When the endpoints
change" maintenance guide.

## Tests

```bash
venv/bin/python -m unittest discover tests -v
```

Stdlib `unittest`, fixture-driven, no network or real browser.

## Troubleshooting

- **`SessionExpiredError`** — the session expired or the file is misnamed.
  Re-run `scripts.auth_login`; the file must be `<handle>_instagram_state.json`.
- **A tool returns `[]`** — Instagram likely rotated an endpoint. See the
  "When the endpoints change" section in `.claude/CLAUDE.md`.
- **Chromium won't launch** — install system libraries with
  `sudo venv/bin/python -m playwright install-deps chromium`.

## Out of scope

Writes (liking, posting, following), comment threads, stories, direct
messages, multiple accounts, and any persistent cache. This is a read-only,
single-account, real-time query tool.
