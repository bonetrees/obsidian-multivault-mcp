"""Tool: read_note — fetch a note's content, frontmatter, tags, and stat."""

from fastmcp import Context

from ..server import get_client, mcp
from ..validation_types import VaultName, VaultPath
from ._helpers import epoch_ms_to_iso, strip_frontmatter


def curate_note(vault: str, path: str, raw: dict) -> dict:
    raw_content = raw.get("content", "") or ""
    raw_frontmatter = raw.get("frontmatter")
    stat = raw.get("stat") or {}
    return {
        "vault": vault,
        "path": path,
        "content": strip_frontmatter(raw_content),
        "frontmatter": raw_frontmatter if raw_frontmatter else None,
        "tags": list(raw.get("tags") or []),
        "stat": {
            "created": epoch_ms_to_iso(stat.get("ctime")),
            "modified": epoch_ms_to_iso(stat.get("mtime")),
            "size": stat.get("size"),
        },
    }


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"obsidian", "read"},
)
async def read_note(ctx: Context, vault: VaultName, path: VaultPath) -> dict:
    """Read a single note's content, frontmatter, tags, and file metadata.

    Returns the note's body markdown (frontmatter fences stripped — the
    parsed frontmatter is returned separately as a dict). `tags` includes
    both frontmatter tags and inline `#tag` references. `stat.created` and
    `stat.modified` are ISO 8601 UTC timestamps.

    For large notes consider whether you really need the full body — for
    targeted reads, use `search_vault` first to find a specific section,
    or `patch_note` to edit without reading.
    """
    client = get_client(ctx, vault)
    raw = await client.read_note(path)
    return curate_note(vault, path, raw)
