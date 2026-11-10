"""What JARVIS heard: one line per wake and per transcript.

``~/.openjarvis/logs/heard.log`` is what to read when "JARVIS didn't
understand me": a wake line without a transcript means the command mic
heard no speech; a transcript line shows what the model made of it; no
wake line means the wake word was missed.  Before this, the only record
was the webview's local storage.  The file is private (0600) and capped
at 2 MB with one rotation.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from openjarvis.core.config import DEFAULT_CONFIG_DIR

HEARD_LOG: Path = DEFAULT_CONFIG_DIR / "logs" / "heard.log"
MAX_BYTES = 2_000_000


def record(kind: str, text: str, **fields: object) -> None:
    """Append ``<time> <kind> k=v ... | text``; never raises."""
    try:
        path = HEARD_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        parts = [time.strftime("%Y-%m-%d %H:%M:%S"), kind]
        parts += [f"{k}={v}" for k, v in fields.items()]
        line = " ".join(parts) + f" | {text.replace(chr(10), ' ')}\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


__all__ = ["HEARD_LOG", "record"]
