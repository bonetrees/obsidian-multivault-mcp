"""CLI entry point. Loads .env, parses args, runs FastMCP on the chosen transport.

Importing this module is side-effect-free — `load_dotenv` and logger setup
both run inside `main()`. That keeps tests and other modules that import
helper functions (`_env_int`, `_parse_args`, `_strip_host_brackets`) from
inadvertently mutating the process environment.
"""

import argparse
import os
import sys

from . import __version__


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


def _strip_host_brackets(host: str) -> str:
    """Strip surrounding brackets from an IPv6-URL-form host.

    uvicorn / starlette want bare IPv6 literals (``::1``), not URL form
    (``[::1]``). Users may reasonably pass ``--host [::1]`` since that's
    the URL form they see in browsers, so normalise it for binding.
    """
    if host.startswith("[") and host.endswith("]") and len(host) > 2:
        return host[1:-1]
    return host


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

    # Load .env and set up the package logger only at actual CLI execution.
    # Module-level dotenv loading would mutate the process environment as a
    # side effect of `import obsidian_multivault_mcp.__main__`. Both must
    # happen before any further imports that read OBSIDIAN_* env vars
    # (config / client / logging) at construction time.
    # pylint: disable-next=import-outside-toplevel
    from dotenv import load_dotenv

    load_dotenv(override=True)

    # `--config` is applied AFTER load_dotenv so the CLI flag takes
    # precedence even when .env defines OBSIDIAN_MCP_CONFIG. Without this
    # ordering, dotenv's override=True would clobber the CLI value.
    if args.config:
        os.environ["OBSIDIAN_MCP_CONFIG"] = args.config

    # pylint: disable-next=import-outside-toplevel
    from .logging_config import setup_logging

    logger = setup_logging("obsidian-multivault-mcp")

    # Imports happen after dotenv + arg parsing so config/env are ready.
    # pylint: disable=import-outside-toplevel
    from .server import mcp
    from . import tools  # noqa: F401  pylint: disable=unused-import,import-outside-toplevel

    if args.transport == "stdio":
        # stdio doesn't bind to a host/port, so don't touch
        # OBSIDIAN_MCP_HOST / OBSIDIAN_MCP_PORT. A malformed
        # OBSIDIAN_MCP_PORT shouldn't be able to crash stdio startup
        # when the value is unused.
        mcp.run(transport="stdio")
        return 0

    # `is not None` rather than `or`: --port 0 (or an explicit empty --host "")
    # would otherwise fall through to the env/default. argparse stores None
    # when the flag is omitted, so a falsy-but-set value is meaningful here.
    host = args.host if args.host is not None else os.environ.get("OBSIDIAN_MCP_HOST", "127.0.0.1")
    port = args.port if args.port is not None else _env_int("OBSIDIAN_MCP_PORT", 8100)

    host = _strip_host_brackets(host)

    # Reuse the project-wide loopback definition rather than maintaining a
    # parallel hardcoded list (which previously missed 127.0.0.0/8, IPv6
    # loopback variants, uppercase "Localhost", etc.).
    # pylint: disable-next=import-outside-toplevel
    from .config import is_loopback_host

    if not is_loopback_host(host):
        logger.warning(
            "Binding %s on non-loopback host %s. The server has no auth — "
            "only do this on a trusted network.",
            args.transport,
            host,
        )

    mcp.run(transport=args.transport, host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
