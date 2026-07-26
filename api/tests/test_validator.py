"""Executor Contract A — the cross-model validator gate (deterministic, hermetic).

The completion call is dependency-injected, so groundedness verdicts are canned here;
the LIVE cross-model proof is `python -m heydey.s3_eval` (50 prompts, real llama3.1 →
qwen3). These tests pin the STRUCTURE: family rule, fail-closed, offline badge."""

import pytest

from heydey import models_config, validator
from heydey.models_config import ConfigError, Pair, Profile
from heydey.validator import ValidatorFamilyError, validate


# ── #2 family pairing — enforced at CONFIG-WRITE (test_family_enforced) ────────
def test_family_enforced_on_save(heydey_home):
    same = Profile(name="bad", default=Pair("llama3.1:8b", "llama3.1:8b"))
    with pytest.raises(ConfigError, match="family"):
        models_config.save_profile(same)


def test_family_enforced_on_task_override(heydey_home):
    p = Profile(name="bad2", default=Pair("llama3.1:8b", "qwen3:8b"),
                tasks={"ask": Pair("qwen3:8b", "qwen3:8b")})  # override violates
    with pytest.raises(ConfigError):
        models_config.save_profile(p)


def test_shipped_profiles_all_valid():
    for prof in models_config.DEFAULT_PROFILES.values():
        prof.validate()  # must not raise — every shipped pair is cross-family


def test_family_rule_immune_to_version_suffixes(heydey_home):
    """Regression (found at S4a): llama3.2:3b must collide with llama3.1:8b —
    an unlisted version suffix may never mint a fake distinct family (fail-open)."""
    versioned = Profile(name="bad3", default=Pair("llama3.1:8b", "llama3.2:3b"))
    with pytest.raises(ConfigError, match="family"):
        models_config.save_profile(versioned)
    dotted = Profile(name="bad4", default=Pair("qwen3:8b", "qwen2.5:14b"))
    with pytest.raises(ConfigError, match="family"):
        models_config.save_profile(dotted)


def test_cross_family_saves_and_loads(heydey_home):
    good = Profile(name="good", default=Pair("llama3.1:8b", "qwen3:8b"))
    models_config.save_profile(good)
    assert models_config.get_pair("good", "ask").validator == "qwen3:8b"


# ── #2 family pairing — enforced at RUNTIME (the gate refuses same-family) ─────
def test_validator_refuses_same_family():
    with pytest.raises(ValidatorFamilyError):
        validate("Any claim.", [{"payload": {"text": "x"}}],
                 executor_model="qwen3:8b", validator_model="qwen3:8b")


# ── #3 fail-closed — a fabricated claim never returns pass:true ───────────────
CHUNKS = [{"payload": {"text": "The timeline is about two months.", "doc_id": "d1", "chunk_id": "c1"},
           "vscore": 0.8}]


def test_fail_closed_on_fabrication():
    # validator (stub) says the sentence is NOT grounded -> passed must be False
    stub = lambda m, p, s, mt: '[{"i":0,"grounded":false,"chunk":null,"reason":"not in evidence"}]'
    r = validate("The timeline is ten years.", CHUNKS,
                 executor_model="llama3.1:8b", validator_model="qwen3:8b", complete_fn=stub)
    assert r.status == "validated"
    assert r.passed is False
    assert r.failed_claims and r.failed_claims[0]["sentence_index"] == 0


def test_grounded_claim_passes():
    stub = lambda m, p, s, mt: '[{"i":0,"grounded":true,"chunk":1,"reason":"chunk 1 states it"}]'
    r = validate("The timeline is about two months.", CHUNKS,
                 executor_model="llama3.1:8b", validator_model="qwen3:8b", complete_fn=stub)
    assert r.passed is True
    assert r.verdicts[0].confidence > 0  # numeric confidence carried (§14-A4)
    assert r.badge.endswith("PASS")


def test_unparseable_verdict_fails_closed():
    # a validator that returns garbage must FAIL closed, never silently pass
    stub = lambda m, p, s, mt: "I could not decide, sorry."
    r = validate("The price is 25 lakh.", CHUNKS,
                 executor_model="llama3.1:8b", validator_model="qwen3:8b", complete_fn=stub)
    assert r.passed is False


# ── #4 offline path — no reachable validator -> UNVALIDATED badge, never a pass ─
def test_offline_badge(heydey_home, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("HEYDEY_OLLAMA_URL", "http://127.0.0.1:9")  # dead port
    r = validate("The timeline is about two months.", CHUNKS,
                 executor_model="llama3.1:8b", validator_model="deepseek/deepseek-chat")
    assert r.status == "unvalidated_offline"
    assert r.passed is None
    assert r.badge == "UNVALIDATED — offline"


# ── sentence splitting sanity (ops text is full of ₹12-18 L, L28, S4a) ────────
def test_split_claims_handles_ops_tokens():
    text = "ORION floor is ₹12 L. AMC is ~18% separate. See L28 and S4a."
    sents = validator.split_claims(text)
    assert len(sents) == 3
    assert "₹12 L" in sents[0]
