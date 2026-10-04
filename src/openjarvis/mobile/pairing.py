"""One-time pairing codes and the QR payload the phone scans.

Flow: the desktop (loopback) calls ``POST /v1/mobile/pair/start`` and shows
the returned QR.  The phone scans it, calls ``POST /v1/mobile/pair/complete``
with the code and receives its device token.  Codes live in memory, expire
after ``CODE_TTL_S`` and work once; too many wrong guesses void every
pending code.
"""

from __future__ import annotations

import json
import secrets
import socket
import threading
import time
from typing import Any, Dict, List, Optional

from openjarvis.mobile import PROTOCOL_VERSION

CODE_TTL_S = 300.0
MAX_FAILED_ATTEMPTS = 5
PAYLOAD_TYPE = "openjarvis-pair"


class PairingManager:
    def __init__(self, ttl_s: float = CODE_TTL_S) -> None:
        self._ttl = ttl_s
        self._codes: Dict[str, float] = {}  # code -> expiry
        self._failed = 0
        self._lock = threading.Lock()

    def new_code(self) -> tuple[str, float]:
        """Issue a code; returns it and its expiry (epoch seconds)."""
        code = secrets.token_urlsafe(16)
        expires = time.time() + self._ttl
        with self._lock:
            self._prune()
            self._codes[code] = expires
            self._failed = 0
        return code, expires

    def consume(self, code: str) -> bool:
        """True (once) for a live code; counts failures otherwise."""
        with self._lock:
            self._prune()
            if code and self._codes.pop(code, None) is not None:
                return True
            self._failed += 1
            if self._failed >= MAX_FAILED_ATTEMPTS:
                self._codes.clear()  # someone is guessing; start over
            return False

    def _prune(self) -> None:
        now = time.time()
        for code, expires in list(self._codes.items()):
            if expires <= now:
                del self._codes[code]


def lan_addresses() -> List[str]:
    """This machine's non-loopback IPv4 addresses, primary first."""
    found: List[str] = []
    # The interface the OS would route outbound traffic through.  UDP
    # connect sends nothing; it only picks a source address.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = str(info[4][0])
            if addr not in found:
                found.append(addr)
    except OSError:
        pass
    return [a for a in found if not a.startswith("127.")]


def candidate_urls(port: int, public_url: str = "") -> List[str]:
    """URLs the phone should try, in order: public (Tailscale/tunnel), LAN."""
    urls: List[str] = []
    if public_url:
        urls.append(public_url.rstrip("/"))
    urls.extend(f"http://{addr}:{port}" for addr in lan_addresses())
    return urls


def build_payload(code: str, expires: float, urls: List[str]) -> Dict[str, Any]:
    return {
        "type": PAYLOAD_TYPE,
        "v": PROTOCOL_VERSION,
        "name": socket.gethostname().removesuffix(".local"),
        "urls": urls,
        "code": code,
        "expires_at": int(expires),
    }


def payload_qr_svg(payload: Dict[str, Any]) -> Optional[str]:
    """The payload as an SVG QR, or None when ``segno`` isn't installed."""
    try:
        import segno
    except ImportError:
        return None
    qr = segno.make(json.dumps(payload, separators=(",", ":")), error="m")
    return qr.svg_inline(scale=6, border=2, dark="#000", light="#fff")


def print_payload_qr(payload: Dict[str, Any]) -> bool:
    """Draw the payload as a QR in the terminal; False without ``segno``."""
    try:
        import segno
    except ImportError:
        return False
    segno.make(json.dumps(payload, separators=(",", ":")), error="m").terminal(
        compact=True
    )
    return True
