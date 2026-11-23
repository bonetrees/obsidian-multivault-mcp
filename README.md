# obsidian-multivault-mcp

MCP server providing multi-vault Obsidian access through the
[obsidian-local-rest-api](https://github.com/coddingtonbear/obsidian-local-rest-api)
plugin's REST API. Wraps the HTTPS endpoints with `httpx` and exposes 9
tools for vault discovery, CRUD, search, and surgical patching.

## Requirements

- Python 3.13+
- Poetry 2.0+
- Obsidian with the **Local REST API** plugin installed and enabled in
  every vault you want to expose. **Minimum required plugin version:
  5.0** (developed and tested against **5.5.2**).

  `patch_note` speaks the plugin's **markdown-patch v2** format: it sends
  a `Markdown-Patch-Version: 2` header and encodes heading targets as
  JSON arrays. Plugin 5.x *requires* that header and rejects a patch
  without it, while plugin 4.x and earlier predate the format entirely
  and would read the JSON array as a literal heading name. So patching
  works against 5.x only — there is no version negotiation. The other
  tools may work against older plugin versions, but they aren't tested
  there, and `if_match` needs 5.x, so 5.0 is the supported floor.

## Plugin Setup

Each vault you want to expose needs the **Local REST API** plugin
installed and configured individually in Obsidian.

For each vault, open Obsidian and go to *Settings → Local REST API*:

1. **Copy the API key** — you'll paste this into your `.env` file.
2. **Set unique ports** under *Advanced*. Every vault's plugin runs its
   own HTTPS server (default 27124), and its HTTP server too if enabled
   (default 27123), so each vault needs its own HTTPS port, plus its own
   HTTP port if you use HTTP. No port may be shared with any other vault's
   server. For example: vault A on 27124 (HTTPS) / 27123 (HTTP), vault B
   on 27125 / 27126, matching the examples below. The port in the plugin
   must match the `port` value in your YAML config.
3. **If using HTTP:** toggle *"Enable non-encrypted (HTTP) server"* on.
   This is under the *Advanced* section. HTTP avoids self-signed
   certificate complexity and is fine for localhost.

Restart Obsidian (or reload the plugin) after changing port settings.

## Quickstart

```bash
poetry install

cp .env.example .env
# Edit .env and set OBSIDIAN_<VAULT>_API_KEY for each vault.

cp obsidian-multivault-mcp-config.example.yaml obsidian-multivault-mcp-config.yaml
# Edit the YAML to describe your vaults.

poetry run python -m obsidian_multivault_mcp
```

By default the server listens on `127.0.0.1:8100` via streamable-http.

## Configuration

Two files drive configuration:

1. **`obsidian-multivault-mcp-config.yaml`** — vault list. One entry per
   Obsidian vault, with scheme/host/port and the env var name that holds
   its API key. Path can be overridden with `OBSIDIAN_MCP_CONFIG`.
2. **`.env`** — API keys. Generate one in Obsidian under *Settings →
   Local REST API* for each vault and copy it into the matching env var.

Example YAML (HTTPS — plugin default):

```yaml
vaults:
  devprojects:
    scheme: "https"
    host: "127.0.0.1"
    port: 27124
    api_key_env: "OBSIDIAN_DEVPROJECTS_API_KEY"
  personal:
    scheme: "https"
    host: "127.0.0.1"
    port: 27125
    api_key_env: "OBSIDIAN_PERSONAL_API_KEY"
```

Example YAML (HTTP — requires enabling the non-encrypted server in plugin
settings). Each vault's HTTPS server keeps running on its own port unless
you turn it off, so the HTTP ports must avoid those too: here, devprojects
serves HTTPS on 27124 and HTTP on 27123, and personal serves HTTPS on 27125
and HTTP on 27126.

```yaml
vaults:
  devprojects:
    scheme: "http"
    host: "127.0.0.1"
    port: 27123
    api_key_env: "OBSIDIAN_DEVPROJECTS_API_KEY"
  personal:
    scheme: "http"
    host: "127.0.0.1"
    port: 27126
    api_key_env: "OBSIDIAN_PERSONAL_API_KEY"
```

API keys never appear in the YAML — `api_key_env` names the env var.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `OBSIDIAN_MCP_CONFIG` | `./obsidian-multivault-mcp-config.yaml` | Path to the YAML config |
| `OBSIDIAN_MCP_HOST` | `127.0.0.1` | Bind host for HTTP transports |
| `OBSIDIAN_MCP_PORT` | `8100` | Bind port for HTTP transports |
| `OBSIDIAN_MCP_LOG_LEVEL` | `INFO` | Logging level |
| `OBSIDIAN_MCP_TIMEOUT` | `30` | HTTP timeout in seconds |
| `OBSIDIAN_<VAULT>_API_KEY` | _(required per vault)_ | One per vault, name set in YAML |

## Transports

```bash
poetry run python -m obsidian_multivault_mcp                          # streamable-http (default)
poetry run python -m obsidian_multivault_mcp --transport sse
poetry run python -m obsidian_multivault_mcp --transport stdio        # for Claude Desktop
poetry run python -m obsidian_multivault_mcp --config ./other.yaml    # custom config path
```

## Claude Desktop

Claude Desktop only speaks stdio in `claude_desktop_config.json`, so
there are two ways to wire it up:

- **Direct stdio (simplest).** Claude Desktop spawns the server itself
  as a child process. Recommended for a single-client setup.
- **Bridge via `mcp-remote`.** Run the server once on streamable-http
  and have Claude Desktop connect through
  [mcp-remote](https://www.npmjs.com/package/mcp-remote). Useful when
  you want to share one server with multiple clients (e.g. Claude
  Desktop + an IDE) or run the server on a different machine.

The config file lives at:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

### Direct stdio

```json
{
  "mcpServers": {
    "obsidian-multivault-mcp": {
      "command": "poetry",
      "args": ["run", "python", "-m", "obsidian_multivault_mcp", "--transport", "stdio"],
      "cwd": "/absolute/path/to/obsidian-multivault-mcp"
    }
  }
}
```

### Bridge via `mcp-remote`

Start the server in another terminal:
`poetry run python -m obsidian_multivault_mcp`

```json
{
  "mcpServers": {
    "obsidian-multivault-mcp": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8100/mcp"]
    }
  }
}
```

Restart Claude Desktop after editing the config.

## Tools

| Tool | Purpose |
|---|---|
| `list_vaults` | Discover configured vaults and read each vault's `Index.md`. Call first when the user hasn't specified a vault. |
| `list_directory` | List immediate files and folders at a path. |
| `read_note` | Read a note's content (frontmatter stripped), parsed frontmatter, tags, stat, and `etag` (the note's version). |
| `write_note` | Create or overwrite a note. `create_only=True` refuses to overwrite an existing note. |
| `append_note` | Append content to the end of an existing note. (LLM must include leading `\n`.) |
| `patch_note` | Surgically edit one heading section, block reference or frontmatter key. Operations: `append`, `prepend`, `replace`, `delete`, and `insert_after` / `insert_before` for a new sibling section. See below. |
| `delete_note` | Delete a note. Requires `confirm=True` as a safety gate. |
| `search_vault` | Text or JsonLogic search in one vault. JsonLogic queries frontmatter, tags and paths, with the plugin's `glob` and `regexp` operators. (Dataview DQL isn't offered: the REST API plugin dropped it.) |
| `search_all_vaults` | Fan-out search across every configured vault in parallel. |

**Safe read-then-write.** `write_note`, `append_note`, `patch_note` and `delete_note` all accept an optional `if_match`: pass the `etag` from `read_note` (or from the previous write's result), and the write is refused, with nothing changed, if the note was edited in the meantime, e.g. by you typing in Obsidian while an agent works. Without `if_match`, writes are unconditional.

**`patch_note` refuses risky edits rather than guessing.** A refused patch writes nothing, and its error says what to do instead. Heading targets are the full path from the document root, joined with `::` (or a list of heading texts, for a heading whose own text contains `::`). It refuses:

- headings in `content` whose levels look absolute (they're relative to the target, so `#` is a direct child), or would land past h6;
- a heading `replace` that would delete child headings: use `within` to edit just the intro, target the child, or pass `replace_children=True`;
- a new section whose title its parent already has. This also makes a retried insert safe;
- heading lines in `within` content, which is spliced in literally.

Frontmatter keys take a string via `content`, or any JSON via `value` (lists, objects, numbers, booleans); `create_if_missing=True` adds a missing key. `delete_scope` picks how much a delete removes. A successful patch may return advisory `warnings` from the plugin. The [agent skill](#agent-skill) covers all of this in workflow form.

## Agent skill

[`skills/obsidian-multivault/SKILL.md`](skills/obsidian-multivault/SKILL.md) teaches an agent to use these tools well: which `patch_note` operation fits which intent, relative heading levels, sibling inserts, etags, and what each refusal means. The tool descriptions carry the critical rules, but they're limited to 2KB each, so the skill fills in the workflow.

- **Claude Code:** copy the `obsidian-multivault` folder into `~/.claude/skills/` (every project) or `.claude/skills/` (one project).
- **Claude apps:** zip the `obsidian-multivault` folder and upload it as a custom skill in Claude's settings.

## Development

```bash
poetry run pytest tests/ -v          # full suite, runs in well under a second
poetry run black src/ tests/         # format
poetry run pylint src/obsidian_multivault_mcp/   # 10/10
```

Tests use `httpx.MockTransport` against `ObsidianVaultClient` for the
client layer, and a FastMCP in-process `Client` against the registered
tools for end-to-end coverage.

## HTTPS with self-signed certificates

The plugin defaults to HTTPS on port 27124 with a self-signed cert.
TLS verification is derived from the configured `host`:

- **HTTPS to loopback** (`127.0.0.1`, `localhost`, `::1`) — client
  connects with `verify=False`. The plugin's self-signed cert is
  expected and can't be MITM'd on the loopback interface.
- **HTTPS to any other host** — verification stays on. If you expose
  the plugin off-localhost you're responsible for installing a
  properly issued cert, and the client will refuse to fall back to an
  unverified TLS session.

The `.yaml` `scheme` field can be set to `"http"` if the plugin's
insecure HTTP server (default port 27123) is enabled, but the plugin
disables that by default and HTTPS is recommended.

## License

See [LICENSE](LICENSE).
