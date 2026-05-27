"""YAML config loader. Resolves API keys from env vars at startup."""

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .logging_config import setup_logging

logger = setup_logging("obsidian-multivault-mcp.config")

DEFAULT_CONFIG_PATH = "./obsidian-multivault-mcp-config.yaml"


def is_loopback_host(host: str) -> bool:
    """True if `host` resolves to the loopback range. Used to decide whether
    TLS verification can safely be skipped (loopback can't be MITM'd in
    practice and the plugin ships a self-signed cert).

    Covers all of 127.0.0.0/8 and ::1, not just the canonical 127.0.0.1 —
    distros like Debian/Ubuntu put the system hostname at 127.0.1.1 and
    those should still be treated as loopback. ``localhost`` is special-
    cased because DNS resolution of that name happens later in httpx, not
    here; the match is case-insensitive since DNS names are.

    Bracketed IPv6 literals (e.g. ``"[::1]"`` as accepted on the CLI or in
    the OBSIDIAN_MCP_HOST env var) are normalized before parsing — config
    loading already strips brackets for stored hosts, but callers that
    pass user input directly need this guard too.
    """
    if host.lower() == "localhost":
        return True
    # Accept both "::1" and "[::1]" — strip brackets if present so
    # ipaddress.ip_address() can parse the literal.
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Not an IP literal — treat as a regular DNS name.
        return False


# Bare DNS labels per RFC 1123: alphanumerics and hyphens, dots between labels,
# no leading/trailing hyphen on any label. Used to validate host strings that
# aren't IP literals.
_DNS_NAME_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


def _validate_host(name: str, host: str) -> None:
    """Reject hosts that look like URLs or include a port.

    The caller already strips surrounding brackets for IPv6 URL form.
    What we want to catch here is e.g. ``"127.0.0.1:27124"`` (port in the
    host string) and ``"https://vault.example.com"`` (scheme in the host
    string) — both produce malformed URLs once we paste ``host`` into
    ``f"{scheme}://{host}:{port}"``.
    """
    if "://" in host:
        raise RuntimeError(
            f"Vault '{name}' host must not include a scheme, got: {host!r}. "
            "Set the scheme via the 'scheme' field."
        )
    if "/" in host:
        raise RuntimeError(f"Vault '{name}' host must not include a path, got: {host!r}.")

    # Accept anything that parses as an IP address. ip_address rejects
    # "127.0.0.1:27124" because ":27124" isn't valid in an IPv4 dotted-quad.
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass

    if ":" in host:
        raise RuntimeError(
            f"Vault '{name}' host {host!r} is not a valid IPv4/IPv6 literal. "
            "Did you include a port? Configure the port via the 'port' field."
        )

    if not _DNS_NAME_RE.match(host):
        raise RuntimeError(f"Vault '{name}' host {host!r} is not a valid DNS name or IP literal.")


@dataclass(frozen=True)
class VaultConfig:
    name: str
    scheme: str
    host: str
    port: int
    # repr=False so an accidental print/log of a VaultConfig doesn't leak the
    # bearer token. The value is still equality-checked and hashable normally.
    api_key: str = field(repr=False)

    @property
    def base_url(self) -> str:
        # IPv6 literals must be bracketed in URLs (RFC 3986). Detect by the
        # presence of ':' in the host. Already-bracketed hosts are normalised
        # away at load time so we can rely on the unbracketed canonical form
        # here. IPv4 dotted-quads and DNS names never contain ':'.
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"

    @property
    def verify_ssl(self) -> bool:
        # HTTP has no TLS; the verify flag is irrelevant but we return True for cleanliness.
        if self.scheme != "https":
            return True
        # HTTPS to loopback: the plugin uses a self-signed cert, so verification has to
        # be off. HTTPS to any non-loopback host: leave verification on — the user must
        # have a properly issued cert if they exposed the plugin off-localhost.
        return not is_loopback_host(self.host)


@dataclass(frozen=True)
class Config:
    vaults: dict[str, VaultConfig]
    path: Path


# pylint: disable-next=too-many-branches,too-many-statements
def load_config(path: str | os.PathLike | None = None) -> Config:
    config_path = Path(path or os.environ.get("OBSIDIAN_MCP_CONFIG", DEFAULT_CONFIG_PATH))
    # is_file() rather than exists() — Path("") resolves to cwd which exists but
    # isn't readable as a YAML file; a directory path would also pass exists().
    if not config_path.is_file():
        if config_path.exists():
            raise RuntimeError(
                f"Config path {config_path} is not a regular file. "
                "Set OBSIDIAN_MCP_CONFIG to a YAML file path."
            )
        raise RuntimeError(
            f"Config file not found: {config_path}. " "Set OBSIDIAN_MCP_CONFIG or create the file."
        )
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Config file {config_path} is not valid YAML: {exc}") from exc
    except OSError as exc:
        # PermissionError, IsADirectoryError on weird race, decoding errors, …
        raise RuntimeError(f"Could not read config file {config_path}: {exc}") from exc

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
        # Strip surrounding brackets if the user wrote an IPv6 literal in URL
        # form (e.g. "[::1]"). Canonical storage is unbracketed so
        # ipaddress.ip_address() can parse it for loopback detection.
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
            if not host:
                raise RuntimeError(f"Vault '{name}' host must not be empty brackets.")
        _validate_host(name, host)

        port = entry.get("port")
        # bool is a subclass of int in Python, so isinstance(True, int) is True.
        # YAML `port: true` would otherwise pass and be treated as port 1.
        if isinstance(port, bool) or not isinstance(port, int) or port <= 0 or port > 65535:
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
