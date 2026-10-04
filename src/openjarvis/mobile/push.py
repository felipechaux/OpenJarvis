"""Push notifications to paired phones: FCM for Android, APNs for iOS.

* FCM HTTP v1 needs ``[server.mobile] fcm_service_account`` (a Firebase
  service account JSON) and ``google-auth``.
* APNs is called directly (no Firebase on iOS) with a token-based ``.p8``
  key: ``apns_key_path``, ``apns_key_id``, ``apns_team_id``, ``apns_topic``
  (the app's bundle id); it needs ``h2`` for HTTP/2 and ``cryptography``.

``uv sync --extra mobile`` installs all of them.  A provider without its
settings is silently off; the app still gets every event live over
``/v1/mobile/ws``.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from openjarvis.mobile.devices import DeviceStore

logger = logging.getLogger(__name__)

_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"


class FcmSender:
    """Sends one notification per device that registered an FCM token."""

    provider = "fcm"

    def __init__(self, service_account_path: str, store: DeviceStore) -> None:
        self._path = Path(service_account_path).expanduser()
        self._store = store
        self._creds: Any = None
        self._project = ""
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._path.is_file()

    def _access_token(self) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        with self._lock:
            if self._creds is None:
                self._project = json.loads(self._path.read_text())["project_id"]
                self._creds = service_account.Credentials.from_service_account_file(
                    str(self._path), scopes=[_SCOPE]
                )
            if not self._creds.valid:
                self._creds.refresh(Request())
            return self._creds.token

    def send_all(self, title: str, body: str, data: Dict[str, str]) -> int:
        """Notify every device with an FCM token; returns how many accepted."""
        targets = [d for d in self._store.list() if d.push.get("provider") == "fcm"]
        if not targets or not self.available:
            return 0
        try:
            token = self._access_token()
        except Exception as exc:  # noqa: BLE001 — push is best effort
            logger.warning("FCM auth failed: %s", exc)
            return 0
        sent = 0
        with httpx.Client(timeout=10) as client:
            for device in targets:
                push_token = device.push["token"]
                if self._send(client, token, push_token, title, body, data):
                    sent += 1
        return sent

    def _send(
        self,
        client: httpx.Client,
        access_token: str,
        push_token: str,
        title: str,
        body: str,
        data: Dict[str, str],
    ) -> bool:
        message = {
            "message": {
                "token": push_token,
                "notification": {"title": title, "body": body},
                "data": {k: str(v) for k, v in data.items()},
                "android": {"priority": "high"},
                "apns": {"payload": {"aps": {"sound": "default"}}},
            }
        }
        try:
            resp = client.post(
                _SEND_URL.format(project=self._project),
                headers={"Authorization": f"Bearer {access_token}"},
                json=message,
            )
        except httpx.HTTPError as exc:
            logger.warning("FCM send failed: %s", exc)
            return False
        if resp.status_code == 200:
            return True
        if resp.status_code == 404 or "UNREGISTERED" in resp.text:
            # The app was uninstalled or the token rotated.
            self._store.drop_push_token(push_token)
        else:
            logger.warning(
                "FCM rejected a push (%s): %s", resp.status_code, resp.text[:200]
            )
        return False


def notification_for(event: Dict[str, Any]) -> Optional[tuple[str, str]]:
    """(title, body) for a coding-session event worth a banner, else None.

    Progress updates are only shown live in the app; a banner is reserved
    for what needs the user: a finished turn, a permission prompt, the
    follow-up summary.
    """
    kind = event.get("kind")
    if kind not in {"stop", "notification", "summary"}:
        return None
    project = str(event.get("project") or "JARVIS")
    if kind == "notification":
        title = f"Permiso pendiente · {project}"
    else:
        title = f"{event.get('cli_label') or 'Claude'} · {project}"
    return title, str(event.get("text") or "")[:240]


class ApnsSender:
    """Sends alerts straight to APNs with a token-based (``.p8``) key."""

    provider = "apns"
    # Apple rejects tokens older than an hour and refreshes faster than every
    # 20 minutes; 50 minutes sits safely between.
    _JWT_TTL_S = 50 * 60

    def __init__(
        self,
        key_path: str,
        key_id: str,
        team_id: str,
        topic: str,
        store: DeviceStore,
        sandbox: bool = True,
    ) -> None:
        self._key_path = Path(key_path).expanduser()
        self._key_id = key_id
        self._team_id = team_id
        self._topic = topic
        self._store = store
        host = "api.sandbox.push.apple.com" if sandbox else "api.push.apple.com"
        self._base = f"https://{host}/3/device/"
        self._jwt = ""
        self._jwt_at = 0.0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self._key_id and self._team_id and self._topic) and (
            self._key_path.is_file()
        )

    def _token(self) -> str:
        with self._lock:
            now = time.time()
            if not self._jwt or now - self._jwt_at > self._JWT_TTL_S:
                self._jwt = apns_jwt(
                    self._key_path.read_bytes(), self._key_id, self._team_id, int(now)
                )
                self._jwt_at = now
            return self._jwt

    def send_all(self, title: str, body: str, data: Dict[str, str]) -> int:
        targets = [d for d in self._store.list() if d.push.get("provider") == "apns"]
        if not targets or not self.available:
            return 0
        try:
            token = self._token()
        except Exception as exc:  # noqa: BLE001 — push is best effort
            logger.warning("APNs key unusable: %s", exc)
            return 0
        payload = {
            "aps": {"alert": {"title": title, "body": body}, "sound": "default"},
            **{k: str(v) for k, v in data.items()},
        }
        headers = {
            "authorization": f"bearer {token}",
            "apns-topic": self._topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
        }
        sent = 0
        try:
            client = httpx.Client(http2=True, timeout=10)
        except ImportError:
            logger.warning("APNs needs the h2 package (uv sync --extra mobile)")
            return 0
        with client:
            for device in targets:
                device_token = device.push["token"]
                try:
                    resp = client.post(
                        self._base + device_token, headers=headers, json=payload
                    )
                except httpx.HTTPError as exc:
                    logger.warning("APNs send failed: %s", exc)
                    continue
                if resp.status_code == 200:
                    sent += 1
                elif resp.status_code == 410 or "BadDeviceToken" in resp.text:
                    self._store.drop_push_token(device_token)
                else:
                    logger.warning(
                        "APNs rejected a push (%s): %s",
                        resp.status_code,
                        resp.text[:200],
                    )
        return sent


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def apns_jwt(p8_pem: bytes, key_id: str, team_id: str, issued_at: int) -> str:
    """ES256 provider token for APNs (header.claims.signature)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature,
    )

    header = _b64url(json.dumps({"alg": "ES256", "kid": key_id}).encode())
    claims = _b64url(json.dumps({"iss": team_id, "iat": issued_at}).encode())
    signing_input = f"{header}.{claims}".encode("ascii")
    key = serialization.load_pem_private_key(p8_pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("APNs key must be an EC (P-256) private key")
    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    # JWS wants the raw 64-byte r||s, not DER.
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header}.{claims}.{_b64url(signature)}"


PUSH_PROVIDERS = ("fcm", "apns")


class PushDispatcher:
    """Sends banners off the request path, rate limited per event kind."""

    # Never more than one banner per (project, kind) in this window, so a
    # chatty session can't spam the phone.
    _MIN_GAP_S = 10.0

    def __init__(self, senders: List[Any]) -> None:
        self._senders = [s for s in senders if s is not None]
        self._last: Dict[tuple[str, str], float] = {}

    @property
    def providers(self) -> List[str]:
        return [s.provider for s in self._senders if s.available]

    def dispatch(self, event: Dict[str, Any]) -> None:
        senders = [s for s in self._senders if s.available]
        if not senders:
            return
        note = notification_for(event)
        if note is None:
            return
        key = (str(event.get("project")), str(event.get("kind")))
        now = time.monotonic()
        if now - self._last.get(key, 0.0) < self._MIN_GAP_S:
            return
        self._last[key] = now
        title, body = note
        data = {
            "event_id": str(event.get("id", "")),
            "kind": str(event.get("kind", "")),
            "project": str(event.get("project", "")),
            "session_id": str(event.get("session_id", "")),
        }
        for sender in senders:
            threading.Thread(
                target=sender.send_all, args=(title, body, data), daemon=True
            ).start()
