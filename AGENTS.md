# instagram-mcp

## Qué es
Servidor MCP **read-only** que expone TU propia cuenta de Instagram a Claude (Code
o Desktop): posts guardados, home feed, perfiles, posts y reels, más
`download_media` para traer los bytes de la media. No hay tools de escritura por
diseño. El paquete Python se llama `instagram_sdk` y también se consume como
librería desde el pipeline `social-ingest`. Stack: Python 3.10-3.12, FastMCP
(`mcp[cli]`), Playwright (Chromium), httpx, stdlib `unittest`.

## Arquitectura
**Fetch dentro de la página.** Una pestaña fija de instagram.com por cuenta, con
imágenes/vídeo/fuentes/CSS/scripts abortados; cada dato sale de un `fetch`
ejecutado con `page.evaluate` en esa pestaña, así el navegador pone las cookies.
Sí construimos nosotros la petición, pero solo con lo que la propia web manda:
REST con `X-IG-App-ID`; GraphQL con `doc_id` + `variables` de `ops.py` y los
tokens del HTML (`fb_dtsg`, `lsd`, `actorID`) + `X-CSRFToken` de la cookie. Nada
de cabeceras inventadas ni firmas.

Piezas clave:
- `runtime.Runtime.run` — resuelve la cuenta (`account` o la por defecto) →
  estado de la cuenta (falla sin red si está bloqueada) → lock global: `asyncio.Lock`
  + fichero `state/nav.lock` entre procesos (**nunca dos llamadas a la vez, ni entre
  cuentas ni entre servidores**) → pausa si cambia de cuenta → contexto →
  `InPageClient` con el presupuesto de la cuenta → persiste presupuesto y estado.
- `accounts.AccountRegistry` — cada `<handle>_instagram_state.json` es una cuenta;
  la cuenta por defecto sale de `INSTAGRAM_DEFAULT_ACCOUNT` → `INSTAGRAM_SESSION_FILE`
  → `INSTAGRAM_USERNAME` → la única sesión. Con varias y nada configurado,
  `AmbiguousAccountError`: **nunca** elegir por orden alfabético.
- `browser.BrowserManager` — un Chromium, un contexto + pestaña por cuenta,
  Client Hints coherentes por CDP, recuperación si Chromium muere, apagado por idle.
  `BrowserSession` es la fachada de librería (`social-ingest`).
- `inpage.InPageClient` — `rest()` / `graphql()` / `navigate_html()`; toda
  respuesta pasa por `guard` antes que nada.
- `guard` + `accounts` — señal de bloqueo → `AccountBlockedError`, cero
  reintentos, estado persistido por cuenta (`cooldown` 12 h para rate limit;
  `blocked` manual para challenge/checkpoint/suspensión/consentimiento/login_required),
  pausa global de 30 min para las demás cuentas, freno por 3 fallos seguidos y
  presupuesto 150/h y 800/día por cuenta.
- `cursors` — las tools paginadas devuelven `{items, next_cursor, stopped}`; el
  cursor es opaco y sin estado en servidor (lleva tool, cuenta y hash de la consulta).
- `ops.py` — único punto a parchear cuando rota un `doc_id`.

## Folder structure
```
src/instagram_sdk/
├── server.py      FastMCP entrypoint — 10 tools (9 read-only + download_media)
├── runtime.py     Runtime.run: cuenta → estado → lock global → contexto → cliente
├── browser.py     BrowserManager + BrowserSession (fachada de librería)
├── inpage.py      InPageClient (fetch en página) + clasificación de respuestas
├── tokens.py      tokens del HTML (enmascarados en repr; nunca a logs ni disco)
├── ops.py         registro de operaciones (doc_id, variables, raíz)
├── guard.py       reglas de bloqueo
├── accounts.py    registro de cuentas, sidecar, estado, freno y presupuesto
├── locks.py       lock de fichero entre procesos
├── pacer.py       pausas + tope por llamada + gancho del presupuesto
├── cursors.py     bucle de paginación + cursor opaco
├── settings.py    única lectura de variables de entorno
├── errors.py      errores tipados
├── auth.py        storage_state, UA y Client Hints
├── models.py · parsers.py · downloader.py
└── endpoints/     _ids · users · saved · home · profile · post · media
scripts/auth_login.py     login interactivo (headed); añade una cuenta sin tocar las demás
scripts/account_state.py  ver / limpiar el estado de bloqueo (no es tool MCP)
tests/                    unittest stdlib, fixture-driven, sin red ni navegador real
tests/fixtures/           sintéticas/anonimizadas (gql_*, api_*, err_*, *.html)
docs/TOOLS.md             tabla de las 10 tools y sus operaciones
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
venv/bin/python -m scripts.auth_login --username otra --locale es-ES --timezone-id Europe/Madrid  # otra cuenta

# Tests — STDLIB unittest, fixture-driven, sin red
venv/bin/python -m unittest discover tests -v
PYTHONPATH=src venv/bin/python -m unittest discover tests       # como el CI
RUN_BROWSER_TESTS=1 PYTHONPATH=src venv/bin/python -m unittest tests.test_browser_local  # Chromium local, sin red
RUN_LIVE_TESTS=1 venv/bin/python -m unittest tests.test_endpoints_live  # opt-in, real
venv/bin/python -m scripts.account_state [<handle>] [--clear <handle>]  # estado por cuenta

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
  Junto a ella vive `<handle>_instagram_meta.json` (huella del login, sin secretos).
  Para probar varias cuentas sin red, simula la segunda con ficheros en un
  `INSTAGRAM_SESSION_DIR` temporal; nunca hagas login de otra cuenta para un test.
  Gitignored (`sessions/`, `*_state.json`); mantenlo así. Ver skill
  `mcp-readonly-server`.
- **`.env`** gitignored, secretos. Sólo `.env.example` es público. No editar.
- **Pacing de `pacer.py`** (`PRE_REQUEST_PAUSE_S`, `BETWEEN_REQUESTS_PAUSE_S`,
  `MAX_REQUESTS_PER_CALL=20`) y topes de páginas de `cursors.py` — lento a
  propósito; apretarlo dispara la detección anti-bot. No aflojar.
- **No paralelices llamadas al browser** — el lock de `Runtime` (proceso + fichero
  `nav.lock`) serializa por diseño, también entre cuentas; dos flujos de peticiones
  a la vez desde la misma IP es lo que Instagram caza. El lock no cruza máquinas.
- **Presupuesto y frenos** (`accounts.Budget`, `GLOBAL_COOLDOWN_S`, freno por fallos)
  — topes propios; no los subas sin consultar.
- **Captcha/challenge → PARA.** `guard` lo convierte en `AccountBlockedError` sin
  reintento y la cuenta queda retenida. No añadas reintentos tras una señal ni
  una tool para desbloquear (el modelo entraría en bucle).
- **Huella** (`auth.user_agent_override`, Client Hints) — cambiar una variable
  cada vez para poder atribuir un bloqueo.
- **Read-only por diseño** — no añadas tools de escritura (like, follow, post,
  comentar, DMs, stories). Fuera de scope a propósito.
- **`tests/fixtures/*.json`** son respuestas reales de Instagram — anonimiza y
  recorta antes de commitear.
- Catálogos de gotchas por área (lazy): `.claude/rules/anti-bot.md` (browser/inpage/pacer),
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
- `.claude/rules/anti-bot.md` (paths: browser.py/inpage.py/pacer.py) — serialización,
  pacing, captcha=stop + lecciones de área.
- `.claude/rules/endpoints.md` (paths: endpoints/**, parsers.py, ops.py) — playbook
  de rotación de `doc_id` + lecciones de endpoint.
- `.claude/rules/security.md` (paths: src/**, scripts/**, tests/fixtures/**) —
  fichero de sesión, `.env`, fixtures a anonimizar.
- `docs/TOOLS.md` — tabla exhaustiva de las 10 tools, cuentas y cursores.
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
