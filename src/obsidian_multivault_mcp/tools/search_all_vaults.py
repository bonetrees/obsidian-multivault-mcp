"""Tool: search_all_vaults — fan out the same query across every configured vault."""

import asyncio

from fastmcp import Context

from ..logging_config import setup_logging
from ..server import get_all_clients, mcp
from ..validation_types import ClampedContextLength, SearchType
from .search_vault import parse_jsonlogic_query, run_search

logger = setup_logging("obsidian-multivault-mcp.tools.search_all_vaults")


async def _one_vault(name, client, search_type, query, context_length) -> tuple[str, dict]:
    try:
        results, warning = await run_search(client, search_type, query, context_length)
        return name, {
            "status": "ok",
            "results": results,
            "total_results": len(results),
            "warning": warning,
            "error": None,
        }
    # Cooperative cancellation (request abort, server shutdown) must propagate
    # so the surrounding gather() / lifespan can unwind. Don't convert it into
    # a per-vault "error" result. CancelledError is a BaseException in
    # Python 3.8+ so the broad except below wouldn't actually catch it, but
    # the explicit re-raise documents the intent and protects against future
    # regressions (e.g. if asyncio ever moves CancelledError back under
    # Exception, or if a contributor adds a `except BaseException`).
    except asyncio.CancelledError:  # pylint: disable=try-except-raise
        raise
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Per-vault isolation: never let one vault's failure kill the whole fan-out.
        # Catch broadly (not just ToolError) so unexpected runtime errors stay scoped
        # to the vault that produced them.
        logger.warning("Vault %r search failed", name, exc_info=True)
        return name, {
            "status": "error",
            "results": [],
            "total_results": 0,
            "warning": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": False},
    tags={"obsidian", "search"},
)
async def search_all_vaults(
    ctx: Context,
    query: str,
    search_type: SearchType = "text",
    context_length: ClampedContextLength = 100,
) -> dict:
    """Run the same search against every configured vault in parallel.

    Useful when the user asks about a topic without specifying a vault.
    Results are grouped by vault (never merged) so you can see which
    vault each match came from.

    - Individual vault failures do not fail the whole call — each vault's
      entry under `results_by_vault` has its own `status` (`"ok"` or
      `"error"`) and `error` message.
    - Dataview fallback is **per-vault**: if vault A has Dataview but
      vault B does not, vault A runs DQL while vault B silently falls
      back to text search, with its own `warning` set.

    Tip: call `list_vaults` first if you need to understand vault
    structure before searching, especially when picking a `search_type`.
    """
    # Validate user-input shape ONCE, before fan-out. A malformed jsonlogic
    # query otherwise gets caught by per-vault isolation in _one_vault and
    # surfaces as N identical {"status": "error"} entries — confusing
    # because it looks like every vault failed independently. Pre-parsing
    # makes the failure mode match search_vault: a single self-correcting
    # ToolError. The per-vault path re-parses cheaply during fan-out.
    if search_type == "jsonlogic":
        parse_jsonlogic_query(query)  # raises ToolError on bad input

    clients = get_all_clients(ctx)
    if not clients:
        return {
            "query": query,
            "search_type": search_type,
            "results_by_vault": {},
            "total_results_all": 0,
        }
    tasks = [
        _one_vault(name, client, search_type, query, context_length)
        for name, client in clients.items()
    ]
    pairs = await asyncio.gather(*tasks)
    results_by_vault = dict(pairs)
    total = sum(entry["total_results"] for entry in results_by_vault.values())
    return {
        "query": query,
        "search_type": search_type,
        "results_by_vault": results_by_vault,
        "total_results_all": total,
    }
