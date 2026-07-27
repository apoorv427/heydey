"""Builder P — heydey/artifacts.py, the F1 "provenance view".

Proves: an approved prepared action's artifact carries its receipt end-to-end
(run_id, risk_tier, claim text) via artifacts.recent_artifacts; a file with no
matching DB row still lists, honestly, with null provenance; the Personal hard
wall and path-traversal/symlink-escape guards apply to OS-file scanning;
recent_os_files is query-time only (no background process) and degrades to []
when mdfind is unavailable; and artifact_summary's state signal (loaded |
empty | error) is reachable in all three shapes with a real next_step.
"""

import json

import pytest

from heydey import approvals, artifacts, config, workspaces


@pytest.fixture()
def art_ws(heydey_home):
    workspaces.create_workspace("artws")
    conn = workspaces.connect("artws")
    yield "artws", conn
    conn.close()


def _write_corpus_config(data: dict) -> None:
    path = config.corpus_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class _FakeProc:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def _stub_mdfind(monkeypatch, by_root: dict):
    """Route artifacts.subprocess.run -> canned mdfind output per -onlyin root."""
    def fake_run(cmd, **kwargs):
        root = cmd[cmd.index("-onlyin") + 1]
        return _FakeProc(stdout="\n".join(by_root.get(root, [])))
    monkeypatch.setattr(artifacts.subprocess, "run", fake_run)


def _no_background_process(monkeypatch):
    def trap(*a, **k):
        raise AssertionError("recent_os_files spawned a background process")
    monkeypatch.setattr(artifacts.subprocess, "Popen", trap)


# ---- 1. approved prepared action -> artifact carries provenance end-to-end -

def test_heydey_artifact_has_provenance(art_ws):
    ws_id, conn = art_ws
    approval_id = approvals.seed_demo_sku_approval(conn)
    result = approvals.decide(conn, approval_id, "approved", workspace_id=ws_id)

    items = artifacts.recent_artifacts(conn, ws_id)
    assert len(items) == 1
    item = items[0]
    assert item["source"] == "heydey"
    assert item["path"] == result["artifact"]
    assert item["kind"] == "csv"
    assert item["run_id"] == f"approval-{approval_id}"
    assert item["approval_id"] == approval_id
    assert item["risk_tier"] == result["risk_tier"]
    assert item["title"] == "Suppress 3 high-RTO SKUs?"
    assert item["receipt"] is not None
    assert "no write fired" in item["receipt"]["claim_text"]
    assert item["receipt"]["created_at"] == result["resolved_at"]


# ---- 2. file with no DB row still lists, provenance is null, never guessed -

def test_orphan_file_lists_with_null_provenance(art_ws):
    ws_id, conn = art_ws
    artifacts_dir = config.workspaces_root() / ws_id / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "manual-note.md").write_text("# not tracked by any approval")

    items = artifacts.recent_artifacts(conn, ws_id)
    assert len(items) == 1
    item = items[0]
    assert item["name"] == "manual-note.md" and item["kind"] == "md"
    assert item["source"] == "heydey"
    assert item["run_id"] is None
    assert item["approval_id"] is None
    assert item["title"] is None
    assert item["risk_tier"] is None
    assert item["receipt"] is None
    assert item["sources"] == []


# ---- 3. Personal hard wall applies to OS-file scanning ---------------------

def test_personal_dir_excluded_from_os_files(tmp_path, heydey_home, monkeypatch):
    root = tmp_path / "scanroot"
    (root / "Personal").mkdir(parents=True)
    keep = root / "notes.md"
    keep.write_text("keep")
    walled = root / "Personal" / "secret.md"
    walled.write_text("walled")
    named = root / "personal-plan.md"
    named.write_text("also walled by filename")

    _stub_mdfind(monkeypatch, {str(root): [str(keep), str(walled), str(named)]})
    _no_background_process(monkeypatch)

    items = artifacts.recent_os_files(roots=[str(root)])
    assert [i["name"] for i in items] == ["notes.md"]
    assert items[0]["source"] == "os"
    assert items[0]["run_id"] is None and items[0]["receipt"] is None  # never fabricated


# ---- 4. path traversal / symlink escape refused ----------------------------

def test_path_traversal_refused(tmp_path, heydey_home, monkeypatch):
    root = tmp_path / "scanroot"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("outside the allowed root")

    dotdot_escape = str(root / ".." / "outside" / "secret.md")
    lines = [dotdot_escape]

    symlink = root / "escape-link.md"
    try:
        symlink.symlink_to(secret)
        lines.append(str(symlink))
    except OSError:
        pass  # sandboxes without symlink support still cover the ../ case

    _stub_mdfind(monkeypatch, {str(root): lines})
    _no_background_process(monkeypatch)

    items = artifacts.recent_os_files(roots=[str(root)])
    assert items == []  # both escape shapes refused; the real file itself exists


# ---- 5. no background process; graceful [] when mdfind is unavailable -----

def test_no_background_process_and_mdfind_missing(tmp_path, heydey_home, monkeypatch):
    root = tmp_path / "scanroot"
    root.mkdir()
    (root / "would-have-matched.md").write_text("irrelevant — mdfind never returns it")
    _no_background_process(monkeypatch)

    def missing(*a, **k):
        raise FileNotFoundError("mdfind not found")
    monkeypatch.setattr(artifacts.subprocess, "run", missing)

    assert artifacts.recent_os_files(roots=[str(root)]) == []


# ---- 6. artifact_summary: all three states reachable, each with a next_step

def test_artifact_summary_empty_state(art_ws, tmp_path, monkeypatch):
    ws_id, conn = art_ws
    scanroot = tmp_path / "scanroot"
    scanroot.mkdir()
    _write_corpus_config({"artifact_roots": [str(scanroot)]})
    _stub_mdfind(monkeypatch, {str(scanroot): []})

    summary = artifacts.artifact_summary(conn, ws_id)
    assert summary["state"] == "empty"
    assert summary["heydey_count"] == 0 and summary["os_count"] == 0
    assert summary["next_step"]  # a real CTA, never blank


def test_artifact_summary_loaded_state(art_ws, tmp_path, monkeypatch):
    ws_id, conn = art_ws
    scanroot = tmp_path / "scanroot"
    scanroot.mkdir()
    _write_corpus_config({"artifact_roots": [str(scanroot)]})
    _stub_mdfind(monkeypatch, {str(scanroot): []})
    approvals.decide(
        conn, approvals.seed_demo_sku_approval(conn), "approved", workspace_id=ws_id
    )

    summary = artifacts.artifact_summary(conn, ws_id)
    assert summary["state"] == "loaded"
    assert summary["heydey_count"] == 1 and summary["os_count"] == 0


def test_artifact_summary_error_state(art_ws):
    ws_id, conn = art_ws
    # give recent_artifacts a real file to iterate, then break the connection
    # underneath it, so the failure is a genuine DB error, not an early-exit.
    approvals.decide(
        conn, approvals.seed_demo_sku_approval(conn), "approved", workspace_id=ws_id
    )
    conn.close()

    summary = artifacts.artifact_summary(conn, ws_id)
    assert summary["state"] == "error"
    assert summary["heydey_count"] == 0 and summary["os_count"] == 0
    assert summary["next_step"]  # a real next step, never blank
