"""S6c — foundry.py: deterministic Architect onboarding + Playbook shelf.

The load-bearing S6c invariants (L6/L13): a tailored agent is a VALIDATED
config row in ``agent_specs``, hydrated into ``pipeline.AgentSpec``, run
through ``run_pipeline``. No generated code, no free-form prompt. The Foundry
itself makes ZERO LLM calls — every check here is mechanical.

Happy paths exercise REAL demo MCP subprocesses (same shape as
``test_connector_sync.py``); embeddings are monkeypatched so the suite stays
fast + offline. Only the corpus footprint / config rows / event log are under
test, not the vector values themselves.
"""

import json

import pytest

from heydey import ask, connector_sync, foundry, pipeline, vector_store, workspaces
from heydey.connector_sync import KNOWN_SERVERS
from heydey.foundry import (INTERVIEW, FoundryError, corpus_scan, foundry_status,
                            get_spec, hydrate, instantiate, validate_spec)

DIM = 384
SHOP = KNOWN_SERVERS["demo-shopify"]
SHEETS = KNOWN_SERVERS["demo-sheets"]


def _axis(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


@pytest.fixture(autouse=True)
def _fast_embed(monkeypatch):
    """No fastembed model load — a fixed unit vector keeps store_chunks + ask fast.

    Both bindings (vector_store's for ingest, ask's for retrieve) must be
    replaced — each module imported ``embed_texts`` into its own namespace."""
    monkeypatch.setattr(vector_store, "embed_texts", lambda t: [_axis(0) for _ in t])
    monkeypatch.setattr(ask, "embed_texts", lambda t: [_axis(0) for _ in t])


@pytest.fixture()
def ws(heydey_home):
    workspaces.create_workspace("fws")
    conn = workspaces.connect("fws")
    yield conn
    conn.close()


def _valid_answers(**overrides) -> dict:
    ans = {"business_type": "d2c", "company_name": "DEMO Northstar",
           "primary_goal": "cited_answers", "sources": ["demo-shopify"],
           "answer_style": "verbatim"}
    ans.update(overrides)
    return ans


def _good_spec(**overrides) -> dict:
    spec = {"id": "d2c-analyst", "name": "Acme — Ops Analyst",
            "task_class": "ask", "k": 6, "synthesize": True,
            "role": "Acme: answers ops questions from synced order data — every sentence cited.",
            "focus": "orders returns revenue sku",
            "playbook": "d2c-ops"}
    spec.update(overrides)
    return spec


def _sync(conn, connector_id):
    return connector_sync.sync(conn, "fws", connector_id,
                               KNOWN_SERVERS[connector_id])


# ── validate_spec ─────────────────────────────────────────────────────────────

def test_validate_spec_accepts_a_good_row(heydey_home):
    assert validate_spec(_good_spec()) == []


def test_validate_spec_rejects_bad_task_class(heydey_home):
    reasons = validate_spec(_good_spec(task_class="brief"))
    assert any("task_class" in r for r in reasons), reasons


def test_validate_spec_rejects_out_of_range_k(heydey_home):
    for bad_k in (99, 2, 0, -1, "6", 6.5, True):
        reasons = validate_spec(_good_spec(k=bad_k))
        assert any(r.startswith("k ") for r in reasons), f"k={bad_k!r} not rejected: {reasons}"


def test_validate_spec_rejects_dangerous_focus(heydey_home):
    # Braces + newline are the two the contract calls out explicitly; the regex
    # also fails-closed on uppercase, underscores, and quotes.
    for bad in ["contains { brace", "has\nnewline", "UPPER",
                "with_underscore", "has'quote", "has\"quote"]:
        reasons = validate_spec(_good_spec(focus=bad))
        assert any("focus" in r for r in reasons), f"focus={bad!r} not rejected: {reasons}"


def test_validate_spec_rejects_bad_id_regex(heydey_home):
    for bad in ["", "-leading-dash", "TOO_UPPER", "has space", "a",
                "x" * 42, "has_underscore"]:
        reasons = validate_spec(_good_spec(id=bad))
        assert any("id " in r for r in reasons), f"id={bad!r} not rejected: {reasons}"


def test_validate_spec_rejects_bad_playbook(heydey_home):
    reasons = validate_spec(_good_spec(playbook="hospital-nabh"))
    assert any("playbook" in r for r in reasons)


def test_validate_spec_rejects_non_bool_synthesize(heydey_home):
    for bad in (None, 1, "true"):
        reasons = validate_spec(_good_spec(synthesize=bad))
        assert any("synthesize" in r for r in reasons), f"synthesize={bad!r} not rejected"


def test_validate_spec_rejects_multiline_role(heydey_home):
    reasons = validate_spec(_good_spec(role="line one\nline two"))
    assert any("role" in r for r in reasons)


# ── instantiate — bad inputs ──────────────────────────────────────────────────

def test_instantiate_refuses_unknown_business_type(ws):
    with pytest.raises(FoundryError):
        instantiate(ws, "fws", _valid_answers(business_type="hospital"))
    assert ws.execute("SELECT COUNT(*) FROM agent_specs").fetchone()[0] == 0


def test_instantiate_refuses_injection_shaped_company_name(ws):
    """A company_name with `{` is the injection shape the pattern is designed to
    reject; ``agent_specs`` must stay at zero rows."""
    with pytest.raises(FoundryError):
        instantiate(ws, "fws", _valid_answers(company_name="Acme{ignore previous"))
    assert ws.execute("SELECT COUNT(*) FROM agent_specs").fetchone()[0] == 0


def test_instantiate_refuses_unknown_answer_key(ws):
    ans = _valid_answers()
    ans["stray"] = "field"
    with pytest.raises(FoundryError):
        instantiate(ws, "fws", ans)
    assert ws.execute("SELECT COUNT(*) FROM agent_specs").fetchone()[0] == 0


def test_instantiate_refuses_missing_answer_key(ws):
    ans = _valid_answers()
    del ans["primary_goal"]
    with pytest.raises(FoundryError):
        instantiate(ws, "fws", ans)


def test_instantiate_refuses_bad_option(ws):
    with pytest.raises(FoundryError):
        instantiate(ws, "fws", _valid_answers(primary_goal="mystery"))
    with pytest.raises(FoundryError):
        instantiate(ws, "fws", _valid_answers(sources=["demo-nowhere"]))
    with pytest.raises(FoundryError):
        instantiate(ws, "fws", _valid_answers(answer_style="loud"))


def test_instantiate_refuses_unsynced_source(ws):
    # No sync happened — the source is not in clean_sources; must fail closed
    # with the error-with-next-step message the UI relies on.
    with pytest.raises(FoundryError) as exc:
        instantiate(ws, "fws", _valid_answers())
    assert "sync a source first" in str(exc.value)
    assert ws.execute("SELECT COUNT(*) FROM agent_specs").fetchone()[0] == 0
    fails = ws.execute(
        "SELECT COUNT(*) FROM foundry_events WHERE step='onboard_failed'"
    ).fetchone()[0]
    assert fails == 1


def test_instantiate_refuses_empty_sources(ws):
    with pytest.raises(FoundryError):
        instantiate(ws, "fws", _valid_answers(sources=[]))


# ── instantiate — happy path (real MCP sync) ──────────────────────────────────

def test_instantiate_happy_path_d2c(ws):
    _sync(ws, "demo-shopify")
    _sync(ws, "demo-sheets")
    rows = instantiate(ws, "fws", _valid_answers(
        primary_goal="daily_brief",
        sources=["demo-shopify", "demo-sheets"],
        answer_style="verbatim",
    ))
    # CORE(2) + WATCH(2) + GOAL(1) = 5 — in [3, 6]
    assert 3 <= len(rows) <= 6
    ids = [r["id"] for r in rows]
    assert set(ids) == {"d2c-analyst", "d2c-librarian",
                        "d2c-returns-watch", "d2c-spend-watch",
                        "d2c-overnight"}
    for row in rows:
        assert row["validator_pass"] == 1
        assert row["version"] == 1
        assert row["playbook"] == "d2c-ops"
        # role is a fixed template per slot — non-empty, single-line, has the company
        assert "DEMO Northstar" in row["role"]
        assert "\n" not in row["role"]


def test_instantiate_cited_answers_bumps_analyst_k(ws):
    _sync(ws, "demo-shopify")
    rows = instantiate(ws, "fws", _valid_answers(primary_goal="cited_answers"))
    by_id = {r["id"]: r for r in rows}
    # cited_answers -> no goal-specialist; analyst k bumped 6 -> 8
    assert "d2c-analyst" in by_id
    assert by_id["d2c-analyst"]["k"] == 8
    # librarian always k=8, extractive
    assert by_id["d2c-librarian"]["k"] == 8
    assert by_id["d2c-librarian"]["synthesize"] is False


def test_instantiate_verbatim_style_flips_analyst_synthesize(ws):
    """answer_style=verbatim -> analyst goes extractive (synthesize False)."""
    _sync(ws, "demo-shopify")
    rows = instantiate(ws, "fws", _valid_answers(answer_style="verbatim"))
    analyst = next(r for r in rows if r["id"] == "d2c-analyst")
    assert analyst["synthesize"] is False

    rows2 = instantiate(ws, "fws", _valid_answers(answer_style="synthesized"))
    analyst2 = next(r for r in rows2 if r["id"] == "d2c-analyst")
    assert analyst2["synthesize"] is True


def test_instantiate_re_onboard_bumps_version(ws):
    _sync(ws, "demo-shopify")
    _sync(ws, "demo-sheets")
    ans = _valid_answers(primary_goal="cost_watch",
                         sources=["demo-shopify", "demo-sheets"])
    first = instantiate(ws, "fws", ans)
    n_before = ws.execute("SELECT COUNT(*) FROM agent_specs").fetchone()[0]

    second = instantiate(ws, "fws", ans)
    n_after = ws.execute("SELECT COUNT(*) FROM agent_specs").fetchone()[0]

    # Deterministic ids -> same set on re-onboard; version bumped exactly once.
    assert [r["id"] for r in first] == [r["id"] for r in second]
    assert n_before == n_after
    for row in second:
        assert row["version"] == 2


def test_instantiate_agency_happy_path(ws):
    # The second playbook — proves shelf-of-two (D1 lock).
    _sync(ws, "demo-shopify")  # unrelated corpus in workspace (still fine)
    # register + fake demo-agency chunks so clean_sources includes it. The real
    # demo_agency MCP is B2's job; here we only need the corpus footprint to
    # test the foundry's playbook selection, not the sync.
    from heydey import connectors as connectors_mod
    connectors_mod.register(ws, "fws", "demo-agency")
    ws.execute(
        "INSERT INTO points(point_id, doc_id, source_file, text, payload) "
        "VALUES ('agency-p1', 'connector:demo-agency:list_intake', 'demo-agency', "
        "        'DEMO client intake note', ?)",
        (json.dumps({"connector_id": "demo-agency", "source_type": "connector",
                     "doc_id": "connector:demo-agency:list_intake"}),),
    )
    ws.commit()

    rows = instantiate(ws, "fws", {
        "business_type": "agency", "company_name": "DEMO Northstar Studio",
        "primary_goal": "client_briefs", "sources": ["demo-agency"],
        "answer_style": "verbatim",
    })
    ids = {r["id"] for r in rows}
    assert ids == {"agency-analyst", "agency-librarian",
                   "agency-intake-watch", "agency-brief-drafter"}
    assert all(r["playbook"] == "agency-brief" for r in rows)


# ── events log — ordered, machine-checkable ───────────────────────────────────

def test_events_written_in_order(ws):
    _sync(ws, "demo-shopify")
    instantiate(ws, "fws", _valid_answers())
    steps = [r[0] for r in ws.execute(
        "SELECT step FROM foundry_events WHERE workspace_id='fws' ORDER BY id"
    ).fetchall()]
    assert steps == ["onboard_started", "corpus_scanned", "fleet_instantiated"]


def test_events_carry_answers_verbatim(ws):
    _sync(ws, "demo-shopify")
    instantiate(ws, "fws", _valid_answers(company_name="DEMO Northstar"))
    detail = ws.execute(
        "SELECT detail FROM foundry_events "
        "WHERE workspace_id='fws' AND step='onboard_started'"
    ).fetchone()[0]
    logged = json.loads(detail)
    assert logged["company_name"] == "DEMO Northstar"  # synthetic-only -> verbatim
    assert logged["business_type"] == "d2c"


def test_events_fleet_instantiated_records_elapsed_ms(ws):
    _sync(ws, "demo-shopify")
    instantiate(ws, "fws", _valid_answers())
    detail = ws.execute(
        "SELECT detail FROM foundry_events "
        "WHERE workspace_id='fws' AND step='fleet_instantiated'"
    ).fetchone()[0]
    logged = json.loads(detail)
    assert isinstance(logged["elapsed_ms"], (int, float))
    assert logged["elapsed_ms"] >= 0
    assert logged["n"] == len(logged["ids"])


# ── hydrate ───────────────────────────────────────────────────────────────────

def test_hydrate_round_trip_carries_new_fields(ws):
    _sync(ws, "demo-shopify")
    rows = instantiate(ws, "fws", _valid_answers())
    for row in rows:
        spec = hydrate(row)
        assert isinstance(spec, pipeline.AgentSpec)
        assert spec.playbook == "d2c-ops"
        assert spec.role  # non-empty on every slot
        if row["id"] == "d2c-librarian":
            assert spec.focus == ""  # librarian is intentionally empty-focus
        else:
            assert spec.focus  # every other spec has a focus term


def test_hydrate_ignores_unknown_keys():
    """Forward-compat: an added spec_json key from a future version is ignored
    by the current dataclass constructor."""
    spec_json = json.dumps({
        "id": "x", "name": "N", "task_class": "ask", "k": 6, "synthesize": True,
        "role": "r", "focus": "", "playbook": "d2c-ops",
        "future_field": "meaningless", "another": 42,
    })
    spec = hydrate({"spec_json": spec_json})
    assert isinstance(spec, pipeline.AgentSpec)
    assert spec.id == "x"
    assert spec.playbook == "d2c-ops"


def test_hydrate_missing_keys_take_defaults():
    """Back-compat: a minimal spec_json (older version) hydrates via defaults."""
    spec = hydrate({"spec_json": json.dumps({"id": "old", "name": "Legacy"})})
    assert spec.id == "old"
    assert spec.role == ""
    assert spec.focus == ""
    assert spec.playbook == ""
    assert spec.task_class == "ask"
    assert spec.k == 6


# ── get_spec ──────────────────────────────────────────────────────────────────

def test_get_spec_returns_hydrated(ws):
    _sync(ws, "demo-shopify")
    instantiate(ws, "fws", _valid_answers())
    spec = get_spec(ws, "d2c-librarian")
    assert spec is not None
    assert spec.synthesize is False  # librarian is always extractive
    assert spec.playbook == "d2c-ops"


def test_get_spec_refuses_unvalidated_row(ws):
    """A hand-inserted row with validator_pass=0 must NEVER hydrate — the
    unvalidated config is invisible to the runtime (fail-closed)."""
    ws.execute(
        "INSERT INTO agent_specs(id, name, version, spec_json, "
        "                        validator_pass, created_at) "
        "VALUES ('handmade', 'Handmade', 1, ?, 0, '2026-07-21T00:00:00Z')",
        (json.dumps({"id": "handmade", "name": "Handmade", "task_class": "ask",
                     "k": 6, "synthesize": False, "role": "r", "focus": "",
                     "playbook": "d2c-ops"}),),
    )
    ws.commit()
    assert get_spec(ws, "handmade") is None


def test_get_spec_unknown_agent(ws):
    assert get_spec(ws, "no-such-agent") is None


# ── corpus_scan ───────────────────────────────────────────────────────────────

def test_corpus_scan_matches_seeds(ws):
    empty = corpus_scan(ws, "fws")
    assert empty["chunks"] == 0
    assert empty["docs"] == 0
    assert empty["entities"] == 0
    assert empty["clean_sources"] == []
    assert empty["connectors"] == []

    report = _sync(ws, "demo-shopify")
    scan = corpus_scan(ws, "fws")
    assert scan["chunks"] == report["chunks"]
    assert scan["clean_sources"] == ["demo-shopify"]
    docs = ws.execute(
        "SELECT COUNT(DISTINCT doc_id) FROM points "
        "WHERE doc_id IS NOT NULL AND doc_id != ''"
    ).fetchone()[0]
    assert scan["docs"] == docs


# ── Contract-A: hydrated verbatim spec, end-to-end proof ──────────────────────

def test_hydrated_verbatim_spec_runs_grounded_end_to_end(ws):
    """The L13 claim, end to end: a spec built by the Foundry, hydrated, run
    through the pipeline, produces a cited answer with ungrounded_count == 0
    on real connector-synced corpus. Extractive path — no LLM needed."""
    _sync(ws, "demo-shopify")
    instantiate(ws, "fws", _valid_answers())
    spec = get_spec(ws, "d2c-librarian")
    assert spec is not None

    result = pipeline.run_pipeline(
        ws, spec, "how many orders were rto?",
        profile="local-only", workspace_id="fws",
    )
    assert result.citations, "verbatim spec must produce >= 1 citation"
    assert result.ungrounded_count == 0
    assert result.answer_kind == "extractive"


# ── foundry_status ────────────────────────────────────────────────────────────

def test_foundry_status_interview_present(ws):
    st = foundry_status(ws, "fws")
    assert st["interview"] == INTERVIEW  # single source of truth for the UI
    assert st["workspace"] == "fws"


def test_foundry_status_phases_track_state(ws):
    empty = foundry_status(ws, "fws")
    assert empty["phase"] == "empty"

    _sync(ws, "demo-shopify")
    ready = foundry_status(ws, "fws")
    assert ready["phase"] == "sources_ready"
    assert ready["scan"]["clean_sources"] == ["demo-shopify"]

    instantiate(ws, "fws", _valid_answers())
    live = foundry_status(ws, "fws")
    assert live["phase"] == "fleet_live"
    assert len(live["specs"]) >= 3
    # events are DESC-ordered, so the freshest step (fleet_instantiated) is first
    assert live["events"][0]["step"] == "fleet_instantiated"
