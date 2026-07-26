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


def test_eval_prompts_default_synthetic_then_local_file(heydey_home):
    from heydey import s3_eval
    g, u, fp, inj = s3_eval._load_prompts()
    assert g == s3_eval.DEFAULT_GROUNDED and len(inj) == len(s3_eval.DEFAULT_INJECTION)
    (config.heydey_home() / "eval_prompts.json").write_text(json.dumps({
        "grounded": ["q1"], "unanswerable": [], "false_premise": [["q2", "bad"]],
        "injection": [["q3", None]],
    }))
    g, u, fp, inj = s3_eval._load_prompts()
    assert g == ["q1"] and fp == [("q2", "bad")] and inj == [("q3", None)]
    (config.heydey_home() / "eval_prompts.json").write_text("{broken")
    with pytest.raises(Exception):
        s3_eval._load_prompts()


def test_s2_gate_queries_default_compile_and_config_override(heydey_home):
    from heydey import s2_gate
    default = s2_gate._gate_queries()
    assert len(default) == 2 and all(hasattr(m, "search") for s in default for m in s["markers"])
    _write_cfg({"s2_failure_queries": [
        {"query": "my q", "expect": "x", "markers": ["foo"], "require": 1}]})
    override = s2_gate._gate_queries()
    assert len(override) == 1 and override[0]["query"] == "my q"
    assert override[0]["markers"][0].search("FOO")  # compiled, case-insensitive


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
