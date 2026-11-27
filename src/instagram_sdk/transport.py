"""How the server is reached: stdio (default) or streamable-http behind a bearer token.

stdio stays the default and needs nothing from this module. HTTP mode is for
one long-lived server (the NAS) that is the only thing driving a browser for
an account; Claude Code on the PC connects to it instead of spawning stdio.

HTTP mode refuses to start without a token: there is no "open" HTTP server.
The token comes from ``INSTAGRAM_MCP_BEARER_TOKEN`` or from the file named by
``INSTAGRAM_MCP_BEARER_TOKEN_FILE`` (a Docker secret), never from the repo.

Every request to the MCP endpoint must carry ``Authorization: Bearer <token>``
(constant-time compare) and a ``Host`` in ``INSTAGRAM_MCP_ALLOWED_HOSTS``
(FastMCP's DNS-rebinding protection; without it a server bound to 0.0.0.0
either rejects the LAN or, with protection off, accepts any host).
``GET /healthz`` answers without a token and without touching Instagram, for
the container healthcheck.
"""

from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP

STDIO = "stdio"
HTTP = "streamable-http"
_TRANSPORT_ALIASES = {
    "": STDIO,
    "stdio": STDIO,
    "http": HTTP,
    "streamable-http": HTTP,
    "streamable_http": HTTP,
}

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_PATH = "/mcp"
HEALTH_PATH = "/healthz"
# Shorter tokens are refused: `openssl rand -hex 32` gives 64 characters.
MIN_TOKEN_LENGTH = 32
_LOCAL_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class TransportConfigError(RuntimeError):
    """The transport configuration is unusable; the server must not start."""


def _env(environ: dict[str, str], name: str) -> str:
    return environ.get(name, "").strip()


def _split(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _read_token(environ: dict[str, str]) -> str:
    direct = _env(environ, "INSTAGRAM_MCP_BEARER_TOKEN")
    file_name = _env(environ, "INSTAGRAM_MCP_BEARER_TOKEN_FILE")
    if direct and file_name:
        raise TransportConfigError(
            "Set INSTAGRAM_MCP_BEARER_TOKEN or INSTAGRAM_MCP_BEARER_TOKEN_FILE, not both."
        )
    if file_name:
        path = Path(file_name).expanduser()
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise TransportConfigError(
                f"Cannot read INSTAGRAM_MCP_BEARER_TOKEN_FILE ({path}): {exc.strerror or exc}"
            ) from None
    else:
        token = direct
    if not token:
        raise TransportConfigError(
            "HTTP transport needs a bearer token: set INSTAGRAM_MCP_BEARER_TOKEN_FILE "
            "(a secret file) or INSTAGRAM_MCP_BEARER_TOKEN. Refusing to start without one."
        )
    if len(token) < MIN_TOKEN_LENGTH:
        raise TransportConfigError(
            f"The bearer token is too short ({len(token)} < {MIN_TOKEN_LENGTH} characters); "
            "generate one with `openssl rand -hex 32`."
        )
    if any(ch.isspace() for ch in token):
        raise TransportConfigError("The bearer token must not contain whitespace.")
    return token


@dataclass(frozen=True)
class TransportSettings:
    transport: str = STDIO
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    path: str = DEFAULT_PATH
    allowed_hosts: list[str] = field(default_factory=lambda: list(_LOCAL_HOSTS))
    allowed_origins: list[str] = field(default_factory=list)
    token: str = field(default="", repr=False)

    @property
    def is_http(self) -> bool:
        return self.transport == HTTP

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "TransportSettings":
        env = dict(os.environ if environ is None else environ)
        raw = _env(env, "INSTAGRAM_MCP_TRANSPORT").lower()
        if raw not in _TRANSPORT_ALIASES:
            raise TransportConfigError(
                f"INSTAGRAM_MCP_TRANSPORT={raw!r} is not valid; use 'stdio' or 'streamable-http'."
            )
        transport = _TRANSPORT_ALIASES[raw]
        if transport == STDIO:
            return cls()

        host = _env(env, "INSTAGRAM_MCP_HOST") or DEFAULT_HOST
        port_raw = _env(env, "INSTAGRAM_MCP_PORT")
        try:
            port = int(port_raw) if port_raw else DEFAULT_PORT
        except ValueError:
            raise TransportConfigError(f"INSTAGRAM_MCP_PORT={port_raw!r} is not a number.") from None
        if not 1 <= port <= 65535:
            raise TransportConfigError(f"INSTAGRAM_MCP_PORT={port} is out of range.")
        path = _env(env, "INSTAGRAM_MCP_PATH") or DEFAULT_PATH
        if not path.startswith("/") or path == HEALTH_PATH:
            raise TransportConfigError(f"INSTAGRAM_MCP_PATH={path!r} must start with '/' and not be {HEALTH_PATH}.")

        extra_hosts = _split(_env(env, "INSTAGRAM_MCP_ALLOWED_HOSTS"))
        if "*" in extra_hosts:
            raise TransportConfigError(
                "INSTAGRAM_MCP_ALLOWED_HOSTS does not accept '*'; list each name or IP "
                "(e.g. 192.168.1.48:*,nas.local:*)."
            )
        allowed_hosts = list(dict.fromkeys(_LOCAL_HOSTS + extra_hosts))
        allowed_origins = _split(_env(env, "INSTAGRAM_MCP_ALLOWED_ORIGINS"))

        return cls(
            transport=HTTP,
            host=host,
            port=port,
            path=path,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
            token=_read_token(env),
        )


async def _send_json(send: Send, status: int, body: dict[str, Any],
                     extra_headers: list[tuple[bytes, bytes]] | None = None) -> None:
    payload = json.dumps(body).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode("ascii")),
        *(extra_headers or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


class BearerTokenMiddleware:
    """Reject every HTTP request without the right ``Authorization: Bearer`` header.

    ``/healthz`` is the only exception. Lifespan events pass through untouched
    (FastMCP's session manager starts there).
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            raise TransportConfigError("BearerTokenMiddleware needs a token.")
        self.app = app
        self._expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") == HEALTH_PATH and scope.get("method") in ("GET", "HEAD"):
            await _send_json(send, 200, {"status": "ok"})
            return
        supplied = b""
        for name, value in scope.get("headers") or ():
            if name.lower() == b"authorization":
                supplied = value
                break
        if not supplied or not hmac.compare_digest(supplied, self._expected):
            await _send_json(
                send, 401, {"error": "unauthorized"},
                [(b"www-authenticate", b'Bearer realm="instagram-mcp"')],
            )
            return
        await self.app(scope, receive, send)


def configure_http(mcp: "FastMCP", settings: TransportSettings) -> ASGIApp:
    """Apply host/port/security to ``mcp`` and return the guarded ASGI app.

    Must run before ``mcp.streamable_http_app()`` is first called: FastMCP
    builds its session manager (and copies the security settings) lazily there.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    if not settings.is_http:
        raise TransportConfigError("configure_http called without HTTP transport.")
    mcp.settings.host = settings.host
    mcp.settings.port = settings.port
    mcp.settings.streamable_http_path = settings.path
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    )
    return BearerTokenMiddleware(mcp.streamable_http_app(), settings.token)


async def serve_http(mcp: "FastMCP", settings: TransportSettings) -> None:
    """Run the guarded streamable-http app with uvicorn until SIGINT/SIGTERM."""
    import uvicorn

    app = configure_http(mcp, settings)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=os.getenv("INSTAGRAM_LOG_LEVEL", "WARNING").lower(),
        # The Authorization header is never logged by uvicorn; the access log
        # only shows method, path and status.
        access_log=True,
        proxy_headers=False,
    )
    await uvicorn.Server(config).serve()
