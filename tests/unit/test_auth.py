"""Unit tests for authentication helpers."""

from __future__ import annotations

import base64
import hashlib

import pytest

from hyperhttp import BasicAuth, BearerAuth, DigestAuth
from hyperhttp._headers import Headers
from hyperhttp._url import URL
from hyperhttp.auth import _coerce_auth, _parse_challenge
from hyperhttp.client import Request


def _make_request(method: str = "GET", path: str = "/") -> Request:
    return Request(method, URL(f"http://example.test{path}"), Headers(), None)


class TestBasicAuth:
    def test_header_format(self) -> None:
        auth = BasicAuth("alice", "s3cret")
        req = _make_request()
        flow = auth.auth_flow(req)
        next(flow)
        header = req.headers["Authorization"]
        assert header.startswith("Basic ")
        token = header[len("Basic ") :]
        assert base64.b64decode(token).decode("utf-8") == "alice:s3cret"

    def test_unicode_password(self) -> None:
        auth = BasicAuth("user", "pässwörd")
        req = _make_request()
        flow = auth.auth_flow(req)
        next(flow)
        token = req.headers["Authorization"][len("Basic ") :]
        assert base64.b64decode(token).decode("utf-8") == "user:pässwörd"

    def test_rejects_non_string(self) -> None:
        with pytest.raises(TypeError):
            BasicAuth("user", 123)  # type: ignore[arg-type]


class TestBearerAuth:
    def test_header_format(self) -> None:
        auth = BearerAuth("abc.def.ghi")
        req = _make_request()
        flow = auth.auth_flow(req)
        next(flow)
        assert req.headers["Authorization"] == "Bearer abc.def.ghi"

    def test_rejects_empty_token(self) -> None:
        with pytest.raises(ValueError):
            BearerAuth("")


class TestCoerce:
    def test_none(self) -> None:
        assert _coerce_auth(None) is None

    def test_auth_passthrough(self) -> None:
        a = BasicAuth("u", "p")
        assert _coerce_auth(a) is a

    def test_tuple_becomes_basic(self) -> None:
        a = _coerce_auth(("alice", "secret"))
        assert isinstance(a, BasicAuth)

    def test_bad_shape_raises(self) -> None:
        with pytest.raises(TypeError):
            _coerce_auth(("only-one",))
        with pytest.raises(TypeError):
            _coerce_auth(("u", 1))  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            _coerce_auth("just a string")


class TestDigestChallengeParser:
    def test_parse_basic_challenge(self) -> None:
        header = (
            'Digest realm="test@example.com", '
            'qop="auth", '
            'nonce="abc123", '
            'opaque="xyz", '
            "algorithm=SHA-256"
        )
        params = _parse_challenge(header)
        assert params["realm"] == "test@example.com"
        assert params["qop"] == "auth"
        assert params["nonce"] == "abc123"
        assert params["opaque"] == "xyz"
        assert params["algorithm"] == "SHA-256"

    def test_parse_handles_escaped_quotes(self) -> None:
        header = r'Digest realm="ex\"ample", nonce="n"'
        params = _parse_challenge(header)
        assert params["realm"] == 'ex"ample'
        assert params["nonce"] == "n"


class TestDigestRFC2617Vector:
    """RFC 2617 §3.5 test vector: classic MD5 ``qop=auth`` example."""

    EXPECTED_RESPONSE = "6629fae49393a05397450978507c4ef1"

    def test_md5_response_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Monkey-patch the cnonce generator so the result is deterministic.
        monkeypatch.setattr(
            "hyperhttp.auth._make_cnonce", lambda: "0a4f113b"
        )
        auth = DigestAuth("Mufasa", "Circle Of Life")
        header = auth._build_authorization(
            method="GET",
            path="/dir/index.html",
            body=None,
            challenge={
                "realm": "testrealm@host.com",
                "nonce": "dcd98b7102dd2f0e8b11d0f600bfb0c093",
                "opaque": "5ccc069c403ebaf9f0171e9517f40e41",
                "qop": "auth",
                "algorithm": "MD5",
            },
        )
        # Extract response=... token.
        params = _params_from_auth_header(header)
        assert params["response"] == self.EXPECTED_RESPONSE
        assert params["username"] == "Mufasa"
        assert params["nc"] == "00000001"
        assert params["cnonce"] == "0a4f113b"
        assert params["qop"] == "auth"


class TestDigestSHA256:
    """Smoke check: SHA-256 algorithm runs the RFC 7616 formula.

    We don't hard-code the RFC 7616 example's opaque/next-nonce dance
    (it's intricate), but we verify the response is exactly
    ``SHA-256(HA1:nonce:nc:cnonce:qop:HA2)`` with HA1/HA2 computed as the
    spec mandates.
    """

    def test_sha256_response_math(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "hyperhttp.auth._make_cnonce",
            lambda: "f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ",
        )

        username = "Mufasa"
        password = "Circle of Life"
        realm = "http-auth@example.org"
        nonce = "7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v"
        uri = "/dir/index.html"
        method = "GET"
        qop = "auth"
        cnonce = "f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ"
        nc = "00000001"

        def h(s: str) -> str:
            return hashlib.sha256(s.encode()).hexdigest()

        ha1 = h(f"{username}:{realm}:{password}")
        ha2 = h(f"{method}:{uri}")
        expected = h(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")

        auth = DigestAuth(username, password)
        header = auth._build_authorization(
            method=method,
            path=uri,
            body=None,
            challenge={
                "realm": realm,
                "nonce": nonce,
                "qop": "auth",
                "algorithm": "SHA-256",
            },
        )
        params = _params_from_auth_header(header)
        assert params["response"] == expected
        assert params["algorithm"] == "SHA-256"


class TestDigestSessAndNonceCount:
    def test_sess_variant_includes_cnonce_in_ha1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "hyperhttp.auth._make_cnonce", lambda: "cnonce-abc"
        )
        auth = DigestAuth("u", "p")
        header = auth._build_authorization(
            method="GET",
            path="/",
            body=None,
            challenge={
                "realm": "r",
                "nonce": "n",
                "qop": "auth",
                "algorithm": "MD5-SESS",
            },
        )
        params = _params_from_auth_header(header)
        # The response has to depend on the ``-sess`` HA1 path. Easiest
        # check: recompute and compare.
        def m(s: str) -> str:
            return hashlib.md5(s.encode()).hexdigest()
        base_ha1 = m("u:r:p")
        sess_ha1 = m(f"{base_ha1}:n:cnonce-abc")
        ha2 = m("GET:/")
        expected = m(f"{sess_ha1}:n:00000001:cnonce-abc:auth:{ha2}")
        assert params["response"] == expected

    def test_nc_increments_when_nonce_reused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "hyperhttp.auth._make_cnonce", lambda: "cn"
        )
        auth = DigestAuth("u", "p")
        first = auth._build_authorization(
            method="GET",
            path="/",
            body=None,
            challenge={"realm": "r", "nonce": "same", "qop": "auth", "algorithm": "MD5"},
        )
        second = auth._build_authorization(
            method="GET",
            path="/",
            body=None,
            challenge={"realm": "r", "nonce": "same", "qop": "auth", "algorithm": "MD5"},
        )
        p1 = _params_from_auth_header(first)
        p2 = _params_from_auth_header(second)
        assert p1["nc"] == "00000001"
        assert p2["nc"] == "00000002"

    def test_nc_resets_on_new_nonce(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("hyperhttp.auth._make_cnonce", lambda: "cn")
        auth = DigestAuth("u", "p")
        auth._build_authorization(
            method="GET", path="/", body=None,
            challenge={"realm": "r", "nonce": "first", "qop": "auth", "algorithm": "MD5"},
        )
        second = auth._build_authorization(
            method="GET", path="/", body=None,
            challenge={"realm": "r", "nonce": "second", "qop": "auth", "algorithm": "MD5"},
        )
        assert _params_from_auth_header(second)["nc"] == "00000001"


class TestDigestAuthFlow:
    def test_flow_passes_through_on_non_401(self) -> None:
        auth = DigestAuth("u", "p")
        req = _make_request()
        flow = auth.auth_flow(req)
        first = next(flow)
        assert first is req
        assert "authorization" not in first.headers  # no creds yet.

        # Simulate a 200 response — the flow should terminate.
        class FakeResp:
            status_code = 200
            headers: dict = {}

        with pytest.raises(StopIteration):
            flow.send(FakeResp())

    def test_flow_ignores_non_digest_challenges(self) -> None:
        auth = DigestAuth("u", "p")
        req = _make_request()
        flow = auth.auth_flow(req)
        next(flow)

        class FakeResp:
            status_code = 401
            headers = Headers()
        r = FakeResp()
        r.headers["WWW-Authenticate"] = 'Basic realm="x"'
        with pytest.raises(StopIteration):
            flow.send(r)

    def test_flow_sends_second_request_with_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("hyperhttp.auth._make_cnonce", lambda: "cn")
        auth = DigestAuth("u", "p")
        req = _make_request("POST", "/submit")
        flow = auth.auth_flow(req)
        next(flow)

        class FakeResp:
            status_code = 401
            headers = Headers()
        r = FakeResp()
        r.headers["WWW-Authenticate"] = (
            'Digest realm="r", nonce="n", qop="auth", algorithm=MD5, opaque="o"'
        )
        second = flow.send(r)
        assert second is req
        auth_header = second.headers["Authorization"]
        assert auth_header.startswith("Digest ")
        params = _params_from_auth_header(auth_header)
        assert params["username"] == "u"
        assert params["realm"] == "r"
        assert params["uri"] == "/submit"
        assert params["nc"] == "00000001"
        assert params["opaque"] == "o"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _params_from_auth_header(header: str) -> dict:
    assert header.startswith("Digest ")
    body = header[len("Digest ") :]
    # Same parser as the challenge side — our emitted header uses the
    # same grammar, so reusing it keeps the test honest.
    return _parse_challenge("Digest " + body)
