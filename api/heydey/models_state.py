"""Active-profile state for the Models panel — which saved profile the router uses.

File-backed (HEYDEY_HOME/models/active.json), falling back to "local-only" — the
profile the S3 gate was proven on — whenever the file is missing or names a
profile that no longer loads. Activation validates the profile first, so a
family-rule-violating profile can never become active even by editing disk state.
"""

import json

from . import config, models_config

DEFAULT = "local-only"


def _path():
    return config.heydey_home() / "models" / "active.json"


def active_profile() -> str:
    path = _path()
    if path.is_file():
        try:
            name = json.loads(path.read_text()).get("active", DEFAULT)
            models_config.load_profile(name)  # must still exist and pass the family rule
            return name
        except (OSError, ValueError, json.JSONDecodeError):
            return DEFAULT
    return DEFAULT


def set_active(name: str) -> None:
    models_config.load_profile(name)  # raises ConfigError on unknown/invalid
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps({"active": name}))
    path.chmod(0o600)
