"""Centralized logging configuration. Logs to stderr at OBSIDIAN_MCP_LOG_LEVEL.

Attaches its handler to the package logger ("obsidian-multivault-mcp"),
not the root logger, so that importing this package into a larger process
doesn't clobber the host's logging configuration. Child loggers like
"obsidian-multivault-mcp.client" propagate up to the package logger;
propagation past the package is disabled to avoid duplicate emission via
a host root handler.
"""

import logging
import os
import sys

PACKAGE_LOGGER_NAME = "obsidian-multivault-mcp"

_CONFIGURED = False


def _resolve_level(raw: str) -> int:
    """Map a string level to the int constant. Unknown values fall back to INFO."""
    # logging._nameToLevel exists on every supported Python; getLevelNamesMapping
    # is 3.11+. Use the underscore name for back-compat and treat it as stable.
    mapping = logging.getLevelNamesMapping()
    resolved = mapping.get(raw.upper())
    if resolved is None:
        # Don't crash the server over a mis-set env var. Use stderr directly
        # since the package logger we're configuring isn't ready yet.
        sys.stderr.write(
            f"obsidian-multivault-mcp: unknown OBSIDIAN_MCP_LOG_LEVEL={raw!r}, "
            f"falling back to INFO.\n"
        )
        return logging.INFO
    return resolved


def setup_logging(name: str) -> logging.Logger:
    global _CONFIGURED  # pylint: disable=global-statement
    if not _CONFIGURED:
        level = _resolve_level(os.environ.get("OBSIDIAN_MCP_LOG_LEVEL", "INFO"))
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        pkg_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
        pkg_logger.setLevel(level)
        # Avoid stacking duplicate handlers if setup_logging is somehow re-run
        # (e.g. across test sessions in the same interpreter).
        if not any(getattr(h, "_obsidian_mcp_marker", False) for h in pkg_logger.handlers):
            # pylint: disable=protected-access
            handler._obsidian_mcp_marker = True  # type: ignore[attr-defined]
            pkg_logger.addHandler(handler)
        pkg_logger.propagate = False
        _CONFIGURED = True
    return logging.getLogger(name)
