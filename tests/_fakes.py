"""Test doubles shared by the unit tests. No network, no Chromium."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from instagram_sdk import ops  # noqa: E402
from instagram_sdk.endpoints.users import UserIdCache  # noqa: E402
from instagram_sdk.inpage import resolve_root  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def root_of(op_name: str, payload: Any) -> Any:
    """What ``InPageClient.graphql`` would return for a full payload."""
    op = ops.TABLE[op_name]
    for path in (op.root, *op.alt_roots):
        value = resolve_root(payload, path)
        if value is None or isinstance(value, (dict, list)):
            return value
    raise AssertionError(f"fixture has no root for {op_name}")


class FakeClient:
    """Implements the four methods endpoints use, from canned answers.

    ``graphql_answers`` / ``rest_answers`` map an op name to a list of answers
    consumed in order (an answer may be an exception to raise, or a callable
    taking the params). GraphQL answers are full payloads, reduced to the
    op's root like the real client does.
    """

    def __init__(
        self,
        *,
        graphql: dict[str, list[Any]] | None = None,
        rest: dict[str, list[Any]] | None = None,
        html: dict[str, str] | None = None,
    ) -> None:
        self._graphql = {k: list(v) for k, v in (graphql or {}).items()}
        self._rest = {k: list(v) for k, v in (rest or {}).items()}
        self._html = html or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.user_ids = UserIdCache(None)
        self.cache: dict[str, Any] = {}
        self.account = "carol"

    def _next(self, table: dict[str, list[Any]], name: str, params: dict[str, Any]) -> Any:
        answers = table.get(name)
        if not answers:
            raise AssertionError(f"unexpected call {name} {params}")
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            answer = answer(params)
        return answer

    async def bootstrap(self, *, force: bool = False) -> None:
        self.calls.append(("bootstrap", "/", {"force": force}))

    def recent_html(self, path: str, max_age_s: float) -> str | None:
        return None

    async def navigate_html(self, path: str) -> str:
        self.calls.append(("html", path, {}))
        return self._html.get(path, "<html></html>")

    async def rest(self, op: str, *, path_params: Any = None, query: Any = None) -> dict:
        params = {"path_params": dict(path_params or {}), "query": dict(query or {})}
        self.calls.append(("rest", op, params))
        return self._next(self._rest, op, params)

    async def graphql(self, op: str, **params: Any) -> Any:
        self.calls.append(("graphql", op, params))
        payload = self._next(self._graphql, op, params)
        if payload is None or not isinstance(payload, dict) or "data" not in payload:
            return payload
        return root_of(op, payload)

    def ops_called(self) -> list[str]:
        return [name for kind, name, _ in self.calls if kind != "bootstrap"]


class FakeCDP:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    async def send(self, method: str, params: Any = None) -> None:
        self.sent.append((method, params))


class FakeResponse:
    def __init__(self, status: int = 200, body: str = "") -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body


class FakePage:
    """Answers ``evaluate(FETCH_JS, arg)`` from a script of canned results."""

    def __init__(self, results: list[Any] | None = None, *, html: str = "",
                 nav_url: Callable[[str], str] | None = None) -> None:
        self.results = list(results or [])
        self.fetches: list[dict[str, Any]] = []
        self.gotos: list[str] = []
        self.url = "about:blank"
        self.html = html
        self._nav_url = nav_url
        self.nav_status = 200
        self.nav_body = ""
        self.closed = False

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.fetches.append(arg)
        if not self.results:
            raise AssertionError(f"unexpected fetch {arg and arg.get('url')}")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def goto(self, url: str, **_: Any) -> FakeResponse:
        self.gotos.append(url)
        self.url = self._nav_url(url) if self._nav_url else url
        return FakeResponse(self.nav_status, self.nav_body)

    async def content(self) -> str:
        return self.html

    def is_closed(self) -> bool:
        return self.closed


class FakeContext:
    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self._cookies = cookies if cookies is not None else {
            "csrftoken": "CSRF_COOKIE", "ig_did": "DID-1", "ds_user_id": "999",
            "sessionid": "SESSION"}

    async def cookies(self, *_: Any) -> list[dict[str, str]]:
        return [{"name": k, "value": v} for k, v in self._cookies.items()]

    async def close(self) -> None:
        pass


def fetch_result(body: Any, *, status: int = 200, url: str = "https://www.instagram.com/api/graphql",
                 content_type: str = "text/javascript; charset=utf-8", raw: str | None = None) -> dict:
    return {
        "ok": True,
        "status": status,
        "url": url,
        "redirected": False,
        "contentType": content_type,
        "text": raw if raw is not None else json.dumps(body),
    }
