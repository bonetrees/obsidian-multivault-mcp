"""Tests for the YAML config loader and VaultConfig.verify_ssl derivation."""

import textwrap

import pytest

from obsidian_multivault_mcp.config import VaultConfig, load_config


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


class TestApiKeyRepr:
    """Bearer token must not appear in str/repr — otherwise an accidental
    log or debug print leaks it."""

    def test_repr_omits_api_key(self):
        cfg = VaultConfig(
            name="v",
            scheme="https",
            host="127.0.0.1",
            port=27124,
            api_key="super-secret-token",
        )
        assert "super-secret-token" not in repr(cfg)
        assert "super-secret-token" not in str(cfg)

    def test_api_key_still_accessible(self):
        # repr=False on the field, not stored differently — value still works.
        cfg = VaultConfig(name="v", scheme="https", host="127.0.0.1", port=27124, api_key="abc")
        assert cfg.api_key == "abc"


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

    def test_https_loopback_range_disables_verify(self):
        # 127.0.0.0/8 is all loopback — Debian/Ubuntu put the system hostname
        # at 127.0.1.1, that should also skip verification.
        assert self._make("https", "127.0.1.1").verify_ssl is False
        assert self._make("https", "127.5.5.5").verify_ssl is False

    def test_https_non_loopback_keeps_verify(self):
        # Non-loopback HTTPS must verify — protect against accidental MITM
        # if someone exposes the plugin beyond localhost.
        assert self._make("https", "10.0.0.5").verify_ssl is True

    def test_https_dns_name_keeps_verify(self):
        # DNS names other than 'localhost' aren't loopback as far as we can
        # tell without resolving them — keep verification on.
        assert self._make("https", "vault.example.com").verify_ssl is True

    def test_http_returns_verify_true_irrelevant(self):
        # HTTP has no TLS; the flag is irrelevant, but we return True for cleanliness.
        assert self._make("http", "127.0.0.1").verify_ssl is True


class TestBaseUrl:
    """Base URLs must bracket IPv6 literals per RFC 3986."""

    def _make(self, host):
        return VaultConfig(name="v", scheme="https", host=host, port=27124, api_key="k")

    def test_ipv4(self):
        assert self._make("127.0.0.1").base_url == "https://127.0.0.1:27124"

    def test_dns_name(self):
        assert self._make("vault.example.com").base_url == "https://vault.example.com:27124"

    def test_ipv6_loopback_bracketed(self):
        # ::1 must turn into [::1] in the URL, otherwise httpx parses
        # "::1:27124" as host=":" port="1:27124" and the connect fails.
        assert self._make("::1").base_url == "https://[::1]:27124"

    def test_ipv6_full_bracketed(self):
        assert (
            self._make("fe80::1ff:fe23:4567:890a").base_url
            == "https://[fe80::1ff:fe23:4567:890a]:27124"
        )


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

    def test_directory_path_rejected(self, tmp_path):
        # Pointing OBSIDIAN_MCP_CONFIG at a directory should fail with a
        # clear message — not a raw IsADirectoryError from open().
        with pytest.raises(RuntimeError, match="not a regular file"):
            load_config(tmp_path)

    def test_missing_file_reported(self, tmp_path):
        with pytest.raises(RuntimeError, match="not found"):
            load_config(tmp_path / "nope.yaml")

    def test_host_with_port_rejected(self, tmp_path, monkeypatch):
        # "127.0.0.1:27124" looks like an IPv6 marker (has ":") but isn't a
        # valid IP. Catching this at load time saves operators from a
        # confusing connect error against a malformed URL.
        monkeypatch.setenv("OBSIDIAN_VAULT_API_KEY", "k")
        path = _write_config(
            tmp_path,
            textwrap.dedent(
                """\
                vaults:
                  v:
                    scheme: "https"
                    host: "127.0.0.1:27124"
                    port: 27124
                    api_key_env: "OBSIDIAN_VAULT_API_KEY"
                """
            ),
        )
        with pytest.raises(RuntimeError, match="Did you include a port"):
            load_config(path)

    def test_host_with_scheme_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OBSIDIAN_VAULT_API_KEY", "k")
        path = _write_config(
            tmp_path,
            textwrap.dedent(
                """\
                vaults:
                  v:
                    scheme: "https"
                    host: "https://vault.example.com"
                    port: 27124
                    api_key_env: "OBSIDIAN_VAULT_API_KEY"
                """
            ),
        )
        with pytest.raises(RuntimeError, match="scheme"):
            load_config(path)

    def test_host_with_path_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OBSIDIAN_VAULT_API_KEY", "k")
        path = _write_config(
            tmp_path,
            textwrap.dedent(
                """\
                vaults:
                  v:
                    scheme: "https"
                    host: "vault.example.com/api"
                    port: 27124
                    api_key_env: "OBSIDIAN_VAULT_API_KEY"
                """
            ),
        )
        with pytest.raises(RuntimeError, match="path"):
            load_config(path)

    def test_dns_name_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OBSIDIAN_VAULT_API_KEY", "k")
        path = _write_config(
            tmp_path,
            textwrap.dedent(
                """\
                vaults:
                  v:
                    scheme: "https"
                    host: "vault.example.com"
                    port: 27124
                    api_key_env: "OBSIDIAN_VAULT_API_KEY"
                """
            ),
        )
        cfg = load_config(path)
        assert cfg.vaults["v"].host == "vault.example.com"
        # Non-loopback HTTPS keeps verification on.
        assert cfg.vaults["v"].verify_ssl is True

    def test_bracketed_ipv6_normalised_to_unbracketed(self, tmp_path, monkeypatch):
        # User writes "[::1]" in YAML (URL form). Canonical storage strips
        # the brackets so ipaddress.ip_address() can parse it for loopback.
        monkeypatch.setenv("OBSIDIAN_VAULT_API_KEY", "k")
        path = _write_config(
            tmp_path,
            textwrap.dedent(
                """\
                vaults:
                  v:
                    scheme: "https"
                    host: "[::1]"
                    port: 27124
                    api_key_env: "OBSIDIAN_VAULT_API_KEY"
                """
            ),
        )
        cfg = load_config(path)
        v = cfg.vaults["v"]
        assert v.host == "::1"  # stored canonical
        assert v.base_url == "https://[::1]:27124"  # bracketed in URL
        assert v.verify_ssl is False  # loopback HTTPS → verify off

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
