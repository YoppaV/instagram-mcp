# instagram-mcp

A read-only [MCP](https://modelcontextprotocol.io) server that lets Claude
(Code or Desktop) query **your own Instagram account** in real time — saved
posts, home feed, profiles, posts and reels — and download a post's media.

It is read-only and personal by design: there are no write/mutation tools.
Data is read through one logged-in Playwright tab on instagram.com: each
request is a `fetch` issued **inside that page**, so the browser attaches your
session cookies, with only the headers and page tokens the web app itself
uses. Images, video, fonts, CSS and scripts are not loaded in that tab.
`download_media` additionally fetches media bytes off Instagram's CDN over
HTTP.

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
Every `limit` is capped at 50: a larger value is clamped, not rejected.

## Requirements

- Python 3.10 – 3.12
- A real Instagram account (you'll log in manually once)
- ~800 MB free disk, mostly Chromium
- CPU only — no GPU is used
- macOS, Linux, WSL2 or native Windows ([Windows steps](#windows-powershell))

## Setup

The setup snippet uses [uv](https://docs.astral.sh/uv/) for the venv and
installs. If you don't have it: `curl -LsSf https://astral.sh/uv/install.sh | sh`
(or substitute the equivalent `python -m venv venv` + `venv/bin/pip install`
commands). On Windows, follow [Windows (PowerShell)](#windows-powershell)
instead: `venv/bin` doesn't exist there.

```bash
# 1. Create the virtualenv and install dependencies
uv venv venv --python 3.12
uv pip install --python venv -r requirements.txt

# 2. Install the package itself (editable — picks up local edits)
uv pip install --python venv -e .

# 3. Download the Chromium binary Playwright drives
venv/bin/python -m playwright install chromium
# If Chromium fails to launch on Linux/WSL2, install its system libs:
#   sudo venv/bin/python -m playwright install-deps chromium

# 4. Configure your handle
cp .env.example .env       # then set INSTAGRAM_USERNAME=yourhandle
```

### Windows (PowerShell)

Native Windows needs no WSL. uv fetches Python 3.12 by itself; you only need
Git and Claude Code. Every command runs from a normal PowerShell window, and
activating the venv is not needed, so the script execution policy doesn't
matter.

```powershell
# uv, if you don't have it. Open a new PowerShell window afterwards.
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

git clone https://github.com/YoppaV/instagram-mcp.git
cd instagram-mcp
uv venv venv --python 3.12
uv pip install --python venv -r requirements.txt
uv pip install --python venv -e .
venv\Scripts\python -m playwright install chromium

# Optional: check the install (no network, no browser)
venv\Scripts\python -m unittest discover tests

# Log in. A Chromium window opens; the password and 2FA are yours to type.
venv\Scripts\python -m scripts.auth_login --username yourhandle

# Register with Claude Code (from the repo folder, so the path is absolute)
claude mcp add instagram --scope user -- "$PWD\venv\Scripts\instagram-mcp.exe"
claude mcp list
```

The session file goes to `%USERPROFILE%\.instagram-mcp\sessions\`. For Claude
Desktop, use the full path to `venv\Scripts\instagram-mcp.exe` as `command`,
with backslashes doubled in the JSON.

## Authenticate (one-time, interactive)

```bash
venv/bin/python -m scripts.auth_login --username yourhandle
```

A Chromium window opens on the Instagram login page. Log in manually
(password, 2FA, "save login info", any suspicious-login checkpoints). Once
the session cookies appear, the storage state is saved to
`~/.instagram-mcp/sessions/<handle>_instagram_state.json` (override the
directory via `INSTAGRAM_SESSION_DIR`). The MCP server reads that file on
every call, so logging in after registering the server works without
restarting it. Re-run this command when the session expires (typically every
few weeks).

`--username` must be your Instagram username, not your login email. An email
or malformed value is refused before the browser opens; if the account that
logs in turns out to be a different one, the file is named after the real
account and you get a warning.

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
        async with browser.page() as ig:  # an in-page client, not a Playwright Page
            posts = await saved.fetch_saved(ig, collection_id=None, limit=10)
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
| `INSTAGRAM_STATE_DIR` | `~/.instagram-mcp/state` | Persistent state: whether Instagram stopped the account, cached user ids. |
| `INSTAGRAM_BLOCK_JS` | `true` | `false` lets instagram.com's scripts run in the tab (fallback mode, more RAM, not measured). |
| `BROWSER_CHANNEL` | — | Optional Playwright channel (e.g. `chromium` for the new headless). |
| `INSTAGRAM_LOG_LEVEL` | `WARNING` | Server log level (logs go to stderr). |

## Anti-bot precautions

Instagram is much more aggressive than other networks at flagging
automation. This server mitigates that by design: randomized multi-second
pauses between requests (1.5–3 s before the first of a call, 3–6.5 s between
the next ones — the old scroll pace), low default `limit` values, a single
browser with serialized (never parallel) tool calls, at most 20 requests per
tool call, Client Hints (`sec-ch-ua*`) that match the user agent instead of
saying `HeadlessChrome`, and a light `navigator.webdriver` scrub.

**When Instagram pushes back, the server stops.** A 429, "please wait a few
minutes", `feedback_required`, a challenge/checkpoint, a suspension or consent
page, or `login_required` raises an `AccountBlockedError` with no retry, and
the account state is written to `INSTAGRAM_STATE_DIR`. From then on every
call fails at once without touching the network:

- rate limits (`rate_limited`, `feedback_required`) cool down for 12 h (24 h,
  then up to 72 h if they repeat within 48 h) and lift by themselves;
- anything that needs a person (challenge, checkpoint, suspension, consent,
  `login_required`) stays blocked until you resolve it in your own browser
  and either log in again or run
  `venv/bin/python -m scripts.account_state --clear <handle>`.

`auth_status` shows the current `state` and `block`.

## Architecture

```
src/instagram_sdk/
├── server.py      FastMCP entrypoint — 9 tools (8 read-only + download_media)
├── runtime.py     Runtime.run(): account state check → lock → context → call
├── browser.py     BrowserManager (1 Chromium, 1 context + fixed tab per account,
│                  resource blocking, Client Hints, crash recovery) + BrowserSession facade
├── inpage.py      InPageClient: rest() / graphql() / navigate_html() via page.evaluate(fetch)
├── tokens.py      fb_dtsg / lsd / actorID from the page HTML (never logged)
├── ops.py         operation registry: doc_id, variables template, response root
├── guard.py       blocking-signal rules (429, challenge, checkpoint, ...)
├── accounts.py    account + persistent block state (cooldown / blocked / expired)
├── pacer.py       pauses between requests + per-call cap
├── cursors.py     cursor-driven pagination loop
├── settings.py    the only reader of environment variables
├── errors.py      typed errors (AccountBlockedError, SessionExpiredError, ...)
├── auth.py        storage_state + session helpers + UA / Client Hints
├── models.py      Post · MediaItem · Profile · Collection · User · Reel
├── parsers.py     pure Instagram-JSON → dataclass extractors
├── downloader.py  pure httpx streaming media-download helper (no Playwright)
└── endpoints/     _ids · users · saved · home · profile · post · media
```

The server keeps one Chromium with one fixed tab on instagram.com. In that
tab every image, video, font, stylesheet and script is aborted; only the HTML
document and our own `fetch` calls go out. Each data request is an in-page
`fetch`, so the browser adds the session cookies:

- **REST** (`GET /api/v1/...`, e.g. saved posts) needs only `X-IG-App-ID`.
- **GraphQL** (`POST /api/graphql`, or `/graphql/query` for the timeline)
  sends the operation's `doc_id` and `variables` plus the page tokens read
  from the HTML (`fb_dtsg`, `lsd`, `actorID`) and `X-CSRFToken` from the
  cookie — the minimal set the web app itself sends.
- **Embedded HTML** — a single post page still carries its data in
  `<script type="application/json">`; `parsers.find_embedded` digs it out.

Every response goes through `guard` before anything else. A tool can no
longer return `[]` because a wait ran out: either Instagram answered an empty
list, or a typed error explains what failed.

## Maintenance — when Instagram rotates operations

Instagram rotates GraphQL `doc_id`s (and sometimes response roots) with its
web deploys. A rotated operation now fails loudly with `DocIdStaleError` /
`OperationFailedError` naming the operation, instead of returning `[]`:

1. Open the relevant page in a browser with DevTools → Network tab open:
   - home feed → scroll `https://www.instagram.com/`
   - profile → `https://www.instagram.com/<username>/`
   - reels → `https://www.instagram.com/<username>/reels/`
   - saved → `https://www.instagram.com/<yourhandle>/saved/`
2. Find the POST to `/api/graphql` (or `/graphql/query`) whose form field
   `fb_api_req_friendly_name` matches the operation in the error. Copy its
   `doc_id` and `variables`.
3. Update that entry in `src/instagram_sdk/ops.py` (`doc_id`, the variables
   template, and `root` if the response shape moved) and bump `TABLE_DATE`.
   If only field names changed, fix the relevant `parsers.py` helper.
4. Save a real response into `tests/fixtures/`, trim and anonymize it, and
   add a parser test.

Current map (verified November 2026, web revision 1056444526):

| Tool | Operations (`ops.py`) |
|---|---|
| `get_saved_posts` | REST `saved_posts`; with a collection: REST `collection_feed` (fallback `saved_collection`) |
| `get_saved_collections` | `saved_collections` |
| `get_home_feed` | `/` HTML (or `home_first`) + `home_page` |
| `get_user_profile` | `search_box` (username → id) + `profile_info` |
| `get_user_posts` | `profile_posts_first` + `profile_posts_page` (+ reels for views) |
| `get_user_reels` | `search_box` + `profile_reels_first` + `profile_reels_page` |
| `get_post` / `download_media` | `/p/<shortcode>/` HTML, key `EMBED_POST_KEY` |

Automatic rediscovery of rotated `doc_id`s is planned, not in this version.

## Tests

```bash
venv/bin/python -m unittest discover tests -v      # Windows: venv\Scripts\python
```

Stdlib `unittest`, fixture-driven, no network or real browser. Two opt-in
suites: `tests/test_browser_local.py` drives a real Chromium but answers
every instagram.com request locally (`RUN_BROWSER_TESTS=1`), and
`tests/test_endpoints_live.py` hits the real site (`RUN_LIVE_TESTS=1`).

## Troubleshooting

- **`SessionExpiredError`** — the session expired or the file is misnamed.
  Re-run `scripts.auth_login`; the file must be
  `<handle>_instagram_state.json`.
- **`AccountBlockedError`** — Instagram stopped the account; nothing was
  retried. Read the `kind` in the message: a rate limit lifts on its own
  (the message says when); anything else needs you to open instagram.com in
  your own browser, resolve the notice, then re-login or run
  `venv/bin/python -m scripts.account_state --clear <handle>`. Lower your
  `limit`s afterwards.
- **`DocIdStaleError` / `OperationFailedError`** naming an operation —
  Instagram rotated it. See
  [Maintenance](#maintenance--when-instagram-rotates-operations).
- **`BudgetExceededError`** — our own cap of 20 requests per call was hit;
  ask for a smaller `limit`.
- **Chromium won't launch** — on Linux/WSL2, install system libraries with
  `sudo venv/bin/python -m playwright install-deps chromium`. On Windows,
  repeat `venv\Scripts\python -m playwright install chromium`.
- **`uv` is not recognized (Windows)** — open a new PowerShell window after
  installing it.

## Out of scope

Writes (liking, posting, following), comment threads, stories, direct
messages and multiple accounts (planned). This is a read-only,
single-account, real-time query tool.

## License

[MIT](./LICENSE) © 2026 Juan Rodríguez Díaz de Greñu
