"""W2 — OAuth 2.0 + PKCE plumbing and the google-workspace manifest.

All endpoint traffic is a fake injected transport — no network, no sleeping,
no real Keychain (secrets_store is monkeypatched to a dict, the same pattern
as test_mcp_host)."""

import json
import urllib.parse

import pytest

from heydey import connector_manifest, connector_oauth, connectors, workspaces
from heydey.connector_manifest import ManifestError
from heydey.connector_oauth import OAuthError

OAUTH_CFG = {
    "auth_endpoint": "https://auth.example/authorize",
    "token_endpoint": "https://auth.example/token",
    "scopes": ["scope.read", "scope.other"],
    "extra_authorize_params": {"access_type": "offline"},
}


@pytest.fixture()
def memory_secrets(monkeypatch):
    stored: dict[str, str] = {}
    monkeypatch.setattr(connector_oauth.secrets_store, "set_secret",
                        lambda name, value: stored.__setitem__(name, value))
    monkeypatch.setattr(connector_oauth.secrets_store, "get_secret",
                        lambda name, **k: stored.get(name))
    return stored


def transport_returning(*responses):
    """Fake transport: pops scripted (status, headers, payload) tuples and
    records every request it saw."""
    queue = list(responses)
    calls = []

    def transport(url, body, headers):
        calls.append({"url": url, "form": dict(urllib.parse.parse_qsl(body.decode()))})
        status, resp_headers, payload = queue.pop(0)
        text = json.dumps(payload) if isinstance(payload, dict) else payload
        return status, resp_headers, text

    transport.calls = calls
    return transport


def token_response(**overrides):
    payload = {"access_token": "at-1", "refresh_token": "rt-1",
               "token_type": "Bearer", "expires_in": 3600, "scope": "scope.read"}
    payload.update(overrides)
    return 200, {}, payload


# ── PKCE (RFC 7636) ───────────────────────────────────────────────────────────

def test_pkce_rfc7636_appendix_b_vector():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert connector_oauth.challenge_of(verifier) == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_generated_verifier_is_valid():
    verifier = connector_oauth.make_verifier()
    assert 43 <= len(verifier) <= 128
    assert "=" not in connector_oauth.challenge_of(verifier)  # unpadded base64url


def test_state_verify_fails_closed():
    with pytest.raises(OAuthError):
        connector_oauth.verify_state("expected", "tampered")
    connector_oauth.verify_state("same", "same")  # no raise


# ── consent URL ───────────────────────────────────────────────────────────────

def test_consent_url_carries_challenge_never_a_secret():
    flow = connector_oauth.begin_auth(OAUTH_CFG, client_id="cid-123",
                                      redirect_uri="http://127.0.0.1:8765/cb")
    parsed = urllib.parse.urlparse(flow["consent_url"])
    q = dict(urllib.parse.parse_qsl(parsed.query))
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == OAUTH_CFG["auth_endpoint"]
    assert q["response_type"] == "code"
    assert q["client_id"] == "cid-123"
    assert q["scope"] == "scope.read scope.other"
    assert q["code_challenge_method"] == "S256"
    assert q["code_challenge"] == connector_oauth.challenge_of(flow["code_verifier"])
    assert q["state"] == flow["state"]
    assert q["access_type"] == "offline"  # manifest extra param folded in
    assert "secret" not in flow["consent_url"].lower()
    assert flow["code_verifier"] not in flow["consent_url"]  # only its hash travels


# ── exchange + storage under the per-workspace Keychain name ─────────────────

def test_exchange_stores_bundle_under_workspace_item(memory_secrets):
    transport = transport_returning(token_response())
    result = connector_oauth.exchange_code(
        "acme", "google-workspace", OAUTH_CFG, client_id="cid", code="auth-code",
        code_verifier="v" * 43, redirect_uri="http://127.0.0.1/cb",
        transport=transport)

    form = transport.calls[0]["form"]
    assert form["grant_type"] == "authorization_code"
    assert form["code_verifier"] == "v" * 43
    assert "client_secret" not in form  # pure PKCE public client

    assert list(memory_secrets) == ["heydey.acme.google-workspace"]
    bundle = json.loads(memory_secrets["heydey.acme.google-workspace"])
    assert bundle["access_token"] == "at-1" and bundle["refresh_token"] == "rt-1"
    assert bundle["expires_at"] > bundle["obtained_at"]
    # the return value masks token material — callers surface status only
    assert result["access_token"] == "•••" and result["refresh_token"] == "•••"


def test_exchange_writes_action_receipt_without_token_values(memory_secrets, heydey_home):
    workspaces.create_workspace("acme")
    conn = workspaces.connect("acme")
    try:
        connector_oauth.exchange_code(
            "acme", "google-workspace", OAUTH_CFG, client_id="cid", code="c",
            code_verifier="v" * 43, redirect_uri="http://127.0.0.1/cb",
            conn=conn, transport=transport_returning(token_response()))
        claim, band, tier = conn.execute(
            "SELECT claim_text, confidence_band, risk_tier FROM receipts"
            " WHERE run_id = 'oauth-google-workspace'").fetchone()
        assert band == "action" and tier == "external"
        assert "token exchanged" in claim and "scope.read" in claim
        assert "at-1" not in claim and "rt-1" not in claim  # never a token value
    finally:
        conn.close()


# ── refresh ───────────────────────────────────────────────────────────────────

def test_refresh_preserves_refresh_token_when_response_omits_it(memory_secrets):
    connector_oauth.exchange_code(
        "acme", "gw", OAUTH_CFG, client_id="cid", code="c", code_verifier="v" * 43,
        redirect_uri="http://x/cb", transport=transport_returning(token_response()))

    # Google-style refresh: new access token, NO refresh_token in the response
    refresh_payload = {"access_token": "at-2", "token_type": "Bearer", "expires_in": 3600}
    transport = transport_returning((200, {}, refresh_payload))
    connector_oauth.refresh("acme", "gw", OAUTH_CFG, client_id="cid",
                            transport=transport)

    assert transport.calls[0]["form"]["grant_type"] == "refresh_token"
    bundle = json.loads(memory_secrets["heydey.acme.gw"])
    assert bundle["access_token"] == "at-2"
    assert bundle["refresh_token"] == "rt-1"  # preserved


def test_refresh_without_stored_bundle_demands_reconsent(memory_secrets):
    with pytest.raises(OAuthError, match="consent"):
        connector_oauth.refresh("acme", "gw", OAUTH_CFG, client_id="cid",
                                transport=transport_returning(token_response()))


def test_get_valid_token_fresh_vs_stale(memory_secrets):
    connector_oauth.exchange_code(
        "acme", "gw", OAUTH_CFG, client_id="cid", code="c", code_verifier="v" * 43,
        redirect_uri="http://x/cb", transport=transport_returning(token_response()))
    bundle = json.loads(memory_secrets["heydey.acme.gw"])

    # fresh: no transport call at all
    token = connector_oauth.get_valid_access_token(
        "acme", "gw", OAUTH_CFG, client_id="cid",
        transport=transport_returning(), now=lambda: bundle["obtained_at"])
    assert token == "at-1"

    # stale: refresh fires, new token comes back
    transport = transport_returning(token_response(access_token="at-3"))
    token = connector_oauth.get_valid_access_token(
        "acme", "gw", OAUTH_CFG, client_id="cid", transport=transport,
        now=lambda: bundle["expires_at"] + 1)
    assert token == "at-3" and len(transport.calls) == 1


# ── rate-limit backoff ────────────────────────────────────────────────────────

def test_backoff_honors_retry_after_then_succeeds(memory_secrets):
    sleeps = []
    transport = transport_returning(
        (429, {"Retry-After": "7"}, {"error": "rate_limited"}),
        token_response())
    connector_oauth.exchange_code(
        "acme", "gw", OAUTH_CFG, client_id="cid", code="c", code_verifier="v" * 43,
        redirect_uri="http://x/cb", transport=transport, sleep=sleeps.append)
    assert sleeps == [7.0]
    assert len(transport.calls) == 2


def test_backoff_5xx_exponential_then_gives_up(memory_secrets):
    sleeps = []
    transport = transport_returning(*[(503, {}, {"error": "unavailable"})] * 4)
    with pytest.raises(OAuthError, match="503"):
        connector_oauth.exchange_code(
            "acme", "gw", OAUTH_CFG, client_id="cid", code="c", code_verifier="v" * 43,
            redirect_uri="http://x/cb", transport=transport, sleep=sleeps.append)
    assert len(transport.calls) == 4  # capped attempts
    assert len(sleeps) == 3  # no sleep after the last failure
    assert sleeps[0] < sleeps[1] < sleeps[2]  # exponential (jitter preserves order)
    assert memory_secrets == {}  # nothing stored on failure


def test_4xx_fails_immediately_no_retry(memory_secrets):
    transport = transport_returning((400, {}, {"error": "invalid_grant"}))
    with pytest.raises(OAuthError, match="invalid_grant"):
        connector_oauth.exchange_code(
            "acme", "gw", OAUTH_CFG, client_id="cid", code="bad", code_verifier="v" * 43,
            redirect_uri="http://x/cb", transport=transport, sleep=lambda s: None)
    assert len(transport.calls) == 1


# ── the google-workspace manifest (first REAL connector class) ───────────────

def test_google_workspace_manifest_loads_read_only():
    manifest = connector_manifest.load_manifest("google-workspace")
    assert manifest["connector_class"] == "oauth"
    assert manifest["server_command"] is None  # live leg is honestly not here yet
    tiers = connector_manifest.declared_tiers(manifest)
    assert tiers and set(tiers.values()) == {"read"}  # read-only v1, resolved
    auth = manifest["auth"]
    assert auth["auth_endpoint"].startswith("https://")
    assert auth["token_endpoint"].startswith("https://")
    assert all(s.endswith(".readonly") for s in auth["scopes"])


def test_manifest_listing_includes_google_workspace():
    assert "google-workspace" in connector_manifest.list_manifest_ids()


def test_register_from_manifest_scopes_the_connector(heydey_home):
    workspaces.create_workspace("acme")
    conn = workspaces.connect("acme")
    try:
        manifest = connector_manifest.load_manifest("google-workspace")
        ref = connector_manifest.register_from_manifest(conn, "acme", manifest)
        assert ref == "heydey.acme.google-workspace"
        row = connectors.get(conn, "acme", "google-workspace")
        assert "drive.readonly" in row["scopes"]
    finally:
        conn.close()


def test_unknown_manifest_raises():
    with pytest.raises(ManifestError):
        connector_manifest.load_manifest("no-such-connector")


def test_manifest_declared_tier_cannot_downgrade(tmp_path, monkeypatch):
    bad = {
        "connector_id": "sneaky", "title": "Sneaky", "connector_class": "local",
        "tools": [{"name": "send_everything", "description": "", "risk_tier": "read"}],
    }
    (tmp_path / "sneaky.json").write_text(json.dumps(bad))
    monkeypatch.setattr(connector_manifest, "manifests_dir", lambda: tmp_path)
    manifest = connector_manifest.load_manifest("sneaky")
    assert connector_manifest.declared_tiers(manifest) == {"send_everything": "external"}
