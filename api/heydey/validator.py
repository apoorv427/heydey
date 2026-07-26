"""The cross-model validator gate (Executor Contract A) — the product's core claim.

No artifact or action ships as "validated" unless a model of a **different family**
than the executor confirmed that every claim-sentence follows from the retrieved
chunks. Groundedness only — never style. This is the one guarantee no single-model
vendor can structurally make (fox-guarding-henhouse).

Behavior (Contract A, verbatim intent):
1. Split the answer into claim-sentences; the validator judges each against the
   supplied chunks: grounded? which chunk? (batched into one call — still
   per-sentence granular, and far cheaper than N calls).
2. Family pairing is a HARD rule: executor family != validator family, or the gate
   refuses to run (:class:`ValidatorFamilyError`). The config-write twin is in
   :mod:`heydey.models_config`.
3. Fail policy lives in the pipeline: pass:false -> one retry -> degrade to
   extractive. This module only *judges*; it never ships text.
4. Offline path: if the configured validator is unreachable, fall back to local
   ``qwen3:8b``; if no validator is reachable at all, return status
   ``unvalidated_offline`` (the badge). That labeled badge is the ONLY permitted
   bypass — never a silent pass.

The completion call is dependency-injected (``complete_fn``) so the contract
assertions run deterministically; the live gate passes the real Ollama client.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import llm_client
from .llm_client import family_of

LOCAL_VALIDATOR_FALLBACK = "qwen3:8b"

# A completion fn: (model, prompt, system, max_tokens) -> text
CompleteFn = Callable[..., str]


class ValidatorFamilyError(RuntimeError):
    """Executor and validator share a family — the gate cannot run (fail-closed)."""


@dataclass
class SentenceVerdict:
    sentence_index: int
    claim: str
    grounded: bool
    chunk: Optional[int]
    reason: str
    confidence: float  # numeric confidence (§14-A4): grounded x normalized retrieval score


@dataclass
class ValidationResult:
    status: str  # "validated" | "unvalidated_offline"
    passed: Optional[bool]  # None only when unvalidated_offline
    executor_model: str
    validator_model: str
    verdicts: list[SentenceVerdict] = field(default_factory=list)
    failed_claims: list[dict] = field(default_factory=list)

    @property
    def badge(self) -> str:
        if self.status == "unvalidated_offline":
            return "UNVALIDATED — offline"
        return f"{self.executor_model} → {self.validator_model} · {'PASS' if self.passed else 'FAIL'}"


# ── sentence splitting ────────────────────────────────────────────────────────
_ABBREV = {"e.g", "i.e", "etc", "vs", "no", "mr", "ms", "dr", "rs", "approx", "cr"}


def split_claims(text: str) -> list[str]:
    """Split an answer into claim-sentences. Tolerant of ₹12-18 L, L28, decimals,
    and inline [1] citations — small-answer heuristic, not a full NLP tokenizer."""
    text = (text or "").strip()
    if not text:
        return []
    # protect decimals (2.5) and lettered locks (L28.) minimally by splitting on
    # sentence-final punctuation followed by whitespace + an uppercase/quote/[ start.
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'\[])", text)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # merge a fragment ending in a known abbreviation into the next sentence
        last = re.sub(r"[^a-z]", "", p.split()[-1].lower()) if p.split() else ""
        if out and last in _ABBREV:
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return out


# ── the validator prompt ──────────────────────────────────────────────────────
_SYSTEM = (
    "You are a strict groundedness checker. You judge ONLY whether each numbered "
    "sentence's factual assertions are directly supported by the numbered evidence "
    "chunks. You do not judge style, tone, or usefulness. A sentence that makes no "
    "factual claim about the business — a refusal, a hedge like 'the records do not "
    "cover this', or a question — is grounded=true by default. A sentence stating a "
    "specific fact (a number, name, date, decision) is grounded=true ONLY if a chunk "
    "states it; otherwise grounded=false."
)


def _build_prompt(sentences: list[str], chunks: list[dict]) -> str:
    ev = "\n".join(
        f"[{i + 1}] {(c.get('payload', {}).get('text') or c.get('text', ''))[:500]}"
        for i, c in enumerate(chunks)
    )
    sent = "\n".join(f"({i}) {s}" for i, s in enumerate(sentences))
    return (
        f"EVIDENCE CHUNKS:\n{ev}\n\n"
        f"SENTENCES TO CHECK:\n{sent}\n\n"
        "Return ONLY a JSON array, one object per sentence, in order:\n"
        '[{"i": 0, "grounded": true, "chunk": 1, "reason": "chunk 1 states it"}, ...]\n'
        "chunk = the supporting chunk number, or null if grounded is false."
    )


def _default_complete(model: str, prompt: str, system: str, max_tokens: int) -> str:
    return llm_client.complete(model, prompt, system=system, max_tokens=max_tokens).text


def _retrieval_conf(chunk_idx: Optional[int], chunks: list[dict]) -> float:
    """Normalise a supporting chunk's cosine score into a 0..1 confidence contribution."""
    if chunk_idx is None or not (1 <= chunk_idx <= len(chunks)):
        return 0.0
    c = chunks[chunk_idx - 1]
    score = c.get("vscore")
    if score is None:
        score = c.get("score")
    if score is None:
        return 0.5  # keyword-only hit: grounded but no cosine — mid confidence
    return max(0.0, min(1.0, float(score)))


def band(conf: float) -> str:
    return "high" if conf >= 0.66 else "medium" if conf >= 0.33 else "low"


def validate(answer_text: str, chunks: list[dict], *, executor_model: str,
             validator_model: str, complete_fn: Optional[CompleteFn] = None,
             max_tokens: int = 700) -> ValidationResult:
    """Judge every claim-sentence for groundedness against ``chunks``.

    Raises ValidatorFamilyError if the pair shares a family (fail-closed). Returns a
    ValidationResult; ``status='unvalidated_offline'`` when no validator is reachable.
    """
    if family_of(executor_model) == family_of(validator_model):
        raise ValidatorFamilyError(
            f"executor {executor_model!r} and validator {validator_model!r} share family "
            f"{family_of(executor_model)!r} — the checker must differ from the writer."
        )

    sentences = split_claims(answer_text)
    if not sentences:
        return ValidationResult("validated", True, executor_model, validator_model)

    complete = complete_fn or _default_complete

    # Offline path: pick a reachable validator, or emit the badge. The injected
    # complete_fn is treated as always reachable (tests supply it deliberately).
    active_validator = validator_model
    if complete_fn is None:
        if not llm_client.is_reachable(validator_model):
            if llm_client.is_reachable(LOCAL_VALIDATOR_FALLBACK) and \
                    family_of(executor_model) != family_of(LOCAL_VALIDATOR_FALLBACK):
                active_validator = LOCAL_VALIDATOR_FALLBACK
            else:
                return ValidationResult("unvalidated_offline", None, executor_model, validator_model)

    prompt = _build_prompt(sentences, chunks)
    try:
        raw = complete(active_validator, prompt, _SYSTEM, max_tokens)
        parsed = llm_client.parse_json(raw)
        if isinstance(parsed, dict):  # a lone object -> wrap
            parsed = [parsed]
        by_i = {int(o.get("i", n)): o for n, o in enumerate(parsed)}
    except Exception:
        # A validator that cannot produce a parseable verdict must FAIL closed, not
        # pass. Treat every sentence as unproven.
        by_i = {}

    verdicts: list[SentenceVerdict] = []
    failed: list[dict] = []
    for i, s in enumerate(sentences):
        o = by_i.get(i, {})
        grounded = bool(o.get("grounded", False))
        chunk = o.get("chunk")
        chunk = int(chunk) if isinstance(chunk, (int, float)) and chunk else None
        reason = str(o.get("reason", "no verdict returned"))[:200]
        conf = round(_retrieval_conf(chunk, chunks) if grounded else 0.0, 3)
        verdicts.append(SentenceVerdict(i, s, grounded, chunk, reason, conf))
        if not grounded:
            failed.append({"sentence_index": i, "claim": s, "reason": reason})

    return ValidationResult(
        status="validated", passed=(len(failed) == 0),
        executor_model=executor_model, validator_model=active_validator,
        verdicts=verdicts, failed_claims=failed,
    )
