"""
SSL/TLS context construction and ALPN setup.
"""

from __future__ import annotations

import ssl
from typing import Optional, Tuple, Union

import certifi

_DEFAULT_ALPN = ("h2", "http/1.1")


def create_ssl_context(
    *,
    verify: Union[bool, str] = True,
    cert: Optional[Union[str, Tuple[str, str], Tuple[str, str, str]]] = None,
    alpn_protocols: Tuple[str, ...] = _DEFAULT_ALPN,
) -> ssl.SSLContext:
    """Build a secure default SSL context.

    - ``verify=True``: use certifi's CA bundle.
    - ``verify=False``: disable verification (NOT recommended; logs once).
    - ``verify=<path>``: use the CA bundle at ``path``.
    """
    if verify is False:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif verify is True:
        ctx = ssl.create_default_context(cafile=certifi.where())
    else:
        ctx = ssl.create_default_context(cafile=verify)

    if cert is not None:
        if isinstance(cert, str):
            ctx.load_cert_chain(cert)
        elif len(cert) == 2:
            ctx.load_cert_chain(cert[0], cert[1])
        else:
            ctx.load_cert_chain(cert[0], cert[1], cert[2])

    try:
        ctx.set_alpn_protocols(list(alpn_protocols))
    except NotImplementedError:  # pragma: no cover - old TLS stack
        pass

    ctx.options |= ssl.OP_NO_COMPRESSION
    return ctx
