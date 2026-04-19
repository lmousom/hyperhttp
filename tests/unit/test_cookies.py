from hyperhttp._headers import Headers
from hyperhttp._url import URL
from hyperhttp.cookies import Cookies


def test_empty_jar_has_zero_len():
    c = Cookies()
    assert len(c) == 0
    assert c.get("x") is None
    assert c.get("x", "default") == "default"


def test_init_from_mapping():
    c = Cookies({"a": "1", "b": "2"})
    assert c.get("a") == "1"
    assert c.get("b") == "2"
    assert len(c) == 2


def test_init_from_sequence():
    c = Cookies([("a", "1")])
    assert c.get("a") == "1"


def test_init_copy_from_cookies():
    src = Cookies({"a": "1"})
    dst = Cookies(src)
    assert dst.get("a") == "1"
    # Independent:
    src.set("b", "2")
    assert dst.get("b") is None


def test_add_to_request_empty_jar_noop():
    c = Cookies()
    h = Headers()
    c.add_to_request(URL("http://example.com/"), h)
    assert "cookie" not in h


def test_add_to_request_sets_cookie_header():
    c = Cookies()
    c.set("session", "abc123", domain="example.com")
    h = Headers()
    c.add_to_request(URL("http://example.com/"), h)
    # http.cookiejar emits the Cookie header.
    assert h.get("cookie") == "session=abc123"


def test_add_to_request_merges_with_existing_cookie_header():
    c = Cookies()
    c.set("session", "abc123", domain="example.com")
    h = Headers({"Cookie": "x=1"})
    c.add_to_request(URL("http://example.com/"), h)
    value = h.get("cookie")
    assert value is not None
    assert "x=1" in value
    assert "session=abc123" in value


def test_extract_from_response_no_set_cookie_is_fast_noop():
    c = Cookies()
    c.extract_from_response(URL("http://example.com/"), Headers({"Content-Type": "text/plain"}))
    assert len(c) == 0


def test_extract_from_response_stores_cookie():
    c = Cookies()
    h = Headers()
    h.add("Set-Cookie", "sid=xyz; Path=/; Domain=example.com")
    c.extract_from_response(URL("http://example.com/"), h)
    assert c.get("sid") == "xyz"


def test_iteration_yields_cookies():
    c = Cookies({"a": "1"})
    names = [cookie.name for cookie in c]
    assert names == ["a"]
