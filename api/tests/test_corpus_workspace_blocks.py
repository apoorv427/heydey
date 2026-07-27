"""W3-B5 — per-workspace corpus blocks in corpus.json, routed through ops_ingest.

The isolation property under test: a workspace WITH a block ingests ONLY its
block's sources (it never inherits the operator's flat ``sources``), while a
block-less workspace keeps today's flat-sources behavior exactly (back-compat).
Proven end-to-end at the db level — two workspaces, two db files, disjoint
corpora."""

import json

import pytest

from heydey import config, ops_ingest, vector_store, workspaces


def _write_cfg(data: dict) -> None:
    path = config.corpus_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture()
def two_client_corpora(heydey_home, tmp_path, monkeypatch):
    """Flat operator sources + one block per client workspace."""
    operator = tmp_path / "operator-docs"
    client_a = tmp_path / "client-a-docs"
    client_b = tmp_path / "client-b-docs"
    for d in (operator, client_a, client_b):
        d.mkdir()
    (operator / "ops.md").write_text("OPERATOR-DOC pricing floor stays.")
    (client_a / "a1.md").write_text("CLIENT-A-DOC intake protocol v3.")
    (client_a / "a-private.md").write_text("NEVER-A-MARKER block-denied")
    (client_a / "Personal").mkdir()
    (client_a / "Personal" / "diary.md").write_text("NEVER-A-MARKER walled")
    (client_b / "b1.md").write_text("CLIENT-B-DOC billing cadence weekly.")

    _write_cfg({
        "sources": [{"root": str(operator), "glob": "*.md"}],
        "deny_names": ["global-denied.md"],
        "workspaces": {
            "client-a": {"sources": [{"root": str(client_a), "glob": "**/*.md"}],
                         "deny_names": ["a-private.md"]},
            "client-b": {"sources": [{"root": str(client_b), "glob": "*.md"}]},
        },
    })
    monkeypatch.setattr(vector_store, "embed_texts",
                        lambda texts: [[0.1] * 384 for _ in texts])
    return {"operator": operator, "client_a": client_a, "client_b": client_b}


def test_block_workspace_sees_only_its_block(two_client_corpora):
    names_a = {p.name for p in ops_ingest.iter_corpus_files("client-a")}
    assert names_a == {"a1.md"}  # block sources only; block deny + Personal wall applied


def test_block_never_unions_with_flat_sources(two_client_corpora):
    """The non-inheritance rule: operator docs must NOT leak into a client block."""
    for ws in ("client-a", "client-b"):
        files = ops_ingest.iter_corpus_files(ws)
        assert all("operator-docs" not in str(p) for p in files)


def test_blockless_workspace_falls_back_to_flat(two_client_corpora):
    names = {p.name for p in ops_ingest.iter_corpus_files("some-new-workspace")}
    assert names == {"ops.md"}  # flat sources, exactly today's behavior
    assert {p.name for p in ops_ingest.iter_corpus_files()} == {"ops.md"}  # no-arg too


def test_no_workspaces_key_is_flat_for_everyone(heydey_home, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "only.md").write_text("flat world")
    _write_cfg({"sources": [{"root": str(docs), "glob": "*.md"}]})
    assert {p.name for p in ops_ingest.iter_corpus_files("anything")} == {"only.md"}


def test_end_to_end_db_isolation(two_client_corpora):
    """Two blocks -> two db files -> disjoint corpora. The one-file-per-workspace
    rail plus block routing, proven at the SELECT level."""
    workspaces.create_workspace("client-a")
    workspaces.create_workspace("client-b")
    report_a = ops_ingest.ingest_ops_corpus("client-a")
    report_b = ops_ingest.ingest_ops_corpus("client-b")
    assert report_a["files"] == 1 and report_b["files"] == 1
    assert report_a["errors"] == [] and report_b["errors"] == []

    conn_a = workspaces.connect("client-a")
    conn_b = workspaces.connect("client-b")
    try:
        text_a = " ".join(r[0] for r in conn_a.execute("SELECT text FROM points"))
        text_b = " ".join(r[0] for r in conn_b.execute("SELECT text FROM points"))
        assert "CLIENT-A-DOC" in text_a and "CLIENT-B-DOC" in text_b
        assert "CLIENT-B-DOC" not in text_a and "CLIENT-A-DOC" not in text_b
        assert "OPERATOR-DOC" not in text_a and "OPERATOR-DOC" not in text_b
        assert "NEVER-A-MARKER" not in text_a  # block deny + Personal wall held
    finally:
        conn_a.close()
        conn_b.close()


def test_block_deny_merges_with_global(heydey_home, tmp_path):
    docs = tmp_path / "cdocs"
    docs.mkdir()
    (docs / "keep.md").write_text("keep")
    (docs / "global-denied.md").write_text("no")
    (docs / "block-denied.md").write_text("no")
    _write_cfg({
        "sources": [],
        "deny_names": ["global-denied.md"],
        "workspaces": {"c": {"sources": [{"root": str(docs), "glob": "*.md"}],
                             "deny_names": ["block-denied.md"]}},
    })
    assert {p.name for p in ops_ingest.iter_corpus_files("c")} == {"keep.md"}
