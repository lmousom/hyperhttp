"""Unit tests for proxy URL parsing, environment pickup, and NO_PROXY."""

from __future__ import annotations

import pytest

from hyperhttp._proxy import ProxyConfig, ProxyURL, parse_proxy_url
from hyperhttp._url import URL
from hyperhttp.exceptions import InvalidURL


class TestParseProxyURL:
    def test_plain_http(self) -> None:
        p = parse_proxy_url("http://proxy.local:3128")
        assert p.scheme == "http"
        assert p.host == "proxy.local"
        assert p.port == 3128
        assert p.username is None
        assert p.password is None
        assert p.basic_auth_header() is None

    def test_https_default_port(self) -> None:
        p = parse_proxy_url("https://secure.proxy")
        assert p.scheme == "https"
        assert p.port == 443

    def test_http_default_port(self) -> None:
        p = parse_proxy_url("http://squid")
        assert p.port == 80

    def test_with_basic_auth(self) -> None:
        p = parse_proxy_url("http://alice:s3cret@proxy.local:8080")
        assert p.username == "alice"
        assert p.password == "s3cret"
        # "alice:s3cret" -> base64
        assert p.basic_auth_header() == "Basic YWxpY2U6czNjcmV0"

    def test_username_no_password(self) -> None:
        p = parse_proxy_url("http://token@proxy.local:3128")
        assert p.username == "token"
        assert p.password is None
        assert p.basic_auth_header() == "Basic dG9rZW46"

    def test_percent_encoded_credentials(self) -> None:
        p = parse_proxy_url("http://user%40corp:p%3Ass@proxy:3128")
        assert p.username == "user@corp"
        assert p.password == "p:ss"

    def test_rejects_socks(self) -> None:
        with pytest.raises(InvalidURL, match="SOCKS"):
            parse_proxy_url("socks5://proxy:1080")

    def test_rejects_unknown_scheme(self) -> None:
        with pytest.raises(InvalidURL):
            parse_proxy_url("ftp://proxy:3128")

    def test_rejects_missing_host(self) -> None:
        with pytest.raises(InvalidURL):
            parse_proxy_url("http://")

    def test_passthrough(self) -> None:
        p = parse_proxy_url("http://a:1")
        assert parse_proxy_url(p) is p

    def test_pool_key_includes_auth(self) -> None:
        a = parse_proxy_url("http://alice:a@proxy:3128")
        b = parse_proxy_url("http://alice:b@proxy:3128")
        c = parse_proxy_url("http://proxy:3128")
        assert a.pool_key() != b.pool_key()
        assert a.pool_key() != c.pool_key()
        assert a != b
        assert parse_proxy_url("http://alice:a@proxy:3128") == a


class TestProxyConfigExplicit:
    def test_single_string_applies_to_all_schemes(self) -> None:
        cfg = ProxyConfig("http://proxy:3128", trust_env=False)
        http = cfg.for_url(URL("http://example.com/"))
        https = cfg.for_url(URL("https://example.com/"))
        assert http is not None and http.host == "proxy"
        assert https is not None and https.host == "proxy"

    def test_per_scheme_mapping(self) -> None:
        cfg = ProxyConfig(
            {"http": "http://http-proxy:3128", "https": "http://https-proxy:3128"},
            trust_env=False,
        )
        p_http = cfg.for_url(URL("http://example.com/"))
        p_https = cfg.for_url(URL("https://example.com/"))
        assert p_http is not None and p_http.host == "http-proxy"
        assert p_https is not None and p_https.host == "https-proxy"

    def test_explicit_none_disables_scheme(self) -> None:
        cfg = ProxyConfig(
            {"http": "http://proxy:3128", "https": None},
            trust_env=False,
        )
        assert cfg.for_url(URL("http://example.com/")) is not None
        assert cfg.for_url(URL("https://example.com/")) is None

    def test_invalid_mapping_key_rejected(self) -> None:
        with pytest.raises(InvalidURL):
            ProxyConfig({"ftp": "http://x:3128"}, trust_env=False)

    def test_invalid_input_type(self) -> None:
        with pytest.raises(TypeError):
            ProxyConfig(12345, trust_env=False)  # type: ignore[arg-type]

    def test_no_config(self) -> None:
        cfg = ProxyConfig(None, trust_env=False)
        assert cfg.for_url(URL("https://example.com/")) is None
        assert cfg.has_any() is False


class TestProxyConfigEnv:
    def test_http_proxy_env_picked_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://env-http:3128")
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("ALL_PROXY", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        cfg = ProxyConfig(None, trust_env=True)
        assert cfg.has_any() is True
        p = cfg.for_url(URL("http://example.com/"))
        assert p is not None and p.host == "env-http"

    def test_lowercase_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.setenv("https_proxy", "http://env-https:3128")
        monkeypatch.delenv("NO_PROXY", raising=False)
        cfg = ProxyConfig(None, trust_env=True)
        p = cfg.for_url(URL("https://example.com/"))
        assert p is not None and p.host == "env-https"

    def test_all_proxy_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("ALL_PROXY", "http://catchall:3128")
        monkeypatch.delenv("NO_PROXY", raising=False)
        cfg = ProxyConfig(None, trust_env=True)
        p = cfg.for_url(URL("http://foo/"))
        assert p is not None and p.host == "catchall"
        p = cfg.for_url(URL("https://foo/"))
        assert p is not None and p.host == "catchall"

    def test_trust_env_false_ignores_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://env-http:3128")
        cfg = ProxyConfig(None, trust_env=False)
        assert cfg.for_url(URL("http://example.com/")) is None
        assert cfg.has_any() is False

    def test_explicit_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://env:3128")
        cfg = ProxyConfig({"http": "http://explicit:3128"}, trust_env=True)
        p = cfg.for_url(URL("http://example.com/"))
        assert p is not None and p.host == "explicit"

    def test_explicit_none_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://env:3128")
        cfg = ProxyConfig({"http": None}, trust_env=True)
        assert cfg.for_url(URL("http://example.com/")) is None


class TestNoProxy:
    def test_exact_host_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://env:3128")
        monkeypatch.setenv("NO_PROXY", "skip.me,another.com")
        cfg = ProxyConfig(None, trust_env=True)
        assert cfg.for_url(URL("http://skip.me/")) is None
        assert cfg.for_url(URL("http://example.com/")) is not None

    def test_suffix_match_via_leading_dot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://env:3128")
        monkeypatch.setenv("NO_PROXY", ".internal")
        cfg = ProxyConfig(None, trust_env=True)
        # ``requests`` / ``httpx`` compat: the leading dot is stripped, then
        # both the bare host and any subdomain match.
        assert cfg.for_url(URL("http://api.internal/")) is None
        assert cfg.for_url(URL("http://internal/")) is None
        assert cfg.for_url(URL("http://external.com/")) is not None

    def test_wildcard_disables_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://env:3128")
        monkeypatch.setenv("NO_PROXY", "*")
        cfg = ProxyConfig(None, trust_env=True)
        assert cfg.for_url(URL("http://anywhere.com/")) is None

    def test_no_proxy_does_not_apply_to_explicit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit ``proxies=`` is authoritative and ignores NO_PROXY."""
        monkeypatch.setenv("NO_PROXY", "skip.me")
        cfg = ProxyConfig({"http": "http://explicit:3128"}, trust_env=True)
        assert cfg.for_url(URL("http://skip.me/")) is not None

    def test_cidr_match_for_ip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://env:3128")
        monkeypatch.setenv("NO_PROXY", "10.0.0.0/8")
        cfg = ProxyConfig(None, trust_env=True)
        assert cfg.for_url(URL("http://10.1.2.3/")) is None
        assert cfg.for_url(URL("http://11.0.0.1/")) is not None

    def test_host_with_port_restriction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://env:3128")
        monkeypatch.setenv("NO_PROXY", "host.local:8080")
        cfg = ProxyConfig(None, trust_env=True)
        assert cfg.for_url(URL("http://host.local:8080/")) is None
        assert cfg.for_url(URL("http://host.local:9090/")) is not None
