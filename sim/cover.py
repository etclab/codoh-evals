"""Cover-distribution sampling (sim-spec §6.2, decision #15).

For each real query in a batch, the target draws `k` covers i.i.d. from D
with replacement; total covers per commit = `B_eff × k`. Three Ds:

  - `matched` (headline): empirical-Zipf weights from a domain-rank file.
  - `uniform`:             uniform over the same universe.
  - `stale`:               uniform over a frozen random 50% subset.

`Sampler.bind(k, rng)` returns the callable that the BatchBuffer takes,
matching the `CoverSampler` signature in `enclave.py`.

NOTE: sim-spec §6.2 names "Umbrella top-1M" as the universe. We don't have
a 1M file checked in; the loader takes any (rank, domain) CSV. For now the
default candidate universe is `data/umbrella-top-10k-resolvable.csv` —
that is *too small* (collides with the reference set; understates leakage).
Before any reported result, point `CoverUniverse.from_file` at a top-1M
list (or at minimum the 100k Cloudflare-Radar file in `data/`).
"""

from __future__ import annotations

import csv
import itertools
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .enclave import CoverEntry


@dataclass
class CoverUniverse:
    """A weighted domain universe."""
    domains: list[str]
    weights: list[float]  # un-normalized; need not sum to 1

    @classmethod
    def from_file(cls, path: str | Path, *, zipf_alpha: float = 1.0) -> "CoverUniverse":
        """Load (rank, domain) CSV and synthesize empirical-Zipf weights:
        weight(rank) = 1 / rank^alpha (default Zipf α=1 — Umbrella's empirical fit).
        """
        path = Path(path)
        domains: list[str] = []
        weights: list[float] = []
        with open(path, newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 2:
                    continue
                try:
                    rank = int(row[0].strip())
                except ValueError:
                    continue  # header
                domains.append(row[1].strip())
                weights.append(1.0 / max(rank, 1) ** zipf_alpha)
        if not domains:
            raise ValueError(f"empty universe: {path}")
        return cls(domains=domains, weights=weights)

    @classmethod
    def synthetic(cls, n: int = 1000, *, prefix: str = "u") -> "CoverUniverse":
        """Tiny synthetic universe for tests. Zipf 1/rank weights."""
        return cls(
            domains=[f"{prefix}{i}.test" for i in range(1, n + 1)],
            weights=[1.0 / i for i in range(1, n + 1)],
        )

    def __len__(self) -> int:
        return len(self.domains)


class Distribution:
    """Base sampler over a CoverUniverse."""

    def __init__(self, universe: CoverUniverse):
        self.universe = universe

    def bind(self, k: int, rng: random.Random) -> Callable:
        """Return a CoverSampler callable bound to (k, rng).

        The returned callable signature matches enclave.CoverSampler:
            (victim_real, bg_real) -> list[CoverEntry]

        Cumulative weights are accumulated **once** here, not on every
        rng.choices call — `random.choices` otherwise re-runs
        `itertools.accumulate(weights)` on each invocation, which is the
        hot loop's worst offender at top-1M scale.
        """
        domains, weights = self._sampling_pool()
        cum = list(itertools.accumulate(weights))

        def _sampler(victim_real: list[str], bg_real: list[str]) -> list[CoverEntry]:
            out: list[CoverEntry] = []
            for h in victim_real:
                for d in rng.choices(domains, cum_weights=cum, k=k):
                    out.append(CoverEntry(d, "victim"))
            for h in bg_real:
                for d in rng.choices(domains, cum_weights=cum, k=k):
                    out.append(CoverEntry(d, "bg"))
            return out

        return _sampler

    def _sampling_pool(self) -> tuple[list[str], list[float]]:
        return self.universe.domains, self.universe.weights


class Matched(Distribution):
    """Empirical-Zipf over the loaded universe (sim-spec §6.2)."""


class Uniform(Distribution):
    """Uniform over the same universe."""

    def _sampling_pool(self):
        return self.universe.domains, [1.0] * len(self.universe.domains)


class Stale(Distribution):
    """Uniform over a frozen random 50% subset of the universe (decision #35)."""

    def __init__(self, universe: CoverUniverse, *, freeze_seed: int = 0):
        super().__init__(universe)
        rng = random.Random(freeze_seed)
        idx = list(range(len(universe.domains)))
        rng.shuffle(idx)
        keep = idx[: len(idx) // 2]
        self._frozen = [universe.domains[i] for i in sorted(keep)]

    def _sampling_pool(self):
        return self._frozen, [1.0] * len(self._frozen)


def make_distribution(kind: str, universe: CoverUniverse) -> Distribution:
    kind = kind.lower()
    if kind == "matched":
        return Matched(universe)
    if kind == "uniform":
        return Uniform(universe)
    if kind == "stale":
        return Stale(universe)
    raise ValueError(f"unknown D: {kind}")
