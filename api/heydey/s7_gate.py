"""S7 gate — run: python -m heydey.s7_gate   (the LAST slice)

The DPDP local-only egress switch, end-to-end on real synced connector data
(§14-C3, Contract C Layer 3). Falsifiable assertions:

  [1] switch present   Profile.egress round-trips (any | local_only)
  [2] sync             demo connector into a synthetic-client workspace
  [3] block            local_only profile w/ CLOUD executor + connector
                       content -> answer_kind=='egress-blocked'; the trap
                       proves NO cloud LLM call was made
  [4] allow            local_only profile w/ LOCAL executor + connector
                       content -> normal run (Ollama lane is fine)
  [5] permissive       egress='any' profile w/ CLOUD executor + connector
                       content -> would have called the cloud (guarded by
                       an executor_fn injection — the gate stays L6/free)
  [6] purity           blueleaf carries zero connector chunks after the run
"""

import shutil
import sys

from . import connector_sync, connectors, models_config, pipeline, workspaces
from .pipeline import AgentSpec

WS = "s7-clinic"


def _rmtree(ws: str) -> None:
    from . import config
    assert ws.startswith("s7-"), f"refuse to rmtree {ws!r}"
    shutil.rmtree(config.workspaces_root() / ws, ignore_errors=True)


def main() -> int:  # noqa: C901 — a gate reads top to bottom
    print("=" * 68)
    print("S7 GATE — DPDP local-only egress switch (Contract C Layer 3)")
    print("=" * 68)
    ok: dict[str, bool] = {}

    # [1] switch presence — a local_only profile round-trips to disk
    clinic = models_config.Profile(
        name="s7-clinic-cloud",
        default=models_config.Pair("deepseek/deepseek-chat", "qwen3:8b"),
        egress="local_only")
    models_config.save_profile(clinic)
    ok["switch"] = models_config.load_profile("s7-clinic-cloud").egress == "local_only"
    clinic_local = models_config.Profile(
        name="s7-clinic-local",
        default=models_config.Pair("llama3.1:8b", "qwen3:8b"),
        egress="local_only")
    models_config.save_profile(clinic_local)
    permissive = models_config.Profile(
        name="s7-permissive",
        default=models_config.Pair("deepseek/deepseek-chat", "qwen3:8b"),
        egress="any")
    models_config.save_profile(permissive)
    print(f"\n[1] switch: local_only round-trips = {ok['switch']}"
          f"  -> {'OK' if ok['switch'] else 'FAIL'}")

    # [2] sync a real connector into a fresh workspace
    _rmtree(WS)
    workspaces.create_workspace(WS)
    conn = workspaces.connect(WS)
    report = connector_sync.sync(conn, WS, "demo-agency",
                                 connector_sync.KNOWN_SERVERS["demo-agency"])
    ok["sync"] = report["chunks"] > 0
    print(f"\n[2] sync demo-agency: chunks={report['chunks']} flagged={report['flagged']}"
          f"  -> {'OK' if ok['sync'] else 'FAIL'}")

    # [3] block: local_only + cloud executor + connector content -> no cloud call
    calls: list[str] = []

    def trap_executor(model, prompt, system, max_tokens):
        calls.append(model)
        return "should never be called"

    spec = AgentSpec(id="clinic-analyst", name="Clinic Analyst", synthesize=True)
    blocked = pipeline.run_pipeline(conn, spec,
                                    "list the client intake requests we synced",
                                    profile="s7-clinic-cloud", workspace_id=WS,
                                    executor_fn=trap_executor)
    ok["block"] = (blocked.answer_kind == "egress-blocked" and not calls
                   and bool(blocked.answer))
    print(f"\n[3] block cloud+connector: kind={blocked.answer_kind!r} "
          f"cloud_calls={len(calls)} answer_len={len(blocked.answer)}"
          f"  -> {'OK' if ok['block'] else 'FAIL'}")

    # [4] allow: local_only + local executor + connector content -> runs normally
    calls.clear()

    def local_executor(model, prompt, system, max_tokens):
        calls.append(model)
        return "The intake list carries DEMO client requests. [1]"

    def local_validator(model, prompt, system, max_tokens):
        return '[{"i": 0, "grounded": true, "chunk": 1, "confidence": 0.9}]'

    allowed = pipeline.run_pipeline(conn, spec,
                                    "list the client intake requests we synced",
                                    profile="s7-clinic-local", workspace_id=WS,
                                    executor_fn=local_executor,
                                    validator_fn=local_validator)
    ok["allow"] = (allowed.answer_kind != "egress-blocked"
                   and calls and calls[0] == "llama3.1:8b")
    print(f"\n[4] allow local+connector: kind={allowed.answer_kind!r} "
          f"executor_called={calls}  -> {'OK' if ok['allow'] else 'FAIL'}")

    # [5] permissive: any + cloud + connector content -> cloud lane runs
    calls.clear()

    def cloud_executor(model, prompt, system, max_tokens):
        calls.append(model)
        return "Cloud answered from the synced intake rows. [1]"

    perm = pipeline.run_pipeline(conn, spec,
                                 "list the client intake requests we synced",
                                 profile="s7-permissive", workspace_id=WS,
                                 executor_fn=cloud_executor,
                                 validator_fn=local_validator)
    ok["permissive"] = (perm.answer_kind != "egress-blocked" and calls
                        and calls[0] == "deepseek/deepseek-chat")
    print(f"\n[5] permissive+cloud+connector: kind={perm.answer_kind!r} "
          f"executor_called={calls}  -> {'OK' if ok['permissive'] else 'FAIL'}")

    conn.close()

    # [6] purity: blueleaf carries zero connector chunks
    ops = workspaces.connect("blueleaf")
    ops_connector_chunks = ops.execute(
        "SELECT COUNT(*) FROM points WHERE json_extract(payload,'$.source_type')='connector'"
    ).fetchone()[0]
    ops.close()
    ok["purity"] = ops_connector_chunks == 0
    print(f"\n[6] purity: blueleaf connector chunks={ops_connector_chunks}"
          f"  -> {'OK' if ok['purity'] else 'FAIL'}")

    overall = all(ok.values())
    print("\n" + "=" * 68)
    print(f"S7 GATE: {'PASS ✅' if overall else 'FAIL ❌'}  (" +
          " · ".join(f"{k} {'✓' if v else '✗'}" for k, v in ok.items()) +
          ")   [the LAST slice — Heydey is feature-complete for the spec]")
    print("=" * 68)
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
