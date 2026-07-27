"""S0 gate: key material never appears in any endpoint response, log, or the db."""

import io
import logging

import pytest
from fastapi.routing import APIRoute

from conftest import auth_headers
from heydey import config, secrets_store, workspaces

CANARY = "sk-heydey-canary-9f8e7d6c5b4a3210"


@pytest.fixture()
def canary_secret(heydey_home):
    """Serve a fake API key from the 0600 env file (keychain untouched in tests)."""
    path = config.secrets_env_path()
    path.write_text(f"# heydey secrets\nOPENROUTER_API_KEY={CANARY}\n")
    path.chmod(0o600)
    value = secrets_store.get_secret("OPENROUTER_API_KEY", use_keychain=False)
    assert value == CANARY
    return value


def test_no_secret_in_response(client, canary_secret, monkeypatch):
    """Sweep every route (all outcomes: 200/201/409/422/401/404) — zero hits."""
    from heydey import ask, server

    # hermetic: no embedder download, no keychain read/write (env-file lane only —
    # /reveal in this sweep 422s before its subprocess call, so no Finder stub needed)
    monkeypatch.setattr(ask, "embed_texts", lambda texts: [[0.0] * 384 for _ in texts])
    monkeypatch.setattr(secrets_store, "set_secret", lambda name, value: None)
    env_only = lambda name, **_: secrets_store._from_env_file(name)  # noqa: E731
    monkeypatch.setattr(server.secrets_store, "get_secret", env_only)
    typed_key = "sk-or-typed-canary-0123456789abcdef"

    responses = [
        client.get("/health"),
        client.get("/workspaces"),
        client.post("/workspaces", json={"id": "sec-ws"}, headers=auth_headers()),
        client.post("/workspaces", json={"id": "sec-ws"}, headers=auth_headers()),  # 409
        client.post("/workspaces", json={"id": "BAD ID"}, headers=auth_headers()),  # 422
        client.post("/workspaces", json={"id": "nope"}),  # 401
        client.get("/does-not-exist"),  # 404
        # S4a surface — swept with a live canary in the secrets store
        client.post("/ask", json={"question": "q", "workspace": "sec-ws"}, headers=auth_headers()),
        client.post("/ask", json={"question": "q", "workspace": "sec-ws", "mode": "full"},
                    headers=auth_headers()),
        client.post("/find", json={"query": "q", "workspace": "sec-ws"}, headers=auth_headers()),
        client.get("/models"),  # key PRESENCE must render as bool, never the value
        client.put("/models", json={"action": "set_key", "provider": "openrouter",
                                    "key": typed_key}, headers=auth_headers()),
        client.put("/models", json={"action": "activate", "profile": "local-only"},
                   headers=auth_headers()),
        client.get("/costs?workspace=sec-ws"),
        client.post("/reveal", json={"path": "/nowhere/x.md"}, headers=auth_headers()),  # 422
        client.get("/sessions?workspace=sec-ws&q=x"),
        client.get("/sessions/detail?run_id=r1&workspace=sec-ws"),  # 404 path
        client.post("/sessions/delete", json={"run_id": "r1", "workspace": "sec-ws"},
                    headers=auth_headers()),  # 404 path
        client.get("/graph?workspace=sec-ws"),
        client.get("/graph/entity?id=1&workspace=sec-ws"),  # 404 path
        # graph rebuild surface (G1): profile is the primary product view, neighbors
        # the 2-hop traversal — both must stay canary-free on hit AND miss paths
        client.get("/graph/profile?key=nope&workspace=sec-ws"),  # 404 path
        client.get("/graph/neighbors?id=1&workspace=sec-ws"),
        # artifacts surface: provenance rows join approvals+receipts, so a secret
        # that ever reached a payload would surface here first
        client.get("/artifacts?workspace=sec-ws"),
        client.get("/artifacts?workspace=sec-ws&include_os=true"),
        client.get("/today?workspace=sec-ws"),
        client.post("/brief/run", json={"workspace": "sec-ws", "notify": False},
                    headers=auth_headers()),
        client.post("/approvals/decide", json={"id": 999, "decision": "approved",
                                               "workspace": "sec-ws"},
                    headers=auth_headers()),  # 422 not-found path
        # S6b surface — register (cheap db insert) + live map read + unknown-sync 422
        # (the 422 path avoids spawning a real MCP subprocess in the secrets sweep)
        client.post("/connectors/register", json={"workspace": "sec-ws",
                                                  "connector_id": "demo-shopify"},
                    headers=auth_headers()),
        client.get("/connectors?workspace=sec-ws"),
        client.post("/connectors/sync", json={"workspace": "sec-ws",
                                              "connector_id": "nope"},
                    headers=auth_headers()),  # 422 unknown-connector path
        # S6c surface — status read + onboard 422 bad-answers (no MCP subprocess)
        # + /ask agent 404 path (proves fail-closed on unknown/unvalidated agent)
        client.get("/foundry/status?workspace=sec-ws"),
        client.post("/foundry/onboard",
                    json={"workspace": "sec-ws",
                          "answers": {"business_type": "hospital"}},
                    headers=auth_headers()),  # 422 bad-answers path
        client.post("/ask",
                    json={"question": "q", "workspace": "sec-ws", "agent": "nope"},
                    headers=auth_headers()),  # 404 unknown-agent path
        # W2 OAuth connect surface — the client_secret the user just typed is the
        # canary here: it must never come back on ANY of these five legs
        client.post("/connectors/oauth/config",
                    json={"connector_id": "google-workspace", "client_id": "cid-sweep",
                          "client_secret": typed_key},
                    headers=auth_headers()),
        client.get("/connectors/oauth/status?connector_id=google-workspace&workspace=sec-ws"),
        client.post("/connectors/oauth/start",
                    json={"connector_id": "google-workspace", "workspace": "sec-ws"},
                    headers=auth_headers()),  # 422 unconfigured path (no keychain in tests)
        client.get("/connectors/oauth/callback?code=abc&state=not-a-pending-flow"),  # 400 html
        client.post("/connectors/oauth/disconnect",
                    json={"connector_id": "google-workspace", "workspace": "sec-ws"},
                    headers=auth_headers()),
    ]
    for response in responses:
        assert CANARY not in response.text, response.request.url
        assert typed_key not in response.text, response.request.url  # a key the user just typed
        for header_value in response.headers.values():
            assert CANARY not in header_value

    # the sweep above must cover every registered route — adding a route without
    # extending this test fails here
    api_paths = {route.path for route in client.app.routes if isinstance(route, APIRoute)}
    assert api_paths == {"/health", "/workspaces", "/ask", "/find", "/models", "/costs",
                         "/reveal", "/today", "/brief/run", "/approvals/decide",
                         "/graph", "/graph/entity", "/graph/profile", "/graph/neighbors",
                         "/artifacts",
                         "/sessions", "/sessions/detail", "/sessions/delete",
                         "/connectors", "/connectors/register", "/connectors/sync",
                         "/connectors/oauth/config", "/connectors/oauth/status",
                         "/connectors/oauth/start", "/connectors/oauth/callback",
                         "/connectors/oauth/disconnect",
                         "/foundry/onboard", "/foundry/status"}

    # ...and the workspace db created while a secret was in memory contains no key material
    conn = workspaces.connect("sec-ws")
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        for row in conn.execute(f'SELECT * FROM "{table}"'):
            assert CANARY not in str(tuple(row)), table
    conn.close()


def test_log_redaction_masks_served_secrets(canary_secret):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(secrets_store.SecretRedactionFilter())
    logger = logging.getLogger("heydey.test_redaction")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("loaded key OPENROUTER_API_KEY=%s for router", CANARY)
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert CANARY not in output
    assert secrets_store.REDACTED in output


def test_group_readable_env_file_refused(heydey_home):
    path = config.secrets_env_path()
    path.write_text(f"LEAKY_KEY={CANARY}\n")
    path.chmod(0o644)
    assert secrets_store.get_secret("LEAKY_KEY", use_keychain=False) is None
