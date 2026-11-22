"""Tool: patch_note — surgical edit by heading, block reference, or frontmatter key."""

from typing import Annotated

from fastmcp import Context
from pydantic import Field, StrictBool, StrictInt

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

# Claude Code truncates a tool's description at 2KB (the plugin hit this in
# obsidian-local-rest-api#295), so the docstring carries only the rules a
# caller must not miss, most critical first, and per-parameter detail lives
# in these Field descriptions, which aren't truncated. A test pins the
# docstring under the limit.


@mcp.tool(
    # destructiveHint: operation="delete" removes content outright (and
    # replace overwrites it), so this is no longer a purely additive tool.
    annotations={"readOnlyHint": False, "openWorldHint": False, "destructiveHint": True},
    tags={"obsidian", "write"},
)
# Every local but the result is a tool parameter, and each one is part of
# the MCP schema, so there is nothing to fold away.
async def patch_note(  # pylint: disable=too-many-locals
    ctx: Context,
    vault: Annotated[VaultName, Field(description="Vault name, as listed by list_vaults.")],
    path: Annotated[
        VaultFilePath,
        Field(description="Note path relative to the vault root, e.g. 'projects/plan.md'."),
    ],
    operation: Annotated[
        PatchOperation,
        Field(
            description=(
                "'append' adds after the target's content and 'prepend' before it; on a "
                "heading with child headings, append lands after the last child. "
                "'replace' overwrites it; 'delete' removes the target (see delete_scope). "
                "'insert_after' / 'insert_before' (heading targets only) add a NEW "
                "section at the target's own level: after its whole section, children "
                "included, or before its heading line. Their content must open with "
                "'# Title', since '#' there means a sibling of the target. The title "
                "must be unique under the target's parent, because a path only reaches "
                "the first of a repeated name: one the parent already has is refused "
                "(which also makes a retry safe). For recurring entries, add a date or "
                "number, e.g. '# 2026-11-22 Update'. "
                "append/prepend/replace/insert need 'content' (or 'value' for "
                "frontmatter); delete takes neither. Append adds no newline: start "
                "content with '\\n'."
            )
        ),
    ],
    target_type: Annotated[
        PatchTargetType,
        Field(
            description="What 'target' names: a heading, a block reference or a frontmatter key."
        ),
    ],
    target: Annotated[
        str | HeadingSegments,
        Field(
            description=(
                "heading: the FULL heading path from the document root, joined with '::'. "
                "For '# Report' > '## Findings' > '### Critical' that is "
                "'Report::Findings::Critical'; a bare 'Critical' fails. If a heading's own "
                "text contains '::', pass a list of heading texts instead, e.g. "
                "['Report', 'Q1::Q2 summary']. block-reference: the block ID without the "
                "'^'. frontmatter-key: the key name, e.g. 'status' or 'tags'."
            )
        ),
    ],
    content: Annotated[
        str | None,
        Field(
            description=(
                "The text to write. Heading targets: heading levels in content are "
                "RELATIVE to the target, so '#' is a direct child and '##' a grandchild "
                "(under '### Critical', '# Details' becomes '#### Details'); for "
                "insert_after/insert_before, '#' is a sibling. Never copy the note's "
                "absolute '#' count: content whose shallowest heading has 2+ '#'s, or "
                "a heading that would land past h6, is refused with nothing written "
                "(see allow_deep_headings). Frontmatter targets: always written as a "
                "YAML string, so '1.0', 'true' and 'null' stay strings; content that "
                "looks like a JSON list or object is refused, so use 'value'. With "
                "'within', spliced literally into that one block."
            )
        ),
    ] = None,
    if_match: Annotated[
        ETag | None,
        Field(
            description=(
                "The 'etag' from read_note (or this tool's previous result). Makes the "
                "write conditional: if the note changed since, nothing is written and the "
                "call fails with 'Precondition failed'. Then re-read, re-apply your change "
                "and retry with the new etag. Strongly recommended whenever your content "
                "came from an earlier read, and for every delete."
            )
        ),
    ] = None,
    value: Annotated[
        FrontmatterValue | None,
        Field(
            description=(
                "Frontmatter-key targets only, instead of 'content': any JSON except null, "
                "written as that type. Use it for lists, objects, numbers and booleans, "
                "e.g. ['project', 'urgent'] or {'owner': 'sim'} or 2. Pass the value "
                "itself, not a string containing it. append/prepend merge: two lists "
                "concatenate (duplicates kept), two objects shallow-merge (new keys win "
                "on append, existing keys on prepend), two strings concatenate. Mixed "
                "types are refused: use replace to change a value's type."
            )
        ),
    ] = None,
    create_if_missing: Annotated[
        StrictBool,
        Field(
            description=(
                "Frontmatter-key targets only. A key that doesn't exist fails unless "
                "this is true, which creates it (and the frontmatter block, if the note "
                "has none)."
            )
        ),
    ] = False,
    delete_scope: Annotated[
        DeleteScope | None,
        Field(
            description=(
                "operation='delete' only. 'all' (default) removes the whole target: a "
                "heading with its body and every child subheading, a block with its '^id', "
                "a frontmatter key (the last key removes the frontmatter block). "
                "'content' empties it but keeps the anchor: the heading line stays (body "
                "and child subheadings still go), the block's '^id' stays, a key is set "
                "to null. 'marker' removes only the anchor: the heading line goes, its "
                "body joins the section above and its children may move up a level; for "
                "a block only the '^id' goes. Not valid for frontmatter."
            )
        ),
    ] = None,
    within: Annotated[
        StrictInt | None,
        Field(
            description=(
                "Heading targets, append/prepend/replace only. Edits ONE top-level block "
                "(paragraph, list, table…) in the text above the heading's first child "
                "heading: 0 is the first, -1 the last. Children are never touched. "
                "replace swaps the block; append/prepend splice onto its end/start, so "
                "append with '\\n- new item' extends a list. Use this to edit a parent "
                "heading's intro. An out-of-range index fails, naming how many blocks "
                "there are."
            )
        ),
    ] = None,
    replace_children: Annotated[
        StrictBool,
        Field(
            description=(
                "Heading replace only, without 'within'. A heading replace overwrites "
                "everything beneath the heading, child headings included, so it is "
                "refused when the heading has children. Set true only to really replace "
                "the whole section, children and all."
            )
        ),
    ] = False,
    allow_deep_headings: Annotated[
        StrictBool,
        Field(
            description=(
                "Heading targets with content, without 'within'. Content whose "
                "shallowest heading has 2+ '#'s looks copied from the note's absolute "
                "levels, and a heading past h6 stops being a heading, so both are "
                "refused. Set true only when that deeper nesting is really intended."
            )
        ),
    ] = False,
) -> dict:
    """Surgically edit one section of a note: a heading, a block reference
    or a frontmatter key. Prefer this over `write_note`, because the rest of
    the note is preserved.

    Rules that matter most (each parameter's description has the details):

    - A heading `target` is the **full path from the document root**,
      joined with `::`: `"Report::Findings::Critical"`, never just
      `"Critical"`. Read the note first if unsure of its structure.
    - Headings inside `content` are **relative to the target**: `#` is a
      direct child, `##` a grandchild. Under `### Critical`, write
      `# Details` to get `#### Details`. Never copy the note's absolute
      `#` count: content whose top heading has 2+ `#`s, or that would go
      past h6, is **refused**.
    - To add a **sibling** section (a new log or decision entry), use
      `insert_after` / `insert_before` on the neighbouring heading, with
      content opening `# Title`. `append` only ever adds children, after
      the last existing child.
    - A heading `replace` overwrites everything beneath the heading,
      **child headings included**, so it is **refused** when the heading
      has children. To edit only the text above the first child, pass
      `within` (a block index). To change one child, target it directly.
      To really replace it all, pass `replace_children=True`.
    - `append` adds no newline: start `content` with `\\n`.
    - Frontmatter: `content` is always written as a string. Use `value`
      for lists, objects, numbers and booleans.
    - Pass `if_match` (the `etag` from `read_note`) so you edit what you
      read. If the note changed, nothing is written.

    Returns `status`, the note's new `etag`, and `warnings`: advisory
    `{code, message}` notes from the plugin on a patch that **was
    applied**. A failed or refused patch raises.
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
        allow_deep_headings=allow_deep_headings,
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
