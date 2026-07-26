"""S1: ported vector store — same behavior as the proven module, per-workspace."""

import pytest

from heydey import vector_store, workspaces

DIM = 384


def _unit_vector(axis: int) -> list[float]:
    vec = [0.0] * DIM
    vec[axis] = 1.0
    return vec


@pytest.fixture()
def conn(heydey_home):
    workspaces.create_workspace("vs")
    connection = workspaces.connect("vs")
    yield connection
    connection.close()


def _seed(conn):
    vector_store.upsert_points(conn, [
        ("p-alpha", _unit_vector(0), {"doc_id": "d1", "text": "alpha pricing memo",
                                      "source_file": "alpha.md", "chunk_index": 0}),
        ("p-beta", _unit_vector(1), {"doc_id": "d2", "text": "beta shipping note",
                                     "source_file": "beta.md", "chunk_index": 0}),
    ])


def test_upsert_and_knn_roundtrip(conn):
    _seed(conn)
    hits = vector_store.search_by_vector(conn, _unit_vector(0), limit=2)
    assert [h["point_id"] for h in hits] == ["p-alpha", "p-beta"]
    assert hits[0]["score"] > 0.99  # identical vector -> cosine similarity ~1
    assert hits[0]["payload"]["text"] == "alpha pricing memo"


def test_upsert_is_idempotent(conn):
    _seed(conn)
    _seed(conn)  # same point_ids again -> update path, no duplicates
    assert vector_store.count_points(conn) == 2


def test_keyword_search_and_phrase_fallback(conn):
    _seed(conn)
    hits = vector_store.keyword_search(conn, "pricing")
    assert [h["point_id"] for h in hits] == ["p-alpha"]
    # parser-tripping query falls back to quoted-phrase instead of raising
    assert vector_store.keyword_search(conn, 'shipping "note') is not None


def test_store_chunks_deterministic_ids(conn, monkeypatch):
    monkeypatch.setattr(vector_store, "embed_texts", lambda texts: [_unit_vector(2) for _ in texts])
    chunk = {"chunk_id": "c-1", "text": "gamma", "source_file": "g.md", "chunk_index": 0}
    vector_store.store_chunks(conn, [chunk], doc_id="d3")
    vector_store.store_chunks(conn, [chunk], doc_id="d3")  # new process would do this too
    assert vector_store.count_points(conn) == 1  # salted-hash() would have duplicated
    assert vector_store.deterministic_point_id("c-1") == vector_store.deterministic_point_id("c-1")


def test_delete_document(conn):
    _seed(conn)
    removed = vector_store.delete_document(conn, "d1")
    assert removed == 1
    assert vector_store.count_points(conn) == 1
    assert conn.execute("SELECT count(*) FROM vec_points").fetchone()[0] == 1
