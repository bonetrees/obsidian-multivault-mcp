date: '2026-05-25'

# Obsidian MCP Server

MCP server providing multi-vault Obsidian access through the obsidian-local-rest-api plugin's REST API. Wraps HTTPS endpoints with httpx. 9 tools for vault discovery, CRUD, search, and surgical patching. Streamable-http default transport, port 8100.

## Commands

- `poetry install` — install dependencies
- `poetry run python -m obsidian_multivault_mcp` — run server (streamable-http, default port 8100)
- `poetry run python -m obsidian_multivault_mcp --transport sse --port 8100` — SSE transport
- `poetry run python -m obsidian_multivault_mcp --transport stdio` — stdio (Claude Desktop)
- `poetry run python -m obsidian_multivault_mcp --config /path/to/config.yaml` — custom config path
- `poetry run black src/ tests/` — format code
- `poetry run pylint src/obsidian_multivault_mcp/` — lint code
- `poetry run pytest tests/ -v` — run tests

## Architecture

```
src/obsidian_multivault_mcp/
├── __init__.py          # Version metadata
├── __main__.py          # CLI: argparse, load_dotenv, --transport, --config, security warning
├── server.py            # FastMCP instance, lifespan, get_client(ctx, vault), get_all_clients(ctx)
├── client.py            # ObsidianVaultClient: httpx async, per-vault, all REST API methods
├── config.py            # YAML config loader, VaultConfig dataclass, API key resolution
├── validation_types.py  # VaultName, VaultPath, PatchOperation, PatchTargetType, SearchType, ClampedContextLength
├── logging_config.py    # Centralized stdlib logging
└── tools/
    ├── __init__.py      # pkgutil auto-discovery: imports every non-_-prefixed module → @mcp.tool() runs
    ├── _helpers.py      # Shared curators: strip_frontmatter, epoch_ms_to_iso, curate_*_match
    ├── list_vaults.py   # Discovery: returns vault names + Index.md content
    ├── list_directory.py
    ├── read_note.py
    ├── write_note.py
    ├── append_note.py
    ├── patch_note.py    # Surgical heading/block/frontmatter targeting
    ├── delete_note.py
    ├── search_vault.py  # Text, JsonLogic, Dataview DQL (with fallback)
    └── search_all_vaults.py  # Parallel fan-out across all vaults
tests/
├── __init__.py
├── conftest.py          # Mock client fixtures, pkgutil auto-discovery
├── test_validation_types.py
├── test_curators.py
├── test_client.py       # httpx.MockTransport
└── test_tools.py        # FastMCP Client(mcp) integration
```

## Key Patterns

- **Env var loading.** `python-dotenv` with `load_dotenv(override=True)` in `__main__.py` before any imports that read env vars. API keys read via `os.environ[config.api_key_env]` during config loading. Do NOT use `python-decouple`.

- **Multi-vault config.** YAML file path set via `OBSIDIAN_MCP_CONFIG` env var. Each vault entry has `scheme`, `host`, `port`, `api_key_env`. Keys never stored in YAML — `api_key_env` names the env var holding the key. Config loaded and validated at lifespan startup.

- **HTTPS with self-signed certs.** Plugin defaults to HTTPS:27124 with self-signed cert. `VaultConfig.verify_ssl` derives from host via the `_is_loopback_host` helper in `config.py`: HTTPS to loopback (any 127.0.0.0/8 IPv4, `::1`, or case-insensitive `localhost`) → `verify=False` (the plugin's self-signed cert is expected and the loopback interface isn't MITM-able); HTTPS to anything else → `verify=True` so a misconfigured remote vault can't silently fall back to an unverified TLS session. Loopback detection uses `ipaddress.ip_address(host).is_loopback` for IP literals so the whole 127.0.0.0/8 range (e.g. Debian's `127.0.1.1`) works, not just `127.0.0.1`. The same helper is reused by `__main__.py`'s non-loopback bind warning. Config `scheme` field defaults to `"https"`.

- **Client per vault.** One `ObsidianVaultClient` instance per configured vault, stored in `dict[str, ObsidianVaultClient]`. Created in lifespan via `AsyncExitStack`. Base URL: `f"{scheme}://{host}:{port}"`.

- **Lifespan sequence.** `load_config()` → resolve API keys from env → create `ObsidianVaultClient` per vault → `stack.enter_async_context(client)` → store in dict → `yield {"clients": clients, "config": config}`.

- **get_client(ctx, vault).** Extracts clients dict from `ctx.request_context.lifespan_context["clients"]`, validates vault name, returns the matching client. Raises `ToolError` with available vault names if unknown. `get_all_clients(ctx)` returns the full dict for fan-out operations.

- **API key strategy.** Fail-fast at lifespan startup. Missing or empty API key env vars raise `RuntimeError` immediately.

- **Timeout enforcement.** `httpx.Timeout(30.0, connect=10.0)` on client. Configurable via `OBSIDIAN_MCP_TIMEOUT`. Timeout exceptions caught and raised as `ToolError` with vault name and operation. **Health check uses a 3 s timeout** (constant `HEALTH_CHECK_TIMEOUT` on the client, not user-configurable) so `list_vaults` doesn't hang when a vault is offline; `get_status()` never raises, it returns `False` on any failure.

- **Client API pattern.** Per-endpoint methods on `ObsidianVaultClient`: `read_note()`, `write_note()`, `append_note()`, `patch_note()`, `delete_note()`, `list_directory()`, `search_simple()`, `search_jsonlogic()`, `search_dataview()`, `get_status()`.

- **Tool naming.** Python function names = MCP names. No `name=` override, no gateway prefix.

- **Tool tags and annotations.** All tools: `tags={"obsidian", "category"}`. Read tools: `readOnlyHint=True`, `openWorldHint=False`. Write tools: `readOnlyHint=False`. `delete_note`: `destructiveHint=True`.

- **Error handling.** The client does *not* use `httpx.raise_for_status` / `HTTPStatusError`. Instead, every response goes through `_raise_for_status()` which parses the API error body `{"message": str, "errorCode": int}` and raises `ToolError` (or its `NotFound` subclass for HTTP 404). Transport-layer exceptions are caught in `_request()`: `ConnectError`, `ConnectTimeout`, generic `TimeoutException`, and a `RequestError` catch-all all map to `ToolError` with vault + operation context — nothing httpx-shaped leaks past the client. Never return `{"error": ...}` dicts.

- **Curator: frontmatter stripping.** Raw API `content` includes YAML frontmatter fences. Curator regex `^---\n(?:.*?\n)?---(?:\n|\Z)` (with `re.DOTALL`) strips the leading block including both fences. The `\n` prefix on the closing fence requires it to be on its own line — without it, a YAML value containing literal dashes (e.g. `description: "test --- here"`) could prematurely terminate the block. The optional `(?:.*?\n)?` allows empty frontmatter (`---\n---\n`). The `\Z` alternative accepts notes that end exactly at the closing fence with no trailing newline. Malformed/unterminated blocks pass through unchanged.

- **Curator: directory listing.** API returns single `files` array. Entries ending with `/` are folders (strip trailing slash). Others are files.

- **Curator: search normalization.** Simple search returns `{filename, score, matches[].context}`. JsonLogic returns `{filename, result}`. Curator normalizes both: `filename` → `path`, extract `context` strings from matches array, scores defaulted to `None` for JsonLogic. The raw `match` object has positional offsets only (start/end) — no matched-text field; just expose `context` strings.

- **Curator: timestamps.** `stat.ctime` and `stat.mtime` are epoch milliseconds. Convert to ISO 8601 strings.

- **Empty results.** Search returning `[]` is valid, not an error. Vault with no `Index.md` returns `index: None` in `list_vaults`, not an error.

- **Dataview fallback.** When Dataview not installed, API returns HTTP 400 with `errorCode: 40070`. `search_dataview()` returns `tuple[list[dict], str | None]` — second element is the warning string when fallback happened (`"Dataview not available, fell back to text search"`), otherwise `None`. **Fallback is per-vault** — in `search_all_vaults`, if vault A has Dataview and vault B doesn't, each handles fallback independently inside its own client call.

## Known Issues / Gotchas

### Exception types

HTTP status codes are inspected by `_raise_for_status()` directly (no `HTTPStatusError`).
Transport errors are caught in `_request()` and mapped to `ToolError`.

| Condition | Where | Response |
|---|---|---|
| HTTP 401 | `_raise_for_status` | `ToolError`: auth failed, check API key |
| HTTP 404 | `_raise_for_status` | `NotFound` (subclass of `ToolError`): not found at path |
| HTTP 405 | `_raise_for_status` | `ToolError`: operation not supported |
| HTTP 400 + `errorCode: 40070` | `search_dataview` | Silent fallback to text search; warning string returned |
| HTTP 400 + `errorCode: 40080` | `_raise_for_status` | `ToolError`: invalid PATCH target, with `::` syntax hint |
| `httpx.ConnectError` | `_request` | `ToolError`: "Cannot connect…, is Obsidian running?" |
| `httpx.ConnectTimeout` | `_request` | `ToolError`: timed out after `CONNECT_TIMEOUT` (10 s) |
| `httpx.TimeoutException` (read/write/pool) | `_request` | `ToolError`: timed out after `self._timeout` (default 30 s) |
| `httpx.RequestError` (anything else: protocol, read, …) | `_request` | `ToolError`: "Transport error… {type}: {exc}" |

### PATCH heading targets

Target must be the **full heading path** from document root using `::` delimiter. `"Report::Findings::Critical"` — not `"Critical"`. A bare child heading returns `errorCode: 40080` (invalid-target). Appended content has no automatic newlines — prepend `\n` to content for append operations.

### Search endpoints differ by type

- Text: `POST /search/simple/?query=...&contextLength=...` (URL params)
- JsonLogic: `POST /search/` with `Content-Type: application/vnd.olrapi.jsonlogic+json` (JSON body)
- Dataview: `POST /search/` with `Content-Type: application/vnd.olrapi.dataview.dql+txt` (text body)

These are different endpoints with different request formats — not the same endpoint with different bodies.

### PUT returns 204 for both create and update

No way to distinguish via status code. Use `"written"` as the unified status value.

### Newlines not automatic on append operations

Both `append_note` (POST) and `patch_note` with `operation: "append"` do not add newlines automatically. Without a leading `\n` in the content, appended text runs directly onto the last line of the existing content. Prepend `\n` to content for both operations.

### Mock strategy

All client tests use `httpx.MockTransport`. Each REST API method gets its own mock handler matching the method + path pattern. For multi-vault tests, create separate mock transports per vault.

## Manual Smoke Checklist

The automated test suite mocks every HTTP exchange. A handful of assumptions are only verifiable against a real `obsidian-local-rest-api` plugin instance — run this checklist against a live vault before tagging a release or after any change to `client.py` / search dispatch:

1. **Bring up a vault.** Open Obsidian, install the Local REST API plugin, generate an API key, point a `obsidian-multivault-mcp-config.yaml` at the vault and the env var.
2. **Start the server** in stdio for direct shell testing or streamable-http for MCP client testing:
   ```bash
   poetry run python -m obsidian_multivault_mcp --transport stdio
   ```
3. **`list_vaults`** — confirm vault appears with `status: "online"` and `index` populated from `Index.md` (or `None` if the file isn't there, not unreachable).
4. **`read_note`** on a note with frontmatter — confirm `content` has the `---` block stripped, `frontmatter` is a parsed dict, `tags` includes both frontmatter and inline `#tag` references, `stat.created` / `stat.modified` are ISO 8601 strings. This exercises the `Accept: application/vnd.olrapi.note+json` header which the test suite can't validate end-to-end.
5. **`patch_note`** with `target_type="heading"`, `target="Parent::Child"` — confirm the right heading is targeted. This exercises the URL-encoded `Target` header round-trip (`Parent%3A%3AChild` → plugin URL-decodes → matches), which is one of the more fragile contract points.
6. **`write_note`** then **`append_note`** then **`delete_note`** — full destructive round-trip against a throwaway file. Confirm `delete_note` actually requires `confirm: true` (Pydantic schema should still accept `confirm: false` and the runtime gate should reject).
7. **`search_vault`** with `search_type="text"` — confirm `results[].context` strings are populated.
8. **`search_vault`** with `search_type="jsonlogic"` and a real expression like `{"in": ["#project", {"var": "tags"}]}` — confirm matches.
9. **`search_vault`** with `search_type="dataview"` in a vault **with** Dataview installed — confirm `warning` is `None`. In a vault **without** Dataview — confirm `warning` is `"Dataview not available, fell back to text search"` and results come from the text search fallback.
10. **`search_all_vaults`** with two vaults configured, one offline — confirm the online one returns results and the offline one shows `status: "error"` with a connection error, not a whole-tool failure.

If any of these diverges from the docs, the mock-based tests are lying. Open an issue with the actual response shape and update the relevant curator / handler.

## Adding a Tool

1. Create `src/obsidian_multivault_mcp/tools/new_tool.py`
2. Import `mcp`, `get_client` from `..server` and types from `..validation_types`
3. Write `curate_new_result(raw: dict) -> dict` — handle timestamp conversion, field renames, frontmatter stripping if applicable. Reuse helpers from `tools/_helpers.py` (`strip_frontmatter`, `epoch_ms_to_iso`, `curate_simple_match`, `curate_structured_match`).
4. Write `@mcp.tool(annotations={...}, tags={...})` async function with `Annotated` params and an LLM-facing docstring. Let `ToolError` propagate from the client layer.
5. Add a client method to `client.py` — per-endpoint pattern.
6. **No edits to `tools/__init__.py` needed** — `pkgutil.iter_modules()` auto-discovers any non-`_`-prefixed module at import time.
7. Add tests: curator in `test_curators.py`, integration in `test_tools.py` using the `mcp_client` + `vault_handlers` fixtures from `conftest.py`.
8. Run `poetry run pytest tests/ && poetry run black src/ tests/ && poetry run pylint src/obsidian_multivault_mcp/`

## Code Style

- Python 3.13, type hints on all functions
- Black + Pylint
- LLM-facing docstrings on all tools (written for tool discovery, not developers)
- Logging: `%s` lazy formatting, stderr, `setup_logging("obsidian-multivault-mcp.module")`
- `ToolError` for all failures, never `{"error": ...}` dicts
- One tool per file in `tools/`

## Environment

| Variable | Default | Description |
|---|---|---|
| `OBSIDIAN_MCP_CONFIG` | `./obsidian-multivault-mcp-config.yaml` | Path to the YAML config file |
| `OBSIDIAN_MCP_HOST` | `127.0.0.1` | Bind host for HTTP transports |
| `OBSIDIAN_MCP_PORT` | `8100` | Bind port for HTTP transports |
| `OBSIDIAN_MCP_LOG_LEVEL` | `INFO` | Logging level |
| `OBSIDIAN_MCP_TIMEOUT` | `30` | Default HTTP timeout in seconds |
| `OBSIDIAN_{VAULT}_API_KEY` | _(required per vault)_ | API key for each vault (env var name configured in YAML) |

Loaded via `python-dotenv` with `load_dotenv(override=True)` in `__main__.py`.

## Dependencies

**Runtime:**
```toml
[project]
dependencies = [
    "fastmcp (>=3.0.0,<4.0.0)",
    "httpx (>=0.28.0,<1.0.0)",
    "pydantic (>=2.11.0,<3.0.0)",
    "python-dotenv (>=1.0.0,<2.0.0)",
    "pyyaml (>=6.0,<7.0)",
]
```

**Dev:**
```toml
[tool.poetry.group.dev.dependencies]
pytest = "^9.0"
pytest-asyncio = "^1.0"
black = "^25.1"
pylint = "^3.3"
```