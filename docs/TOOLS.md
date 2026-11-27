# TOOLS — instagram-mcp

Referencia exhaustiva de las **9 tools** que expone `server.py` (8 read-only +
`download_media`). Todas devuelven JSON (dataclasses → dict); `download_media`
además devuelve objetos `Image` inline. Router y arquitectura en `../AGENTS.md`.

Todo `limit` tiene un tope de 50: un valor mayor se recorta, no da error. Cada
llamada hace como mucho 20 peticiones a Instagram y 8 páginas.

| Tool | Parámetros | Operaciones (`ops.py`) |
|------|-----------|----------------|
| `auth_status` | — | sin red; lee el fichero de sesión y el estado de la cuenta (`state`, `block`) |
| `get_saved_posts` | `collection_id?`, `limit=24` | REST `saved_posts`; con colección, REST `collection_feed` (respaldo GraphQL `saved_collection`) |
| `get_saved_collections` | — | GraphQL `saved_collections` (raíz `viewer.collections_unified_with_auto_collections`) |
| `get_home_feed` | `limit=15` | HTML de `/` (o `home_first`) + `home_page` |
| `get_user_profile` | `username` | `search_box` (username → id, coincidencia exacta; respaldo `profile_posts_first`) + `profile_info` |
| `get_user_posts` | `username`, `limit=24`, `include_reels=false` | `profile_posts_first` + `profile_posts_page`; reels del autor para las vistas |
| `get_post` | `id_or_url` | HTML de `/p/<code>/`, clave `EMBED_POST_KEY` (+ reels del autor para las vistas) |
| `get_user_reels` | `username`, `limit=12` | `search_box` + `profile_reels_first` + `profile_reels_page` |
| `download_media` | `id_or_url`, `indices?`, `download_videos=false` | re-fetch del post (sin vistas) + httpx CDN fuera del lock |

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
- **`download_media`** re-fetchea el post fresco antes de descargar porque las URLs
  del CDN de Instagram son firmadas y efímeras; escribe vía `.part` + rename.

## Debugging (resumen; detalle en el [README](../README.md#troubleshooting))
- `AccountBlockedError` → parar; ver `kind`. Rate limit: esperar. Resto: resolver
  en el navegador propio y re-login o `scripts.account_state --clear`.
- `DocIdStaleError` / `OperationFailedError` con nombre de operación → `doc_id`
  rotado; DevTools → Network → copiar `doc_id`/`variables` a `ops.py`.
- `SessionExpiredError` → re-login (`python -m scripts.auth_login`).
- Chromium no arranca (WSL2/Linux) → `sudo venv/bin/python -m playwright install-deps chromium`;
  en Windows, repetir `venv\Scripts\python -m playwright install chromium`.
