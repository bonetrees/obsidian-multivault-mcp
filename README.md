# obsidian-multivault-mcp

MCP server providing multi-vault Obsidian access through the
[obsidian-local-rest-api](https://github.com/coddingtonbear/obsidian-local-rest-api)
plugin's REST API. Wraps the HTTPS endpoints with `httpx` and exposes 9
tools for vault discovery, CRUD, search, and surgical patching.

## Requirements

- Python 3.13+
- Poetry 2.0+
- Obsidian with the **Local REST API** plugin installed and enabled in
  every vault you want to expose. **Minimum tested plugin version:
  3.4.3** — older versions are missing the PATCH v3 endpoint and the
  `/search/simple/` endpoint this server depends on.

## Plugin Setup

Each vault you want to expose needs the **Local REST API** plugin
installed and configured individually in Obsidian.

For each vault, open Obsidian and go to *Settings → Community Plugins →
Local REST API → Options*:

1. **Copy the API key** — you'll paste this into your `.env` file.
2. **Set a unique port** under *Advanced*. Each vault must listen on its
   own port (e.g. vault A on 27123, vault B on 27124). The port in the
   plugin must match the `port` value in your YAML config.
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

Example YAML (HTTP — requires enabling the insecure server in plugin settings):

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
    port: 27124
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

Claude Desktop does not natively support streamable-http in
`claude_desktop_config.json`. Use
[mcp-remote](https://www.npmjs.com/package/mcp-remote) to bridge:

1. Start the server: `poetry run python -m obsidian_multivault_mcp`
2. Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

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
| `read_note` | Read a note's content (frontmatter stripped), parsed frontmatter, tags, and stat. |
| `write_note` | Create or overwrite a note. |
| `append_note` | Append content to the end of an existing note. (LLM must include leading `\n`.) |
| `patch_note` | Surgically edit by heading / block reference / frontmatter key. Heading targets use `::` from the document root. |
| `delete_note` | Delete a note. Requires `confirm=True` as a safety gate. |
| `search_vault` | Text, JsonLogic, or Dataview DQL search in one vault. Dataview falls back to text search with a warning if the plugin is missing. |
| `search_all_vaults` | Fan-out search across every configured vault in parallel. |

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
