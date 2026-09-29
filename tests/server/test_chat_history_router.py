"""Chat conversations are mirrored into the knowledge store."""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from openjarvis.connectors.store import KnowledgeStore  # noqa: E402
from openjarvis.server import chat_history_router as ch  # noqa: E402


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db = tmp_path / "knowledge.db"
    monkeypatch.setattr(ch, "_open_store", lambda: KnowledgeStore(db_path=db))
    app = FastAPI()
    app.include_router(ch.router)
    client = TestClient(app)
    client.db = db  # type: ignore[attr-defined]
    return client


def _conv(text: str, conv_id: str = "c1") -> dict:
    return {
        "id": conv_id,
        "title": "New chat",
        "updatedAt": 1790700000000,
        "messages": [
            {"role": "user", "content": text},
            {"role": "assistant", "content": "Entendido, señor."},
        ],
    }


def _search(db: Path, query: str) -> list:
    with KnowledgeStore(db_path=db) as store:
        return store.retrieve(query, source=ch.SOURCE)


def test_indexed_conversation_is_searchable(client: TestClient) -> None:
    res = client.post(
        "/v1/chat-history/index",
        json={"conversations": [_conv("Hablemos de DadoMatch y su backend")]},
    )
    assert res.json() == {"indexed": 1, "chunks": 1}
    hits = _search(client.db, "DadoMatch")
    assert hits and "Felipe: Hablemos de DadoMatch" in hits[0].content


def test_reindex_replaces_previous_version(client: TestClient) -> None:
    client.post("/v1/chat-history/index", json={"conversations": [_conv("pinguino")]})
    client.post("/v1/chat-history/index", json={"conversations": [_conv("jirafa")]})
    assert _search(client.db, "pinguino") == []
    assert len(_search(client.db, "jirafa")) == 1


def test_delete_forgets_conversation(client: TestClient) -> None:
    client.post("/v1/chat-history/index", json={"conversations": [_conv("koala")]})
    assert client.delete("/v1/chat-history/c1").json() == {"deleted": True}
    assert _search(client.db, "koala") == []


def test_long_conversation_is_chunked_between_messages() -> None:
    messages = [{"role": "user", "content": "x" * 900} for _ in range(4)]
    chunks = ch.chunk_transcript(messages)
    assert len(chunks) == 4
    assert all(c.startswith("Felipe: ") for c in chunks)


def test_bad_payload_is_ignored(client: TestClient) -> None:
    assert client.post("/v1/chat-history/index", json={"x": 1}).json() == {
        "indexed": 0,
        "chunks": 0,
    }


def test_chunks_are_marked_as_past_conversations(client: TestClient) -> None:
    client.post("/v1/chat-history/index", json={"conversations": [_conv("lemur")]})
    content = _search(client.db, "lemur")[0].content
    assert content.startswith(
        "[Lo que Felipe le dijo a JARVIS en una conversación pasada del 2026-09-"
    )


def test_search_falls_back_to_any_term(tmp_path: Path) -> None:
    with KnowledgeStore(db_path=tmp_path / "k.db") as store:
        store.store("DadoMatch ya está en la App Store", source="apple_notes")
        # AND of both words finds nothing; the fallback still does.
        assert store.retrieve("DadoMatch project")
        assert store.retrieve("¿dado match?")  # punctuation + split spelling
        assert store.retrieve("zzz qqq") == []


def test_only_the_users_side_is_indexed(client: TestClient) -> None:
    client.post("/v1/chat-history/index", json={"conversations": [_conv("nutria")]})
    content = _search(client.db, "nutria")[0].content
    assert "Felipe: nutria" in content and "JARVIS:" not in content


def test_knowledge_search_hides_chats_unless_asked(tmp_path: Path) -> None:
    from openjarvis.tools.knowledge_search import KnowledgeSearchTool

    store = KnowledgeStore(db_path=tmp_path / "k.db")
    store.store("DadoMatch en la nota", source="apple_notes")
    store.store("Felipe: hablemos de DadoMatch", source=ch.SOURCE)
    tool = KnowledgeSearchTool(store=store)
    default = tool.execute(query="DadoMatch").content
    assert "apple_notes" in default and ch.SOURCE not in default
    chats = tool.execute(query="DadoMatch", source=ch.SOURCE).content
    assert ch.SOURCE in chats


def test_search_bridges_spellings(tmp_path: Path) -> None:
    with KnowledgeStore(db_path=tmp_path / "k.db") as store:
        store.store("modifica el nodo de DadoMatchProject", source="a")
        store.store("consulta dado match project", source="b")
        sources = {r.source for r in store.retrieve("DadoMatch")}
        assert sources == {"a", "b"}
