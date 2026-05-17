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

Twitter serves its data over GraphQL. Instagram's web app serves data mostly
from JSON endpoints under `/api/v1/...` (plus a `/graphql/query` call for a
single post page). We never craft signed HTTP requests by hand — Instagram
requires headers like `X-IG-App-ID` and CSRF tokens. Instead, exactly as in
`twitter-mcp`, Playwright navigates to the real page, scrolls, and
**intercepts the JSON responses the web app itself fires** — Instagram signs
those requests for us.

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
    ├── _ids.py        # URL builders + the /api/v1 path fragment constants
    ├── saved.py       # get_saved_posts · get_saved_collections
    ├── home.py        # get_home_feed
    ├── profile.py     # get_user_profile · get_user_posts · get_user_reels
    └── post.py        # get_post
```

`scraper.scroll_collect` is generic over the item type (`Post`, `Reel`); pass
`key_of=` to dedupe. Its `fragment` argument accepts either one path substring
or a tuple — `get_user_posts` uses a tuple to merge the ~12 posts embedded in
`web_profile_info` with the rest from the `feed/user` pagination endpoint.
`get_user_profile` and `get_post` use `intercept_single_response` (no scrolling).

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
(`tests/fixtures/api_*.json`, `graphql_*.json`) and never touch the network or
a real browser. The endpoint tests use a `FakePage` that records response
handlers and dispatches synthetic JSON payloads through them.

## When the endpoints change

Instagram rotates its internal endpoints and JSON shapes every few weeks. All
the path fragments we match against live in **one file** —
`src/instagram_sdk/endpoints/_ids.py` (`FR_TIMELINE`, `FR_WEB_PROFILE`,
`FR_USER_FEED`, `FR_GRAPHQL`, `FR_CLIPS`, `FR_SAVED`, `FR_COLLECTION_FEED`,
`FR_COLLECTIONS_LIST`).

If a tool starts returning empty lists:

1. Open the relevant instagram.com page in a browser with DevTools → Network
   tab open (filter `api/v1`):
   - home feed → scroll `https://www.instagram.com/`
   - profile → `https://www.instagram.com/<username>/`
   - a post → `https://www.instagram.com/p/<shortcode>/`
   - reels → `https://www.instagram.com/<username>/reels/`
   - saved → `https://www.instagram.com/<yourhandle>/saved/`
2. Find the request that carries the items you expect (it usually has
   `next_max_id` / `more_available` in the response, or `feed_items` for the
   home timeline).
3. If the path moved, update the matching `FR_*` constant in `_ids.py`.
4. Save the JSON response into `tests/fixtures/` as `api_<name>.json` (or
   `graphql_<name>.json` for the post query), trim/anonymize it, and add a
   parser test in `tests/test_parsers.py`.

The parsers in `parsers.py` are deliberately tolerant — `parse_post` sniffs
whether it got the private-API shape (`media_type`, `image_versions2`) or the
GraphQL shape (`__typename`, `display_url`) and normalizes both. If only the
field *names* inside a payload changed, adjust the relevant `parse_*` helper
and re-run the fixture tests.

### Endpoints to confirm against a live session

These were built against the documented/expected Instagram web shapes and
should be verified once in DevTools with a real logged-in account:

- `feed/user/{id}` — whether each item is the media directly or under `item["media"]`
  (the parser tolerates both).
- The live `/graphql/query` `doc_id` for a post page, and that the response key
  is still `data.xdt_shortcode_media`.
- A specific saved collection's URL — `collection_url()` in `_ids.py` builds
  `/<handle>/saved/<collection_id>/`; if Instagram requires the name slug,
  patch that builder.
- Whether `web_profile_info` still embeds the first posts in
  `edge_owner_to_timeline_media.edges` (it is unreliable per-account — the
  `feed/user` fallback covers an empty `edges`).
