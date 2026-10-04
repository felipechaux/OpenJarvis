"""Mobile companion support: device pairing, per-device tokens and push.

The phone talks to the same FastAPI server as the desktop app.  It pairs
once by scanning a QR (``jarvis mobile pair``), receives a device token,
and from then on uses ``/v1/chat/completions`` (SSE) for chat and the
``/v1/mobile/ws`` RPC channel for live events and session control.
"""

from __future__ import annotations

# Bump PROTOCOL_VERSION on any change to the pairing payload or the
# /v1/mobile/ws message shapes; bump MIN_PROTOCOL only when older apps can no
# longer work, so they fail loudly instead of silently misbehaving.
PROTOCOL_VERSION = 1
MIN_PROTOCOL = 1

__all__ = ["MIN_PROTOCOL", "PROTOCOL_VERSION"]
