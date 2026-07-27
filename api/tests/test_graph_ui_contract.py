"""The /graph surface is an instrument, not a picture — proofs for the founder's
report "the graph is not interactive at all i cant hold and move the entities.
even cant, strecth it out."

Two halves:

1. **API shape the canvas consumes.** /graph/neighbors and /graph/profile must
   keep returning the fields the UI renders (predicate + provenance on every
   hop and every relation). If the server ever drops one, the panel would have
   to invent it — cite-or-silent says it must not, so this fails first.

2. **Client contract, read off the shipped source.** The interaction gates
   (drag-pins, double-click-releases, zoom/pan, click-expands, four states) and
   two bundler regressions that shipped broken once and cost a debugging cycle:
   `selection.transition()` and `zoom.transform()` both need d3-transition's
   prototype patches, which the Next bundler drops — the built page threw
   "transition is not a function" and every zoom control was dead.
"""

from pathlib import Path

import pytest

from heydey import graph, graph_resolve, workspaces
from conftest import auth_headers

WEBAPP = Path(__file__).resolve().parents[2] / "webapp" / "app"
CANVAS = WEBAPP / "components" / "graph" / "GraphCanvas.tsx"
PANEL = WEBAPP / "components" / "GraphPanel.tsx"
PROFILE = WEBAPP / "components" / "graph" / "EntityProfile.tsx"
ROUTE = WEBAPP / "api" / "graph" / "route.ts"


# ── 1. the API shape the canvas renders ──────────────────────────────────────

@pytest.fixture()
def seeded(heydey_home):
    """A tiny typed graph: Alpha -BLOCKS-> Beta -DEPENDS_ON-> Gamma."""
    workspaces.create_workspace("uiws")
    conn = workspaces.connect("uiws")
    a = graph_resolve.resolve_entity(conn, "Alpha Corp", "org", "uiws", confidence=0.9, doc_id="d1")
    b = graph_resolve.resolve_entity(conn, "Beta Ltd", "org", "uiws", confidence=0.9, doc_id="d1")
    c = graph_resolve.resolve_entity(conn, "Gamma Works", "org", "uiws", confidence=0.9, doc_id="d2")
    graph.add_edge(conn, a, b, "BLOCKS", confidence=0.8, doc_id="d1", chunk_id="d1#0",
                   extractor="regex")
    graph.add_edge(conn, b, c, "DEPENDS_ON", confidence=0.8, doc_id="d2", chunk_id="d2#0",
                   extractor="regex")
    conn.commit()
    conn.close()
    return {"a": a, "b": b, "c": c}


def test_neighbors_rows_carry_everything_the_canvas_merges(client, seeded):
    """Click-to-expand merges rows into the canvas: it needs id/label/type to
    draw a node, `via` to attach the edge, and predicate+doc_id to label it."""
    response = client.get(f"/graph/neighbors?id={seeded['a']}&workspace=uiws&hops=2&limit=40",
                          headers=auth_headers())
    assert response.status_code == 200
    rows = response.json()
    assert isinstance(rows, list) and rows, "the canvas merges a LIST of hops"
    for row in rows:
        for field in ("id", "label", "type", "hop", "predicate", "doc_id", "via"):
            assert field in row, f"canvas reads row[{field!r}]"
        assert row["predicate"], "an unlabelled edge would render as anonymous grey"
        assert row["doc_id"], "every drawn hop must name the doc it came from"
    by_id = {r["id"]: r for r in rows}
    assert by_id[seeded["b"]]["predicate"] == "BLOCKS"
    assert by_id[seeded["c"]]["via"] == seeded["b"], "2nd hop attaches to the 1st, not the root"

    # weight is deliberately absent -> the panel draws "weight unknown", never a
    # made-up number (dashed edge in GraphCanvas)
    assert "weight" not in rows[0]


def test_profile_relations_carry_predicate_and_provenance(client, seeded):
    """Every relation row in the side panel prints its predicate AND its doc."""
    response = client.get("/graph/profile?key=Alpha Corp&workspace=uiws", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    for section in ("entity", "aliases", "mention_docs", "related", "receipts"):
        assert section in body, f"profile panel renders {section}"
    assert body["entity"]["label"] == "Alpha Corp"
    assert body["related"], "Alpha has a typed relation"
    for relation in body["related"]:
        assert relation["predicate"], "a relation without a predicate is not a relation"
        assert relation["doc_id"], "a relation without provenance must never render"
        assert {"id", "label", "type"} <= set(relation["entity"])


def test_profile_miss_answers_with_a_next_step(client, seeded):
    """The panel's not-found state prints the server's own next_step — a bare
    404 with no instruction is the blank-panel defect in another costume."""
    response = client.get("/graph/profile?key=nope-not-here&workspace=uiws", headers=auth_headers())
    assert response.status_code == 404
    body = response.json()
    assert body["detail"] and body["next_step"]


# ── 2. the client contract, read off the shipped source ──────────────────────

def _read(path: Path) -> str:
    assert path.exists(), f"missing UI file: {path}"
    return path.read_text(encoding="utf8")


def _code(path: Path) -> str:
    """Source with comment-only lines dropped — the regression guards below
    forbid CALLS, and the file documents those same calls in prose."""
    return "\n".join(line for line in _read(path).splitlines()
                     if not line.lstrip().startswith("//"))


def test_nodes_are_draggable_and_pin_where_dropped():
    source = _read(CANVAS)
    assert 'from "d3-drag"' in source, "complaint #1: nodes must be grabbable"
    assert ".on(\"drag\"" in source and "d.fx = event.x" in source, "drag must move the node"
    assert "d.pinned = true" in source, "a dropped node stays where it was dropped"
    assert "onDoubleClick" in source and "unpin" in source, "double-click releases the pin"
    assert "gestureRef.current.moved" in source, (
        "a plain click selects; only a gesture that actually moved may pin")
    assert 'fill="var(--conf-warn)"' in source, "a pin needs a visible affordance"


def test_canvas_zooms_pans_and_spreads():
    source = _read(CANVAS)
    assert 'from "d3-zoom"' in source and "scaleExtent(SCALE_EXTENT)" in source
    assert "[0.2, 8]" in source, "wheel zoom range"
    assert 'on("dblclick.zoom", null)' in source, "double-click is the unpin gesture, not zoom"
    for control in ("fitView", "resetView", "zoomBy"):
        assert f"onClick={{{control}" in source or f"onClick={{() => {control}" in source, (
            f"the {control} control is how you stretch it out")
    assert "linkDistance" in source and "chargeOf" in source, "sliders spread the layout live"


def test_canvas_never_reintroduces_the_d3_transition_bundler_trap():
    """REGRESSION. Both of these shipped once and threw in the built page:

        select(svg).transition()        -> "transition is not a function"
        zoomBehaviour.transform(sel, t) -> "interrupt is not a function"

    Both need prototype patches installed by d3-transition's bare side-effect
    import, which the bundler drops. The canvas tweens the transform itself and
    writes d3-zoom's `__zoom` state directly instead.
    """
    source = _code(CANVAS)
    assert ".transition()" not in source
    assert 'import "d3-transition"' not in source
    assert "behaviour.transform(" not in source
    assert "__zoom" in source, "programmatic zoom writes d3-zoom's own state"


def test_canvas_typed_edges_and_legend():
    source = _read(CANVAS)
    assert "link.predicate" in source, "edges are typed now — show the predicate"
    assert "colorFor(node.type)" in source, "colour by entity type"
    assert "link.weight" in source, "thickness by weight"
    assert "ResizeObserver" in source, "the viewBox tracks the container (no letterboxing)"


def test_panel_ships_four_states_with_real_next_steps():
    source = _read(PANEL)
    for state in ('phase === "loading"', 'phase === "error"', 'phase === "empty"'):
        assert state in source, f"missing state: {state}"
    assert "heydey.ops_ingest --workspace" in source, "empty state prints the exact ingest command"
    assert "heydey.graph_backfill --workspace" in source
    assert "heydey_supervisor.py" in source, "error state prints how to fix it"
    assert "retry" in source and "check again" in source, "every dead end has a way out"


def test_panel_expands_on_click_and_reports_the_delta_honestly():
    source = _read(PANEL)
    assert "/api/graph?neighbors=" in source, "clicking a node pulls its 2-hop neighbourhood"
    assert "audited paths" in source and "nothing new" in source, (
        "report what the expansion actually added, including 'nothing'")
    assert "weight: null" in source, "a merged edge reports unknown weight, never a fake one"


def test_profile_panel_prints_provenance_per_relation():
    source = _read(PROFILE)
    assert "/api/graph?profile=" in source
    assert "relation.predicate" in source and "docLabel(relation.doc_id)" in source, (
        "every relation row shows its predicate and the doc it came from")
    for state in ('phase === "loading"', 'phase === "error"', 'phase === "missing"'):
        assert state in source
    assert "no answer has cited this entity yet" in source, "empty receipts say so"


def test_route_handler_proxies_server_side_and_validates_ids():
    source = _read(ROUTE)
    assert "proxyJson" in source, "the browser never talks to the supervisor directly"
    for mode in ("/graph/neighbors?id=", "/graph/profile?key=", "/graph/entity?id=", "/graph?"):
        assert mode in source
    assert "/^\\d{1,12}$/" in source or "^\\d{1,12}$" in source, "entity ids are numeric only"
    assert "next_step" in source, "even a 400 tells you what to do instead"


def test_no_supervisor_token_in_client_components():
    """L: secrets never reach the browser. The token is read in
    app/lib/supervisor.ts (server-only) and nowhere else."""
    for path in (CANVAS, PANEL, PROFILE):
        source = _read(path)
        assert "supervisor.json" not in source
        assert "Authorization" not in source
        assert "127.0.0.1" not in source


def test_graph_ui_adds_no_npm_dependency():
    """Wrapper-free (L6): d3 and react were already dependencies; the interactive
    graph must not have pulled in a graph library."""
    imports = set()
    for path in (CANVAS, PANEL, PROFILE, WEBAPP / "components" / "graph" / "model.ts"):
        for line in _read(path).splitlines():
            if line.startswith("import ") and '"' in line:
                imports.add(line.rsplit('"', 2)[-2])
    external = {i for i in imports if not i.startswith(".")}
    assert external <= {"react", "next/link", "d3-drag", "d3-force", "d3-selection", "d3-zoom"}, (
        f"unexpected import(s): {external - {'react', 'd3-drag', 'd3-force', 'd3-selection', 'd3-zoom'}}")
