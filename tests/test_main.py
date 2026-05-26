"""Tests for the __main__ CLI entrypoint helpers."""

import pytest

from obsidian_multivault_mcp.__main__ import _env_int, _parse_args, _strip_host_brackets


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


class TestArgsZeroValuesPreserved:
    """argparse stores None when the flag is omitted; --port 0 means "0",
    not "use default". The earlier `or` shortcut would have masked it."""

    def test_port_zero_parsed_as_int(self):
        args = _parse_args(["--port", "0"])
        assert args.port == 0  # not None, not falsy-replaced

    def test_port_omitted_is_none(self):
        args = _parse_args([])
        assert args.port is None


class TestStripHostBrackets:
    """uvicorn wants `::1`, not `[::1]`. Users will reasonably pass the URL
    form on the CLI — normalise it before binding."""

    def test_bracketed_ipv6_stripped(self):
        assert _strip_host_brackets("[::1]") == "::1"
        assert _strip_host_brackets("[fe80::1]") == "fe80::1"

    def test_unbracketed_passthrough(self):
        assert _strip_host_brackets("127.0.0.1") == "127.0.0.1"
        assert _strip_host_brackets("::1") == "::1"
        assert _strip_host_brackets("localhost") == "localhost"

    def test_partial_brackets_passthrough(self):
        # Not a valid URL form — don't try to fix it, leave it for uvicorn
        # to fail with a clearer error if applicable.
        assert _strip_host_brackets("[::1") == "[::1"
        assert _strip_host_brackets("::1]") == "::1]"

    def test_empty_brackets_passthrough(self):
        # "[]" alone (len 2) isn't a real address — don't collapse to "".
        assert _strip_host_brackets("[]") == "[]"
