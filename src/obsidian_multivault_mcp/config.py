"""YAML config loader. Resolves API keys from env vars at startup."""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .logging_config import setup_logging

logger = setup_logging("obsidian-multivault-mcp.config")

DEFAULT_CONFIG_PATH = "./obsidian-multivault-mcp-config.yaml"

# Hosts where TLS verification is disabled by default because the plugin
# ships a self-signed cert and the connection cannot be MITM'd in practice.
# For any other host we keep verification on so a misconfigured remote
# vault doesn't silently fall back to an unverified TLS session.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class VaultConfig:
    name: str
    scheme: str
    host: str
    port: int
    api_key: str

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def verify_ssl(self) -> bool:
        # HTTP has no TLS; the verify flag is irrelevant but we return True for cleanliness.
        if self.scheme != "https":
            return True
        # HTTPS to loopback: the plugin uses a self-signed cert, so verification has to
        # be off. HTTPS to any non-loopback host: leave verification on — the user must
        # have a properly issued cert if they exposed the plugin off-localhost.
        return self.host not in LOOPBACK_HOSTS


@dataclass(frozen=True)
class Config:
    vaults: dict[str, VaultConfig]
    path: Path


# pylint: disable-next=too-many-branches
def load_config(path: str | os.PathLike | None = None) -> Config:
    config_path = Path(path or os.environ.get("OBSIDIAN_MCP_CONFIG", DEFAULT_CONFIG_PATH))
    if not config_path.exists():
        raise RuntimeError(
            f"Config file not found: {config_path}. " "Set OBSIDIAN_MCP_CONFIG or create the file."
        )
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Config file {config_path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict) or "vaults" not in raw:
        raise RuntimeError(f"Config file {config_path} missing top-level 'vaults' key.")
    raw_vaults = raw["vaults"]
    if not isinstance(raw_vaults, dict) or not raw_vaults:
        raise RuntimeError(f"Config file {config_path} 'vaults' must be a non-empty mapping.")

    vaults: dict[str, VaultConfig] = {}
    for name, entry in raw_vaults.items():
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError(f"Vault name must be a non-empty string, got: {name!r}.")
        # Reject (don't silently strip) — a name with surrounding whitespace would
        # store as ' personal ' but fail VaultName validation when callers pass 'personal'.
        if name.strip() != name:
            raise RuntimeError(
                f"Vault name must not have leading or trailing whitespace, got: {name!r}."
            )
        if not isinstance(entry, dict):
            raise RuntimeError(f"Vault '{name}' config must be a mapping.")

        scheme = entry.get("scheme", "https")
        if scheme not in ("http", "https"):
            raise RuntimeError(f"Vault '{name}' scheme must be 'http' or 'https', got: {scheme!r}.")

        host = entry.get("host", "127.0.0.1")
        if not isinstance(host, str) or not host.strip():
            raise RuntimeError(f"Vault '{name}' host must be a non-empty string.")
        if host.strip() != host:
            raise RuntimeError(
                f"Vault '{name}' host must not have leading or trailing whitespace, got: {host!r}."
            )

        port = entry.get("port")
        if not isinstance(port, int) or port <= 0 or port > 65535:
            raise RuntimeError(
                f"Vault '{name}' port must be an integer in 1..65535, got: {port!r}."
            )

        api_key_env = entry.get("api_key_env")
        if not isinstance(api_key_env, str) or not api_key_env.strip():
            raise RuntimeError(f"Vault '{name}' missing or invalid 'api_key_env'.")
        if api_key_env.strip() != api_key_env:
            raise RuntimeError(
                f"Vault '{name}' api_key_env must not have leading or trailing whitespace, "
                f"got: {api_key_env!r}."
            )

        api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(
                f"Vault '{name}' API key env var {api_key_env!r} is missing or empty."
            )

        vaults[name] = VaultConfig(
            name=name,
            scheme=scheme,
            host=host,
            port=port,
            api_key=api_key,
        )

    logger.info(
        "Loaded config from %s with %s vault(s): %s",
        config_path,
        len(vaults),
        ", ".join(sorted(vaults)),
    )
    return Config(vaults=vaults, path=config_path)
