"""Shared test fixtures: tool auto-import, mock vault clients, in-process MCP client."""

from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx
import pytest

# Importing the tools package runs pkgutil auto-discovery in its __init__.py,
# which fires every @mcp.tool() decoration and registers them on the
# singleton FastMCP instance. Same path production code takes via __main__.py.
import obsidian_multivault_mcp.tools  # noqa: F401
from obsidian_multivault_mcp import server as server_module
from obsidian_multivault_mcp.client import ObsidianVaultClient
from obsidian_multivault_mcp.config import Config, VaultConfig


def make_mock_vault_client(name: str, handler) -> ObsidianVaultClient:
    """Build an ObsidianVaultClient wired to a MockTransport with the given handler."""
    transport = httpx.MockTransport(handler)
    return ObsidianVaultClient(
        name=name,
        base_url="https://127.0.0.1:27124",
        api_key="test-key",
        verify_ssl=False,
        transport=transport,
    )


def _test_config(vault_names) -> Config:
    return Config(
        vaults={
            name: VaultConfig(
                name=name,
                scheme="https",
                host="127.0.0.1",
                port=27124,
                api_key="test-key",
            )
            for name in vault_names
        },
        path=Path("/test/config.yaml"),
    )


@pytest.fixture
def vault_handlers():
    """Override per-test by parametrizing or redefining the fixture.

    Maps vault name → httpx.MockTransport handler. The default is empty
    (no configured vaults) so tests must opt in explicitly.
    """
    return {}


@pytest.fixture
async def mcp_client(vault_handlers, monkeypatch):
    """FastMCP in-process Client wired to a server whose lifespan injects
    ObsidianVaultClient instances backed by httpx.MockTransport.
    """
    from fastmcp import Client

    @asynccontextmanager
    async def fake_lifespan(_server):
        clients: dict[str, ObsidianVaultClient] = {}
        async with AsyncExitStack() as stack:
            for name, handler in vault_handlers.items():
                vault_client = make_mock_vault_client(name, handler)
                await stack.enter_async_context(vault_client)
                clients[name] = vault_client
            yield {
                "clients": clients,
                "config": _test_config(list(vault_handlers)),
            }

    monkeypatch.setattr(server_module.mcp, "_lifespan", fake_lifespan)

    async with Client(server_module.mcp) as client:
        yield client
