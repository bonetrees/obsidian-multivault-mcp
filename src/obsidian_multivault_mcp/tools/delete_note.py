"""Tool: delete_note — destructive note removal, gated by an explicit confirm flag."""

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import StrictBool

from ..server import get_client, mcp
from ..validation_types import VaultFilePath, VaultName


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "openWorldHint": False,
        "destructiveHint": True,
    },
    tags={"obsidian", "write"},
)
async def delete_note(
    ctx: Context, vault: VaultName, path: VaultFilePath, confirm: StrictBool = False
) -> dict:
    """Delete a note from a vault. Destructive — confirm with the user first.

    `confirm` must be `True` to proceed; this is a safety gate to prevent
    accidental deletion. Always check in with the user before calling.

    Only deletes individual notes (files), not folders. Consider whether
    the vault's `Index.md` needs updating after deletion if other tool
    calls rely on its contents.
    """
    if confirm is not True:
        raise ToolError(
            "delete_note requires confirm=True. This is a destructive operation; "
            "confirm with the user before retrying."
        )
    client = get_client(ctx, vault)
    await client.delete_note(path)
    return {"vault": vault, "path": path, "status": "deleted"}
