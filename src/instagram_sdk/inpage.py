"""In-page client: every Instagram request is a ``fetch`` run inside the tab.

The browser attaches the session cookies (``credentials: 'include'``), so a
REST call only needs ``X-IG-App-ID``; a GraphQL call adds the page tokens read
from the HTML (``fb_dtsg``, ``lsd``, ``actorID``). That is the minimal set the
discovery verified. Nothing is signed by hand beyond that.

Every response goes through ``guard`` first. A blocking signal persists the
account state, closes its context and raises — zero retries. Without a signal
the result is classified (see ``classify_graphql``): OK, TOKEN (refresh tokens
once), STALE (doc_id or response root changed — fails with a clear message;
self-heal is a later PR), TRANSIT (one retry after a pause).

Endpoints only use ``bootstrap``, ``navigate_html``, ``rest`` and ``graphql``,
so tests drive them with a small double.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping
from urllib.parse import urlencode

from . import guard, ops
from .browser import AccountContext, is_target_closed
from .errors import (
    BrowserUnavailableError,
    DocIdStaleError,
    EndpointGoneError,
    OperationFailedError,
    SessionExpiredError,
    TokenError,
)
from .tokens import Tokens, try_extract_tokens

if TYPE_CHECKING:
    from .users import UserIdCache

log = logging.getLogger("instagram_sdk")

BASE = "https://www.instagram.com"
FETCH_TIMEOUT_MS = 30_000
NAV_TIMEOUT_MS = 30_000
TOKEN_MAX_AGE_S = 1800
TOKEN_REFRESH_MIN_AGE_S = 60
MAX_GRAPHQL_SENDS = 3
FB_NOT_LOGGED_IN = 1357001
# Headers beyond the minimal set, kept ready but off (decision D9).
HEADER_PROFILE = "minimal"
ASBD_ID = "359341"

FETCH_JS = """
async ({url, method, headers, body, timeoutMs}) => {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, {method, headers, body, credentials: 'include',
                                redirect: 'follow', signal: ctrl.signal});
    const text = await r.text();
    return {ok: true, status: r.status, url: r.url, redirected: r.redirected,
            contentType: r.headers.get('content-type') || '', text};
  } catch (e) {
    return {ok: false, error: String(e)};
  } finally {
    clearTimeout(timer);
  }
}
"""

OK = "ok"
NULL = "null"
TOKEN = "token"
STALE = "stale"
TRANSIT = "transit"
SESSION = "session"

# REST path parameters are ids; anything else could rewrite the path.
_SAFE_PATH_PARAM = re.compile(r"^[A-Za-z0-9_]{1,64}$")

OnBlock = Callable[[guard.BlockSignal], Awaitable[Exception]]


def parse_body(text: str | None) -> Any | None:
    """JSON from a response body; tolerates ``for (;;);`` and chunked lines."""
    if not text:
        return None
    t = text.lstrip()
    if t.startswith("for (;;);"):
        t = t[len("for (;;);"):]
    try:
        return json.loads(t)
    except ValueError:
        pass
    lines = t.splitlines()
    for idx, line in enumerate(lines):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            extra = sum(1 for rest in lines[idx + 1:] if rest.strip())
            if extra:
                log.debug("instagram_sdk: multi-part response, %d extra lines", extra)
            return value
    return None


_ABSENT = object()


def resolve_root(body: Any, path: tuple[str, ...]) -> Any:
    """Walk ``path``; ``_ABSENT`` if a key is missing, ``None`` if a value is null."""
    node = body
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return _ABSENT
        node = node[key]
        if node is None:
            return None
    return node


def classify_graphql(status: int, body: Any, op: ops.GraphQLOp) -> tuple[str, Any]:
    """Classify a GraphQL response that carried no blocking signal."""
    if status >= 500:
        return TRANSIT, None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, int) and not isinstance(err, bool) and "errorSummary" in body:
            return (SESSION if err == FB_NOT_LOGGED_IN else TOKEN), None
        for path in (op.root, *op.alt_roots):
            value = resolve_root(body, path)
            if value is _ABSENT:
                continue
            if value is None:
                errors = body.get("errors")
                # A null root is "does not exist" only when nothing failed.
                if op.nullable_root and body.get("data") is not None and not errors:
                    return NULL, None
                continue
            if status == 200:
                return OK, value
    if status in (400, 403):
        return TOKEN, None
    return STALE, None


def _graphql_detail(body: Any) -> str:
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return str(first.get("message") or first.get("summary") or first)[:200]
        if body.get("data") is None:
            return "response has no data"
        return "expected root missing from data"
    return "response is not JSON"


def _is_user_id(value: str | None) -> bool:
    return bool(value) and value.isdigit() and value != "0"


class InPageClient:
    def __init__(
        self,
        actx: AccountContext,
        *,
        on_block: OnBlock,
        table: Mapping[str, ops.Op] = ops.TABLE,
        user_ids: "UserIdCache | None" = None,
    ) -> None:
        self._actx = actx
        self._on_block = on_block
        self._table = table
        self.user_ids = user_ids
        self.account = actx.handle

    # ------------------------------------------------------------------
    @property
    def cache(self) -> dict[str, Any]:
        return self._actx.cache

    @property
    def pacer(self) -> Any:
        return self._actx.pacer

    def new_call(self) -> None:
        self._actx.pacer.new_call()

    async def cookies(self) -> dict[str, str]:
        try:
            jar = await self._actx.context.cookies(BASE)
        except Exception as exc:
            raise self._browser_error(exc) from exc
        return {c.get("name", ""): c.get("value", "") for c in jar}

    async def viewer_id(self) -> str | None:
        """The logged-in account's Instagram pk (what REST paths take).

        In order: ``appScopedIdentity`` from the page HTML (loading the page
        if needed); the live ``ds_user_id`` cookie; the id recorded in the
        session file / sidecar (a pk never changes, even once that cookie has
        expired and the browser dropped it). Never ``actorID``: that is the
        Facebook-side ``17841…`` id, not the pk.
        """
        tokens = self._actx.tokens
        if tokens is None or not _is_user_id(tokens.viewer_pk):
            await self.bootstrap()
            tokens = self._actx.tokens
        if tokens is not None and _is_user_id(tokens.viewer_pk):
            return tokens.viewer_pk
        live = (await self.cookies()).get("ds_user_id")
        if _is_user_id(live):
            return live
        fallback = self._actx.user_id
        return fallback if _is_user_id(fallback) else None

    def recent_html(self, path: str, max_age_s: float) -> str | None:
        last = self._actx.last_html
        if last and last[0] == path and time.monotonic() - last[2] <= max_age_s:
            return last[1]
        return None

    def _browser_error(self, exc: BaseException) -> Exception:
        if is_target_closed(exc):
            return BrowserUnavailableError(f"browser went away: {exc}", retryable=True)
        return exc if isinstance(exc, Exception) else RuntimeError(str(exc))

    async def _block(self, signal: guard.BlockSignal) -> None:
        raise await self._on_block(signal)

    # ------------------------------------------------------------------
    async def bootstrap(self, *, force: bool = False) -> None:
        """Make sure a document is loaded and tokens are fresh."""
        tokens = self._actx.tokens
        loaded = self._actx.last_html is not None and not self._on_error_page()
        if not force and loaded and tokens is not None and tokens.age_s() < TOKEN_MAX_AGE_S:
            return
        if not force and loaded and tokens is None:
            # A document is loaded but carried no tokens; REST still works.
            return
        await self.navigate_html("/")

    def _on_error_page(self) -> bool:
        try:
            return str(self._actx.page.url or "").startswith("chrome-error://")
        except Exception:
            return True

    async def _logged_out(self, tokens: Tokens | None) -> bool:
        """The login page can come back as a 200 with no redirect."""
        if tokens is not None and tokens.actor_id == "0":
            return True
        return not (await self.cookies()).get("sessionid")

    async def navigate_html(self, path: str) -> str:
        """Load ``path`` in the fixed tab and return its HTML (one request)."""
        await self._actx.pacer.before_request()
        page = self._actx.page
        try:
            response = await page.goto(BASE + path, wait_until="domcontentloaded",
                                       timeout=NAV_TIMEOUT_MS)
        except Exception as exc:
            self._actx.last_html = None  # the tab may now sit on chrome-error://
            err = self._browser_error(exc)
            if isinstance(err, BrowserUnavailableError):
                raise err from exc
            raise OperationFailedError("navigate", f"{path}: {exc}") from exc
        status = None
        try:
            status = response.status if response is not None else None
        except Exception:
            status = None
        body: Any = None
        raw = ""
        if status is not None and status >= 400 and status != 404:
            try:
                raw = await response.text()
            except Exception:
                raw = ""
            body = parse_body(raw)
        signal = guard.check_navigation(
            page.url, status, body=body if isinstance(body, dict) else None,
            text_head=raw[: guard.TEXT_HEAD_CHARS],
        )
        if signal is not None:
            await self._block(signal)
        if status is not None and status >= 400 and status != 404:
            self._actx.last_html = None
            raise OperationFailedError("navigate", f"{path}: HTTP {status}")
        try:
            html = await page.content()
        except Exception as exc:
            raise self._browser_error(exc) from exc
        fresh = try_extract_tokens(html)
        if await self._logged_out(fresh):
            await self._block(guard.BlockSignal(guard.SESSION_EXPIRED, "logged-out page"))
        if fresh is not None:
            self._actx.tokens = fresh
        self._actx.last_html = (path, html, time.monotonic())
        return html

    async def _ensure_tokens(self) -> Tokens:
        await self.bootstrap()
        tokens = self._actx.tokens
        if tokens is None:
            await self.bootstrap(force=True)
            tokens = self._actx.tokens
        if tokens is None:
            raise TokenError("tokens", "could not read fb_dtsg/lsd/actorID from the page HTML")
        return tokens

    async def _fetch(self, url: str, method: str, headers: dict[str, str],
                     body: str | None) -> dict[str, Any]:
        try:
            res = await self._actx.page.evaluate(
                FETCH_JS,
                {"url": url, "method": method, "headers": headers, "body": body,
                 "timeoutMs": FETCH_TIMEOUT_MS},
            )
        except Exception as exc:
            err = self._browser_error(exc)
            if isinstance(err, BrowserUnavailableError):
                raise err from exc
            return {"ok": False, "error": str(exc)}
        return res if isinstance(res, dict) else {"ok": False, "error": "bad evaluate result"}

    async def _guard(self, res: dict[str, Any], body: Any) -> None:
        signal = guard.check_fetch(
            int(res.get("status") or 0),
            str(res.get("url") or ""),
            bool(res.get("redirected")),
            str(res.get("contentType") or ""),
            body if isinstance(body, (dict, list)) else None,
            str(res.get("text") or "")[: guard.TEXT_HEAD_CHARS],
        )
        if signal is not None:
            await self._block(signal)

    # ------------------------------------------------------------------
    async def rest(
        self,
        op_name: str,
        *,
        path_params: Mapping[str, str] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        op = self._table[op_name]
        if not isinstance(op, ops.RestOp):
            raise TypeError(f"{op_name!r} is not a REST operation")
        await self.bootstrap()
        for key, value in (path_params or {}).items():
            if not _SAFE_PATH_PARAM.match(str(value)):
                raise ValueError(f"unsafe value for path parameter {key!r}: {value!r}")
        path = op.path.format(**(path_params or {}))
        params = {**dict(op.query), **{k: v for k, v in (query or {}).items() if v is not None}}
        url = path + ("?" + urlencode(params) if params else "")
        headers = {"X-IG-App-ID": self._app_id()}
        if HEADER_PROFILE == "weblike":
            headers.update({"X-ASBD-ID": ASBD_ID, "X-Requested-With": "XMLHttpRequest",
                            "X-IG-WWW-Claim": "0"})
        for attempt in range(2):
            await self._actx.pacer.before_request(op.budget_class)
            res = await self._fetch(url, "GET", headers, None)
            if not res.get("ok"):
                if attempt == 0:
                    await self._actx.pacer.retry_pause()
                    continue
                raise OperationFailedError(op_name, f"network error: {res.get('error')}")
            body = parse_body(res.get("text"))
            await self._guard(res, body)
            status = int(res.get("status") or 0)
            if status >= 500 and attempt == 0:
                await self._actx.pacer.retry_pause()
                continue
            if status == 404 and not isinstance(body, dict):
                raise EndpointGoneError(op_name, f"{path} answers 404 HTML (retired?)")
            if status == 200 and isinstance(body, dict):
                if body.get("status") == "fail":
                    raise OperationFailedError(op_name, str(body.get("message") or "status fail"))
                return body
            raise OperationFailedError(op_name, f"HTTP {status}")
        raise OperationFailedError(op_name, "gave up")  # pragma: no cover

    def _app_id(self) -> str:
        tokens = self._actx.tokens
        return tokens.app_id if tokens is not None else "936619743392459"

    async def graphql(self, op_name: str, **params: Any) -> Any:
        """Run a GraphQL op; return its root node (``None`` = nullable root null)."""
        op = self._table[op_name]
        if not isinstance(op, ops.GraphQLOp):
            raise TypeError(f"{op_name!r} is not a GraphQL operation")
        needs_did = "ig_did" in ops.template_holes(ops._thaw(op.variables))
        tokens = await self._ensure_tokens()
        token_refreshed = False
        transit_retried = False
        for _send in range(MAX_GRAPHQL_SENDS):
            cookies = await self.cookies()
            call_params = dict(params)
            if needs_did:
                call_params["ig_did"] = cookies.get("ig_did", "")
            variables = ops.build_variables(op, call_params)
            form = {
                "av": tokens.actor_id,
                "__user": "0",
                "__a": "1",
                "__comet_req": "7",
                "fb_dtsg": tokens.fb_dtsg,
                "jazoest": tokens.jazoest,
                "lsd": tokens.lsd,
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": op.friendly_name,
                "variables": json.dumps(variables, separators=(",", ":")),
                "server_timestamps": "true",
                "doc_id": op.doc_id,
            }
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-IG-App-ID": tokens.app_id,
                "X-CSRFToken": cookies.get("csrftoken") or tokens.csrf_html or "",
                "X-FB-LSD": tokens.lsd,
                "X-FB-Friendly-Name": op.friendly_name,
            }
            if HEADER_PROFILE == "weblike":
                headers["X-ASBD-ID"] = ASBD_ID
            await self._actx.pacer.before_request(op.budget_class)
            res = await self._fetch(op.endpoint, "POST", headers, urlencode(form))
            if not res.get("ok"):
                if not transit_retried:
                    transit_retried = True
                    await self._actx.pacer.retry_pause()
                    continue
                raise OperationFailedError(op_name, f"network error: {res.get('error')}")
            body = parse_body(res.get("text"))
            await self._guard(res, body)
            status = int(res.get("status") or 0)
            kind, value = classify_graphql(status, body, op)
            if kind == OK:
                return value
            if kind == NULL:
                return None
            if kind == SESSION:
                await self._block(guard.BlockSignal(guard.SESSION_EXPIRED, "error 1357001"))
            if kind == TRANSIT and not transit_retried:
                transit_retried = True
                await self._actx.pacer.retry_pause()
                continue
            # The exact shapes of token/stale errors are not observed yet, so be
            # tolerant: a stale-looking failure with tokens older than a minute
            # first refreshes the tokens (one normal navigation), then gives up.
            if not token_refreshed and (
                kind == TOKEN
                or (kind == STALE and tokens.age_s() > TOKEN_REFRESH_MIN_AGE_S)
            ):
                token_refreshed = True
                await self.bootstrap(force=True)
                if self._actx.tokens is None:
                    raise TokenError(op_name, "tokens missing after refresh")
                tokens = self._actx.tokens
                continue
            detail = _graphql_detail(body)
            if kind == TOKEN:
                raise TokenError(op_name, f"tokens rejected (HTTP {status}): {detail}")
            if kind == TRANSIT:
                raise OperationFailedError(op_name, f"HTTP {status} after retry")
            raise DocIdStaleError(
                op_name,
                f"{detail} (HTTP {status}). The doc_id or response root of "
                f"{op.friendly_name} probably changed; update instagram_sdk/ops.py "
                "(README, Maintenance).",
            )
        raise OperationFailedError(op_name, f"no usable answer after {MAX_GRAPHQL_SENDS} sends")
