"""
SSL/TLS context construction and ALPN setup.
"""

from __future__ import annotations

import ssl
import warnings
from typing import Optional, Tuple, Union

import certifi

_DEFAULT_ALPN = ("h2", "http/1.1")


class InsecureRequestWarning(Warning):
    """Emitted the first time a Client is configured with ``verify=False``.

    Disabling TLS verification exposes the connection to active MITM attacks.
    The warning is emitted by the ``warnings`` module so it integrates with
    standard ``-W`` filters and test harnesses; silence it with::

        import warnings
        from hyperhttp import InsecureRequestWarning
        warnings.simplefilter("ignore", InsecureRequestWarning)

    Or — preferred — supply a real CA bundle via ``verify="/path/to/ca.pem"``.
    """


_INSECURE_WARNING_TEXT = (
    "Unverified HTTPS request: TLS certificate verification is disabled "
    "(verify=False). This connection is vulnerable to active MITM attacks; "
    "the server's identity is NOT checked. Use verify=<path-to-ca-bundle> "
    "for pinned roots, or use verify=True in production."
)


def _warn_insecure_verify() -> None:
    """Emit :class:`InsecureRequestWarning` at the caller's stacklevel.

    ``stacklevel=3`` so the warning points at the user's ``hyperhttp.Client(
    ..., verify=False)`` call, not at our internal TLS setup.
    """
    warnings.warn(_INSECURE_WARNING_TEXT, InsecureRequestWarning, stacklevel=3)


def create_ssl_context(
    *,
    verify: Union[bool, str] = True,
    cert: Optional[Union[str, Tuple[str, str], Tuple[str, str, str]]] = None,
    alpn_protocols: Tuple[str, ...] = _DEFAULT_ALPN,
) -> ssl.SSLContext:
    """Build a secure default SSL context.

    - ``verify=True``: use certifi's CA bundle.
    - ``verify=False``: disable verification (NOT recommended). Emits an
      :class:`InsecureRequestWarning` every time this context is built so
      misconfigurations are visible in tests and CI logs.
    - ``verify=<path>``: use the CA bundle at ``path``.
    """
    if verify is False:
        _warn_insecure_verify()
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
