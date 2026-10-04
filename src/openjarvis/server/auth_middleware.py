"""API key authentication middleware for the OpenJarvis server."""

from __future__ import annotations

import ipaddress
import logging
import os
import secrets
from urllib.parse import parse_qs

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Reachable without a token: the version handshake and the pairing step the
# phone performs before it has one (guarded by the one-time code).
_PUBLIC_PATHS = frozenset({"/v1/mobile/hello", "/v1/mobile/pair/complete"})
# A request a local proxy relayed (cloudflared, Tailscale Serve, nginx)
# arrives from 127.0.0.1 but is remote; these headers give it away.
_FORWARD_HEADERS = (b"x-forwarded-for", b"cf-connecting-ip", b"forwarded", b"x-real-ip")


class AuthMiddleware:
    """Validates ``Authorization: Bearer <key>`` on ``/v1/*`` and ``/api/*`` routes.

    Covers HTTP and WebSocket connections (a WebSocket may pass the token
    as ``?token=`` instead, since some clients cannot set headers).  The
    key is the server API key or, with ``device_auth``, a paired phone's
    device token.  With ``trust_loopback`` a direct request from this
    machine (the desktop app) needs no token.

    Webhook routes and health checks are exempt — they use
    per-channel signature verification instead.

    Sets ``request.state.auth`` to ``"key"``, ``"loopback"``, ``"device"``
    or ``"open"`` and, for devices, ``request.state.device_id``.
    """

    def __init__(
        self,
        app: ASGIApp,
        api_key: str = "",
        *,
        device_auth: bool = False,
        trust_loopback: bool = False,
    ) -> None:
        self.app = app
        self._api_key = api_key or os.environ.get("OPENJARVIS_API_KEY", "")
        self._device_auth = device_auth
        self._trust_loopback = trust_loopback

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        enforced = bool(self._api_key) or self._device_auth
        path = scope.get("path", "")
        if not enforced or not self._requires_auth(path):
            state["auth"] = "open"
            await self.app(scope, receive, send)
            return
        if self._trust_loopback and _is_direct_loopback(scope):
            state["auth"] = "loopback"
            await self.app(scope, receive, send)
            return

        token = _bearer(scope)
        if token is None:
            await _reject(scope, receive, send, "Missing Authorization header")
            return
        if self._api_key and secrets.compare_digest(token, self._api_key):
            state["auth"] = "key"
            await self.app(scope, receive, send)
            return
        if self._device_auth:
            from openjarvis.mobile.devices import get_device_store

            device = get_device_store().verify(token)
            if device is not None:
                state["auth"] = "device"
                state["device_id"] = device.id
                await self.app(scope, receive, send)
                return
        await _reject(scope, receive, send, "Invalid API key")

    @staticmethod
    def _requires_auth(path: str) -> bool:
        """Only protect API routes, not the frontend UI or static assets."""
        if path in _PUBLIC_PATHS:
            return False
        return path.startswith("/v1/") or path.startswith("/api/")


def _bearer(scope: Scope) -> str | None:
    """The token from ``Authorization: Bearer …`` (or ``?token=`` on a WS);
    "" for a malformed header, None when there is none at all."""
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            return token if scheme.lower() == "bearer" else ""
    if scope["type"] == "websocket":
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        if query.get("token"):
            return query["token"][0]
    return None


def _is_direct_loopback(scope: Scope) -> bool:
    client = scope.get("client")
    if not client:
        return False
    try:
        if not ipaddress.ip_address(client[0]).is_loopback:
            return False
    except ValueError:
        return False
    return not any(name in _FORWARD_HEADERS for name, _ in scope.get("headers", []))


async def _reject(scope: Scope, receive: Receive, send: Send, detail: str) -> None:
    if scope["type"] == "websocket":
        await receive()  # websocket.connect
        await send({"type": "websocket.close", "code": 1008, "reason": detail})
        return
    response = JSONResponse({"detail": detail}, status_code=401)
    await response(scope, receive, send)


def generate_api_key() -> str:
    """Generate a new API key with ``oj_sk_`` prefix."""
    return f"oj_sk_{secrets.token_urlsafe(32)}"


def check_bind_safety(host: str, *, api_key: str, device_auth: bool = False) -> None:
    """Refuse to bind non-loopback without an API key.

    Raises ``SystemExit`` if *host* is not a loopback address and
    *api_key* is empty — unless ``device_auth`` (``[server.mobile]
    enabled``) gates the API with paired-device tokens instead.
    """
    import ipaddress
    import sys

    try:
        is_loop = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loop = host in ("localhost", "")

    if not is_loop and not api_key and not device_auth:
        logger.error(
            "Binding to %s requires OPENJARVIS_API_KEY to be set. "
            "Run: jarvis auth generate-key",
            host,
        )
        sys.exit(1)
