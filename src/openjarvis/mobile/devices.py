"""Paired phones and their tokens, persisted in ``~/.openjarvis/mobile/``.

Only a SHA-256 of each device token is stored, so the file never holds a
usable credential.  Revoking a device deletes its entry; its token stops
working on the next request.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from openjarvis.core.config import DEFAULT_CONFIG_DIR

TOKEN_PREFIX = "oj_dev_"
DEFAULT_PATH = DEFAULT_CONFIG_DIR / "mobile" / "devices.json"
# last_seen is written at most this often per device, not on every request.
_TOUCH_EVERY_S = 60.0


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Device:
    id: str
    name: str
    platform: str  # "android" | "ios" | …
    token_sha256: str
    created_at: float
    last_seen: float = 0.0
    app_protocol: int = 0
    # {"provider": "fcm", "token": "…"} once the app registered for push.
    push: Dict[str, str] = field(default_factory=dict)

    def public(self) -> Dict[str, object]:
        """What the API shows: everything but the token hash."""
        data = asdict(self)
        data.pop("token_sha256")
        data["push"] = {"provider": self.push.get("provider", "")} if self.push else {}
        return data


class DeviceStore:
    """Thread-safe JSON-backed registry of paired devices."""

    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._devices: Dict[str, Device] = self._load()

    def _load(self) -> Dict[str, Device]:
        try:
            raw = json.loads(self._path.read_text())
        except (FileNotFoundError, ValueError):
            return {}
        devices = {}
        for item in raw.get("devices", []):
            try:
                device = Device(**item)
            except TypeError:
                continue  # an entry from a newer/older format; skip it
            devices[device.id] = device
        return devices

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        data = {"devices": [asdict(d) for d in self._devices.values()]}
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)

    def add(self, name: str, platform: str) -> tuple[Device, str]:
        """Register a device; returns it and its token (shown only once)."""
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        device = Device(
            id=secrets.token_hex(8),
            name=(name or "Teléfono").strip()[:64],
            platform=(platform or "unknown").strip().lower()[:16],
            token_sha256=_hash(token),
            created_at=time.time(),
        )
        with self._lock:
            self._devices[device.id] = device
            self._save()
        return device, token

    def verify(self, token: str) -> Optional[Device]:
        """The device owning ``token``, or None."""
        if not token.startswith(TOKEN_PREFIX):
            return None
        digest = _hash(token)
        with self._lock:
            for device in self._devices.values():
                if hmac.compare_digest(device.token_sha256, digest):
                    now = time.time()
                    if now - device.last_seen > _TOUCH_EVERY_S:
                        device.last_seen = now
                        self._save()
                    return device
        return None

    def get(self, device_id: str) -> Optional[Device]:
        with self._lock:
            return self._devices.get(device_id)

    def list(self) -> List[Device]:
        with self._lock:
            return sorted(self._devices.values(), key=lambda d: d.created_at)

    def remove(self, device_id: str) -> bool:
        with self._lock:
            if self._devices.pop(device_id, None) is None:
                return False
            self._save()
            return True

    def update(self, device_id: str, **changes: object) -> Optional[Device]:
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                return None
            for key, value in changes.items():
                setattr(device, key, value)
            self._save()
            return device

    def drop_push_token(self, push_token: str) -> None:
        """Forget a push token the provider reported as unregistered."""
        with self._lock:
            changed = False
            for device in self._devices.values():
                if device.push.get("token") == push_token:
                    device.push = {}
                    changed = True
            if changed:
                self._save()


_store: Optional[DeviceStore] = None
_store_lock = threading.Lock()


def get_device_store() -> DeviceStore:
    """Process-wide store (the auth middleware and the router share it)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = DeviceStore()
        return _store


def set_device_store(store: Optional[DeviceStore]) -> None:
    """Swap the process-wide store (tests)."""
    global _store
    with _store_lock:
        _store = store
