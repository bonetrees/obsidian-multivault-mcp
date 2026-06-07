"""Tool: patch_note — surgical edit by heading, block reference, or frontmatter key."""

from fastmcp import Context

from ..server import get_client, mcp
from ..validation_types import (
    PatchOperation,
    PatchTargetType,
    VaultFilePath,
    VaultName,
)


@mcp.tool(
    annotations={"readOnlyHint": False, "openWorldHint": False},
    tags={"obsidian", "write"},
)
async def patch_note(
    ctx: Context,
    vault: VaultName,
    path: VaultFilePath,
    operation: PatchOperation,
    target_type: PatchTargetType,
    target: str,
    content: str,
) -> dict:
    """Surgically edit a note by targeting a specific section.

    Prefer this over `write_note` for edits — it preserves the rest of
    the document.

    **Target syntax depends on `target_type`:**

    - `target_type="heading"` — `target` is the **full heading path from
      the document root** using `::` as separator. For a note structured
      as `# Report` → `## Findings` → `### Critical`, the target is
      `"Report::Findings::Critical"`. A bare child heading like
      `"Critical"` will fail with `invalid-target`. Always read the note
      first if you are unsure of the structure.
    - `target_type="block-reference"` — `target` is the block ID (the
      `^block-id` at the end of a block, without the `^`).
    - `target_type="frontmatter-key"` — `target` is the YAML key name
      (e.g. `status`, `date`, `title`). **Scalar-valued keys only** —
      the `content` you supply must be a scalar (string, number,
      boolean, date). Array- or object-valued keys (e.g. `tags`) are
      not supported by this tool; content that parses as a JSON
      array/object is rejected with a clear error.

    **Operations:**

    - `append` — add content after the targeted section. **Include a
      leading `\\n` in `content`** — the API does not add newlines
      automatically.
    - `prepend` — add content before the targeted section.
    - `replace` — overwrite the targeted section's content entirely.
      **Caution for headings:** replacing a parent heading overwrites the
      entire section *including all child subheadings and their bodies*
      (everything down to the next same-or-higher-level heading). To edit
      only a parent's intro text without touching children, target the
      deepest specific heading instead.
    """
    client = get_client(ctx, vault)
    await client.patch_note(
        path=path,
        operation=operation,
        target_type=target_type,
        target=target,
        content=content,
    )
    return {
        "vault": vault,
        "path": path,
        "status": "patched",
        "operation": operation,
        "target_type": target_type,
        "target": target,
    }
