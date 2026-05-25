"""CLI entry point. Loads .env, parses args, runs FastMCP on the chosen transport."""

import argparse
import os
import sys

from dotenv import load_dotenv

# load_dotenv must run before anything that reads OBSIDIAN_* env vars.
load_dotenv(override=True)

# pylint: disable=wrong-import-position
from . import __version__
from .logging_config import setup_logging

logger = setup_logging("obsidian-multivault-mcp")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="obsidian-multivault-mcp",
        description=(
            "MCP server providing multi-vault Obsidian access via the "
            "obsidian-local-rest-api plugin."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=["streamable-http", "sse", "stdio"],
        default="streamable-http",
        help="Transport protocol (default: streamable-http).",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind host for HTTP transports (default: $OBSIDIAN_MCP_HOST or 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port for HTTP transports (default: $OBSIDIAN_MCP_PORT or 8100).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to YAML config file (overrides $OBSIDIAN_MCP_CONFIG).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args(argv)


def _env_int(var: str, default: int) -> int:
    raw = os.environ.get(var)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        # Surface a clear single-line error instead of a raw ValueError stack.
        raise SystemExit(
            f"obsidian-multivault-mcp: invalid {var}={raw!r} (expected integer): {exc}"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.config:
        os.environ["OBSIDIAN_MCP_CONFIG"] = args.config

    host = args.host or os.environ.get("OBSIDIAN_MCP_HOST", "127.0.0.1")
    port = args.port or _env_int("OBSIDIAN_MCP_PORT", 8100)

    # Reuse the project-wide loopback definition rather than maintaining a
    # parallel hardcoded list (which previously missed 127.0.0.0/8, IPv6
    # loopback variants, uppercase "Localhost", etc.).
    # pylint: disable-next=import-outside-toplevel
    from .config import _is_loopback_host

    if args.transport != "stdio" and not _is_loopback_host(host):
        logger.warning(
            "Binding %s on non-loopback host %s. The server has no auth — "
            "only do this on a trusted network.",
            args.transport,
            host,
        )

    # Imports happen after dotenv + arg parsing so config/env are ready.
    # pylint: disable=import-outside-toplevel
    from .server import mcp
    from . import tools  # noqa: F401  pylint: disable=unused-import,import-outside-toplevel

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=args.transport, host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
