"""Self-heal of GraphQL ``doc_id``/variables when Instagram rotates them.

Two pieces:

``OpsStore``
    The repo table (``ops.TABLE``) with a persistent cache laid over it:
    ``<state>/ops_cache.json``, outside the repo, keyed by ``friendly_name``
    and global (not per account): a ``doc_id`` goes with the web revision. A
    cache entry wins only when it is at least as recent as ``ops.TABLE_DATE``,
    so updating the table in the repo invalidates older cures without
    deleting anything. Captured variables are re-templated with the repo's
    holes before saving; a capture missing a required hole is another
    variant of the query and is dropped whole. A corrupt cache is ignored
    (logged) and the table is used.

``Healer``
    Runs when ``InPageClient.graphql`` classifies an answer as STALE (no
    blocking signal: ``guard`` ran first and would have raised). Layers,
    cheapest first:

    0. ``queryID`` of the Relay preloaders (``adp_<Query>RelayPreloader_…``)
       in the HTML already loaded in the fixed tab — no request;
    1. the real page with JS (``op.heal.path``) in a second tab of the same
       context, capturing the GraphQL POSTs the web app itself sends
       (``doc_id`` + variables of every op seen), plus the preloaders of
       that page's HTML;
    1b. the ``<Name>_instagramRelayOperation`` modules of the JS bundles
       that page downloaded (``doc_id`` only).

    A blocking signal on the heal page (navigation or any of the web's own
    GraphQL answers) stops the account like anywhere else. Limits: one cure
    per op every 6 h, two per account per day; a cure costs 10 requests of
    the budget. ``INSTAGRAM_SELF_HEAL=false`` turns it off.

Nothing here logs or stores tokens, cookies or request bodies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import string
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from . import guard, ops
from .accounts import atomic_write_json

if TYPE_CHECKING:
    from .inpage import InPageClient

log = logging.getLogger("instagram_sdk")

CACHE_VERSION = 1
HEAL_COST = 10
HEAL_WAIT_S = 20.0
HEAL_NAV_TIMEOUT_MS = 30_000
SCROLL_STEPS = 3
SCROLL_PAUSE_S: tuple[float, float] = (3.0, 6.5)
OP_HEAL_INTERVAL_S = 6 * 3600
ACCOUNT_HEALS_PER_DAY = 2
DEBUG_KEEP = 5
HEAL_BLOCKED_RESOURCES = frozenset({"image", "media", "font", "stylesheet"})

# Outcomes of the last ``Healer.heal`` (for the error message).
HEALED = "healed"
UNCHANGED = "unchanged"
NOT_FOUND = "not_found"
LIMITED = "limited"
DISABLED = "disabled"

_PRELOADER = re.compile(r"adp_(\w+?)RelayPreloader_")
_QUERY_ID = re.compile(r'"queryID"\s*:\s*"(\d+)"')
_PRELOADER_WINDOW = 2000
_BUNDLE_OP = re.compile(
    r'__d\(\s*"(\w+?)_instagramRelayOperation"\s*,\s*\[\]\s*,\s*\(?\s*function\s*\([^)]*\)'
    r'\s*\{\s*(?:"use strict";\s*)?\w+\.exports\s*=\s*"(\d+)"'
)
_DIGITS = re.compile(r"^\d{5,25}$")
# Observed 2026-11-28 with a made-up doc_id (HTTP 200, no ``data`` key):
# {"errors":[{"message":"The GraphQL document with ID <n> was not found.",
#   "severity":"CRITICAL"}],"extensions":{"is_final":true}}
_DOC_NOT_FOUND = re.compile(r"graphql document with id \d+ was not found", re.I)


def is_unknown_doc_id(body: Any) -> bool:
    """True for Instagram's answer to a ``doc_id`` it does not know (rotated)."""
    if not isinstance(body, dict):
        return False
    errors = body.get("errors")
    if not isinstance(errors, list):
        return False
    return any(isinstance(e, dict) and isinstance(e.get("message"), str)
               and _DOC_NOT_FOUND.search(e["message"]) for e in errors)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def extract_preloaded_query_ids(html: str) -> dict[str, str]:
    """``{query name: queryID}`` from Relay preloaders in a page's HTML.

    Best effort: for every ``adp_<Name>RelayPreloader_`` the first ``"queryID"``
    in the next characters is taken. Nothing matching → ``{}``.
    """
    found: dict[str, str] = {}
    for match in _PRELOADER.finditer(html or ""):
        name = match.group(1)
        if name in found:
            continue
        window = html[match.end(): match.end() + _PRELOADER_WINDOW]
        qid = _QUERY_ID.search(window)
        if qid:
            found[name] = qid.group(1)
    return found


def extract_bundle_doc_ids(js: str) -> dict[str, str]:
    """``{operation name: doc_id}`` from ``*_instagramRelayOperation`` modules."""
    return {m.group(1): m.group(2) for m in _BUNDLE_OP.finditer(js or "")}


def parse_graphql_post(post_data: str | None) -> tuple[str, str, Any] | None:
    """``(friendly_name, doc_id, variables)`` of a web GraphQL POST body."""
    if not post_data:
        return None
    try:
        form = parse_qs(post_data, keep_blank_values=True)
    except Exception:
        return None
    friendly = (form.get("fb_api_req_friendly_name") or [""])[0]
    doc_id = (form.get("doc_id") or [""])[0]
    if not friendly or not _DIGITS.match(doc_id):
        return None
    raw = (form.get("variables") or [""])[0]
    try:
        variables = json.loads(raw) if raw else None
    except ValueError:
        variables = None
    return friendly, doc_id, variables if isinstance(variables, dict) else None


def retemplate(template: Any, captured: Any) -> Any | None:
    """Put the template's ``"$x"`` holes back into captured variables.

    Everything else in ``captured`` (new or changed flags, ``data`` fields) is
    adopted as is. ``None`` when a path holding a required hole is missing:
    the capture is another variant of the query.
    """
    if not isinstance(template, Mapping):
        return captured
    if not isinstance(captured, Mapping):
        return None if _required_holes(template) else captured
    out = {k: v for k, v in captured.items()}
    for key, tvalue in template.items():
        hole = ops._hole(tvalue)
        if hole is not None:
            if key in captured:
                out[key] = tvalue
            elif not hole[1]:
                return None
            continue
        if isinstance(tvalue, Mapping) and ops.template_holes(tvalue):
            if key not in captured:
                if _required_holes(tvalue):
                    return None
                continue
            sub = retemplate(tvalue, captured[key])
            if sub is None:
                return None
            out[key] = sub
    return out


def _required_holes(template: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(template, Mapping):
        for value in template.values():
            found |= _required_holes(value)
    elif isinstance(template, (list, tuple)):
        for value in template:
            found |= _required_holes(value)
    else:
        hole = ops._hole(template)
        if hole and not hole[1]:
            found.add(hole[0])
    return found


def _utc_now_iso(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# OpsStore
# --------------------------------------------------------------------------
class OpsStore:
    def __init__(
        self,
        path: Path | None,
        *,
        table: Mapping[str, ops.Op] = ops.TABLE,
        table_date: str = ops.TABLE_DATE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self._table = table
        self._table_date = table_date
        self._clock = clock
        self._data: dict[str, Any] | None = None
        self._mtime: int | None = None
        self._by_friendly = {
            op.friendly_name: op for op in table.values() if isinstance(op, ops.GraphQLOp)
        }

    # -- file ------------------------------------------------------------
    def _empty(self) -> dict[str, Any]:
        return {"version": CACHE_VERSION, "ops": {}, "heals": {"ops": {}, "accounts": {}}}

    def _load(self) -> dict[str, Any]:
        if self.path is None:
            if self._data is None:
                self._data = self._empty()
            return self._data
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if self._data is not None and mtime == self._mtime:
            return self._data
        data = self._empty()
        if mtime is not None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
                    raise ValueError("unknown format")
                if isinstance(raw.get("ops"), dict):
                    data["ops"] = raw["ops"]
                heals = raw.get("heals")
                if isinstance(heals, dict):
                    for key in ("ops", "accounts"):
                        if isinstance(heals.get(key), dict):
                            data["heals"][key] = heals[key]
            except (OSError, ValueError) as exc:
                log.warning("instagram_sdk: ignoring unreadable %s (%s)", self.path, exc)
        self._data, self._mtime = data, mtime
        return data

    def _save(self) -> None:
        if self.path is None or self._data is None:
            return
        try:
            atomic_write_json(self.path, self._data)
            self._mtime = self.path.stat().st_mtime_ns
        except OSError as exc:
            log.warning("instagram_sdk: could not write %s (%s)", self.path, exc)

    # -- ops -------------------------------------------------------------
    def get(self, op: ops.GraphQLOp) -> ops.GraphQLOp:
        """The repo op with a valid, recent enough cache entry laid over it."""
        entry = self._load()["ops"].get(op.friendly_name)
        if not isinstance(entry, dict):
            return op
        updated = str(entry.get("updated_at") or "")
        if updated[:10] < self._table_date:
            return op
        doc_id = str(entry.get("doc_id") or "")
        if not _DIGITS.match(doc_id):
            return op
        base = self._table.get(op.name, op)
        repo_vars = ops._thaw(base.variables)
        variables = entry.get("variables")
        if isinstance(variables, dict) and self._holes_ok(repo_vars, variables):
            return replace(op, doc_id=doc_id, variables=ops._freeze(variables))
        return replace(op, doc_id=doc_id, variables=base.variables)

    @staticmethod
    def _holes_ok(repo: Any, cached: Any) -> bool:
        return (_required_holes(repo) <= ops.template_holes(cached)
                and ops.template_holes(cached) <= ops.template_holes(repo))

    def known(self, friendly_name: str) -> ops.GraphQLOp | None:
        return self._by_friendly.get(friendly_name)

    def update(
        self,
        friendly_name: str,
        *,
        doc_id: str,
        variables: Mapping[str, Any] | None,
        source: str,
        client_revision: str | None = None,
    ) -> bool:
        """Store a capture for a registered op. True if doc_id or variables changed."""
        op = self._by_friendly.get(friendly_name)
        if op is None or not _DIGITS.match(str(doc_id)):
            return False
        current = self.get(op)
        repo_vars = ops._thaw(op.variables)
        new_vars: Any = None
        if variables is not None:
            new_vars = retemplate(repo_vars, variables)
            if new_vars is None:
                log.warning("instagram_sdk: capture of %s is another variant (a required "
                            "variable is missing); ignored", friendly_name)
                return False
        changed = str(doc_id) != current.doc_id or (
            new_vars is not None and new_vars != ops._thaw(current.variables)
        )
        entry: dict[str, Any] = {
            "doc_id": str(doc_id),
            "source": source,
            "updated_at": _utc_now_iso(self._clock),
        }
        if client_revision:
            entry["client_revision"] = str(client_revision)
        if new_vars is not None:
            entry["variables"] = new_vars
        else:
            # Keep the variables a previous cure learned, if any.
            old = self._load()["ops"].get(friendly_name)
            if isinstance(old, dict) and isinstance(old.get("variables"), dict):
                entry["variables"] = old["variables"]
        self._load()["ops"][friendly_name] = entry
        self._save()
        return changed

    # -- limits ------------------------------------------------------------
    def heal_refusal(self, op_name: str, account: str) -> str | None:
        """Why a cure may not run now, or ``None``."""
        now = self._clock()
        heals = self._load()["heals"]
        last = heals["ops"].get(op_name)
        if isinstance(last, (int, float)) and now - last < OP_HEAL_INTERVAL_S:
            return f"{op_name} was already healed less than 6 h ago"
        recent = [t for t in heals["accounts"].get(account.casefold(), [])
                  if isinstance(t, (int, float)) and now - t < 86400]
        if len(recent) >= ACCOUNT_HEALS_PER_DAY:
            return f"{ACCOUNT_HEALS_PER_DAY} self-heals already ran today for this account"
        return None

    def record_heal(self, op_name: str, account: str) -> None:
        now = self._clock()
        heals = self._load()["heals"]
        heals["ops"][op_name] = now
        key = account.casefold()
        heals["accounts"][key] = [
            t for t in heals["accounts"].get(key, [])
            if isinstance(t, (int, float)) and now - t < 86400
        ] + [now]
        self._save()


# --------------------------------------------------------------------------
# Healer
# --------------------------------------------------------------------------
class _Capture:
    def __init__(self) -> None:
        self.requests: dict[str, tuple[str, Any]] = {}
        self.bundle_ids: dict[str, str] = {}
        self.signal: guard.BlockSignal | None = None
        self.tasks: set[asyncio.Task] = set()
        # Requests of the heal page that reached www.instagram.com.
        self.sent = 0
        self.kinds: dict[str, int] = {}


class Healer:
    def __init__(
        self,
        store: OpsStore,
        *,
        enabled: bool = True,
        debug_dir: Path | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        uniform: Callable[[float, float], float] = random.uniform,
        wait_s: float = HEAL_WAIT_S,
    ) -> None:
        self.store = store
        self.enabled = enabled
        self.debug_dir = debug_dir
        self._sleep = sleep or asyncio.sleep
        self._uniform = uniform
        self._wait_s = wait_s
        self.last_outcome: str | None = None
        self.last_sent = 0

    # ------------------------------------------------------------------
    async def heal(self, client: "InPageClient", op: ops.GraphQLOp,
                   params: Mapping[str, Any]) -> bool:
        """Try to rediscover ``op``. True if its doc_id or variables changed."""
        self.last_outcome = DISABLED
        if not self.enabled:
            return False
        actx = client._actx
        # Layer 0: preloaders of the HTML already in the fixed tab (free).
        last = actx.last_html
        if last is not None and self._from_preloaders(op, last[1], "heal:preloader", actx):
            self.last_outcome = HEALED
            return True
        refusal = self.store.heal_refusal(op.name, client.account)
        if refusal is not None:
            log.warning("instagram_sdk: self-heal of %s skipped: %s", op.name, refusal)
            self.last_outcome = LIMITED
            return False
        path = self._heal_path(client, op, params)
        self.store.record_heal(op.name, client.account)
        await actx.pacer.before_request(op.budget_class, cost=HEAL_COST)
        log.warning("instagram_sdk: self-heal of %s via %s", op.name, path)
        capture, html = await self._load_page(client, path, op)
        if capture.signal is not None:
            await client._block(capture.signal)
        revision = actx.tokens.client_revision if actx.tokens is not None else None
        changed = False
        seen = False
        for friendly, (doc_id, variables) in capture.requests.items():
            if self.store.known(friendly) is None:
                continue
            did = self.store.update(friendly, doc_id=doc_id, variables=variables,
                                    source="heal:xhr", client_revision=revision)
            if friendly == op.friendly_name:
                seen, changed = True, did
        if not seen and html and self._from_preloaders(op, html, "heal:page_preloader", actx):
            seen = changed = True
        if not seen:
            doc_id = capture.bundle_ids.get(op.friendly_name)
            if doc_id:
                seen = True
                changed = self.store.update(op.friendly_name, doc_id=doc_id, variables=None,
                                            source="heal:bundle", client_revision=revision)
        self.last_outcome = HEALED if changed else (UNCHANGED if seen else NOT_FOUND)
        log.warning("instagram_sdk: self-heal of %s: %s (%d requests to instagram.com %s, "
                    "%d GraphQL ops seen)", op.name, self.last_outcome, capture.sent,
                    capture.kinds, len(capture.requests))
        self.last_sent = capture.sent
        return changed

    def _from_preloaders(self, op: ops.GraphQLOp, html: str, source: str, actx: Any) -> bool:
        qid = extract_preloaded_query_ids(html).get(op.friendly_name)
        if not qid or qid == op.doc_id:
            return False
        revision = actx.tokens.client_revision if actx.tokens is not None else None
        return self.store.update(op.friendly_name, doc_id=qid, variables=None,
                                 source=source, client_revision=revision)

    def _heal_path(self, client: "InPageClient", op: ops.GraphQLOp,
                   params: Mapping[str, Any]) -> str:
        """The page to load; ``/`` (bundles only) when the op has no recipe."""
        if op.heal is None:
            return "/"
        values: dict[str, Any] = {k: v for k, v in params.items() if v is not None}
        values["viewer"] = client.account
        if "username" not in values and values.get("id"):
            name = _username_for(client.user_ids, str(values["id"]))
            if name:
                values["username"] = name
        fields = {f for _, f, _, _ in string.Formatter().parse(op.heal.path) if f}
        if not fields <= set(values):
            return "/"
        for f in fields:
            if not re.match(r"^[A-Za-z0-9_.]{1,64}$", str(values[f])):
                return "/"
        return op.heal.path.format(**values)

    async def _load_page(self, client: "InPageClient", path: str,
                         op: ops.GraphQLOp) -> tuple[_Capture, str]:
        from .inpage import BASE, parse_body

        actx = client._actx
        capture = _Capture()
        html = ""
        page = await actx.context.new_page()
        try:
            if actx.ua_override:
                cdp = await actx.context.new_cdp_session(page)
                await cdp.send("Emulation.setUserAgentOverride", actx.ua_override)

            async def _route(route: Any) -> None:
                # The web's GraphQL POSTs are captured here. The one we are
                # healing is aborted: its shape is all we need and our retry
                # sends it anyway. The rest go through so the page behaves
                # like the web (later queries can depend on earlier ones).
                try:
                    request = route.request
                    if request.resource_type in HEAL_BLOCKED_RESOURCES:
                        await route.abort()
                        return
                    parsed = None
                    if (request.method == "POST"
                            and urlparse(request.url).path in ops.ENDPOINTS):
                        parsed = parse_graphql_post(request.post_data)
                    if parsed is not None:
                        friendly, doc_id, variables = parsed
                        capture.requests.setdefault(friendly, (doc_id, variables))
                        if friendly == op.friendly_name:
                            await route.abort()
                            return
                    if (urlparse(request.url).hostname or "").endswith("instagram.com"):
                        capture.sent += 1
                        kind = "graphql" if parsed is not None else request.resource_type
                        capture.kinds[kind] = capture.kinds.get(kind, 0) + 1
                    await route.fallback()
                except Exception:
                    pass

            await page.route("**/*", _route)

            async def read_response(response: Any) -> None:
                try:
                    url = response.url
                    rtype = response.request.resource_type
                    if urlparse(url).path in ops.ENDPOINTS:
                        text = await response.text()
                        body = parse_body(text)
                        signal = guard.check_fetch(
                            int(response.status), url, False,
                            str(response.headers.get("content-type", "")),
                            body if isinstance(body, (dict, list)) else None,
                            (text or "")[: guard.TEXT_HEAD_CHARS],
                        )
                        if signal is not None and capture.signal is None:
                            capture.signal = signal
                    elif rtype == "script":
                        capture.bundle_ids.update(extract_bundle_doc_ids(await response.text()))
                except Exception:
                    return

            def on_response(response: Any) -> None:
                task = asyncio.get_running_loop().create_task(read_response(response))
                capture.tasks.add(task)
                task.add_done_callback(capture.tasks.discard)

            page.on("response", on_response)
            response = await page.goto(BASE + path, wait_until="domcontentloaded",
                                       timeout=HEAL_NAV_TIMEOUT_MS)
            status = None
            try:
                status = response.status if response is not None else None
            except Exception:
                status = None
            signal = guard.check_navigation(page.url, status)
            if signal is not None:
                capture.signal = signal
                return capture, html
            await self._wait_for(capture, op.friendly_name, self._wait_s)
            if op.heal is not None and op.heal.action == "scroll":
                for _ in range(SCROLL_STEPS):
                    if op.friendly_name in capture.requests or capture.signal is not None:
                        break
                    await self._sleep(self._uniform(*SCROLL_PAUSE_S))
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await self._wait_for(capture, op.friendly_name, 5.0)
                    signal = guard.check_navigation(page.url, None)
                    if signal is not None:
                        capture.signal = signal
                        break
            try:
                html = await page.content()
            except Exception:
                html = ""
            if capture.tasks:
                await asyncio.wait(list(capture.tasks), timeout=10)
        except Exception as exc:
            log.warning("instagram_sdk: self-heal page failed: %s", exc)
        finally:
            try:
                await page.close()
            except Exception:
                pass
            for task in list(capture.tasks):
                task.cancel()
        return capture, html

    async def _wait_for(self, capture: _Capture, friendly: str, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if friendly in capture.requests or capture.signal is not None:
                return
            await self._sleep(0.5)

    # ------------------------------------------------------------------
    def dump_debug(self, op_name: str, body: Any) -> Path | None:
        """Keep the response body of a failed op (no headers, no request, no tokens)."""
        if self.debug_dir is None or body is None:
            return None
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_]", "_", op_name)[:64]
            path = self.debug_dir / f"{safe}-{int(time.time() * 1000)}.json"
            path.write_text(json.dumps(body, ensure_ascii=False)[:200_000], encoding="utf-8")
            try:
                path.chmod(0o600)
            except OSError:
                pass
            dumps = sorted(self.debug_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
            for old in dumps[:-DEBUG_KEEP]:
                old.unlink(missing_ok=True)
            return path
        except OSError as exc:
            log.warning("instagram_sdk: could not write debug dump (%s)", exc)
            return None


def _username_for(user_ids: Any, pk: str) -> str | None:
    """Reverse lookup in ``UserIdCache`` (the caller resolved it just before)."""
    try:
        data = user_ids._load()  # noqa: SLF001 - same package, read only
    except Exception:
        return None
    for name, entry in (data or {}).items():
        if isinstance(entry, dict) and str(entry.get("pk")) == pk:
            return name
    return None
