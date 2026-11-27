"""username → numeric user id.

``web_profile_info`` is retired (404 HTML), and the profile query wants the
numeric id, so it is resolved here, in order:

1. the on-disk cache ``<state>/user_ids.json`` (30 days);
2. the search box query, accepting only an **exact** username match (search
   is fuzzy: no exact match means no answer);
3. the first grid page (``profile_posts_first``), whose nodes carry
   ``user.pk`` — useless for accounts with no posts or private ones.

``None`` when nothing matched.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ..accounts import _atomic_write_json
from ._ids import normalize_username

log = logging.getLogger("instagram_sdk")

USER_ID_TTL_S = 30 * 24 * 3600


class UserIdCache:
    """Small persistent map; in memory only when ``path`` is ``None``."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._data: dict[str, dict[str, Any]] | None = None

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._data is None:
            self._data = {}
            if self._path is not None:
                try:
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        self._data = {k: v for k, v in raw.items() if isinstance(v, dict)}
                except FileNotFoundError:
                    pass
                except (OSError, ValueError):
                    log.warning("instagram_sdk: unreadable %s; ignoring", self._path)
        return self._data

    def get(self, username: str) -> str | None:
        entry = self._load().get(username.casefold())
        if not entry:
            return None
        if time.time() - float(entry.get("at", 0)) > USER_ID_TTL_S:
            return None
        pk = entry.get("pk")
        return str(pk) if pk else None

    def put(self, username: str, pk: str) -> None:
        self._load()[username.casefold()] = {"pk": str(pk), "at": time.time()}
        self._save()

    def drop(self, username: str) -> None:
        if self._load().pop(username.casefold(), None) is not None:
            self._save()

    def _save(self) -> None:
        if self._path is None or self._data is None:
            return
        try:
            _atomic_write_json(self._path, self._data)
        except OSError:
            log.warning("instagram_sdk: could not write %s", self._path)


def _exact(users: Any, username: str) -> str | None:
    want = username.casefold()
    for row in users or []:
        user = row.get("user") if isinstance(row, dict) else None
        if isinstance(user, dict) and str(user.get("username", "")).casefold() == want:
            pk = user.get("pk") or user.get("id")
            if pk:
                return str(pk)
    return None


async def resolve_user_id(ig: Any, username: str, *, use_cache: bool = True) -> str | None:
    handle = normalize_username(username)
    cache: UserIdCache | None = getattr(ig, "user_ids", None)
    if use_cache and cache is not None:
        cached = cache.get(handle)
        if cached:
            return cached

    pk: str | None = None
    root = await ig.graphql("search_box", query=handle)
    if isinstance(root, dict):
        pk = _exact(root.get("users"), handle)

    if pk is None:
        conn = await ig.graphql("profile_posts_first", username=handle)
        for edge in (conn or {}).get("edges") or []:
            node = (edge or {}).get("node") or {}
            user = node.get("user") or {}
            if str(user.get("username", "")).casefold() == handle.casefold():
                pk = str(user.get("pk") or user.get("id") or "") or None
            break

    if pk and cache is not None:
        cache.put(handle, pk)
    return pk
