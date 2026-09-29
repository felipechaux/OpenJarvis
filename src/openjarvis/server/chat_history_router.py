"""Index the desktop app's chat conversations into the knowledge store.

Conversations live in the app's localStorage, so without this the server —
and ``knowledge_search`` — never sees what the user talked about with
JARVIS.  The app posts each conversation after a reply (and all of them on
startup); each one is stored as ``source="jarvis_chat"`` chunks, replacing
the previous version of that conversation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Request

router = APIRouter(prefix="/v1/chat-history", tags=["chat-history"])

SOURCE = "jarvis_chat"
_CHUNK_CHARS = 1500
_MAX_MESSAGE_CHARS = 4000
# Only the user's side is indexed: JARVIS's past replies can be wrong (e.g.
# claiming it saved something it didn't), and once retrieved they get
# repeated as facts.
_SPEAKERS = {"user": "Felipe"}


def _doc_id(conversation_id: str) -> str:
    return f"{SOURCE}:{conversation_id}"


def _lines(messages: List[Dict[str, Any]]) -> List[str]:
    out = []
    for msg in messages:
        speaker = _SPEAKERS.get(str(msg.get("role") or ""))
        text = " ".join(str(msg.get("content") or "").split())
        if speaker and text:
            out.append(f"{speaker}: {text[:_MAX_MESSAGE_CHARS]}")
    return out


def _header(updated_at: Any) -> str:
    """Marks a chunk as a record, so the model reports it instead of
    continuing it as if it were the current conversation."""
    ts = _timestamp(updated_at)
    when = f" del {ts[:10]}" if ts else ""
    return (
        f"[Lo que Felipe le dijo a JARVIS en una conversación pasada{when}. "
        "No es la conversación actual: resúmelo en pasado.]"
    )


def chunk_transcript(messages: List[Dict[str, Any]], header: str = "") -> List[str]:
    """Transcript chunks of about ``_CHUNK_CHARS``, split between messages.

    Each chunk starts with ``header`` when one is given.
    """
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for line in _lines(messages):
        if current and size + len(line) > _CHUNK_CHARS:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return [f"{header}\n{c}" for c in chunks] if header else chunks


def _timestamp(value: Any) -> str:
    try:
        seconds = float(value) / 1000.0  # the app stores epoch milliseconds
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def index_conversation(store: Any, conv: Dict[str, Any]) -> int:
    """(Re)index one conversation; returns the number of chunks stored."""
    conv_id = str(conv.get("id") or "").strip()
    if not conv_id:
        return 0
    doc_id = _doc_id(conv_id)
    store.delete(doc_id)
    chunks = chunk_transcript(
        list(conv.get("messages") or []), _header(conv.get("updatedAt"))
    )
    title = str(conv.get("title") or "Conversación con JARVIS")
    timestamp = _timestamp(conv.get("updatedAt"))
    for i, chunk in enumerate(chunks):
        store.store(
            chunk,
            source=SOURCE,
            doc_type="conversation",
            doc_id=doc_id,
            title=title,
            author="Felipe",
            participants=["Felipe", "JARVIS"],
            timestamp=timestamp or None,
            thread_id=conv_id,
            chunk_index=i,
        )
    return len(chunks)


def _open_store() -> Any:
    from openjarvis.connectors.store import KnowledgeStore

    return KnowledgeStore()


@router.post("/index")
async def index(request: Request) -> Dict[str, Any]:
    """Body: ``{"conversations": [{id, title, updatedAt, messages}]}``."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {"indexed": 0, "chunks": 0}
    convs = payload.get("conversations") if isinstance(payload, dict) else None
    if not isinstance(convs, list):
        return {"indexed": 0, "chunks": 0}
    indexed = chunks = 0
    with _open_store() as store:
        for conv in convs:
            if isinstance(conv, dict):
                n = index_conversation(store, conv)
                indexed += 1 if n else 0
                chunks += n
    return {"indexed": indexed, "chunks": chunks}


@router.delete("/{conversation_id}")
async def forget(conversation_id: str) -> Dict[str, Any]:
    with _open_store() as store:
        return {"deleted": store.delete(_doc_id(conversation_id))}
