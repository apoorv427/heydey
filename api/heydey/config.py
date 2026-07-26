"""Runtime paths + constants.

Everything lives under HEYDEY_HOME (default ~/.heydey). Paths are functions,
not module constants, so tests can point HEYDEY_HOME at a temp dir.
"""

import json
import os
from pathlib import Path

DEFAULT_PORT = 4393  # "HEYD" on a phone keypad; override with HEYDEY_PORT

# Origins allowed to send state-changing requests (the local webapp only).
# Any other Origin header on a mutating request -> 401 (S0 kickoff #3).
ALLOWED_ORIGINS = frozenset(
    {
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    }
)


def heydey_home() -> Path:
    return Path(os.environ.get("HEYDEY_HOME", str(Path.home() / ".heydey")))


def workspaces_root() -> Path:
    return heydey_home() / "workspaces"


def runtime_dir() -> Path:
    return heydey_home() / "runtime"


def runtime_file() -> Path:
    return runtime_dir() / "supervisor.json"


def logs_dir() -> Path:
    return heydey_home() / "logs"


def secrets_env_path() -> Path:
    return heydey_home() / "secrets.env"


def port() -> int:
    return int(os.environ.get("HEYDEY_PORT", DEFAULT_PORT))


def corpus_config_path() -> Path:
    """Machine-local corpus + paths config. NEVER committed — this is where an
    installation's own folders, deny-list names, and migration paths live, so the
    source tree stays free of any operator's personal paths (INSTALL.md has the
    schema + an example)."""
    return heydey_home() / "corpus.json"


def load_corpus_config() -> dict:
    """Parse corpus.json. Missing file -> {} (unconfigured install, callers show
    the quickstart hint). Malformed JSON raises loudly — a half-read corpus
    config must never silently narrow the Personal wall or the reveal roots."""
    path = corpus_config_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed corpus config {path}: {exc}") from exc
