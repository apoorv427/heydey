"""D2C ops Playbook — the first real vertical that reads GUARDED connector rows
back out of ``mcp_results`` and turns them into founder-facing work (S6b, §C).

Two outputs, both grounded in synced Shopify orders, never in raw connector text:

  - a Morning-Brief ``d2c-ops`` section (computed RTO numbers + a connector
    breadcrumb), and
  - a prepared SKU-suppression approval built FROM the real synced rows — it
    replaces ``approvals.seed_demo_sku_approval``'s hardcoded card while keeping
    the SAME ``kind="sku_suppression"`` payload so the S4b executor runs as-is.

Security posture (Contract C, layer 1): this module reads ONLY rows the guard
already classified ``injection_risk=0``. A poisoned feed lands in ``mcp_results``
tagged and structurally EXCLUDED here (``WHERE injection_risk = 0``), so it can
never drive a brief line or an approval. Isolation is the file boundary — the db
IS the workspace — so the query never uses ``workspace_id`` as the safety filter.

Deterministic by design: pure SQL + arithmetic, no LLM call — safe on the cron/
Morning-Brief path (Contract B bans LLM-in-cron).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from . import approvals

SECTION = "d2c-ops"
# The stable citation for every d2c line — the tool the numbers come from. The
# workspace prefix on the stored connector_id is deliberately dropped: the crumb
# names the SOURCE, not the workspace (§C brief_section breadcrumb).
SOURCE_CRUMB = "connector:demo-shopify:list_orders"


def _latest_orders_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The newest CLEAN demo-shopify ``list_orders`` capture, or None.

    ``injection_risk = 0`` is the quarantine gate — flagged rows never surface
    here. ``connector_id LIKE '%.demo-shopify'`` matches the ``{workspace}.{id}``
    key the host stores; the db file already scopes us to one workspace."""
    return conn.execute(
        "SELECT raw_text, received_at FROM mcp_results"
        " WHERE connector_id LIKE '%.demo-shopify'"
        "   AND tool = 'list_orders'"
        "   AND injection_risk = 0"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _demo_product_id(sku: str) -> int:
    """A stable fake Shopify product id derived from the SKU (sha256, not the
    salted builtin ``hash``, so it is identical across processes). DEMO ONLY —
    replaced by the real product id the day a live Shopify connector lands."""
    return int(hashlib.sha256(sku.encode("utf-8")).hexdigest()[:12], 16)


def rto_stats(conn: sqlite3.Connection, workspace_id: str) -> dict | None:
    """Compute return-to-origin stats from the latest clean synced order capture.

    Parses the guarded ``raw_text`` JSON defensively — ANY shape surprise returns
    None rather than a half-built number (never build on malformed data)."""
    row = _latest_orders_row(conn)
    if row is None:
        return None
    try:
        orders = json.loads(row["raw_text"])
    except (TypeError, ValueError):
        return None
    if not isinstance(orders, list) or not orders:
        return None

    total = 0
    rto = 0
    per_sku: dict[str, list[int]] = {}  # sku -> [orders, rto]
    for order in orders:
        if not isinstance(order, dict):
            return None
        sku = order.get("sku")
        status = order.get("status")
        if not isinstance(sku, str) or not isinstance(status, str):
            return None
        total += 1
        bucket = per_sku.setdefault(sku, [0, 0])
        bucket[0] += 1
        if status == "rto":
            rto += 1
            bucket[1] += 1

    by_sku = [
        {"sku": sku, "orders": n, "rto": r, "rto_rate_pct": round(100 * r / n, 1)}
        for sku, (n, r) in per_sku.items()
    ]
    by_sku.sort(key=lambda s: (-s["rto_rate_pct"], s["sku"]))  # worst first, stable
    base = round(100 * rto / total, 1)
    return {"orders": total, "rto": rto, "rto_rate_pct": base,
            "by_sku": by_sku, "base_rate_pct": base}


def brief_section(conn: sqlite3.Connection, workspace_id: str) -> list[dict]:
    """Morning-Brief ``d2c-ops`` items (same shape as morning_brief sections).

    Returns [] when nothing is synced yet — the section simply does not render,
    never a fake line (§C: no fake data)."""
    stats = rto_stats(conn, workspace_id)
    if not stats:
        return []
    row = _latest_orders_row(conn)
    date = (row["received_at"] or "")[:10] if row is not None else ""

    def _crumb() -> dict:
        return {"source": SOURCE_CRUMB, "chunk": None, "date": date}

    top = stats["by_sku"][0]
    lines = [
        f"RTO {stats['rto_rate_pct']}% across {stats['orders']} orders "
        f"({stats['rto']} returned)",
        f"RTO concentrated in {top['sku']} "
        f"({top['rto_rate_pct']}% vs {stats['base_rate_pct']}% base)",
    ]
    return [{"section": SECTION, "line": line, "breadcrumb": _crumb()} for line in lines]


def suppression_approval(conn: sqlite3.Connection, workspace_id: str,
                         top_n: int = 3) -> int | None:
    """Build the SKU-suppression tray payload FROM the real synced rows (replaces
    ``seed_demo_sku_approval``'s hardcoded card). Returns the approval id, or None
    when there is no synced data. Keeps ``kind="sku_suppression"`` so the S4b
    prepared-action executor works unchanged."""
    stats = rto_stats(conn, workspace_id)
    if not stats:
        return None

    skus = [
        {"sku": s["sku"], "title": f"DEMO · {s['sku']}",
         "product_id": _demo_product_id(s["sku"]),  # demo id until real Shopify
         "return_rate": s["rto_rate_pct"],           # COMPUTED from synced rows
         "units_30d": s["orders"]}
        for s in stats["by_sku"][:top_n]
    ]
    payload = {
        "kind": "sku_suppression",
        "title": f"Suppress {len(skus)} high-RTO SKUs?",
        "demo": True,  # product ids are demo placeholders (rates are real)
        "store_handle": "demo-store",
        "base_return_rate": stats["base_rate_pct"],
        "skus": skus,
        "source": SOURCE_CRUMB,
    }
    return approvals.create_approval(conn, action_class="outbound", payload=payload)
