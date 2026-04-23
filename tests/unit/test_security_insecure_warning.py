"""
Disabling TLS verification should not be silent. We emit an
``InsecureRequestWarning`` at Client-construction time and again whenever a
new TLS context is built — so the misconfiguration surfaces in test logs and
CI even if no HTTPS request has happened yet.
"""

from __future__ import annotations

import warnings

import pytest

import hyperhttp
from hyperhttp.connection.tls import InsecureRequestWarning, create_ssl_context


def test_warning_fires_for_client_verify_false() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        hyperhttp.Client(verify=False)
    messages = [
        w for w in caught if issubclass(w.category, InsecureRequestWarning)
    ]
    assert messages, "expected InsecureRequestWarning at Client(verify=False)"
    assert "verify=False" in str(messages[0].message)


def test_warning_does_not_fire_for_default() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        hyperhttp.Client()
    for w in caught:
        assert not issubclass(w.category, InsecureRequestWarning)


def test_warning_does_not_fire_for_custom_ssl_context() -> None:
    # Supplying a pre-built ssl_context is an explicit choice — users who go
    # that far presumably know what they're doing. We only warn on the
    # boolean shortcut.
    import ssl

    ctx = ssl.create_default_context()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        hyperhttp.Client(verify=False, ssl_context=ctx)
    for w in caught:
        assert not issubclass(w.category, InsecureRequestWarning)


def test_warning_fires_for_create_ssl_context() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ctx = create_ssl_context(verify=False)
    import ssl

    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False
    messages = [
        w for w in caught if issubclass(w.category, InsecureRequestWarning)
    ]
    assert messages


def test_warning_can_be_silenced() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("ignore", InsecureRequestWarning)
        hyperhttp.Client(verify=False)
    for w in caught:
        assert not issubclass(w.category, InsecureRequestWarning)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
