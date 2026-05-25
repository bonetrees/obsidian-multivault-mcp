"""Tests for the __main__ CLI entrypoint helpers."""

import pytest

from obsidian_multivault_mcp.__main__ import _env_int


class TestEnvInt:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv("OBSIDIAN_MCP_PORT", raising=False)
        assert _env_int("OBSIDIAN_MCP_PORT", 8100) == 8100

    def test_empty_returns_default(self, monkeypatch):
        monkeypatch.setenv("OBSIDIAN_MCP_PORT", "")
        assert _env_int("OBSIDIAN_MCP_PORT", 8100) == 8100

    def test_valid_int_parsed(self, monkeypatch):
        monkeypatch.setenv("OBSIDIAN_MCP_PORT", "9000")
        assert _env_int("OBSIDIAN_MCP_PORT", 8100) == 9000

    def test_invalid_raises_systemexit_with_clear_message(self, monkeypatch):
        # Garbage env var must not surface as a raw ValueError stack trace;
        # SystemExit with a single-line message is what the CLI should do.
        monkeypatch.setenv("OBSIDIAN_MCP_PORT", "not-a-port")
        with pytest.raises(SystemExit) as exc_info:
            _env_int("OBSIDIAN_MCP_PORT", 8100)
        msg = str(exc_info.value)
        assert "OBSIDIAN_MCP_PORT" in msg
        assert "not-a-port" in msg
        assert "integer" in msg
