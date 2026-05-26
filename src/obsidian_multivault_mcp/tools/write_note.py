"""Tool: write_note — create or fully overwrite a note in a vault."""

from fastmcp import Context

from ..server import get_client, mcp
from ..validation_types import VaultFilePath, VaultName


@mcp.tool(
    annotations={"readOnlyHint": False, "openWorldHint": False},
    tags={"obsidian", "write"},
)
async def write_note(ctx: Context, vault: VaultName, path: VaultFilePath, content: str) -> dict:
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

    The API does not distinguish create vs. overwrite — both succeed with
    `status: "written"`.
    """
    client = get_client(ctx, vault)
    await client.write_note(path, content)
    return {"vault": vault, "path": path, "status": "written"}
