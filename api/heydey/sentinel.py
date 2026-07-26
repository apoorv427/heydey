"""Sentinel — health / drift / cost / eval + graph-growth monitor (S3, core agent #4).

On-demand (and later launchd-scheduled) health sweep. Emits Morning-Brief flags; it
never auto-corrects. Deliberately NOT a nightly LLM cron — that is the exact failure
class (LightRAG) this whole subsystem exists to avoid. Every check is pure SQLite +
counters; the one place an LLM could enter (drift eval) is an explicit on-demand call,
not a batch.

Checks:
  - graph_growth    : activity_edges 24h zero-growth -> flag (anti-LightRAG canary)
  - retrieval       : the corpus is non-empty and the store answers (S2 must stay green)
  - validator_rate  : share of receipts with validator_pass over recent runs
  - cost            : today's spend vs the profile budget
  - brief_freshness : if the launchd schedule is installed, the last Morning Brief
                      must be <26h old (S4b). Manual mode (no plist) is ok-by-design.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import graph, vector_store


def _check(name: str, ok: bool, detail: str, flag: str = "") -> dict:
    return {"name": name, "status": "ok" if ok else "flag", "detail": detail,
            "flag": flag if not ok else ""}


def run_sentinel(conn: sqlite3.Connection, *, budget_usd: float = 0.0) -> dict:
    """Full sweep -> {checks: [...], flags: [...], summary}. Flags feed the Morning Brief."""
    checks: list[dict] = []

    # 1. graph growth (the anti-LightRAG canary)
    h = graph.health(conn)
    if h["edges"] == 0:
        checks.append(_check("graph_growth", False,
                             "graph has 0 activity edges — no queries have run yet",
                             "Graph cold: run a query to grow it."))
    elif h["stalled_24h"]:
        checks.append(_check("graph_growth", False,
                             f"no new activity edges in >24h (last {h['last_grown']})",
                             "Graph stalled 24h — is the query path alive?"))
    else:
        checks.append(_check("graph_growth", True,
                             f"{h['edges']} edges, {h['entities']} entities, last grew {h['last_grown']}"))

    # 2. retrieval health (S2 stays green)
    try:
        n = vector_store.count_points(conn)
        checks.append(_check("retrieval", n > 0, f"{n} points in store",
                             "Store is EMPTY — retrieval cannot answer." if n == 0 else ""))
    except Exception as exc:
        checks.append(_check("retrieval", False, f"store error: {exc}", "Vector store unreadable."))

    # 3. validator pass rate over recent receipts
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(validator_pass),0) FROM ("
        "  SELECT validator_pass FROM receipts ORDER BY id DESC LIMIT 500)"
    ).fetchone()
    total, passed = row[0], row[1]
    if total == 0:
        checks.append(_check("validator_rate", True, "no receipts yet"))
    else:
        rate = passed / total
        checks.append(_check("validator_rate", rate >= 0.5,
                             f"{passed}/{total} sentence-receipts grounded ({rate:.0%})",
                             f"Validator pass-rate low ({rate:.0%}) — retrieval or executor drift?"))

    # 4. cost vs budget
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    spend = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) FROM costs WHERE created_at LIKE ?", (f"{today}%",)
    ).fetchone()[0]
    over = budget_usd > 0 and spend > budget_usd
    checks.append(_check("cost", not over, f"today ${spend:.4f} / budget ${budget_usd:.2f}",
                         f"Over budget: ${spend:.4f} > ${budget_usd:.2f}" if over else ""))

    # 5. Morning Brief freshness (only binding once the schedule is installed)
    from . import morning_brief  # local import — avoids a module cycle at load
    if not morning_brief.launchd_plist_path().is_file():
        checks.append(_check("brief_freshness", True, "manual mode — no schedule installed"))
    else:
        row = conn.execute("SELECT MAX(created_at) FROM briefs").fetchone()
        last = row[0] if row else None
        if last is None:
            checks.append(_check("brief_freshness", False, "schedule installed, no brief ever ran",
                                 "Morning Brief scheduled but has never run — check launchd."))
        else:
            age_ok = datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(hours=26)
            checks.append(_check("brief_freshness", age_ok, f"last brief {last}",
                                 "" if age_ok else f"Morning Brief stale (last {last}) — launchd dead?"))

    flags = [c["flag"] for c in checks if c["status"] == "flag"]
    return {"checks": checks, "flags": flags,
            "summary": f"{len(checks) - len(flags)}/{len(checks)} green",
            "graph": h}
