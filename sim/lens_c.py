"""Lens (c) — long-term intersection attack.

Synthetic user with a fixed 10-page repertoire (drawn from the CrUX top-1k
bucket — finest popularity bucket CrUX exposes; see `lens-c-plan.md`),
observed over `D ∈ {1, 3, 7}` synthetic days. Each day reshuffles page
order, jitters intra-page timings, gets a fresh background-traffic sample,
and a fresh cover-sampling RNG stream — matching `sim-spec §8.3` and
decision 13b (no multi-day crawl, bootstrap-resampled days).

Public surface:
    LensCParams         — hyperparameters
    UserDayLog          — one (user, day) trial result
    UserResult          — list[UserDayLog] for one user across max_days
    pick_repertoire     — choose the user's 10 sites
    synthesize_day      — generate the day's victim entry stream
    run_user_day        — drive one (user, day) through the BatchBuffer
    run_user            — run one user over max_days

Underflow handling: per sim-spec §6.1 verbatim, T_max-fired commits with
B_eff < B_min revert to ODoH and contribute nothing to S'. Lens (c)
records `odoh_fallback_count` per day (count of victim queries dropped
that way) as a co-metric alongside `days_to_fingerprint` — high underflow
rates artificially inflate privacy by removing victim queries from S'.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field

from .background import BgEvent, generate_bg_events
from .cache import LRUCache
from .cover import CoverUniverse, make_distribution
from .enclave import BatchBuffer, Commit
from .trace_loader import PageKey, Trace


# =====================================================================
# Parameters
# =====================================================================

@dataclass
class LensCParams:
    # Cell axes (mirror TrialParams).
    B: int
    T_max_s: float
    k: int = 3
    lambda_bg: int = 100
    N: int = 1024
    alpha: float = 0.5
    D: str = "matched"
    init: str = "cold"
    # Lens-(c)-specific.
    n_users: int = 30
    max_days: int = 7
    repertoire_size: int = 10
    # Top-1k = the finest popularity bucket CrUX exposes (see lens-c-plan
    # "Spec deviations"). Trace `rank` is a row-index from CrUX top-10k
    # filtered by resolvability — within a CrUX magnitude band, ordering
    # carries no popularity meaning.
    repertoire_bucket_max_rank: int = 1000
    # Day timeline: 10 pages × 30 min gaps with ±10% lognormal jitter on
    # each gap → ~5h active window, well within the 8h target in §8.3.
    page_gap_minutes: float = 30.0
    page_gap_jitter_sigma: float = 0.1
    # Per-page intra-page timing jitter (one ±10% lognormal multiplier per
    # page applied to all entry offsets in that page).
    intra_page_jitter_sigma: float = 0.1


# =====================================================================
# Logs
# =====================================================================

@dataclass
class UserDayLog:
    day_idx: int
    S_prime_total: set[str]
    n_commits: int
    n_victim_batches: int
    underflow_count: int
    # Count of victim queries that fell in underflow batches and were
    # therefore unobservable via S' (per §6.1: "no S' contribution").
    odoh_fallback_count: int


@dataclass
class UserResult:
    user_idx: int
    repertoire: list[str]   # site names (not full PageKeys)
    days: list[UserDayLog] = field(default_factory=list)


# =====================================================================
# Repertoire selection
# =====================================================================

def pick_repertoire(
    trace: Trace,
    *,
    user_seed: int,
    repertoire_size: int,
    bucket_max_rank: int,
) -> list[PageKey]:
    """Pick `repertoire_size` distinct sites with `rank ≤ bucket_max_rank`,
    returning one canonical PageKey per site (lowest run number, lowest
    day) as the replay source.
    """
    rng = random.Random(user_seed)
    canonical: dict[str, PageKey] = {}
    for pk in trace.pages.keys():
        rank, site, run, day = pk
        if rank > bucket_max_rank:
            continue
        cur = canonical.get(site)
        if cur is None or (run, day) < (cur[2], cur[3]):
            canonical[site] = pk
    if len(canonical) < repertoire_size:
        raise ValueError(
            f"only {len(canonical)} sites with rank ≤ {bucket_max_rank}; "
            f"need {repertoire_size} for repertoire"
        )
    sites_sorted = sorted(canonical.keys())
    chosen = rng.sample(sites_sorted, repertoire_size)
    return [canonical[s] for s in chosen]


# =====================================================================
# Synthetic-day generator
# =====================================================================

def synthesize_day(
    repertoire: list[PageKey],
    *,
    trace: Trace,
    rng: random.Random,
    page_gap_minutes: float,
    page_gap_jitter_sigma: float,
    intra_page_jitter_sigma: float,
) -> list[tuple[str, float]]:
    """Replay each repertoire page in randomized order with jittered timing.

    For each page i (in the day's reshuffled order):
        page_start_i = page_start_{i-1} + 30min × Lognormal(0, σ_gap)
        intra-page entries: t = page_start_i + entry.offset_ms × Lognormal(0, σ_intra)

    σ_gap = σ_intra = 0.1 → ±10% lognormal jitter per §8.3.

    Returns [(hostname, t_ms), ...] sorted by t_ms.
    """
    keys = list(repertoire)
    rng.shuffle(keys)
    gap_ms = page_gap_minutes * 60_000.0
    out: list[tuple[str, float]] = []
    page_start = 0.0
    for i, pk in enumerate(keys):
        if i > 0:
            page_start += gap_ms * rng.lognormvariate(0.0, page_gap_jitter_sigma)
        intra_factor = rng.lognormvariate(0.0, intra_page_jitter_sigma)
        for entry in trace.pages[pk]:
            t = page_start + entry.offset_ms * intra_factor
            out.append((entry.hostname, t))
    out.sort(key=lambda r: r[1])
    return out


# =====================================================================
# Per-(user, day) driver
# =====================================================================

def run_user_day(
    repertoire: list[PageKey],
    *,
    day_idx: int,
    params: LensCParams,
    trace: Trace,
    cover_universe: CoverUniverse,
    seed: int,
) -> UserDayLog:
    """Drive one (user, day) through a fresh enclave + cache + bg pool.

    Per sim-spec §8.3: each day gets a fresh `BackgroundUserPool` seed,
    fresh cover-sampling RNG stream, and (defaulted to) cold cache —
    matching the §5.3 maximally-pessimistic privacy setting.
    """
    if params.init == "warm":
        raise NotImplementedError("warm init not wired for lens (c)")
    if params.init != "cold":
        raise ValueError(params.init)

    rng = random.Random(seed)
    victim_entries = synthesize_day(
        repertoire,
        trace=trace, rng=rng,
        page_gap_minutes=params.page_gap_minutes,
        page_gap_jitter_sigma=params.page_gap_jitter_sigma,
        intra_page_jitter_sigma=params.intra_page_jitter_sigma,
    )
    if not victim_entries:
        raise ValueError("empty repertoire-day stream")

    last_t = victim_entries[-1][1]
    T_max_ms = params.T_max_s * 1000.0
    end_t = last_t + T_max_ms + 1.0

    cache = LRUCache(params.N)
    dist = make_distribution(params.D, cover_universe)
    cover_sampler = dist.bind(k=params.k, rng=rng)
    bb = BatchBuffer(
        cache, B=params.B, T_max=T_max_ms,
        sample_covers=cover_sampler,
    )

    bg = generate_bg_events(
        trace=trace, universe=cover_universe,
        n_users=params.lambda_bg, t_end_ms=end_t, rng=rng,
    )
    merged = _time_merge(victim_entries, bg)

    commits: list[Commit] = []
    last_event_t = 0.0
    for host, t, owner in merged:
        # Sparse-traffic time-trigger: same pattern as trial.run_trial —
        # if the gap to the next event exceeds T_max, fire `tick` at the
        # window boundary so t_commit doesn't drift.
        if bb.first_t is not None and (t - bb.first_t) >= bb.T_max:
            c = bb.tick(bb.first_t + bb.T_max)
            if c is not None:
                commits.append(c)
        c = bb.submit(host, t, owner)
        if c is not None:
            commits.append(c)
        last_event_t = t
    final = bb.flush(last_event_t + T_max_ms)
    if final is not None:
        commits.append(final)

    S_prime_total: set[str] = set()
    n_victim_batches = 0
    underflow_count = 0
    odoh_fallback_count = 0
    for c in commits:
        if c.victim_in_batch or c.victim_covers:
            S_prime_total |= set(c.S_prime)
            n_victim_batches += 1
        if c.underflow:
            underflow_count += 1
            odoh_fallback_count += len(c.dropped_victim_real)

    return UserDayLog(
        day_idx=day_idx,
        S_prime_total=S_prime_total,
        n_commits=len(commits),
        n_victim_batches=n_victim_batches,
        underflow_count=underflow_count,
        odoh_fallback_count=odoh_fallback_count,
    )


# =====================================================================
# Per-user driver (multi-day)
# =====================================================================

def run_user(
    user_idx: int,
    *,
    params: LensCParams,
    trace: Trace,
    cover_universe: CoverUniverse,
    base_seed: int,
    cell_tag: str = "",
) -> UserResult:
    """Run one user over `max_days` synthetic days.

    Stable seeding: `cell_tag` (e.g. cell hash) participates so re-running
    the same cell produces identical (user, day) streams.
    """
    user_seed = _seed("user", base_seed, cell_tag, user_idx)
    repertoire = pick_repertoire(
        trace, user_seed=user_seed,
        repertoire_size=params.repertoire_size,
        bucket_max_rank=params.repertoire_bucket_max_rank,
    )
    days: list[UserDayLog] = []
    for d in range(params.max_days):
        day_seed = _seed("day", base_seed, cell_tag, user_idx, d)
        days.append(run_user_day(
            repertoire, day_idx=d, params=params,
            trace=trace, cover_universe=cover_universe, seed=day_seed,
        ))
    return UserResult(
        user_idx=user_idx,
        repertoire=[pk[1] for pk in repertoire],
        days=days,
    )


# =====================================================================
# Helpers
# =====================================================================

def _time_merge(
    victim_entries: list[tuple[str, float]],
    bg: list[BgEvent],
):
    """Yield (host, t, owner) in non-decreasing t. Stable: bg before victim
    on tie (matches trial._time_merge convention)."""
    merged: list[tuple[float, int, str, str]] = []
    for h, t in victim_entries:
        merged.append((t, 1, h, "victim"))
    for ev in bg:
        merged.append((ev.t_ms, 0, ev.hostname, "bg"))
    merged.sort(key=lambda r: (r[0], r[1]))
    for t, _tier, host, owner in merged:
        yield host, t, owner


def _seed(*parts) -> int:
    """Stable 31-bit seed from arbitrary string/int parts. Uses blake2b
    rather than Python's randomized `hash()` so the same (cell, user, day)
    tuple always yields the same trial across processes / re-runs.
    """
    payload = "|".join(str(p) for p in parts).encode()
    return int.from_bytes(
        hashlib.blake2b(payload, digest_size=4).digest(), "big"
    ) & 0x7FFFFFFF
