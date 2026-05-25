"""FastMCP server instance, lifespan, and client accessors."""

from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from .config import load_config
from .logging_config import setup_logging

if TYPE_CHECKING:
    from .client import ObsidianVaultClient

logger = setup_logging("obsidian-multivault-mcp.server")


@asynccontextmanager
async def obsidian_lifespan(_server: FastMCP) -> AsyncIterator[dict]:
    config = load_config()
    clients: dict[str, "ObsidianVaultClient"] = {}
    async with AsyncExitStack() as stack:
        try:
            # Imported lazily so Phase 1 scaffold loads even before client.py exists.
            from .client import ObsidianVaultClient  # pylint: disable=import-outside-toplevel
        except ImportError:
            logger.warning("client.py not yet available — running without vault clients.")
            ObsidianVaultClient = None  # type: ignore[assignment]

        if ObsidianVaultClient is not None:
            for name, vault_cfg in config.vaults.items():
                client = ObsidianVaultClient(
                    name=name,
                    base_url=vault_cfg.base_url,
                    api_key=vault_cfg.api_key,
                    verify_ssl=vault_cfg.verify_ssl,
                )
                await stack.enter_async_context(client)
                clients[name] = client
                logger.info(
                    "Connected to vault %s at %s:%s",
                    name,
                    vault_cfg.host,
                    vault_cfg.port,
                )

        yield {"clients": clients, "config": config}


mcp = FastMCP(name="obsidian-multivault-mcp", lifespan=obsidian_lifespan)


def get_client(ctx: Context, vault: str) -> "ObsidianVaultClient":
    clients: dict[str, "ObsidianVaultClient"] = ctx.request_context.lifespan_context["clients"]
    if vault not in clients:
        available = ", ".join(sorted(clients.keys())) or "(none configured)"
        raise ToolError(f"Unknown vault '{vault}'. Available vaults: {available}.")
    return clients[vault]


def get_all_clients(ctx: Context) -> dict[str, "ObsidianVaultClient"]:
    return ctx.request_context.lifespan_context["clients"]
