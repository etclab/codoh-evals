"""LRU cache with pre-cache suppression on insert (sim-spec §5).

A domain already present is "suppressed" — `add_all` skips it (no LRU
update) and reports the actual *new* insertions. Those are what the
attacker observes (sim-spec §5.2, §7.1).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Iterable


class LRUCache:
    __slots__ = ("capacity", "_data")

    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        self.capacity = capacity
        self._data: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, host: str) -> bool:
        return host in self._data

    def __len__(self) -> int:
        return len(self._data)

    def add_all(self, hosts: Iterable[str]) -> list[str]:
        """Insert every host not already present; return the list of newly
        inserted hosts in submission order. Pre-cached hosts are suppressed
        (no LRU touch — matches sim-spec §5.2 "no-op insertion").
        """
        new_inserts: list[str] = []
        for h in hosts:
            if h in self._data:
                continue
            self._data[h] = None
            new_inserts.append(h)
            if len(self._data) > self.capacity:
                self._data.popitem(last=False)
        return new_inserts

    def snapshot(self) -> list[str]:
        return list(self._data)
