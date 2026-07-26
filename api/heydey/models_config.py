"""Model router config — the Models-panel logic (L32), family rule enforced at SAVE.

A *profile* maps a task-class to an (executor, validator) model pair plus a daily
budget. The proof-grade guarantee — the model that writes an answer is never the
model that checks it — is made **unmisconfigurable** here: ``save_profile`` refuses
to persist any pair whose executor and validator share a family. This is the
config-write half of Executor Contract A (``test_family_enforced``); the runtime
half lives in :mod:`heydey.validator`.

Three shipped profiles (Contract A pairing table):
- ``local-only``  — llama3.1:8b (llama) -> qwen3:8b (qwen).  $0, sovereign, offline.
                     This is what the S3 gate is proven on (no cloud key required).
- ``balanced``    — deepseek-chat (deepseek) -> qwen3:8b (qwen).  cloud exec, local check.
- ``quality-first``— claude-sonnet (anthropic) -> deepseek-chat (deepseek).

Profiles persist as JSON under HEYDEY_HOME/models/<profile>.json. No secrets here —
BYO keys live in Keychain / env, only referenced by the provider lane.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from . import config
from .llm_client import family_of


class ConfigError(ValueError):
    """A profile violated the family rule (or was otherwise invalid) at save time."""


@dataclass
class Pair:
    executor: str
    validator: str

    def check(self) -> None:
        ef, vf = family_of(self.executor), family_of(self.validator)
        if ef == vf:
            raise ConfigError(
                f"validator-family rule violated: executor {self.executor!r} and validator "
                f"{self.validator!r} are both family {ef!r}. The checker must differ from the "
                "writer — pick a different-family validator."
            )


@dataclass
class Profile:
    name: str
    default: Pair
    tasks: dict[str, Pair] = field(default_factory=dict)
    budget_usd: float = 0.0
    egress: str = "any"  # "any" | "local_only" — Contract C Layer 3 (DPDP clinic posture)

    def validate(self) -> "Profile":
        """Family rule over the default pair AND every task override. Raises on violation."""
        self.default.check()
        for pair in self.tasks.values():
            pair.check()
        return self

    def pair_for(self, task_class: str) -> Pair:
        return self.tasks.get(task_class, self.default)


DEFAULT_PROFILES: dict[str, Profile] = {
    "local-only": Profile(
        name="local-only",
        default=Pair("llama3.1:8b", "qwen3:8b"),
        budget_usd=0.0,
    ),
    "balanced": Profile(
        name="balanced",
        default=Pair("deepseek/deepseek-chat", "qwen3:8b"),
        budget_usd=2.0,
    ),
    "quality-first": Profile(
        name="quality-first",
        default=Pair("anthropic/claude-sonnet-4.6", "deepseek/deepseek-chat"),
        budget_usd=5.0,
    ),
}


def _profiles_dir():
    d = config.heydey_home() / "models"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _to_dict(p: Profile) -> dict:
    return {"name": p.name, "default": asdict(p.default),
            "tasks": {k: asdict(v) for k, v in p.tasks.items()},
            "budget_usd": p.budget_usd, "egress": p.egress}


def _from_dict(d: dict) -> Profile:
    return Profile(
        name=d["name"],
        default=Pair(**d["default"]),
        tasks={k: Pair(**v) for k, v in d.get("tasks", {}).items()},
        budget_usd=d.get("budget_usd", 0.0),
        egress=d.get("egress", "any"),
    )


def save_profile(profile: Profile) -> None:
    """Validate the family rule, THEN persist. A same-family pair never reaches disk."""
    profile.validate()  # raises ConfigError before any write — the unmisconfigurable gate
    path = _profiles_dir() / f"{profile.name}.json"
    path.write_text(json.dumps(_to_dict(profile), indent=2))
    path.chmod(0o600)


def load_profile(name: str) -> Profile:
    """Load a saved profile, else fall back to the built-in default of that name."""
    path = _profiles_dir() / f"{name}.json"
    if path.is_file():
        return _from_dict(json.loads(path.read_text())).validate()
    if name in DEFAULT_PROFILES:
        return DEFAULT_PROFILES[name]
    raise ConfigError(f"unknown profile {name!r}")


def get_pair(profile_name: str, task_class: str = "ask") -> Pair:
    """(executor, validator) for a task under a profile — the router's one public call."""
    return load_profile(profile_name).pair_for(task_class)
