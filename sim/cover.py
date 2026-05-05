"""Cover-distribution sampling.

For each real query in a batch, the target draws `k` covers i.i.d. from D
with replacement; total covers per commit = `B_eff × k`. Three Ds:

  - `matched` (headline): empirical Zipf over the universe.
  - `uniform`:             uniform over the same universe.
  - `stale`:               uniform over a frozen random 50% subset.

`Sampler.bind(k, rng)` returns the callable that the BatchBuffer takes,
matching the `CoverSampler` signature in `enclave.py`.

Standard universe: **CrUX top-1M**
(`data/crux-202603.csv`, schema `origin,rank` with magnitude-band ranks
{1k, 5k, 10k, 50k, 100k, 500k, 1M}). Per-origin weight = mass of a 1/r
Zipf integrated over each band, divided by band size — yields proper
inter-band ratios under CrUX's band-rank coarseness; sampling within a
band is uniform (CrUX doesn't expose finer ordering).

Use `CoverUniverse.default()` to load the standard universe; pair with
`DEFAULT_REFERENCE_PATH` (CrUX top-10k, DNS-resolvable filter) for the
reference set when the trace's `Q_w` is not used directly.
"""

from __future__ import annotations

import csv
import itertools
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .enclave import CoverEntry

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DEFAULT_COVER_PATH = DATA_DIR / "crux-202603.csv"                  # CrUX 1M
DEFAULT_REFERENCE_PATH = DATA_DIR / "crux-top10k-resolvable.csv"   # CrUX 10k (resolvable)


# CrUX magnitude bands keyed by the band's upper edge (== the `rank` value
# CrUX emits). Per-origin weight = ln(high/low) / (high - low) — the average
# per-origin mass of a 1/r Zipf integrated across (low, high].
_CRUX_BANDS: dict[int, tuple[int, int]] = {
    1_000:     (1,         1_000),
    5_000:     (1_000,     5_000),
    10_000:    (5_000,    10_000),
    50_000:    (10_000,   50_000),
    100_000:   (50_000,  100_000),
    500_000:   (100_000, 500_000),
    1_000_000: (500_000, 1_000_000),
}


def _crux_band_weight(rank: int) -> float:
    band = _CRUX_BANDS.get(rank)
    if band is None:
        raise ValueError(f"unknown CrUX magnitude-band rank: {rank}")
    low, high = band
    return math.log(high / max(low, 1)) / (high - low)


@dataclass
class CoverUniverse:
    """A weighted domain universe."""
    domains: list[str]
    weights: list[float]  # un-normalized; need not sum to 1

    @classmethod
    def default(cls) -> "CoverUniverse":
        """Standard cover universe: CrUX top-1M."""
        return cls.from_file(DEFAULT_COVER_PATH)

    @classmethod
    def from_file(cls, path: str | Path, *, zipf_alpha: float = 1.0) -> "CoverUniverse":
        """Load a domain-rank CSV. Auto-detects:

          - **CrUX** (`origin,rank` header): per-origin Zipf-band weighting.
          - **Legacy** (`rank,domain`, optional header): weight = 1 / rank^alpha.
        """
        path = Path(path)
        with open(path, newline="") as f:
            reader = csv.reader(f)
            first = next(reader, None)
            if first is None:
                raise ValueError(f"empty universe: {path}")
            if [c.strip() for c in first[:2]] == ["origin", "rank"]:
                return cls._load_crux_rows(reader, path)
            return cls._load_legacy_rows(
                itertools.chain([first], reader), path, zipf_alpha,
            )

    @classmethod
    def from_crux(cls, path: str | Path) -> "CoverUniverse":
        """Load a CrUX `origin,rank` CSV explicitly."""
        path = Path(path)
        with open(path, newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header is None or [c.strip() for c in header[:2]] != ["origin", "rank"]:
                raise ValueError(f"expected CrUX schema (origin,rank): {path}")
            return cls._load_crux_rows(reader, path)

    @classmethod
    def _load_crux_rows(cls, rows: Iterable[list[str]], path: Path) -> "CoverUniverse":
        """CrUX origins → hostnames; dedupe by host (lowest rank wins —
        http+https variants of the same host collapse to the higher-popularity
        rank)."""
        best_rank: dict[str, int] = {}
        for row in rows:
            if len(row) < 2:
                continue
            try:
                rank = int(row[1].strip())
            except ValueError:
                continue
            origin = row[0].strip()
            host = origin.split("://", 1)[-1]
            if not host:
                continue
            if host not in best_rank or rank < best_rank[host]:
                best_rank[host] = rank
        if not best_rank:
            raise ValueError(f"empty CrUX universe: {path}")
        domains = list(best_rank.keys())
        weights = [_crux_band_weight(best_rank[h]) for h in domains]
        return cls(domains=domains, weights=weights)

    @classmethod
    def _load_legacy_rows(
        cls, rows: Iterable[list[str]], path: Path, zipf_alpha: float,
    ) -> "CoverUniverse":
        domains: list[str] = []
        weights: list[float] = []
        for row in rows:
            if len(row) < 2:
                continue
            try:
                rank = int(row[0].strip())
            except ValueError:
                continue  # header row
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
    """Empirical-Zipf over the loaded universe."""


class Uniform(Distribution):
    """Uniform over the same universe."""

    def _sampling_pool(self):
        return self.universe.domains, [1.0] * len(self.universe.domains)


class Stale(Distribution):
    """Uniform over a frozen random 50% subset of the universe."""

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
