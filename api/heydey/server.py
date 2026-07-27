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

W2 surface: /connectors/oauth/{config,status,start,callback,disconnect} — the
"log into my own account" leg over connector_oauth's PKCE helpers.

All mutations (POST/PUT) are bearer-gated by the middleware; the webapp calls
them server-side so the token never reaches a browser.

NO failure reaches a user as a bare text 500: every route returns
``{detail, next_step}`` JSON, and an unexpected exception is converted by the
outermost middleware (2026-07-27 — the founder hit "supervisor answered HTTP
500 with a non-JSON body" on Sync now).
"""

import dataclasses
import html
import json
import logging
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import __version__, approvals, artifacts, ask, config, connector_manifest, connector_oauth
from . import connector_sync, connectors, episodic, foundry
from . import graph, models_config, models_state, morning_brief, ops_ingest, pipeline
from . import secrets_store, sentinel, workspaces
from .auth import request_is_authorized
from .llm_client import family_of
from .schema import SCHEMA_VERSION

log = logging.getLogger(__name__)

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

# How long a started OAuth consent flow stays resumable (pending_oauth, below).
OAUTH_PENDING_TTL_S = 600

# What to DO when a route dies unexpectedly, by path prefix — FIRST match wins,
# so the more specific prefix is listed first. "Something broke" is not a state
# a surface can render: every error, including the ones we did not foresee,
# hands back a next step.
_NEXT_STEP_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("/connectors/oauth", "re-check the OAuth client config, then start the consent flow again"),
    ("/connectors", "retry the sync; if it repeats, check ~/.heydey/logs/supervisor.log"),
    ("/ask", "retry in evidence mode (retrieval only); a model lane may be offline"),
    ("/find", "retry the search; if it repeats, re-run ingest for this workspace"),
    ("/graph", "reload the panel; if it repeats, run the graph backfill for this workspace"),
    ("/artifacts", "reload the panel; check that the configured artifact roots still exist"),
    ("/foundry", "re-run onboarding; the answers are not saved until every spec validates"),
    ("/models", "reload the models panel; a profile file may be malformed"),
    ("/reveal", "open the file from Finder yourself — the breadcrumb is in the receipt"),
)
_DEFAULT_NEXT_STEP = ("retry; if it repeats, check ~/.heydey/logs/supervisor.log "
                      "and restart the supervisor")


def _next_step_for(path: str) -> str:
    for prefix, step in _NEXT_STEP_BY_PREFIX:  # most specific prefix listed first
        if path.startswith(prefix):
            return step
    return _DEFAULT_NEXT_STEP


def _safe_detail(exc: Exception) -> str:
    """A one-line, user-showable description of a failure — with any secret this
    process has served masked out, because an exception message is a response
    body here and key material must never ride one."""
    text = f"{type(exc).__name__}: {exc}"[:300]
    for value in secrets_store._served_values:
        if value and value in text:
            text = text.replace(value, secrets_store.REDACTED)
    return text


def _error(status: int, detail: str, next_step: str, **extra) -> JSONResponse:
    """The ONE error shape. ``detail`` says what happened, ``next_step`` says
    what the user can do about it — a surface renders both, never a blank panel."""
    return JSONResponse({"detail": detail, "next_step": next_step, **extra},
                        status_code=status)


def _missing_workspace(workspace: str) -> JSONResponse | None:
    """404 JSON when the workspace id is invalid or has no db file, else None.
    Cheaper than connect() (no migration) for read/status routes."""
    try:
        if workspaces.db_path(workspace).is_file():
            return None
        detail = f"workspace {workspace!r} does not exist"
    except workspaces.WorkspaceError as exc:
        detail = str(exc)
    return _error(404, detail, 'create it first: POST /workspaces {"id": "..."}')


# ── OAuth: client credentials + the pending-consent map ──────────────────────

def _oauth_secret_ref(connector_id: str, field: str) -> str:
    """Keychain item name for an OAuth CLIENT credential. Per connector, not per
    workspace — a provider console issues one client per install. Distinct from
    ``connectors.keychain_ref`` (per-workspace), which holds the TOKEN bundle."""
    return f"heydey.oauth.{connector_id}.{field}"


def _oauth_client(connector_id: str) -> tuple[str | None, str]:
    return (secrets_store.get_secret(_oauth_secret_ref(connector_id, "client_id")),
            secrets_store.get_secret(_oauth_secret_ref(connector_id, "client_secret")) or "")


def _oauth_manifest(connector_id: str) -> dict:
    """The connector's manifest, or ManifestError — including for a connector
    that exists but is not an OAuth class (fail closed, never guess endpoints)."""
    manifest = connector_manifest.load_manifest(connector_id)
    if manifest.get("connector_class") != "oauth":
        raise connector_manifest.ManifestError(
            f"connector {connector_id!r} is not an OAuth connector "
            f"(class {manifest.get('connector_class')!r})")
    return manifest


def _unknown_oauth_connector(exc: Exception) -> JSONResponse:
    oauth_ids = []
    for cid in connector_manifest.list_manifest_ids():
        try:
            _oauth_manifest(cid)
        except connector_manifest.ManifestError:
            continue
        oauth_ids.append(cid)
    return _error(422, str(exc), f"use one of the OAuth connectors: {oauth_ids or '(none installed)'}")


def redirect_uri() -> str:
    """The one URI the provider console must whitelist. Loopback + the
    supervisor's own port — the callback below is the route that answers it."""
    return f"http://127.0.0.1:{config.port()}/connectors/oauth/callback"


def _bundle_view(workspace: str, connector_id: str) -> dict | None:
    """The stored token bundle with every token value stripped. Returns None
    when nothing usable is stored (a cleared bundle reads as absent)."""
    bundle = connector_oauth.load_bundle(workspace, connector_id)
    if not bundle or not bundle.get("access_token"):
        return None
    return {
        "scopes": (bundle.get("scope") or "").split(),
        "expires_at": float(bundle.get("expires_at") or 0) or None,
        "obtained_at": float(bundle.get("obtained_at") or 0) or None,
        "has_refresh_token": bool(bundle.get("refresh_token")),
    }


def _oauth_status_payload(connector_id: str, workspace: str, manifest: dict) -> dict:
    """The FOUR states this connector can be in, named — the UI renders one of
    them and never a blank panel:

      unconfigured — no client credentials yet (empty-with-CTA)
      disconnected — configured, no account linked (CTA: Connect)
      connected    — a usable token bundle is stored (loaded)
      expired      — the token is past expiry with no refresh token
                     (error-with-next-step: reconnect)
    """
    client_id, _ = _oauth_client(connector_id)
    stored = _bundle_view(workspace, connector_id)
    requested = list((manifest.get("auth") or {}).get("scopes", []))
    expired = bool(stored and stored["expires_at"] is not None
                   and stored["expires_at"] <= time.time()
                   and not stored["has_refresh_token"])
    if not client_id:
        state, next_step = "unconfigured", (
            f"add this connector's OAuth client id (POST /connectors/oauth/config) and "
            f"whitelist the redirect URI {redirect_uri()} in the provider console")
    elif stored is None:
        state, next_step = "disconnected", (
            "start the consent flow: POST /connectors/oauth/start, then open consent_url")
    elif expired:
        state, next_step = "expired", (
            "the stored token expired and the provider issued no refresh token — "
            "connect again")
    else:
        state, next_step = "connected", "nothing — this account is linked; sync when ready"
    return {
        "connector_id": connector_id,
        "workspace": workspace,
        "title": manifest.get("title", connector_id),
        "state": state,
        "configured": bool(client_id),
        "connected": state == "connected",
        "scopes": stored["scopes"] if stored else [],
        "scopes_requested": requested,
        "expires_at": stored["expires_at"] if stored else None,
        "has_refresh_token": bool(stored and stored["has_refresh_token"]),
        "redirect_uri": redirect_uri(),
        "next_step": next_step,
    }


def _oauth_page(status: int, heading: str, body: str) -> HTMLResponse:
    """The provider redirects a BROWSER here, so this leg answers HTML, not JSON.
    Self-contained (no asset can load from a page served on a random port) and
    every interpolated value is escaped."""
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(heading)} · Heydey</title>"
        "<style>body{font:16px/1.55 -apple-system,BlinkMacSystemFont,sans-serif;"
        "margin:0;min-height:100vh;display:grid;place-items:center;background:#0f1115;"
        "color:#e7e9ee}main{max-width:34rem;padding:2rem}h1{font-size:1.35rem;margin:0 0 .6rem}"
        "p{margin:0;color:#a7adbb}</style>"
        f"<main><h1>{html.escape(heading)}</h1><p>{html.escape(body)}</p></main>",
        status_code=status)


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


class OAuthConfigBody(BaseModel):
    connector_id: str
    client_id: str
    client_secret: str = ""  # stored via secrets_store, NEVER echoed, never in the db


class OAuthStartBody(BaseModel):
    connector_id: str
    workspace: str = "blueleaf"


class OAuthDisconnectBody(BaseModel):
    connector_id: str
    workspace: str = "blueleaf"


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

    # Pending OAuth consent flows, keyed by the state we minted: {state -> {
    # code_verifier, connector_id, workspace, started_at}}. Deliberately
    # IN-MEMORY and per-app — the PKCE verifier is single-use secret material
    # with a ~1-minute useful life, so writing it to disk would buy nothing and
    # widen the blast radius. Tradeoff: a supervisor restart mid-consent loses
    # the flow, and the callback then says exactly that ("click Connect again").
    pending_oauth: dict[str, dict] = {}

    def _prune_pending(now: float) -> None:
        for key, flow in list(pending_oauth.items()):
            if now - flow["started_at"] > OAUTH_PENDING_TTL_S:
                pending_oauth.pop(key, None)

    @app.middleware("http")
    async def require_token_on_mutations(request: Request, call_next):
        if not request_is_authorized(request.method, request.headers, app.state.token):
            return JSONResponse(
                {"detail": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    # Registered LAST -> runs OUTERMOST (Starlette builds the user middleware
    # stack so the most recently added wraps the rest), which is what makes this
    # the last line of defense: anything the auth middleware or any route raises
    # comes back as JSON with a next step instead of Starlette's bare text 500.
    @app.middleware("http")
    async def json_errors(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 — the point is that NOTHING escapes
            log.exception("unhandled error on %s %s", request.method, request.url.path)
            return _error(500, _safe_detail(exc), _next_step_for(request.url.path),
                          where=f"{request.method} {request.url.path}")

    # Belt to the middleware's suspenders: if the middleware stack itself fails,
    # Starlette's ServerErrorMiddleware still answers JSON, never text/plain.
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        log.exception("unhandled error (outer) on %s %s", request.method, request.url.path)
        return _error(500, _safe_detail(exc), _next_step_for(request.url.path),
                      where=f"{request.method} {request.url.path}")

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

    @app.get("/graph/profile")
    def graph_profile(key: str, workspace: str = "blueleaf"):
        """Entity profile — 'everything about X': aliases, the docs that
        mention it, typed relations, receipts. The graph rebuild's primary
        product surface (the force-directed canvas is the secondary lens)."""
        try:
            conn = workspaces.connect(workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            profile = graph.entity_profile(conn, key, workspace_id=workspace)
            if profile is None:
                return JSONResponse(
                    {"detail": f"no entity matching {key!r}",
                     "next_step": "run the graph backfill, or try the canonical label"},
                    status_code=404)
            return profile
        finally:
            conn.close()

    @app.get("/graph/neighbors")
    def graph_neighbors(id: int, workspace: str = "blueleaf", hops: int = 2, limit: int = 50):
        try:
            conn = workspaces.connect(workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            t0 = time.perf_counter()
            payload = graph.neighbors(conn, id, hops=min(hops, 3), limit=min(limit, 100))
            if isinstance(payload, dict):
                payload["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return payload
        finally:
            conn.close()

    # ── Artifacts: "what did my AI just make?" (provenance, not a file browser) ─

    @app.get("/artifacts")
    def artifacts_list(workspace: str = "blueleaf", limit: int = 50, include_os: bool = False):
        """Heydey-produced artifacts with their provenance (run, approval,
        risk tier, receipt). ``include_os=true`` additionally lists recent
        files from configured folders — query-time only, no watcher — and
        those carry provenance null because we did NOT make them."""
        try:
            conn = workspaces.connect(workspace)
        except workspaces.WorkspaceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        try:
            payload = {
                "summary": artifacts.artifact_summary(conn, workspace),
                "artifacts": artifacts.recent_artifacts(conn, workspace, limit=min(limit, 200)),
            }
            if include_os:
                payload["os_files"] = artifacts.recent_os_files(limit=min(limit, 100))
            return payload
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
            return _error(422, f"unknown connector {body.connector_id!r}",
                          f"pick one of {sorted(connector_sync.KNOWN_SERVERS)}")

        # CEO decision (2026-07-27): a SYNTHETIC connector aimed at a protected
        # corpus is REROUTED into the demo workspace instead of failing — the
        # founder gets data to look at, the real corpus stays pure. The purity
        # guard is untouched and still absolute: we change the TARGET, never the
        # rule, and connector_sync.sync re-asserts it on the target it is handed.
        target, rerouted = body.workspace, False
        blocked = connector_sync.sync_blocked_reason(body.workspace, body.connector_id)
        if blocked:
            target, rerouted = connector_sync.DEMO_WORKSPACE, True
            still_blocked = connector_sync.sync_blocked_reason(target, body.connector_id)
            if still_blocked:  # e.g. the demo workspace was itself marked protected
                return _error(409, still_blocked,
                              f"drop {target!r} from HEYDEY_PROTECTED_WORKSPACES, or sync "
                              f"this demo connector into a scratch workspace of your own",
                              requested_workspace=body.workspace, routed_to=None)
            try:
                workspaces.create_workspace(target)
            except workspaces.WorkspaceExists:
                pass
            except workspaces.WorkspaceError as exc:
                return _error(500, str(exc),
                              f"create the demo workspace by hand: "
                              f'POST /workspaces {{"id": "{target}"}}')
        try:
            conn = workspaces.connect(target)
        except workspaces.WorkspaceError as exc:
            return _error(404, str(exc), 'create it first: POST /workspaces {"id": "..."}')
        try:
            # sync register-if-needs and spawns the real MCP server; returns the report
            report = connector_sync.sync(conn, target, body.connector_id, command)
        except connectors.ConnectorError as exc:
            # The purity guard (or a registry refusal) — a rule, not a crash.
            return _error(422, str(exc),
                          "sync this connector into a demo or client workspace instead",
                          requested_workspace=body.workspace)
        except Exception as exc:  # a connector subprocess dying is THEIR fault, not a 500
            log.exception("connector %s failed to sync into %s", body.connector_id, target)
            return _error(502, _safe_detail(exc),
                          "the connector's MCP server did not complete — retry, and check "
                          "~/.heydey/logs/supervisor.log if it repeats",
                          connector_id=body.connector_id, workspace=target)
        finally:
            conn.close()
        if rerouted:
            # Extra keys ONLY on a reroute: the happy-path report shape is a
            # contract the S6b tests pin exactly.
            report = {**report, "routed_to": target, "requested_workspace": body.workspace,
                      "note": (f"synthetic demo data kept out of your real corpus — synced "
                               f"into the {target!r} workspace instead of "
                               f"{body.workspace!r}"),
                      "next_step": f"switch to the {target!r} workspace to see these rows"}
        return report

    # ── W2: OAuth connect — "log into my own account" (PKCE, Contract C L4) ───

    @app.post("/connectors/oauth/config")
    def oauth_config(body: OAuthConfigBody):
        """Store the provider-console client credentials. They go to the
        Keychain via secrets_store — never the db, never a response body."""
        try:
            _oauth_manifest(body.connector_id)
        except connector_manifest.ManifestError as exc:
            return _unknown_oauth_connector(exc)
        if not body.client_id.strip():
            return _error(422, "client_id is required",
                          "copy the OAuth client id from the provider console "
                          f"(redirect URI: {redirect_uri()})")
        secrets_store.set_secret(_oauth_secret_ref(body.connector_id, "client_id"),
                                 body.client_id.strip())
        if body.client_secret.strip():
            secrets_store.set_secret(_oauth_secret_ref(body.connector_id, "client_secret"),
                                     body.client_secret.strip())
        return {
            "connector_id": body.connector_id,
            "configured": True,
            "client_secret_stored": bool(body.client_secret.strip()),  # the VALUE never leaves
            "redirect_uri": redirect_uri(),
            "next_step": "click Connect to open the provider's consent screen",
        }

    @app.get("/connectors/oauth/status")
    def oauth_status(connector_id: str, workspace: str = "blueleaf"):
        try:
            manifest = _oauth_manifest(connector_id)
        except connector_manifest.ManifestError as exc:
            return _unknown_oauth_connector(exc)
        missing = _missing_workspace(workspace)
        if missing is not None:
            return missing
        return _oauth_status_payload(connector_id, workspace, manifest)

    @app.post("/connectors/oauth/start")
    def oauth_start(body: OAuthStartBody):
        """Mint a PKCE verifier + state and hand back the consent URL. The
        verifier stays here (pending_oauth); only its SHA-256 challenge travels."""
        try:
            manifest = _oauth_manifest(body.connector_id)
        except connector_manifest.ManifestError as exc:
            return _unknown_oauth_connector(exc)
        missing = _missing_workspace(body.workspace)
        if missing is not None:
            return missing
        client_id, _ = _oauth_client(body.connector_id)
        if not client_id:
            return _error(422, f"no OAuth client configured for {body.connector_id!r}",
                          "POST /connectors/oauth/config with the client id from the "
                          "provider console first",
                          redirect_uri=redirect_uri(), state_name="unconfigured")
        flow = connector_oauth.begin_auth(manifest["auth"], client_id=client_id,
                                          redirect_uri=redirect_uri())
        now = time.time()
        _prune_pending(now)
        pending_oauth[flow["state"]] = {
            "code_verifier": flow["code_verifier"],  # never leaves this process
            "state": flow["state"],
            "connector_id": body.connector_id,
            "workspace": body.workspace,
            "started_at": now,
        }
        return {
            "consent_url": flow["consent_url"],
            "state": flow["state"],
            "redirect_uri": redirect_uri(),
            "expires_in": OAUTH_PENDING_TTL_S,
            "scopes_requested": list(manifest["auth"].get("scopes", [])),
            "next_step": "open consent_url, approve the scopes, then close the tab it lands on",
        }

    @app.get("/connectors/oauth/callback")
    def oauth_callback(code: str = "", state: str = "", error: str = ""):
        """The provider's redirect target. Unauthenticated by necessity (a
        browser arrives here with no bearer token), so its gate is the state:
        server-minted, single-use (popped before the exchange, so a replayed
        code finds nothing), TTL-bounded, and on a 127.0.0.1-only listener."""
        now = time.time()
        _prune_pending(now)
        flow = pending_oauth.pop(state, None)
        if flow is None:
            return _oauth_page(400, "That consent link has expired",
                               "This callback did not match a consent flow this supervisor "
                               "started (it may have timed out, already been used, or the "
                               "supervisor restarted). Go back to Connectors and click "
                               "Connect again.")
        if error:
            return _oauth_page(400, "Consent was not granted",
                               f"The provider returned '{error}'. Nothing was stored. Go back "
                               f"to Connectors and click Connect again when you're ready.")
        if not code:
            return _oauth_page(400, "No authorization code arrived",
                               "The provider redirected here without a code, so there is "
                               "nothing to exchange. Start the connect flow again.")
        try:
            # constant-time compare against the value we minted (the pop already
            # matched by key; this is the explicit, timing-safe assertion)
            connector_oauth.verify_state(flow["state"], state)
            manifest = _oauth_manifest(flow["connector_id"])
            client_id, client_secret = _oauth_client(flow["connector_id"])
            if not client_id:
                return _oauth_page(400, "This connector is no longer configured",
                                   "Its OAuth client id was removed while consent was open. "
                                   "Add it again in Connectors, then retry.")
            conn = workspaces.connect(flow["workspace"])
            try:
                connector_oauth.exchange_code(
                    flow["workspace"], flow["connector_id"], manifest["auth"],
                    client_id=client_id, client_secret=client_secret, code=code,
                    code_verifier=flow["code_verifier"], redirect_uri=redirect_uri(),
                    conn=conn)
                # Linked accounts belong on the Live Map — register only AFTER the
                # exchange succeeded, so nothing claims a connection we don't have.
                connector_manifest.register_from_manifest(conn, flow["workspace"], manifest)
            finally:
                conn.close()
        except (connector_oauth.OAuthError, connector_manifest.ManifestError,
                connectors.ConnectorError, workspaces.WorkspaceError) as exc:
            log.warning("oauth callback failed for %s: %s", flow["connector_id"], exc)
            return _oauth_page(400, "Could not finish connecting", f"{_safe_detail(exc)} "
                               "Go back to Connectors and click Connect again.")
        except Exception as exc:  # noqa: BLE001 — a browser must never see a stack trace
            log.exception("oauth callback crashed for %s", flow["connector_id"])
            return _oauth_page(500, "Could not finish connecting",
                               f"{_safe_detail(exc)} Check ~/.heydey/logs/supervisor.log, "
                               "then try again from Connectors.")
        return _oauth_page(200, f"Connected · {manifest.get('title', flow['connector_id'])}",
                           f"This account is linked to the {flow['workspace']} workspace. "
                           f"You can close this tab and go back to Heydey.")

    @app.post("/connectors/oauth/disconnect")
    def oauth_disconnect(body: OAuthDisconnectBody):
        """Destroy the stored token bundle for this workspace + connector.

        secrets_store exposes no delete, so we overwrite the Keychain item with
        an empty value — the token material is gone either way — and then VERIFY
        it reads as absent rather than claiming a disconnect we didn't achieve."""
        try:
            _oauth_manifest(body.connector_id)
        except connector_manifest.ManifestError as exc:
            return _unknown_oauth_connector(exc)
        missing = _missing_workspace(body.workspace)
        if missing is not None:
            return missing
        was_connected = _bundle_view(body.workspace, body.connector_id) is not None
        if was_connected:
            secrets_store.set_secret(
                connectors.keychain_ref(body.workspace, body.connector_id), "")
            if _bundle_view(body.workspace, body.connector_id) is not None:
                ref = connectors.keychain_ref(body.workspace, body.connector_id)
                return _error(500, f"the token bundle for {ref!r} still reads back after "
                                   f"clearing it",
                              f"remove it by hand: security delete-generic-password "
                              f"-s heydey -a {ref}")
        return {
            "connector_id": body.connector_id,
            "workspace": body.workspace,
            "disconnected": True,
            "was_connected": was_connected,
            "next_step": "connect again from Connectors whenever you're ready",
        }

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
