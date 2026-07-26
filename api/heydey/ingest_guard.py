"""Ingest Guard — ported from the proven BLOS module (S1, lock L7), extended for
MCP connector results (Contract C, layer 1 — primed here, enforced at S6).

Every chunk passes here BEFORE storage, on every lane. Posture: TAG-AND-REDACT,
not block — flips to quarantine-on-detect the day client/govt data enters.

  1. PII scrub — Indian-context phone / Aadhaar / email -> `[REDACTED-PII]` in the
     stored text. Originals go to a LOCAL-ONLY audit log under ~/.heydey/logs/
     (never the repo, never the KB, never off the machine).
  2. Injection tagging — pattern scan; sets `injection_risk` + `injection_patterns`.
     Surfaces render `[flagged-source]` when citing a flagged chunk.

MCP extension (S1): `guard_mcp_result` stores the RAW connector result in the
workspace db (mcp_results), tags it, and returns the ONLY text permitted into LLM
context. Flagged content is stored but EXCLUDED from context — the LLM sees a
sanitized summary + `[connector_result_stored:<id>]`, never raw connector text.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone

from . import config

# ── PII patterns (order matters: email, then 12-digit Aadhaar, then 10-digit phone,
#    so a 12-digit Aadhaar is never clipped by the phone matcher) ─────────────────
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_AADHAAR = re.compile(r"(?<!\d)(?:\d{4}\s\d{4}\s\d{4}|\d{12})(?!\d)")
_PHONE = re.compile(r"(?<![\d])(?:\+?91[\-\s]?|0)?[6-9]\d{4}[\-\s]?\d{5}(?![\d])")

_PII_PASSES = [("email", _EMAIL), ("aadhaar", _AADHAAR), ("phone", _PHONE)]

# ── Injection patterns (case-insensitive · focused to avoid false positives) ─────
_INJECTION = [
    ("ignore_previous", re.compile(r"ignore\s+(?:all\s+)?(?:the\s+)?previous\s+instructions", re.I)),
    ("disregard_above", re.compile(r"disregard\s+(?:the\s+)?(?:above|previous|prior)", re.I)),
    ("forget_instructions", re.compile(r"forget\s+(?:everything|all|your\s+instructions|the\s+above)", re.I)),
    ("you_are_now", re.compile(r"you\s+are\s+now\s+(?:a|an|the|going)", re.I)),
    ("new_instructions", re.compile(r"new\s+instructions\s*:", re.I)),
    ("reveal_prompt", re.compile(r"(?:reveal|print|show|repeat)\s+(?:your\s+)?(?:system\s+)?prompt", re.I)),
    ("system_prompt_ref", re.compile(r"system\s+prompt", re.I)),
    ("jailbreak", re.compile(r"\b(?:jailbreak|DAN\s+mode)\b", re.I)),
    ("override_safety", re.compile(r"(?:override|bypass|disable)\s+(?:your\s+)?(?:safety|guardrails|restrictions)", re.I)),
]

_SUMMARY_CHARS = 400


def _pii_audit_log():
    return config.logs_dir() / "pii-audit.jsonl"


def scrub_pii(text: str) -> tuple[str, list[dict]]:
    """Redact PII in `text`. Returns (scrubbed_text, findings). Findings hold the
    ORIGINAL matched string + type — caller decides whether to audit-log them."""
    findings: list[dict] = []

    def _sub(kind):
        def repl(match):
            findings.append({"type": kind, "original": match.group(0)})
            return "[REDACTED-PII]"
        return repl

    for kind, pattern in _PII_PASSES:
        text = pattern.sub(_sub(kind), text)
    return text, findings


def scan_injection(text: str) -> list[str]:
    """Return the names of injection patterns matched in `text` (empty = clean)."""
    return [name for name, pattern in _INJECTION if pattern.search(text)]


def _audit_pii(findings: list[dict], source: str, ref: str) -> None:
    """Append original PII to the local-only audit log. Never raises."""
    if not findings:
        return
    try:
        log_path = _pii_audit_log()
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(log_path, "a") as fh:
            for finding in findings:
                fh.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "source": source,
                    "ref": ref,
                    "type": finding["type"],
                    "original": finding["original"],
                }, ensure_ascii=False) + "\n")
    except OSError:
        pass  # audit is best-effort; never break ingest


def guard_chunk(chunk: dict) -> dict:
    """Guard one chunk dict in place: scrub PII in `text`, log originals, tag injection.
    Adds metadata: pii_redacted (int), injection_risk (bool), injection_patterns (list)."""
    text = chunk.get("text", "")
    scrubbed, findings = scrub_pii(text)
    if findings:
        _audit_pii(findings, chunk.get("source_file") or chunk.get("source_path", ""),
                   chunk.get("chunk_id", ""))
        chunk["text"] = scrubbed
    chunk["pii_redacted"] = len(findings)
    patterns = scan_injection(scrubbed)
    chunk["injection_risk"] = bool(patterns)
    chunk["injection_patterns"] = patterns
    return chunk


def guard_chunks(chunks: list[dict]) -> list[dict]:
    """Entry point the ingest lanes call."""
    return [guard_chunk(c) for c in chunks]


# ── MCP extension (S1) ───────────────────────────────────────────────────────────

def _neutralize(text: str) -> str:
    """Replace every matched injection span with [flagged] — for summaries only."""
    for _name, pattern in _INJECTION:
        text = pattern.sub("[flagged]", text)
    return text


def guard_mcp_result(conn: sqlite3.Connection, connector_id: str, tool: str,
                     raw_text: str) -> dict:
    """Guard one connector result. Stores the raw text in mcp_results (workspace-
    scoped file = workspace-scoped result), tags it, and returns:

      context_block — the ONLY text allowed into LLM context:
        clean  -> the PII-scrubbed text (still never the raw string)
        flagged-> "[connector_result_stored:<id>] ..." + neutralized summary;
                  the raw text stays in the db, excluded from context.
    """
    scrubbed, findings = scrub_pii(raw_text or "")
    if findings:
        _audit_pii(findings, f"connector:{connector_id}", tool or "")
    patterns = scan_injection(scrubbed)

    cursor = conn.execute(
        "INSERT INTO mcp_results(connector_id, tool, received_at, raw_text, "
        "pii_redacted, injection_risk, injection_patterns, sanitized_summary) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            connector_id,
            tool,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            raw_text,
            len(findings),
            int(bool(patterns)),
            json.dumps(patterns),
            None,  # filled below once computed
        ),
    )
    stored_id = cursor.lastrowid

    if patterns:
        summary = _neutralize(scrubbed)[:_SUMMARY_CHARS]
        context_block = (
            f"[connector_result_stored:{stored_id}] "
            f"(flagged: {', '.join(patterns)} — content withheld) summary: {summary}"
        )
    else:
        summary = scrubbed[:_SUMMARY_CHARS]
        context_block = scrubbed

    conn.execute("UPDATE mcp_results SET sanitized_summary=? WHERE id=?", (summary, stored_id))
    conn.commit()

    return {
        "stored_id": stored_id,
        "injection_risk": bool(patterns),
        "injection_patterns": patterns,
        "pii_redacted": len(findings),
        "context_block": context_block,
    }
