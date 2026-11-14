"""Tool: patch_note — surgical edit by heading, block reference, or frontmatter key."""

from fastmcp import Context
from pydantic import StrictBool

from ..server import get_client, mcp
from ..validation_types import (
    ETag,
    FrontmatterValue,
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
    content: str | None = None,
    if_match: ETag | None = None,
    value: FrontmatterValue | None = None,
    create_if_missing: StrictBool = False,
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
      (e.g. `status`, `date`, `tags`). Give the new value as **one of**:
      - `content` (a string): always written as a YAML *string* —
        `content="1.0"` writes the string `"1.0"`, not the number; the
        same goes for `"true"` and `"null"`. A string that looks like a
        JSON list/object is rejected, because it's ambiguous.
      - `value` (any JSON except null): written as that JSON type. Use
        this for lists, objects, numbers and booleans:
        `value=["project", "urgent"]` writes a real YAML list,
        `value={"owner": "sim"}` a map, `value=2` a number. Pass the
        list itself, not a string containing it.

      A key that doesn't exist yet fails unless `create_if_missing=True`
      (which also creates the frontmatter block if the note has none).
      `value` and `create_if_missing` are for frontmatter-key targets
      only.

    **Operations** (`content` is required for heading and block targets):

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

    **Frontmatter merge rules** (`append`/`prepend` on a frontmatter
    key): two lists are concatenated (duplicates kept); two objects are
    shallow-merged, with the new keys winning on `append` and the
    existing keys winning on `prepend`; two strings are concatenated.
    Mixed types (e.g. a string onto a list) are refused — use `replace`
    to change a value's type.

    The response carries a `warnings` list. It is normally empty; a
    non-empty entry means the patch **was applied** but something about
    the result is worth knowing (e.g. a created heading nested deeper
    than Markdown's six levels, which leaves it unaddressable by later
    `heading` patches). Warnings are not failures — a failed patch
    raises instead.

    Pass `if_match` (the `etag` from `read_note`, or from this tool's
    previous result) to make the write conditional: if the note has
    changed since that version, nothing is written and the call fails
    with "Precondition failed". Then read the note again, re-apply your
    change to the current content, and retry with the new `etag`.
    Strongly recommended whenever your content was derived from an
    earlier read. The result's `etag` is the note's new version.
    """
    client = get_client(ctx, vault)
    warnings, etag = await client.patch_note(
        path=path,
        operation=operation,
        target_type=target_type,
        target=target,
        content=content,
        if_match=if_match,
        value=value,
        create_if_missing=create_if_missing,
    )
    return {
        "vault": vault,
        "path": path,
        "status": "patched",
        "operation": operation,
        "target_type": target_type,
        "target": target,
        "warnings": warnings,
        "etag": etag,
    }
