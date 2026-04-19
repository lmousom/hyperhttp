"""
Lightweight cookie jar.

Wraps ``http.cookiejar.CookieJar`` with an HTTP/1-style request/response
interface so it can be used directly by the client. We don't pretend to
match every quirk of browser cookie handling — just enough RFC 6265 for
sane API/back-end usage.
"""

from __future__ import annotations

from http.cookiejar import Cookie, CookieJar
from typing import Iterable, Iterator, Mapping, Optional, Tuple, Union
from urllib.request import Request as _UrlReq

from hyperhttp._headers import Headers
from hyperhttp._url import URL

__all__ = ["Cookies"]

CookiesInput = Union[None, "Cookies", CookieJar, Mapping[str, str], Iterable[Tuple[str, str]]]


class _FakeResponse:
    """Adapter that ``CookieJar.extract_cookies`` understands."""

    def __init__(self, headers: Headers) -> None:
        self._headers = headers

    def info(self) -> "_FakeResponse":
        return self

    def get_all(self, name: str, default=None):  # noqa: D401
        values = self._headers.get_list(name)
        return values if values else default


class Cookies:
    def __init__(self, initial: CookiesInput = None) -> None:
        self._jar: CookieJar = CookieJar()
        if initial is None:
            return
        if isinstance(initial, Cookies):
            for cookie in initial._jar:
                self._jar.set_cookie(cookie)
        elif isinstance(initial, CookieJar):
            for cookie in initial:
                self._jar.set_cookie(cookie)
        elif isinstance(initial, Mapping):
            for name, value in initial.items():
                self.set(name, value)
        else:
            for name, value in initial:
                self.set(name, value)

    def set(
        self,
        name: str,
        value: str,
        *,
        domain: str = "",
        path: str = "/",
    ) -> None:
        cookie = Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=bool(domain),
            domain_initial_dot=False,
            path=path,
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        self._jar.set_cookie(cookie)

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        for cookie in self._jar:
            if cookie.name == name:
                return cookie.value
        return default

    def __iter__(self) -> Iterator[Cookie]:
        return iter(self._jar)

    def __len__(self) -> int:
        return sum(1 for _ in self._jar)

    def add_to_request(self, url: URL, headers: Headers) -> None:
        # Fast path: if the jar is empty, nothing to do — and we avoid the
        # cost of urllib.request.Request() entirely on the hot path.
        if not len(self):
            return
        req = _UrlReq(str(url))
        self._jar.add_cookie_header(req)
        cookie_header = req.get_header("Cookie", None)
        if cookie_header:
            existing = headers.get("cookie")
            if existing:
                headers.set("Cookie", f"{existing}; {cookie_header}")
            else:
                headers.set("Cookie", cookie_header)

    def extract_from_response(self, url: URL, headers: Headers) -> None:
        # Fast path: skip the (somewhat expensive) cookiejar plumbing when
        # the response has no Set-Cookie header.
        if "set-cookie" not in headers:
            return
        req = _UrlReq(str(url))
        resp = _FakeResponse(headers)
        try:
            self._jar.extract_cookies(resp, req)  # type: ignore[arg-type]
        except Exception:  # cookiejar can be picky; never break the response
            pass
