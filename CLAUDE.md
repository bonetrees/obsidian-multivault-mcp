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

- **HTTPS with self-signed certs.** Plugin defaults to HTTPS:27124 with self-signed cert. `VaultConfig.verify_ssl` derives from host: HTTPS to loopback (`127.0.0.1`, `localhost`, `::1`) → `verify=False` (the plugin's self-signed cert is expected and the loopback interface isn't MITM-able); HTTPS to anything else → `verify=True` so a misconfigured remote vault can't silently fall back to an unverified TLS session. Config `scheme` field defaults to `"https"`. The `LOOPBACK_HOSTS` set lives in `config.py`.

- **Client per vault.** One `ObsidianVaultClient` instance per configured vault, stored in `dict[str, ObsidianVaultClient]`. Created in lifespan via `AsyncExitStack`. Base URL: `f"{scheme}://{host}:{port}"`.

- **Lifespan sequence.** `load_config()` → resolve API keys from env → create `ObsidianVaultClient` per vault → `stack.enter_async_context(client)` → store in dict → `yield {"clients": clients, "config": config}`.

- **get_client(ctx, vault).** Extracts clients dict from `ctx.request_context.lifespan_context["clients"]`, validates vault name, returns the matching client. Raises `ToolError` with available vault names if unknown. `get_all_clients(ctx)` returns the full dict for fan-out operations.

- **API key strategy.** Fail-fast at lifespan startup. Missing or empty API key env vars raise `RuntimeError` immediately.

- **Timeout enforcement.** `httpx.Timeout(30.0, connect=10.0)` on client. Configurable via `OBSIDIAN_MCP_TIMEOUT`. Timeout exceptions caught and raised as `ToolError` with vault name and operation. **Health check uses a 3 s timeout** (constant `HEALTH_CHECK_TIMEOUT` on the client, not user-configurable) so `list_vaults` doesn't hang when a vault is offline; `get_status()` never raises, it returns `False` on any failure.

- **Client API pattern.** Per-endpoint methods on `ObsidianVaultClient`: `read_note()`, `write_note()`, `append_note()`, `patch_note()`, `delete_note()`, `list_directory()`, `search_simple()`, `search_jsonlogic()`, `search_dataview()`, `get_status()`.

- **Tool naming.** Python function names = MCP names. No `name=` override, no gateway prefix.

- **Tool tags and annotations.** All tools: `tags={"obsidian", "category"}`. Read tools: `readOnlyHint=True`, `openWorldHint=False`. Write tools: `readOnlyHint=False`. `delete_note`: `destructiveHint=True`.

- **Error handling.** Catch `httpx.HTTPStatusError`, `httpx.ConnectError`, `httpx.TimeoutException` → raise `ToolError`. Never return `{"error": ...}` dicts. Parse API error body `{"message": str, "errorCode": int}` for actionable messages.

- **Curator: frontmatter stripping.** Raw API `content` includes YAML frontmatter fences. Curator regex `^---\n.*?---\n` (with `re.DOTALL`) strips the leading block including both fences; empty frontmatter (`---\n---\n`) is also stripped. Malformed/unterminated blocks pass through unchanged.

- **Curator: directory listing.** API returns single `files` array. Entries ending with `/` are folders (strip trailing slash). Others are files.

- **Curator: search normalization.** Simple search returns `{filename, score, matches[].context}`. JsonLogic returns `{filename, result}`. Curator normalizes both: `filename` → `path`, extract `context` strings from matches array, scores defaulted to `None` for JsonLogic. The raw `match` object has positional offsets only (start/end) — no matched-text field; just expose `context` strings.

- **Curator: timestamps.** `stat.ctime` and `stat.mtime` are epoch milliseconds. Convert to ISO 8601 strings.

- **Empty results.** Search returning `[]` is valid, not an error. Vault with no `Index.md` returns `index: None` in `list_vaults`, not an error.

- **Dataview fallback.** When Dataview not installed, API returns HTTP 400 with `errorCode: 40070`. `search_dataview()` returns `tuple[list[dict], str | None]` — second element is the warning string when fallback happened (`"Dataview not available, fell back to text search"`), otherwise `None`. **Fallback is per-vault** — in `search_all_vaults`, if vault A has Dataview and vault B doesn't, each handles fallback independently inside its own client call.

## Known Issues / Gotchas

### Exception types

| Exception | Catch? | Response |
|---|---|---|
| `httpx.HTTPStatusError` (401) | Yes | `ToolError`: auth failed, check API key |
| `httpx.HTTPStatusError` (404) | Yes | `ToolError`: not found |
| `httpx.HTTPStatusError` (400, code 40070) | Yes | Dataview fallback to simple search |
| `httpx.HTTPStatusError` (400, code 40080) | Yes | `ToolError`: invalid PATCH target |
| `httpx.ConnectError` | Yes | `ToolError`: Obsidian not running |
| `httpx.TimeoutException` | Yes | `ToolError`: timeout with value |
| Other `httpx` errors | Let propagate | FastMCP catches |

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