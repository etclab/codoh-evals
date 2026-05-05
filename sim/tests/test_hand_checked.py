"""Hand-checked fixture.

5 pages, 3 trials. Every expected `S'` is computed by hand below — if the
simulator's output drifts, the asserts fail. At least one trial must put
a `dns_ms=0` host inside the victim page; that host must appear in S'
(failed-DNS rows are first-class observables — the query reaches the
resolver in production, so the proxy/adversary sees it).

Reference set (Q_w):
  P1: {a, b, c}     (overlaps P2 on {b,c})
  P2: {b, c, d}
  P3: {e, f}
  P4: {g, h, i, j}
  P5: {nx, k}        ← `nx` is a failed-DNS row (dns_ms = 0)
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sim.attacker import candidate_set, evaluate
from sim.cache import LRUCache
from sim.enclave import BatchBuffer
from sim.trace_loader import Entry, Trace


# =====================================================================
# Fixture
# =====================================================================

def _make_fixture() -> Trace:
    """5 pages, 1 run each. Hand-built so expected outputs are obvious."""
    pages = {
        (1, "P1", 1, "d1"): [Entry("a", 0.0, 5.0), Entry("b", 100.0, 5.0),
                              Entry("c", 200.0, 5.0)],
        (2, "P2", 1, "d1"): [Entry("b", 0.0, 5.0), Entry("c", 100.0, 5.0),
                              Entry("d", 200.0, 5.0)],
        (3, "P3", 1, "d1"): [Entry("e", 0.0, 5.0), Entry("f", 100.0, 5.0)],
        (4, "P4", 1, "d1"): [Entry("g", 0.0, 5.0), Entry("h", 100.0, 5.0),
                              Entry("i", 200.0, 5.0), Entry("j", 300.0, 5.0)],
        # nx is the failed-DNS row (dns_ms == 0); k is normal.
        (5, "P5", 1, "d1"): [Entry("nx", 0.0, 0.0), Entry("k", 50.0, 5.0)],
    }
    Q_w = {site: {e.hostname for e in entries} for (_r, site, _run, _d), entries in pages.items()}
    return Trace(
        pages=pages, Q_w=Q_w,
        site_rank={"P1": 1, "P2": 2, "P3": 3, "P4": 4, "P5": 5},
        days={"d1"}, runs={1},
    )


def _reference_set(trace: Trace) -> dict[str, set[str]]:
    return {site: set(hs) for site, hs in trace.Q_w.items()}


# =====================================================================
# Trial helpers — drive the BatchBuffer directly (no covers, no bg, no
# random) so expected S' is fully deterministic.
# =====================================================================

def _drive(victim_entries, *, B: int, T_max_ms: float, cache: LRUCache):
    """Submit victim entries through a BatchBuffer with no covers / bg.
    Tick at every event time + flush at end. Returns list of Commit."""
    bb = BatchBuffer(cache, B=B, T_max=T_max_ms)
    commits = []
    last_t = 0.0
    for e in victim_entries:
        # Sparse-traffic safe: fire time trigger at first_t+T_max if expired,
        # not at the (later) next event time. Mirrors trial.run_trial.
        if bb.first_t is not None and (e.offset_ms - bb.first_t) >= bb.T_max:
            c = bb.tick(bb.first_t + bb.T_max)
            if c is not None:
                commits.append(c)
        c = bb.submit(e.hostname, e.offset_ms, "victim")
        if c is not None:
            commits.append(c)
        last_t = e.offset_ms
    final = bb.flush(last_t + T_max_ms)
    if final is not None:
        commits.append(final)
    return commits


# =====================================================================
# Trial 1 — failed-DNS host visible in S'
# =====================================================================

def test_trial1_failed_dns_observable():
    trace = _make_fixture()
    ref = _reference_set(trace)
    P5_entries = trace.pages[(5, "P5", 1, "d1")]

    # B=2, both nx and k in one batch. Cache empty.
    cache = LRUCache(100)
    commits = _drive(P5_entries, B=2, T_max_ms=1000.0, cache=cache)

    # Hand-computed:
    #   batch 0: pending {nx, k}; size trigger fires; new_entries=[nx,k];
    #            S = [nx, k]; bg_real = []; S' = [nx, k].
    #   final flush: nothing pending → no commit.
    assert len(commits) == 1, f"expected 1 commit, got {len(commits)}"
    c = commits[0]
    assert not c.underflow
    assert c.B_eff == 2
    assert sorted(c.S_prime) == ["k", "nx"]
    assert "nx" in c.S_prime, "failed-DNS host must be observable"
    # candidate set / ranking against full reference: only P5 = {nx, k} hits 1.0.
    res = evaluate(c.S_prime, ref, victim="P5", alpha=0.5)
    assert res.candidate_size == 1
    assert res.top1_hit
    assert res.victim_score == 1.0
    print("  ok: trial 1 — nx (dns_ms=0) appears in S', P5 uniquely identified")


# =====================================================================
# Trial 2 — pre-cached suppression hides a victim host
# =====================================================================

def test_trial2_pre_cached_hides_host():
    trace = _make_fixture()
    ref = _reference_set(trace)
    P1_entries = trace.pages[(1, "P1", 1, "d1")]

    # Cache pre-warmed with {a}. Victim = P1, B=2.
    cache = LRUCache(100)
    cache.add_all(["a"])
    commits = _drive(P1_entries, B=2, T_max_ms=1000.0, cache=cache)

    # Hand-computed:
    #   submit a (pending {a}, count=1); submit b (pending {a,b}, count=2 → trigger).
    #     inserts = [a, b]; a pre-cached → suppressed → new_entries = [b];
    #     S = [b]; S' = [b]. pre_cached_suppressed = ["a"].
    #   submit c (pending {c}, count=1).
    #   flush at last_t + T_max = 200 + 1000 = 1200, pending count=1 < B=2 → underflow.
    assert len(commits) == 2
    c0, c1 = commits
    assert sorted(c0.S_prime) == ["b"]
    assert c0.pre_cached_suppressed == ["a"]
    assert c0.B_eff == 2
    assert c1.underflow
    assert c1.B_eff == 0
    assert c1.S_prime == []
    # Lens-(b) cross-batch union from victim batches = {b}. P1 and P2 tie at 1/3;
    # alphabetical tiebreak picks P1 → top-1 hit.
    S_total = set(c0.S_prime) | set(c1.S_prime)
    res = evaluate(S_total, ref, victim="P1", alpha=0.5)
    assert res.candidate_size == 0           # 1/3 < 0.5
    assert not res.top1_hit                  # P1 ties P2 at 1/3 → ambiguous
    assert res.top5_hit                      # only 2 tied at top → guaranteed top-5
    assert res.n_strictly_greater == 0
    assert res.n_tied_with_victim == 2
    # Expected RR under uniform tiebreak over {P1, P2}: (1/1 + 1/2) / 2 = 0.75
    assert abs(res.rr - 0.75) < 1e-9
    assert abs(res.victim_score - 1.0 / 3.0) < 1e-9
    print("  ok: trial 2 — pre-cached `a` suppressed; tied score, no top-1 bias")


# =====================================================================
# Trial 3 — large page sliced across batches (lens-b cross-batch union)
# =====================================================================

def test_trial3_lens_b_cross_batch():
    trace = _make_fixture()
    ref = _reference_set(trace)
    P4_entries = trace.pages[(4, "P4", 1, "d1")]

    cache = LRUCache(100)
    # B=3, T_max big enough that only the size trigger fires. Page emits
    # 4 unique hosts → 1 size-triggered batch + 1 underflow on flush.
    commits = _drive(P4_entries, B=3, T_max_ms=10_000.0, cache=cache)

    # Hand-computed:
    #   submits g, h, i → trigger at i. inserts=[g,h,i], cache empty;
    #     new=[g,h,i]; S' = [g,h,i].
    #   submit j → pending {j}, count=1.
    #   flush → underflow (1 < B=3).
    assert len(commits) == 2
    c0, c1 = commits
    assert sorted(c0.S_prime) == ["g", "h", "i"]
    assert not c0.underflow
    assert c1.underflow

    # Lens-(a) per-batch on c0: score(P4) = 3/4 = 0.75 ≥ α=0.5 → in candidate set.
    res_a = evaluate(c0.S_prime, ref, victim="P4", alpha=0.5)
    assert res_a.candidate_size == 1
    assert res_a.top1_hit
    assert abs(res_a.victim_score - 0.75) < 1e-9

    # Lens-(b) cross-batch union: still {g,h,i} (underflow batch contributes
    # nothing). Identical to lens-(a) here.
    S_total = set(c0.S_prime) | set(c1.S_prime)
    res_b = evaluate(S_total, ref, victim="P4", alpha=0.5)
    assert res_b.victim_score == 0.75
    print("  ok: trial 3 — P4 sliced into 1 commit + underflow flush, lens-b 0.75")


if __name__ == "__main__":
    print("[test_hand_checked]")
    test_trial1_failed_dns_observable()
    test_trial2_pre_cached_hides_host()
    test_trial3_lens_b_cross_batch()
    print("all passed.")
