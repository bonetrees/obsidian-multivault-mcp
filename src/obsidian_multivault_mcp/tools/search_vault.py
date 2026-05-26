"""Tool: search_vault — text, JsonLogic, or Dataview DQL search in one vault."""

import json

from fastmcp import Context
from fastmcp.exceptions import ToolError

from ..server import get_client, mcp
from ..validation_types import (
    ClampedContextLength,
    SearchType,
    VaultName,
)
from ._helpers import curate_simple_match, curate_structured_match


def parse_jsonlogic_query(query: str) -> dict:
    """Parse a JsonLogic query string and validate it's a JSON object.

    Raises ToolError with a self-correcting message on either failure mode.
    Exposed (no underscore) so search_all_vaults can call this eagerly
    before fan-out — otherwise a malformed user-input query would be
    swallowed by per-vault isolation and surface as N identical error
    entries instead of a single ToolError.
    """
    try:
        parsed = json.loads(query)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"search_type='jsonlogic' requires `query` to be a valid JSON string "
            f"(got error: {exc.msg} at column {exc.colno}). "
            'Example: \'{"in": ["#tag", {"var": "tags"}]}\'.'
        ) from exc
    # JsonLogic expressions are always JSON objects. `[]`, `true`, `"foo"`
    # would parse cleanly but produce a vague upstream 4xx — give the
    # caller a clear self-correcting message instead.
    if not isinstance(parsed, dict):
        raise ToolError(
            f"search_type='jsonlogic' requires `query` to parse to a JSON object, "
            f"got {type(parsed).__name__}. "
            'Example: \'{"in": ["#tag", {"var": "tags"}]}\'.'
        )
    return parsed


async def run_search(
    client, search_type: str, query: str, context_length: int
) -> tuple[list[dict], str | None]:
    """Dispatch one search against a single vault client. Returns
    (curated_results, optional_warning)."""
    if search_type == "text":
        raw = await client.search_simple(query, context_length)
        return [curate_simple_match(item) for item in raw], None
    if search_type == "jsonlogic":
        parsed = parse_jsonlogic_query(query)
        raw = await client.search_jsonlogic(parsed)
        return [curate_structured_match(item) for item in raw], None
    if search_type == "dataview":
        raw, warning = await client.search_dataview(query, context_length)
        if warning is None:
            return [curate_structured_match(item) for item in raw], None
        # fallback path returned search_simple-shaped results
        return [curate_simple_match(item) for item in raw], warning
    raise ToolError(f"Unknown search_type: {search_type!r}.")


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"obsidian", "search"},
)
async def search_vault(
    ctx: Context,
    vault: VaultName,
    query: str,
    search_type: SearchType = "text",
    context_length: ClampedContextLength = 100,
) -> dict:
    """Search a single vault using text, JsonLogic, or Dataview DQL.

    - `search_type="text"` (default) — simple full-text search. Always
      available. `context_length` controls characters of surrounding
      context per match (clamped to 20..500).
    - `search_type="jsonlogic"` — structured query against frontmatter,
      tags, and path. `query` must be a JSON string holding a JsonLogic
      expression, e.g. `'{"in": ["#project", {"var": "tags"}]}'`.
    - `search_type="dataview"` — Dataview DQL query (only TABLE queries
      are supported by the plugin). If the Dataview plugin is not
      installed in the target vault, this silently falls back to text
      search and the response includes a `warning` string.

    For cross-vault search, use `search_all_vaults`. Keep queries
    specific — broad searches in large vaults can be slow.
    """
    client = get_client(ctx, vault)
    results, warning = await run_search(client, search_type, query, context_length)
    return {
        "vault": vault,
        "query": query,
        "search_type": search_type,
        "results": results,
        "total_results": len(results),
        "warning": warning,
    }
