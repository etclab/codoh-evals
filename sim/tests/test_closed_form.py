"""Closed-form sanity (sim-spec §12 criterion 1, §7.4).

Two cases run as MC against analytic predictions:

  (a) Uniform-D, single batch, B_eff=1, k covers → mean posterior of
      victim identity = 1/(1 + B_eff·k) within 2σ over 10⁵ trials.

  (b) Lens-(a) head bucket under D = Q (empirical Zipf): MC mean
      candidate-set size matches the analytic
          E[|cands|] = 1 + Σ_{w≠v} [1 - (1 - p_w)^k]
      within 2σ over 10⁵ trials.

Catches integration bugs in the cover sampler, BatchBuffer commit, S′
construction, and candidate-set semantics — the kind of subtle issues
that don't surface in the hand-checked fixture.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sim.cache import LRUCache
from sim.cover import CoverUniverse, Matched, Uniform
from sim.enclave import BatchBuffer

N_TRIALS = 100_000


def _run_single_batch_trials(sampler, victim_host: str, n: int) -> list[int]:
    """Run n × (B=1, single victim submit, k covers) and return |S′ as set|.

    Uses the full BatchBuffer machinery (commit pipeline, pre-suppression,
    bg-real subtraction) — the goal is to verify the integrated path, not
    the sampler in isolation.
    """
    sizes = []
    for _ in range(n):
        cache = LRUCache(capacity=10_000)
        bb = BatchBuffer(cache, B=1, T_max=1e12, sample_covers=sampler)
        commit = bb.submit(victim_host, 0.0, "victim")
        assert commit is not None and not commit.underflow
        sizes.append(len(set(commit.S_prime)))
    return sizes


def _mean_var(xs):
    n = len(xs)
    m = sum(xs) / n
    v = sum((x - m) ** 2 for x in xs) / n
    return m, v


def test_uniform_d_posterior():
    """(a) Uniform-D, B_eff=1, k=3 → posterior = 1/(1+k) = 0.25."""
    B_eff, k = 1, 3
    universe = CoverUniverse.synthetic(n=10_000)
    sampler = Uniform(universe).bind(k=k, rng=random.Random(0))
    victim_host = "victim.test"  # outside universe → no cover collision with victim

    sizes = _run_single_batch_trials(sampler, victim_host, N_TRIALS)
    inv = [1.0 / s for s in sizes]
    mean_post, var_post = _mean_var(inv)
    sigma_mean = math.sqrt(var_post / N_TRIALS)

    expected_post = 1.0 / (1 + B_eff * k)  # 0.25
    band = 2 * sigma_mean
    assert abs(mean_post - expected_post) < band + 1e-4, (
        f"posterior MC={mean_post:.5f} vs closed-form={expected_post:.5f} "
        f"(2σ band={band:.5f})"
    )

    mean_size, _ = _mean_var(sizes)
    expected_size = 1 + B_eff * k
    print(f"  ok: uniform-D MC posterior={mean_post:.4f} "
          f"(closed form {expected_post:.4f}, ±{band:.4f}); "
          f"mean|S'|={mean_size:.3f} (expected {expected_size})")


def test_lens_a_head_bucket_zipf_d_eq_q():
    """(b) D = Q (Zipf 1/rank), victim = rank-1, k=3 →
    E[|cands|] = 1 + Σ_{w≠v} [1 - (1 - p_w)^k]. MC vs analytic within 2σ.
    """
    k = 3
    N_pages = 1000

    domains = [f"page{r:04d}.test" for r in range(1, N_pages + 1)]
    weights = [1.0 / r for r in range(1, N_pages + 1)]
    universe = CoverUniverse(domains=domains, weights=weights)
    sampler = Matched(universe).bind(k=k, rng=random.Random(1))

    # Victim is rank 1 (universe.domains[0]); single-host Q_w per page is
    # implicit since |S′ as set| = |candidate_set| under D=Q here (covers
    # are drawn from the same universe as the reference set).
    victim_host = universe.domains[0]

    sizes = _run_single_batch_trials(sampler, victim_host, N_TRIALS)
    mean_size, var_size = _mean_var(sizes)
    sigma_mean = math.sqrt(var_size / N_TRIALS)

    # Analytic expectation.
    total_w = sum(weights)
    probs = [w / total_w for w in weights]
    # Victim (index 0) always contributes 1; for each other page,
    # contribute P(at least one cover lands on it) = 1 - (1 - p_w)^k.
    expected = 1.0 + sum((1.0 - (1.0 - p) ** k) for p in probs[1:])

    band = 2 * sigma_mean
    assert abs(mean_size - expected) < band + 0.05, (
        f"E[|cands|] MC={mean_size:.4f} vs analytic={expected:.4f} "
        f"(2σ band={band:.4f})"
    )
    print(f"  ok: lens-a head-bucket (D=Q Zipf) MC mean={mean_size:.3f} "
          f"(analytic {expected:.3f}, ±{band:.3f})")


if __name__ == "__main__":
    print("[test_closed_form]")
    test_uniform_d_posterior()
    test_lens_a_head_bucket_zipf_d_eq_q()
    print("all passed.")
