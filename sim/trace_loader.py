"""Per-entry trace loader and synthetic generator for the CODoH simulator.

Schema (sim-spec §3.1):
    rank, site, run, day, hostname, started_offset_ms, dns_ms

`dns_ms == 0` is the failed-DNS sentinel (NXDOMAIN/SERVFAIL/timeout) — the
query reached the resolver in production, so it is a first-class observable
and the simulator must batch it at `started_offset_ms` (decision #41).

Public surface:
    load_real(path)                          -> Trace
    generate_synthetic(n_sites, n_runs, ...) -> Trace
    Trace.filter_all_runs_intact()           -> Trace        (decision #40)
    Trace.stats()                            -> dict
    Trace.magnitude_band_sample(rng, ...)    -> bucket sample (sim-spec §8.1)
"""

from __future__ import annotations

import csv
import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

PageKey = tuple[int, str, int, str]  # (rank, site, run, day)


@dataclass(frozen=True, slots=True)
class Entry:
    hostname: str
    offset_ms: float
    dns_ms: float

    @property
    def failed(self) -> bool:
        return self.dns_ms == 0.0


@dataclass
class Trace:
    pages: dict[PageKey, list[Entry]] = field(default_factory=dict)
    Q_w: dict[str, set[str]] = field(default_factory=dict)  # union over (run, day)
    site_rank: dict[str, int] = field(default_factory=dict)
    days: set[str] = field(default_factory=set)
    runs: set[int] = field(default_factory=set)

    # ---- views -----------------------------------------------------------

    @property
    def n_pages(self) -> int:
        return len(self.pages)

    @property
    def sites(self) -> list[str]:
        return sorted(self.site_rank, key=lambda s: self.site_rank[s])

    def runs_per_site(self) -> dict[str, set[int]]:
        out: dict[str, set[int]] = defaultdict(set)
        for (_rank, site, run, _day) in self.pages:
            out[site].add(run)
        return out

    # ---- summary stats (used by the §12 criterion-2b regression test) ----

    def stats(self) -> dict:
        per_page_entries = sorted(len(v) for v in self.pages.values())
        per_page_unique = sorted(
            len({e.hostname for e in v}) for v in self.pages.values()
        )
        Q_w_sizes = sorted(len(v) for v in self.Q_w.values())
        return {
            "n_pages": len(self.pages),
            "n_sites": len(self.site_rank),
            "n_entries": sum(per_page_entries),
            "n_failed_dns": sum(1 for v in self.pages.values() for e in v if e.failed),
            "per_page_entries": _quants(per_page_entries),
            "per_page_unique_hosts": _quants(per_page_unique),
            "Q_w": _quants(Q_w_sizes),
            "days": sorted(self.days),
            "runs": sorted(self.runs),
        }

    # ---- filters / samplers ---------------------------------------------

    def filter_all_runs_intact(self, expected: set[int] | None = None) -> "Trace":
        """Drop sites that don't have every expected run (decision #40).
        Applied before magnitude-band sampling so advertised `n` is honest.
        """
        if expected is None:
            expected = set(self.runs)
        rps = self.runs_per_site()
        keep = {s for s, rs in rps.items() if rs >= expected}
        new_pages = {k: v for k, v in self.pages.items() if k[1] in keep}
        new_Q = {s: hs for s, hs in self.Q_w.items() if s in keep}
        new_rank = {s: r for s, r in self.site_rank.items() if s in keep}
        return Trace(
            pages=new_pages, Q_w=new_Q, site_rank=new_rank,
            days=set(self.days), runs=set(self.runs),
        )

    def magnitude_band_sample(
        self,
        rng: random.Random,
        top1k_all: bool = True,
        mid_n: int = 200,
        tail_n: int = 200,
    ) -> dict[str, list[tuple[int, str]]]:
        """CrUX magnitude-band victim sampling (sim-spec §8.1).
        Returns {bucket_name: [(rank, site), ...]}.
        """
        by_bucket: dict[str, list[tuple[int, str]]] = {
            "top-1k": [], "1k-5k": [], "5k-10k": [],
        }
        for site, rank in self.site_rank.items():
            if rank <= 1000:
                by_bucket["top-1k"].append((rank, site))
            elif rank <= 5000:
                by_bucket["1k-5k"].append((rank, site))
            elif rank <= 10000:
                by_bucket["5k-10k"].append((rank, site))
        for v in by_bucket.values():
            v.sort()
        out = {}
        out["top-1k"] = list(by_bucket["top-1k"]) if top1k_all \
            else _sample(rng, by_bucket["top-1k"], mid_n)
        out["1k-5k"] = _sample(rng, by_bucket["1k-5k"], mid_n)
        out["5k-10k"] = _sample(rng, by_bucket["5k-10k"], tail_n)
        return out


# =====================================================================
# Real CSV loader
# =====================================================================

EXPECTED_HEADER = [
    "rank", "site", "run", "day", "hostname", "started_offset_ms", "dns_ms",
]


def load_real(path: str | Path) -> Trace:
    path = Path(path)
    pages: dict[PageKey, list[Entry]] = defaultdict(list)
    Q_w: dict[str, set[str]] = defaultdict(set)
    site_rank: dict[str, int] = {}
    days: set[str] = set()
    runs: set[int] = set()

    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        if header != EXPECTED_HEADER:
            raise ValueError(
                f"trace header mismatch: expected {EXPECTED_HEADER}, got {header}"
            )
        for row in reader:
            rank = int(row[0])
            site = row[1]
            run = int(row[2])
            day = row[3]
            host = row[4]
            offset = float(row[5])
            dns_ms = float(row[6])
            key: PageKey = (rank, site, run, day)
            pages[key].append(Entry(host, offset, dns_ms))
            Q_w[site].add(host)
            site_rank.setdefault(site, rank)
            days.add(day)
            runs.add(run)

    # Already monotonic per validate-trace; sort defensively (cheap).
    for k in pages:
        pages[k].sort(key=lambda e: e.offset_ms)

    return Trace(
        pages=dict(pages), Q_w=dict(Q_w), site_rank=site_rank,
        days=days, runs=runs,
    )


# =====================================================================
# Synthetic generator (sim-spec §3.3)
# =====================================================================

def generate_synthetic(
    n_sites: int = 50,
    n_runs: int = 3,
    *,
    day: str = "2026-05-05",
    seed: int = 0,
    mean_q_per_page: float = 8.0,
    nb_dispersion: float = 4.0,
    interarrival_log_mu_ms: float = 200.0,
    interarrival_log_sigma: float = 1.0,
    failed_dns_rate: float = 0.19,
) -> Trace:
    """Synthetic per-entry trace with the same schema as the real CSV.

    Each page draws m queries from NegBinomial(mean, dispersion) and emits
    them with lognormal inter-arrivals. Some fraction are tagged failed-DNS
    (dns_ms=0) to exercise the sentinel path. Used only for plumbing /
    unit tests; never reported.
    """
    rng = random.Random(seed)
    pages: dict[PageKey, list[Entry]] = {}
    Q_w: dict[str, set[str]] = defaultdict(set)
    site_rank: dict[str, int] = {}

    for i in range(n_sites):
        rank = i + 1
        site = f"synth{rank:05d}.test"
        site_rank[site] = rank
        # Per-site host pool (so runs share most hosts but with jitter).
        pool_size = max(2, _neg_binom(rng, mean_q_per_page * 1.5, nb_dispersion))
        pool = [f"h{j}.{site}" for j in range(pool_size)]
        for run in range(1, n_runs + 1):
            m = max(1, _neg_binom(rng, mean_q_per_page, nb_dispersion))
            chosen = rng.sample(pool, k=min(m, len(pool)))
            t = 0.0
            entries: list[Entry] = []
            for host in chosen:
                gap = rng.lognormvariate(
                    math.log(max(interarrival_log_mu_ms, 1.0)),
                    interarrival_log_sigma,
                )
                t += gap
                if rng.random() < failed_dns_rate:
                    dns_ms = 0.0
                else:
                    dns_ms = max(0.1, rng.lognormvariate(math.log(20.0), 0.8))
                entries.append(Entry(host, t, dns_ms))
                Q_w[site].add(host)
            pages[(rank, site, run, day)] = entries

    return Trace(
        pages=pages, Q_w=dict(Q_w), site_rank=site_rank,
        days={day}, runs=set(range(1, n_runs + 1)),
    )


# =====================================================================
# helpers
# =====================================================================

def _quants(sorted_vals: list[int]) -> dict:
    if not sorted_vals:
        return {"n": 0}
    return {
        "n": len(sorted_vals),
        "min": sorted_vals[0],
        "median": statistics.median(sorted_vals),
        "p95": _q(sorted_vals, 0.95),
        "p99": _q(sorted_vals, 0.99),
        "max": sorted_vals[-1],
    }


def _q(sorted_vals, q: float) -> int:
    idx = max(0, min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def _sample(rng: random.Random, pop: list, k: int) -> list:
    if k >= len(pop):
        return list(pop)
    return rng.sample(pop, k)


def _neg_binom(rng: random.Random, mean: float, dispersion: float) -> int:
    """Negative-binomial sample with given mean and dispersion (variance/mean ≥ 1).
    Uses Gamma–Poisson mixture: λ ~ Gamma(k=mean/dispersion, θ=dispersion).
    """
    shape = max(1e-3, mean / max(dispersion, 1.0))
    scale = max(1.0, dispersion)
    lam = rng.gammavariate(shape, scale)
    # Poisson sample via Knuth's algorithm.
    L = math.exp(-lam)
    k_, p = 0, 1.0
    while p > L:
        k_ += 1
        p *= rng.random()
    return k_ - 1
