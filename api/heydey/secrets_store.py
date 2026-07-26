"""Secrets — macOS Keychain first, chmod-600 env file fallback (S0 kickoff #4).

Key material must never appear in heydey.db, any log, receipt, or API response.
Two structural guards live here:

- get_secret() only reads from the Keychain (`security` CLI) or an env file whose
  permissions are exactly owner-only; a group/world-readable file is refused.
- every value this process has served is registered for log redaction, and
  install_redaction() masks those values in any log record that slips through.
"""

import logging
import os
import stat
import subprocess

from . import config

KEYCHAIN_SERVICE = "heydey"
REDACTED = "•••redacted•••"

log = logging.getLogger(__name__)

_served_values: set[str] = set()


def _register(value: str) -> str:
    if value:
        _served_values.add(value)
    return value


def _from_keychain(name: str) -> str | None:
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.rstrip("\n")
    return value or None


def _from_env_file(name: str) -> str | None:
    path = config.secrets_env_path()
    if not path.is_file():
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        log.warning(
            "secrets env file %s has mode %o (must be 0600) — refusing to read it",
            path,
            mode,
        )
        return None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip() or None
    return None


def get_secret(name: str, *, use_keychain: bool = True) -> str | None:
    """Keychain, then the 0600 env file. Returns None if absent — never raises
    into a response path, never logs the value."""
    value = _from_keychain(name) if use_keychain else None
    if value is None:
        value = _from_env_file(name)
    if value is None:
        return None
    return _register(value)


def set_secret(name: str, value: str) -> None:
    """Write/update a generic password in the login Keychain."""
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", name, "-w", value],
        check=True,
        capture_output=True,
        timeout=10,
    )
    _register(value)


class SecretRedactionFilter(logging.Filter):
    """Masks any served secret value that appears in a log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _served_values:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = message
        for value in _served_values:
            if value and value in redacted:
                redacted = redacted.replace(value, REDACTED)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install_redaction() -> None:
    """Attach the redaction filter to the root + uvicorn loggers' handlers."""
    redaction = SecretRedactionFilter()
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.addFilter(redaction)
        for handler in logger.handlers:
            handler.addFilter(redaction)
