"""Mobile companion API: pairing, devices, push registration, live channel.

Chat stays on ``POST /v1/chat/completions`` (SSE) so the phone gets the
same agent, tools, persona and memory as the desktop.  Everything live
travels over one WebSocket RPC channel, ``/v1/mobile/ws``:

    request   {"id": 1, "method": "sessions.live", "params": {}}
    response  {"id": 1, "result": {...}}  |  {"id": 1, "error": {"code", "message"}}
    event     {"event": "session_event" | "agent_event" | "hello", "data": {...}}

Methods: ``hello`` ({"protocol"}), ``ping``, ``sessions.live``,
``sessions.events`` ({"after"}), ``sessions.answer`` ({"name", "prompt_id",
"key"}), ``sessions.message`` ({"name", "text"}), ``push.register``
({"provider": "fcm" | "apns", "token"}).

Authentication is the auth middleware's job (it covers WebSockets): a
paired device token, the server API key, or a direct loopback request.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from openjarvis.core.events import Event, EventBus, EventType
from openjarvis.mobile import MIN_PROTOCOL, PROTOCOL_VERSION
from openjarvis.mobile.devices import DeviceStore, get_device_store
from openjarvis.mobile.pairing import (
    PairingManager,
    build_payload,
    candidate_urls,
    payload_qr_svg,
)
from openjarvis.mobile.push import (
    PUSH_PROVIDERS,
    ApnsSender,
    FcmSender,
    PushDispatcher,
)
from openjarvis.server import coding_sessions_router as sessions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/mobile", tags=["mobile"])

_POLL_S = 1.5
_AGENT_EVENTS = {
    EventType.AGENT_TURN_START: "agent_turn_start",
    EventType.INFERENCE_START: "inference_start",
    EventType.INFERENCE_END: "inference_end",
    EventType.TOOL_CALL_START: "tool_call_start",
    EventType.TOOL_CALL_END: "tool_call_end",
}


class RpcError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MobileHub:
    """Fans session and agent events out to phones (WebSocket and push)."""

    def __init__(self, store: DeviceStore, push: Optional[PushDispatcher]) -> None:
        self.store = store
        self.push = push
        self.pairing = PairingManager()
        self._clients: Set[WebSocket] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._task is None:
            self._task = asyncio.create_task(self._poll_sessions())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def subscribe(self, bus: EventBus) -> None:
        for event_type in _AGENT_EVENTS:
            bus.subscribe(event_type, self._on_agent_event)

    # ── fan-out ──────────────────────────────────────────────────────

    async def _poll_sessions(self) -> None:
        """Forward new coding-session events; push the ones that need the user.

        Starts after the newest event so a restart never re-notifies.
        """
        last = sessions.latest_event_id()
        while True:
            await asyncio.sleep(_POLL_S)
            try:
                events, last = sessions.events_after(last)
            except Exception:  # noqa: BLE001 — keep the loop alive
                logger.debug("mobile session poll failed", exc_info=True)
                continue
            for event in events:
                await self.broadcast("session_event", event)
                if self.push is not None:
                    self.push.dispatch(event)

    def _on_agent_event(self, event: Event) -> None:
        # Called from the agent's thread.
        if not self._clients or self._loop is None:
            return
        payload = {
            "type": _AGENT_EVENTS.get(event.event_type, event.event_type.value),
            "timestamp": event.timestamp,
            "data": event.data or {},
        }
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast("agent_event", payload), self._loop
            )
        except RuntimeError:
            pass  # loop closed during shutdown

    async def broadcast(self, name: str, data: Dict[str, Any]) -> None:
        for ws in list(self._clients):
            try:
                await ws.send_json({"event": name, "data": data})
            except Exception:  # noqa: BLE001 — a dead socket is dropped
                self._clients.discard(ws)

    # ── WebSocket ────────────────────────────────────────────────────

    async def serve(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        device_id = getattr(ws.state, "device_id", None)
        try:
            await ws.send_json({"event": "hello", "data": hello_info(self)})
            while True:
                message = await ws.receive_json()
                await ws.send_json(await self._handle(message, device_id))
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 — malformed frames end the session
            logger.debug("mobile ws closed", exc_info=True)
        finally:
            self._clients.discard(ws)

    async def _handle(self, message: Any, device_id: Optional[str]) -> Dict[str, Any]:
        if not isinstance(message, dict):
            return {
                "id": None,
                "error": {"code": "bad_request", "message": "expected an object"},
            }
        req_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        handler = _METHODS.get(method)
        try:
            if handler is None or not isinstance(params, dict):
                raise RpcError("unknown_method", f"unknown method: {method}")
            result = await handler(self, params, device_id)
        except RpcError as exc:
            return {"id": req_id, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # noqa: BLE001
            logger.warning("mobile rpc %s failed: %s", method, exc)
            return {
                "id": req_id,
                "error": {"code": "internal", "message": str(exc)[:200]},
            }
        return {"id": req_id, "result": result}


def hello_info(hub: MobileHub) -> Dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "min_protocol": MIN_PROTOCOL,
        "name": socket.gethostname().removesuffix(".local"),
        "push": hub.push.providers if hub.push is not None else [],
        "time": time.time(),
    }


def check_protocol(app_protocol: Any) -> int:
    try:
        version = int(app_protocol)
    except (TypeError, ValueError):
        raise RpcError("bad_request", "protocol must be an integer") from None
    if version < MIN_PROTOCOL:
        raise RpcError(
            "incompatible_protocol",
            f"app protocol {version} is too old; update the app (min {MIN_PROTOCOL})",
        )
    if version > PROTOCOL_VERSION:
        raise RpcError(
            "incompatible_protocol",
            f"app protocol {version} is newer than the server ({PROTOCOL_VERSION}); "
            "update OpenJarvis",
        )
    return version


# ── RPC methods ──────────────────────────────────────────────────────

Handler = Callable[[MobileHub, Dict[str, Any], Optional[str]], Awaitable[Any]]


async def _rpc_hello(
    hub: MobileHub, params: Dict[str, Any], device_id: Optional[str]
) -> Any:
    version = check_protocol(params.get("protocol"))
    if device_id:
        hub.store.update(device_id, app_protocol=version)
    return hello_info(hub)


async def _rpc_ping(
    hub: MobileHub, params: Dict[str, Any], device_id: Optional[str]
) -> Any:
    return {"pong": time.time()}


async def _rpc_live(
    hub: MobileHub, params: Dict[str, Any], device_id: Optional[str]
) -> Any:
    from openjarvis.server import session_watcher

    return {"sessions": await asyncio.to_thread(session_watcher.live)}


async def _rpc_events(
    hub: MobileHub, params: Dict[str, Any], device_id: Optional[str]
) -> Any:
    events, last = sessions.events_after(int(params.get("after") or 0))
    return {"events": events, "last_id": last}


async def _rpc_answer(
    hub: MobileHub, params: Dict[str, Any], device_id: Optional[str]
) -> Any:
    return await sessions.answer_session_prompt(
        str(params.get("name") or ""),
        params.get("prompt_id"),
        str(params.get("key") or ""),
    )


async def _rpc_message(
    hub: MobileHub, params: Dict[str, Any], device_id: Optional[str]
) -> Any:
    return await sessions.message_to_session(
        str(params.get("name") or ""), str(params.get("text") or "")
    )


async def _rpc_push_register(
    hub: MobileHub, params: Dict[str, Any], device_id: Optional[str]
) -> Any:
    return register_push(hub, device_id, params)


def register_push(
    hub: MobileHub, device_id: Optional[str], params: Dict[str, Any]
) -> Dict[str, Any]:
    """Store a device's push token (``provider``: "fcm" or "apns")."""
    if not device_id:
        raise RpcError("forbidden", "only a paired device can register for push")
    provider = str(params.get("provider") or "")
    token = str(params.get("token") or "")
    if provider not in PUSH_PROVIDERS or not token:
        raise RpcError("bad_request", "expected provider 'fcm' or 'apns' and a token")
    hub.store.update(device_id, push={"provider": provider, "token": token[:4096]})
    enabled = hub.push is not None and provider in hub.push.providers
    return {"ok": True, "push_enabled": enabled}


_METHODS: Dict[str, Handler] = {
    "hello": _rpc_hello,
    "ping": _rpc_ping,
    "sessions.live": _rpc_live,
    "sessions.events": _rpc_events,
    "sessions.answer": _rpc_answer,
    "sessions.message": _rpc_message,
    "push.register": _rpc_push_register,
}


# ── HTTP ─────────────────────────────────────────────────────────────


def _hub(request: Request | WebSocket) -> MobileHub:
    hub = getattr(request.app.state, "mobile_hub", None)
    if hub is None:
        raise HTTPException(status_code=409, detail="Mobile companion is disabled")
    return hub


def _require_owner(request: Request) -> None:
    """Pairing and device management are for this machine, not for phones."""
    if getattr(request.state, "auth", "open") not in {"loopback", "key"}:
        raise HTTPException(status_code=403, detail="Not allowed from a device")


@router.get("/hello")
async def hello(request: Request) -> Dict[str, Any]:
    """Version handshake; public so the app can check before pairing."""
    return hello_info(_hub(request))


@router.post("/pair/start")
async def pair_start(request: Request) -> Dict[str, Any]:
    """Issue a one-time pairing code and the QR the phone scans."""
    hub = _hub(request)
    _require_owner(request)
    config = request.app.state.config
    code, expires = hub.pairing.new_code()
    urls = candidate_urls(config.server.port, config.server.mobile.public_url)
    payload = build_payload(code, expires, urls)
    return {"payload": payload, "qr_svg": payload_qr_svg(payload)}


@router.post("/pair/complete")
async def pair_complete(request: Request) -> Dict[str, Any]:
    """Trade a pairing code for a device token (shown only this once)."""
    hub = _hub(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    try:
        version = check_protocol(body.get("protocol"))
    except RpcError as exc:
        raise HTTPException(status_code=426, detail=exc.message) from None
    if not hub.pairing.consume(str(body.get("code") or "")):
        raise HTTPException(status_code=401, detail="Invalid or expired pairing code")
    device, token = hub.store.add(
        str(body.get("name") or ""), str(body.get("platform") or "")
    )
    hub.store.update(device.id, app_protocol=version)
    return {"token": token, "device": device.public(), **hello_info(hub)}


@router.get("/devices")
async def list_devices(request: Request) -> Dict[str, Any]:
    hub = _hub(request)
    _require_owner(request)
    return {"devices": [d.public() for d in hub.store.list()]}


@router.delete("/devices/{device_id}")
async def revoke_device(device_id: str, request: Request) -> Dict[str, Any]:
    """Unpair a device.  A phone may unpair itself; only the owner others."""
    hub = _hub(request)
    if getattr(request.state, "device_id", None) != device_id:
        _require_owner(request)
    if not hub.store.remove(device_id):
        raise HTTPException(status_code=404, detail="No such device")
    return {"ok": True}


@router.post("/push/register")
async def push_register(request: Request) -> Dict[str, Any]:
    """Same as the ``push.register`` RPC, for push services that run without
    the WebSocket (Android's FirebaseMessagingService, iOS app delegate)."""
    hub = _hub(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    try:
        return register_push(hub, getattr(request.state, "device_id", None), body)
    except RpcError as exc:
        status = 403 if exc.code == "forbidden" else 400
        raise HTTPException(status_code=status, detail=exc.message) from None


@router.websocket("/ws")
async def mobile_ws(ws: WebSocket) -> None:
    hub = getattr(ws.app.state, "mobile_hub", None)
    if hub is None:
        await ws.close(code=1008, reason="Mobile companion is disabled")
        return
    await hub.serve(ws)


def create_mobile_hub(config: Any, bus: Optional[EventBus]) -> MobileHub:
    """Build the hub for ``[server.mobile]`` with whichever push providers
    are configured (FCM for Android, APNs for iOS)."""
    store = get_device_store()
    cfg = config.server.mobile
    senders: list = []
    if cfg.fcm_service_account:
        fcm = FcmSender(cfg.fcm_service_account, store)
        if fcm.available:
            senders.append(fcm)
        else:
            logger.warning(
                "FCM service account not found at %s; Android push is off",
                cfg.fcm_service_account,
            )
    if cfg.apns_key_path:
        apns = ApnsSender(
            cfg.apns_key_path,
            cfg.apns_key_id,
            cfg.apns_team_id,
            cfg.apns_topic,
            store,
            sandbox=cfg.apns_sandbox,
        )
        if apns.available:
            senders.append(apns)
        else:
            logger.warning("APNs settings incomplete; iOS push is off")
    hub = MobileHub(store, PushDispatcher(senders))
    if bus is not None:
        hub.subscribe(bus)
    return hub
