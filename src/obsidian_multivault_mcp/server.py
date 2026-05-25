"""FastMCP server instance, lifespan, and client accessors."""

from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncIterator

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from .client import ObsidianVaultClient
from .config import load_config
from .logging_config import setup_logging

logger = setup_logging("obsidian-multivault-mcp.server")


@asynccontextmanager
async def obsidian_lifespan(_server: FastMCP) -> AsyncIterator[dict]:
    config = load_config()
    clients: dict[str, ObsidianVaultClient] = {}
    async with AsyncExitStack() as stack:
        for name, vault_cfg in config.vaults.items():
            client = ObsidianVaultClient(
                name=name,
                base_url=vault_cfg.base_url,
                api_key=vault_cfg.api_key,
                verify_ssl=vault_cfg.verify_ssl,
            )
            await stack.enter_async_context(client)
            clients[name] = client
            # __aenter__ only constructs the httpx.AsyncClient; it does not
            # verify connectivity. Actual reachability is checked lazily by
            # tools that call get_status() (e.g. list_vaults).
            logger.info(
                "Initialized client for vault %s at %s:%s",
                name,
                vault_cfg.host,
                vault_cfg.port,
            )

        yield {"clients": clients, "config": config}


mcp = FastMCP(name="obsidian-multivault-mcp", lifespan=obsidian_lifespan)


def get_client(ctx: Context, vault: str) -> ObsidianVaultClient:
    clients: dict[str, ObsidianVaultClient] = ctx.request_context.lifespan_context["clients"]
    if vault not in clients:
        available = ", ".join(sorted(clients.keys())) or "(none configured)"
        raise ToolError(f"Unknown vault '{vault}'. Available vaults: {available}.")
    return clients[vault]


def get_all_clients(ctx: Context) -> dict[str, ObsidianVaultClient]:
    return ctx.request_context.lifespan_context["clients"]
