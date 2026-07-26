"""Authenticated localhost (S0 kickoff #3).

The supervisor binds 127.0.0.1 only and generates a per-launch bearer token,
handed to the UI via ~/.heydey/runtime/supervisor.json (0600). Any state-changing
request (POST/PUT/DELETE/PATCH) without that token, or carrying a foreign
Origin header, is refused with 401 before it reaches a route.
"""

import hmac
import json
import os
import secrets
from datetime import datetime, timezone

from . import config

MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def write_runtime_file(port: int, token: str, pid: int) -> None:
    """Token handoff to the UI: owner-only file, rewritten on every launch."""
    config.runtime_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    path = config.runtime_file()
    payload = {
        "port": port,
        "token": token,
        "pid": pid,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    os.chmod(path, 0o600)


def read_runtime_file() -> dict | None:
    path = config.runtime_file()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def request_is_authorized(method: str, headers, token: str) -> bool:
    """True if the request may proceed. Reads (GET/HEAD/OPTIONS) pass freely —
    the 127.0.0.1 bind is their boundary; mutations need origin + token."""
    if method.upper() not in MUTATING_METHODS:
        return True
    origin = headers.get("origin")
    if origin is not None and origin not in config.ALLOWED_ORIGINS:
        return False
    authorization = headers.get("authorization", "")
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(credential.strip(), token)
