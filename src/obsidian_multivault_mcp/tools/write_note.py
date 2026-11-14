"""Tool: write_note — create or fully overwrite a note in a vault."""

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import StrictBool

from ..client import PreconditionFailed
from ..server import get_client, mcp
from ..validation_types import ETag, VaultFilePath, VaultName


@mcp.tool(
    annotations={"readOnlyHint": False, "openWorldHint": False},
    tags={"obsidian", "write"},
)
async def write_note(
    ctx: Context,
    vault: VaultName,
    path: VaultFilePath,
    content: str,
    if_match: ETag | None = None,
    create_only: StrictBool = False,
) -> dict:
    """Create a new note or fully overwrite an existing one.

    Intermediate directories are created automatically. **This overwrites
    the entire file** — use `append_note` to add to an existing note, or
    `patch_note` for surgical edits to a specific heading, block, or
    frontmatter key.

    Include frontmatter as a YAML block between `---` fences at the top
    of `content` if needed:

        ---
        title: My Note
        tags: [project]
        ---
        Body text here.

    Remember to update the vault's `Index.md` if you create notes in a
    new folder that other LLM calls will need to discover.

    **Overwriting safely.** Pass `if_match` (the `etag` from `read_note`, or from this tool's
    previous result) to make the write conditional: if the note has
    changed since that version, nothing is written and the call fails
    with "Precondition failed". Then read the note again, re-apply your
    change to the current content, and retry with the new `etag`.
    Strongly recommended whenever your content was derived from an
    earlier read. The result's `etag` is the note's new version.

    **Creating safely.** Set `create_only=True` to write only if no note
    exists at `path` yet; an existing note is left untouched and the call
    fails. Success then reports `status: "created"`. Without it, create
    and overwrite are indistinguishable and both report
    `status: "written"`. `create_only` and `if_match` are mutually
    exclusive.
    """
    if create_only and if_match is not None:
        raise ToolError(
            "write_note: pass either create_only=True (note must not exist) or "
            "if_match (note must be at that version), not both."
        )
    client = get_client(ctx, vault)
    try:
        etag = await client.write_note(
            path, content, if_match=if_match, if_none_match="*" if create_only else None
        )
    except PreconditionFailed as exc:
        if not create_only:
            raise
        raise ToolError(
            f"write_note with create_only=True: a note already exists at '{path}' in "
            f"vault '{vault}', so nothing was written. Read it first, or drop "
            "create_only to overwrite it."
        ) from exc
    status = "created" if create_only else "written"
    return {"vault": vault, "path": path, "status": status, "etag": etag}
