# instagram-mcp

## Qué es
Servidor MCP **read-only** que expone TU propia cuenta de Instagram a Claude (Code
o Desktop): posts guardados, home feed, perfiles, posts y reels, más
`download_media` para traer los bytes de la media. No hay tools de escritura por
diseño. El paquete Python se llama `instagram_sdk` y también se consume como
librería desde el pipeline `social-ingest`. Stack: Python 3.10-3.12, FastMCP
(`mcp[cli]`), Playwright (Chromium), httpx, stdlib `unittest`.

## Arquitectura
No fabricamos peticiones HTTP firmadas a mano (Instagram exige `X-IG-App-ID`,
tokens CSRF...). En su lugar **conducimos un navegador logueado con Playwright e
interceptamos las respuestas JSON que la propia web de Instagram dispara** — ella
firma por nosotros. Patrón compartido: ver skill `playwright-intercept`.

Tres sabores de transporte comparten un mismo matcher (`scraper._matches`):
- **GraphQL** — casi todo POSTea a `/api/graphql`; las operaciones se distinguen
  por `fb_api_req_friendly_name` del body → constantes `FN_*` en `endpoints/_ids.py`.
- **REST** — saved posts y collection feeds usan `GET /api/v1/feed/...` con path
  distintivo → constantes `FR_*`.
- **HTML embebido** — página de un post y *primer* batch del home feed van en
  `<script type="application/json">` → `parsers.find_embedded`, `EMBED_POST_KEY`.

Piezas clave:
- `browser.BrowserSession` — singleton Playwright lazy; una context caliente,
  llamadas **serializadas tras `asyncio.Lock`** (nunca paralelas), apagado por idle.
- `auth` — carga `storage_state` (cookies), detecta expiración
  (`SessionExpiredError`, redirect a `/accounts/login`); "autenticado" = cookies
  `sessionid` + `ds_user_id`.
- `scraper.scroll_collect` / `intercept_single_response` — el loop genérico
  navegar→interceptar→scrollear que reusan todos los endpoints.

El detalle del contrato de cada endpoint vive en `src/instagram_sdk/endpoints/CLAUDE.md`
(lazy). Forma compartida con los 6 MCP hermanos: skill `mcp-readonly-server`.

## Folder structure
```
src/instagram_sdk/
├── server.py      FastMCP entrypoint — 9 tools (8 read-only + download_media)
├── browser.py     Singleton Playwright (lazy, idle timeout, asyncio.Lock)
├── auth.py        storage_state + helpers de sesión + SessionExpiredError
├── models.py      Post · MediaItem · Profile · Collection · User · Reel (dataclasses)
├── parsers.py     extractores puros JSON-de-Instagram → dataclass
├── scraper.py     scroll_collect() + intercept_single_response() + pacing
├── downloader.py  descarga de bytes con httpx (puro, sin Playwright)
└── endpoints/     _ids · saved · home · profile · post · media  (ver CLAUDE.md)
src/config.py            loader de .env para scripts/auth_login (NO lo usa el server)
scripts/auth_login.py    login interactivo (headed) one-time
tests/                   unittest stdlib, fixture-driven, sin red ni navegador real
tests/fixtures/          gql_*.json (GraphQL) · api_*.json (REST) · post_page.html
docs/TOOLS.md            tabla exhaustiva de las 9 tools (params + endpoint reconocido)
```

## Comandos
El intérprete es siempre `venv/bin/python` (venv local; ver Setup en README).
```bash
# Instalar (uv recomendado)
uv venv venv --python 3.12
uv pip install --python venv/bin/python -r requirements.txt
uv pip install --python venv/bin/python -e .
venv/bin/python -m playwright install chromium

# Run del server
venv/bin/instagram-mcp                       # = python -m instagram_sdk.server
venv/bin/mcp dev src/instagram_sdk/server.py # MCP Inspector

# Login one-time (headed; password + 2FA manual)
venv/bin/python -m scripts.auth_login --username yourhandle

# Tests — STDLIB unittest, fixture-driven, sin red
venv/bin/python -m unittest discover tests -v
PYTHONPATH=src venv/bin/python -m unittest discover tests       # como el CI
RUN_LIVE_TESTS=1 venv/bin/python -m unittest tests.test_endpoints_live  # opt-in, real

# Lint / format / typecheck — NO configurados aún en pyproject.
# Las reglas globales piden black + isort + ruff. Propuesto:
venv/bin/python -m ruff check src tests
venv/bin/python -m black --check src tests scripts
```
> Este repo usa **`unittest`, NO `pytest`**. El CI corre
> `python -m unittest discover tests -v` en Python 3.10 y 3.12 sobre rama `main`.
> No introduzcas pytest sin acordarlo. GAP: no hay `[tool.ruff]`/`[tool.black]` en
> `pyproject.toml`, así que esos comandos corren con defaults (fuera de scope).

## Dangerous areas
- **Fichero de sesión** `~/.instagram-mcp/sessions/<handle>_instagram_state.json`
  (override `INSTAGRAM_SESSION_DIR`/`INSTAGRAM_SESSION_FILE`) — cookie de login viva
  = una contraseña. **NUNCA editar/sobrescribir** (= re-login manual con 2FA).
  Gitignored (`sessions/`, `*_state.json`); mantenlo así. Ver skill
  `mcp-readonly-server`.
- **`.env`** gitignored, secretos. Sólo `.env.example` es público. No editar.
- **Pacing de `scraper.py`** (`PRE_NAV_DELAY_RANGE_S`, `SCROLL_DELAY_RANGE_S`,
  `MAX_SCROLLS_CAP=60`) — lento a propósito; apretarlo dispara la detección
  anti-bot. No aflojar.
- **No paralelices llamadas al browser** — el `asyncio.Lock` de `BrowserSession`
  serializa por diseño; dos pestañas raspando a la vez es lo que Instagram caza.
- **Captcha/challenge → PARA.** Baja los `limit`, prueba `BROWSER_HEADLESS=false`,
  asume que la cuenta está marcada. Sin reintentos automáticos.
- **Read-only por diseño** — no añadas tools de escritura (like, follow, post,
  comentar, DMs, stories). Fuera de scope a propósito.
- **`tests/fixtures/*.json`** son respuestas reales de Instagram — anonimiza y
  recorta antes de commitear.
- Catálogos de gotchas por área (lazy): `.claude/rules/anti-bot.md` (browser/scraper),
  `.claude/rules/endpoints.md` (rotación `_ids.py`/parsers), `.claude/rules/security.md`
  (sesión/`.env`/fixtures).

## Self-Improvement Rule
Cuando arregles un bug no obvio, aprendas algo de la arquitectura, o el usuario te
corrija: **añade una entrada `Fecha · Síntoma · Causa · Fix · Dónde`**. Si la
lección es de un ÁREA concreta va a su `.claude/rules/<área>.md`; si es repo-wide
va a `## Recent Lessons` aquí abajo. Si es transversal a varios repos MCP (forma
server/auth/sesión/captcha-stop), persístela también en `~/.claude/projects/*/memory/`
y considera subirla a la skill `mcp-readonly-server`. Si hay que romper el
read-only o el pacing, DETENTE y consulta — no lo cambies en silencio.

## Mapa de conocimiento
Índice de las capas lazy (se cargan solas al tocar lo que cubren):
- `.claude/rules/anti-bot.md` (paths: browser.py/scraper.py) — serialización,
  pacing, captcha=stop + lecciones de área.
- `.claude/rules/endpoints.md` (paths: endpoints/**, parsers.py) — playbook de
  rotación de friendly-names + las 3 vías de transporte + lecciones de endpoint.
- `.claude/rules/security.md` (paths: src/**, scripts/**, tests/fixtures/**) —
  fichero de sesión, `.env`, fixtures a anonimizar.
- `src/instagram_sdk/endpoints/CLAUDE.md` — contrato endpoint = navegar→
  scroll_collect→serializar (nested, lazy al tocar el subtree).
- `docs/TOOLS.md` — tabla exhaustiva de las 9 tools + saved-privados/_resolve_own_handle.
- Skills globales (referencia por nombre, NO duplicar):
  `mcp-readonly-server` (forma server/auth/sesión/test/captcha de los 7 MCP),
  `playwright-intercept` (navegar+interceptar JSON vs forjar requests),
  `content-pipeline-subprocess` / `ingesta-social` (consumidor aguas abajo
  `social-ingest`).

## Recent Lessons
Sólo lecciones **repo-wide**; las de área van a su `.claude/rules/<área>.md`.

- 2026-05 · Imports relativos rompen `mcp dev` · el loader `spec_from_file_location`
  carga `server.py` sin `__package__` · usar imports absolutos `from instagram_sdk
  import ...` · `src/instagram_sdk/server.py:29-33`.
- 2026-05 · Cookies de login podrían acabar versionadas · la sesión vivía en
  `./sessions/` dentro del repo · mover a `~/.instagram-mcp/sessions/` por defecto;
  in-repo queda como fallback legacy · `server._candidate_session_dirs`, `.gitignore`.

## Git rules
Commits `<type>: <desc>` (feat/fix/refactor/docs/test/chore/perf/ci). Atribución
desactivada globalmente. Rama por defecto `main` (el CI dispara en `main`). Para
PRs analiza el historial completo con `git diff main...HEAD` (resumen + test plan:
unittest en 3.10 y 3.12 como el CI) y reusa los slash-commands de `.claude/commands/`
(commit-push, pr, feature) una vez existan.

## Proyectos hermanos
- `../../mcp/twitter-mcp/`, `../../mcp/linkedin-mcp/` — MCP hermanos (misma forma:
  skill `mcp-readonly-server`).
- `../../tools/social-ingest/` — consumidor de `instagram_sdk` como librería.
