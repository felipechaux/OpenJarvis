"""Startup sync — refresh all connected connectors when the server boots.

The desktop app spawns a fresh ``jarvis serve`` on every launch, so running
this once at server startup keeps Gmail, Calendar, Apple Notes and every other
connected source up to date for the assistant without any manual "sync" click.

Syncs run sequentially in a single background thread (see ``cli/serve.py``) so
they never block server boot and don't hammer every provider at once. Each
sync is incremental — :class:`~openjarvis.connectors.sync_engine.SyncEngine`
resumes from the last saved cursor — so re-syncing on every launch is cheap.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def sync_connected_connectors(only: Optional[List[str]] = None) -> Dict[str, object]:
    """Sync every currently-connected connector once.

    Parameters
    ----------
    only:
        If given, restrict the sync to these connector ids. Otherwise every
        connected connector is synced.

    Returns
    -------
    dict
        Summary of the form ``{"synced": {id: count}, "skipped": [...],
        "errors": {id: message}}``. Always returns — individual connector
        failures are logged and recorded, never raised.
    """
    from openjarvis.connectors.pipeline import IngestionPipeline
    from openjarvis.connectors.store import KnowledgeStore
    from openjarvis.connectors.sync_engine import SyncEngine
    from openjarvis.core.registry import ConnectorRegistry
    from openjarvis.server.connectors_router import _ensure_connectors_registered

    _ensure_connectors_registered()

    synced: Dict[str, int] = {}
    skipped: List[str] = []
    errors: Dict[str, str] = {}

    keys = sorted(ConnectorRegistry.keys())
    if only:
        wanted = set(only)
        keys = [k for k in keys if k in wanted]

    with KnowledgeStore() as store:
        pipeline = IngestionPipeline(store=store)
        engine = SyncEngine(pipeline=pipeline)

        for connector_id in keys:
            try:
                instance = ConnectorRegistry.get(connector_id)()
            except Exception as exc:  # registry / construction failure
                logger.debug("Startup sync: cannot build %s: %s", connector_id, exc)
                continue

            try:
                if not instance.is_connected():
                    skipped.append(connector_id)
                    continue
            except Exception as exc:
                logger.debug(
                    "Startup sync: is_connected() failed for %s: %s",
                    connector_id,
                    exc,
                )
                skipped.append(connector_id)
                continue

            try:
                count = engine.sync(instance)
                synced[connector_id] = count
                logger.info(
                    "Startup sync: %s refreshed (%d new item(s))",
                    connector_id,
                    count,
                )
            except Exception as exc:
                errors[connector_id] = str(exc)
                logger.warning("Startup sync: %s failed: %s", connector_id, exc)

    logger.info(
        "Startup sync complete: %d synced, %d skipped, %d error(s)",
        len(synced),
        len(skipped),
        len(errors),
    )
    return {"synced": synced, "skipped": skipped, "errors": errors}


__all__ = ["sync_connected_connectors"]
