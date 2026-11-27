"""streamable-http transport: config, bearer token and allowed hosts. No network.

The ASGI app is exercised in-process with Starlette's TestClient (httpx
transport, no socket). A throwaway FastMCP instance is used per test because
FastMCP's session manager can only be started once.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mcp.server.fastmcp import FastMCP  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from instagram_sdk import transport  # noqa: E402
from instagram_sdk.transport import (  # noqa: E402
    HTTP,
    STDIO,
    TransportConfigError,
    TransportSettings,
    configure_http,
)

TOKEN = "t" * 40
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _http_env(**extra: str) -> dict[str, str]:
    env = {"INSTAGRAM_MCP_TRANSPORT": "streamable-http", "INSTAGRAM_MCP_BEARER_TOKEN": TOKEN}
    env.update(extra)
    return env


class FromEnvTests(unittest.TestCase):
    def test_stdio_is_the_default(self) -> None:
        s = TransportSettings.from_env({})
        self.assertEqual(s.transport, STDIO)
        self.assertFalse(s.is_http)

    def test_stdio_ignores_http_variables(self) -> None:
        s = TransportSettings.from_env({"INSTAGRAM_MCP_TRANSPORT": "stdio", "INSTAGRAM_MCP_PORT": "x"})
        self.assertEqual(s.transport, STDIO)

    def test_unknown_transport_is_an_error(self) -> None:
        with self.assertRaises(TransportConfigError):
            TransportSettings.from_env({"INSTAGRAM_MCP_TRANSPORT": "sse"})

    def test_http_without_token_refuses_to_start(self) -> None:
        with self.assertRaisesRegex(TransportConfigError, "token"):
            TransportSettings.from_env({"INSTAGRAM_MCP_TRANSPORT": "http"})

    def test_short_token_is_refused(self) -> None:
        with self.assertRaisesRegex(TransportConfigError, "too short"):
            TransportSettings.from_env(_http_env(INSTAGRAM_MCP_BEARER_TOKEN="abc"))

    def test_token_and_token_file_together_are_refused(self) -> None:
        with self.assertRaisesRegex(TransportConfigError, "not both"):
            TransportSettings.from_env(_http_env(INSTAGRAM_MCP_BEARER_TOKEN_FILE="/x"))

    def test_token_file_is_read_and_stripped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token"
            path.write_text(TOKEN + "\n", encoding="utf-8")
            s = TransportSettings.from_env(
                {"INSTAGRAM_MCP_TRANSPORT": "http", "INSTAGRAM_MCP_BEARER_TOKEN_FILE": str(path)}
            )
        self.assertEqual(s.token, TOKEN)
        self.assertNotIn(TOKEN, repr(s))

    def test_missing_token_file_is_an_error(self) -> None:
        with self.assertRaisesRegex(TransportConfigError, "Cannot read"):
            TransportSettings.from_env(
                {"INSTAGRAM_MCP_TRANSPORT": "http", "INSTAGRAM_MCP_BEARER_TOKEN_FILE": "/nonexistent/t"}
            )

    def test_http_settings(self) -> None:
        s = TransportSettings.from_env(_http_env(
            INSTAGRAM_MCP_HOST="0.0.0.0",
            INSTAGRAM_MCP_PORT="8765",
            INSTAGRAM_MCP_ALLOWED_HOSTS="192.168.1.48:*, nas.lan:8765",
        ))
        self.assertEqual((s.transport, s.host, s.port, s.path), (HTTP, "0.0.0.0", 8765, "/mcp"))
        self.assertIn("192.168.1.48:*", s.allowed_hosts)
        self.assertIn("nas.lan:8765", s.allowed_hosts)
        self.assertIn("localhost:*", s.allowed_hosts)  # healthcheck / local client

    def test_wildcard_host_is_refused(self) -> None:
        with self.assertRaisesRegex(TransportConfigError, r"\*"):
            TransportSettings.from_env(_http_env(INSTAGRAM_MCP_ALLOWED_HOSTS="*"))

    def test_bad_port_is_an_error(self) -> None:
        for port in ("abc", "0", "70000"):
            with self.assertRaises(TransportConfigError):
                TransportSettings.from_env(_http_env(INSTAGRAM_MCP_PORT=port))


class HttpAppTests(unittest.TestCase):
    def _client(self, **env: str) -> TestClient:
        app_mcp = FastMCP("test", log_level="WARNING")

        @app_mcp.tool()
        def ping() -> str:
            return "pong"

        settings = TransportSettings.from_env(_http_env(**env))
        app = configure_http(app_mcp, settings)
        return TestClient(app, base_url="http://192.168.1.48:8000")

    def test_healthz_needs_no_token(self) -> None:
        with self._client(INSTAGRAM_MCP_ALLOWED_HOSTS="192.168.1.48:*") as c:
            r = c.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_missing_token_is_401(self) -> None:
        with self._client(INSTAGRAM_MCP_ALLOWED_HOSTS="192.168.1.48:*") as c:
            r = c.post("/mcp", json=INIT, headers=MCP_HEADERS)
        self.assertEqual(r.status_code, 401)
        self.assertIn("Bearer", r.headers.get("www-authenticate", ""))

    def test_wrong_token_is_401(self) -> None:
        with self._client(INSTAGRAM_MCP_ALLOWED_HOSTS="192.168.1.48:*") as c:
            r = c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": "Bearer " + "x" * 40})
            r2 = c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": TOKEN})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r2.status_code, 401)

    def test_host_not_allowed_is_rejected_even_with_token(self) -> None:
        with self._client() as c:  # only localhost allowed
            r = c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 421)

    def test_token_and_allowed_host_initialize(self) -> None:
        with self._client(INSTAGRAM_MCP_ALLOWED_HOSTS="192.168.1.48:*") as c:
            r = c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("mcp-session-id", {k.lower() for k in r.headers})


class MainTests(unittest.TestCase):
    def test_main_exits_without_token_and_never_builds_runtime(self) -> None:
        from instagram_sdk import server as server_mod

        env = {"INSTAGRAM_MCP_TRANSPORT": "streamable-http"}
        with mock.patch.dict("os.environ", env, clear=False), \
                mock.patch.dict("os.environ", {"INSTAGRAM_MCP_BEARER_TOKEN": "",
                                               "INSTAGRAM_MCP_BEARER_TOKEN_FILE": ""}), \
                mock.patch("anyio.run") as run, \
                mock.patch.object(sys, "stderr"):
            with self.assertRaises(SystemExit) as cm:
                server_mod.main()
        self.assertEqual(cm.exception.code, 2)
        run.assert_not_called()

    def test_main_stdio_by_default(self) -> None:
        from instagram_sdk import server as server_mod

        with mock.patch.dict("os.environ", {"INSTAGRAM_MCP_TRANSPORT": ""}), \
                mock.patch("anyio.run") as run:
            server_mod.main()
        settings = run.call_args.args[1]
        self.assertFalse(settings.is_http)


if __name__ == "__main__":
    unittest.main()
