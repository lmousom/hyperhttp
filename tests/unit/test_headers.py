import pytest

from hyperhttp._headers import Headers


def test_init_empty():
    h = Headers()
    assert len(h) == 0
    assert list(h.items()) == []


def test_init_from_mapping_preserves_case():
    h = Headers({"Content-Type": "text/plain"})
    assert h.get("content-type") == "text/plain"
    assert list(h.items()) == [("Content-Type", "text/plain")]


def test_init_from_sequence_allows_duplicates():
    h = Headers([("Set-Cookie", "a"), ("Set-Cookie", "b")])
    assert h.get_list("set-cookie") == ["a", "b"]
    # get() combines with ", " per RFC 9110.
    assert h.get("set-cookie") == "a, b"


def test_init_copies_headers_instance():
    src = Headers({"X": "1"})
    copy = Headers(src)
    src.set("X", "2")
    assert copy.get("X") == "1"


def test_add_appends_duplicates():
    h = Headers()
    h.add("X", "1")
    h.add("X", "2")
    assert h.get_list("x") == ["1", "2"]


def test_set_replaces_all():
    h = Headers([("X", "a"), ("X", "b")])
    h.set("X", "c")
    assert h.get_list("x") == ["c"]


def test_setdefault_adds_when_missing():
    h = Headers()
    assert h.setdefault("X", "1") == "1"
    assert h.setdefault("X", "2") == "1"
    assert h.get("X") == "1"


def test_pop_returns_last_value_and_removes_all():
    h = Headers([("X", "a"), ("X", "b")])
    assert h.pop("X") == "b"
    assert "X" not in h
    assert h.pop("X", "default") == "default"


def test_delitem_raises_when_missing():
    h = Headers()
    with pytest.raises(KeyError):
        del h["X"]


def test_getitem_raises_when_missing():
    h = Headers()
    with pytest.raises(KeyError):
        _ = h["X"]


def test_contains_is_case_insensitive():
    h = Headers({"Content-Type": "text/plain"})
    assert "content-type" in h
    assert "CONTENT-TYPE" in h
    assert 42 not in h  # type: ignore[operator]


def test_update_mapping():
    h = Headers({"A": "1"})
    h.update({"A": "2", "B": "3"})
    assert h.get("A") == "2"
    assert h.get("B") == "3"


def test_update_headers_instance():
    a = Headers({"X": "1"})
    b = Headers({"Y": "2"})
    a.update(b)
    assert a.get("Y") == "2"


def test_update_none_noop():
    h = Headers({"A": "1"})
    h.update(None)
    assert h.get("A") == "1"


def test_copy_is_independent():
    h = Headers({"X": "1"})
    c = h.copy()
    c.set("X", "2")
    assert h.get("X") == "1"


def test_raw_returns_bytes_pairs():
    h = Headers({"Content-Type": "text/plain"})
    assert h.raw() == [(b"Content-Type", b"text/plain")]


def test_keys_values_items():
    h = Headers([("A", "1"), ("B", "2")])
    assert list(h.keys()) == ["A", "B"]
    assert list(h.values()) == ["1", "2"]
    assert list(h.items()) == [("A", "1"), ("B", "2")]


def test_equality_only_with_headers():
    a = Headers({"A": "1"})
    b = Headers({"A": "1"})
    assert a == b
    assert (a == {"A": "1"}) is False


def test_repr_is_stable():
    h = Headers([("A", "1")])
    assert "A" in repr(h) and "1" in repr(h)
