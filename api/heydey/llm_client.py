"""Thin multi-provider LLM client — wrapper-free (L6). No langchain/litellm/openai-sdk.

Two lanes, both hand-rolled over stdlib urllib:

- **ollama** — local, $0, always-on. This host runs Ollama 0.23.2, which has
  *native* think-control: pass a top-level ``think: false`` and qwen3's reasoning
  is silenced cleanly (clean JSON in ~3.6s warm). The ``/no_think`` string hack
  carried in the 2026-05 BLOS router is OBSOLETE here — with it qwen3 burned every
  token into an empty content field (measured: 300 tokens, 0 chars, 31s). Verified
  2026-07-19: ``think:false`` -> 21 tokens, valid JSON, 3.6s.
- **openrouter** — optional cloud lane (BYO key, ``OPENROUTER_API_KEY`` / Keychain).
  Selected only by the ``balanced`` / ``quality-first`` profiles; the S3 validator
  gate never *requires* it — the local cross-family pair (llama3.1 -> qwen3) is the
  documented offline path (Executor Contract A #4) and is what the gate is proven on.

Every call returns a :class:`Completion` carrying token counts + measured cost +
latency, so the pipeline can write the ``costs`` / ``receipts`` ledgers honestly.
Fail-closed: an unreachable provider RAISES (never a silent empty pass).
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_OLLAMA_URL = "http://localhost:11434"

# Model-family registry. The cross-model rule (executor family != validator family)
# is the product's core claim, so family inference must be explicit + auditable, not
# guessed. Local ids are pinned; cloud ids fall back to their vendor prefix.
_FAMILY: dict[str, str] = {
    "llama3.1:8b": "llama",
    "llama3.1": "llama",
    "qwen3:8b": "qwen",
    "qwen3": "qwen",
    "llava:7b": "llava",
    "deepseek/deepseek-chat": "deepseek",
    "anthropic/claude-sonnet-4.6": "anthropic",
    "meta-llama/llama-3.1-8b-instruct": "llama",
    "qwen/qwen-2.5-72b-instruct": "qwen",
}

# Cloud price table (USD per 1M tokens, in/out). Local = 0. Unknown cloud -> 0 + a
# tier_reason note so the ledger never silently invents a cost.
_PRICE: dict[str, tuple[float, float]] = {
    "deepseek/deepseek-chat": (0.27, 1.10),
    "anthropic/claude-sonnet-4.6": (3.00, 15.00),
    "meta-llama/llama-3.1-8b-instruct": (0.02, 0.03),
    "qwen/qwen-2.5-72b-instruct": (0.35, 0.40),
}

# Models served by local Ollama on this host (provider = ollama). Everything else
# with a vendor "/" prefix routes to openrouter.
LOCAL_MODELS = frozenset({"llama3.1:8b", "qwen3:8b", "llava:7b"})


class LLMError(RuntimeError):
    """A provider was unreachable or returned an error. Fail-closed signal."""


def family_of(model: str) -> str:
    """Auditable family label for a model id. Cloud ids -> vendor prefix; local ids
    reduce to their base name with version suffixes stripped (llama3.2:3b -> llama),
    so a same-family model can never dodge the rule behind a version number."""
    if model in _FAMILY:
        return _FAMILY[model]
    base = model.split("/")[0].split(":")[0].lower()
    stripped = re.sub(r"[\d.]+$", "", base).rstrip("-_.")
    base = stripped or base
    return {"meta-llama": "llama"}.get(base, base)


def provider_of(model: str) -> str:
    return "ollama" if model in LOCAL_MODELS or ":" in model and "/" not in model else "openrouter"


def ollama_url() -> str:
    return os.environ.get("HEYDEY_OLLAMA_URL", DEFAULT_OLLAMA_URL)


@dataclass
class Completion:
    text: str
    model: str
    provider: str
    tokens_in: int
    tokens_out: int
    latency_ms: float
    cost_usd: float


def _loose_json(raw: str) -> str:
    """Strip ```fences``` and any stray <think> block; return the outermost {...} or [...]."""
    raw = raw.strip()
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw).strip().rstrip("`").strip()
    # prefer an array if one is present (validator returns a list), else an object
    for opener, closer in (("[", "]"), ("{", "}")):
        s, e = raw.find(opener), raw.rfind(closer)
        if s != -1 and e > s:
            return raw[s : e + 1]
    return raw


def parse_json(text: str):
    """Loose JSON parse for small-model output. Raises json.JSONDecodeError on failure."""
    return json.loads(_loose_json(text))


def _cost(model: str, tin: int, tout: int) -> float:
    rin, rout = _PRICE.get(model, (0.0, 0.0))
    return (tin / 1_000_000) * rin + (tout / 1_000_000) * rout


def _call_ollama(model, prompt, system, max_tokens, temperature, timeout) -> tuple[str, int, int]:
    # Inline system into the user turn: a separate system role reliably re-triggers
    # qwen3's thinking despite think:false, per the proven BLOS note. llama3.1 is
    # unbothered by the inline form, so one code path serves both.
    content = f"{system}\n\n{prompt}" if system else prompt
    body = {
        "model": model,
        "think": False,  # native think-control (Ollama >=0.23) — the real qwen3 fix
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        # Keep BOTH lane models resident between calls — the S4a/S4b full-lane
        # variance (25-67s) was Ollama swapping ~5GB models in and out per call.
        "keep_alive": os.environ.get("HEYDEY_OLLAMA_KEEP_ALIVE", "30m"),
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    req = urllib.request.Request(
        f"{ollama_url()}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise LLMError(f"ollama unreachable ({model}): {exc}") from exc
    msg = data.get("message", {})
    return msg.get("content", "") or "", data.get("prompt_eval_count", 0), data.get("eval_count", 0)


def _call_openrouter(model, prompt, system, max_tokens, temperature, timeout) -> tuple[str, int, int]:
    # env var first (dev / CI), then Keychain via secrets_store (the /models UI's
    # "store in Keychain" flow lands the key here; found while wiring the
    # DeepSeek recall re-measure — the UI stored it but this lane never looked)
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        from . import secrets_store
        key = secrets_store.get_secret("OPENROUTER_API_KEY")
    if not key:
        raise LLMError(f"OPENROUTER_API_KEY not set — cannot reach cloud model {model}")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "stream": False}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise LLMError(f"openrouter HTTP {exc.code} ({model}): {exc.read().decode(errors='replace')[:200]}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise LLMError(f"openrouter unreachable ({model}): {exc}") from exc
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return content, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


def complete(model: str, prompt: str, *, system: str = "", max_tokens: int = 512,
             temperature: float = 0.0, timeout: float = 120.0) -> Completion:
    """Single completion. Dispatch by provider. Fail-closed: raises LLMError on failure."""
    provider = provider_of(model)
    fn = _call_ollama if provider == "ollama" else _call_openrouter
    t0 = time.time()
    text, tin, tout = fn(model, prompt, system, max_tokens, temperature, timeout)
    return Completion(
        text=text, model=model, provider=provider, tokens_in=tin, tokens_out=tout,
        latency_ms=round((time.time() - t0) * 1000, 1), cost_usd=_cost(model, tin, tout),
    )


def is_reachable(model: str) -> bool:
    """Cheap liveness probe used by the validator's offline-path decision."""
    try:
        if provider_of(model) == "ollama":
            req = urllib.request.Request(f"{ollama_url()}/api/tags")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                tags = json.loads(resp.read().decode())
            names = {m.get("name", "").split(":")[0] for m in tags.get("models", [])}
            names |= {m.get("name", "") for m in tags.get("models", [])}
            return model in names or model.split(":")[0] in names
        return bool(os.getenv("OPENROUTER_API_KEY"))
    except Exception:
        return False
