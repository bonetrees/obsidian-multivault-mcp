"""Tool: append_note — add content to the end of an existing note."""

from fastmcp import Context

from ..server import get_client, mcp
from ..validation_types import ETag, VaultFilePath, VaultName


@mcp.tool(
    annotations={"readOnlyHint": False, "openWorldHint": False},
    tags={"obsidian", "write"},
)
async def append_note(
    ctx: Context,
    vault: VaultName,
    path: VaultFilePath,
    content: str,
    if_match: ETag | None = None,
) -> dict:
    """Append content to the end of an existing note.

    **Include a leading `\\n` in `content`** — the API does not add
    newlines automatically, so without one the appended text runs
    directly onto the last line of the existing note.

    The note must already exist (the API does not create on POST). To
    create a new note, use `write_note`. Useful for adding entries to
    logs, journals, or running lists.

    Pass `if_match` (the `etag` from `read_note`, or from this tool's
    previous result) to make the write conditional: if the note has
    changed since that version, nothing is written and the call fails
    with "Precondition failed". Then read the note again, re-apply your
    change to the current content, and retry with the new `etag`.
    Strongly recommended whenever your content was derived from an
    earlier read. The result's `etag` is the note's new version.
    """
    client = get_client(ctx, vault)
    etag = await client.append_note(path, content, if_match=if_match)
    return {"vault": vault, "path": path, "status": "appended", "etag": etag}
