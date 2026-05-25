"""Tool: list_vaults — discover configured vaults and their Index.md content."""

import asyncio

from fastmcp import Context
from fastmcp.exceptions import ToolError

from ..client import NotFound
from ..logging_config import setup_logging
from ..server import get_all_clients, mcp
from ._helpers import strip_frontmatter

logger = setup_logging("obsidian-multivault-mcp.tools.list_vaults")

_INDEX_FILENAME = "Index.md"


async def _fetch_one_vault(name: str, client) -> dict:
    is_online = await client.get_status()
    if not is_online:
        return {"name": name, "status": "unreachable", "index": None}
    try:
        raw = await client.read_note(_INDEX_FILENAME)
    except NotFound:
        # Index.md missing is the common case and is not an error worth
        # surfacing — the vault is still online.
        return {"name": name, "status": "online", "index": None}
    except ToolError as exc:
        # Other errors (auth failure, method-not-allowed, transport, …) get
        # logged and reported as unreachable so the vault isn't quietly marked
        # online while the LLM can't actually use it.
        logger.warning("Vault %r status check passed but Index.md fetch failed: %s", name, exc)
        return {"name": name, "status": "unreachable", "index": None}
    body = strip_frontmatter(raw.get("content", "") or "")
    return {"name": name, "status": "online", "index": body or None}


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"obsidian", "discovery"},
)
async def list_vaults(ctx: Context) -> dict:
    """List all configured Obsidian vaults with their Index.md content.

    Call this FIRST when the user hasn't specified which vault to target.
    The returned `vaults` array gives you the available vault names (use
    these as the `vault` parameter on other tools) and the Index.md
    content describing each vault's structure.

    Each vault should maintain an Index.md at its root following this
    schema, which you can read and also author/maintain:

        # {Vault Name}

        {One-paragraph description of what this vault contains and its
        primary purpose.}

        ## Folders

        ### {folder-name}
        {What this folder contains, when to look here, and any naming
        conventions used.}

        ### {another-folder}
        {Description.}

        ## Key Files

        - `{path/to/file.md}` — {What this file is and when to reference
          it}
        - `{path/to/another.md}` — {Description}

    `status: "online"` means the vault both passed the health check and
    is actually usable for reads (`Index.md` returned 200 or was absent).
    `status: "unreachable"` covers everything else — Obsidian closed,
    plugin disabled, wrong port, authentication failure, transport
    errors — all of which surface here so the LLM doesn't waste a tool
    call on a vault it can't read from. A vault that's online but has
    no `Index.md` still shows `status: "online"` with `index: None`.

    Filename is strict — only `Index.md` (capital I) is read.
    """
    clients = get_all_clients(ctx)
    if not clients:
        return {"vaults": []}
    results = await asyncio.gather(
        *[_fetch_one_vault(name, client) for name, client in clients.items()]
    )
    return {"vaults": list(results)}
