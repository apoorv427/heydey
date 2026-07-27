"""OAuth 2.0 + PKCE plumbing for real connectors (W2 · Contract C Layer 4).

Hand-rolled over stdlib urllib (L6 — the PROTOCOL is the rail, no SDK). Three
moves, all stateless helpers so the CLI or server owns the session:

  begin_auth()    -> consent URL + PKCE verifier + state (nothing stored yet)
  exchange_code() -> token bundle, stored via secrets_store under the
                     per-workspace Keychain name ``heydey.{workspace}.{connector}``
  get_valid_access_token() -> bundle from Keychain; auto-refresh when stale

Security posture, structural:

- PKCE S256 (RFC 7636): the code_verifier never leaves this process except in
  the token POST body; the consent URL carries only its SHA-256 challenge.
- No secret ever enters a URL query string — client_secret (Google issues one
  even for PKCE desktop clients and documents it as not-confidential) rides in
  the POST body only; ``build_consent_url`` has no secret parameter at all.
- Token values never appear in receipts, exceptions, or logs. The bundle lives
  in the Keychain under the exact per-workspace item name — the same isolation
  rail ``connectors.keychain_ref`` already proves (no wildcard, no shared item).
- Every exchange/refresh writes an ``action`` receipt row (risk ``external`` —
  the handshake is a deliberate network egress event): connecting an account
  leaves a receipt like everything else.

Rate limits: ``_post_form`` retries 429/5xx with exponential backoff + jitter,
honors ``Retry-After``, and fails after 4 attempts — the transport is
injectable so tests never sleep or touch the network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import secrets as pysecrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import connectors, secrets_store
from .risk import EXTERNAL

# Refresh this many seconds before nominal expiry — clock skew + request time.
EXPIRY_SKEW_S = 60
_MAX_ATTEMPTS = 4
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 30.0
_RETRYABLE = {429, 500, 502, 503, 504}


class OAuthError(RuntimeError):
    """Exchange/refresh failed. Message carries the endpoint + status, NEVER a
    token or code value."""


# ── PKCE (RFC 7636, S256 only) ────────────────────────────────────────────────

def make_verifier() -> str:
    """43–128 chars of [A-Za-z0-9-._~]; token_urlsafe(64) → 86 chars."""
    return pysecrets.token_urlsafe(64)


def challenge_of(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def make_state() -> str:
    return pysecrets.token_urlsafe(16)


def verify_state(expected: str, got: str) -> None:
    """CSRF check on the callback — mismatch fails closed."""
    if not expected or not pysecrets.compare_digest(expected, got or ""):
        raise OAuthError("state mismatch on OAuth callback — dropping the code")


# ── consent URL ───────────────────────────────────────────────────────────────

def build_consent_url(auth_endpoint: str, *, client_id: str, redirect_uri: str,
                      scopes: list[str], state: str, code_challenge: str,
                      extra_params: dict[str, str] | None = None) -> str:
    """The user-facing consent URL. Query carries the PKCE CHALLENGE only —
    there is deliberately no client_secret parameter on this function."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    params.update(extra_params or {})
    return f"{auth_endpoint}?{urllib.parse.urlencode(params)}"


def begin_auth(oauth_cfg: dict, *, client_id: str, redirect_uri: str) -> dict:
    """One-call start: mints verifier + state, returns everything the caller's
    session must hold until the callback. Nothing is stored here."""
    verifier = make_verifier()
    state = make_state()
    url = build_consent_url(
        oauth_cfg["auth_endpoint"],
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=list(oauth_cfg.get("scopes", [])),
        state=state,
        code_challenge=challenge_of(verifier),
        extra_params=oauth_cfg.get("extra_authorize_params"),
    )
    return {"consent_url": url, "code_verifier": verifier, "state": state}


# ── transport with backoff (injectable — tests never sleep or dial out) ──────

def _default_transport(url: str, body: bytes, headers: dict) -> tuple[int, dict, str]:
    """POST; returns (status, response_headers, text). HTTPError is data here —
    the retry decision belongs to _post_form, not the transport."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read().decode("utf-8", "replace")


def _post_form(url: str, form: dict, *, transport=None, sleep=time.sleep) -> dict:
    """POST x-www-form-urlencoded with 429/5xx backoff. Returns parsed JSON."""
    transport = transport or _default_transport
    body = urllib.parse.urlencode(form).encode("ascii")
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Accept": "application/json"}
    last_status = None
    for attempt in range(_MAX_ATTEMPTS):
        status, resp_headers, text = transport(url, body, headers)
        if status < 400:
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise OAuthError(f"non-JSON token response from {url} (status {status})") from exc
        last_status = status
        if status not in _RETRYABLE or attempt == _MAX_ATTEMPTS - 1:
            break
        retry_after = resp_headers.get("Retry-After") or resp_headers.get("retry-after")
        if retry_after and str(retry_after).isdigit():
            delay = min(float(retry_after), _BACKOFF_CAP_S)
        else:
            delay = min(_BACKOFF_BASE_S * (2 ** attempt), _BACKOFF_CAP_S)
            delay += random.uniform(0, delay / 4)  # jitter
        sleep(delay)
    # 4xx body is the provider's error json (e.g. invalid_grant) — safe + useful,
    # but cap it so a hostile endpoint can't stuff a screenful into our logs.
    raise OAuthError(f"token endpoint {url} failed (status {last_status}): {text[:300]}")


# ── token bundle storage (Keychain, per-workspace item — never the db) ───────

def _bundle_ref(workspace_id: str, connector_id: str) -> str:
    return connectors.keychain_ref(workspace_id, connector_id)


def _store_bundle(workspace_id: str, connector_id: str, bundle: dict) -> str:
    ref = _bundle_ref(workspace_id, connector_id)
    secrets_store.set_secret(ref, json.dumps(bundle))
    return ref


def load_bundle(workspace_id: str, connector_id: str) -> dict | None:
    raw = secrets_store.get_secret(_bundle_ref(workspace_id, connector_id))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None  # not ours / corrupt — treat as absent, never raise a token into a trace


def _bundle_from_response(payload: dict, *, prior: dict | None = None,
                          scopes_requested: list[str] | None = None) -> dict:
    if "access_token" not in payload:
        raise OAuthError("token response carried no access_token")
    now = time.time()
    return {
        "access_token": payload["access_token"],
        # providers (Google included) omit refresh_token on refresh — keep the old one
        "refresh_token": payload.get("refresh_token")
                         or (prior or {}).get("refresh_token", ""),
        "token_type": payload.get("token_type", "Bearer"),
        "scope": payload.get("scope",
                             " ".join(scopes_requested or (prior or {}).get("scope", "").split())),
        "expires_at": now + float(payload.get("expires_in", 3600)),
        "obtained_at": now,
    }


def _write_action_receipt(conn: sqlite3.Connection | None, connector_id: str,
                          event: str, scope: str) -> None:
    """The handshake's receipt row — scopes yes, token values NEVER."""
    if conn is None:
        return
    conn.execute(
        "INSERT INTO receipts(run_id, sentence_index, claim_text, doc_id, chunk_id,"
        " retrieval_score, confidence_band, validator_pass, model, tokens, cost_usd,"
        " risk_tier, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"oauth-{connector_id}", 0,
         f"oauth {event} · {connector_id} · scopes: {scope or '(none reported)'}",
         "", "", None, "action", None, "none", None, 0.0, EXTERNAL,
         datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()


# ── the three flows ───────────────────────────────────────────────────────────

def exchange_code(workspace_id: str, connector_id: str, oauth_cfg: dict, *,
                  client_id: str, code: str, code_verifier: str, redirect_uri: str,
                  client_secret: str = "", conn: sqlite3.Connection | None = None,
                  transport=None, sleep=time.sleep) -> dict:
    """Authorization code -> token bundle in the Keychain. Returns the bundle
    with the refresh_token masked (callers surface status, never the secret)."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    }
    if client_secret:
        form["client_secret"] = client_secret
    payload = _post_form(oauth_cfg["token_endpoint"], form, transport=transport, sleep=sleep)
    bundle = _bundle_from_response(payload, scopes_requested=oauth_cfg.get("scopes"))
    _store_bundle(workspace_id, connector_id, bundle)
    _write_action_receipt(conn, connector_id, "token exchanged", bundle["scope"])
    return {**bundle, "access_token": "•••", "refresh_token": "•••"}


def refresh(workspace_id: str, connector_id: str, oauth_cfg: dict, *,
            client_id: str, client_secret: str = "",
            conn: sqlite3.Connection | None = None,
            transport=None, sleep=time.sleep) -> dict:
    """Refresh the stored bundle in place. Raises OAuthError when there is no
    bundle or no refresh_token — re-consent is the only honest way out."""
    prior = load_bundle(workspace_id, connector_id)
    if not prior or not prior.get("refresh_token"):
        raise OAuthError(
            f"no refresh token stored for {connector_id!r} in workspace "
            f"{workspace_id!r} — run the consent flow again")
    form = {
        "grant_type": "refresh_token",
        "refresh_token": prior["refresh_token"],
        "client_id": client_id,
    }
    if client_secret:
        form["client_secret"] = client_secret
    payload = _post_form(oauth_cfg["token_endpoint"], form, transport=transport, sleep=sleep)
    bundle = _bundle_from_response(payload, prior=prior)
    _store_bundle(workspace_id, connector_id, bundle)
    _write_action_receipt(conn, connector_id, "token refreshed", bundle["scope"])
    return {**bundle, "access_token": "•••", "refresh_token": "•••"}


def get_valid_access_token(workspace_id: str, connector_id: str, oauth_cfg: dict, *,
                           client_id: str, client_secret: str = "",
                           conn: sqlite3.Connection | None = None,
                           transport=None, sleep=time.sleep,
                           now=time.time) -> str:
    """The one call a live connector makes per request: stored token if fresh,
    auto-refresh when inside the expiry skew. Returns the RAW access token —
    for the Authorization header only, never for storage or logs."""
    bundle = load_bundle(workspace_id, connector_id)
    if bundle is None:
        raise OAuthError(
            f"no token stored for {connector_id!r} in workspace {workspace_id!r}"
            f" — run the consent flow first")
    if now() < float(bundle.get("expires_at", 0)) - EXPIRY_SKEW_S:
        return bundle["access_token"]
    refresh(workspace_id, connector_id, oauth_cfg, client_id=client_id,
            client_secret=client_secret, conn=conn, transport=transport, sleep=sleep)
    return load_bundle(workspace_id, connector_id)["access_token"]
