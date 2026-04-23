"""
Retry-log URL sanitization and redirect-reference hardening.

* ``URL.sanitized()`` strips userinfo and query-string — those routinely
  carry credentials (``?api_key=...``, ``user:pass@host``) that must not
  reach log aggregators.
* ``URL.join()`` rejects references containing CR/LF/NUL/TAB so a redirect
  target can't smuggle control bytes via the ``urljoin`` stdlib function.
"""

from __future__ import annotations

import pytest

from hyperhttp._url import URL
from hyperhttp.errors.retry import _safe_url_for_log
from hyperhttp.exceptions import InvalidURL


# ---------------------------------------------------------------------------
# URL.sanitized()
# ---------------------------------------------------------------------------


def test_sanitized_strips_query_string() -> None:
    u = URL("https://api.example.com/v1/users?api_key=SECRET123")
    out = u.sanitized()
    assert "SECRET123" not in out
    assert "<redacted>" in out
    assert out == "https://api.example.com/v1/users?<redacted>"


def test_sanitized_preserves_path_and_scheme() -> None:
    u = URL("https://example.com/foo/bar")
    assert u.sanitized() == "https://example.com/foo/bar"


def test_sanitized_includes_non_default_port() -> None:
    u = URL("https://example.com:8443/health")
    assert u.sanitized() == "https://example.com:8443/health"


def test_sanitized_drops_userinfo() -> None:
    u = URL("https://alice:hunter2@example.com/x")
    out = u.sanitized()
    assert "alice" not in out
    assert "hunter2" not in out
    assert out == "https://example.com/x"


def test_safe_url_for_log_handles_garbage() -> None:
    # Non-parseable strings shouldn't crash the logger and shouldn't leak
    # any query-string secrets if urlparse somehow keeps them.
    out = _safe_url_for_log("not a url?api_key=SECRET")
    assert "SECRET" not in out


def test_safe_url_for_log_strips_secrets() -> None:
    out = _safe_url_for_log(
        "https://user:tok@api.example.com/v1/x?token=AAAA&api_key=BBBB"
    )
    assert "tok" not in out
    assert "AAAA" not in out
    assert "BBBB" not in out


# ---------------------------------------------------------------------------
# URL.join() safety on redirects
# ---------------------------------------------------------------------------


def test_join_rejects_crlf_in_redirect_target() -> None:
    u = URL("https://example.com/a")
    with pytest.raises(InvalidURL):
        u.join("/b\r\nX-Smuggle: 1")


def test_join_rejects_empty_reference() -> None:
    u = URL("https://example.com/a")
    with pytest.raises(InvalidURL):
        u.join("")


def test_join_rejects_control_char_references() -> None:
    u = URL("https://example.com/a")
    for ch in ("\r", "\n", "\0", "\t"):
        with pytest.raises(InvalidURL):
            u.join(f"/b{ch}c")


def test_join_rejects_non_http_scheme() -> None:
    u = URL("https://example.com/a")
    # URL.__init__ enforces http/https — javascript: target must be rejected.
    with pytest.raises(InvalidURL):
        u.join("javascript:alert(1)")


def test_join_resolves_relative() -> None:
    u = URL("https://example.com/a/b")
    assert str(u.join("c")) == "https://example.com/a/c"
    assert str(u.join("/c")) == "https://example.com/c"


def test_join_resolves_scheme_relative() -> None:
    # ``//evil/path`` is a scheme-relative redirect; URL.__init__ will apply
    # our stricter validation to the resulting URL.
    u = URL("https://example.com/a")
    joined = u.join("//other.example.com/path")
    assert joined.scheme == "https"
    assert joined.host == "other.example.com"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
