"""Tool: patch_note — surgical edit by heading, block reference, or frontmatter key."""

from fastmcp import Context
from pydantic import StrictBool, StrictInt

from ..server import get_client, mcp
from ..validation_types import (
    DeleteScope,
    ETag,
    FrontmatterValue,
    HeadingSegments,
    PatchOperation,
    PatchTargetType,
    VaultFilePath,
    VaultName,
)


@mcp.tool(
    # destructiveHint: operation="delete" removes content outright (and
    # replace overwrites it), so this is no longer a purely additive tool.
    annotations={"readOnlyHint": False, "openWorldHint": False, "destructiveHint": True},
    tags={"obsidian", "write"},
)
async def patch_note(
    ctx: Context,
    vault: VaultName,
    path: VaultFilePath,
    operation: PatchOperation,
    target_type: PatchTargetType,
    target: str | HeadingSegments,
    content: str | None = None,
    if_match: ETag | None = None,
    value: FrontmatterValue | None = None,
    create_if_missing: StrictBool = False,
    delete_scope: DeleteScope | None = None,
    within: StrictInt | None = None,
    replace_children: StrictBool = False,
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
      first if you are unsure of the structure. If a heading's own text
      contains `::`, pass the path as a **list** of heading texts instead,
      e.g. `["Report", "Q1::Q2 summary"]`.

      **Headings inside `content` are relative to the target, not
      absolute.** A leading `#` becomes a *direct child* of the target,
      `##` a grandchild, and so on. Under `### Critical`, write
      `# Details` to get `#### Details`; writing `### Details` would
      produce `###### Details`, three levels below the target. Never
      copy the absolute `#` count from the note.
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

    **Operations** (`append`/`prepend`/`replace` need `content`, or `value`
    for frontmatter; `delete` takes neither):

    - `append` — add content after the targeted section. **Include a
      leading `\\n` in `content`** — the API does not add newlines
      automatically.
    - `prepend` — add content before the targeted section.
    - `replace` — overwrite the targeted section's content entirely. For a
      heading, that is everything beneath it **including all child
      subheadings and their bodies**. So a heading `replace` on a heading
      that has children is **refused** (nothing is written) unless you pass
      `replace_children=True`. Usually you want one of these instead:
      - To edit only the text above the first child heading, pass
        `within`: the index of one top-level block there (a paragraph,
        list, table…; `0` = first, `-1` = last). The edit applies to that
        block only, and children are never touched. With `within`,
        `replace` swaps the block, and `append`/`prepend` splice onto its
        end/start, so `append` with `"\\n- new item"` extends a list.
        Out-of-range indexes fail, naming how many blocks there are.
      - To change one child, target it directly (`"Doc::Child"`).
    - `delete` — remove the target. How much goes is set by `delete_scope`:
      - `"all"` (default): the whole target. A heading goes **together
        with its body and every child subheading** beneath it; a block
        goes with its `^id`; a frontmatter key is removed (removing the
        last key removes the frontmatter block).
      - `"content"`: empty it but keep the anchor. The heading line stays
        (its body **and child subheadings** are still removed); the
        block's `^id` stays; a frontmatter key stays, set to null.
      - `"marker"`: remove only the anchor. The heading line goes and its
        body joins the section above, and its child subheadings may move
        up a level (e.g. `###` becomes `##`). For a block, only its `^id`
        goes. Not valid for frontmatter.
      Read the note first and pass its `etag` as `if_match`, so you
      delete what you looked at rather than whatever is there now.

    **Frontmatter merge rules** (`append`/`prepend` on a frontmatter
    key): two lists are concatenated (duplicates kept); two objects are
    shallow-merged, with the new keys winning on `append` and the
    existing keys winning on `prepend`; two strings are concatenated.
    Mixed types (e.g. a string onto a list) are refused — use `replace`
    to change a value's type.

    The response carries a `warnings` list of `{code, message}` entries.
    It is normally empty; a non-empty entry means the patch **was
    applied** but something about the result is worth knowing:

    - `heading-depth-overflow` (from the plugin): a heading in `content`
      landed deeper than Markdown's six levels, which leaves it
      unaddressable by later `heading` patches.
    - `heading-level-not-relative` (from this server): the shallowest
      heading in `content` has two or more `#`s, so it probably copied
      the note's absolute level and landed too deep. Check the note and
      fix the heading.

    Warnings are not failures — a failed patch raises instead.

    Pass `if_match` (the `etag` from `read_note`, or from this tool's
    previous result) to make the write conditional: if the note has
    changed since that version, nothing is written and the call fails
    with "Precondition failed". Then read the note again, re-apply your
    change to the current content, and retry with the new `etag`.
    Strongly recommended whenever your content was derived from an
    earlier read. The result's `etag` is the note's new version.
    """
    warnings, etag = await get_client(ctx, vault).patch_note(
        path=path,
        operation=operation,
        target_type=target_type,
        target=target,
        content=content,
        if_match=if_match,
        value=value,
        create_if_missing=create_if_missing,
        delete_scope=delete_scope,
        within=within,
        replace_children=replace_children,
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
