"""The founder's HTTP 500 is dead, and the OAuth connect leg exists (2026-07-27).

What the founder hit: clicking "Sync now" answered ``supervisor answered HTTP
500 with a non-JSON body``. Root cause was the purity guard doing its job —
``connector_sync._assert_sync_allowed`` refuses a SYNTHETIC demo connector aimed
at a protected real workspace — while the route let ``ConnectorError`` escape
into FastAPI's bare text 500. The guard is correct; its SURFACING was the bug.

Three things are proven here, in order of how badly the founder needed them:

  1. /connectors/sync never answers a bare 500. A blocked synthetic sync is
     REROUTED into the demo workspace (200 + ``routed_to``), and every other
     failure comes back as JSON carrying a ``next_step``. The purity invariant
     is unchanged: synthetic rows still never land in the protected workspace.
  2. The whole CLASS is fixed — an unexpected exception on ANY route returns
     JSON with a next step, with served secrets masked out of the message.
  3. The OAuth connect legs (config/status/start/callback/disconnect) each
     render one of four named states, and the client_secret is never echoed.

Hermetic: the Keychain is a dict, the token endpoint is a fake transport, and
the only subprocess is the bundled demo MCP server the S6b tests already spawn.
"""

import json
import time
import urllib.parse

import pytest

from heydey import (config, connector_oauth, connector_sync, connectors, graph,
                    secrets_store, server, vector_store, workspaces)

from conftest import auth_headers

DIM = 384
GW = "google-workspace"       # the one first-party OAuth manifest
CLIENT_SECRET = "gocspx-super-secret-value-9f8e7d6c"


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def keychain(monkeypatch):
    """Dict-backed Keychain: no `security` subprocess, no real secrets touched.
    Patching the MODULE means server.py and connector_oauth.py both see it."""
    store: dict[str, str] = {}
    monkeypatch.setattr(secrets_store, "set_secret",
                        lambda name, value: store.__setitem__(name, value))
    monkeypatch.setattr(secrets_store, "get_secret",
                        lambda name, **kw: store.get(name) or None)
    return store


@pytest.fixture()
def stub_embed(monkeypatch):
    vec = [0.0] * DIM
    vec[0] = 1.0
    monkeypatch.setattr(vector_store, "embed_texts", lambda texts: [vec for _ in texts])


@pytest.fixture()
def oauth_ws(heydey_home):
    workspaces.create_workspace("oauthws")
    return "oauthws"


def _store_bundle(keychain, workspace, connector_id, **overrides):
    bundle = {"access_token": "at-1", "refresh_token": "rt-1", "token_type": "Bearer",
              "scope": "https://www.googleapis.com/auth/drive.readonly",
              "expires_at": time.time() + 3600, "obtained_at": time.time()}
    bundle.update(overrides)
    keychain[connectors.keychain_ref(workspace, connector_id)] = json.dumps(bundle)
    return bundle


def _configure(client, keychain, *, client_secret: str = CLIENT_SECRET):
    return client.post("/connectors/oauth/config",
                       json={"connector_id": GW, "client_id": "cid-123",
                             "client_secret": client_secret},
                       headers=auth_headers())


def _assert_structured_error(resp, *, status: int | None = None):
    """Whatever went wrong, the browser gets JSON it can render: what happened
    AND what to do next. This is the assertion the founder's bug would fail."""
    if status is not None:
        assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/json"), resp.text
    body = resp.json()  # raises if the body is the bare text 500 we're killing
    assert body.get("detail"), body
    assert body.get("next_step"), body
    return body


# ── 1. the founder's 500: /connectors/sync ────────────────────────────────────

@pytest.fixture()
def rerouted(client, heydey_home, stub_embed, monkeypatch):
    """Sync a SYNTHETIC connector at a PROTECTED workspace — the exact click
    that 500'd. Spawns the real demo MCP server, like the S6b HTTP tests."""
    monkeypatch.setenv("HEYDEY_PROTECTED_WORKSPACES", "prot")
    workspaces.create_workspace("prot")
    resp = client.post("/connectors/sync",
                       json={"workspace": "prot", "connector_id": "demo-shopify"},
                       headers=auth_headers())
    return resp


def test_protected_sync_routes_to_demo_instead_of_500(rerouted):
    assert rerouted.status_code == 200, rerouted.text
    report = rerouted.json()
    assert report["routed_to"] == connector_sync.DEMO_WORKSPACE
    assert report["requested_workspace"] == "prot"
    assert "synthetic" in report["note"] and "real corpus" in report["note"]
    assert report["next_step"]
    assert report["chunks"] > 0 and report["connector_id"] == "demo-shopify"


def test_synthetic_rows_never_reach_the_protected_workspace(rerouted):
    assert rerouted.status_code == 200
    prot = workspaces.connect("prot")
    try:
        assert prot.execute(
            "SELECT COUNT(*) FROM points WHERE json_extract(payload,'$.source_type')"
            "='connector'").fetchone()[0] == 0
        assert connectors.list_connectors(prot, "prot") == []
    finally:
        prot.close()
    demo = workspaces.connect(connector_sync.DEMO_WORKSPACE)
    try:
        assert demo.execute(
            "SELECT COUNT(*) FROM points WHERE json_extract(payload,'$.source_type')"
            "='connector'").fetchone()[0] > 0
    finally:
        demo.close()


def test_happy_path_report_shape_is_unchanged(client, heydey_home, stub_embed):
    """No routing keys on a sync that went where it was asked to — the S6b
    contract pins this shape exactly."""
    workspaces.create_workspace("plainws")
    resp = client.post("/connectors/sync",
                       json={"workspace": "plainws", "connector_id": "demo-sheets"},
                       headers=auth_headers())
    assert resp.status_code == 200
    assert set(resp.json()) == {"connector_id", "tools_pulled", "chunks", "flagged",
                                "entities", "synced_at"}


def test_connector_error_from_sync_is_json_not_500(client, heydey_home, monkeypatch):
    """Belt for the exact escape path: whatever raises ConnectorError inside
    sync (a future guard included) surfaces as a structured 422."""
    workspaces.create_workspace("errws")

    def boom(conn, workspace_id, connector_id, command):
        raise connectors.ConnectorError("refusing to sync synthetic data here")

    monkeypatch.setattr(connector_sync, "sync", boom)
    resp = client.post("/connectors/sync",
                       json={"workspace": "errws", "connector_id": "demo-shopify"},
                       headers=auth_headers())
    body = _assert_structured_error(resp, status=422)
    assert "refusing to sync" in body["detail"]
    assert body["requested_workspace"] == "errws"


def test_connector_subprocess_crash_is_json_502(client, heydey_home, monkeypatch):
    workspaces.create_workspace("crashws")

    def boom(conn, workspace_id, connector_id, command):
        raise RuntimeError("mcp server exited with code 9")

    monkeypatch.setattr(connector_sync, "sync", boom)
    resp = client.post("/connectors/sync",
                       json={"workspace": "crashws", "connector_id": "demo-shopify"},
                       headers=auth_headers())
    body = _assert_structured_error(resp, status=502)
    assert "mcp server exited" in body["detail"]
    assert body["connector_id"] == "demo-shopify"


def test_unroutable_sync_is_structured_not_500(client, heydey_home, monkeypatch):
    """Demo workspace itself marked protected -> nowhere legal to land. Still a
    JSON answer with a next step, never a crash."""
    monkeypatch.setenv("HEYDEY_PROTECTED_WORKSPACES",
                       f"prot2,{connector_sync.DEMO_WORKSPACE}")
    workspaces.create_workspace("prot2")
    resp = client.post("/connectors/sync",
                       json={"workspace": "prot2", "connector_id": "demo-shopify"},
                       headers=auth_headers())
    body = _assert_structured_error(resp, status=409)
    assert body["routed_to"] is None
    assert "HEYDEY_PROTECTED_WORKSPACES" in body["next_step"]


def test_unknown_connector_422_carries_a_next_step(client, heydey_home):
    workspaces.create_workspace("uws")
    body = _assert_structured_error(
        client.post("/connectors/sync", json={"workspace": "uws", "connector_id": "nope"},
                    headers=auth_headers()),
        status=422)
    assert "demo-shopify" in body["next_step"]


def test_missing_workspace_sync_404_carries_a_next_step(client, heydey_home):
    body = _assert_structured_error(
        client.post("/connectors/sync",
                    json={"workspace": "no-such-ws", "connector_id": "demo-shopify"},
                    headers=auth_headers()),
        status=404)
    assert "/workspaces" in body["next_step"]


# ── 2. the CLASS fix: no route can answer a bare 500 ─────────────────────────

def test_unexpected_error_on_any_route_returns_json(client, heydey_home, monkeypatch):
    workspaces.create_workspace("gws")

    def boom(*_args, **_kwargs):  # signature-agnostic: graph.panel is not our file
        raise RuntimeError("graph exploded")

    monkeypatch.setattr(graph, "panel", boom)
    resp = client.get("/graph?workspace=gws")
    body = _assert_structured_error(resp, status=500)
    assert "graph exploded" in body["detail"]
    assert body["where"] == "GET /graph"
    assert "graph backfill" in body["next_step"]  # a next step for THIS surface


def test_unexpected_error_detail_masks_served_secrets(client, heydey_home, monkeypatch):
    """An exception message is a response body here — a key that leaked into one
    must come back redacted, not printed."""
    canary = "sk-heydey-error-canary-13579"
    path = config.secrets_env_path()
    path.write_text(f"SOME_KEY={canary}\n")
    path.chmod(0o600)
    assert secrets_store.get_secret("SOME_KEY", use_keychain=False) == canary  # now served
    workspaces.create_workspace("gws2")

    def boom(*_args, **_kwargs):
        raise RuntimeError(f"upstream rejected key {canary}")

    monkeypatch.setattr(graph, "panel", boom)
    resp = client.get("/graph?workspace=gws2")
    body = _assert_structured_error(resp, status=500)
    assert canary not in resp.text
    assert secrets_store.REDACTED in body["detail"]


# ── 3. OAuth connect: four states on every leg ───────────────────────────────

def test_config_requires_token(client, keychain, heydey_home):
    resp = client.post("/connectors/oauth/config",
                       json={"connector_id": GW, "client_id": "cid"})
    assert resp.status_code == 401


def test_config_unknown_connector_is_structured(client, keychain, heydey_home):
    body = _assert_structured_error(
        client.post("/connectors/oauth/config",
                    json={"connector_id": "not-a-connector", "client_id": "cid"},
                    headers=auth_headers()),
        status=422)
    assert GW in body["next_step"]


def test_config_requires_a_client_id(client, keychain, heydey_home):
    body = _assert_structured_error(
        client.post("/connectors/oauth/config",
                    json={"connector_id": GW, "client_id": "   "},
                    headers=auth_headers()),
        status=422)
    assert "redirect" in body["next_step"].lower()


def test_config_stores_credentials_and_never_echoes_the_secret(client, keychain, heydey_home):
    resp = _configure(client, keychain)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True and body["client_secret_stored"] is True
    assert body["redirect_uri"] == server.redirect_uri()
    assert body["redirect_uri"].endswith("/connectors/oauth/callback")
    assert CLIENT_SECRET not in resp.text          # the value never leaves the machine
    assert "cid-123" not in resp.text
    # ...but it IS stored, under a keychain name, never the db
    assert keychain["heydey.oauth.google-workspace.client_id"] == "cid-123"
    assert keychain["heydey.oauth.google-workspace.client_secret"] == CLIENT_SECRET


def test_status_four_states(client, keychain, oauth_ws):
    def status():
        resp = client.get(f"/connectors/oauth/status?connector_id={GW}&workspace={oauth_ws}")
        assert resp.status_code == 200, resp.text
        return resp.json()

    # (1) empty-with-CTA: nothing configured yet
    first = status()
    assert first["state"] == "unconfigured"
    assert first["configured"] is False and first["connected"] is False
    assert first["redirect_uri"] == server.redirect_uri()   # what to paste in the console
    assert first["scopes_requested"] and first["scopes"] == []
    assert "config" in first["next_step"]

    # (2) configured, no account linked yet
    _configure(client, keychain)
    second = status()
    assert second["state"] == "disconnected"
    assert second["configured"] is True and second["connected"] is False
    assert "/connectors/oauth/start" in second["next_step"]

    # (3) loaded: a usable bundle is stored
    _store_bundle(keychain, oauth_ws, GW)
    third = status()
    assert third["state"] == "connected" and third["connected"] is True
    assert third["scopes"] == ["https://www.googleapis.com/auth/drive.readonly"]
    assert third["expires_at"] > time.time()
    assert third["has_refresh_token"] is True
    assert "at-1" not in str(third) and "rt-1" not in str(third)  # no token material

    # (4) error-with-next-step: expired, and no refresh token to save it
    _store_bundle(keychain, oauth_ws, GW, expires_at=time.time() - 10, refresh_token="")
    fourth = status()
    assert fourth["state"] == "expired" and fourth["connected"] is False
    assert "connect again" in fourth["next_step"]


def test_status_unknown_connector_and_missing_workspace(client, keychain, oauth_ws):
    _assert_structured_error(
        client.get(f"/connectors/oauth/status?connector_id=nope&workspace={oauth_ws}"),
        status=422)
    body = _assert_structured_error(
        client.get(f"/connectors/oauth/status?connector_id={GW}&workspace=no-such-ws"),
        status=404)
    assert "/workspaces" in body["next_step"]


def test_start_without_config_says_how_to_configure(client, keychain, oauth_ws):
    body = _assert_structured_error(
        client.post("/connectors/oauth/start",
                    json={"connector_id": GW, "workspace": oauth_ws},
                    headers=auth_headers()),
        status=422)
    assert "/connectors/oauth/config" in body["next_step"]
    assert body["redirect_uri"] == server.redirect_uri()


def test_start_returns_a_consent_url_and_keeps_the_verifier_server_side(
        client, keychain, oauth_ws):
    _configure(client, keychain)
    resp = client.post("/connectors/oauth/start",
                       json={"connector_id": GW, "workspace": oauth_ws},
                       headers=auth_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(body["consent_url"]).query))
    assert query["client_id"] == "cid-123"
    assert query["redirect_uri"] == server.redirect_uri()
    assert query["code_challenge_method"] == "S256" and query["code_challenge"]
    assert query["state"] == body["state"]
    assert "code_verifier" not in resp.text          # only its SHA-256 travels
    assert CLIENT_SECRET not in resp.text            # and never the secret
    assert body["scopes_requested"] and body["expires_in"] > 0


def test_start_unknown_workspace_404(client, keychain, heydey_home):
    _configure(client, keychain)
    _assert_structured_error(
        client.post("/connectors/oauth/start",
                    json={"connector_id": GW, "workspace": "no-such-ws"},
                    headers=auth_headers()),
        status=404)


def _fake_token_endpoint(monkeypatch, payload=None, status=200):
    """Patch the module-level transport connector_oauth._post_form resolves at
    call time — no network, no sleeping."""
    seen = []
    body = payload or {"access_token": "at-live", "refresh_token": "rt-live",
                       "token_type": "Bearer", "expires_in": 3600,
                       "scope": "https://www.googleapis.com/auth/drive.readonly"}

    def transport(url, data, headers):
        seen.append({"url": url, "form": dict(urllib.parse.parse_qsl(data.decode()))})
        return status, {}, json.dumps(body)

    monkeypatch.setattr(connector_oauth, "_default_transport", transport)
    return seen


def _start_flow(client, keychain, workspace):
    _configure(client, keychain)
    resp = client.post("/connectors/oauth/start",
                       json={"connector_id": GW, "workspace": workspace},
                       headers=auth_headers())
    assert resp.status_code == 200, resp.text
    return resp.json()["state"]


def test_callback_completes_the_connection(client, keychain, oauth_ws, monkeypatch):
    seen = _fake_token_endpoint(monkeypatch)
    state = _start_flow(client, keychain, oauth_ws)

    resp = client.get(f"/connectors/oauth/callback?code=auth-code-1&state={state}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/html")
    assert "close this tab" in resp.text
    assert "auth-code-1" not in resp.text  # the code never renders back into the page

    # the exchange used PKCE + the stored client credentials
    form = seen[0]["form"]
    assert form["grant_type"] == "authorization_code" and form["code"] == "auth-code-1"
    assert form["client_id"] == "cid-123" and form["client_secret"] == CLIENT_SECRET
    assert len(form["code_verifier"]) >= 43

    # ...and the surfaces agree: status connected, Live Map row present
    status = client.get(
        f"/connectors/oauth/status?connector_id={GW}&workspace={oauth_ws}").json()
    assert status["state"] == "connected" and status["connected"] is True
    live = client.get(f"/connectors?workspace={oauth_ws}").json()
    assert GW in {row["connector_id"] for row in live["connectors"]}


def test_callback_state_is_single_use(client, keychain, oauth_ws, monkeypatch):
    _fake_token_endpoint(monkeypatch)
    state = _start_flow(client, keychain, oauth_ws)
    assert client.get(f"/connectors/oauth/callback?code=c1&state={state}").status_code == 200
    replay = client.get(f"/connectors/oauth/callback?code=c1&state={state}")
    assert replay.status_code == 400
    assert "Connect again" in replay.text


def test_callback_unknown_state_is_html_with_a_next_step(client, keychain, oauth_ws):
    resp = client.get("/connectors/oauth/callback?code=c&state=never-issued")
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("text/html")
    assert "Connect again" in resp.text
    assert "close this tab" not in resp.text  # never claims success


def test_callback_provider_denial_stores_nothing(client, keychain, oauth_ws, monkeypatch):
    seen = _fake_token_endpoint(monkeypatch)
    state = _start_flow(client, keychain, oauth_ws)
    resp = client.get(f"/connectors/oauth/callback?error=access_denied&state={state}")
    assert resp.status_code == 400 and "access_denied" in resp.text
    assert seen == []  # no exchange attempted
    assert connectors.keychain_ref(oauth_ws, GW) not in keychain
    status = client.get(
        f"/connectors/oauth/status?connector_id={GW}&workspace={oauth_ws}").json()
    assert status["state"] == "disconnected"


def test_callback_token_endpoint_failure_is_a_page_not_a_trace(
        client, keychain, oauth_ws, monkeypatch):
    _fake_token_endpoint(monkeypatch, payload={"error": "invalid_grant"}, status=400)
    state = _start_flow(client, keychain, oauth_ws)
    resp = client.get(f"/connectors/oauth/callback?code=stale&state={state}")
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("text/html")
    assert "invalid_grant" in resp.text and "Connect again" in resp.text
    assert "Traceback" not in resp.text


def test_disconnect_clears_the_bundle_and_is_idempotent(client, keychain, oauth_ws):
    _configure(client, keychain)
    _store_bundle(keychain, oauth_ws, GW)

    first = client.post("/connectors/oauth/disconnect",
                        json={"connector_id": GW, "workspace": oauth_ws},
                        headers=auth_headers())
    assert first.status_code == 200, first.text
    assert first.json()["disconnected"] is True and first.json()["was_connected"] is True
    assert "at-1" not in keychain.get(connectors.keychain_ref(oauth_ws, GW), "")

    status = client.get(
        f"/connectors/oauth/status?connector_id={GW}&workspace={oauth_ws}").json()
    assert status["state"] == "disconnected"      # client creds survive, the token doesn't

    second = client.post("/connectors/oauth/disconnect",
                         json={"connector_id": GW, "workspace": oauth_ws},
                         headers=auth_headers())
    assert second.status_code == 200
    assert second.json()["was_connected"] is False


def test_disconnect_rejects_unknown_connector_and_workspace(client, keychain, oauth_ws):
    _assert_structured_error(
        client.post("/connectors/oauth/disconnect",
                    json={"connector_id": "nope", "workspace": oauth_ws},
                    headers=auth_headers()),
        status=422)
    _assert_structured_error(
        client.post("/connectors/oauth/disconnect",
                    json={"connector_id": GW, "workspace": "no-such-ws"},
                    headers=auth_headers()),
        status=404)


def test_no_oauth_leg_can_answer_a_bare_500(client, keychain, oauth_ws):
    """Sweep the surface with hostile input: every answer is renderable JSON
    (detail + next_step) or the callback's HTML page. Never text/plain."""
    hostile = [
        client.post("/connectors/oauth/config", json={"connector_id": "../../etc/passwd",
                                                      "client_id": "x"},
                    headers=auth_headers()),
        client.get("/connectors/oauth/status?connector_id=&workspace="),
        client.get(f"/connectors/oauth/status?connector_id={GW}&workspace=../escape"),
        client.post("/connectors/oauth/start", json={"connector_id": GW, "workspace": "BAD ID"},
                    headers=auth_headers()),
        client.post("/connectors/oauth/disconnect", json={"connector_id": GW,
                                                          "workspace": "../escape"},
                    headers=auth_headers()),
    ]
    for resp in hostile:
        assert resp.status_code in (400, 404, 422), (resp.request.url, resp.status_code)
        assert resp.headers["content-type"].startswith("application/json"), resp.text
        body = resp.json()
        assert body.get("detail") and body.get("next_step"), (resp.request.url, body)

    page = client.get("/connectors/oauth/callback")  # no code, no state at all
    assert page.status_code == 400
    assert page.headers["content-type"].startswith("text/html")
    assert "Connect again" in page.text
