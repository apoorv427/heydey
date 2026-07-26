"""S5-CORE gate — run: python -m heydey.s5core_gate

Scope honesty: this is the Session Browser CORE over Heydey's own episodic log.
The FULL S5 gate (build doc §6: capture daemon over the 966 historical Claude
sessions, pause control) is NOT claimed here and remains open.

Stated assertions, on the real workspace:
1. RECALL BY INTENT: the machine-local recall probe (corpus.json `recall_probe`
   — a query naming work this installation actually did) surfaces those runs,
   top match carrying receipts.
2. PII SCRUBBED AT WRITE: an intent containing a phone number is stored
   redacted (checked via a throwaway run, deleted after).
3. FORGET FORGETS: deleting a session removes it AND its receipts.
"""

import sys

from . import config, episodic, workspaces


def main() -> int:
    conn = workspaces.connect("blueleaf")
    try:
        print("=" * 68)
        print("S5-CORE GATE — Session Browser (episodic log; capture daemon = open)")
        print("=" * 68)

        probe = config.load_corpus_config().get(
            "recall_probe", {"query": "morning brief", "expect": "brief"})
        hits = episodic.search(conn, probe["query"], limit=5)
        top = hits[0] if hits else None
        recall_ok = bool(top and probe["expect"] in (top["intent"] or "").lower()
                         and top["receipts"] > 0)
        print(f"\n[1] recall-by-intent: {len(hits)} match(es)"
              f"  -> {'OK' if recall_ok else 'FAIL'}")
        for h in hits[:3]:
            print(f"    [{h['at'][5:16]}] {h['intent'][:56]} · {h['receipts']} receipt(s)")

        run_id = "s5gate-pii-probe"
        episodic.record_run(conn, run_id, "call me on 9876543210 re the quote",
                            duration=0.0, cost=0.0, workspace_id="blueleaf")
        stored = conn.execute("SELECT intent FROM sessions WHERE id=?", (run_id,)).fetchone()[0]
        pii_ok = "9876543210" not in stored and "[REDACTED-PII]" in stored
        print(f"\n[2] pii-at-write: stored as {stored!r}"
              f"  -> {'OK' if pii_ok else 'FAIL'}")

        conn.execute(
            "INSERT INTO receipts(run_id, sentence_index, claim_text, created_at)"
            " VALUES (?, 0, 'probe receipt', datetime('now'))", (run_id,))
        conn.commit()
        deleted = episodic.delete_session(conn, run_id)
        left = conn.execute("SELECT COUNT(*) FROM receipts WHERE run_id=?", (run_id,)).fetchone()[0]
        gone = episodic.session_detail(conn, run_id) is None
        forget_ok = deleted and gone and left == 0
        print(f"\n[3] forget-forgets: deleted={deleted} · session gone={gone} · "
              f"orphan receipts={left}  -> {'OK' if forget_ok else 'FAIL'}")

        overall = recall_ok and pii_ok and forget_ok
        print("\n" + "=" * 68)
        print(f"S5-CORE GATE: {'PASS ✅' if overall else 'FAIL ❌'}  "
              f"(recall {'✓' if recall_ok else '✗'} · pii {'✓' if pii_ok else '✗'} · "
              f"forget {'✓' if forget_ok else '✗'})   [full S5 remains OPEN by design]")
        print("=" * 68)
        return 0 if overall else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
