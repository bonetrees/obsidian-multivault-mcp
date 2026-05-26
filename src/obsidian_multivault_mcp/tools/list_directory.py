"""Tool: list_directory — list files and folders at a path within a vault."""

from fastmcp import Context

from ..server import get_client, mcp
from ..validation_types import VaultName, VaultPath


def curate_directory_listing(vault: str, path: str, raw_files: list[str]) -> dict:
    files: list[str] = []
    folders: list[str] = []
    for entry in raw_files:
        if entry.endswith("/"):
            folders.append(entry.rstrip("/"))
        else:
            files.append(entry)
    return {
        "vault": vault,
        "path": path,
        "files": sorted(files),
        "folders": sorted(folders),
    }


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"obsidian", "navigation"},
)
async def list_directory(ctx: Context, vault: VaultName, path: VaultPath = "") -> dict:
    """List immediate files and subdirectories at a path within a vault.

    Pass `path=""` (or omit) to list the vault root. Returns only direct
    children (not recursive). Combine with `list_vaults` to understand
    overall structure, or with `read_note` once you've narrowed down a
    specific file.
    """
    client = get_client(ctx, vault)
    raw_files = await client.list_directory(path)
    return curate_directory_listing(vault, path, raw_files)
