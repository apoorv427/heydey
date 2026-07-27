"""Connector manifests — the parse step where risk tiers are DECIDED (W2).

A manifest is a checked-in JSON file describing one real connector class:
identity, auth (OAuth endpoints + scopes), and its tool list with declared
risk tiers. Parsing is where Contract C Layer 2 meets the 4-tier taxonomy:
every tool's tier is resolved here via ``risk.resolve_tier`` — riskier of
(declared, verb-shape inference) — so a mislabeled manifest fails closed
before any host ever spawns.

First-party manifests live in ``heydey/manifests/*.json``. Validation raises
``ManifestError`` on anything structurally wrong; there is no lenient mode.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import connectors, risk


class ManifestError(ValueError):
    pass


def manifests_dir() -> Path:
    return Path(__file__).parent / "manifests"


def list_manifest_ids() -> list[str]:
    return sorted(p.stem for p in manifests_dir().glob("*.json"))


def _validate(manifest: dict, source: str) -> dict:
    for key in ("connector_id", "title", "connector_class", "tools"):
        if not manifest.get(key):
            raise ManifestError(f"{source}: missing required field {key!r}")

    auth = manifest.get("auth") or {}
    if manifest["connector_class"] == "oauth":
        for key in ("auth_endpoint", "token_endpoint", "scopes"):
            if not auth.get(key):
                raise ManifestError(f"{source}: oauth manifest missing auth.{key}")
        for key in ("auth_endpoint", "token_endpoint"):
            if not str(auth[key]).startswith("https://"):
                raise ManifestError(f"{source}: auth.{key} must be https, got {auth[key]!r}")

    seen: set[str] = set()
    for tool in manifest["tools"]:
        name = tool.get("name", "")
        if not name:
            raise ManifestError(f"{source}: tool with no name")
        if name in seen:
            raise ManifestError(f"{source}: duplicate tool {name!r}")
        seen.add(name)
        try:
            tool["risk_tier"] = risk.resolve_tier(
                tool.get("risk_tier"), name, tool.get("description", ""))
        except risk.RiskError as exc:
            raise ManifestError(f"{source}: tool {name!r}: {exc}") from exc
    return manifest


def load_manifest(connector_id: str) -> dict:
    """Load + validate one first-party manifest. Tool tiers come back RESOLVED."""
    path = manifests_dir() / f"{connector_id}.json"
    if not path.is_file():
        raise ManifestError(f"no manifest for connector {connector_id!r} at {path}")
    try:
        manifest = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path.name}: invalid JSON — {exc}") from exc
    if manifest.get("connector_id") != connector_id:
        raise ManifestError(
            f"{path.name}: connector_id {manifest.get('connector_id')!r} != filename stem")
    return _validate(manifest, path.name)


def declared_tiers(manifest: dict) -> dict[str, str]:
    """name -> resolved tier, the shape MCPHost consumes at its manifest parse."""
    return {t["name"]: t["risk_tier"] for t in manifest.get("tools", [])}


def register_from_manifest(conn: sqlite3.Connection, workspace_id: str,
                           manifest: dict) -> str:
    """Register the connector for this workspace with the manifest's scopes.
    Returns the Keychain ref the token bundle will live under."""
    scopes = " ".join((manifest.get("auth") or {}).get("scopes", []))
    return connectors.register(
        conn, workspace_id, manifest["connector_id"], scopes=scopes)
