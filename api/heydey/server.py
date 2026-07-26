"""FastAPI app factory for the supervisor. Docs/openapi closed (localhost
hardening — a hard-won production lesson). S0 surface: /health + /workspaces.
S4a surface: /ask (evidence | full pipeline), /find (doc-level search),
/models (profiles + BYO keys, family rule enforced at save), /costs,
/reveal (Finder breadcrumb resolution, ops-roots only, Personal-walled).
S6c surface: /foundry/onboard (Architect run), /foundry/status (5Q interview
+ scan + specs + events — single source of truth for the /agents UI); /ask
gains an optional `agent` param that resolves to a validated agent_specs
row via foundry.get_spec (fail-closed on unknown/unvalidated) and drives
retrieval-focus/spec.k for evidence and a hydrated AgentSpec through
run_pipeline for full mode. The first successful agent-run per workspace
lands ONE `first_answer` event — the stopwatch's closing tick.

All mutations (POST/PUT) are bearer-gated by the middleware; the webapp calls
them server-side so the token never reaches a browser.
"""

import dataclasses
import json
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import __version__, approvals, ask, config, connector_sync, connectors, episodic, foundry
from . import graph, models_config, models_state, morning_brief, ops_ingest, pipeline
from . import secrets_store, sentinel, workspaces
from .auth import request_is_authorized
from .llm_client import family_of
from .schema import SCHEMA_VERSION

# Where a receipt's breadcrumb may resolve to — machine-local roots from
# corpus.json (`reveal_roots` + every source root) plus this repo. /reveal
# refuses anything else (and anything the Personal wall excludes).
def reveal_roots() -> tuple[Path, ...]:
    repo = Path(__file__).resolve().parents[2]
    cfg = config.load_corpus_config()
    roots: list[Path] = [Path(p).expanduser() for p in cfg.get("reveal_roots", [])]
    roots += [Path(s["root"]).expanduser() for s in cfg.get("sources", [])]
    ordered: list[Path] = []
    for r in (*roots, repo):
        if r not in ordered:
            ordered.append(r)
    return tuple(ordered)

KEY_PROVIDERS = {"openrouter": "OPENROUTER_API_KEY"}


class WorkspaceCreate(BaseModel):
    id: str


class AskBody(BaseModel):
    question: str
    workspace: str = "blueleaf"
    mode: str = "evidence"  # evidence (retrieval only, instant) | full (validated pipeline)
    k: int = 6
    # S6c: when set, resolves to a validated agent_specs row via foundry.get_spec.
    # None -> 404. Evidence retrieves with spec.focus + spec.k; full runs the
    # hydrated spec through run_pipeline (body.k is IGNORED — the spec IS the config).
    agent: str | None = None


class FindBody(BaseModel):
    query: str
    workspace: str = "blueleaf"
    k: int = 8


class ModelsUpdate(BaseModel):
    action: str  # activate | save | set_key
    profile: str | None = None
    profile_data: dict | None = None
    provider: str | None = None
    key: str | None = None


class RevealBody(BaseModel):
    path: str


class BriefRunBody(BaseModel):
    workspace: str = "blueleaf"
    notify: bool = True


class SessionDelete(BaseModel):
    run_id: str
    workspace: str = "blueleaf"


class ApprovalDecision(BaseModel):
    id: int
    decision: str  # approved | denied
    workspace: str = "blueleaf"


class ConnectorRegister(BaseModel):
    workspace: str = "blueleaf"
    connector_id: str  # must be a KNOWN_SERVERS key (422 otherwise)


class ConnectorSync(BaseModel):
    workspace: str = "blueleaf"
    connector_id: str


class FoundryOnboardBody(BaseModel):
    workspace: str
    answers: dict


def _profile_view(p: models_config.Profile) -> dict:
    def pair_view(pair: models_config.Pair) -> dict:
        return {
            "executor": pair.executor,
            "validator": pair.validator,
            "executor_family": family_of(pair.executor),
            "validator_family": family_of(pair.validator),
        }

    return {
        "name": p.name,
        "default": pair_view(p.default),
        "tasks": {k: pair_view(v) for k, v in p.tasks.items()},
        "budget_usd": p.budget_usd,
    }


def _reveal_allowed(path: Path) -> bool:
    resolved = path.resolve()
    if ops_ingest._excluded(resolved):  # the Personal hard wall applies to reveals too
        return False
    return any(resolved.is_relative_to(root) for root in reveal_roots())


def _log_first_answer_once(conn, workspace_id: str, detail: dict) -> None:
    """Insert ONE ``first_answer`` foundry_events row per workspace — the
    closing tick of the M:SS stopwatch. SELECT-1 guard keeps it idempotent
    across every subsequent agent-run in the same workspace (per §C)."""
    seen = conn.execute(
        "SELECT 1 FROM foundry_events WHERE workspace_id = ? AND step = 'first_answer' "
        "LIMIT 1",
        (workspace_id,),
    ).fetchone()
    if seen is not None:
        return
    conn.execute(
        "INSERT INTO foundry_events(workspace_id, step, detail, created_at) "
        "VALUES (?, 'first_answer', ?, ?)",
        (workspace_id, json.dumps(detail, default=str),
         datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()


def create_app(token: str) -> FastAPI:
    app = FastAPI(
        title="heydey-supervisor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.token = token

    @app.middleware("http")
    async def require_token_on_mutations(request: Request, call_next):
        if not request_is_authorized(request.method, request.headers, app.state.token):
            return JSONResponse(
                {"detail": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "service": "heydey-supervisor",
            "version": __version__,
            "schema_version": SCHEMA_VERSION,
            "workspaces": len(workspaces.list_workspaces()),
            "jobs": {"owner": "supervisor", "active": 0},  # single job owner (stub)
        }

    @app.get("/workspaces")
    def list_all():
        return {"workspaces": workspaces.list_workspaces()}

    @app.post("/workspaces", status_code=201)
    def create(body: WorkspaceCreate):
        try:
            path = workspaces.create_workspace(body.id)
        except workspaces.WorkspaceExists:
            return JSONResponse({"detail": "workspace exists"}, status_code=409)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        return {"id": body.id, "db": str(path)}

    # ── S4a: Ask ──────────────────────────────────────────────────────────────

    @app.post("/ask")
    def ask_endpoint(body: AskBody):
        try:
            conn = workspaces.connect(body.workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            profile = models_state.active_profile()

            # S6c: resolve the agent BEFORE any retrieval. None from get_spec is
            # both "unknown row" and "row with validator_pass != 1" — either is a
            # fail-closed 404, and neither an unvalidated config nor a missing id
            # gets to touch the corpus. body.k is IGNORED when agent is set — the
            # spec IS the config (§C in the contract).
            agent_spec = None
            if body.agent is not None:
                agent_spec = foundry.get_spec(conn, body.agent)
                if agent_spec is None:
                    return JSONResponse(
                        {"detail": "unknown or unvalidated agent"}, status_code=404
                    )

            if body.mode == "evidence":
                t0 = time.perf_counter()
                if agent_spec is not None:
                    retrieval_q = (
                        f"{body.question} {agent_spec.focus}".strip()
                        if agent_spec.focus else body.question
                    )
                    hits = ask.retrieve(conn, retrieval_q, k=agent_spec.k)
                else:
                    hits = ask.retrieve(conn, body.question, k=body.k)
                citations = [ask._citation(h) for h in hits]
                preview = ask._extractive_answer(body.question, hits)
                try:
                    # Contract B: edges are a byproduct of EVERY query, not only
                    # the full pipeline — and a graph failure never breaks an answer
                    graph.record_coretrieval(conn, f"ev-{uuid.uuid4().hex[:12]}", hits)
                except Exception:
                    pass
                # S6c: closing tick of the M:SS stopwatch — one row per workspace,
                # SELECT-1 guarded. Only fires when an agent produced the answer.
                if agent_spec is not None:
                    _log_first_answer_once(conn, body.workspace, {
                        "mode": "evidence", "agent": body.agent,
                        "citations": len(citations),
                    })
                return {
                    "mode": "evidence",
                    "question": body.question,
                    "citations": citations,
                    "preview": preview,
                    "profile": profile,
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "related_runs": episodic.recall(conn, body.question, 3),
                }
            if body.mode != "full":
                return JSONResponse({"detail": f"unknown mode {body.mode!r}"}, status_code=422)
            if agent_spec is not None:
                spec = agent_spec  # body.k IGNORED — the spec IS the config
            else:
                spec = pipeline.AgentSpec(id="ask-ui", name="Ask", task_class="ask", k=body.k)
            result = pipeline.run_pipeline(
                conn, spec, body.question, profile=profile, workspace_id=body.workspace
            )
            payload = dataclasses.asdict(result)
            payload.pop("hits", None)  # full chunk payloads stay server-side
            payload["mode"] = "full"
            payload["profile"] = profile
            if agent_spec is not None:
                _log_first_answer_once(conn, body.workspace, {
                    "run_id": result.run_id, "agent": body.agent,
                    "citations": len(result.citations),
                })
            return payload
        finally:
            conn.close()

    # ── S4a: Find (doc-level, the Library bar / blos find backend) ───────────

    @app.post("/find")
    def find_endpoint(body: FindBody):
        try:
            conn = workspaces.connect(body.workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            t0 = time.perf_counter()
            return {
                "query": body.query,
                "documents": ask.find_docs(conn, body.query, k=body.k),
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        finally:
            conn.close()

    # ── S4a: Models panel ────────────────────────────────────────────────────

    @app.get("/models")
    def models_get():
        profiles = {}
        for name in models_config.DEFAULT_PROFILES:
            try:
                profiles[name] = _profile_view(models_config.load_profile(name))
            except models_config.ConfigError:
                continue
        return {
            "active": models_state.active_profile(),
            "profiles": profiles,
            "keys": {
                provider: bool(secrets_store.get_secret(env_name))
                for provider, env_name in KEY_PROVIDERS.items()
            },
        }

    @app.put("/models")
    def models_put(body: ModelsUpdate):
        if body.action == "activate":
            if not body.profile:
                return JSONResponse({"detail": "profile required"}, status_code=422)
            try:
                models_state.set_active(body.profile)
            except models_config.ConfigError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=422)
            return {"active": models_state.active_profile()}

        if body.action == "save":
            if not body.profile_data:
                return JSONResponse({"detail": "profile_data required"}, status_code=422)
            try:
                profile = models_config._from_dict(body.profile_data)
                models_config.save_profile(profile)  # family rule enforced HERE — the gate
            except models_config.ConfigError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=422)
            except (KeyError, TypeError) as exc:
                return JSONResponse({"detail": f"malformed profile: {exc}"}, status_code=422)
            return {"saved": profile.name, "profile": _profile_view(profile)}

        if body.action == "set_key":
            env_name = KEY_PROVIDERS.get(body.provider or "")
            if env_name is None:
                return JSONResponse({"detail": f"unknown provider {body.provider!r}"}, status_code=422)
            if not body.key or len(body.key.strip()) < 8:
                return JSONResponse({"detail": "key missing or too short"}, status_code=422)
            secrets_store.set_secret(env_name, body.key.strip())
            return {"stored": True, "provider": body.provider}  # the key itself is never echoed

        return JSONResponse({"detail": f"unknown action {body.action!r}"}, status_code=422)

    # ── S4a: Cost ledger ─────────────────────────────────────────────────────

    @app.get("/costs")
    def costs_get(workspace: str = "blueleaf"):
        try:
            conn = workspaces.connect(workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            today = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM costs WHERE created_at >= date('now')"
            ).fetchone()
            week = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM costs WHERE created_at >= date('now','-7 day')"
            ).fetchone()
            rows = conn.execute(
                "SELECT run_id, model, tokens_in, tokens_out, cost_usd, latency_ms, created_at"
                " FROM costs ORDER BY id DESC LIMIT 20"
            ).fetchall()
            return {
                "today_usd": round(today[0], 6),
                "today_calls": today[1],
                "week_usd": round(week[0], 6),
                "week_calls": week[1],
                "recent": [
                    {
                        "run_id": r[0], "model": r[1], "tokens_in": r[2], "tokens_out": r[3],
                        "cost_usd": r[4], "latency_ms": r[5], "created_at": r[6],
                    }
                    for r in rows
                ],
            }
        finally:
            conn.close()

    # ── S4c: the read-only graph panel (Contract B render spec) ──────────────

    @app.get("/graph")
    def graph_panel(workspace: str = "blueleaf", limit: int = 50):
        try:
            conn = workspaces.connect(workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            t0 = time.perf_counter()
            payload = graph.panel(conn, limit=min(limit, 50))  # perf cap is server-side
            payload["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return payload
        finally:
            conn.close()

    @app.get("/graph/entity")
    def graph_entity(id: int, workspace: str = "blueleaf"):
        try:
            conn = workspaces.connect(workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            detail = graph.entity_detail(conn, id)
            if detail is None:
                return JSONResponse({"detail": f"entity {id} not found"}, status_code=404)
            return detail
        finally:
            conn.close()

    # ── S5 core: Session Browser (read + right-to-forget; capture daemon later) ─

    @app.get("/sessions")
    def sessions_list(workspace: str = "blueleaf", q: str = "", limit: int = 30):
        try:
            conn = workspaces.connect(workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            return {"sessions": episodic.search(conn, q, limit=min(limit, 100)), "query": q}
        finally:
            conn.close()

    @app.get("/sessions/detail")
    def sessions_detail(run_id: str, workspace: str = "blueleaf"):
        try:
            conn = workspaces.connect(workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            detail = episodic.session_detail(conn, run_id)
            if detail is None:
                return JSONResponse({"detail": f"session {run_id!r} not found"}, status_code=404)
            return detail
        finally:
            conn.close()

    @app.post("/sessions/delete")
    def sessions_delete(body: SessionDelete):
        try:
            conn = workspaces.connect(body.workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            if not episodic.delete_session(conn, body.run_id):
                return JSONResponse({"detail": f"session {body.run_id!r} not found"}, status_code=404)
            return {"deleted": body.run_id}
        finally:
            conn.close()

    # ── S4b: Today (brief + tray + health), simulate-overnight, decisions ────

    @app.get("/today")
    def today(workspace: str = "blueleaf"):
        try:
            conn = workspaces.connect(workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            return {
                "brief": morning_brief.latest(conn),
                "approvals": approvals.pending(conn),
                "sentinel": sentinel.run_sentinel(conn, budget_usd=2.0),
            }
        finally:
            conn.close()

    @app.post("/brief/run")
    def brief_run(body: BriefRunBody):
        try:
            brief = morning_brief.run(body.workspace, kind="manual", notify=body.notify)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        return brief

    @app.post("/approvals/decide")
    def approvals_decide(body: ApprovalDecision):
        try:
            conn = workspaces.connect(body.workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            return approvals.decide(conn, body.id, body.decision, workspace_id=body.workspace)
        except approvals.ApprovalError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        finally:
            conn.close()

    # ── S6b: Connectors (Live Map + register/sync over the proven S6a floor) ──

    @app.get("/connectors")
    def connectors_list(workspace: str = "blueleaf"):
        try:
            conn = workspaces.connect(workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            return {
                "connectors": connector_sync.live_map(conn, workspace),
                "known": list(connector_sync.KNOWN_SERVERS),
            }
        finally:
            conn.close()

    @app.post("/connectors/register")
    def connectors_register(body: ConnectorRegister):
        # request-shape gate first: an unknown connector is 422 regardless of workspace
        if body.connector_id not in connector_sync.KNOWN_SERVERS:
            return JSONResponse({"detail": f"unknown connector {body.connector_id!r}"},
                                status_code=422)
        try:
            conn = workspaces.connect(body.workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            ref = connectors.register(conn, body.workspace, body.connector_id)
            return {"registered": body.connector_id, "keychain_ref": ref}
        finally:
            conn.close()

    @app.post("/connectors/sync")
    def connectors_sync(body: ConnectorSync):
        command = connector_sync.KNOWN_SERVERS.get(body.connector_id)
        if command is None:  # need the spawn command, so an unknown id is 422 here too
            return JSONResponse({"detail": f"unknown connector {body.connector_id!r}"},
                                status_code=422)
        try:
            conn = workspaces.connect(body.workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            # sync register-if-needs and spawns the real MCP server; returns the report
            return connector_sync.sync(conn, body.workspace, body.connector_id, command)
        finally:
            conn.close()

    # ── S6c: Foundry — Architect onboarding (5Q -> 3-6 validated agents) ─────

    @app.post("/foundry/onboard")
    def foundry_onboard(body: FoundryOnboardBody):
        try:
            conn = workspaces.connect(body.workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            t0 = time.perf_counter()
            try:
                specs = foundry.instantiate(conn, body.workspace, body.answers)
            except foundry.FoundryError as exc:
                # FoundryError is the fail-closed message the UI renders as
                # "error-with-next-step". agent_specs is untouched (§A4 all-or-nothing).
                return JSONResponse({"detail": str(exc)}, status_code=422)
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            scan = foundry.corpus_scan(conn, body.workspace)
            playbook = specs[0]["playbook"] if specs else None
            return {
                "playbook": playbook,
                "specs": specs,
                "scan": scan,
                "elapsed_ms": elapsed_ms,
            }
        finally:
            conn.close()

    @app.get("/foundry/status")
    def foundry_status_endpoint(workspace: str = "blueleaf"):
        try:
            conn = workspaces.connect(workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            # foundry.foundry_status carries INTERVIEW so the webapp hardcodes
            # zero question text — single source of truth (§A4).
            return foundry.foundry_status(conn, workspace)
        finally:
            conn.close()

    # ── S4a: Reveal-in-Finder (breadcrumb resolution) ────────────────────────

    @app.post("/reveal")
    def reveal(body: RevealBody):
        path = Path(body.path).expanduser()
        if not path.is_absolute():
            return JSONResponse({"detail": "absolute path required"}, status_code=422)
        if not _reveal_allowed(path):
            return JSONResponse({"detail": "path outside the allowed corpus roots"}, status_code=422)
        if not path.exists():
            return JSONResponse({"detail": "path does not exist"}, status_code=404)
        subprocess.run(["open", "-R", str(path)], check=False, timeout=10)
        return {"revealed": str(path)}

    return app
