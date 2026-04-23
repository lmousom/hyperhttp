"""
Case-insensitive multimap for HTTP headers.

The implementation is intentionally plain: a list of ``(raw_name, value)``
pairs plus a dict of lower-cased name → list-of-indices. This preserves the
order and casing the user supplied, while giving O(1) case-insensitive lookup
and O(1) amortized append.
"""

from __future__ import annotations

from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from hyperhttp._validate import validate_header_name, validate_header_value

HeadersInput = Union[
    None,
    "Headers",
    Mapping[str, str],
    Sequence[Tuple[str, str]],
]


class Headers:
    """Ordered, case-insensitive HTTP header multimap."""

    __slots__ = ("_list", "_index")

    def __init__(self, initial: HeadersInput = None) -> None:
        self._list: List[Tuple[str, str]] = []
        self._index: Dict[str, List[int]] = {}
        if initial is None:
            return
        if isinstance(initial, Headers):
            for name, value in initial._list:
                self._append(name, value)
            return
        if isinstance(initial, Mapping):
            items: Iterable[Tuple[str, str]] = initial.items()
        else:
            items = initial
        for name, value in items:
            self._append(name, value)

    # -- mutation -----------------------------------------------------------

    def _append(self, name: str, value: str) -> None:
        # Validate every insertion — cheap, and the only place every code path
        # (``add``, ``set``, ``update``, ``__init__``) funnels through.
        validate_header_name(name)
        validate_header_value(value)
        key = name.lower()
        self._index.setdefault(key, []).append(len(self._list))
        self._list.append((name, value))

    def add(self, name: str, value: str) -> None:
        """Append a header (allowing duplicates). Values are coerced to str."""
        self._append(str(name), str(value))

    def set(self, name: str, value: str) -> None:
        """Replace all values for ``name`` with a single value."""
        self.pop(name)
        self._append(str(name), str(value))

    def __setitem__(self, name: str, value: str) -> None:
        self.set(name, value)

    def update(self, other: HeadersInput) -> None:
        if other is None:
            return
        if isinstance(other, Headers):
            iterator: Iterable[Tuple[str, str]] = other._list
        elif isinstance(other, Mapping):
            iterator = other.items()
        else:
            iterator = other
        for name, value in iterator:
            self.set(name, value)

    def setdefault(self, name: str, value: str) -> str:
        existing = self.get(name)
        if existing is not None:
            return existing
        self._append(str(name), str(value))
        return str(value)

    def pop(self, name: str, default: Any = None) -> Any:
        key = name.lower()
        indices = self._index.pop(key, None)
        if not indices:
            return default
        indices_set = set(indices)
        last_value = self._list[indices[-1]][1]
        self._list = [pair for i, pair in enumerate(self._list) if i not in indices_set]
        # Rebuild index since positions shifted.
        self._rebuild_index()
        return last_value

    def _rebuild_index(self) -> None:
        self._index.clear()
        for i, (raw, _) in enumerate(self._list):
            self._index.setdefault(raw.lower(), []).append(i)

    def __delitem__(self, name: str) -> None:
        if self.pop(name, _MISSING) is _MISSING:
            raise KeyError(name)

    # -- lookup -------------------------------------------------------------

    def get(self, name: str, default: Any = None) -> Any:
        indices = self._index.get(name.lower())
        if not indices:
            return default
        # Combine multiple values per RFC 9110 with ", ".
        if len(indices) == 1:
            return self._list[indices[0]][1]
        return ", ".join(self._list[i][1] for i in indices)

    def get_list(self, name: str) -> List[str]:
        indices = self._index.get(name.lower())
        if not indices:
            return []
        return [self._list[i][1] for i in indices]

    def __getitem__(self, name: str) -> str:
        value = self.get(name, _MISSING)
        if value is _MISSING:
            raise KeyError(name)
        return value  # type: ignore[return-value]

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        return name.lower() in self._index

    def __iter__(self) -> Iterator[str]:
        for name, _ in self._list:
            yield name

    def __len__(self) -> int:
        return len(self._list)

    def keys(self) -> Iterator[str]:
        return (name for name, _ in self._list)

    def values(self) -> Iterator[str]:
        return (value for _, value in self._list)

    def items(self) -> Iterator[Tuple[str, str]]:
        return iter(self._list)

    def raw(self) -> List[Tuple[bytes, bytes]]:
        """Return headers as ``(name, value)`` byte pairs for the wire."""
        return [(name.encode("ascii"), value.encode("latin-1")) for name, value in self._list]

    def copy(self) -> "Headers":
        new = Headers()
        new._list = list(self._list)
        new._rebuild_index()
        return new

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Headers):
            return self._list == other._list
        return NotImplemented

    def __repr__(self) -> str:
        pairs = ", ".join(f"({n!r}, {v!r})" for n, v in self._list)
        return f"Headers([{pairs}])"


class _Missing:
    pass


_MISSING = _Missing()

__all__ = ["Headers", "HeadersInput"]
