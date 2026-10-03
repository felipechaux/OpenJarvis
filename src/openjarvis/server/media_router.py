"""Now playing and media controls for the desktop notch.

Spotify plays in the user's browser (open.spotify.com, see
``tools/browser_launcher.py``).  The notch polls ``GET /v1/media/now-playing``
and its buttons call ``POST /v1/media/{action}``; both run JavaScript in the
Spotify tab where it is, so the browser never comes to the front.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Dict

from fastapi import APIRouter, Request

router = APIRouter(prefix="/v1/media", tags=["media"])

# Every open notch polls; one AppleScript round-trip serves them all.
_CACHE_S = 1.5
_cache: Dict[str, Any] = {"ts": 0.0, "data": {"state": "none"}}
_lock = threading.Lock()


def _now_playing() -> Dict[str, Any]:
    from openjarvis.tools.browser_launcher import spotify_now_playing

    with _lock:
        if time.monotonic() - _cache["ts"] < _CACHE_S:
            return _cache["data"]
        try:
            data = spotify_now_playing()
        except Exception:  # noqa: BLE001 — a missing player is "none"
            data = {"state": "none"}
        _cache.update(ts=time.monotonic(), data=data)
        return data


@router.get("/now-playing")
async def now_playing() -> Dict[str, Any]:
    return await asyncio.to_thread(_now_playing)


@router.post("/{action}")
async def control(action: str, request: Request) -> Dict[str, Any]:
    """play / pause / next / previous.  JSON content type required: it forces
    a CORS preflight, so other web pages cannot drive the player."""
    from openjarvis.tools.browser_launcher import spotify_control

    if not request.headers.get("content-type", "").startswith("application/json"):
        return {"ok": False, "error": "content-type must be application/json"}
    if action not in ("play", "pause", "next", "previous"):
        return {"ok": False, "error": "unknown action"}
    ok = await asyncio.to_thread(spotify_control, action)
    with _lock:
        _cache["ts"] = 0.0  # show the new track / state on the next poll
    return {"ok": ok}
