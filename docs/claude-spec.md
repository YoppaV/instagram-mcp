# Spec de configuración Claude/Codex — instagram-mcp

> Documento PROPOSE-ONLY. Nada de lo que aparece aquí se ha aplicado al repo.
> Describe qué AGENTS.md, skills, hooks y comandos deberían crearse y por qué.
> El único fichero que esta tarea ha escrito es este `docs/claude-spec.md`.

---

## 1. Auditoría del estado actual

Hoy el repo **no tiene ninguna configuración de agente**: no existe `AGENTS.md`,
ni `CLAUDE.md`, ni `.claude/`, ni `docs/` (esta carpeta se crea al escribir este
spec). Toda la "documentación para agentes" vive en el `README.md` — que, dicho
sea de paso, es excelente y será la fuente principal para redactar el AGENTS.md.

Dato importante: `.gitignore` (líneas 19-21) ya excluye explícitamente
`CLAUDE.md` y `.claude/` con el comentario *"project-local; not part of the
public surface"*. Eso significa que la convención canónica (AGENTS.md como
fuente de verdad versionada + `CLAUDE.md` stub de una línea) **choca** con la
intención actual de mantener la config fuera del checkout público. Hay que
decidir conscientemente (ver §7): lo recomendado es **versionar `AGENTS.md`**
(es tool-agnóstico, útil para cualquier colaborador y para Codex) y dejar
`CLAUDE.md` ignorado como stub local.

Puntuación frente a los 9 criterios del playbook (0 = ausente, 5 = ejemplar).
Todo el conocimiento existe pero **disperso en el README, no en un AGENTS.md**:

| # | Criterio | Hoy | Nota |
|---|----------|-----|------|
| 1 | Overview / qué es | 2/5 | Está en el README, no en AGENTS.md. |
| 2 | Stack / build | 2/5 | `pyproject.toml` claro; comandos en README. |
| 3 | Arquitectura | 3/5 | README tiene un mapa de módulos muy bueno + las 3 vías de transporte. |
| 4 | Folder structure | 2/5 | Implícita en README "Architecture". |
| 5 | Comandos (dev/test/lint) | 1/5 | Run y tests sí; **lint/format/typecheck NO documentados** pese a que las reglas globales exigen black/isort/ruff. |
| 6 | Editing rules + zonas peligrosas | 2/5 | README avisa de la sesión y del ToS, pero no en formato "no edites X". |
| 7 | Debugging playbook | 4/5 | La sección "Maintenance — when Instagram rotates endpoints" es oro (estilo twitter-mcp). |
| 8 | AGENTS.md por directorio | 0/5 | Inexistente. |
| 9 | Self-improvement loop | 0/5 | Inexistente (gap universal). |

**Puntuación global: 2/5.** El proyecto está completamente construido y muy bien
documentado para humanos, pero no tiene una capa de config tool-agnóstica para
agentes y le faltan dos piezas concretas: los comandos de calidad
(lint/format/typecheck) y el loop de auto-mejora.

Gaps concretos:
- No hay `AGENTS.md` raíz ni `CLAUDE.md` stub.
- Sin comandos de **lint / format / typecheck** (las reglas globales piden
  black + isort + ruff; el repo no declara herramienta de lint en `pyproject.toml`).
- Discrepancia de framework de test: las reglas globales dicen **pytest + cobertura
  80%**, pero el repo usa **stdlib `unittest`** (`python -m unittest discover tests`)
  y el CI (`.github/workflows/tests.yml`) también. El AGENTS.md debe documentar la
  realidad (unittest) y no inventar pytest.
- Sin bloque "Recent Lessons" ni regla de auto-mejora.
- Sin skill compartida `mcp-readonly-server` (los 7 repos MCP hermanos repiten la
  misma forma server/auth/sesión/test/captcha-stop).

---

## 2. AGENTS.md raíz propuesto

Borrador completo, listo para pegar en `/home/juanr/projects/mcp/instagram-mcp/AGENTS.md`.
`CLAUDE.md` (ignorado por git) sería un stub de una línea: `@AGENTS.md`.

````markdown
# instagram-mcp

## Qué es
Servidor MCP **read-only** que expone TU propia cuenta de Instagram a Claude
(Code o Desktop): posts guardados, home feed, perfiles, posts y reels, más
`download_media` para traer los bytes de la media. No hay tools de escritura por
diseño. Stack: Python 3.10–3.12, FastMCP (`mcp[cli]`), Playwright (Chromium),
httpx. El paquete Python se llama `instagram_sdk` y también se consume como
librería desde el pipeline `social-ingest`.

## Arquitectura / Conceptos core
No fabricamos peticiones HTTP firmadas a mano (Instagram exige `X-IG-App-ID`,
tokens CSRF...). En su lugar **conducimos un navegador logueado con Playwright y
interceptamos las respuestas JSON que la propia web de Instagram dispara** — ella
firma las peticiones por nosotros. Tres sabores de transporte comparten un mismo
matcher (`scraper._matches`):

- **GraphQL** — casi todo POSTea a `/api/graphql`; las operaciones se distinguen
  por el campo `fb_api_req_friendly_name` del body. Esos nombres son las
  constantes `FN_*` en `endpoints/_ids.py`.
- **REST** — saved posts y collection feeds usan `GET /api/v1/feed/...` con un
  path distintivo → constantes `FR_*`.
- **HTML embebido** — la página de un post y el *primer* batch del home feed se
  renderizan en `<script type="application/json">` → `parsers.find_embedded`,
  clave `EMBED_POST_KEY`.

Piezas clave:
- `browser.BrowserSession` — singleton Playwright lazy. Una sola context caliente,
  llamadas **serializadas tras un `asyncio.Lock`** (nunca paralelas), apagado por
  idle timeout. Esto es deliberado: dos pestañas raspando a la vez es lo que
  dispara la detección de automatización.
- `auth` — carga `storage_state` (cookies), detecta expiración (`SessionExpiredError`,
  redirect a `/accounts/login`), señal de "autenticado" = cookies `sessionid` +
  `ds_user_id` presentes.
- `scraper.scroll_collect` / `intercept_single_response` — el loop genérico
  navegar→interceptar→scrollear que reusan todos los endpoints.

## Folder structure
```
src/instagram_sdk/
├── server.py      FastMCP entrypoint — 9 tools (8 read-only + download_media)
├── browser.py     Singleton Playwright (lazy, idle timeout, asyncio.Lock)
├── auth.py        storage_state + helpers de sesión + SessionExpiredError
├── models.py      Post · MediaItem · Profile · Collection · User · Reel (dataclasses)
├── parsers.py     extractores puros JSON-de-Instagram → dataclass
├── scraper.py     scroll_collect() + intercept_single_response()
├── downloader.py  descarga de bytes con httpx (puro, sin Playwright)
└── endpoints/     _ids · saved · home · profile · post · media
src/config.py      loader de .env para scripts/auth_login (NO lo usa el server)
scripts/auth_login.py  login interactivo (headed) one-time
tests/             unittest stdlib, fixture-driven, sin red ni navegador real
tests/fixtures/    gql_*.json (GraphQL) · api_*.json (REST) · post_page.html
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
PYTHONPATH=src venv/bin/python -m unittest discover tests   # como el CI
RUN_LIVE_TESTS=1 venv/bin/python -m unittest tests.test_endpoints_live  # opt-in, pega al sitio real

# Lint / format / typecheck — NO configurados aún en pyproject.
# Las reglas globales piden black + isort + ruff. Propuesto (ver spec §5):
venv/bin/python -m ruff check src tests
venv/bin/python -m black --check src tests scripts
```
> Ojo: este repo usa `unittest`, NO `pytest`. El CI corre
> `python -m unittest discover tests -v` en Python 3.10 y 3.12. No introduzcas
> pytest sin acordarlo.

## API / Tools
9 tools (8 read-only + `download_media`). Todas devuelven JSON (dataclasses →
dict); `download_media` además devuelve objetos `Image` inline.

| Tool | Parámetros | Reconocido por |
|------|-----------|----------------|
| `auth_status` | — | sin red; lee el fichero de sesión |
| `get_saved_posts` | `collection_id?`, `limit=24` | `FR_SAVED` / `FR_COLLECTION_FEED` (REST) |
| `get_saved_collections` | — | `FN_COLLECTIONS` |
| `get_home_feed` | `limit=15` | `FN_TIMELINE` + HTML embebido del primer batch |
| `get_user_profile` | `username` | `FN_PROFILE` |
| `get_user_posts` | `username`, `limit=24`, `include_reels=false` | `FN_PROFILE_POSTS` |
| `get_post` | `id_or_url` | HTML embebido, clave `EMBED_POST_KEY` |
| `get_user_reels` | `username`, `limit=12` | `FN_REELS` (2 friendly names) |
| `download_media` | `id_or_url`, `indices?`, `download_videos=false` | re-fetch del post + httpx CDN |

`get_saved_*` son privados → navegan bajo `/<handle>/saved/...` y `<handle>` se
deriva del nombre del fichero de sesión (`_resolve_own_handle`).

## Editing rules + Dangerous areas
- **`endpoints/_ids.py` es el único punto a parchear cuando Instagram rota
  endpoints.** No esparzas friendly-names por el código.
- **NUNCA edites/sobrescribas el fichero de sesión**
  `~/.instagram-mcp/sessions/<handle>_instagram_state.json` (override:
  `INSTAGRAM_SESSION_DIR` / `INSTAGRAM_SESSION_FILE`). Es una cookie de login viva
  = una contraseña. Sobrescribirlo = re-login manual con 2FA. Está gitignored
  (`sessions/`, `*_state.json`) — mantenlo así.
- **No toques `.env`** (gitignored). Sólo `.env.example` es público.
- **No aflojes el pacing de `scraper.py`** (`PRE_NAV_DELAY_RANGE_S`,
  `SCROLL_DELAY_RANGE_S`, `MAX_SCROLLS_CAP=60`). Los rangos son lentos a propósito;
  apretarlos dispara la detección de automatización de Instagram.
- **No paralelices llamadas al browser.** El `asyncio.Lock` de `BrowserSession`
  serializa por diseño; los handlers `page.on("response")` colisionarían y, peor,
  dos pestañas raspando a la vez es exactamente lo que Instagram caza.
- **Si la cuenta empieza a ver challenges/captchas → PARA.** Baja los `limit`,
  prueba `BROWSER_HEADLESS=false`, y considera que la cuenta está marcada. No
  insistas con reintentos automáticos.
- **Read-only por diseño:** no añadas tools de escritura (like, follow, post,
  comentar, DMs, stories). Está fuera de scope a propósito.
- No commitees ficheros `tests/fixtures/*.json` sin **anonimizar y recortar** (son
  respuestas reales de Instagram).

## Debugging playbook
**Síntoma: un tool devuelve `[]` (lista vacía).** Casi siempre Instagram rotó un
friendly-name de GraphQL (lo hacen cada pocas semanas).
1. Abre la página con DevTools → Network:
   home `https://www.instagram.com/` · perfil `/<username>/` · reels
   `/<username>/reels/` · saved `/<yourhandle>/saved/` · post `/p/<shortcode>/`.
2. Mira las requests a `/api/graphql` y `/graphql/query`. El body trae
   `fb_api_req_friendly_name` — ése es el id que matcheamos. Encuentra el que
   lleva los items esperados.
3. Si cambió el nombre → actualiza la constante `FN_*` en `endpoints/_ids.py`.
   Si se movió un path REST → `FR_*`. Si cambió la forma del JSON → ajusta la
   clave de conexión / campos en el `extract_*` / `parse_*` de `parsers.py`.
4. Guarda una respuesta real en `tests/fixtures/` (`gql_<name>.json` /
   `api_<name>.json`), recórtala y anonimízala, y añade un test de parser.

**Síntoma: `SessionExpiredError`.** Sesión caducada o fichero mal nombrado.
Re-corre `python -m scripts.auth_login`; el fichero DEBE llamarse
`<handle>_instagram_state.json`. `_resolve_own_handle` deriva el handle del stem.

**Síntoma: Chromium no arranca (WSL2/Linux).** Faltan libs nativas:
`sudo venv/bin/python -m playwright install-deps chromium`.

**Síntoma: `download_media` devuelve `skipped` con `no_url`.** Las URLs del CDN
de Instagram son firmadas y efímeras; por eso `download_media` re-fetchea el post
fresco antes de descargar. Vídeos sólo bajan con `download_videos=True`.

## Recent Lessons
Formato: Fecha · Síntoma · Causa · Fix · Dónde. Sembrado con lecciones reales
extraídas del código y del README (ver spec §6 para el detalle).

- 2026-05 · Imports relativos rompen `mcp dev` · El loader `spec_from_file_location`
  carga `server.py` sin `__package__` · Usar imports absolutos `from instagram_sdk
  import ...` · `src/instagram_sdk/server.py:29-33`.
- 2026-05 · Cookies de login podrían acabar versionadas · La sesión vivía en
  `./sessions/` dentro del repo · Mover a `~/.instagram-mcp/sessions/` por defecto;
  in-repo queda como fallback legacy · `server._candidate_session_dirs`, `.gitignore`.
- 2026-05 · Reels devolvía la mitad de items · La pestaña Reels dispara DOS
  friendly-names (`PolarisProfileReelsTabContentQuery` inicial +
  `..._connection` en scroll) · `FN_REELS` es una tupla y el matcher acepta
  ambos · `endpoints/_ids.py:29-32`.
- 2026-05 · Shortcodes del home feed no parseaban · Los carruseles devuelven
  formas extendidas (~39 chars con metadata tras `_`) · `_SHORTCODE_RE` permisivo
  hasta 128 chars · `endpoints/_ids.py:50`.

## Self-Improvement Rule
Cuando arregles un bug no obvio, aprendas algo de la arquitectura, o el usuario
te corrija: **añade una entrada a `## Recent Lessons`** (Fecha · Síntoma · Causa ·
Fix · Dónde). Si la lección es transversal a varios repos MCP (forma
server/auth/sesión/captcha-stop), persístela también en
`~/.claude/projects/*/memory/` y considera subirla a la skill compartida
`mcp-readonly-server`.

## Git rules
Commits: `<type>: <descripción>` (feat/fix/refactor/docs/test/chore/perf/ci).
Atribución desactivada globalmente. Rama `main`. Para PRs analiza el historial
completo con `git diff main...HEAD` y reusa los slash-commands de
`.claude/commands/` (commit-push, pr, feature) una vez existan.
````

---

## 3. AGENTS.md por directorio

**Recomendación: NO crear AGENTS.md por directorio todavía.** El repo es pequeño
(8 módulos en `instagram_sdk/`, 6 endpoints, ~unos pocos miles de líneas). El
AGENTS.md raíz cubre todo con holgura y el README ya documenta el mapa de módulos.
Introducir ficheros por directorio aquí sería sobre-ingeniería y duplicación.

Único candidato razonable si el repo creciera: `src/instagram_sdk/endpoints/AGENTS.md`
con el contrato "cada endpoint = navegar(URL) → scroll_collect(fragment, extractor)
→ serializar; el fragment vive en `_ids.py`; añade fixture al tocar parsers". De
momento basta con tenerlo como sub-sección del raíz.

---

## 4. Skills a extraer

Conocimiento transversal que NO debe duplicarse en este repo sino vivir en
`~/.claude/skills/` y referenciarse desde el AGENTS.md.

### `mcp-readonly-server` (nueva, GLOBAL)
- **Qué contiene:** la forma compartida por los 7 servidores MCP read-only/scoped
  hermanos (`instagram-mcp`, `twitter-mcp`, `linkedin-mcp`, `reddit-mcp`,
  `polymarket-mcp`, `polymarket-flippers`, `eod-research-mcp`):
  - Entrypoint FastMCP `server.py`, run `venv/bin/python -m <pkg>.server`.
  - `browser.py` singleton Playwright + `auth.py` con `storage_state` y
    `SessionExpiredError`; sesión en `sessions/<handle>_state.json` **que jamás se
    edita/sobrescribe** (= re-login con 2FA).
  - Tool `auth_status` que NO hace red.
  - Tests fixture-driven con stdlib `unittest`, sin red, live test opt-in.
  - Disciplina anti-bot: pacing aleatorio, llamadas serializadas, **captcha/challenge
    = PARAR**, tiers read vs write.
  - Playbook de rotación de endpoints (un único fichero `_ids.py`/`endpoints.py`
    a parchear; capturar fixture real anonimizada).
- **Quién la consume:** los 7 repos MCP. Cada AGENTS.md la referencia en vez de
  repetir el boilerplate; sólo documenta lo específico (Instagram intercepta JSON
  vía Playwright, twitter rota fragmentos GraphQL, linkedin captcha=stop, etc.).
- **Beneficio aquí:** instagram-mcp documenta sólo su particularidad (3 transportes:
  GraphQL/REST/HTML embebido) y delega el resto a la skill.

### `playwright-intercept` (opcional, GLOBAL)
- **Qué contiene:** patrón "no fabriques requests firmadas; navega con el browser
  logueado e intercepta las respuestas JSON que dispara la propia web", con el
  matcher dual (friendly-name del body vs substring de URL) y el `scroll_collect`.
- **Quién la consume:** instagram-mcp y twitter-mcp (ambos interceptan GraphQL).
  linkedin-mcp parcialmente.

Nota: NO extraer a skill el contenido específico de Instagram (constantes `FN_*`,
las 3 vías de transporte, `EMBED_POST_KEY`). Eso es propio del repo y vive en su
AGENTS.md / README.

---

## 5. Hooks y slash-commands recomendados

### Hooks (familia social-ingest), adaptados a este stack
| Hook | ¿Aplica? | Adaptación |
|------|----------|-----------|
| `protect-files` | **Sí, crítico** | Bloquear writes a `**/sessions/*_state.json`, `~/.instagram-mcp/sessions/**`, `.env`. Sobrescribir la sesión = re-login 2FA. |
| `scan-secrets` | **Sí** | Pre-commit: detectar cookies `sessionid`/`ds_user_id` y rutas de sesión filtradas en fixtures o diffs. |
| `block-dangerous-commands` | **Sí** | Bloquear cualquier `playwright`/curl que pegue a instagram.com fuera del flujo de auth; evitar borrar `~/.instagram-mcp/`. |
| `format-on-save` | Sí (suave) | Correr `black` + `ruff --fix` sobre `src/`, `tests/`, `scripts/` al guardar `.py`. Primero hay que añadir black/ruff a deps de dev. |
| `test-on-edit` | Sí (acotado) | Al tocar `parsers.py`/`endpoints/**`/`_ids.py`, correr `python -m unittest discover tests` (rápido, sin red). NO disparar el live test. |

### Slash-commands (eod-etl), adaptados
| Comando | ¿Aplica? | Adaptación |
|---------|----------|-----------|
| `commit-push` | Sí | `<type>: <desc>`, sin atribución, push a `main` o rama. |
| `pr` | Sí | `git diff main...HEAD`, resumen + test plan (`python -m unittest discover tests -v` en 3.10 y 3.12 como el CI). |
| `feature` | Sí | Scaffolding de nuevo endpoint: añadir `FN_*` en `_ids.py`, `extract_*` en `parsers.py`, fixture `gql_<name>.json`, tool en `server.py`, test. |

---

## 6. Dangerous areas + Debugging playbook + Recent Lessons (seed)

Estas tres secciones están **redactadas completas en el AGENTS.md de §2**. Resumen
del contenido específico del repo:

**Dangerous areas:** fichero de sesión (`~/.instagram-mcp/sessions/<handle>_instagram_state.json`,
nunca editar/sobrescribir = 2FA), `.env`, el pacing de `scraper.py` (no apretar),
la serialización del browser (no paralelizar), captcha = parar, read-only (no
añadir writes), fixtures (anonimizar).

**Debugging playbook:** `[]` → rotación de endpoint (DevTools → friendly-name →
parchear `_ids.py` → fixture); `SessionExpiredError` → re-login + nombre de fichero;
Chromium no arranca → `install-deps`; `download_media`/`no_url` → URLs CDN efímeras.

**Recent Lessons (seed)** — 4 lecciones reales encontradas leyendo el código:
1. Imports absolutos obligatorios para que `mcp dev` cargue `server.py`
   (`server.py:29-33`).
2. Sesión movida de `./sessions/` a `~/.instagram-mcp/sessions/` para no versionar
   cookies (`server._candidate_session_dirs`).
3. La pestaña Reels dispara dos friendly-names → `FN_REELS` es tupla
   (`_ids.py:29-32`).
4. Shortcodes de carrusel en home feed llegan extendidos (~39 chars) → `_SHORTCODE_RE`
   permisivo hasta 128 (`_ids.py:50`).

---

## 7. Por qué

Cada cambio propuesto ataca un gap concreto de §1:

- **AGENTS.md raíz + `CLAUDE.md` stub (`@AGENTS.md`)** → resuelve los criterios
  1-7, que hoy sólo existen dispersos en el README (global 2/5). Un agente
  necesita un único fichero tool-agnóstico que sirva igual a Claude y a Codex.
  *Decisión de versionado:* `.gitignore` hoy excluye `CLAUDE.md`/`.claude/`. Se
  propone **versionar `AGENTS.md`** (quitarlo del ignore o añadir excepción
  `!AGENTS.md`) porque es público-friendly y útil para colaboradores; el stub
  `CLAUDE.md` puede quedarse ignorado como artefacto local.
- **Sección Comandos con lint/format/typecheck** → cierra el gap #5 (1/5), el más
  habitualmente ausente. Hace explícito que el test runner es **unittest, no
  pytest**, evitando que un agente "corrija" el repo hacia pytest contra el CI.
- **Recent Lessons + Self-Improvement Rule** → cierra el gap #9 (0/5), universal.
  Sembrado con 4 lecciones reales para que no nazca vacío.
- **Skill `mcp-readonly-server`** → elimina la duplicación del boilerplate
  server/auth/sesión/captcha-stop entre los 7 repos MCP; este repo sólo documenta
  su particularidad (3 transportes Playwright). Resuelve el criterio #8 sin crear
  AGENTS.md por directorio innecesarios.
- **Hooks `protect-files`/`scan-secrets`** → blindan la zona peligrosa #6: la
  cookie de sesión y el `.env`, cuyo borrado/filtrado tiene coste real (re-login
  2FA, fuga de credenciales).
- **No crear AGENTS.md por directorio** → el repo es pequeño; hacerlo sería
  duplicación, justo lo que el playbook desaconseja.
