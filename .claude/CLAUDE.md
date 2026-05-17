# Instagram MCP Server

This project exposes a read-only Instagram SDK as an MCP server so Claude
(Code or Desktop) can query the user's own account — saved posts, home feed,
profiles, posts and reels — in real time during any conversation.

It is read-only and personal: there are no write/mutation tools by design.
Architecture, conventions and patterns mirror the sibling `twitter-mcp`
project; the difference is the data source (see "How Instagram differs").

## MCP tools

| Tool | What it does |
|---|---|
| `auth_status` | Check if an Instagram session is loaded (no network call). |
| `get_saved_posts` | Your saved posts. `collection_id` (optional), `limit`. |
| `get_saved_collections` | Your save-collections, each with name + item count. |
| `get_home_feed` | Your home timeline. `limit`. Sponsored posts are dropped. |
| `get_user_profile` | Bio, counters, private/verified flags, pic, links for `username`. |
| `get_user_posts` | A profile's grid posts. `limit`, `include_reels`. |
| `get_post` | One post by shortcode or URL: author, caption, media, counts. |
| `get_user_reels` | A profile's reels. `limit`. |

All tools return plain JSON (dataclass → dict). No mutations: read-only by design.

## How Instagram differs from Twitter

We never craft signed HTTP requests by hand — Instagram requires headers like
`X-IG-App-ID` and CSRF tokens. Instead, exactly as in `twitter-mcp`, Playwright
navigates to the real page, scrolls, and **intercepts the JSON responses the
web app itself fires** — Instagram signs those requests for us.

What it fires, as of 2026:

- **GraphQL** — most feeds POST to `/api/graphql` (or `/graphql/query`). The
  URL is the same for every operation; they are told apart by the
  `fb_api_req_friendly_name` field in the request body (e.g.
  `PolarisProfilePostsQuery`). Those names are the `FN_*` constants in
  `endpoints/_ids.py`.
- **REST** — saved posts and collection feeds are still plain
  `GET /api/v1/feed/...` endpoints with a distinctive URL path (`FR_*`).
- **Embedded HTML** — a single post page and the home feed's *first* batch are
  server-rendered into `<script type="application/json">` blobs, not fetched
  over a separate request. `parsers.find_embedded` digs those out.

`scraper._matches` recognizes a response by either its friendly name or a URL
substring, so one `fragment` string covers both transport styles.

## Architecture

```
src/instagram_sdk/
├── server.py          # FastMCP entrypoint, registers the 8 tools
├── browser.py         # Async Playwright singleton (lazy, idle timeout, asyncio.Lock)
├── auth.py            # storage_state load + session_summary + login helpers
├── models.py          # Post, MediaItem, Profile, Collection, User, Reel
├── parsers.py         # Instagram JSON → dataclass extractors (pure)
├── scraper.py         # scroll_collect() + intercept_single_response()
└── endpoints/
    ├── _ids.py        # URL builders + the FN_*/FR_* endpoint identifiers
    ├── saved.py       # get_saved_posts · get_saved_collections
    ├── home.py        # get_home_feed
    ├── profile.py     # get_user_profile · get_user_posts · get_user_reels
    └── post.py        # get_post
```

`scraper.scroll_collect` is generic over the item type (`Post`, `Reel`); pass
`key_of=` to dedupe, `keep=` to filter items before they count toward `limit`
(how `get_user_posts` excludes reels), and `embed_extractor=` to seed items
from the landing page's HTML (how `get_home_feed` picks up its first batch).
`get_user_profile` uses `intercept_single_response` (no scrolling); `get_post`
uses `fetch_rendered_html` and parses the embedded JSON.

## Running the server

```bash
venv/bin/python -m instagram_sdk.server
```

Session file resolution, in order:
- `INSTAGRAM_SESSION_FILE` env var, or
- `sessions/<INSTAGRAM_USERNAME>_instagram_state.json`, or
- the only `sessions/*_instagram_state.json` it can find.

`BROWSER_IDLE_TIMEOUT_S` (default 180) controls how long Chromium stays warm
between tool calls before being shut down to free RAM. `BROWSER_HEADLESS`
(default `true`) toggles headless mode.

## Auth

```bash
venv/bin/python -m scripts.auth_login --username yourhandle
```

Headed Chromium → log in to instagram.com manually (password, 2FA,
"save login info", suspicious-login checkpoints — all handled by you in the
window) → cookies (`sessionid`, `ds_user_id`, `csrftoken`, `mid`, …) are
persisted to `sessions/<handle>_instagram_state.json`.

The session filename also tells the server **which account is yours** — the
saved-posts tools navigate under `/<handle>/saved/...`, so the file must be
named `<handle>_instagram_state.json`.

When the MCP server hits an `/accounts/login` redirect mid-call, tools raise
`SessionExpiredError` with the re-login command. They never auto-login.

## Anti-bot precautions — important

Instagram detects and punishes automation **far more aggressively than
Twitter**. It can rate-limit or lock the account. The design is deliberately
conservative; keep it that way:

- Randomized pauses between scrolls (`SCROLL_DELAY_RANGE_S = (3.0, 6.5)`s) and
  before each navigation. Do not tighten these.
- Default `limit` values are low (home 15, posts/saved 24, reels 12). Prefer
  small requests; ask for more only when needed.
- One single browser instance (singleton) with an `asyncio.Lock` — tool calls
  are **serialized**. Never run two scrapes concurrently, never `asyncio.gather`
  over endpoints.
- `max_scrolls` is capped so a stalled feed can't scroll forever.
- A lightweight `navigator.webdriver` scrub and a realistic locale/timezone are
  set on the context.

If the account starts getting challenges, slow down further (raise the delay
ranges, lower limits) and consider running headed (`BROWSER_HEADLESS=false`).

## Connecting Claude clients

**Claude Code**: `.mcp.json` is committed at the repo root. Open Claude Code in
this directory and the `instagram` server is auto-discovered.

**Claude Desktop**: add to the `mcpServers` block of `claude_desktop_config.json`:

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

## Testing

```bash
venv/bin/python -m unittest discover tests -v
```

unittest stdlib only — no pytest. Tests are fixture-driven
(`tests/fixtures/gql_*.json`, `api_*.json`, `post_page.html`) and never touch
the network or a real browser. The endpoint tests use a `FakePage` that records
response handlers and dispatches synthetic payloads through them.

## Current endpoint map

Verified live (May 2026). Every identifier below is a constant in
`src/instagram_sdk/endpoints/_ids.py` — the one file to patch when Instagram
rotates things.

| Tool | Recognized by | Response shape |
|---|---|---|
| `get_home_feed` | `FN_TIMELINE` (`PolarisFeedRootPaginationCachedQuery_subscribe`) + first batch embedded in `/` HTML | `data.xdt_api__v1__feed__timeline__connection.edges[].node.media` |
| `get_user_profile` | `FN_PROFILE` (`PolarisProfilePageContentQuery`) | `data.user` |
| `get_user_posts` | `FN_PROFILE_POSTS` (`PolarisProfilePostsQuery`) | `data.xdt_api__v1__feed__user_timeline_graphql_connection.edges[].node` |
| `get_user_reels` | `FN_REELS` (`PolarisProfileReelsTabContentQuery`) | `data.xdt_api__v1__clips__user__connection_v2.edges[].node.media` |
| `get_saved_collections` | `FN_COLLECTIONS` (`PolarisProfileSavedTabContentQuery`) | `data.xdt_api__v1__collections__list_graphql_connection.edges[].node` |
| `get_saved_posts` | `FR_SAVED` / `FR_COLLECTION_FEED` (REST) | `items[].media` |
| `get_post` | embedded HTML key `EMBED_POST_KEY` (`xdt_api__v1__media__shortcode__web_info`) | `items[0]` |

Notes:
- A profile grid mixes posts and reels; `get_user_posts` excludes reels by
  default. For a reels-heavy account it may return fewer than `limit` posts —
  ask with `include_reels=True` for the raw grid.
- The reels-tab payload is light: `Reel.video_url` / `caption` come back
  `null`. Call `get_post(<shortcode>)` for a reel's full media.

## When the endpoints change

Instagram rotates these every few weeks. If a tool starts returning empty lists:

1. Open the relevant page in a browser with DevTools → Network tab open:
   - home feed → scroll `https://www.instagram.com/`
   - profile → `https://www.instagram.com/<username>/`
   - reels → `https://www.instagram.com/<username>/reels/`
   - saved → `https://www.instagram.com/<yourhandle>/saved/`
   - a post → `https://www.instagram.com/p/<shortcode>/`
2. Watch the requests to `/api/graphql` and `/graphql/query`. For each, the
   request's form body has an `fb_api_req_friendly_name` — that is the name we
   match. Find the one whose response carries the items you expect.
3. If the friendly name changed, update the matching `FN_*` constant in
   `_ids.py`; if a REST path moved, update the `FR_*` constant. If the JSON
   shape changed, adjust the connection key / fields in the relevant
   `extract_*` or `parse_*` function in `parsers.py`.
4. Save a real response into `tests/fixtures/` (`gql_<name>.json` for GraphQL,
   `api_<name>.json` for REST), trim/anonymize it, and add a parser test.

To capture a friendly name + body quickly, a throwaway script can register a
Playwright `page.on("response", ...)` handler, read
`response.request.post_data` for the `fb_api_req_friendly_name`, and dump
`response.text()` — that is exactly how this map was built.

`parse_post` is deliberately tolerant: it accepts any media node carrying
`media_type` / `image_versions2` / `code`, so a node moving between feeds
keeps parsing. If only field *names* changed, fix the relevant helper and
re-run the fixture tests.
