"""Approvals tray + the prepared-action pattern (S4b · L12/L34 · v1.1 §14-A5).

Lifecycle: pending -> approved | denied (single transition, actor + timestamp).
Approving a prepared-action does exactly three local things:
  1. writes the action ARTIFACT (a CSV the founder can hand to anyone),
  2. formats the exact admin DEEP-LINK (URL string only — a formatter, not a client),
  3. writes a RECEIPT row.
**No connector write fires. Structurally**: this module imports no HTTP client, no
socket — there is nothing here that *can* reach a network. That is the L34 claim,
and `test_prepared_action_no_write` traps the interpreter's network entry points
to prove it.

Demo seed: 3 high-RTO SKUs (clearly labeled demo data; real rows arrive with the
Shopify connector at S6 — same payload shape, same executor).
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config, risk

VALID_DECISIONS = {"approved", "denied"}


class ApprovalError(ValueError):
    """Unknown approval id, double-decide, or malformed payload."""


def generate_shopify_admin_url(store_handle: str, product_id: str | int) -> str:
    """The documented Shopify admin product URL — a pure formatter (L34)."""
    return f"https://admin.shopify.com/store/{store_handle}/products/{product_id}"


def create_approval(conn: sqlite3.Connection, *, action_class: str, payload: dict,
                    run_id: str = "", risk_tier: str = "") -> int:
    """Every prepared action carries a 4-tier risk label (W2). Callers that
    don't say default to ``external`` — the top tier, fail closed."""
    tier = risk.validate_tier(risk_tier) if risk_tier else risk.EXTERNAL
    cursor = conn.execute(
        "INSERT INTO approvals(run_id, action_class, risk_tier, payload, status, requested_at)"
        " VALUES (?,?,?,?,?,?)",
        (run_id, action_class, tier, json.dumps(payload), "pending",
         datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()
    return cursor.lastrowid


def pending(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, run_id, action_class, risk_tier, payload, status, requested_at"
        " FROM approvals WHERE status = 'pending' ORDER BY id DESC"
    ).fetchall()
    return [
        {"id": r[0], "run_id": r[1], "action_class": r[2], "risk_tier": r[3],
         "payload": json.loads(r[4]), "status": r[5], "requested_at": r[6]}
        for r in rows
    ]


def seed_demo_sku_approval(conn: sqlite3.Connection) -> int:
    """The A9 storyboard card: 'Suppress 3 high-RTO SKUs?' — labeled demo data."""
    payload = {
        "kind": "sku_suppression",
        "title": "Suppress 3 high-RTO SKUs?",
        "demo": True,
        "store_handle": "demo-store",
        "base_return_rate": 3.1,
        "skus": [
            {"sku": "HD-TEE-BLK-M", "product_id": 8843291001, "title": "Core Tee · Black M",
             "return_rate": 12.4, "units_30d": 412},
            {"sku": "HD-JGR-NVY-L", "product_id": 8843291044, "title": "Jogger · Navy L",
             "return_rate": 11.1, "units_30d": 268},
            {"sku": "HD-HOD-GRY-S", "product_id": 8843291078, "title": "Hoodie · Grey S",
             "return_rate": 9.8, "units_30d": 190},
        ],
    }
    # The prepared-action pattern IS write_local: artifact + deep-link on this
    # machine, no connector write fired — the canonical example of the tier.
    return create_approval(conn, action_class="outbound", payload=payload,
                           risk_tier=risk.WRITE_LOCAL)


def _execute_sku_suppression(workspace_id: str, payload: dict) -> dict:
    """The prepared action: artifact CSV + deep-links. Local filesystem only."""
    artifacts_dir = config.workspaces_root() / workspace_id / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    artifact_path = artifacts_dir / f"sku-suppression-{stamp}.csv"

    store = payload.get("store_handle", "unknown-store")
    skus = payload.get("skus", [])
    with artifact_path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["sku", "title", "return_rate_pct", "base_rate_pct",
                         "units_30d", "admin_deep_link"])
        for sku in skus:
            writer.writerow([
                sku["sku"], sku.get("title", ""), sku.get("return_rate", ""),
                payload.get("base_return_rate", ""), sku.get("units_30d", ""),
                generate_shopify_admin_url(store, sku["product_id"]),
            ])

    primary = generate_shopify_admin_url(store, skus[0]["product_id"]) if skus else ""
    return {"artifact": str(artifact_path), "deep_link": primary,
            "deep_links": [generate_shopify_admin_url(store, s["product_id"]) for s in skus],
            "note": "no write was fired — prepared action only"}


def _execute_connector_call(workspace_id: str, payload: dict) -> dict:
    """Approving a connector action records the consent — the MCP host performs
    the actual call, re-checking this approval id (Contract C Layer 2). Nothing
    is executed here; the tray row IS the artifact."""
    return {"note": "approval recorded — the MCP host performs the call under this id",
            "tool": payload.get("tool", ""), "connector_id": payload.get("connector_id", "")}


_EXECUTORS = {"sku_suppression": _execute_sku_suppression,
              "connector_call": _execute_connector_call}


def decide(conn: sqlite3.Connection, approval_id: int, decision: str, *,
           workspace_id: str, actor: str = "ceo") -> dict:
    """Single-transition decide. Approve executes the prepared action + receipt."""
    if decision not in VALID_DECISIONS:
        raise ApprovalError(f"decision must be one of {sorted(VALID_DECISIONS)}")
    row = conn.execute(
        "SELECT status, payload, action_class, risk_tier FROM approvals WHERE id = ?",
        (approval_id,)
    ).fetchone()
    if row is None:
        raise ApprovalError(f"approval {approval_id} not found")
    if row[0] != "pending":
        raise ApprovalError(f"approval {approval_id} already {row[0]} — decisions are single-shot")

    payload = json.loads(row[1])
    tier = row[3] or risk.EXTERNAL  # pre-taxonomy rows decide at the top tier
    resolved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result: dict = {}

    if decision == "approved":
        executor = _EXECUTORS.get(payload.get("kind", ""))
        if executor is None:
            raise ApprovalError(f"no prepared-action executor for kind {payload.get('kind')!r}")
        result = executor(workspace_id, payload)
        artifact = result.get("artifact", "")  # connector_call approvals carry no artifact
        artifact_note = f" · artifact {Path(artifact).name}" if artifact else ""
        conn.execute(
            "INSERT INTO receipts(run_id, sentence_index, claim_text, doc_id, chunk_id,"
            " retrieval_score, confidence_band, validator_pass, model, tokens, cost_usd,"
            " risk_tier, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"approval-{approval_id}", 0,
             f"prepared-action {payload.get('kind')}: {payload.get('title', '')} · "
             f"approved by {actor}{artifact_note} · risk {tier} · no write fired",
             artifact, "", None, "action", None, "none", None, 0.0, tier, resolved_at),
        )

    conn.execute(
        "UPDATE approvals SET status = ?, resolved_at = ? WHERE id = ?",
        (decision, resolved_at, approval_id),
    )
    conn.commit()
    return {"id": approval_id, "status": decision, "resolved_at": resolved_at,
            "actor": actor, "risk_tier": tier, **result}
