"""Tests for the YAML config loader and VaultConfig.verify_ssl derivation."""

import textwrap

import pytest

from obsidian_multivault_mcp.config import VaultConfig, load_config


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


class TestVerifySsl:
    """verify_ssl is derived: only HTTPS-to-loopback disables verification."""

    def _make(self, scheme, host):
        return VaultConfig(name="v", scheme=scheme, host=host, port=27124, api_key="k")

    def test_https_loopback_ipv4_disables_verify(self):
        assert self._make("https", "127.0.0.1").verify_ssl is False

    def test_https_localhost_disables_verify(self):
        assert self._make("https", "localhost").verify_ssl is False

    def test_https_loopback_ipv6_disables_verify(self):
        assert self._make("https", "::1").verify_ssl is False

    def test_https_non_loopback_keeps_verify(self):
        # Non-loopback HTTPS must verify — protect against accidental MITM
        # if someone exposes the plugin beyond localhost.
        assert self._make("https", "10.0.0.5").verify_ssl is True

    def test_http_returns_verify_true_irrelevant(self):
        # HTTP has no TLS; the flag is irrelevant, but we return True for cleanliness.
        assert self._make("http", "127.0.0.1").verify_ssl is True


class TestLoadConfigValidation:
    """Reject (don't silently strip) surrounding whitespace on stringly fields."""

    def test_vault_name_whitespace_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OBSIDIAN_VAULT_API_KEY", "k")
        path = _write_config(
            tmp_path,
            textwrap.dedent(
                """\
                vaults:
                  " vault ":
                    scheme: "https"
                    host: "127.0.0.1"
                    port: 27124
                    api_key_env: "OBSIDIAN_VAULT_API_KEY"
                """
            ),
        )
        with pytest.raises(RuntimeError, match="whitespace"):
            load_config(path)

    def test_host_whitespace_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OBSIDIAN_VAULT_API_KEY", "k")
        path = _write_config(
            tmp_path,
            textwrap.dedent(
                """\
                vaults:
                  v:
                    scheme: "https"
                    host: " 127.0.0.1 "
                    port: 27124
                    api_key_env: "OBSIDIAN_VAULT_API_KEY"
                """
            ),
        )
        with pytest.raises(RuntimeError, match="whitespace"):
            load_config(path)

    def test_port_bool_rejected(self, tmp_path, monkeypatch):
        # YAML `port: true` parses as Python True; bool is a subclass of int,
        # so a naive isinstance(int) check would treat it as port 1.
        monkeypatch.setenv("OBSIDIAN_VAULT_API_KEY", "k")
        path = _write_config(
            tmp_path,
            textwrap.dedent(
                """\
                vaults:
                  v:
                    scheme: "https"
                    host: "127.0.0.1"
                    port: true
                    api_key_env: "OBSIDIAN_VAULT_API_KEY"
                """
            ),
        )
        with pytest.raises(RuntimeError, match="port"):
            load_config(path)

    def test_api_key_env_whitespace_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv(" OBSIDIAN_VAULT_API_KEY ", "k")
        path = _write_config(
            tmp_path,
            textwrap.dedent(
                """\
                vaults:
                  v:
                    scheme: "https"
                    host: "127.0.0.1"
                    port: 27124
                    api_key_env: " OBSIDIAN_VAULT_API_KEY "
                """
            ),
        )
        with pytest.raises(RuntimeError, match="whitespace"):
            load_config(path)

    def test_happy_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OBSIDIAN_VAULT_API_KEY", "k")
        path = _write_config(
            tmp_path,
            textwrap.dedent(
                """\
                vaults:
                  v:
                    scheme: "https"
                    host: "127.0.0.1"
                    port: 27124
                    api_key_env: "OBSIDIAN_VAULT_API_KEY"
                """
            ),
        )
        cfg = load_config(path)
        assert set(cfg.vaults) == {"v"}
        assert cfg.vaults["v"].host == "127.0.0.1"
        assert cfg.vaults["v"].api_key == "k"
        assert cfg.vaults["v"].verify_ssl is False  # https + loopback
