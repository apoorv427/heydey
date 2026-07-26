"""S2: ops-corpus ingester — Personal hard wall, guard wiring, idempotency."""

import pytest

from heydey import ops_ingest, vector_store, workspaces


@pytest.fixture()
def fake_corpus(heydey_home, tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    (corpus / "docs").mkdir(parents=True)
    (corpus / "Personal").mkdir()

    (corpus / "docs" / "LOCKS.md").write_text(
        "# Locks\n\nL1: pricing floor is 25L. Contact ravi@example.com for terms.\n"
    )
    (corpus / "docs" / "personal-notes.md").write_text("NEVER-INGEST-MARKER")
    (corpus / "Personal" / "diary.md").write_text("NEVER-INGEST-MARKER")
    (corpus / "docs" / "MEMORY.md").write_text("NEVER-INGEST-MARKER")

    monkeypatch.setattr(ops_ingest, "CORPUS_SOURCES", [
        (corpus, "**/*.md"),
    ])
    workspaces.create_workspace("ops")
    # embedding stubbed: ingest wiring is under test, not the model
    monkeypatch.setattr(vector_store, "embed_texts",
                        lambda texts: [[0.1] * 384 for _ in texts])
    return corpus


def test_personal_wall_and_denylist(fake_corpus):
    files = ops_ingest.iter_corpus_files()
    names = {f.name for f in files}
    assert names == {"LOCKS.md"}  # Personal/, personal-*, MEMORY.md all excluded


def test_ingest_scrubs_pii_and_is_idempotent(fake_corpus):
    report_first = ops_ingest.ingest_ops_corpus("ops")
    assert report_first["files"] == 1
    assert report_first["chunks"] >= 1
    assert report_first["pii_redactions"] == 1  # the email
    assert report_first["errors"] == []

    conn = workspaces.connect("ops")
    stored_text = " ".join(
        row["text"] for row in conn.execute("SELECT text FROM points")
    )
    assert "ravi@example.com" not in stored_text
    assert "NEVER-INGEST-MARKER" not in stored_text
    assert "[REDACTED-PII]" in stored_text
    count_first = vector_store.count_points(conn)
    conn.close()

    report_second = ops_ingest.ingest_ops_corpus("ops")  # re-run: no duplicates
    conn = workspaces.connect("ops")
    assert vector_store.count_points(conn) == count_first
    assert report_second["chunks"] == report_first["chunks"]
    conn.close()
