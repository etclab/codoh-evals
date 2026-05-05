"""Unit tests for cache + enclave + attacker.

Covers:
  - LRU eviction and pre-cache suppression
  - size and time triggers, underflow path
  - failed-DNS sentinel is observable in S'
  - strong-attacker bg-real subtraction
  - candidate-set / ranking
  - lens (b) cross-batch union
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sim.attacker import candidate_set, evaluate, rank, score
from sim.cache import LRUCache
from sim.enclave import BatchBuffer, Commit, CoverEntry


# =====================================================================
# Cache
# =====================================================================

def test_cache_lru_eviction():
    c = LRUCache(3)
    new = c.add_all(["a", "b", "c"])
    assert new == ["a", "b", "c"]
    assert len(c) == 3
    new = c.add_all(["d"])
    assert new == ["d"]
    assert "a" not in c  # a was LRU; evicted
    assert {"b", "c", "d"} == set(c.snapshot())
    print("  ok: LRU evicts in insertion order")


def test_cache_pre_cache_suppression():
    c = LRUCache(10)
    c.add_all(["a", "b"])
    new = c.add_all(["a", "c", "b", "d"])
    assert new == ["c", "d"]  # a, b suppressed
    print("  ok: pre-cached entries suppressed by add_all")


# =====================================================================
# Enclave: triggers
# =====================================================================

def test_size_trigger():
    c = LRUCache(100)
    bb = BatchBuffer(c, B=3, T_max=1000.0)
    assert bb.submit("h1", 0.0, "victim") is None
    assert bb.submit("h2", 1.0, "victim") is None
    commit = bb.submit("h3", 2.0, "victim")
    assert commit is not None
    assert commit.B_eff == 3
    assert set(commit.S_prime) == {"h1", "h2", "h3"}
    assert not commit.underflow
    print(f"  ok: size trigger fires at B (B_eff={commit.B_eff})")


def test_time_trigger():
    c = LRUCache(100)
    bb = BatchBuffer(c, B=10, T_max=100.0, B_min=1)  # B_min=1 → no underflow on flush
    bb.submit("h1", 0.0, "victim")
    bb.submit("h2", 50.0, "bg")
    assert bb.tick(99.0) is None  # T_max not reached
    commit = bb.tick(101.0)
    assert commit is not None
    assert commit.B_eff == 2
    assert not commit.underflow
    print("  ok: time trigger fires at T_max")


def test_underflow():
    c = LRUCache(100)
    bb = BatchBuffer(c, B=5, T_max=100.0)  # B_min defaults to B
    bb.submit("h1", 0.0, "victim")
    bb.submit("h2", 5.0, "victim")
    commit = bb.tick(101.0)
    assert commit is not None
    assert commit.underflow
    assert commit.B_eff == 0
    assert commit.S_prime == []
    # Cache must NOT be updated on underflow.
    assert len(c) == 0
    print("  ok: underflow → ODoH fallback, no S', cache untouched")


def test_unique_pending_dedup():
    """B trigger counts UNIQUE pending hosts."""
    c = LRUCache(100)
    bb = BatchBuffer(c, B=2, T_max=1000.0)
    bb.submit("h1", 0.0, "victim")
    assert bb.submit("h1", 1.0, "victim") is None  # duplicate doesn't trip B
    commit = bb.submit("h2", 2.0, "victim")
    assert commit is not None
    assert commit.B_eff == 2
    print("  ok: size trigger uses unique-host count")


# =====================================================================
# Enclave: S' construction
# =====================================================================

def test_pre_cached_suppression_in_S_prime():
    c = LRUCache(100)
    c.add_all(["seen.com"])  # pre-existing cache state
    bb = BatchBuffer(c, B=2, T_max=1000.0)
    bb.submit("seen.com", 0.0, "victim")
    commit = bb.submit("new.com", 1.0, "victim")
    assert commit is not None
    assert "seen.com" not in commit.S_prime  # suppressed
    assert "new.com" in commit.S_prime
    assert "seen.com" in commit.pre_cached_suppressed
    print("  ok: pre-cached host suppressed from S'")


def test_strong_attacker_subtracts_bg():
    """S' = victim_real ∪ all_covers; bg_real subtracted."""
    c = LRUCache(100)
    bb = BatchBuffer(c, B=3, T_max=1000.0)
    bb.submit("v1", 0.0, "victim")
    bb.submit("b1", 1.0, "bg")
    commit = bb.submit("b2", 2.0, "bg")
    assert commit is not None
    assert "v1" in commit.S_prime
    assert "b1" not in commit.S_prime
    assert "b2" not in commit.S_prime
    print("  ok: strong attacker subtracts bg_real from S'")


def test_overlap_host_bg_first_then_victim():
    """If bg queries a host first and victim queries the same host later,
    the host is still victim_real and must NOT be subtracted from S'
    (strong-attacker invariant `S' ⊇ victim_real`). Catches the
    first-seen-owner bug.
    """
    c = LRUCache(100)
    bb = BatchBuffer(c, B=2, T_max=1000.0)
    assert bb.submit("h_overlap", 1.0, "bg") is None
    # Same host re-submitted by victim — same unique host, no size-trigger.
    assert bb.submit("h_overlap", 2.0, "victim") is None
    # Add a victim-only host to push to size trigger.
    commit = bb.submit("h_v", 3.0, "victim")
    assert commit is not None
    assert "h_overlap" in commit.S_prime, \
        "overlap host (bg-then-victim) was wrongly subtracted"
    assert "h_v" in commit.S_prime
    assert "h_overlap" in commit.victim_in_batch
    print("  ok: overlap host (bg then victim) survives subtraction")


def test_overlap_host_victim_first_then_bg():
    """Symmetric to the above — victim submits first, bg later. Result must
    be the same: overlap host stays in S'."""
    c = LRUCache(100)
    bb = BatchBuffer(c, B=2, T_max=1000.0)
    assert bb.submit("h_overlap", 1.0, "victim") is None
    assert bb.submit("h_overlap", 2.0, "bg") is None
    commit = bb.submit("h_b", 3.0, "bg")
    assert commit is not None
    # Strong attacker subtracts bg-only hosts: only h_b is bg-only.
    assert "h_overlap" in commit.S_prime
    assert "h_b" not in commit.S_prime
    assert "h_overlap" in commit.victim_in_batch
    print("  ok: overlap host (victim then bg) survives subtraction")


def test_failed_dns_observable_in_S_prime():
    """Failed-DNS rows (dns_ms=0) feed the batcher exactly like resolved
    rows: the query reaches the resolver in production, so the proxy sees
    it. The enclave doesn't see dns_ms — it only sees the hostname; this
    test stands in for the integration check that failed-DNS hostnames are
    observable to the attacker.
    """
    c = LRUCache(100)
    bb = BatchBuffer(c, B=2, T_max=1000.0)
    # Simulate a victim page where one query is a failed-DNS row.
    bb.submit("ad-tracker.example", 0.0, "victim")  # would be dns_ms=0 in trace
    commit = bb.submit("page.example", 100.0, "victim")
    assert commit is not None
    assert "ad-tracker.example" in commit.S_prime
    assert "page.example" in commit.S_prime
    print("  ok: failed-DNS host (modeled as plain submit) appears in S'")


def test_covers_remain_in_S_prime():
    """Covers stay sealed in the target→enclave bundle. They must appear
    in S' — the attacker has no visibility into per-query cover sets, so
    can't subtract them."""
    c = LRUCache(100)

    def sample(victim_real, bg_real):
        # one cover per real query, attributed to the same owner
        out = [CoverEntry(f"cover-v-{h}", "victim") for h in victim_real]
        out += [CoverEntry(f"cover-b-{h}", "bg") for h in bg_real]
        return out

    bb = BatchBuffer(c, B=2, T_max=1000.0, sample_covers=sample)
    bb.submit("v1", 0.0, "victim")
    commit = bb.submit("b1", 1.0, "bg")
    assert commit is not None
    # v1 stays (victim real); b1 subtracted (bg real); both covers stay.
    assert set(commit.S_prime) == {"v1", "cover-v-v1", "cover-b-b1"}
    assert commit.victim_covers == ["cover-v-v1"]
    assert commit.bg_covers == ["cover-b-b1"]
    print("  ok: covers (both victim and bg) survive in S'")


# =====================================================================
# Attacker
# =====================================================================

def test_score_and_candidate_set():
    Q = {"a": {"x", "y", "z"}, "b": {"y"}, "c": {"u", "v"}}
    Sp = {"x", "y"}
    assert score(Q["a"], Sp) == 2 / 3
    assert score(Q["b"], Sp) == 1.0
    assert score(Q["c"], Sp) == 0.0
    cands = set(candidate_set(Sp, Q, alpha=0.5))
    assert cands == {"a", "b"}
    cands = set(candidate_set(Sp, Q, alpha=0.7))
    assert cands == {"b"}
    print("  ok: score / candidate_set with α threshold")


def test_rank_and_evaluate():
    Q = {
        "victim": {"x", "y", "z"},
        "near":   {"x", "y", "w"},
        "far":    {"u", "v"},
    }
    res = evaluate({"x", "y", "z"}, Q, victim="victim", alpha=0.5)
    assert res.candidate_size == 2  # victim (3/3) and near (2/3)
    assert res.top1_hit
    assert res.top5_hit
    assert res.rr == 1.0
    print("  ok: evaluate computes top-1/top-5/MRR")


def test_time_trigger_under_sparse_traffic():
    """Under sparse traffic, the time trigger must fire at `first_t + T_max`,
    not at the next event's t. The trial loop is responsible for invoking
    `tick(first_t + T_max)` when the gap exceeds T_max — this test mirrors
    that pattern and asserts t_commit is at the window boundary, not later.
    """
    c = LRUCache(100)
    bb = BatchBuffer(c, B=10, T_max=1000.0, B_min=1)
    bb.submit("h1", 0.0, "victim")
    # Simulate a 50s gap to next event.
    next_t = 50_000.0
    assert bb.first_t == 0.0
    assert next_t - bb.first_t >= bb.T_max
    commit = bb.tick(bb.first_t + bb.T_max)  # fire at the boundary
    assert commit is not None
    assert commit.t_commit == 1000.0  # NOT 50000.0
    assert commit.B_eff == 1
    assert not commit.underflow
    # Buffer cleared; next event starts a fresh window.
    assert bb.first_t is None
    bb.submit("h2", next_t, "bg")
    assert bb.first_t == next_t
    print("  ok: time trigger fires at first_t+T_max under sparse traffic")


def test_no_alphabetical_tiebreak_bias():
    """Strict tie semantics — top-1 hit only if victim is the *unique* max.
    Two identical-score sites must not award top-1 to the alphabetically-
    earlier one. Asserted both ways: victim alpha-first, then alpha-last.
    """
    Q = {"aaaa-site": {"x"}, "zzzz-site": {"x"}}
    Sp = {"x"}
    res_a = evaluate(Sp, Q, victim="aaaa-site", alpha=0.5)
    res_z = evaluate(Sp, Q, victim="zzzz-site", alpha=0.5)
    assert not res_a.top1_hit and not res_z.top1_hit, "name-tiebreak leaked"
    assert res_a.top5_hit and res_z.top5_hit  # 2 tied, both fit in top-5
    assert res_a.n_tied_with_victim == 2
    assert res_a.n_strictly_greater == 0
    # Expected RR under uniform tiebreak: (1/1 + 1/2)/2 = 0.75 for both.
    assert abs(res_a.rr - 0.75) < 1e-9
    assert abs(res_z.rr - 0.75) < 1e-9
    print("  ok: tie produces no top-1 hit regardless of victim's name")


def test_unique_top1_still_hits():
    # other1/other2 have an extra non-S' host so their score is 0.5 < victim's 1.0
    Q = {"victim": {"x", "y", "z"},
         "other1": {"x", "u"},
         "other2": {"y", "v"}}
    res = evaluate({"x", "y", "z"}, Q, victim="victim", alpha=0.5)
    assert res.top1_hit and res.top5_hit
    assert res.n_strictly_greater == 0 and res.n_tied_with_victim == 1
    assert abs(res.rr - 1.0) < 1e-9
    print("  ok: unique max still counts as a top-1 hit")


def test_lens_b_cross_batch_union():
    """Lens (b): attacker unions S' across every batch the victim's
    queries touched."""
    c = LRUCache(100)
    bb = BatchBuffer(c, B=2, T_max=10000.0)
    Q = {"victim": {"v1", "v2", "v3", "v4"},
         "decoy":  {"v1", "x", "y", "z"}}
    # Victim's 4 queries spread across 2 batches.
    commits: list[Commit] = []
    out = bb.submit("v1", 0.0, "victim")
    out = bb.submit("v2", 1.0, "victim"); commits.append(out)
    out = bb.submit("v3", 2.0, "victim")
    out = bb.submit("v4", 3.0, "victim"); commits.append(out)
    assert all(c is not None for c in commits)
    S_total: set[str] = set()
    for c_ in commits:
        S_total |= set(c_.S_prime)
    res = evaluate(S_total, Q, victim="victim", alpha=0.5)
    assert res.top1_hit  # full Q_v recovered → score 1.0
    assert res.victim_score == 1.0
    print("  ok: lens-(b) cross-batch union recovers full Q_v")


if __name__ == "__main__":
    print("[test_enclave]")
    test_cache_lru_eviction()
    test_cache_pre_cache_suppression()
    test_size_trigger()
    test_time_trigger()
    test_underflow()
    test_unique_pending_dedup()
    test_pre_cached_suppression_in_S_prime()
    test_strong_attacker_subtracts_bg()
    test_overlap_host_bg_first_then_victim()
    test_overlap_host_victim_first_then_bg()
    test_failed_dns_observable_in_S_prime()
    test_covers_remain_in_S_prime()
    test_time_trigger_under_sparse_traffic()
    test_score_and_candidate_set()
    test_rank_and_evaluate()
    test_no_alphabetical_tiebreak_bias()
    test_unique_top1_still_hits()
    test_lens_b_cross_batch_union()
    print("all passed.")
