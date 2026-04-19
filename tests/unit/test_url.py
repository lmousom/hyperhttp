import pytest

from hyperhttp._url import URL, encode_query
from hyperhttp.exceptions import InvalidURL


def test_basic_https_defaults():
    u = URL("https://example.com/foo?a=1#frag")
    assert u.scheme == "https"
    assert u.host == "example.com"
    assert u.port == 443
    assert u.path == "/foo"
    assert u.query == "a=1"
    assert u.fragment == "frag"
    assert u.target == "/foo?a=1"
    assert u.host_port == "example.com"
    assert u.authority == "example.com"
    assert u.is_secure is True


def test_http_with_nonstandard_port():
    u = URL("http://example.com:8080/x")
    assert u.port == 8080
    assert u.host_port == "example.com:8080"
    assert u.is_secure is False


def test_path_defaults_to_slash():
    u = URL("http://example.com")
    assert u.path == "/"
    assert u.target == "/"


def test_userinfo_roundtrip():
    u = URL("http://alice:s3cret@example.com/x")
    assert u.userinfo == "alice:s3cret"


def test_userinfo_user_only():
    u = URL("http://alice@example.com/x")
    assert u.userinfo == "alice"


def test_equality_and_hash():
    a = URL("http://x/")
    b = URL("http://x/")
    assert a == b
    assert hash(a) == hash(b)
    assert a == "http://x/"
    assert a != 42


def test_repr_and_str():
    u = URL("http://x/a")
    assert str(u) == "http://x/a"
    assert "http://x/a" in repr(u)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-a-url",
        "ftp://example.com/",
        "http:///no-host",
    ],
)
def test_invalid_urls(bad):
    with pytest.raises(InvalidURL):
        URL(bad)


def test_with_query_merges_mapping():
    u = URL("http://x/?a=1").with_query({"b": 2})
    assert u.target == "/?a=1&b=2"


def test_with_query_merges_sequence():
    u = URL("http://x/?a=1").with_query([("b", 2), ("c", 3)])
    assert "b=2" in u.target and "c=3" in u.target


def test_with_query_string_appended():
    u = URL("http://x/?a=1").with_query("b=2")
    assert "a=1" in u.target and "b=2" in u.target


def test_with_query_none_is_self():
    u = URL("http://x/")
    assert u.with_query(None) is u


def test_join_resolves_relative_redirect():
    u = URL("http://x/a/b").join("/c")
    assert str(u) == "http://x/c"


def test_encode_query_variants():
    assert encode_query(None) == ""
    assert encode_query("a=1") == "a=1"
    assert encode_query(b"a=1") == "a=1"
    assert encode_query({"a": 1}) == "a=1"
    assert encode_query([("a", 1), ("b", "x")]) == "a=1&b=x"
