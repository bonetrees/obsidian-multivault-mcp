"""Centralized logging configuration. Logs to stderr at OBSIDIAN_MCP_LOG_LEVEL."""

import logging
import os
import sys

_CONFIGURED = False


def setup_logging(name: str) -> logging.Logger:
    global _CONFIGURED  # pylint: disable=global-statement
    if not _CONFIGURED:
        level = os.environ.get("OBSIDIAN_MCP_LOG_LEVEL", "INFO").upper()
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(level)
        _CONFIGURED = True
    return logging.getLogger(name)
