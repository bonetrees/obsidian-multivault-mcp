"""Tests for the package's log-level resolution."""

import logging

from obsidian_multivault_mcp.logging_config import _resolve_level


class TestResolveLevel:
    def test_known_level(self):
        assert _resolve_level("DEBUG") == logging.DEBUG
        assert _resolve_level("INFO") == logging.INFO
        assert _resolve_level("WARNING") == logging.WARNING

    def test_lowercase_normalised(self):
        assert _resolve_level("warning") == logging.WARNING

    def test_unknown_falls_back_to_info(self, capsys):
        # A typo (e.g. "INFOR" or "VERBOSE") must not crash startup.
        result = _resolve_level("VERBOSE")
        assert result == logging.INFO
        captured = capsys.readouterr()
        assert "VERBOSE" in captured.err
        assert "falling back to INFO" in captured.err
