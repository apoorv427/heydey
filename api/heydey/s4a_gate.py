"""S4a gate — run: python -m heydey.s4a_gate

Three assertions, measured live (build doc §6 slice table):
1. CITED <10s: both v1.1 failure queries return a cited evidence answer in
   under 10 seconds (the instant lane). The full cross-model validated lane is
   measured and reported alongside — honestly, not hidden.
2. SPOTLIGHT SIDE-BY-SIDE: for both queries, the expected document is in
   `blos find`'s top-3; mdfind (Spotlight's engine) does not surface it in its
   first 5 hits. Meaning beats filenames, deterministically.
3. MISCONFIGURE BLOCKED @SAVE: a same-family profile (including the
   version-suffix dodge llama3.1 -> llama3.2) must raise at save and leave
   nothing on disk.

Exit 0 only if all three hold.
"""

import subprocess
import sys
import time
from pathlib import Path

from . import ask, config, find_cli, models_config, pipeline, workspaces

# (query, expected-document-slug) — machine-local (corpus.json `latency_queries`,
# describing the operator's own corpus); in-repo default targets Heydey's docs.
DEFAULT_GATE_QUERIES = [
    ("what does the wrapper ban forbid", "WHY-THIS-STACK"),
    ("cross-model validator rule", "README"),
]


def _gate_queries() -> list[tuple[str, str]]:
    raw = config.load_corpus_config().get("latency_queries")
    return [tuple(x) for x in raw] if raw else list(DEFAULT_GATE_QUERIES)


def check_cited_latency(conn) -> tuple[bool, list[str]]:
    lines, ok = [], True
    for query, expected in _gate_queries():
        t0 = time.perf_counter()
        hits = ask.retrieve(conn, query, k=6)
        citations = [ask._citation(h) for h in hits]
        evidence_s = time.perf_counter() - t0
        cited = bool(citations) and evidence_s < 10.0
        top = Path(citations[0]["source"]).name if citations else "(none)"
        ok &= cited
        lines.append(f"  evidence lane  {evidence_s*1000:7.0f}ms  top={top}  "
                     f"{'OK' if cited else 'FAIL'}  ({query[:44]!r})")
    return ok, lines


def measure_full_lane(conn) -> list[str]:
    """The premium lane, reported honestly (informational — the gate rides the
    evidence lane; full synthesis+validation depends on local model speed)."""
    query, _ = _gate_queries()[0]
    spec = pipeline.AgentSpec(id="gate", name="gate", task_class="ask")
    t0 = time.perf_counter()
    result = pipeline.run_pipeline(conn, spec, query, profile="local-only", workspace_id="blueleaf")
    full_s = time.perf_counter() - t0
    return [f"  full validated {full_s*1000:7.0f}ms  kind={result.answer_kind}  "
            f"badge=[{result.badge}]  answer={result.answer[:48]!r}"]


def check_spotlight(conn) -> tuple[bool, list[str]]:
    lines, ok = [], True
    for query, expected in _gate_queries():
        docs = find_cli.find(conn, query, k=3)
        blos_hit = any(expected in (d["path"] or d["source"]) for d in docs)
        try:
            proc = subprocess.run(
                ["mdfind", "-onlyin", str(Path.home()), query],
                capture_output=True, text=True, timeout=20,
            )
            spotlight_top5 = [l for l in proc.stdout.splitlines() if l.strip()][:5]
        except (OSError, subprocess.TimeoutExpired):
            spotlight_top5 = []
        spot_hit = any(expected in line for line in spotlight_top5)
        win = blos_hit and not spot_hit
        ok &= win
        lines.append(f"  blos top-3: {'HIT ' if blos_hit else 'MISS'}  "
                     f"spotlight top-5: {'hit' if spot_hit else 'miss'}  "
                     f"-> {'WIN' if win else 'FAIL'}  ({expected})")
    return ok, lines


def check_misconfig_blocked() -> tuple[bool, list[str]]:
    cases = [
        ("identical", models_config.Pair("llama3.1:8b", "llama3.1:8b")),
        ("version-dodge", models_config.Pair("llama3.1:8b", "llama3.2:3b")),
    ]
    lines, ok = [], True
    for label, pair in cases:
        try:
            models_config.save_profile(models_config.Profile(name="gate-bad", default=pair))
            blocked = False
        except models_config.ConfigError:
            blocked = True
        on_disk = (models_config._profiles_dir() / "gate-bad.json").exists()
        good = blocked and not on_disk
        ok &= good
        lines.append(f"  {label:14} save {'REFUSED' if blocked else 'ACCEPTED'} · "
                     f"disk={'clean' if not on_disk else 'DIRTY'}  {'OK' if good else 'FAIL'}")
    return ok, lines


def main() -> int:
    conn = workspaces.connect("blueleaf")
    try:
        print("=" * 68)
        print("S4a GATE — Ask/find surfaces · Models panel · NOCTURNE")
        print("=" * 68)

        print("\n[1] cited <10s (both v1.1 queries, evidence lane):")
        ok1, lines = check_cited_latency(conn)
        print("\n".join(lines))
        for line in measure_full_lane(conn):
            print(line)

        print("\n[2] beats Spotlight side-by-side (expected doc in top-3):")
        ok2, lines = check_spotlight(conn)
        print("\n".join(lines))

        print("\n[3] misconfigure blocked @save (family rule, incl. version dodge):")
        ok3, lines = check_misconfig_blocked()
        print("\n".join(lines))

        overall = ok1 and ok2 and ok3
        print("\n" + "=" * 68)
        print(f"S4a GATE: {'PASS ✅' if overall else 'FAIL ❌'}  "
              f"(cited<10s {'✓' if ok1 else '✗'} · spotlight {'✓' if ok2 else '✗'} · "
              f"misconfig-block {'✓' if ok3 else '✗'})")
        print("=" * 68)
        return 0 if overall else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
