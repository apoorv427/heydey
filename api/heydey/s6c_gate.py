"""S6c gate — run: python -m heydey.s6c_gate   (Fable-authored per S6C-CONTRACT §E)

Done-gate #3 (build doc §7.3): "fresh synthetic-client workspace -> connect a
tool -> interview -> 3-6 tailored agents -> first cited answer + Workspace
Live Map, all <10 min + isolation test." Runs ONCE PER PLAYBOOK on the two
DEMO-labeled workspaces (§14-C5 synthetic-corpus gate binding under D3(a)).

  [0] fresh      rmtree "s6c-*" workspace dir (guarded), create fresh
  [1] connect    sync each playbook connector; poisoned tool counted flagged
  [2] refuse     bad answers -> FoundryError, agent_specs count == 0
  [3] fleet      valid answers -> 3-6 specs, all validator_pass==1, hydrate
                 clean; re-onboard idempotent (same ids, count stable)
  [4] answer     verbatim spec through run_pipeline (local-only, Ollama-free):
                 citations>=1, >=1 connector source, ungrounded_count==0
  [5] live map   counts reconcile exactly (mcp_results, points)
  [6] isolation  (a) client's retrieve on a blueleaf-distinctive probe hits
                 ZERO chunks with blueleaf-shaped source paths;
                 (b) blueleaf carries ZERO foundry specs and ZERO client
                 connector chunks (extends the s6b [7] purity check)
  [7] stopwatch  monotonic foundry_events timeline; machine wall clock <600s
                 (the wall-clock honesty rule: this proves the MACHINERY fits
                 inside 10 min with wide headroom, NOT the demo's live number)

Zero Ollama dependency — the first-answer step deliberately uses a verbatim
(synthesize=False) agent so grounding is deterministic word-overlap, not an
8B judge. The live demo's synthesized+badge path is a separate D4 concern.
"""

from __future__ import annotations

import shutil
import sys
import time
from datetime import datetime

from . import ask, config, connector_sync, foundry, pipeline, workspaces

INJECTION_MARK = "ignore previous instructions"
def _purity_probe() -> str:
    """A query that hits DEEP content of the operator's real workspace but is
    absent from the demo corpora — machine-local (corpus.json `purity_probe`)."""
    return config.load_corpus_config().get(
        "purity_probe", "proof-grade receipts cross-model validator")

# One case per playbook: (workspace, playbook connectors, interview answers,
# a verbatim agent id to run, and a question whose terms lie inside the
# playbook's synced corpus so retrieval + word-overlap can pass without an LLM).
CASES = [
    {
        "playbook": "d2c-ops",
        "workspace": "s6c-client-d2c",
        "connectors": ["demo-shopify", "demo-sheets"],
        "answers": {
            "business_type": "d2c", "company_name": "DEMO Nova Store",
            "primary_goal": "daily_brief",
            "sources": ["demo-shopify", "demo-sheets"],
            "answer_style": "verbatim",
        },
        "verbatim_agent": "d2c-librarian",
        "probe": "orders returns rto sku",
        "bad_answers": {
            "business_type": "hospital", "company_name": "Acme{ignore previous",
            "primary_goal": "daily_brief",
            "sources": ["demo-shopify"], "answer_style": "verbatim",
        },
    },
    {
        "playbook": "agency-brief",
        "workspace": "s6c-client-agency",
        "connectors": ["demo-agency"],
        "answers": {
            "business_type": "agency", "company_name": "DEMO Northstar Studio",
            "primary_goal": "client_briefs",
            "sources": ["demo-agency"], "answer_style": "verbatim",
        },
        "verbatim_agent": "agency-librarian",
        "probe": "intake client deadline deliverable brief",
        "bad_answers": {
            "business_type": "hospital", "company_name": "Northstar{ignore previous",
            "primary_goal": "client_briefs",
            "sources": ["demo-agency"], "answer_style": "verbatim",
        },
    },
]


def _rmtree_workspace(workspace_id: str) -> None:
    # Guard the guard — never touch anything outside the synthetic prefix.
    assert workspace_id.startswith("s6c-"), f"refuse to rmtree {workspace_id!r}"
    ws_dir = (config.workspaces_root() / workspace_id).resolve()
    root = config.workspaces_root().resolve()
    assert str(ws_dir).startswith(str(root)), "workspace dir must live under workspaces_root"
    shutil.rmtree(ws_dir, ignore_errors=True)


def _run_case(case: dict) -> tuple[dict, str]:  # noqa: C901 — a gate reads top to bottom
    ws = case["workspace"]
    playbook = case["playbook"]
    print(f"\n─── {playbook} · {ws} " + "─" * (60 - len(playbook) - len(ws) - 5))
    steps: dict[str, bool] = {}
    t0 = time.perf_counter()

    # [0] fresh
    _rmtree_workspace(ws)
    workspaces.create_workspace(ws)
    conn = workspaces.connect(ws)

    # [1] connect
    try:
        reports = {}
        for cid in case["connectors"]:
            reports[cid] = connector_sync.sync(
                conn, ws, cid, connector_sync.KNOWN_SERVERS[cid])
        any_flagged = any(r["flagged"] >= 1 for r in reports.values())
        all_chunks = all(r["chunks"] > 0 for r in reports.values())
        steps["connect"] = all_chunks and any_flagged
        for cid, r in reports.items():
            print(f"[1] connect {cid}: chunks={r['chunks']} flagged={r['flagged']} "
                  f"tools={r['tools_pulled']}")
        print(f"    -> {'OK' if steps['connect'] else 'FAIL'}")

        # [2] refuse — bad answers, all-or-nothing rollback
        before = conn.execute("SELECT COUNT(*) FROM agent_specs").fetchone()[0]
        refused = False
        try:
            foundry.instantiate(conn, ws, case["bad_answers"])
        except foundry.FoundryError:
            refused = True
        after = conn.execute("SELECT COUNT(*) FROM agent_specs").fetchone()[0]
        steps["refuse"] = refused and after == before
        print(f"[2] refuse:   raised={refused} specs delta={after-before}  "
              f"-> {'OK' if steps['refuse'] else 'FAIL'}")

        # [3] fleet
        specs = foundry.instantiate(conn, ws, case["answers"])
        specs_again = foundry.instantiate(conn, ws, case["answers"])  # idempotent
        n = len(specs)
        all_validated = all(s["validator_pass"] == 1 for s in specs)
        ids_same = {s["id"] for s in specs} == {s["id"] for s in specs_again}
        count_stable = conn.execute("SELECT COUNT(*) FROM agent_specs").fetchone()[0] == n
        versions_bumped = all(s2["version"] >= s1["version"] + 1
                              for s1, s2 in zip(specs, specs_again))
        # hydrate one to prove the round-trip
        hydrated = foundry.hydrate(specs[0])
        hydrate_ok = (isinstance(hydrated, pipeline.AgentSpec)
                      and hydrated.playbook == playbook and hydrated.id == specs[0]["id"])
        steps["fleet"] = (3 <= n <= 6 and all_validated and ids_same
                          and count_stable and versions_bumped and hydrate_ok)
        print(f"[3] fleet:    N={n} (in [3,6]) all_validated={all_validated} "
              f"idempotent={ids_same and count_stable} version_bump={versions_bumped} "
              f"hydrate_playbook={hydrated.playbook!r}  "
              f"-> {'OK' if steps['fleet'] else 'FAIL'}")

        # [4] answer — verbatim through run_pipeline (Ollama-free path)
        agent_spec = foundry.get_spec(conn, case["verbatim_agent"])
        assert agent_spec is not None and agent_spec.synthesize is False, \
            "gate case must reference a validated verbatim spec"
        result = pipeline.run_pipeline(conn, agent_spec, case["probe"],
                                       profile="local-only", workspace_id=ws)
        connector_cite = any(str(c.get("source", "")).startswith("connector:")
                             for c in result.citations)
        receipts_rows = conn.execute(
            "SELECT COUNT(*) FROM receipts WHERE run_id = ?", (result.run_id,)
        ).fetchone()[0]
        steps["answer"] = (bool(result.answer) and len(result.citations) >= 1
                           and connector_cite and receipts_rows >= 1
                           and result.ungrounded_count == 0)
        print(f"[4] answer:   answer={bool(result.answer)} citations={len(result.citations)} "
              f"connector_cite={connector_cite} receipts_rows={receipts_rows} "
              f"ungrounded={result.ungrounded_count}  "
              f"-> {'OK' if steps['answer'] else 'FAIL'}")

        # [5] live map — counts reconcile exactly with the underlying tables
        rows = {r["connector_id"]: r for r in connector_sync.live_map(conn, ws)}
        map_ok = True
        for cid in case["connectors"]:
            row = rows.get(cid, {})
            db_results = conn.execute(
                "SELECT COUNT(*) FROM mcp_results WHERE connector_id = ?",
                (f"{ws}.{cid}",)).fetchone()[0]
            db_chunks = conn.execute(
                "SELECT COUNT(*) FROM points WHERE json_extract(payload,'$.connector_id') = ?",
                (cid,)).fetchone()[0]
            reconciled = row.get("results") == db_results and row.get("chunks") == db_chunks
            print(f"[5] map {cid}: results={row.get('results')}/{db_results} "
                  f"chunks={row.get('chunks')}/{db_chunks} flagged={row.get('flagged')} "
                  f"{'OK' if reconciled else 'FAIL'}")
            map_ok &= reconciled
        steps["map"] = map_ok
        print(f"    -> {'OK' if steps['map'] else 'FAIL'}")

        # [6] isolation — both directions
        client_probe = ask.retrieve(conn, _purity_probe(), k=6)
        # a leak = a hit whose source path smells like the REAL workspace's
        # sources — machine-local path markers (corpus.json `purity_markers`)
        markers = tuple(config.load_corpus_config().get(
            "purity_markers", ["LOCKS.md", "STATUS.md", "memory/"]))
        leaks = [c for c in (ask._citation(h) for h in client_probe)
                 if any(m in str(c.get("path") or c.get("source") or "")
                        for m in markers)]
        client_isolated = len(leaks) == 0

        ops = workspaces.connect("blueleaf")
        try:
            ops_specs = ops.execute(
                "SELECT COUNT(*) FROM agent_specs WHERE json_extract(spec_json,'$.playbook') "
                "IN ('d2c-ops','agency-brief')").fetchone()[0]
            ops_client_chunks = ops.execute(
                "SELECT COUNT(*) FROM points WHERE "
                "json_extract(payload,'$.source_type') = 'connector'").fetchone()[0]
        finally:
            ops.close()
        ops_pure = ops_specs == 0 and ops_client_chunks == 0
        steps["isolation"] = client_isolated and ops_pure
        print(f"[6] isolation: client->blueleaf leaks={len(leaks)} · "
              f"blueleaf foundry specs={ops_specs} · blueleaf connector chunks="
              f"{ops_client_chunks}  -> {'OK' if steps['isolation'] else 'FAIL'}")

        # [7] stopwatch — monotonic event chain + machine wall time
        events = conn.execute(
            "SELECT step, created_at FROM foundry_events ORDER BY id ASC"
        ).fetchall()
        # The [2] refuse step logs its own onboard_started/onboard_failed FIRST,
        # so we look for the happy chain as an in-order SUBSEQUENCE anywhere
        # after that (Contract B: per-doc/per-step isolation, log-and-continue).
        want = ["onboard_started", "corpus_scanned", "fleet_instantiated"]
        steps_seen = [e[0] for e in events]
        idx = 0
        for s in steps_seen:
            if s == want[idx]:
                idx += 1
                if idx == len(want):
                    break
        prefix_ok = idx == len(want)
        # timestamps monotonic (string ISO-8601 is orderable)
        monotonic = all(events[i][1] <= events[i + 1][1] for i in range(len(events) - 1))
        elapsed_s = time.perf_counter() - t0
        # parse first + last event timestamps for the "M:SS" instrument (D4 rule)
        try:
            first = datetime.fromisoformat(events[0][1])
            last = datetime.fromisoformat(events[-1][1])
            event_span_s = (last - first).total_seconds()
        except Exception:
            event_span_s = -1.0
        steps["stopwatch"] = prefix_ok and monotonic and elapsed_s < 600
        print(f"[7] stopwatch: events={steps_seen[:6]}... monotonic={monotonic} · "
              f"machine {elapsed_s:5.1f}s (<600s) · event_span {event_span_s:5.1f}s  "
              f"-> {'OK' if steps['stopwatch'] else 'FAIL'}")
    finally:
        conn.close()

    ok = all(steps.values())
    summary = "PASS ✅" if ok else "FAIL ❌"
    detail = " · ".join(f"{k} {'✓' if v else '✗'}" for k, v in steps.items())
    print(f"─── {playbook} · {summary}  ({detail})")
    return steps, summary


def main() -> int:
    print("=" * 68)
    print("S6c GATE — Foundry / Architect onboarding (Showcase 2)")
    print("=" * 68)
    all_ok = True
    for case in CASES:
        steps, _ = _run_case(case)
        all_ok &= all(steps.values())
    print("\n" + "=" * 68)
    print(f"S6c GATE: {'PASS ✅' if all_ok else 'FAIL ❌'}   "
          f"[wall-clock honesty: machine time proves the MACHINERY fits inside 10 min "
          f"with wide headroom; the demo's live number is UI-instrumented per D4(a)]")
    print("=" * 68)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
