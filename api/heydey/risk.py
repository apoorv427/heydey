"""4-tier action-risk taxonomy — read / write_local / exec / external (W2).

Pattern study: OpenWorker's typed risk engine (taxonomy only — no runtime code
adopted, L6). The tier is decided at MANIFEST-PARSE time, carried through the
approvals tray, and stamped on every receipt row, so risk class is audit trail,
not UI copy.

Two rules are load-bearing:

- **A declaration can never downgrade.** A first-party manifest may declare a
  tool's tier, but the resolved tier is the RISKIER of (declared, inferred from
  verb shape). A tool named ``send_report`` declared ``read`` resolves
  ``external`` — a mislabeled manifest fails closed, it does not open a hole.
- **Unknown shape → external.** A name with no declaration and no recognizable
  verb resolves to the top tier (the same fail-closed default as
  ``mcp_host.classify_tool``'s outbound).

Tier semantics:
  read        — pulls data; auto-runs.
  write_local — writes only to this machine (prepared-action artifacts, drafts).
  exec        — runs code/commands locally.
  external    — writes/sends/spends outside this machine.
Everything except ``read`` requires an approved tray row.
"""

from __future__ import annotations

import re

READ = "read"
WRITE_LOCAL = "write_local"
EXEC = "exec"
EXTERNAL = "external"

# Ascending risk. Ordering is what makes riskier-of resolution possible.
RISK_TIERS = (READ, WRITE_LOCAL, EXEC, EXTERNAL)
_RANK = {tier: rank for rank, tier in enumerate(RISK_TIERS)}

# Verb shapes, mirroring mcp_host's classifier: spend/write verbs mean the call
# reaches the connector's remote service, so they infer EXTERNAL, not write_local
# — write_local is claimable only by explicit tier (prepared actions, manifests).
_EXTERNAL_SHAPE = re.compile(
    r"\b(pay|charge|refund|purchase|spend|transfer|create|write|send|update|delete"
    r"|post|publish|suppress|cancel|set|add|remove|upload)\b|_pay\b", re.I)
_EXEC_SHAPE = re.compile(r"\b(run|execute|exec|shell|eval|spawn|compile|script)\b", re.I)
_READ_SHAPE = re.compile(r"\b(list|get|read|search|fetch|pull|query|describe|count|show)\b", re.I)


class RiskError(ValueError):
    """Unknown tier value — a manifest or caller bug, never silently coerced."""


def validate_tier(tier: str) -> str:
    if tier not in _RANK:
        raise RiskError(f"unknown risk tier {tier!r} — must be one of {RISK_TIERS}")
    return tier


def riskier(a: str, b: str) -> str:
    return a if _RANK[validate_tier(a)] >= _RANK[validate_tier(b)] else b


def infer_tier(name: str, description: str = "") -> str | None:
    """Verb-shape inference; None when the name carries no signal (the caller
    decides the fail-closed default so declared tiers stay usable)."""
    text = re.sub(r"[_\-./]", " ", f"{name} {description}")
    candidates = []
    if _EXTERNAL_SHAPE.search(text):
        candidates.append(EXTERNAL)
    if _EXEC_SHAPE.search(text):
        candidates.append(EXEC)
    if _READ_SHAPE.search(text):
        candidates.append(READ)
    if not candidates:
        return None
    return max(candidates, key=_RANK.__getitem__)


def resolve_tier(declared: str | None, name: str, description: str = "") -> str:
    """The one resolution rule: riskier of (declared, inferred); no signal at
    all → external. Invalid declarations raise instead of coercing."""
    inferred = infer_tier(name, description)
    if declared is None:
        return inferred if inferred is not None else EXTERNAL
    validate_tier(declared)
    if inferred is None:
        return declared
    return riskier(declared, inferred)


def requires_approval(tier: str) -> bool:
    """Only ``read`` auto-runs. Everything else needs an approved tray row."""
    return validate_tier(tier) != READ
