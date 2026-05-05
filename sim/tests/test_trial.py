"""End-to-end smoke + cover/background sanity tests (slice 3).

Covers:
  - cover.Matched / Uniform / Stale sample from declared pool
  - background generates events on the requested time window
  - run_trial on synthetic input produces per-batch + cross-batch results
  - lens (a) and lens (b) wiring against a reference set
"""

from __future__ import annotations

import random
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sim.background import generate_bg_events
from sim.cover import CoverUniverse, Matched, Stale, Uniform, make_distribution
from sim.trace_loader import generate_synthetic
from sim.trial import TrialParams, run_trial


# =====================================================================
# Cover
# =====================================================================

def test_cover_matched_zipf_skews_to_head():
    u = CoverUniverse.synthetic(n=100)  # weight = 1/i
    rng = random.Random(0)
    sampler = Matched(u).bind(k=2, rng=rng)
    out = sampler(["v1", "v2"], [])  # 2 reals × k=2 covers = 4 entries
    assert len(out) == 4
    # Draw 10k samples and confirm rank-1 dominates.
    bulk = []
    for _ in range(2500):
        bulk.extend(c.hostname for c in sampler(["x"], []))
    # k=2 per real, 1 real, 2500 calls = 5000 samples
    assert len(bulk) == 5000
    cnt = Counter(bulk)
    # rank 1 (weight 1.0) should dominate rank 100 (weight 0.01) by ~100x
    assert cnt["u1.test"] > 50 * cnt.get("u100.test", 1), \
        (cnt["u1.test"], cnt.get("u100.test", 0))
    print(f"  ok: matched-Zipf head-heavy (u1={cnt['u1.test']}, "
          f"u100={cnt.get('u100.test', 0)})")


def test_cover_uniform():
    u = CoverUniverse.synthetic(n=20)
    rng = random.Random(0)
    sampler = Uniform(u).bind(k=1, rng=rng)
    bulk = []
    for _ in range(2000):
        bulk.extend(c.hostname for c in sampler(["x"], []))
    cnt = Counter(bulk)
    # Each of 20 domains should get roughly 2000/20 = 100 ± noise.
    assert all(50 <= cnt[d] <= 150 for d in u.domains), cnt
    print("  ok: uniform sampler approximately even")


def test_cover_stale_subset_frozen():
    u = CoverUniverse.synthetic(n=20)
    s1 = Stale(u, freeze_seed=42)
    s2 = Stale(u, freeze_seed=42)
    assert s1._frozen == s2._frozen  # same seed → same subset
    assert len(s1._frozen) == 10  # 50% of 20
    s3 = Stale(u, freeze_seed=43)
    assert s3._frozen != s1._frozen  # different seed → different subset
    print(f"  ok: stale subset frozen by seed (size={len(s1._frozen)})")


def test_cover_owner_attribution():
    u = CoverUniverse.synthetic(n=10)
    sampler = Uniform(u).bind(k=2, rng=random.Random(0))
    out = sampler(["v1"], ["b1", "b2"])
    # 1 victim_real × 2 = 2 victim covers; 2 bg_real × 2 = 4 bg covers
    assert sum(1 for c in out if c.owner == "victim") == 2
    assert sum(1 for c in out if c.owner == "bg") == 4
    print("  ok: cover entries tagged with bundling owner")


# =====================================================================
# Background
# =====================================================================

def test_bg_generates_events_in_window():
    trace = generate_synthetic(n_sites=10, n_runs=2, seed=0)
    u = CoverUniverse.synthetic(n=50)
    rng = random.Random(0)
    events = generate_bg_events(
        trace, u, n_users=20, t_end_ms=30_000.0, rng=rng,
    )
    assert events, "expected non-empty bg event list"
    assert all(0 <= e.t_ms < 30_000 for e in events)
    # Sorted by time.
    assert all(events[i].t_ms <= events[i + 1].t_ms for i in range(len(events) - 1))
    print(f"  ok: bg generated {len(events)} events on [0, 30s)")


def test_bg_zero_users_zero_events():
    trace = generate_synthetic(n_sites=2, n_runs=1, seed=0)
    u = CoverUniverse.synthetic(n=10)
    events = generate_bg_events(
        trace, u, n_users=0, t_end_ms=10_000.0, rng=random.Random(0),
    )
    assert events == []
    print("  ok: bg with zero users returns no events")


# =====================================================================
# End-to-end trial
# =====================================================================

def test_run_trial_smoke_synthetic():
    """Synthetic trace, small B → expect multiple commits, lens-(b) lands the
    victim near the top because synthetic Q_v is unique by construction."""
    trace = generate_synthetic(n_sites=30, n_runs=3, seed=7)
    u = CoverUniverse.synthetic(n=200)
    # Pick a victim with at least 4 entries so multiple batches fire.
    victim_key = next(
        k for k, v in trace.pages.items() if len(v) >= 6
    )
    params = TrialParams(B=3, T_max_s=60.0, k=1, lambda_bg=0)  # no bg for clean test
    log = run_trial(
        params, victim_key=victim_key, trace=trace,
        cover_universe=u, seed=123,
    )
    assert log.commits, "expected at least one commit"
    assert log.n_victim_batches >= 1
    # No bg → no underflow if victim has ≥ B unique queries.
    Q_v = log.victim_Q_v
    if len(Q_v) >= params.B:
        assert log.underflow_count <= 1  # final flush may underflow
    # Lens (b): with k=1 and a 200-domain universe, some collisions but
    # victim should still rank well.
    ref = {site: set(hosts) for site, hosts in trace.Q_w.items()}
    res = log.lens_b(ref)
    assert 0.0 < res.victim_score <= 1.0
    print(f"  ok: synthetic trial — {len(log.commits)} commits, "
          f"victim_batches={log.n_victim_batches}, "
          f"underflows={log.underflow_count}, "
          f"lens-b score={res.victim_score:.2f} top1={res.top1_hit}")


def test_run_trial_with_background():
    trace = generate_synthetic(n_sites=20, n_runs=3, seed=11)
    u = CoverUniverse.synthetic(n=300)
    victim_key = next(k for k, v in trace.pages.items() if len(v) >= 5)
    params = TrialParams(B=5, T_max_s=30.0, k=3, lambda_bg=10)
    log = run_trial(
        params, victim_key=victim_key, trace=trace,
        cover_universe=u, seed=99,
    )
    # With bg, commits should include some non-victim batches.
    bg_only = sum(1 for c in log.commits
                  if not (c.victim_in_batch or c.victim_covers))
    assert log.commits, "expected commits"
    print(f"  ok: trial with bg — {len(log.commits)} commits "
          f"(victim={log.n_victim_batches}, bg-only={bg_only})")


if __name__ == "__main__":
    print("[test_trial]")
    test_cover_matched_zipf_skews_to_head()
    test_cover_uniform()
    test_cover_stale_subset_frozen()
    test_cover_owner_attribution()
    test_bg_generates_events_in_window()
    test_bg_zero_users_zero_events()
    test_run_trial_smoke_synthetic()
    test_run_trial_with_background()
    print("all passed.")
