"""Strong-attacker scoring.

Reference set is `{site: Q_w (set of hostnames)}`. Attacker observation is
either a single batch's `S'` or the cross-batch union for lens (b).
Score is set-based: |Q_w ∩ S'| / |Q_w|. Candidates are sites with score
≥ α. Ranking uses descending score, with stable ties broken by site name.
"""

from __future__ import annotations

from dataclasses import dataclass


def score(Q_w: set[str], S_prime: set[str]) -> float:
    if not Q_w:
        return 0.0
    return len(Q_w & S_prime) / len(Q_w)


def candidate_set(
    S_prime: set[str] | list[str],
    reference_set: dict[str, set[str]],
    alpha: float = 0.5,
) -> list[str]:
    S_set = S_prime if isinstance(S_prime, set) else set(S_prime)
    return [w for w, Q_w in reference_set.items()
            if score(Q_w, S_set) >= alpha]


def rank(
    S_prime: set[str] | list[str],
    reference_set: dict[str, set[str]],
) -> list[tuple[str, float]]:
    """Sort by descending score (stable). Within-tie order is **ambiguous**
    by construction — do not derive top-K from rank position. Use
    `evaluate()` for hit metrics, which counts ties explicitly.
    """
    S_set = S_prime if isinstance(S_prime, set) else set(S_prime)
    scored = [(w, score(Q_w, S_set)) for w, Q_w in reference_set.items()]
    scored.sort(key=lambda x: -x[1])
    return scored


@dataclass
class AttackResult:
    candidate_size: int
    top1_hit: bool
    top5_hit: bool
    rr: float           # expected reciprocal rank under uniform tiebreak
    victim_score: float
    n_strictly_greater: int   # `g` — sites with score > victim's
    n_tied_with_victim: int   # `t` — sites with score == victim's (incl. victim)


def evaluate(
    S_prime: set[str] | list[str],
    reference_set: dict[str, set[str]],
    victim: str,
    *,
    alpha: float = 0.5,
) -> AttackResult:
    """Compute candidate-set size + top-K hits + expected MRR.

    **Tie semantics (strict, name-bias-free):** top-K is a hit only when
    the victim is *guaranteed* in the top K under any tiebreak — i.e., the
    entire tie group containing the victim fits within the top K
    (`n_strictly_greater + n_tied ≤ K`). top-1 reduces to "victim is the
    unique highest scorer". MRR is the expected reciprocal rank under
    uniform random tiebreak: `(1/t) · Σ_{i=1..t} 1/(g+i)`.
    """
    S_set = S_prime if isinstance(S_prime, set) else set(S_prime)
    cands = candidate_set(S_set, reference_set, alpha)

    if victim not in reference_set:
        return AttackResult(
            candidate_size=len(cands), top1_hit=False, top5_hit=False,
            rr=0.0, victim_score=0.0,
            n_strictly_greater=0, n_tied_with_victim=0,
        )

    victim_score = score(reference_set[victim], S_set)
    g = 0
    t = 0
    for w, Q_w in reference_set.items():
        s = score(Q_w, S_set)
        if s > victim_score:
            g += 1
        elif s == victim_score:
            t += 1
    # Strict top-K: every tied position must lie within K.
    top1 = victim_score > 0.0 and g == 0 and t == 1
    top5 = victim_score > 0.0 and (g + t) <= 5
    if victim_score == 0.0 or t == 0:
        rr = 0.0
    else:
        rr = sum(1.0 / (g + i) for i in range(1, t + 1)) / t
    return AttackResult(
        candidate_size=len(cands),
        top1_hit=top1, top5_hit=top5, rr=rr,
        victim_score=victim_score,
        n_strictly_greater=g, n_tied_with_victim=t,
    )
