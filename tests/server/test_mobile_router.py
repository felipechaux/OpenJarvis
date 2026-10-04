"""Tests for the mobile companion: pairing, device auth, RPC channel, push."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi", reason="openjarvis[server] not installed")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from openjarvis.mobile import MIN_PROTOCOL, PROTOCOL_VERSION
from openjarvis.mobile.devices import DeviceStore, set_device_store
from openjarvis.mobile.pairing import PairingManager
from openjarvis.mobile.push import PushDispatcher, notification_for
from openjarvis.server import mobile_router
from openjarvis.server.auth_middleware import AuthMiddleware
from openjarvis.server.mobile_router import MobileHub

LOOPBACK = ("127.0.0.1", 50000)
REMOTE = ("192.168.1.50", 50000)


@pytest.fixture
def store(tmp_path):
    s = DeviceStore(tmp_path / "devices.json")
    set_device_store(s)
    yield s
    set_device_store(None)


@pytest.fixture
def app(store):
    app = FastAPI()
    app.include_router(mobile_router.router)
    app.add_middleware(AuthMiddleware, device_auth=True, trust_loopback=True)
    app.state.config = SimpleNamespace(
        server=SimpleNamespace(
            port=8000, mobile=SimpleNamespace(public_url="https://jarvis.ts.net")
        )
    )
    app.state.mobile_hub = MobileHub(store, push=PushDispatcher([]))

    @app.get("/v1/models")
    async def models():
        return {"models": []}

    return app


def _owner(app):
    return TestClient(app, client=LOOPBACK)


def _phone(app, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app, client=REMOTE, headers=headers)


def _pair(app) -> tuple[str, dict]:
    payload = _owner(app).post("/v1/mobile/pair/start").json()["payload"]
    resp = _phone(app).post(
        "/v1/mobile/pair/complete",
        json={
            "code": payload["code"],
            "name": "Pixel",
            "platform": "android",
            "protocol": PROTOCOL_VERSION,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["token"], body["device"]


class TestPairing:
    def test_payload_shape(self, app):
        body = _owner(app).post("/v1/mobile/pair/start").json()
        payload = body["payload"]
        assert payload["type"] == "openjarvis-pair"
        assert payload["v"] == PROTOCOL_VERSION
        assert payload["urls"][0] == "https://jarvis.ts.net"
        assert payload["code"]
        assert body["qr_svg"] is None or body["qr_svg"].startswith("<svg")

    def test_full_flow_gives_working_token(self, app):
        token, device = _pair(app)
        assert token.startswith("oj_dev_")
        assert device["name"] == "Pixel"
        assert "token_sha256" not in device
        assert _phone(app, token).get("/v1/models").status_code == 200

    def test_code_is_single_use(self, app):
        payload = _owner(app).post("/v1/mobile/pair/start").json()["payload"]
        body = {"code": payload["code"], "protocol": PROTOCOL_VERSION}
        assert (
            _phone(app).post("/v1/mobile/pair/complete", json=body).status_code == 200
        )
        assert (
            _phone(app).post("/v1/mobile/pair/complete", json=body).status_code == 401
        )

    def test_old_app_protocol_rejected(self, app):
        payload = _owner(app).post("/v1/mobile/pair/start").json()["payload"]
        resp = _phone(app).post(
            "/v1/mobile/pair/complete",
            json={"code": payload["code"], "protocol": MIN_PROTOCOL - 1},
        )
        assert resp.status_code == 426

    def test_phone_cannot_start_pairing(self, app):
        token, _ = _pair(app)
        assert _phone(app, token).post("/v1/mobile/pair/start").status_code == 403

    def test_remote_without_token_cannot_start_pairing(self, app):
        assert _phone(app).post("/v1/mobile/pair/start").status_code == 401

    def test_guessing_voids_pending_codes(self):
        manager = PairingManager()
        code, _ = manager.new_code()
        for _ in range(5):
            assert not manager.consume("wrong")
        assert not manager.consume(code)

    def test_expired_code(self):
        manager = PairingManager(ttl_s=-1)
        code, _ = manager.new_code()
        assert not manager.consume(code)


class TestDeviceAuth:
    def test_unknown_token_rejected(self, app):
        assert _phone(app, "oj_dev_nope").get("/v1/models").status_code == 401

    def test_missing_token_rejected(self, app):
        assert _phone(app).get("/v1/models").status_code == 401

    def test_loopback_trusted(self, app):
        assert _owner(app).get("/v1/models").status_code == 200

    def test_proxied_loopback_not_trusted(self, app):
        client = TestClient(
            app, client=LOOPBACK, headers={"X-Forwarded-For": "1.2.3.4"}
        )
        assert client.get("/v1/models").status_code == 401

    def test_hello_is_public(self, app):
        body = _phone(app).get("/v1/mobile/hello").json()
        assert body["protocol"] == PROTOCOL_VERSION
        assert body["min_protocol"] == MIN_PROTOCOL

    def test_revoke_kills_token(self, app):
        token, device = _pair(app)
        assert (
            _owner(app).delete(f"/v1/mobile/devices/{device['id']}").status_code == 200
        )
        assert _phone(app, token).get("/v1/models").status_code == 401

    def test_device_cannot_revoke_others(self, app):
        token, _ = _pair(app)
        _, other = _pair(app)
        resp = _phone(app, token).delete(f"/v1/mobile/devices/{other['id']}")
        assert resp.status_code == 403

    def test_device_can_unpair_itself(self, app):
        token, device = _pair(app)
        assert (
            _phone(app, token).delete(f"/v1/mobile/devices/{device['id']}").status_code
            == 200
        )

    def test_store_never_persists_token(self, app, store, tmp_path):
        token, _ = _pair(app)
        assert token not in (tmp_path / "devices.json").read_text()


class TestWebSocket:
    def test_rejects_without_token(self, app):
        with pytest.raises(WebSocketDisconnect):
            with _phone(app).websocket_connect("/v1/mobile/ws") as ws:
                ws.receive_json()

    def test_rpc_roundtrip(self, app, store):
        token, device = _pair(app)
        with _phone(app).websocket_connect(f"/v1/mobile/ws?token={token}") as ws:
            assert ws.receive_json()["event"] == "hello"

            ws.send_json(
                {"id": 1, "method": "hello", "params": {"protocol": PROTOCOL_VERSION}}
            )
            assert ws.receive_json()["result"]["protocol"] == PROTOCOL_VERSION

            ws.send_json({"id": 2, "method": "ping"})
            assert "pong" in ws.receive_json()["result"]

            ws.send_json(
                {
                    "id": 3,
                    "method": "push.register",
                    "params": {"provider": "fcm", "token": "abc"},
                }
            )
            assert ws.receive_json()["result"]["ok"] is True

            ws.send_json({"id": 4, "method": "nope"})
            assert ws.receive_json()["error"]["code"] == "unknown_method"

            ws.send_json(
                {
                    "id": 5,
                    "method": "hello",
                    "params": {"protocol": PROTOCOL_VERSION + 1},
                }
            )
            assert ws.receive_json()["error"]["code"] == "incompatible_protocol"
        assert store.get(device["id"]).push == {"provider": "fcm", "token": "abc"}

    def test_header_auth(self, app):
        token, _ = _pair(app)
        with _phone(app, token).websocket_connect("/v1/mobile/ws") as ws:
            assert ws.receive_json()["event"] == "hello"

    def test_loopback_cannot_register_push(self, app):
        with _owner(app).websocket_connect("/v1/mobile/ws") as ws:
            ws.receive_json()
            ws.send_json(
                {
                    "id": 1,
                    "method": "push.register",
                    "params": {"provider": "fcm", "token": "x"},
                }
            )
            assert ws.receive_json()["error"]["code"] == "forbidden"


class TestPush:
    def test_only_actionable_events_notify(self):
        assert notification_for({"kind": "progress", "text": "x"}) is None
        title, body = notification_for(
            {
                "kind": "notification",
                "project": "openjarvis",
                "text": "Claude pide permiso",
            }
        )
        assert "openjarvis" in title and body == "Claude pide permiso"
        assert notification_for({"kind": "stop", "project": "p", "text": "terminó"})

    def test_dispatch_rate_limited(self, monkeypatch):
        sent = []

        class FakeSender:
            available = True
            provider = "fcm"

            def send_all(self, title, body, data):
                sent.append(title)

        # Run the send inline instead of in a thread.
        monkeypatch.setattr(
            "openjarvis.mobile.push.threading.Thread",
            lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args)),
        )
        dispatcher = PushDispatcher([FakeSender()])
        event = {"id": 1, "kind": "stop", "project": "p", "text": "listo"}
        dispatcher.dispatch(event)
        dispatcher.dispatch({**event, "id": 2})
        assert len(sent) == 1

    def test_unregistered_token_dropped(self, store):
        device, _ = store.add("Pixel", "android")
        store.update(device.id, push={"provider": "fcm", "token": "dead"})
        store.drop_push_token("dead")
        assert store.get(device.id).push == {}


class TestPushRegistration:
    def test_http_register(self, app, store):
        token, device = _pair(app)
        resp = _phone(app, token).post(
            "/v1/mobile/push/register", json={"provider": "apns", "token": "abc123"}
        )
        assert resp.status_code == 200
        assert resp.json()["push_enabled"] is False  # no APNs key configured
        assert store.get(device["id"]).push == {"provider": "apns", "token": "abc123"}

    def test_http_register_rejects_unknown_provider(self, app):
        token, _ = _pair(app)
        resp = _phone(app, token).post(
            "/v1/mobile/push/register", json={"provider": "sms", "token": "x"}
        )
        assert resp.status_code == 400

    def test_http_register_needs_a_device(self, app):
        resp = _owner(app).post(
            "/v1/mobile/push/register", json={"provider": "fcm", "token": "x"}
        )
        assert resp.status_code == 403


class TestApnsJwt:
    def test_signature_verifies(self):
        import base64
        import json

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import (
            encode_dss_signature,
        )

        from openjarvis.mobile.push import apns_jwt

        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        token = apns_jwt(pem, "KEY123", "TEAM456", 1_700_000_000)
        header_b64, claims_b64, sig_b64 = token.split(".")

        def unb64(part: str) -> bytes:
            return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

        assert json.loads(unb64(header_b64)) == {"alg": "ES256", "kid": "KEY123"}
        assert json.loads(unb64(claims_b64)) == {"iss": "TEAM456", "iat": 1_700_000_000}
        raw = unb64(sig_b64)
        assert len(raw) == 64
        der = encode_dss_signature(
            int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        )
        key.public_key().verify(
            der, f"{header_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256())
        )
