"""Tool: append_note — add content to the end of an existing note."""

from fastmcp import Context

from ..server import get_client, mcp
from ..validation_types import VaultFilePath, VaultName


@mcp.tool(
    annotations={"readOnlyHint": False, "openWorldHint": False},
    tags={"obsidian", "write"},
)
async def append_note(ctx: Context, vault: VaultName, path: VaultFilePath, content: str) -> dict:
    """Append content to the end of an existing note.

    **Include a leading `\\n` in `content`** — the API does not add
    newlines automatically, so without one the appended text runs
    directly onto the last line of the existing note.

    The note must already exist (the API does not create on POST). To
    create a new note, use `write_note`. Useful for adding entries to
    logs, journals, or running lists.
    """
    client = get_client(ctx, vault)
    await client.append_note(path, content)
    return {"vault": vault, "path": path, "status": "appended"}
