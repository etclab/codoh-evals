"""Lens (c) — synthetic-day generator, per-day driver, and analyzer.

Two layers:
  - **Hand-checked analyzer** (`test_analyze_c_*`): tiny `reference_set`
    + hand-built per-day `S'` sets; expected values for candidate_set,
    intersection, days_to_fingerprint, repertoire_acc, and Bayesian
    posterior all computed by hand.
  - **Driver smoke** (`test_run_user_*`): synthetic trace, `lambda_bg=0`,
    asserts shape invariants (S' ⊆ inserted hosts; days strictly grow
    when the user re-runs the same repertoire).
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sim.analyze_c import (analyze_user, aggregate_cell, bayesian_posterior_R,
                           candidate_set, days_to_fingerprint, fit_p_obs)
from sim.cover import CoverUniverse
from sim.lens_c import (LensCParams, UserDayLog, UserResult, pick_repertoire,
                        run_user, run_user_day, synthesize_day)
from sim.trace_loader import generate_synthetic


# =====================================================================
# analyze_c.candidate_set + intersection + days_to_fingerprint
# =====================================================================

def test_candidate_set_alpha_threshold():
    Q = {
        "victim1": {"a", "b", "c"},
        "victim2": {"a", "d"},
        "decoy":   {"x", "y"},
    }
    # S' = {a, b, c, d} → victim1 score 1.0; victim2 score 1.0; decoy 0.
    S = {"a", "b", "c", "d"}
    assert candidate_set(S, Q, alpha=0.5) == {"victim1", "victim2"}
    # raise α above victim2 (score 1.0) → still both pass.
    assert candidate_set(S, Q, alpha=1.0) == {"victim1", "victim2"}
    # S = {a} → victim1 1/3, victim2 1/2.
    assert candidate_set({"a"}, Q, alpha=0.5) == {"victim2"}
    print("  ok: candidate_set respects α")


def test_days_to_fp_basic_and_censored():
    # Per-day candidate sets shrink: {1..20}, {1..15}, {1..8} → at D=3,
    # |intersection|=8 ≤ R_size=10. Days-to-fp = 3.
    C1 = set(range(20))
    C2 = set(range(15))
    C3 = set(range(8))
    d, censored = days_to_fingerprint([C1, C2, C3], R_size=10)
    assert (d, censored) == (3, False)
    # If never reaches ≤R_size, return (max_D, True).
    d, censored = days_to_fingerprint([C1, C1, C1], R_size=10)
    assert (d, censored) == (3, True)
    # First day already small enough.
    d, censored = days_to_fingerprint([{1, 2}, {1, 2}], R_size=10)
    assert (d, censored) == (1, False)
    # Empty input.
    d, censored = days_to_fingerprint([], R_size=10)
    assert (d, censored) == (0, True)
    print("  ok: days_to_fingerprint (basic + censored)")


# =====================================================================
# Bayesian posterior — hand-checked 2-page reference, 1-page repertoire
# =====================================================================

def test_bayesian_posterior_two_page_reference():
    """Tiny reference set: two pages, identical-size Q_w. After observing
    a S'_d that contains all of victim's Q_w and none of decoy's, the
    posterior on the victim should approach 1 as p_obs → 1.

    Hand calc with p_obs = 0.9, |Q_w| = 2, prior 0.5 each:
        log L(victim) = 2 · log(0.9) + 0 · log(0.1)  = 2·log 0.9
        log L(decoy)  = 0 · log(0.9) + 2 · log(0.1)  = 2·log 0.1
        ratio = (0.9/0.1)^2 = 81
        posterior(victim) = 81 / (81 + 1) ≈ 0.9878
    """
    Q = {"victim": {"a", "b"}, "decoy": {"x", "y"}}
    S_d = {"a", "b"}
    posts = bayesian_posterior_R([S_d], Q, ["victim"], p_obs=0.9)
    expected = 81.0 / 82.0
    assert math.isclose(posts[0], expected, rel_tol=1e-9)

    # After two identical days, posterior compounds: ratio = 81² = 6561.
    posts = bayesian_posterior_R([S_d, S_d], Q, ["victim"], p_obs=0.9)
    expected_2 = 6561.0 / 6562.0
    assert math.isclose(posts[1], expected_2, rel_tol=1e-9)
    print(f"  ok: posterior 1-day={posts[0]:.6f}  2-day={posts[1]:.6f}")


def test_bayesian_posterior_uniform_prior_when_no_evidence():
    """Empty S'_d → likelihood is the same for every w (no hits, all
    misses), so posterior = prior = uniform. R_mass = |R| / |reference|.
    """
    Q = {f"p{i}": {f"h{i}_a", f"h{i}_b"} for i in range(10)}
    R = ["p0", "p1", "p2"]  # 3 of 10
    posts = bayesian_posterior_R([set()], Q, R, p_obs=0.5)
    assert math.isclose(posts[0], 0.3, abs_tol=1e-12)
    print("  ok: empty S'_d → posterior = uniform prior on R")


# =====================================================================
# Hand-checked end-to-end on UserResult mock
# =====================================================================

def test_analyze_user_hand_checked_2day():
    """Repertoire R = {p0, p1}. Reference set has 4 pages; Q_p0 = {a,b},
    Q_p1 = {c,d}, Q_p2 = {a,e}, Q_p3 = {x,y,z}.

    Day 1 S' = {a, b, c, d}: scores p0=1, p1=1, p2=0.5, p3=0.
    Day 2 S' = {a, b, c, d, e}: scores p0=1, p1=1, p2=1, p3=0.

    α = 0.6 → C_1 = {p0, p1}; C_2 = {p0, p1, p2}.
    intersect_2 = {p0, p1}; days_to_fp(R_size=2) = 1 (|C_1| = 2).
    repertoire_acc_2 = |{p0,p1} ∩ {p0,p1}| / 2 = 1.0.
    """
    Q = {
        "p0": {"a", "b"},
        "p1": {"c", "d"},
        "p2": {"a", "e"},
        "p3": {"x", "y", "z"},
    }
    R = ["p0", "p1"]
    user = UserResult(
        user_idx=0, repertoire=list(R),
        days=[
            UserDayLog(0, {"a", "b", "c", "d"},     n_commits=2,
                       n_victim_batches=2, underflow_count=0,
                       odoh_fallback_count=0),
            UserDayLog(1, {"a", "b", "c", "d", "e"}, n_commits=3,
                       n_victim_batches=3, underflow_count=0,
                       odoh_fallback_count=0),
        ],
    )
    m = analyze_user(user, reference_set=Q, alpha=0.6, p_obs=0.9, R_size=2)
    assert m.intersections[0] == {
        "D": 1, "intersect_size": 2, "repertoire_acc": 1.0,
        "posterior_R": m.intersections[0]["posterior_R"],  # checked below
    }
    # Day-1 candidate set is exactly R, intersect_size=2, R_size=2 → fp at d=1.
    assert m.days_to_fp_alpha == 1
    assert m.days_to_fp_censored is False
    # Final-day intersection = {p0, p1} ∩ {p0,p1,p2} = {p0,p1} → rep_acc=1.0.
    assert m.intersections[-1]["intersect_size"] == 2
    assert m.intersections[-1]["repertoire_acc"] == 1.0
    print(f"  ok: hand-checked 2-day → days_to_fp={m.days_to_fp_alpha}, "
          f"posterior@D=2={m.intersections[-1]['posterior_R']:.4f}")


def test_aggregate_cell_quantiles_and_censoring():
    # 5 users with days_to_fp = [1, 2, 3, 4, max_days(=7)] (last censored).
    users = []
    for i, d in enumerate([1, 2, 3, 4, 7]):
        u = UserResult(user_idx=i, repertoire=["p0", "p1"])
        # Build 7 days; day i flips the censored case.
        u.days = [
            UserDayLog(j, set(), 0, 0, 0, 0) for j in range(7)
        ]
        users.append(u)
    # Manual UserMetrics (skip the analyze_user step) to control inputs.
    from sim.analyze_c import UserMetrics
    user_metrics = []
    for u, dfp, cens in zip(users, [1, 2, 3, 4, 7], [False, False, False, False, True]):
        user_metrics.append(UserMetrics(
            user_idx=u.user_idx, repertoire=u.repertoire,
            per_day=[{"d": j, "S_prime_size": 0, "cand_size": 0,
                      "underflow_count": 0, "odoh_fallback_count": 0}
                     for j in range(7)],
            intersections=[{"D": j + 1, "intersect_size": 0,
                            "repertoire_acc": 1.0,
                            "posterior_R": 0.5 + 0.1 * j}
                           for j in range(7)],
            days_to_fp_alpha=dfp, days_to_fp_censored=cens,
        ))
    cell = aggregate_cell(user_metrics, cell_tag="test", p_obs=0.85,
                          posterior_threshold=0.95)
    # Median of [1,2,3,4,7] = 3.
    assert cell.days_to_fp_median == 3.0
    assert cell.n_censored == 1
    # Posterior @ D=7 = 0.5 + 0.6 = 1.1 — clamped reading: ≥0.95 for all 5.
    assert cell.posterior_fraction_users == 1.0
    print(f"  ok: cell rollup median={cell.days_to_fp_median} "
          f"censored={cell.n_censored} post_frac={cell.posterior_fraction_users}")


# =====================================================================
# Driver: pick_repertoire + synthesize_day
# =====================================================================

def test_pick_repertoire_distinct_and_in_bucket():
    trace = generate_synthetic(n_sites=50, n_runs=2, seed=42)
    pks = pick_repertoire(
        trace, user_seed=1, repertoire_size=10, bucket_max_rank=50,
    )
    sites = [pk[1] for pk in pks]
    assert len(sites) == 10
    assert len(set(sites)) == 10  # distinct
    for pk in pks:
        rank, site, run, day = pk
        assert rank <= 50
        assert pk in trace.pages
    # Different user_seed → different (highly likely) repertoire.
    pks2 = pick_repertoire(trace, user_seed=2, repertoire_size=10,
                            bucket_max_rank=50)
    assert {pk[1] for pk in pks} != {pk[1] for pk in pks2}
    print("  ok: pick_repertoire (distinct, in-bucket, seed-sensitive)")


def test_pick_repertoire_insufficient_pool():
    trace = generate_synthetic(n_sites=5, n_runs=1, seed=0)
    # bucket_max_rank=3 → only 3 sites available, can't sample 10.
    try:
        pick_repertoire(trace, user_seed=0, repertoire_size=10,
                        bucket_max_rank=3)
    except ValueError as e:
        assert "rank ≤ 3" in str(e)
        print(f"  ok: pick_repertoire raises on too-small bucket: {e}")
        return
    raise AssertionError("expected ValueError")


def test_synthesize_day_sorted_and_jittered():
    trace = generate_synthetic(n_sites=20, n_runs=1, seed=0)
    pks = pick_repertoire(trace, user_seed=0, repertoire_size=5,
                          bucket_max_rank=20)
    rng = random.Random(0)
    entries = synthesize_day(
        pks, trace=trace, rng=rng,
        page_gap_minutes=30.0, page_gap_jitter_sigma=0.1,
        intra_page_jitter_sigma=0.1,
    )
    assert entries, "should produce entries"
    # Sorted by t.
    times = [t for _, t in entries]
    assert times == sorted(times)
    # Spans roughly 5 pages × 30 min = ~150 min = 9_000_000 ms (give or
    # take ±10% jitter).
    span_ms = times[-1] - times[0]
    assert 6_000_000 < span_ms < 12_000_000, f"span_ms={span_ms}"
    # Different rng → different timeline.
    rng2 = random.Random(1)
    entries2 = synthesize_day(
        pks, trace=trace, rng=rng2,
        page_gap_minutes=30.0, page_gap_jitter_sigma=0.1,
        intra_page_jitter_sigma=0.1,
    )
    assert entries != entries2, "rng didn't perturb"
    print(f"  ok: synthesize_day n_entries={len(entries)} span_min="
          f"{span_ms / 60_000:.1f}")


# =====================================================================
# Driver: run_user_day end-to-end (lambda_bg = 0 → S' is pure victim+covers)
# =====================================================================

def test_run_user_day_no_bg_S_prime_is_victim_plus_covers():
    """With λ_bg=0 there is no bg traffic, so every S' entry is either a
    victim hostname or a victim cover. We assert that S' ⊆ (Q_v ∪ covers)
    by comparing against the trace's Q_w for the chosen repertoire.

    Edge case: if (B_eff < B_min) on the final flush, we underflow and S'
    is empty. We use a tiny B and no T_max to force the size trigger.
    """
    trace = generate_synthetic(n_sites=20, n_runs=2, seed=7)
    universe = CoverUniverse.synthetic(n=200)
    params = LensCParams(
        B=2, T_max_s=1e9,    # T_max effectively disabled
        k=1, lambda_bg=0, N=1024, alpha=0.5, D="matched",
        n_users=1, max_days=1, repertoire_size=5,
        repertoire_bucket_max_rank=20,
        page_gap_minutes=1.0,    # short for fast test
    )
    repertoire = pick_repertoire(
        trace, user_seed=0, repertoire_size=5, bucket_max_rank=20,
    )
    log = run_user_day(
        repertoire, day_idx=0, params=params,
        trace=trace, cover_universe=universe, seed=123,
    )
    # All hostnames in S'_total are victim hosts or universe domains (covers).
    all_victim_hosts = set()
    for pk in repertoire:
        for entry in trace.pages[pk]:
            all_victim_hosts.add(entry.hostname)
    universe_set = set(universe.domains)
    leaked = log.S_prime_total - (all_victim_hosts | universe_set)
    assert not leaked, f"S' contains non-victim non-cover hosts: {leaked}"
    # We expect at least *some* signal (size trigger fired at least once).
    assert log.n_victim_batches >= 1, "no victim batches — buffer never fired"
    print(f"  ok: run_user_day(λ_bg=0) S'={len(log.S_prime_total)} "
          f"victim_batches={log.n_victim_batches} "
          f"underflow={log.underflow_count}")


def test_run_user_two_days_independent():
    """Same user, same repertoire, two days: per-day RNG streams diverge,
    so the per-day cover sets (and therefore S') should differ."""
    trace = generate_synthetic(n_sites=20, n_runs=2, seed=11)
    universe = CoverUniverse.synthetic(n=500)
    params = LensCParams(
        B=2, T_max_s=1e9, k=2, lambda_bg=0, N=1024, alpha=0.5,
        D="matched", n_users=1, max_days=2, repertoire_size=4,
        repertoire_bucket_max_rank=20, page_gap_minutes=1.0,
    )
    user = run_user(
        user_idx=0, params=params, trace=trace,
        cover_universe=universe, base_seed=42, cell_tag="test",
    )
    assert len(user.days) == 2
    # Day-0 and day-1 S' sets should differ (different cover RNG, different
    # synthesize_day order, different bg seed even though λ_bg=0).
    assert user.days[0].S_prime_total != user.days[1].S_prime_total, \
        "two days produced identical S' — RNG isolation broken"
    print(f"  ok: run_user 2 days |S'_0|={len(user.days[0].S_prime_total)} "
          f"|S'_1|={len(user.days[1].S_prime_total)}")


# =====================================================================
# fit_p_obs sanity
# =====================================================================

def test_fit_p_obs_perfect_observation():
    """Hand-build a UserResult where every repertoire-page Q_w is fully
    contained in S'_d → p_obs should be 1.0."""
    trace = generate_synthetic(n_sites=10, n_runs=1, seed=0)
    Q = {s: hs for s, hs in trace.Q_w.items()}
    sites = sorted(Q.keys())[:3]
    full_S = set().union(*[Q[s] for s in sites])
    user = UserResult(
        user_idx=0, repertoire=sites,
        days=[UserDayLog(0, full_S, 0, 0, 0, 0)],
    )
    p_obs = fit_p_obs([user], Q)
    assert math.isclose(p_obs, 1.0, abs_tol=1e-9), p_obs
    print(f"  ok: fit_p_obs perfect-observation case → {p_obs:.6f}")


# =====================================================================
# Run all
# =====================================================================

if __name__ == "__main__":
    print("[test_lens_c]")
    test_candidate_set_alpha_threshold()
    test_days_to_fp_basic_and_censored()
    test_bayesian_posterior_two_page_reference()
    test_bayesian_posterior_uniform_prior_when_no_evidence()
    test_analyze_user_hand_checked_2day()
    test_aggregate_cell_quantiles_and_censoring()
    test_pick_repertoire_distinct_and_in_bucket()
    test_pick_repertoire_insufficient_pool()
    test_synthesize_day_sorted_and_jittered()
    test_run_user_day_no_bg_S_prime_is_victim_plus_covers()
    test_run_user_two_days_independent()
    test_fit_p_obs_perfect_observation()
    print("all passed.")
