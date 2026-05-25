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


def setup_logging(name: str) -> logging.Logger:
    global _CONFIGURED  # pylint: disable=global-statement
    if not _CONFIGURED:
        level = os.environ.get("OBSIDIAN_MCP_LOG_LEVEL", "INFO").upper()
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
