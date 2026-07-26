"""Machine-local corpus config (HEYDEY_HOME/corpus.json) — the F1 de-hardcode.

The repo must carry NO operator paths or private filenames: sources, extra deny
names, reveal roots, and migration paths all come from corpus.json. These tests
pin the loader, the deny-merge, the unconfigured behavior, and reveal_roots().
"""

import json
from pathlib import Path

import pytest

from heydey import config, ops_ingest, server


def _write_cfg(data: dict) -> None:
    path = config.corpus_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_missing_config_means_no_sources(heydey_home):
    assert config.load_corpus_config() == {}
    assert ops_ingest.iter_corpus_files() == []


def test_malformed_config_raises_loudly(heydey_home):
    config.corpus_config_path().parent.mkdir(parents=True, exist_ok=True)
    config.corpus_config_path().write_text("{not json")
    with pytest.raises(ValueError):
        config.load_corpus_config()


def test_config_sources_and_deny_names_merge(heydey_home, tmp_path):
    corpus = tmp_path / "docs"
    (corpus / "Personal").mkdir(parents=True)
    (corpus / "a.md").write_text("keep me")
    (corpus / "secret-topic.md").write_text("config-denied")
    (corpus / "MEMORY.md").write_text("code-denied index")
    (corpus / "Personal" / "b.md").write_text("walled")
    _write_cfg({
        "sources": [{"root": str(corpus), "glob": "**/*.md"}],
        "deny_names": ["secret-topic.md"],
    })
    files = ops_ingest.iter_corpus_files()
    assert [f.name for f in files] == ["a.md"]  # deny-merge + structural wall both applied


def test_reveal_roots_config_plus_repo(heydey_home, tmp_path):
    repo_root = Path(server.__file__).resolve().parents[2]
    # unconfigured: repo root only
    assert server.reveal_roots() == (repo_root,)
    # configured: reveal_roots first, then source roots, repo always last; deduped
    _write_cfg({
        "reveal_roots": [str(tmp_path / "kb")],
        "sources": [{"root": str(tmp_path / "kb"), "glob": "*.md"},
                    {"root": str(tmp_path / "docs"), "glob": "*.md"}],
    })
    roots = server.reveal_roots()
    assert roots[0] == tmp_path / "kb"
    assert roots[-1] == repo_root
    assert len([r for r in roots if r == tmp_path / "kb"]) == 1
