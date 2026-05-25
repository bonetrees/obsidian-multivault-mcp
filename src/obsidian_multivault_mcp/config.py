"""YAML config loader. Resolves API keys from env vars at startup."""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .logging_config import setup_logging

logger = setup_logging("obsidian-multivault-mcp.config")

DEFAULT_CONFIG_PATH = "./obsidian-multivault-mcp-config.yaml"


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
        return self.scheme != "https"


@dataclass(frozen=True)
class Config:
    vaults: dict[str, VaultConfig]
    path: Path


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
        if not isinstance(entry, dict):
            raise RuntimeError(f"Vault '{name}' config must be a mapping.")

        scheme = entry.get("scheme", "https")
        if scheme not in ("http", "https"):
            raise RuntimeError(f"Vault '{name}' scheme must be 'http' or 'https', got: {scheme!r}.")

        host = entry.get("host", "127.0.0.1")
        if not isinstance(host, str) or not host.strip():
            raise RuntimeError(f"Vault '{name}' host must be a non-empty string.")

        port = entry.get("port")
        if not isinstance(port, int) or port <= 0 or port > 65535:
            raise RuntimeError(
                f"Vault '{name}' port must be an integer in 1..65535, got: {port!r}."
            )

        api_key_env = entry.get("api_key_env")
        if not isinstance(api_key_env, str) or not api_key_env.strip():
            raise RuntimeError(f"Vault '{name}' missing or invalid 'api_key_env'.")

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
