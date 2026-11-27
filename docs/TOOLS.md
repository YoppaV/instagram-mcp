# TOOLS — instagram-mcp

Referencia exhaustiva de las **10 tools** que expone `server.py` (9 read-only +
`download_media`). Todas devuelven JSON (dataclasses → dict); `download_media`
además devuelve objetos `Image` inline. Router y arquitectura en `../AGENTS.md`.

Todo `limit` tiene un tope de 50: un valor mayor se recorta, no da error. Cada
llamada hace como mucho 20 peticiones a Instagram y 8 páginas; para seguir, se
usa `next_cursor`.

**Todas** aceptan `account?` (un handle de `list_accounts`); sin él se usa la
cuenta por defecto (`INSTAGRAM_DEFAULT_ACCOUNT` → `INSTAGRAM_SESSION_FILE` →
`INSTAGRAM_USERNAME` con sesión → la única sesión; con varias y nada configurado,
`AmbiguousAccountError`). Las marcadas **sobre** devuelven
`{items, next_cursor, stopped}` y aceptan `cursor?`.

| Tool | Parámetros | Operaciones (`ops.py`) |
|------|-----------|----------------|
| `auth_status` | `account?` | sin red; sesión, `user_id`, `is_default`, `default_source`, `state`, `block`, `warning` |
| `list_accounts` | — | sin red; lo mismo por cada cuenta (`[]` si no hay sesiones) |
| `get_saved_posts` (sobre) | `collection_id?`, `limit=24`, `cursor?` | REST `saved_posts`; con colección, REST `collection_feed` (respaldo GraphQL `saved_collection`) |
| `get_saved_collections` | `account?` | GraphQL `saved_collections` (raíz `viewer.collections_unified_with_auto_collections`) |
| `get_home_feed` (sobre) | `limit=15`, `cursor?` | HTML de `/` (o `home_first`) + `home_page` |
| `get_user_profile` | `username` | `search_box` (username → id, coincidencia exacta; respaldo `profile_posts_first`) + `profile_info` |
| `get_user_posts` (sobre) | `username`, `limit=24`, `include_reels=false`, `cursor?` | `profile_posts_first` + `profile_posts_page`; reels del autor para las vistas |
| `get_post` | `id_or_url` | HTML de `/p/<code>/`, clave `EMBED_POST_KEY` (+ reels del autor para las vistas) |
| `get_user_reels` (sobre) | `username`, `limit=12`, `cursor?` | `search_box` + `profile_reels_first` + `profile_reels_page` |
| `download_media` | `id_or_url`, `indices?`, `download_videos=false` | re-fetch del post (sin vistas) + httpx CDN fuera del lock |
| `get_post_comments` (sobre) | `id_or_url`, `limit=20`, `cursor?`, `include_replies=false` | `comments_first` (15) + `comments_page` (10/página); con respuestas, `comment_replies` por hilo |
| `search_users` | `query`, `limit=10` | `search_box` (1 petición) |
| `search_hashtag` (sobre) | `tag`, `limit=24`, `cursor?` | `keyword_first` + `keyword_page` con `query="#tag"` (≈24 posts/página, máx. 3 páginas) |

## Notas
- **Transporte**: cada petición es un `fetch` dentro de una pestaña fija de
  instagram.com con imágenes, vídeo, fuentes, CSS y scripts bloqueados. Ya no se
  navega y se hace scroll.
- **Bloqueos**: 429, "please wait", `feedback_required`, challenge/checkpoint,
  suspensión, consentimiento o `login_required` → `AccountBlockedError` sin
  reintento; la cuenta queda en `cooldown` (12 h, se levanta sola) o `blocked`
  (manual: re-login o `python -m scripts.account_state --clear <handle>`).
  Mientras dure, toda llamada falla al instante sin red.
- **Vistas (`view_count` / `play_count`)**: sólo la pestaña Reels (`play_count`) y
  el feed REST de guardados las traen. `get_user_posts(include_reels=True)` y
  `get_post` las completan con la pestaña Reels del autor (primera página + 2 más,
  best-effort: un fallo ahí no hace fallar la tool).
- **Saved** ya no usan el handle: los feeds REST y las consultas `viewer` van por
  la cookie de sesión. Una colección se lee primero por REST (trae `video_url`);
  si ese endpoint desaparece, el respaldo GraphQL llega **sin `video_url` ni
  `taken_at`** (la web no los pide en esa vista).
- **Paginación**: la gobierna el cursor de Instagram, no los ítems que pasan el
  filtro; un perfil casi todo reels ya no corta `get_user_posts` antes de tiempo.
  `stopped` ∈ `end` (no hay más), `limit`, `page_cap` (tope de 8 páginas: hay
  cursor para seguir), `budget` (presupuesto agotado a mitad: hay cursor).
- **Cursor opaco y sin estado**: `base64url` de `{v, t, a, q, u, s}` (versión, tool,
  cuenta, hash de los parámetros de la consulta sin `limit`, cursor de Instagram y
  cuántos ítems de esa página ya se dieron). Sirve tras reiniciar y desde otro
  cliente; usarlo con otra tool, cuenta o consulta da `ValueError` sin red. Los
  cursores de Instagram caducan: si uno falla, empezar sin cursor.
- **Multi-cuenta**: un Chromium, un contexto por cuenta (cookies y huella propias,
  la huella sale del sidecar `<handle>_instagram_meta.json`), creado bajo demanda y
  cerrado por inactividad por separado. Las llamadas nunca se solapan (ni entre
  cuentas ni entre procesos: `INSTAGRAM_STATE_DIR/nav.lock`); cambiar de cuenta
  añade 5-10 s de pausa. Una señal de bloqueo en una cuenta pausa las demás 30 min.
- **Presupuesto**: 150 peticiones/hora y 800/día por cuenta
  (`INSTAGRAM_MAX_REQUESTS_PER_HOUR` / `_PER_DAY`), persistido; al pasarlo,
  `BudgetExceededError` con la espera. 3 fallos seguidos en 10 min → 15 min de pausa.
  Las tools devuelven lo leído con `stopped: "budget"` y cursor; las funciones de
  librería que devuelven lista lanzan `BudgetExceededError` con lo leído en `.partial`.
- **Estado en cerrado**: un fichero de estado ilegible o corrupto, o un
  `INSTAGRAM_STATE_DIR` no escribible, dan `StateUnavailableError` sin red (salvo
  `INSTAGRAM_ALLOW_DEGRADED_STATE=1`).
- **Id propio** (`viewer_id()`): sale de `appScopedIdentity` en el HTML; respaldo,
  la cookie `ds_user_id` viva y luego el id guardado en la sesión/sidecar. **No**
  `actorID`: en instagram.com es el id `17841…` del lado Facebook, no el pk. La
  cookie `ds_user_id` puede caducar meses antes que la sesión. `auth_status` exige solo un
  `sessionid` vigente.
- **`download_media`** re-fetchea el post fresco antes de descargar porque las URLs
  del CDN de Instagram son firmadas y efímeras; escribe vía temporal + rename en
  `<downloads>/<cuenta>/post_<shortcode>_<pk>_<NN>.<ext>` (el `pk` evita que dos
  shortcodes que solo difieren en mayúsculas se pisen en Windows/macOS/SMB).
- **Comentarios** (`get_post_comments`): `media_id` sale del shortcode sin
  petición (o se pasa el id numérico). Orden "popular", como la web. Cada
  `Comment`: `pk`, `text`, `created_at`, `like_count`, `reply_count`, `parent_pk`,
  `is_pinned`, `is_edited`, `author`, `replies`. `include_replies` es una muestra:
  solo los 3 primeros comentarios con respuestas, 5 respuestas cada uno (una
  petición por hilo); `replies` = `[]` si no tiene, `null` si no se pidieron o no
  cupieron. Cuentan en la clase de presupuesto `comments` (40/h, 200/día).
  La página siguiente de Instagram puede repetir comentarios de la anterior (visto
  en real: 2 de 15); el cursor lleva los últimos 30 `pk` para descartarlos.
- **Búsqueda**: `search_users` da filas sin `is_private` (sale `false`). La página
  clásica de hashtag ya no existe (`/explore/tags/x/` → búsqueda por palabra clave),
  así que `search_hashtag` es la búsqueda de `#tag` (resultados "top", casi todo
  reels). Un uuid de sesión por búsqueda viaja dentro del cursor. Clase `search`
  (30/h, 150/día): pocas búsquedas seguidas.

## Debugging (resumen; detalle en el [README](../README.md#troubleshooting))
- `AccountBlockedError` → parar; ver `kind`. Rate limit: esperar. Resto: resolver
  en el navegador propio y re-login o `scripts.account_state --clear`.
- `DocIdStaleError` / `OperationFailedError` con nombre de operación → `doc_id`
  rotado; DevTools → Network → copiar `doc_id`/`variables` a `ops.py`.
- `SessionExpiredError` → re-login (`python -m scripts.auth_login`).
- Chromium no arranca (WSL2/Linux) → `sudo venv/bin/python -m playwright install-deps chromium`;
  en Windows, repetir `venv\Scripts\python -m playwright install chromium`.
