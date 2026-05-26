# instagram-mcp

A read-only [MCP](https://modelcontextprotocol.io) server that lets Claude
(Code or Desktop) query **your own Instagram account** in real time — saved
posts, home feed, profiles, posts and reels — and download a post's media.

It is read-only and personal by design: there are no write/mutation tools.
Data is read by driving a logged-in Playwright browser and intercepting the
JSON responses Instagram's own web app fires — no hand-crafted, signed API
requests. `download_media` additionally fetches media bytes off Instagram's
CDN over HTTP.

> ⚠️ **Use this on accounts you own.** This is a personal scraper — point it
> at your own saved posts, your own home feed, profiles and posts you can
> already see while logged in. Scraping public Instagram data is in a legal
> grey area and Instagram's Terms of Service explicitly forbid automated
> access. You are responsible for how you use this. The server is
> deliberately slow and conservative to minimize account risk, but if your
> account starts seeing challenges, stop running it.

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
| `download_media` | `id_or_url`, `indices` (optional), `download_videos` (default false) |

Tools return plain JSON (dataclasses serialized to dicts). `download_media`
additionally returns inline `Image` objects for any image bytes it fetched.

## Requirements

- Python 3.10 – 3.12
- A real Instagram account (you'll log in manually once)
- ~500 MB free disk for Chromium
- CPU only — no GPU is used

## Setup

The setup snippet uses [uv](https://docs.astral.sh/uv/) for the venv and
installs. If you don't have it: `curl -LsSf https://astral.sh/uv/install.sh | sh`
(or substitute the equivalent `python -m venv venv` + `venv/bin/pip install`
commands).

```bash
# 1. Create the virtualenv and install dependencies
uv venv venv --python 3.12
uv pip install --python venv/bin/python -r requirements.txt

# 2. Install the package itself (editable — picks up local edits)
uv pip install --python venv/bin/python -e .

# 3. Download the Chromium binary Playwright drives
venv/bin/python -m playwright install chromium
# If Chromium fails to launch on Linux/WSL2, install its system libs:
#   sudo venv/bin/python -m playwright install-deps chromium

# 4. Configure your handle
cp .env.example .env       # then set INSTAGRAM_USERNAME=yourhandle
```

## Authenticate (one-time, interactive)

```bash
venv/bin/python -m scripts.auth_login --username yourhandle
```

A Chromium window opens on the Instagram login page. Log in manually
(password, 2FA, "save login info", any suspicious-login checkpoints). Once
the session cookies appear, the storage state is saved to
`~/.instagram-mcp/sessions/<handle>_instagram_state.json` (override the
directory via `INSTAGRAM_SESSION_DIR`). The MCP server reads that file on
every call; re-run this command when the session expires (typically every
few weeks).

The session file contains a logged-in cookie for your account — treat it
like a password. It is `.gitignore`d and lives outside the repo by default.

## Run

```bash
venv/bin/instagram-mcp
# equivalent to: venv/bin/python -m instagram_sdk.server
```

Inspect it interactively with the MCP Inspector:

```bash
venv/bin/mcp dev src/instagram_sdk/server.py
```

## Connect Claude

**Claude Code** — register it once as a user-scoped server so it is
available from every project:

```bash
claude mcp add instagram --scope user \
  --env BROWSER_HEADLESS=true \
  --env BROWSER_IDLE_TIMEOUT_S=180 \
  -- /path/to/instagram-mcp/venv/bin/instagram-mcp
```

Requires the `pip install -e .` step from Setup so the `instagram-mcp`
console script exists. The server auto-discovers your session file from
`~/.instagram-mcp/sessions/`; set `INSTAGRAM_SESSION_FILE` in the `--env`
list above if you keep it somewhere else. Check the registration with
`claude mcp get instagram` (`Scope: User config`).

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "instagram": {
      "command": "/path/to/instagram-mcp/venv/bin/instagram-mcp"
    }
  }
}
```

## Use from Python (as a library)

The `instagram_sdk` package is the underlying read-only SDK. It powers the
MCP server above and is also consumed directly by
[`social-ingest`](https://github.com/YoppaV/social-ingest), which pulls
saved posts and profile reels into an Obsidian vault.

```python
import asyncio
from pathlib import Path
from instagram_sdk.browser import BrowserSession
from instagram_sdk.endpoints import saved

async def main():
    session = Path.home() / ".instagram-mcp" / "sessions" / "yourhandle_instagram_state.json"
    browser = BrowserSession(session, idle_timeout_s=0, headless=True)
    try:
        async with browser.page() as page:
            posts = await saved.fetch_saved(
                page, handle="yourhandle", collection_id=None, limit=10
            )
        for post in posts:
            print(post.shortcode, post.caption[:80] if post.caption else "")
    finally:
        await browser.shutdown()

asyncio.run(main())
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `INSTAGRAM_USERNAME` | — | Handle used by `scripts.auth_login` to name the session file. |
| `INSTAGRAM_SESSION_FILE` | auto-discovered | Explicit path to the storage_state JSON. |
| `INSTAGRAM_SESSION_DIR` | `~/.instagram-mcp/sessions` | Directory holding `<handle>_instagram_state.json`. The legacy in-repo `sessions/` is checked as a fallback. |
| `BROWSER_IDLE_TIMEOUT_S` | `180` | Seconds Chromium stays warm between calls. |
| `BROWSER_HEADLESS` | `true` | `false` launches a visible browser (harder to detect). |
| `BROWSER_LOCALE` | `en-US` | Locale reported to instagram.com. Align with your account's real region for a less detectable fingerprint. |
| `BROWSER_TIMEZONE_ID` | `Etc/UTC` | Timezone reported to instagram.com. Same as above. |
| `INSTAGRAM_DOWNLOADS_DIR` | `~/.instagram-mcp/downloads` | Where `download_media` writes fetched files. |

## Anti-bot precautions

Instagram is much more aggressive than other networks at flagging
automation. This server mitigates that by design: randomized multi-second
pauses between scrolls and navigations, low default `limit` values, a
single browser instance with serialized (never parallel) tool calls, capped
scroll budgets, and a light `navigator.webdriver` scrub. Keep requests
small. If the account starts seeing challenges, lower limits further and
try `BROWSER_HEADLESS=false`.

## Architecture

```
src/instagram_sdk/
├── server.py      FastMCP entrypoint — 9 tools (8 read-only + download_media)
├── browser.py     Playwright singleton (lazy, idle timeout, asyncio.Lock)
├── auth.py        storage_state + session helpers + SessionExpiredError
├── models.py      Post · MediaItem · Profile · Collection · User · Reel
├── parsers.py     pure Instagram-JSON → dataclass extractors
├── scraper.py     scroll_collect() + intercept_single_response()
├── downloader.py  pure httpx streaming media-download helper (no Playwright)
└── endpoints/     _ids · saved · home · profile · post · media
```

We never craft signed HTTP requests by hand — Instagram requires headers
like `X-IG-App-ID` and CSRF tokens. Instead, Playwright navigates to the
real page, scrolls, and **intercepts the JSON responses the web app itself
fires** — Instagram signs those requests for us. Three transport flavors
share one matcher:

- **GraphQL** — most feeds POST to `/api/graphql` (or `/graphql/query`).
  The URL is the same for every operation; they're distinguished by the
  `fb_api_req_friendly_name` field in the request body (e.g.
  `PolarisProfilePostsQuery`). Those names are the `FN_*` constants in
  `endpoints/_ids.py`.
- **REST** — saved posts and collection feeds use plain
  `GET /api/v1/feed/...` endpoints with a distinctive URL path (`FR_*`).
- **Embedded HTML** — single post pages and the home feed's *first* batch
  are server-rendered into `<script type="application/json">` blobs.
  `parsers.find_embedded` digs them out.

## Maintenance — when Instagram rotates endpoints

Instagram rotates the GraphQL friendly names every few weeks. If a tool
starts returning `[]`:

1. Open the relevant page in a browser with DevTools → Network tab open:
   - home feed → scroll `https://www.instagram.com/`
   - profile → `https://www.instagram.com/<username>/`
   - reels → `https://www.instagram.com/<username>/reels/`
   - saved → `https://www.instagram.com/<yourhandle>/saved/`
   - a single post → `https://www.instagram.com/p/<shortcode>/`
2. Watch the requests to `/api/graphql` and `/graphql/query`. The
   request's form body has an `fb_api_req_friendly_name` — that's the
   identifier we match. Find the one whose response carries the items
   you expect.
3. If the friendly name changed, update the matching `FN_*` constant in
   `src/instagram_sdk/endpoints/_ids.py`. If a REST path moved, update
   the `FR_*` constant. If the JSON shape changed, adjust the
   connection key / fields in the relevant `extract_*` or `parse_*`
   function in `parsers.py`.
4. Save a real response into `tests/fixtures/` (`gql_<name>.json` for
   GraphQL, `api_<name>.json` for REST), trim and anonymize it, and add
   a parser test.

Current endpoint map (verified May 2026):

| Tool | Recognized by |
|---|---|
| `get_home_feed` | `FN_TIMELINE` + first batch embedded in `/` HTML |
| `get_user_profile` | `FN_PROFILE` |
| `get_user_posts` | `FN_PROFILE_POSTS` |
| `get_user_reels` | `FN_REELS` |
| `get_saved_collections` | `FN_COLLECTIONS` |
| `get_saved_posts` | `FR_SAVED` / `FR_COLLECTION_FEED` (REST) |
| `get_post` | embedded HTML key `EMBED_POST_KEY` |

`parse_post` is deliberately tolerant: it accepts any media node carrying
`media_type` / `image_versions2` / `code`, so a node moving between
feeds keeps parsing. If only field *names* changed, fix the relevant
helper and re-run the fixture tests.

## Tests

```bash
venv/bin/python -m unittest discover tests -v
```

Stdlib `unittest`, fixture-driven, no network or real browser. There is one
live integration test in `tests/test_endpoints_live.py` that hits the real
site; it self-skips unless `RUN_LIVE_TESTS=1`.

## Troubleshooting

- **`SessionExpiredError`** — the session expired or the file is misnamed.
  Re-run `scripts.auth_login`; the file must be
  `<handle>_instagram_state.json`.
- **A tool returns `[]`** — Instagram likely rotated an endpoint. See
  [Maintenance — when Instagram rotates endpoints](#maintenance--when-instagram-rotates-endpoints).
- **Chromium won't launch** — install system libraries with
  `sudo venv/bin/python -m playwright install-deps chromium`.

## Out of scope

Writes (liking, posting, following), comment threads, stories, direct
messages, multiple accounts, and any persistent cache. This is a read-only,
single-account, real-time query tool.

## License

[MIT](./LICENSE) © 2026 Juan Rodríguez Díaz de Greñu
