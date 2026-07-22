# engine/pe_config.py
"""Instance config loader for the PosterEngine (PE) supervisor.

Each configured PE instance (dev, prod) is polled by engine/pe_poller.py.
Config lives at ~/.local/share/token-budget/pe_instances.json (default) —
no existing loader in this repo predates this file.
"""

import json
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

DEFAULT_CONFIG_PATH = os.path.expanduser(
    "~/.local/share/token-budget/pe_instances.json"
)

_LOCALHOST_HOSTS = {"127.0.0.1", "localhost", "::1"}


class PEConfigError(ValueError):
    """Raised when pe_instances.json is malformed or unsafe."""


@dataclass(frozen=True)
class PEInstance:
    name: str
    base_url: str
    token_ref: str
    kick_method: str  # "launchctl" | "ssh"
    budget_24h_usd: float
    ssh_host: Optional[str] = None


_REQUIRED_FIELDS = ("name", "base_url", "token_ref", "kick_method", "budget_24h_usd")


def _validate_url(base_url: str, name: str) -> None:
    parsed = urlparse(base_url)
    if parsed.hostname in _LOCALHOST_HOSTS:
        return
    if parsed.scheme != "https":
        raise PEConfigError(
            f"pe_instances.json: instance '{name}' has non-localhost base_url "
            f"'{base_url}' without https — refusing to send a Bearer token "
            f"over plaintext."
        )


def load_pe_instances(path: str = DEFAULT_CONFIG_PATH) -> list[PEInstance]:
    """Load and validate PE instance config. Missing file -> empty list."""
    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    instances = []
    for entry in raw:
        missing = [k for k in _REQUIRED_FIELDS if k not in entry]
        if missing:
            raise PEConfigError(
                f"pe_instances.json: entry {entry.get('name', '?')} missing "
                f"required field(s): {missing}"
            )
        _validate_url(entry["base_url"], entry["name"])
        if entry["kick_method"] not in ("launchctl", "ssh"):
            raise PEConfigError(
                f"pe_instances.json: instance '{entry['name']}' has invalid "
                f"kick_method '{entry['kick_method']}'"
            )
        if entry["kick_method"] == "ssh" and not entry.get("ssh_host"):
            raise PEConfigError(
                f"pe_instances.json: instance '{entry['name']}' has "
                f"kick_method=ssh but no ssh_host"
            )
        instances.append(PEInstance(
            name=entry["name"],
            base_url=entry["base_url"].rstrip("/"),
            token_ref=entry["token_ref"],
            kick_method=entry["kick_method"],
            budget_24h_usd=float(entry["budget_24h_usd"]),
            ssh_host=entry.get("ssh_host"),
        ))
    return instances
