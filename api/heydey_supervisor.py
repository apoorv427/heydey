#!/usr/bin/env python3.12
"""Heydey supervisor — single job owner, authenticated localhost (S0 stub).

Boot sequence:
  1. logging to ~/.heydey/logs/supervisor.log with secret redaction installed
  2. per-launch bearer token -> ~/.heydey/runtime/supervisor.json (0600) for the UI
  3. serve on 127.0.0.1 ONLY (never 0.0.0.0)

KeepAlive-ready: launchd plist at launchd/ai.heydey.supervisor.plist; a failed
bind exits nonzero so launchd restarts us; SIGTERM shuts down cleanly.
"""

import logging
import os
import sys

import uvicorn

from heydey import config
from heydey.auth import generate_token, write_runtime_file
from heydey.secrets_store import install_redaction
from heydey.server import create_app

log = logging.getLogger("heydey.supervisor")


def _setup_logging() -> None:
    config.logs_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(config.logs_dir() / "supervisor.log"),
            logging.StreamHandler(sys.stderr),
        ],
    )
    install_redaction()


def _already_running(port: int) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


def main() -> int:
    _setup_logging()
    port = config.port()
    # Guard BEFORE the runtime-file write: a second launch used to clobber the
    # live supervisor's token (everything then 401s against the survivor) and
    # only afterwards die on the bind. Found twice on 2026-07-19.
    if _already_running(port):
        log.error(
            "a supervisor already answers on 127.0.0.1:%s — leaving its runtime "
            "token untouched. Stop it first: lsof -ti :%s | xargs kill", port, port,
        )
        return 2
    token = generate_token()
    write_runtime_file(port=port, token=token, pid=os.getpid())
    log.info("supervisor boot: 127.0.0.1:%s · runtime file %s", port, config.runtime_file())

    app = create_app(token)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    except SystemExit as exc:  # uvicorn re-raises startup failures (e.g. port in use)
        return int(exc.code or 1)
    except OSError as exc:
        log.error("bind failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
