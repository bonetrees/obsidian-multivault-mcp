date: '2026-05-26'

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
├── __main__.py          # CLI: argparse, --transport, --config; load_dotenv + setup_logging run inside main()
├── server.py            # FastMCP instance, LifespanContext TypedDict, lifespan, get_client(ctx, vault), get_all_clients(ctx)
├── client.py            # ObsidianVaultClient: httpx async, per-vault, all REST API methods; NotFound subclass
├── config.py            # YAML config loader, VaultConfig dataclass, _is_loopback_host, API key resolution
├── validation_types.py  # VaultName, VaultPath, VaultFilePath, PatchOperation, PatchTargetType, SearchType, ClampedContextLength
├── logging_config.py    # Centralised stdlib logging, scoped to the package logger
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
    ├── search_vault.py  # Text and JsonLogic (parse_jsonlogic_query helper)
    └── search_all_vaults.py  # Parallel fan-out; eager jsonlogic input validation before fan-out
skills/
└── obsidian-multivault/SKILL.md  # Agent skill: how to use the tools well. Keep it in sync with tool behaviour
tests/
├── __init__.py
├── conftest.py          # Mock client fixtures, FastMCP Client + mcp._lifespan swap
├── test_validation_types.py
├── test_config.py       # YAML loader, _is_loopback_host, base_url IPv6 bracketing
├── test_logging_config.py
├── test_main.py         # CLI helpers (_env_int, _parse_args, _strip_host_brackets) and main() integration
├── test_curators.py
├── test_client.py       # httpx.MockTransport
├── plugin_fakes.py      # Stateful plugin fakes: FakeVaultNote (ETag / If-Match), FakeFrontmatterNote (v2 frontmatter merge rules), FakeHeadingNote (subtree replace, sibling insert, Within, document map)
└── test_tools.py        # FastMCP Client(mcp) integration
```

## Key Patterns

- **Env var loading.** `python-dotenv` with `load_dotenv(override=True)` runs *inside* `main()` (not at module import) so importing `obsidian_multivault_mcp.__main__` is side-effect-free for tests and library consumers. It still runs before any further imports that read `OBSIDIAN_*` env vars (config / client / logging are all imported lazily after dotenv). `--config` is applied to `os.environ` *after* `load_dotenv` so the CLI flag takes precedence over `.env`. API keys read via `os.environ[config.api_key_env]` during config loading. Do NOT use `python-decouple`.

- **Multi-vault config.** YAML file path set via `OBSIDIAN_MCP_CONFIG` env var. Each vault entry has `scheme`, `host`, `port`, `api_key_env`. Keys never stored in YAML — `api_key_env` names the env var holding the key. Config loaded and validated at lifespan startup.

- **HTTPS with self-signed certs.** Plugin defaults to HTTPS:27124 with self-signed cert. `VaultConfig.verify_ssl` derives from host via the `_is_loopback_host` helper in `config.py`: HTTPS to loopback (any 127.0.0.0/8 IPv4, `::1`, or case-insensitive `localhost`) → `verify=False` (the plugin's self-signed cert is expected and the loopback interface isn't MITM-able); HTTPS to anything else → `verify=True` so a misconfigured remote vault can't silently fall back to an unverified TLS session. Loopback detection uses `ipaddress.ip_address(host).is_loopback` for IP literals so the whole 127.0.0.0/8 range (e.g. Debian's `127.0.1.1`) works, not just `127.0.0.1`. The same helper is reused by `__main__.py`'s non-loopback bind warning. Config `scheme` field defaults to `"https"`.

- **Client per vault.** One `ObsidianVaultClient` instance per configured vault, stored in `dict[str, ObsidianVaultClient]`. Created in lifespan via `AsyncExitStack`. Base URL: `f"{scheme}://{host}:{port}"`.

- **Lifespan sequence.** `load_config()` → resolve API keys from env → create `ObsidianVaultClient` per vault → `stack.enter_async_context(client)` → store in dict → `yield {"clients": clients, "config": config}`.

- **get_client(ctx, vault).** Extracts clients dict from `ctx.request_context.lifespan_context["clients"]`, validates vault name, returns the matching client. Raises `ToolError` with available vault names if unknown. `get_all_clients(ctx)` returns the full dict for fan-out operations.

- **API key strategy.** Fail-fast at lifespan startup. Missing or empty API key env vars raise `RuntimeError` immediately.

- **Timeout enforcement.** `httpx.Timeout(30.0, connect=10.0)` on client. Configurable via `OBSIDIAN_MCP_TIMEOUT`. Timeout exceptions caught and raised as `ToolError` with vault name and operation. **Health check uses a 3 s timeout** (constant `HEALTH_CHECK_TIMEOUT` on the client, not user-configurable) so `list_vaults` doesn't hang when a vault is offline; `get_status()` never raises, it returns `False` on any failure.

- **Client API pattern.** Per-endpoint methods on `ObsidianVaultClient`: `read_note()`, `write_note()`, `append_note()`, `patch_note()`, `delete_note()`, `list_directory()`, `search_simple()`, `search_jsonlogic()`, `get_status()`.

- **Tool naming.** Python function names = MCP names. No `name=` override, no gateway prefix.

- **Tool tags and annotations.** All tools: `tags={"obsidian", "category"}`. Read tools: `readOnlyHint=True`, `openWorldHint=False`. Write tools: `readOnlyHint=False`. `delete_note`: `destructiveHint=True`.

- **Error handling.** The client does *not* use `httpx.raise_for_status` / `HTTPStatusError`. Instead, every response goes through `_raise_for_status()` which parses the API error body `{"message": str, "errorCode": int}` and raises `ToolError` (or its `NotFound` subclass for HTTP 404). Transport-layer exceptions are caught in `_request()`: `ConnectError`, `ConnectTimeout`, generic `TimeoutException`, and a `RequestError` catch-all all map to `ToolError` with vault + operation context — nothing httpx-shaped leaks past the client. Never return `{"error": ...}` dicts.

- **Curator: frontmatter stripping.** Raw API `content` includes YAML frontmatter fences. Curator regex `^---\r?\n(?:.*?\r?\n)?---(?:\r?\n|\Z)` (with `re.DOTALL`) strips the leading block including both fences. The `\r?\n` at each line break makes the pattern CRLF-tolerant (notes edited on Windows). The `\r?\n` prefix on the closing fence requires it to be on its own line — without it, a YAML value containing literal dashes (e.g. `description: "test --- here"`) could prematurely terminate the block. The optional `(?:.*?\r?\n)?` allows empty frontmatter (`---\n---\n`). The `\Z` alternative accepts notes that end exactly at the closing fence with no trailing newline. Malformed/unterminated blocks pass through unchanged.

- **Curator: directory listing.** API returns single `files` array. Entries ending with `/` are folders (strip trailing slash). Others are files.

- **Curator: search normalization.** Simple search returns `{filename, score, matches[].context}`. JsonLogic returns `{filename, result}`. Curator normalizes both: `filename` → `path`, extract `context` strings from matches array, scores defaulted to `None` for JsonLogic. The raw `match` object has positional offsets only (start/end) — no matched-text field; just expose `context` strings.

- **Curator: timestamps.** `stat.ctime` and `stat.mtime` are epoch milliseconds. Convert to ISO 8601 strings.

- **Empty results.** Search returning `[]` is valid, not an error. Vault with no `Index.md` returns `index: None` in `list_vaults`, not an error.

- **No Dataview search.** `search_type` is `"text"` or `"jsonlogic"` only. obsidian-local-rest-api dropped Dataview DQL search before 4.1.7: 2.0.0 imported the Dataview API, but 4.1.7 through 5.5.2 register a single `POST /search/` handler, JsonLogic. Any other `Content-Type`, the old `application/vnd.olrapi.dataview.dql+txt` included, is `400`/`40012` whether or not Dataview is installed. The server used to send DQL and fall back to text search on `40070`. But in 5.x, `40070` is `InvalidFilterQuery` (a JsonLogic evaluation failure), so that fallback never ran, and every live DQL search failed with `40012` (Asana, 2026-11-22). The option and the search tools' `warning` field (its only producer) were removed. Don't re-add it unless the plugin brings DQL back.

## Known Issues / Gotchas

### Exception types

HTTP status codes are inspected by `_raise_for_status()` directly (no `HTTPStatusError`).
Transport errors are caught in `_request()` and mapped to `ToolError`.

| Condition | Where | Response |
|---|---|---|
| HTTP 401 | `_raise_for_status` | `ToolError`: auth failed, check API key |
| HTTP 404 | `_raise_for_status` | `NotFound` (subclass of `ToolError`): not found at path |
| HTTP 405 | `_raise_for_status` | `ToolError`: operation not supported |
| HTTP 400 + `errorCode: 40070` | `_raise_for_status` (generic) | `ToolError`: the plugin couldn't evaluate a JsonLogic query (`InvalidFilterQuery`). Not "Dataview not installed": see *No Dataview search* |
| HTTP 400 + `errorCode: 40080` | `_raise_for_status` | `ToolError`: invalid PATCH target (+ `hint` when the caller passed one) |
| HTTP 400 + `errorCode: 40082/40083/40084` | `_raise_for_status` | `ToolError`: "PATCH protocol mismatch" — client/plugin disagree on the markdown-patch format. Names the version header we send; check the plugin version |
| HTTP 409 (patch) | `_raise_for_status` (generic) | `ToolError`: content preexists / frontmatter key collision |
| HTTP 404 / `40400` on a `within` PATCH | `_raise_for_status` + `WITHIN_TARGET_HINT` | `NotFound`: the note is missing (bare "Not Found"), the heading didn't match ("could not resolve heading target"), or the index is out of range (names the block count). The hint names all three and carries both the full-path and the block-index advice, so a wrong heading never gets only block-index advice |
| Heading `replace` on a heading with children | `_refuse_child_loss` (client-side) | `ToolError`, nothing sent: names the children and points at `within`, a child target, or `replace_children=True` |
| Heading content whose shallowest heading has 2+ `#`s | `_refuse_absolute_levels` (client-side) | `ToolError`, nothing sent: explains relative levels, points at `insert_after` for siblings and at `allow_deep_headings=True` |
| Heading content that would land past h6 | `_refuse_heading_overflow` (client-side, after a `text/markdown` GET) | `ToolError`, nothing sent: names the heading, its landing level and the target's level |
| `insert_after`/`insert_before` content not opening with `# ` | `_refuse_headless_insert` (client-side) | `ToolError`, nothing sent: shows the expected shape, points at append/prepend for text inside the target |
| A `#` title in insert/append/prepend content that its parent already has (often a retry), or that the content repeats | `_refuse_duplicate_titles` (client-side, after a document-map GET) | `ToolError`, nothing sent: names the parent and title, says a retry needs nothing more, suggests free titles |
| A heading line in `within` content | `_refuse_within_headings` (client-side, no request) | `ToolError`, nothing sent: points at append/prepend for children and insert_after/insert_before for siblings |
| HTTP 404 on a frontmatter PATCH | `_raise_for_status` + `FRONTMATTER_TARGET_HINT` | `NotFound`: the *key* is absent (plugin: "could not resolve frontmatter target"); hint names `create_if_missing=True` |
| HTTP 400 + `errorCode: 40081` | `_raise_for_status` (generic) | `ToolError`: invalid patch instruction, e.g. a payload on a delete, or an operation × scope × target-type combination the plugin doesn't support. `_check_patch_args` refuses the ones we know of before sending |
| HTTP 400 + `errorCode: 40059` | `_raise_for_status` (generic) | `ToolError`: invalid `Target-Scope` value. Unreachable through the tool, which only sends values from `_DELETE_SCOPE_WIRE` |
| HTTP 400 + `errorCode: 40080` "not mergeable" | `_raise_for_status` + `FRONTMATTER_TARGET_HINT` | `ToolError`: append/prepend across mismatched types; hint says to use `replace` |
| HTTP 412 / `errorCode: 41200` | `_raise_for_status` | `PreconditionFailed` (subclass of `ToolError`): note changed since the caller's `if_match` version (or exists, for `create_only`); nothing was written. Never carries a target hint: the version, not the address, is wrong |
| HTTP 400 + `errorCode: 40024` | `_raise_for_status` | `ToolError`: "Malformed etag" — deliberately *not* `PreconditionFailed`, since re-reading and retrying can't fix a mangled tag |
| `httpx.ConnectError` | `_request` | `ToolError`: "Cannot connect…, is Obsidian running?" |
| `httpx.ConnectTimeout` | `_request` | `ToolError`: timed out after `CONNECT_TIMEOUT` (10 s) |
| `httpx.TimeoutException` (read/write/pool) | `_request` | `ToolError`: timed out after `self._timeout` (default 30 s) |
| `httpx.RequestError` (anything else: protocol, read, …) | `_request` | `ToolError`: "Transport error… {type}: {exc}" |

### PATCH speaks markdown-patch v2 (plugin 5.x)

**Tested against `obsidian-local-rest-api` ("Local REST API with MCP") v5.5.2.** The plugin serves two incompatible patch formats and treats header-based targeting as ambiguous between them, so `patch_note` **must** send `Markdown-Patch-Version: 2`. Omit it and every patch fails with `errorCode: 40084` regardless of how correct the rest of the request is — that was the v0.2.x outage.

Wire contract for header targeting, all enforced in `client.py`:

- **`Markdown-Patch-Version: 2` is mandatory.** v1 still works but the plugin advertises `Deprecation: true; sunset-version="6.0"` against it, so v2 is the only forward-compatible choice.
- **Heading targets are percent-encoded JSON arrays**, not delimiter-joined strings: `["Report","Findings"]` → `%5B%22Report%22%2C%22Findings%22%5D`. Block and frontmatter targets stay opaque percent-encoded strings.
- **Never send `Target-Delimiter` or `Trim-Target-Whitespace`.** They are v1-only, and their mere presence under v2 is itself a `40084`. Because `Trim-Target-Whitespace` is unavailable, `_encode_patch_target` does not trim heading segments — the caller's exact heading text is preserved.
- **Body carriers:** a `text/*` body becomes the instruction's `content`; an `application/json` body becomes its `value`. The frontmatter branch's existing `application/json` is the correct carrier, so bodies needed no change. The plugin parses with `express.json({strict: false})`, so a bare JSON scalar (`"published"`) is accepted.
- **Success returns 200 with the patched document as the body**, an `ETag`, and any advisory notices as percent-encoded JSON in a `Markdown-Patch-Warnings` header. `patch_note()` returns those warnings as `list[dict]` (`{code, message}`); the tool surfaces them under `warnings`. They are advisory — the write already happened — so an unparseable header is logged and dropped, never promoted to a failure. The client adds no warnings of its own: heading content it doubts is refused before sending (see *Sibling inserts and heading levels*).

The **tool API is unchanged**: callers still pass the full heading path from the document root with `::` (`"Report::Findings::Critical"`, not `"Critical"`), and the split to a JSON array happens inside the client. A heading `target` may also be a **list** of heading texts (`["Report", "Q1::Q2"]`), sent as the JSON array verbatim. That's the only way to address a heading whose own text contains `::`. Appended content still has no automatic newlines — prepend `\n` for append operations.

Under v2 an unmatched heading comes back as **404** (`TargetNotFoundError`), not `40080`. Since a 404 on PATCH otherwise reads identically to "the note doesn't exist", `patch_note` passes `HEADING_TARGET_HINT` into `_raise_for_status(..., hint=...)` for heading targets only. Don't hardcode that hint in `_raise_for_status` again — it used to fire on every `40080`, including frontmatter-key patches where the `::` advice is meaningless.

### Delete and `Target-Scope`

`patch_note(operation="delete")` sends **no body and no Content-Type**. Any payload on a delete is `40081`, and the plugin only treats a body as absent when it has no bytes and no `Transfer-Encoding`. It sends `Target-Scope` from `delete_scope` on deletes, and `markerAndContent` on sibling inserts, and **on nothing else**, so append/prepend/replace keep their exact wire shape. Read from the 5.5.2 source and verified live:

| `delete_scope` (wire) | heading | block | frontmatter key |
|---|---|---|---|
| `"all"` (`markerAndContent`), **tool default** | heading line + body + **all child subheadings** | the whole block + `^id` | key removed. The last key removes the `---` block |
| `"content"` (`content`), **plugin default** | body + **all child subheadings**; heading line kept | text removed, `^id` kept | key kept, set to `null` |
| `"marker"` (`marker`) | heading line only; body joins the section above, children promoted a level | `^id` only | invalid (40081), refused client-side |

- The tool's default is `"all"`, not the plugin's `content`, because "delete the key" should remove it rather than leave `key: null` behind. **Both** heading scopes except `marker` take the child subheadings with them.
- **Root hazard:** a heading `Target` of `[]` addresses the *document root*, so a delete there wipes the body. A `::` string can never split to `[]`, but a list target could. `HeadingSegments` and `_check_patch_args` both refuse an empty list. Keep both.
- `Create-Target-If-Missing` never applies to delete, so a missing target is always 404.
- The plugin's own HTTP DELETE on a path-element URL returns 405 advising "PATCH with Operation: replace and an empty body". **That advice is wrong under v2:** replace with no payload is 40081. Use `Operation: delete`.

### Heading replace and child headings

Probed live on 5.5.2. A heading's `content` scope (what append/prepend/replace use) is its **whole subtree**, so a `replace` on a parent deletes every child heading and its body, with `200` and no warning. Under an h1 that's usually the whole document. The plugin owns this and will never refuse it, so the client does:

- **A heading `replace` without `within` is refused when the target has children**, unless `replace_children=True`. `_refuse_child_loss` fetches the v2 document map (`GET` with `Accept: application/vnd.olrapi.document-map+json`, a nested heading tree like `{"Doc": {"Child": {}}}`) and walks it by segment. A heading missing from the map is let through, so the PATCH reports the miss with its usual 404 and hint. Repeated headings' map keys carry a non-printable suffix (U+FC750, then `chr(0xF6440 + n)` with n=0 for the second copy; `tests/plugin_fakes.py::repeat_key`), stripped for the error message. The GET is skipped with `within` or `replace_children=True`. There's a small window between the GET and the PATCH. It guards against caller mistakes, not concurrent edits (`if_match` covers those).
- **`within: int` sends the `Within` header**: an index into the heading's intro blocks, the top-level blocks *above its first child heading* (`0` first, `-1` last). It works with header targeting, not only URL targeting. With the default `content` scope, `replace` swaps that block and `append`/`prepend` splice literally onto its end/start (`"\n- item"` extends a list), and children are never touched. Out of range is `404`/`40400` naming the block count, including on a heading with no intro text. Heading targets and append/prepend/replace only; never sent with a `Target-Scope`.
- The heading-level refusals are skipped for `within` edits: they're literal splices, so the relative-level rule doesn't apply. Instead, **`within` content may contain no heading lines at all** (`_refuse_within_headings`, outside code fences). Verified live: `# Sneaky` spliced into an intro landed as a real h1, took over the children below it and left the target empty, with 200 and no warning.

### Sibling inserts and heading levels

Read from the markdown-patch source and probed live on 5.5.2. A heading append or replace can only ever produce **children**: content-scope `append` on a heading with children lands **after its last child**, rebased by the target's level. Adding a *sibling* used to need a whole-note `write_note`.

- **`insert_after` / `insert_before`** are tool-facing operations. On the wire they're `append` / `prepend` with `Target-Scope: markerAndContent` (`_INSERT_WIRE`). The plugin inserts a new section after the target's whole subtree, or before its heading line, rebasing the content by the **parent's** level, so `#` lands at the target's own level. Heading targets only, `content` only, never with `within`.
- **Insert content must open with a `#` heading line** (`_refuse_headless_insert`). Otherwise the text silently joins whichever section precedes it.
- **The plugin leaves no blank line between an inserted section and the next heading**: `new body\n## Next`. That's markdown-patch owning the joint, and a trailing `\n\n` in content is normalised away. It's still structurally a heading, because an ATX heading interrupts a paragraph, so it's cosmetic.
- **The plugin doesn't make inserts idempotent, so the client does.** A retried insert adds a second copy (200, no 409). The `Reject-If-Content-Preexists: true` header doesn't help either, because its check only searches the target's own subtree, and the sibling lands outside it (both verified live). `_refuse_duplicate_titles` therefore refuses a new section whose title its parent already has, using the document map (one GET). That also prevents repeated names, since a path only ever reaches the first. A title that exists under a *different* parent is fine. It checks every `#` heading in the content, against the parent's children and against each other. The parent is the target's parent for the inserts, and **the target itself for `append`/`prepend`**, since a `#` appended under a heading becomes its child. Before that, appending `# Update` to a parent bypassed the check (found in the skill review). `replace` isn't checked, because it swaps the children out.
- **Sibling titles must be unique under a parent: decided, with no override** (Asana, 2026-11-22). The insert descriptions say so and suggest a date or number (`# 2026-11-22 Update`), and the refusal suggests `'<title> (n)'` or a dated title. The alternatives were rejected:
  - *An override flag* would create sections no path can reach, and would switch off the retry protection above.
  - *Occurrence addressing* ("the Nth `Update`") works at the plugin level. Verified live: suffixed document-map keys are accepted as heading-target segments and reach the Nth copy. But the keys are **positional**, so a copy added above shifts them. Callers would have to count copies, pair every write with `if_match`, and spend description space near the 2KB limit. That counting is the same kind that caused the heading-level incidents.
  - Revisit occurrence addressing only if editing existing notes with repeated titles under one parent becomes a real need. It stays cheap: the client would pick the Nth map key whose printable text matches.
- **The blank-line workaround was declined.** A follow-up `within=-1` append of `"\n"` on the new section does restore the blank line, but it's a second, non-atomic write. It also only works when the new section has intro text and no child headings. This is better fixed upstream.

Heading content is checked before sending, for every heading write with `content` except `within` (`_check_heading_patch`). These used to be an advisory `heading-level-not-relative` warning, but that arrived after the bad heading was written, and the repair was a whole-note rewrite:

- **Absolute-looking levels are refused** (`_refuse_absolute_levels`): the shallowest ATX heading outside fences has 2+ `#`s. A `#` count copied from the note is always ≥ 2, since nothing below the root is shallower than h2, while a relative `##` is a level skip that's rarely meant. This needs no request. It deliberately isn't "top level ≤ the target's level", which would refuse a correct `#` under an h1 and miss an absolute child copy.
- **Overflow past h6 is refused** (`_refuse_heading_overflow`): this needs the target's real level, which the document map doesn't carry. So when the content has a heading, `_target_levels` GETs the note as `text/markdown` and walks its ATX headings, skipping frontmatter and fences. A trailing `#` run is only a closing sequence after whitespace (`_CLOSING_HASHES`), so `C#` keeps its `#`, as in the plugin. Stripping it used to make a `C#` path miss and skip the check. A miss (a setext heading, say) skips the check, and the plugin's `heading-depth-overflow` warning stays the backstop.
- **`allow_deep_headings=True`** skips both refusals, and the GET with them. It doesn't skip the headless-insert check.

### Path-element targeting: considered and declined

`/vault/note.md/heading/A/B` targeting was evaluated (Asana follow-up) and **not adopted**. In the 5.5.2 source, header targeting is deprecated for v1 and for GET/PUT/POST, but **PATCH with `Target-Type`/`Target` under an explicit `Markdown-Patch-Version: 2` is documented as non-deprecated**, and the v2 path emits no deprecation signal. Its other selling point, addressing headings that contain `::`, is covered by list targets. Things to know if this is revisited after a plugin release that deprecates PATCH header targeting:
- It's a migration, not an addition: a URL target together with `Target-Type`/`Target` headers is `42200`.
- A path-element PATCH with **no** version header is silently treated as v2.
- `/vault/note.md/heading` with no segments is the document root.
- Extra segments after a block id or frontmatter key are silently ignored.

### Structured frontmatter: `content` vs `value`

`patch_note` takes **exactly one** of `content` or `value` for frontmatter-key targets:

- **`content: str` is always a YAML string.** It is never parsed into a value. `"1.0"`, `"true"` and `"null"` stay strings. **Guard B** (in `_patch_body`) is *parse-to-reject*: if `content` parses as a JSON list or object, the call fails and points the caller at `value`. The parse result is only a yes/no signal and is thrown away. Parse-to-*coerce* would silently turn `"1.0"` into `1.0`. Don't "simplify" it into that.
- **`value: FrontmatterValue` is the caller saying "this is structured".** It's any JSON except null, typed at the tool boundary (`validation_types.FrontmatterValue`, spelled out so the schema isn't an empty `{}`). It's sent as-is, `application/json`, i.e. v2's `value` carrier. The JSON type of the MCP argument *is* the signal, so nothing is sniffed from a string. Null is excluded because `None` means "not provided".
- `value` and `create_if_missing` are frontmatter-only. Both are rejected before any request for heading or block targets.

Plugin semantics, probed live on 5.5.2 and modelled by `tests/plugin_fakes.py::FakeFrontmatterNote`:

- `replace` sets the key to any type.
- `append`/`prepend` merge two lists (concatenated, **duplicates kept**), two objects (shallow; **new keys win on `append`, existing keys win on `prepend`**) or two strings. Any other pairing is `400/40080` "not mergeable".
- A missing key is **404 for every operation** unless `Create-Target-If-Missing: true` (`create_if_missing=True`), which creates the key, and the frontmatter block too if the note has none. That header is v1-era, but v2 still honours it for frontmatter. It is *not* verified for heading/block targets, hence frontmatter-only.
- A date-shaped string (`"2026-12-01"`) is written **unquoted**, but `read_note` still returns it as a string. Obsidian's Properties UI may display it as a date. This predates `value`; it happens via `content` too.

### Search endpoints differ by type

- Text: `POST /search/simple/?query=...&contextLength=...` (URL params)
- JsonLogic: `POST /search/` with `Content-Type: application/vnd.olrapi.jsonlogic+json` (JSON body). The plugin adds `glob` and `regexp` operators. It's the only `/search/` format 5.x accepts.

These are different endpoints with different request formats — not the same endpoint with different bodies.

### PUT returns 204 for both create and update

No way to distinguish via status code, so an unconditional `write_note` reports `"written"` either way. `create_only=True` sends `If-None-Match: *`, which *does* distinguish: success means the note was created (`"created"`), and an existing note comes back as 412, which the tool rewords to "already exists".

### ETags and preconditions (plugin 5.x)

Probed live against 5.5.2. All four write tools take an optional `if_match`, sent as `If-Match`. `read_note` and every write return the note's `etag`.

- **The file ETag is strong and content-derived.** Rewriting identical bytes keeps it; any edit moves it. It is returned on GET/HEAD `text/markdown`, PUT, POST and PATCH.
- **The note+json GET that `read_note` relies on does *not* carry it.** Express stamps a *weak* tag (`W/"…"`) hashed from the JSON response body, and does the same on every error response. The plugin compares `If-Match` strongly, so a weak tag can never match. `_etag()` drops weak tags rather than hand back one that guarantees a 412.
- **`read_note` therefore makes two requests: HEAD (`Accept: text/markdown`) for the tag, *then* the note+json GET.** Don't swap them. With the tag fetched first, a change between the two requests leaves the tag *older* than the content, so a write based on it fails safe with 412. The other order hands back a tag newer than what the caller read, which is exactly the silent clobber this feature exists to prevent. `test_read_note_etag_is_never_newer_than_content` pins this.
- **Preconditions are honoured on PUT, POST, PATCH *and* DELETE.** `*` and comma-separated lists work. `If-Match` against a missing note is 412. An unquoted tag isn't malformed, it just never matches (412); empty or space-separated lists are `40024`.

### Newlines not automatic on append operations

Both `append_note` (POST) and `patch_note` with `operation: "append"` do not add newlines automatically. Without a leading `\n` in the content, appended text runs directly onto the last line of the existing content. Prepend `\n` to content for both operations.

### Mock strategy

All client tests use `httpx.MockTransport`. Each REST API method gets its own mock handler matching the method + path pattern. For multi-vault tests, create separate mock transports per vault.

**A mock that returns `200` for everything is worse than no test.** The patch handlers used to do exactly that, asserting nothing about required headers — so the suite stayed fully green while every live `patch_note` call was failing with `40084`. Where a request has a contract the plugin actually enforces, the mock must enforce it too: `tests/test_client.py::markdown_patch_v2_handler` rejects what the real plugin rejects (missing/invalid version header, a heading `Target` that isn't a JSON array, v1-only headers under v2) and all PATCH success-path tests route through it. `test_contract_handler_rejects_nonconformant_requests` guards the guard, so it can't quietly go permissive. The same goes for preconditions: `tests/plugin_fakes.py::FakeVaultNote` keeps real note state, computes content-derived tags, serves only a weak tag on note+json, and refuses stale / malformed / weak `If-Match` exactly as the plugin does. `TestPreconditionFake` guards it. Likewise `FakeFrontmatterNote` holds real frontmatter and applies the plugin's merge, not-mergeable and missing-key rules, so a list sent as a JSON *string* lands as a string there too. `TestFrontmatterFake` guards it. `FakeHeadingNote` holds a real note and replaces a heading's whole subtree, keys repeated siblings with the plugin's real suffix (`repeat_key`), rebases content headings (by the target's level, or the parent's for `markerAndContent` sibling inserts), applies `Within` to intro blocks only, and serves the document map and the raw markdown, so a client that skipped the child check would lose the children there too. `TestHeadingFake` guards it. `markdown_patch_v2_handler` answers document-map and `text/markdown` GETs itself (empty tree and empty note by default; pass `document_map_factory` / `note_factory`), so record requests *outside* it when a test needs to see those GETs.

## Manual Smoke Checklist

The automated test suite mocks every HTTP exchange. A handful of assumptions are only verifiable against a real `obsidian-local-rest-api` plugin instance — run this checklist against a live vault before tagging a release or after any change to `client.py` / search dispatch:

1. **Bring up a vault.** Open Obsidian, install the Local REST API plugin, generate an API key, point a `obsidian-multivault-mcp-config.yaml` at the vault and the env var.
2. **Start the server** in stdio for direct shell testing or streamable-http for MCP client testing:
   ```bash
   poetry run python -m obsidian_multivault_mcp --transport stdio
   ```
3. **`list_vaults`** — confirm vault appears with `status: "online"` and `index` populated from `Index.md` (or `None` if the file isn't there, not unreachable).
4. **`read_note`** on a note with frontmatter — confirm `content` has the `---` block stripped, `frontmatter` is a parsed dict, `tags` includes both frontmatter and inline `#tag` references, `stat.created` / `stat.modified` are ISO 8601 strings. This exercises the `Accept: application/vnd.olrapi.note+json` header which the test suite can't validate end-to-end.
5. **`patch_note`** with `target_type="heading"`, `target="Parent::Child"` — confirm the right heading is targeted. This exercises the markdown-patch v2 `Target` round-trip (`::` path → JSON array → percent-encoded → plugin decodes and matches structurally), the single most fragile contract point in the client. Then repeat for `target_type="block-reference"` and `target_type="frontmatter-key"`, each with `append` / `prepend` / `replace` — the version header and target encoding are shared by all nine combinations, so a v2 regression can hide in any of them.
   **Then structured frontmatter:** `value=["a","b"]` on `tags` → `read_note` shows a real list. Append/prepend a list and an object, checking which side wins on prepend. `value=2` / `value=true` come back as int / bool, while `content="1.0"`, `"true"` and `"null"` still come back as strings. A list-shaped `content` is refused and pointed at `value`. A missing key 404s with the `create_if_missing` hint, and `create_if_missing=True` creates it, even on a note with no frontmatter block.
   **Then delete:** run each `delete_scope` on a heading with a child (the sibling must survive `"all"`), on a block, and on a frontmatter key. Deleting the last frontmatter key should remove the `---` block, and `marker` on frontmatter should be refused. A heading whose text contains `::` should be unreachable as a string but reachable as a list target, for both append and delete.
   **Then child headings:** on `# Doc` (intro paragraph + list) → `### Child` → `## Sib`, a plain `replace` on `Doc` must be refused with the note untouched. `within=0` replace, `within=-1` append (`"\n- item"`) and `within=1` prepend must edit only the intro, leaving both children. `within=7` must 404 naming the block count, and `replace_children=True` must replace everything.
   **Then sibling inserts and levels:** on `## Decisions` → `### Entry one` (with an h4 child) → `### Entry two`, `insert_after` on `Entry one` with `# Entry new` must land as an h3 after the h4 child, and `insert_before` on `Entry two` must land just above it. Repeating the same `insert_after` must be refused as a duplicate, and so must an `append` of `# Entry one` to `Decisions`. A `within=0` replace whose content is `# Sneaky` must be refused with the note untouched. Appending `### New entry` must be refused with the etag unchanged, as must a headless insert and `## Seven` appended under an h5. With `allow_deep_headings=True` the h7 is written and the plugin's `heading-depth-overflow` warning comes back.
   **Also re-check the plugin version** (`manifest.json` in the vault's `.obsidian/plugins/obsidian-local-rest-api/`). The docs are written against **5.5.2**; a bump is the single likeliest cause of a sudden patch failure, and the plugin's own error text is the best source for what changed.
6. **`write_note`** then **`append_note`** then **`delete_note`** — full destructive round-trip against a throwaway file. Confirm `delete_note` actually requires `confirm: true` (Pydantic schema should still accept `confirm: false` and the runtime gate should reject).
   **Then preconditions:** `read_note` → confirm `etag` equals the plugin's own `ETag` from a HEAD and is not `W/`-prefixed. For each of the four write tools: `read_note`, edit the note out of band (raw PUT, or type in Obsidian), then call the tool with the stale `if_match` → it must fail with "Precondition failed" and leave the edit intact. Also `write_note(create_only=True)` against an existing note → "already exists", untouched.
7. **`search_vault`** with `search_type="text"` — confirm `results[].context` strings are populated.
8. **`search_vault`** with `search_type="jsonlogic"` and a real expression like `{"in": ["#project", {"var": "tags"}]}` — confirm matches.
9. **`search_vault`** with `search_type="dataview"` — confirm it's refused by schema validation, before any request. Neither search tool's result should have a `warning` key.
10. **`search_all_vaults`** with two vaults configured, one offline — confirm the online one returns results and the offline one shows `status: "error"` with a connection error, not a whole-tool failure.

If any of these diverges from the docs, the mock-based tests are lying. Open an issue with the actual response shape and update the relevant curator / handler.

## Adding a Tool

1. Create `src/obsidian_multivault_mcp/tools/new_tool.py`
2. Import `mcp`, `get_client` from `..server` and types from `..validation_types`
3. Write `curate_new_result(raw: dict) -> dict` — handle timestamp conversion, field renames, frontmatter stripping if applicable. Reuse helpers from `tools/_helpers.py` (`strip_frontmatter`, `epoch_ms_to_iso`, `curate_simple_match`, `curate_structured_match`).
4. Write `@mcp.tool(annotations={...}, tags={...})` async function with `Annotated` params and an LLM-facing docstring. Keep the docstring under 2KB, with detail in parameter descriptions (see Code Style). Let `ToolError` propagate from the client layer.
5. Add a client method to `client.py` — per-endpoint pattern.
6. **No edits to `tools/__init__.py` needed** — `pkgutil.iter_modules()` auto-discovers any non-`_`-prefixed module at import time.
7. Add tests: curator in `test_curators.py`, integration in `test_tools.py` using the `mcp_client` + `vault_handlers` fixtures from `conftest.py`.
8. Run `poetry run pytest tests/ && poetry run black src/ tests/ && poetry run pylint src/obsidian_multivault_mcp/`

## Code Style

- Python 3.13, type hints on all functions
- Black + Pylint
- LLM-facing docstrings on all tools (written for tool discovery, not developers)
- **Keep `skills/obsidian-multivault/SKILL.md` in sync.** It's the agent-facing workflow guide. When a tool's behaviour, parameters or refusals change, update the skill in the same PR.
- **Tool docstrings stay under 2KB.** Claude Code truncates tool descriptions at 2KB before the model sees them, so anything past the cut is silently lost (`patch_note`'s whole operations section once was). Put the rules a caller must not miss first, and per-parameter detail in `Annotated[..., Field(description=...)]`, which isn't truncated. `tests/test_tools.py::TestToolDescriptions` enforces the limit for every tool.
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

Loaded via `python-dotenv` with `load_dotenv(override=True)` inside `main()` (not at module import time — see *Env var loading* above).

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