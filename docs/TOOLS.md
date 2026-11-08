# TOOLS — instagram-mcp

Referencia exhaustiva de las **9 tools** que expone `server.py` (8 read-only +
`download_media`). Todas devuelven JSON (dataclasses → dict); `download_media`
además devuelve objetos `Image` inline. Router y arquitectura en `../AGENTS.md`.

Todo `limit` tiene un tope de 50: un valor mayor se recorta, no da error.

| Tool | Parámetros | Reconocido por |
|------|-----------|----------------|
| `auth_status` | — | sin red; lee el fichero de sesión |
| `get_saved_posts` | `collection_id?`, `limit=24` | `FR_SAVED` / `FR_COLLECTION_FEED` (REST) |
| `get_saved_collections` | — | `FN_COLLECTIONS` (GraphQL) |
| `get_home_feed` | `limit=15` | `FN_TIMELINE` + HTML embebido del primer batch |
| `get_user_profile` | `username` | `FN_PROFILE` |
| `get_user_posts` | `username`, `limit=24`, `include_reels=false` | `FN_PROFILE_POSTS` |
| `get_post` | `id_or_url` | HTML embebido, clave `EMBED_POST_KEY` |
| `get_user_reels` | `username`, `limit=12` | `FN_REELS` (tupla de 2 friendly names) |
| `download_media` | `id_or_url`, `indices?`, `download_videos=false` | re-fetch del post + httpx CDN |

## Notas
- **Saved son privados** → navegan bajo `/<handle>/saved/...`; `<handle>` se deriva
  del nombre del fichero de sesión (`_resolve_own_handle`).
- **`download_media`** re-fetchea el post fresco antes de descargar porque las URLs
  del CDN de Instagram son firmadas y efímeras. Si devuelve `skipped` con `no_url`,
  esa media ya no tiene URL válida. Los vídeos sólo bajan con `download_videos=True`.
- Las constantes `FN_*` / `FR_*` / `EMBED_POST_KEY` viven en `endpoints/_ids.py`
  (único punto a parchear ante rotación — ver [README](../README.md#maintenance--when-instagram-rotates-endpoints)).

## Debugging (resumen; detalle en el [README](../README.md#troubleshooting))
- `get_saved_posts` devuelve `[]` tras ~30 s → `--username` no era el de la cuenta;
  repetir el login con el usuario exacto.
- Otra tool devuelve `[]` → friendly-name de GraphQL rotado; DevTools → Network →
  `fb_api_req_friendly_name` → parchear `FN_*`/`FR_*` → fixture anonimizada.
- `SessionExpiredError` → re-login (`python -m scripts.auth_login`); fichero debe
  llamarse `<handle>_instagram_state.json`.
- Chromium no arranca (WSL2/Linux) → `sudo venv/bin/python -m playwright install-deps chromium`;
  en Windows, repetir `venv\Scripts\python -m playwright install chromium`.
