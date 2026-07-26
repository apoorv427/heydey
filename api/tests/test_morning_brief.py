"""S4b: Morning Brief — deterministic, cited, per-section isolated, no LLM in the
scheduled path (Contract B). Done-gate §7.2: ≥5 cited items spanning LOCKS/STATUS/
memory, every line breadcrumbed, notification fired."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from heydey import morning_brief, sentinel, vector_store, workspaces

DIM = 384


def _vec(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


def _payload(text: str, source_path: str, chunk: int = 0, hours_ago: float = 1.0) -> dict:
    mtime = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return {"text": text, "source_path": source_path, "chunk_index": chunk,
            "mtime_iso": mtime, "doc_id": source_path}


@pytest.fixture()
def brief_ws(heydey_home):
    """A corpus spanning LOCKS + STATUS + memory, like the real ops corpus."""
    workspaces.create_workspace("briefws")
    conn = workspaces.connect("briefws")
    vector_store.upsert_points(conn, [
        ("p1", _vec(0), _payload("LOCKS: router now · reference-board · done=proven GATE",
                                 "/x/AcmeHQ/_System/docs/phase1/LOCKS.md", 0)),
        ("p2", _vec(1), _payload("STATUS: S3 gate green, CEO owes token-sheet sign-off",
                                 "/x/AcmeHQ/_System/STATUS.md", 2)),
        ("p3", _vec(2), _payload("memory: ORION 4.5 build PENDING P-45b next",
                                 "/x/memory/beatpol-4.5-build.md", 1)),
        ("p4", _vec(3), _payload("an old chunk outside the change window",
                                 "/x/memory/old-note.md", 0, hours_ago=200.0)),
    ])
    conn.commit()
    yield "briefws", conn
    conn.close()


def test_brief_has_five_cited_items_spanning_sources(brief_ws, monkeypatch):
    ws_id, conn = brief_ws
    items = morning_brief.build_brief(conn)
    assert len(items) >= 5
    for item in items:  # every line breadcrumbed — no exceptions
        assert item["breadcrumb"]["source"], item
    sources = " ".join(str(i["breadcrumb"]["source"]) for i in items)
    assert "LOCKS" in sources and "STATUS" in sources and "memory" in sources


def test_changed_section_respects_window(brief_ws):
    _, conn = brief_ws
    changed = morning_brief._section_changed(conn, window_hours=48)
    sources = [c["breadcrumb"]["source"] for c in changed]
    assert any("STATUS.md" in s for s in sources)
    assert not any("old-note" in s for s in sources), "200h-old doc must be outside the 48h window"


def test_run_persists_and_notifies(brief_ws, monkeypatch):
    ws_id, conn = brief_ws
    calls = []
    monkeypatch.setattr(morning_brief, "notify_macos", lambda title, text: calls.append(text) or True)
    brief = morning_brief.run(ws_id, notify=True)
    assert brief["notified"] is True and calls, "notification must fire"
    stored = morning_brief.latest(conn)
    # run() used its own connection — reopen to observe
    conn2 = workspaces.connect(ws_id)
    stored = morning_brief.latest(conn2)
    conn2.close()
    assert stored is not None and stored["id"] == brief["id"]
    assert len(stored["items"]) >= 5


def test_section_failure_isolated(brief_ws, monkeypatch):
    """Contract B: one dead section logs + skips — the brief still ships."""
    _, conn = brief_ws

    def boom(conn_, hours):
        raise RuntimeError("section exploded")

    monkeypatch.setitem(
        dict(enumerate(morning_brief._SECTIONS)), 0, ("changed", boom)
    )  # replace via list mutation below — dict trick doesn't mutate the list
    original = morning_brief._SECTIONS[0]
    morning_brief._SECTIONS[0] = ("changed", boom)
    try:
        items = morning_brief.build_brief(conn)
    finally:
        morning_brief._SECTIONS[0] = original
    assert any("section failed" in i["line"] for i in items if i["section"] == "changed")
    assert any(i["section"] == "sentinel" for i in items), "later sections still ran"


def test_notify_failure_never_raises(monkeypatch):
    import subprocess as sp

    def boom(*a, **k):
        raise OSError("no osascript here")

    monkeypatch.setattr(morning_brief.subprocess, "run", boom)
    assert morning_brief.notify_macos("t", "x") is False  # False, not an exception


def test_scheduled_path_makes_no_llm_call(brief_ws, monkeypatch):
    """Contract B's banned pattern: the cron path must never touch an LLM lane."""
    _, conn = brief_ws
    from heydey import llm_client

    def trap(*a, **k):
        raise AssertionError("LLM call inside the scheduled Morning Brief path")

    monkeypatch.setattr(llm_client, "complete", trap)
    items = morning_brief.build_brief(conn)  # must complete without tripping the trap
    assert items


def test_launchd_plist_written_not_loaded(heydey_home, tmp_path, monkeypatch):
    monkeypatch.setattr(morning_brief, "launchd_plist_path", lambda: tmp_path / "brief.plist")
    path = morning_brief.install_launchd("blueleaf")
    content = path.read_text()
    assert morning_brief.LAUNCHD_LABEL in content and "--workspace" in content
    assert "<integer>7</integer>" in content  # 07:00 daily


def test_sentinel_brief_freshness_states(brief_ws, monkeypatch, tmp_path):
    ws_id, conn = brief_ws
    # manual mode: no plist -> ok
    monkeypatch.setattr(morning_brief, "launchd_plist_path", lambda: tmp_path / "absent.plist")
    report = sentinel.run_sentinel(conn)
    fresh = next(c for c in report["checks"] if c["name"] == "brief_freshness")
    assert fresh["status"] == "ok" and "manual mode" in fresh["detail"]

    # installed + never ran -> flag
    plist = tmp_path / "installed.plist"
    plist.write_text("x")
    monkeypatch.setattr(morning_brief, "launchd_plist_path", lambda: plist)
    report = sentinel.run_sentinel(conn)
    fresh = next(c for c in report["checks"] if c["name"] == "brief_freshness")
    assert fresh["status"] == "flag"

    # installed + fresh brief -> ok
    conn.execute(
        "INSERT INTO briefs(workspace_id, kind, items, created_at, notified) VALUES (?,?,?,?,1)",
        (ws_id, "morning", "[]", datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()
    report = sentinel.run_sentinel(conn)
    fresh = next(c for c in report["checks"] if c["name"] == "brief_freshness")
    assert fresh["status"] == "ok"
