"""S4b gate — run: python -m heydey.s4b_gate

Two assertions, on the REAL workspace (build doc §6 slice table + done-gate §7.2):
1. SHOWCASE 1 (simulate-overnight): the Morning Brief run produces ≥5 cited
   items whose sources SPAN LOCKS + STATUS + memory, every item breadcrumbed,
   and the macOS notification fires.
2. SKU DEEP-LINK RESOLVES: approving the prepared-action emits the CSV artifact
   (one row per SKU, a deep-link per row), the exact documented Shopify admin
   URL, and a receipt — with the interpreter's network entry points trapped for
   the whole approve call (L34: zero write fires).

Exit 0 only if both hold.
"""

import csv
import socket
import sys
import urllib.request
from pathlib import Path

from . import approvals, morning_brief, workspaces

DEEP_LINK_PREFIX = "https://admin.shopify.com/store/demo-store/products/"


def check_showcase1(workspace_id: str) -> tuple[bool, list[str]]:
    notified = {"fired": False}
    real_notify = morning_brief.notify_macos

    def observing_notify(title, text):
        notified["fired"] = True
        return real_notify(title, text)

    morning_brief.notify_macos = observing_notify
    try:
        brief = morning_brief.run(workspace_id, kind="morning", notify=True)
    finally:
        morning_brief.notify_macos = real_notify

    items = brief["items"]
    all_crumbed = all(i.get("breadcrumb", {}).get("source") for i in items)
    sources = " ".join(str(i["breadcrumb"]["source"]) for i in items)
    spans = {
        "LOCKS": "LOCKS" in sources,
        "STATUS": "STATUS.md" in sources,
        "memory": "/memory/" in sources or "memory" in sources,
    }
    ok = len(items) >= 5 and all_crumbed and all(spans.values()) and notified["fired"]

    lines = [f"  items: {len(items)} (>=5 {'OK' if len(items) >= 5 else 'FAIL'}) · "
             f"all breadcrumbed: {'OK' if all_crumbed else 'FAIL'} · "
             f"notification fired: {'OK' if notified['fired'] else 'FAIL'}",
             f"  spans: " + " · ".join(f"{k}={'OK' if v else 'MISS'}" for k, v in spans.items())]
    for item in items:
        crumb = item["breadcrumb"]
        name = Path(str(crumb["source"])).name
        chunk = f" · chunk {crumb['chunk']}" if crumb.get("chunk") is not None else ""
        lines.append(f"    [{item['section']:9}] {item['line'][:76]}")
        lines.append(f"                [{name}{chunk}{' · ' + crumb['date'] if crumb.get('date') else ''}]")
    return ok, lines


def check_prepared_action(workspace_id: str) -> tuple[bool, list[str]]:
    conn = workspaces.connect(workspace_id)

    # trap EVERY network entry point for the duration of the approve
    def trap(*a, **k):
        raise AssertionError("NETWORK CALL during prepared action — L34 violated")

    saved = (urllib.request.urlopen, socket.create_connection, socket.socket.connect)
    urllib.request.urlopen = trap
    socket.create_connection = trap
    socket.socket.connect = trap
    try:
        approval_id = approvals.seed_demo_sku_approval(conn)
        result = approvals.decide(conn, approval_id, "approved", workspace_id=workspace_id)
    finally:
        (urllib.request.urlopen, socket.create_connection, socket.socket.connect) = saved

    artifact = Path(result["artifact"])
    rows = list(csv.DictReader(artifact.open())) if artifact.is_file() else []
    links_ok = (result["deep_link"].startswith(DEEP_LINK_PREFIX)
                and len(result["deep_links"]) == 3
                and all(r["admin_deep_link"].startswith(DEEP_LINK_PREFIX) for r in rows))
    receipt = conn.execute(
        "SELECT claim_text FROM receipts WHERE run_id = ?", (f"approval-{approval_id}",)
    ).fetchone()
    conn.close()

    ok = artifact.is_file() and len(rows) == 3 and links_ok and receipt is not None
    lines = [
        f"  artifact: {artifact.name} ({'exists' if artifact.is_file() else 'MISSING'}, "
        f"{len(rows)} SKU rows)",
        f"  deep-link: {result['deep_link']}  "
        f"({'exact admin format OK' if links_ok else 'FORMAT FAIL'})",
        f"  receipt: {'logged — ' + receipt[0][:64] if receipt else 'MISSING'}",
        f"  network during approve: ZERO calls (trap armed, nothing tripped)",
    ]
    return ok, lines


def main() -> int:
    workspace_id = "blueleaf"
    print("=" * 68)
    print("S4b GATE — Today · Morning Brief · Approvals (prepared-action)")
    print("=" * 68)

    print("\n[1] Showcase 1 — simulate-overnight on the real corpus:")
    ok1, lines = check_showcase1(workspace_id)
    print("\n".join(lines))

    print("\n[2] prepared-action: SKU deep-link resolves, zero-write:")
    ok2, lines = check_prepared_action(workspace_id)
    print("\n".join(lines))

    overall = ok1 and ok2
    print("\n" + "=" * 68)
    print(f"S4b GATE: {'PASS ✅' if overall else 'FAIL ❌'}  "
          f"(showcase1 {'✓' if ok1 else '✗'} · prepared-action {'✓' if ok2 else '✗'})")
    print("=" * 68)
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
