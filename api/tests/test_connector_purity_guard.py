"""Regression guard for the 2026-07-25 leak class (F9): a synthetic/demo
connector must never sync into a protected (real, document-backed) workspace.

The incident: ``POST /connectors/sync`` defaults ``workspace`` to the production
workspace, so a demo-prep sync with no explicit workspace wrote 11 synthetic
rows into it. Nothing automated caught it. This test IS the tripwire — it
exercises ``_assert_sync_allowed`` directly (no MCP spawn, CI-safe)."""

import pytest

from heydey import connector_sync
from heydey.connectors import ConnectorError


def test_demo_connector_refused_into_protected_workspace():
    for cid in connector_sync.KNOWN_SERVERS:
        with pytest.raises(ConnectorError):
            connector_sync._assert_sync_allowed("blueleaf", cid)


def test_demo_connector_allowed_into_ephemeral_gate_workspaces():
    # The exact workspaces the S6b/S6c/S7 gates sync demo connectors into —
    # the guard must never block these, or it breaks the gates.
    for ws in ("demo-d2c", "s7-clinic", "s6c-client-d2c", "s6c-client-agency"):
        for cid in connector_sync.KNOWN_SERVERS:
            connector_sync._assert_sync_allowed(ws, cid)  # must not raise


def test_real_connector_allowed_into_protected_workspace():
    # A real (non-synthetic) connector into the production workspace is legitimate.
    connector_sync._assert_sync_allowed("blueleaf", "google-workspace")  # must not raise


def test_protected_set_is_env_overridable(monkeypatch):
    monkeypatch.setenv("HEYDEY_PROTECTED_WORKSPACES", "acme,globex")
    with pytest.raises(ConnectorError):
        connector_sync._assert_sync_allowed("acme", "demo-shopify")
    # blueleaf is no longer protected once the set is overridden
    connector_sync._assert_sync_allowed("blueleaf", "demo-shopify")  # must not raise
