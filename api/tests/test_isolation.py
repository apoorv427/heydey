"""S0 gate: structural workspace isolation — one SQLite file per workspace."""

import sqlite3

import pytest

from heydey import workspaces


def test_isolation_A_not_B(heydey_home):
    """A connection opened for workspace A physically cannot return B's rows."""
    path_a = workspaces.create_workspace("ws-a")
    path_b = workspaces.create_workspace("ws-b")
    assert path_a != path_b
    assert path_a.parent != path_b.parent  # separate directories, separate files

    conn = workspaces.connect("ws-a")
    conn.execute("INSERT INTO docs(doc_id, title) VALUES ('doc-a', 'alpha-only-doc')")
    conn.execute("INSERT INTO entities(label, created_at) VALUES ('alpha-only-entity', 'now')")
    conn.commit()
    conn.close()

    conn = workspaces.connect("ws-b")
    conn.execute("INSERT INTO docs(doc_id, title) VALUES ('doc-b', 'beta-only-doc')")
    conn.commit()
    conn.close()

    # A sees only A
    conn_a = workspaces.connect("ws-a")
    docs_a = {row["doc_id"] for row in conn_a.execute("SELECT doc_id FROM docs")}
    assert docs_a == {"doc-a"}
    assert (
        conn_a.execute("SELECT COUNT(*) FROM docs WHERE doc_id = 'doc-b'").fetchone()[0]
        == 0
    )

    # ...and cannot even ATTACH B's file on a live connection (authorizer denies)
    with pytest.raises(sqlite3.DatabaseError):
        conn_a.execute(f"ATTACH DATABASE '{path_b}' AS b")
    conn_a.close()

    # B sees only B
    conn_b = workspaces.connect("ws-b")
    docs_b = {row["doc_id"] for row in conn_b.execute("SELECT doc_id FROM docs")}
    assert docs_b == {"doc-b"}
    assert (
        conn_b.execute(
            "SELECT COUNT(*) FROM entities WHERE label = 'alpha-only-entity'"
        ).fetchone()[0]
        == 0
    )
    conn_b.close()


@pytest.mark.parametrize(
    "bad_id",
    ["../evil", "a/../b", "a/b", "A-Upper", "has space", "", ".", "..", "-leading", "x" * 65],
)
def test_invalid_workspace_ids_rejected(heydey_home, bad_id):
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.create_workspace(bad_id)
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.connect(bad_id)


def test_connect_refuses_unknown_workspace(heydey_home):
    with pytest.raises(workspaces.WorkspaceNotFound):
        workspaces.connect("never-created")


def test_create_twice_refused(heydey_home):
    workspaces.create_workspace("dup")
    with pytest.raises(workspaces.WorkspaceExists):
        workspaces.create_workspace("dup")
