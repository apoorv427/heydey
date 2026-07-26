"""`heydey init` (W1/B1): folders -> workspace -> ingest -> gate -> TTFR, non-interactive."""

import json

from heydey import config, init_cli, vector_store, workspaces


def test_init_end_to_end_non_interactive(heydey_home, tmp_path, monkeypatch, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("The wrapper ban forbids langchain — the core is hand-built and banned deps are CI-tested.")
    (docs / "b.md").write_text("The cross-model validator rule: executor family differs from validator family, fail-closed.")
    monkeypatch.setattr(vector_store, "embed_texts", lambda texts: [[0.1] * 384 for _ in texts])

    rc = init_cli.main(["--workspace", "w1", "--root", str(docs), "--yes"])
    assert rc == 0

    cfg = json.loads(config.corpus_config_path().read_text())
    assert {"root": str(docs), "glob": "**/*.md"} in cfg["sources"]

    conn = workspaces.connect("w1")
    assert vector_store.count_points(conn) >= 2
    conn.close()

    out = capsys.readouterr().out
    assert "TTFR" in out and "ingest: 2 files" in out

    # idempotent + non-clobbering: run again, no dup sources, no dup points
    rc = init_cli.main(["--workspace", "w1", "--root", str(docs), "--yes"])
    assert rc == 0
    cfg2 = json.loads(config.corpus_config_path().read_text())
    assert cfg2["sources"].count({"root": str(docs), "glob": "**/*.md"}) == 1


def test_init_refuses_empty(heydey_home, capsys):
    assert init_cli.main(["--yes"]) == 2
