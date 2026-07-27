"""Morning Brief — the overnight run behind the Today surface (S4b, done-gate §7.2).

Deterministic by design: the scheduled path makes NO LLM call (Executor Contract B
bans LLM-in-cron), so it cannot silently rot the way the old BLOS LightRAG nightly
did. Every item is an extract or a measured number with a breadcrumb; grounding is
by construction. Sections are isolated per-item: one failing section logs and
skips, the brief still ships (Contract B's per-document rule, applied to sections).

Sections (≥5 cited items spanning LOCKS / STATUS / memory — the gate):
  changed    docs whose mtime moved in the window (points.mtime_iso, json_extract)
  gates      open PENDING / GATE / "CEO owes" markers (FTS, recency-ordered)
  locks      the most recently touched LOCKS.md chunks
  sentinel   health sweep result (graph growth · retrieval · validator · cost)
  costs      today / 7-day spend from the ledger
  sessions   yesterday's episodic activity
  approvals  pending tray count

CLI:
  python -m heydey.morning_brief --workspace blueleaf          # simulate-overnight
  python -m heydey.morning_brief --workspace blueleaf --no-notify
  python -m heydey.morning_brief --install-launchd             # writes plist, does NOT load
"""

import argparse
import json
import logging
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import episodic, sentinel, workspaces

log = logging.getLogger(__name__)

LAUNCHD_LABEL = "ai.heydey.morningbrief"
BRIEF_HOUR = 7  # 07:00 local, daily


def _crumb(source: str, chunk: int | None = None, date: str = "") -> dict:
    return {"source": source, "chunk": chunk, "date": date}


def _payload_citation(payload: dict) -> dict:
    # ops chunks stamp the file mtime as created_at; the migrated KB uses mtime_iso
    date = (payload.get("mtime_iso") or payload.get("created_at")
            or payload.get("ingested_at") or "")[:10]
    return _crumb(
        payload.get("source_path") or payload.get("source_file") or payload.get("title") or "unknown",
        payload.get("chunk_index", 0),
        date,
    )


# ── sections (each returns list[{section, line, breadcrumb}]) ─────────────────

def _section_changed(conn: sqlite3.Connection, window_hours: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    rows = conn.execute(
        "SELECT COALESCE(json_extract(payload,'$.source_path'), json_extract(payload,'$.source_file')),"
        "       MAX(COALESCE(json_extract(payload,'$.mtime_iso'), json_extract(payload,'$.created_at'))),"
        "       COUNT(*)"
        " FROM points"
        " WHERE COALESCE(json_extract(payload,'$.mtime_iso'), json_extract(payload,'$.created_at')) > ?"
        " GROUP BY 1 ORDER BY 2 DESC LIMIT 5",
        (cutoff,),
    ).fetchall()
    items = []
    for source, mtime, chunks in rows:
        name = Path(source).name if source else "unknown"
        items.append({
            "section": "changed",
            "line": f"{name} changed · {chunks} chunk(s) current",
            "breadcrumb": _crumb(source or "unknown", None, (mtime or "")[:10]),
        })
    return items


def _section_gates(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT p.payload FROM fts_points f JOIN points p ON p.rowid = f.rowid"
        " WHERE fts_points MATCH ? ORDER BY bm25(fts_points) LIMIT 12",
        ('"CEO owes" OR "PENDING" OR "GATE" OR "blocker"',),
    ).fetchall()
    items, seen = [], set()
    for (raw,) in rows:
        payload = json.loads(raw)
        crumb = _payload_citation(payload)
        if crumb["source"] in seen:
            continue
        seen.add(crumb["source"])
        text = " ".join((payload.get("text") or "").split())
        items.append({
            "section": "gates",
            "line": text[:150],
            "breadcrumb": crumb,
        })
        if len(items) == 3:
            break
    return items


def _section_locks(conn: sqlite3.Connection) -> list[dict]:
    # canonical LOCKS.md docs first (GLOB = case-sensitive; LIKE would also match
    # memory files *about* locks, which is how the S4b gate first caught this) —
    # fall back to locks-flavored docs only when no real LOCKS.md is in the corpus
    source = ("COALESCE(json_extract(payload,'$.source_path'),"
              "         json_extract(payload,'$.source_file'))")
    mtime = ("COALESCE(json_extract(payload,'$.mtime_iso'),"
             "         json_extract(payload,'$.created_at'))")
    rows = conn.execute(
        f"SELECT payload FROM points WHERE {source} GLOB '*/LOCKS.md'"
        f" ORDER BY {mtime} DESC LIMIT 2",
    ).fetchall()
    if not rows:
        rows = conn.execute(
            f"SELECT payload FROM points WHERE {source} LIKE '%LOCKS%'"
            f" ORDER BY {mtime} DESC LIMIT 2",
        ).fetchall()
    items = []
    for (raw,) in rows:
        payload = json.loads(raw)
        text = " ".join((payload.get("text") or "").split())
        items.append({
            "section": "locks",
            "line": text[:150],
            "breadcrumb": _payload_citation(payload),
        })
    return items


def _section_sentinel(conn: sqlite3.Connection) -> list[dict]:
    report = sentinel.run_sentinel(conn, budget_usd=2.0)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    items = [{
        "section": "sentinel",
        "line": f"health {report['summary']} · graph {report['graph']['edges']} edges, "
                f"{report['graph']['entities']} entities, last grew {report['graph']['last_grown'] or 'never'}",
        "breadcrumb": _crumb(f"sentinel · {now}"),
    }]
    for flag in report["flags"]:
        items.append({
            "section": "sentinel",
            "line": f"FLAG: {flag}",
            "breadcrumb": _crumb(f"sentinel · {now}"),
        })
    return items


def _section_costs(conn: sqlite3.Connection) -> list[dict]:
    today = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM costs WHERE created_at >= date('now')"
    ).fetchone()
    week = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM costs WHERE created_at >= date('now','-7 day')"
    ).fetchone()
    return [{
        "section": "costs",
        "line": f"spend today ${today[0]:.4f} ({today[1]} calls) · 7d ${week[0]:.4f} ({week[1]} calls)",
        "breadcrumb": _crumb("costs ledger"),
    }]


def _section_sessions(conn: sqlite3.Connection) -> list[dict]:
    rows = episodic.recent(conn, n=5)
    if not rows:
        return [{
            "section": "sessions",
            "line": "no runs recorded yesterday",
            "breadcrumb": _crumb("sessions"),
        }]
    intents = " · ".join((r.get("intent") or "")[:40] for r in rows[:3])
    return [{
        "section": "sessions",
        "line": f"{len(rows)} recent run(s) · latest: {intents}",
        "breadcrumb": _crumb("sessions"),
    }]


def _section_approvals(conn: sqlite3.Connection) -> list[dict]:
    count = conn.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending'").fetchone()[0]
    line = (f"{count} approval(s) waiting in the tray" if count
            else "approvals tray clear")
    return [{"section": "approvals", "line": line, "breadcrumb": _crumb("approvals tray")}]


_SECTIONS = [
    ("changed", lambda conn, hours: _section_changed(conn, hours)),
    ("gates", lambda conn, hours: _section_gates(conn)),
    ("locks", lambda conn, hours: _section_locks(conn)),
    ("sentinel", lambda conn, hours: _section_sentinel(conn)),
    ("costs", lambda conn, hours: _section_costs(conn)),
    ("sessions", lambda conn, hours: _section_sessions(conn)),
    ("approvals", lambda conn, hours: _section_approvals(conn)),
]

# Workspace-aware sections (S6b): the core sections above take (conn, hours);
# these take (conn, workspace_id) so a Playbook can read that workspace's own
# connector rows. Registered at import IF the optional module imports cleanly —
# a broken Playbook must never take the whole brief down. Keep it dumb.
EXTRA_SECTIONS: list[tuple[str, "callable"]] = []

try:
    from . import playbook_d2c
    EXTRA_SECTIONS.append(("d2c-ops", playbook_d2c.brief_section))
except Exception as exc:  # optional section — log and ship the brief without it
    log.warning("d2c-ops section not registered: %s", exc)

try:
    from . import playbook_pm
    EXTRA_SECTIONS.append(("pm", playbook_pm.brief_section))
except Exception as exc:  # optional section — log and ship the brief without it
    log.warning("pm section not registered: %s", exc)


def build_brief(conn: sqlite3.Connection, *, window_hours: int = 48,
                workspace_id: str = "blueleaf") -> list[dict]:
    """Assemble all sections. One failing section = log + skip, never a dead brief."""
    items: list[dict] = []

    def _run(name: str, fn, arg) -> None:  # per-section isolation (Contract B)
        try:
            items.extend(fn(conn, arg))
        except Exception as exc:
            log.warning("brief section %s failed: %s", name, exc)
            items.append({
                "section": name,
                "line": f"section failed: {exc} — see logs",
                "breadcrumb": _crumb("brief runner"),
            })

    for name, fn in _SECTIONS:
        _run(name, fn, window_hours)
    for name, fn in EXTRA_SECTIONS:  # workspace_id flows to these (S6b)
        _run(name, fn, workspace_id)
    return items


def notify_macos(title: str, text: str) -> bool:
    """Fire a macOS notification. Never raises — a failed toast must not kill the brief."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{text}" with title "{title}"'],
            check=False, capture_output=True, timeout=10,
        )
        return True
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("notification failed: %s", exc)
        return False


def run(workspace_id: str, *, kind: str = "morning", notify: bool = True,
        window_hours: int = 48) -> dict:
    """The overnight run: build → persist → notify. Returns the stored brief."""
    conn = workspaces.connect(workspace_id)
    try:
        items = build_brief(conn, window_hours=window_hours, workspace_id=workspace_id)
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        notified = False
        if notify:
            cited = sum(1 for i in items if i["breadcrumb"]["source"] not in ("brief runner",))
            notified = notify_macos(
                "Heydey — Morning Brief",
                f"{cited} cited items ready. Open Today to read with receipts.",
            )
        cursor = conn.execute(
            "INSERT INTO briefs(workspace_id, kind, items, created_at, notified)"
            " VALUES (?,?,?,?,?)",
            (workspace_id, kind, json.dumps(items), created_at, int(notified)),
        )
        conn.commit()
        return {"id": cursor.lastrowid, "workspace_id": workspace_id, "kind": kind,
                "items": items, "created_at": created_at, "notified": notified}
    finally:
        conn.close()


def latest(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT id, workspace_id, kind, items, created_at, notified"
        " FROM briefs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "workspace_id": row[1], "kind": row[2],
            "items": json.loads(row[3]), "created_at": row[4], "notified": bool(row[5])}


# ── launchd (written, never auto-loaded — the CEO loads it deliberately) ──────

def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def launchd_plist(python_path: str, workspace_id: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{python_path}</string>
    <string>-m</string><string>heydey.morning_brief</string>
    <string>--workspace</string><string>{workspace_id}</string>
  </array>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>{BRIEF_HOUR}</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>/tmp/heydey-morningbrief.log</string>
  <key>StandardErrorPath</key><string>/tmp/heydey-morningbrief.log</string>
</dict></plist>
"""


def install_launchd(workspace_id: str) -> Path:
    path = launchd_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(launchd_plist(sys.executable, workspace_id))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="heydey.morning_brief", description=__doc__)
    parser.add_argument("--workspace", default="blueleaf")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--window-hours", type=int, default=48)
    parser.add_argument("--install-launchd", action="store_true",
                        help="write the daily 07:00 plist (NOT loaded — prints the load command)")
    args = parser.parse_args(argv)

    if args.install_launchd:
        path = install_launchd(args.workspace)
        print(f"plist written: {path}")
        print(f"load it deliberately with:  launchctl load {path}")
        return 0

    brief = run(args.workspace, notify=not args.no_notify, window_hours=args.window_hours)
    print(f"Morning Brief · {brief['created_at']} · {len(brief['items'])} items · "
          f"notified={brief['notified']}")
    for item in brief["items"]:
        crumb = item["breadcrumb"]
        source = Path(crumb["source"]).name if "/" in str(crumb["source"]) else crumb["source"]
        chunk = f" · chunk {crumb['chunk']}" if crumb.get("chunk") is not None else ""
        date = f" · {crumb['date']}" if crumb.get("date") else ""
        print(f"  [{item['section']:9}] {item['line'][:110]}")
        print(f"              [{source}{chunk}{date}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
