"""S0 gate: all reserved schemas present in a fresh heydey.db; migration clean."""

import struct

from heydey import workspaces
from heydey.schema import REQUIRED_TABLES, SCHEMA_VERSION, migrate, table_names


def test_all_schemas_present_in_fresh_db(heydey_home):
    workspaces.create_workspace("fresh")
    conn = workspaces.connect("fresh")
    names = table_names(conn)
    missing = REQUIRED_TABLES - names
    assert not missing, f"missing reserved tables: {sorted(missing)}"
    version = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert version == str(SCHEMA_VERSION)
    conn.close()


def test_migration_idempotent(heydey_home):
    workspaces.create_workspace("twice")
    conn = workspaces.connect("twice")
    migrate(conn)  # second run on an already-migrated db: no error, nothing lost
    assert REQUIRED_TABLES <= table_names(conn)
    conn.close()


def test_vector_and_fts_actually_functional(heydey_home):
    """The reserved vec0/fts5 tables work — proves sqlite-vec + FTS5 load for real."""
    workspaces.create_workspace("vecfts")
    conn = workspaces.connect("vecfts")

    conn.execute(
        "INSERT INTO points(point_id, doc_id, text, payload) "
        "VALUES ('p1', 'd1', 'heydey shows its receipts', '{}')"
    )
    conn.commit()
    fts_hits = conn.execute(
        "SELECT rowid FROM fts_points WHERE fts_points MATCH 'receipts'"
    ).fetchall()
    assert len(fts_hits) == 1  # the AFTER INSERT trigger fed FTS

    embedding = struct.pack("384f", *([0.5] * 384))
    conn.execute("INSERT INTO vec_points(rowid, embedding) VALUES (1, ?)", (embedding,))
    conn.commit()
    row = conn.execute(
        "SELECT rowid, distance FROM vec_points WHERE embedding MATCH ? AND k = 1",
        (embedding,),
    ).fetchone()
    assert row["rowid"] == 1
    assert row["distance"] < 1e-5  # identical vector, cosine distance ~0
    conn.close()


def test_old_schema_workspace_migrates_on_open(heydey_home):
    """Regression (S4b): a workspace created under an older SCHEMA_VERSION must
    receive new tables on next connect() — the live db, created at v2, 500'd
    /today with `no such table: briefs` because _open never version-checked."""
    from heydey import workspaces

    workspaces.create_workspace("oldws")
    conn = workspaces.connect("oldws")
    conn.execute("DROP TABLE briefs")  # simulate a pre-v3 db
    conn.execute("UPDATE schema_meta SET value = '2' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    conn = workspaces.connect("oldws")  # reopening must heal the schema
    assert conn.execute("SELECT COUNT(*) FROM briefs").fetchone()[0] == 0
    version = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    from heydey.schema import SCHEMA_VERSION

    assert int(version) == SCHEMA_VERSION
    conn.close()
