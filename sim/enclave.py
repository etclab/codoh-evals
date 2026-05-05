"""Enclave batch buffer + commit logic.

`BatchBuffer.submit(host, t, owner)` accumulates a real query. The buffer
fires a commit when the size trigger (`|unique pending| ≥ B`) hits; the
caller polls `tick(t)` to fire the time trigger (`t - first_t ≥ T_max`).

A commit:
  1. samples covers via the injected callable (default returns `[]` — a
     no-cover commit, which is the upper bound on leakage).
  2. forms `inserts = unique_real + covers`, suppresses pre-cached entries
     against the cache, and updates the cache with the survivors.
  3. constructs `S = post-suppression inserts` and the strong-attacker
     observation `S' = multiset_diff(S, bg_real_known)` — covers stay
     sealed in the target→enclave bundle, so they remain in S'.

Underflow: when T_max fires with `unique pending < B_min`, the enclave
reverts to ODoH, pending insertions are discarded, no `S'` contribution
is made, and `B_eff = 0` is logged.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from .cache import LRUCache


@dataclass
class CoverEntry:
    hostname: str
    owner: str  # "victim" | "bg" — which real-query this cover was bundled with


CoverSampler = Callable[[list[str], list[str]], list[CoverEntry]]
"""Signature: (victim_real, bg_real) -> list[CoverEntry].

Slice-3 default samples k covers per real query i.i.d. from D. Slice-2
default returns `[]`.
"""


def _no_covers(_victim_real: list[str], _bg_real: list[str]) -> list[CoverEntry]:
    return []


@dataclass
class Commit:
    batch_id: int
    t_commit: float
    B_eff: int
    S_prime: list[str]
    victim_in_batch: list[str]
    victim_covers: list[str]
    bg_covers: list[str]
    underflow: bool
    pre_cached_suppressed: list[str] = field(default_factory=list)


@dataclass
class _Pending:
    host: str
    t: float
    owner: str  # "victim" | "bg"


class BatchBuffer:
    """Accumulates real queries; fires commits on size or time trigger."""

    def __init__(
        self,
        cache: LRUCache,
        *,
        B: int,
        T_max: float,
        B_min: int | None = None,
        sample_covers: CoverSampler = _no_covers,
    ):
        if B <= 0:
            raise ValueError(f"B must be > 0, got {B}")
        self.cache = cache
        self.B = B
        self.T_max = float(T_max)
        self.B_min = B if B_min is None else B_min
        self.sample_covers = sample_covers
        self._pending: list[_Pending] = []
        # host -> set of owners that queried it. A host is `victim_real` if
        # "victim" is in its owner set, regardless of which owner issued
        # the query first. The strong-attacker invariant is `S' ⊇
        # victim_real`, so an overlap host (queried by both bg and victim)
        # must survive the strong-attacker subtraction.
        self._unique: dict[str, set[str]] = {}
        self._first_t: float | None = None
        self._next_batch_id = 0

    @property
    def unique_count(self) -> int:
        return len(self._unique)

    @property
    def first_t(self) -> float | None:
        return self._first_t

    def submit(self, host: str, t: float, owner: str) -> Commit | None:
        """Accept a real query. Returns a Commit if the size trigger fires."""
        if owner not in ("victim", "bg"):
            raise ValueError(owner)
        self._pending.append(_Pending(host, t, owner))
        owners = self._unique.get(host)
        if owners is None:
            self._unique[host] = {owner}
        else:
            owners.add(owner)
        if self._first_t is None:
            self._first_t = t
        if self.unique_count >= self.B:
            return self._commit(t, time_triggered=False)
        return None

    def tick(self, t: float) -> Commit | None:
        """Time-trigger check. Caller invokes periodically."""
        if self._first_t is None:
            return None
        if t - self._first_t >= self.T_max:
            return self._commit(t, time_triggered=True)
        return None

    def flush(self, t: float) -> Commit | None:
        """End-of-trial flush — emits whatever's pending (or underflows)."""
        if self._first_t is None:
            return None
        return self._commit(t, time_triggered=True)

    # -----------------------------------------------------------------

    def _commit(self, t_commit: float, *, time_triggered: bool) -> Commit:
        batch_id = self._next_batch_id
        self._next_batch_id += 1

        unique_real = list(self._unique.keys())
        owners_of = {h: set(s) for h, s in self._unique.items()}
        if time_triggered and self.unique_count < self.B_min:
            self._reset()
            return Commit(
                batch_id=batch_id, t_commit=t_commit,
                B_eff=0, S_prime=[], victim_in_batch=[],
                victim_covers=[], bg_covers=[],
                underflow=True,
            )

        # Overlap hosts (queried by both) count as victim_real and are NOT
        # subtracted by the strong attacker — `bg_real` is bg-only.
        victim_real = [h for h in unique_real if "victim" in owners_of[h]]
        bg_real = [h for h in unique_real if "victim" not in owners_of[h]]

        covers = self.sample_covers(victim_real, bg_real)
        inserts = unique_real + [c.hostname for c in covers]

        # Pre-cache suppression: add_all skips already-cached hosts and
        # returns only the survivors (the actual cache-state delta).
        suppressed_set = {h for h in inserts if h in self.cache}
        new_entries = self.cache.add_all(inserts)

        # S = post-suppression inserts: the attacker observes the cache-
        # state delta, not the raw insert request, so pre-cached hosts
        # that never modify the cache must not appear in S.
        S = list(new_entries)

        # Strong-attacker subtraction: remove bg-only instances from S.
        # Overlap hosts are in victim_real (above), not bg_real, so they
        # survive subtraction — preserves `S' ⊇ victim_real`. Covers
        # stay in S' (sealed in the target→enclave bundle, the attacker
        # has no visibility into per-query cover sets).
        S_prime_counter = Counter(S)
        for h in bg_real:
            if S_prime_counter.get(h, 0) > 0:
                S_prime_counter[h] -= 1
                if S_prime_counter[h] == 0:
                    del S_prime_counter[h]
        S_prime = list(S_prime_counter.elements())

        commit = Commit(
            batch_id=batch_id, t_commit=t_commit,
            B_eff=len(unique_real), S_prime=S_prime,
            victim_in_batch=list(victim_real),
            victim_covers=[c.hostname for c in covers if c.owner == "victim"],
            bg_covers=[c.hostname for c in covers if c.owner == "bg"],
            underflow=False,
            pre_cached_suppressed=sorted(suppressed_set),
        )
        self._reset()
        return commit

    def _reset(self) -> None:
        self._pending.clear()
        self._unique.clear()
        self._first_t = None
