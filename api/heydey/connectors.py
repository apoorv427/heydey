"""Connector registry — Layers 3 + 4 of the MCP security boundary (Contract C).

Layer 4 (workspace isolation): a connector row lives INSIDE the workspace's own
db file, and its Keychain item is named ``heydey.{workspace_id}.{connector_id}``
— per-item, never a wildcard. Every accessor takes the workspace_id again and
validates it against the row, so a crossed wire fails instead of leaking.

Layer 3 (regulatory egress): profiles may declare ``egress: local_only``.
:func:`egress_allows` is the ONE gate — any answer path that consumed
connector-sourced content must pass it before a cloud model is called.
"""

from __future__ import annotations

import sqlite3

from . import secrets_store
from .llm_client import provider_of


class ConnectorError(ValueError):
    pass


class EgressError(RuntimeError):
    """Connector-sourced content was about to leave the machine under a
    local-only profile. Fail closed."""


def keychain_ref(workspace_id: str, connector_id: str) -> str:
    return f"heydey.{workspace_id}.{connector_id}"


def register(conn: sqlite3.Connection, workspace_id: str, connector_id: str, *,
             scopes: str = "", allowed_actions: str = "") -> str:
    """Register a connector for THIS workspace. Returns the keychain ref the
    credential must be stored under. No secret value ever touches this db."""
    if not workspace_id or not connector_id:
        raise ConnectorError("workspace_id and connector_id are required")
    ref = keychain_ref(workspace_id, connector_id)
    conn.execute(
        "INSERT INTO connectors(workspace_id, connector_id, keychain_ref, scopes, allowed_actions)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(workspace_id, connector_id) DO UPDATE SET"
        " scopes=excluded.scopes, allowed_actions=excluded.allowed_actions",
        (workspace_id, connector_id, ref, scopes, allowed_actions),
    )
    conn.commit()
    return ref


def list_connectors(conn: sqlite3.Connection, workspace_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT connector_id, keychain_ref, scopes, allowed_actions FROM connectors"
        " WHERE workspace_id = ?", (workspace_id,),
    ).fetchall()
    return [{"connector_id": r[0], "keychain_ref": r[1], "scopes": r[2],
             "allowed_actions": r[3]} for r in rows]


def get(conn: sqlite3.Connection, workspace_id: str, connector_id: str) -> dict | None:
    """Layer 4 accessor: the row must exist AND belong to the asking workspace."""
    row = conn.execute(
        "SELECT workspace_id, connector_id, keychain_ref, scopes, allowed_actions"
        " FROM connectors WHERE workspace_id = ? AND connector_id = ?",
        (workspace_id, connector_id),
    ).fetchone()
    if row is None:
        return None
    if row[0] != workspace_id:  # defense in depth — the WHERE already guarantees it
        return None
    return {"connector_id": row[1], "keychain_ref": row[2], "scopes": row[3],
            "allowed_actions": row[4]}


def store_credential(workspace_id: str, connector_id: str, value: str) -> str:
    """Store the connector credential under its per-workspace Keychain name."""
    ref = keychain_ref(workspace_id, connector_id)
    secrets_store.set_secret(ref, value)
    return ref


def get_credential(workspace_id: str, connector_id: str) -> str | None:
    return secrets_store.get_secret(keychain_ref(workspace_id, connector_id))


# ── Layer 3: the egress gate ──────────────────────────────────────────────────

def egress_allows(profile_egress: str, model: str, *, uses_connector_content: bool) -> None:
    """Raise EgressError if connector-sourced content is about to reach a cloud
    lane under a local-only profile. Call before EVERY model call whose context
    includes connector content (the DPDP clinic/hospital posture, v1.1 §14-C3)."""
    if not uses_connector_content:
        return
    if profile_egress == "local_only" and provider_of(model) != "ollama":
        raise EgressError(
            f"profile egress=local_only forbids sending connector-sourced content to "
            f"cloud model {model!r} — use a local lane or change the profile deliberately")
