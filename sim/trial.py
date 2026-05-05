"""Per-trial simulation loop.

A trial = one victim page × one (B, T_max, k, λ_bg, N, α, D, init) cell ×
one trial seed. Drives the victim's trace + pre-generated background
events into the enclave's BatchBuffer in time-merge order, harvests
commits, and returns a TrialLog suitable for both per-batch (lens-a) and
cross-batch-union (lens-b) attacker analysis.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterable

from .attacker import AttackResult, evaluate
from .background import BgEvent, generate_bg_events
from .cache import LRUCache
from .cover import CoverUniverse, make_distribution
from .enclave import BatchBuffer, Commit
from .trace_loader import Trace


@dataclass
class TrialParams:
    B: int
    T_max_s: float          # seconds
    k: int = 3
    lambda_bg: int = 100    # concurrent bg users
    N: int = 1024           # cache capacity
    alpha: float = 0.5
    D: str = "matched"      # cover distribution
    init: str = "cold"      # "cold" | "warm" (warm not implemented in slice 3)


@dataclass
class TrialLog:
    params: TrialParams
    victim_site: str
    victim_rank: int
    victim_Q_v: set[str]
    commits: list[Commit] = field(default_factory=list)
    n_victim_batches: int = 0
    underflow_count: int = 0

    @property
    def S_prime_total(self) -> set[str]:
        out: set[str] = set()
        for c in self.commits:
            if c.victim_in_batch or c.victim_covers:
                out |= set(c.S_prime)
        return out

    def lens_a(self, reference_set: dict[str, set[str]]) -> list[AttackResult]:
        """Per-batch attack results (lens a — popularity-conditioned ID)."""
        out = []
        for c in self.commits:
            if not (c.victim_in_batch or c.victim_covers):
                continue
            out.append(evaluate(c.S_prime, reference_set, self.victim_site,
                                alpha=self.params.alpha))
        return out

    def lens_b(self, reference_set: dict[str, set[str]]) -> AttackResult:
        """Cross-batch-union attack result (lens b — headline lens)."""
        return evaluate(self.S_prime_total, reference_set, self.victim_site,
                        alpha=self.params.alpha)


def run_trial(
    params: TrialParams,
    *,
    victim_key: tuple[int, str, int, str],   # (rank, site, run, day)
    trace: Trace,
    cover_universe: CoverUniverse,
    seed: int,
) -> TrialLog:
    """Run one trial. Returns a TrialLog with the per-batch S' record."""
    rng = random.Random(seed)
    rank, site, _run, _day = victim_key
    victim_entries = trace.pages[victim_key]
    if not victim_entries:
        raise ValueError(f"empty victim trace for {victim_key}")

    Q_v = {e.hostname for e in victim_entries}
    last_t = victim_entries[-1].offset_ms
    T_max_ms = params.T_max_s * 1000.0
    end_t = last_t + T_max_ms + 1.0

    # cache (cold default; warm = TODO)
    cache = LRUCache(params.N)
    if params.init not in ("cold", "warm"):
        raise ValueError(params.init)
    if params.init == "warm":
        # Burn-in: run background-only until cache reaches N unique entries.
        raise NotImplementedError("warm init not wired yet")

    # cover sampler
    dist = make_distribution(params.D, cover_universe)
    cover_sampler = dist.bind(k=params.k, rng=rng)

    bb = BatchBuffer(
        cache, B=params.B, T_max=T_max_ms,
        sample_covers=cover_sampler,
    )

    # pre-generate bg events; merge with victim events in time order
    bg = generate_bg_events(
        trace=trace, universe=cover_universe,
        n_users=params.lambda_bg, t_end_ms=end_t, rng=rng,
    )
    merged = _time_merge(victim_entries, bg)

    log = TrialLog(
        params=params, victim_site=site, victim_rank=rank, victim_Q_v=Q_v,
    )

    last_event_t = 0.0
    for host, t, owner in merged:
        # If the gap to this event exceeds T_max, fire the time trigger
        # *at* `first_t + T_max` rather than at the (much later) next event
        # — otherwise t_commit drifts forward under sparse traffic, and
        # the T_max axis loses its meaning. (Sub-event ticking would only
        # matter if multiple commits could pile up in one gap, but the
        # buffer clears on commit, so one early tick is sufficient.)
        if bb.first_t is not None and (t - bb.first_t) >= bb.T_max:
            c = bb.tick(bb.first_t + bb.T_max)
            if c is not None:
                _record(log, c)
        c = bb.submit(host, t, owner)
        if c is not None:
            _record(log, c)
        last_event_t = t

    # End-of-trial flush: T_max may not have fired but we want the residual
    # batch (or the underflow log) before terminating.
    final = bb.flush(last_event_t + T_max_ms)
    if final is not None:
        _record(log, final)

    log.n_victim_batches = sum(
        1 for c in log.commits if c.victim_in_batch or c.victim_covers
    )
    log.underflow_count = sum(1 for c in log.commits if c.underflow)
    return log


# -----------------------------------------------------------------

def _time_merge(victim_entries, bg_events: list[BgEvent]):
    """Yield (host, t, owner) in non-decreasing t. Stable: bg before victim
    on tie (so a bg event scheduled at the same instant as a victim query
    is observed first — minor; either ordering is defensible)."""
    merged = []
    for e in victim_entries:
        merged.append((e.offset_ms, 1, e.hostname, "victim"))
    for ev in bg_events:
        merged.append((ev.t_ms, 0, ev.hostname, "bg"))
    merged.sort(key=lambda r: (r[0], r[1]))
    for t, _tier, host, owner in merged:
        yield host, t, owner


def _record(log: TrialLog, c: Commit) -> None:
    log.commits.append(c)
