"""S4c gate — run: python -m heydey.s4c_gate

Assertions (build doc §6: "live entities from S2 corpus; perf-capped"):
1. LIVE: the panel returns nodes from the real ops corpus — the configured
   anchor entities (corpus.json `graph_anchors`) present, every node label
   resolves (no <unknown>), every edge endpoint is a live entity.
2. PERF-CAPPED: <=50 nodes on the wire regardless of the requested limit, and
   the panel query answers in <250ms.
3. DRILL: a top node's detail carries its source doc + the runs that touched it.
4. HYGIENE: zero dangling edge endpoints in the whole table (the S4c regression
   — 1,344 orphaned edges were found live before the identity-preserving fix).

Exit 0 only if all hold.
"""

import sys
import time

from . import config, graph, workspaces


def main() -> int:
    conn = workspaces.connect("blueleaf")
    try:
        print("=" * 68)
        print("S4c GATE — read-only graph panel (Contract B render)")
        print("=" * 68)

        t0 = time.perf_counter()
        payload = graph.panel(conn, limit=999)  # server must cap, not trust the caller
        latency_ms = (time.perf_counter() - t0) * 1000
        nodes, edges = payload["nodes"], payload["edges"]
        labels = {n["label"] for n in nodes}
        ids = {n["id"] for n in nodes}

        # Anchor entities that must appear in this installation's live graph —
        # machine-local (corpus.json `graph_anchors`); default = the product itself.
        anchors = config.load_corpus_config().get("graph_anchors", ["Heydey"])
        anchors_ok = all(a in labels for a in anchors)
        live_ok = (anchors_ok
                   and all(n["label"] != "<unknown>" for n in nodes)
                   and all(e["source"] in ids and e["target"] in ids for e in edges))
        print(f"\n[1] live: {len(nodes)} nodes · {len(edges)} edges · "
              f"anchors {'✓' if anchors_ok else '✗'} ({', '.join(anchors)}) · no <unknown> "
              f"{'✓' if all(n['label'] != '<unknown>' for n in nodes) else '✗'}"
              f"  -> {'OK' if live_ok else 'FAIL'}")
        top = " · ".join(f"{n['label']}({n['score']})" for n in nodes[:6])
        print(f"    top-6: {top}")

        perf_ok = len(nodes) <= 50 and latency_ms < 250
        print(f"\n[2] perf-capped: nodes {len(nodes)} (<=50) · panel {latency_ms:.1f}ms (<250ms)"
              f"  -> {'OK' if perf_ok else 'FAIL'}")

        detail = graph.entity_detail(conn, nodes[0]["id"]) if nodes else None
        drill_ok = bool(detail and detail["source_doc_id"] and detail["runs"])
        if detail:
            print(f"\n[3] drill: {detail['label']} -> doc {detail['source_doc_id'].rsplit('/', 1)[-1]}"
                  f" · {len(detail['runs'])} run(s) · {len(detail['receipts'])} receipt(s)"
                  f"  -> {'OK' if drill_ok else 'FAIL'}")

        dangling = conn.execute(
            "SELECT COUNT(*) FROM activity_edges ae"
            " WHERE NOT EXISTS (SELECT 1 FROM entities e WHERE e.id=ae.entity_a)"
            "    OR NOT EXISTS (SELECT 1 FROM entities e WHERE e.id=ae.entity_b)"
        ).fetchone()[0]
        hygiene_ok = dangling == 0
        print(f"\n[4] hygiene: {dangling} dangling edge endpoint(s)"
              f"  -> {'OK' if hygiene_ok else 'FAIL'}")

        overall = live_ok and perf_ok and drill_ok and hygiene_ok
        print("\n" + "=" * 68)
        print(f"S4c GATE: {'PASS ✅' if overall else 'FAIL ❌'}  "
              f"(live {'✓' if live_ok else '✗'} · perf {'✓' if perf_ok else '✗'} · "
              f"drill {'✓' if drill_ok else '✗'} · hygiene {'✓' if hygiene_ok else '✗'})")
        print("=" * 68)
        return 0 if overall else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
