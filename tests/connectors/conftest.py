"""Keep connector tests away from the developer's real credentials.

Google connectors fall back to the shared ``~/.openjarvis/connectors/
google.json`` when their own file doesn't exist yet — which is exactly the
state a ``tmp_path`` fixture starts in.  Without this, a test that writes a
fake token and then calls ``disconnect()`` overwrote and deleted the real
shared token on any machine where JARVIS is set up.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_shared_google_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "openjarvis.connectors.oauth._SHARED_GOOGLE_CREDENTIALS_PATH",
        str(tmp_path / "google_shared.json"),
    )
